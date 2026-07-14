# Check engine: single async worker with a DB-driven tick loop

The check engine is **one long-running asyncio process**. It runs a **tick loop** (waking on a short, fixed interval — e.g. every 5 seconds) that queries the DB for **Monitors whose `next_check_at <= now`**, spawns an asyncio task to run each due **Check**, and on completion advances `next_check_at = now + monitor.interval`. Concurrency is bounded by a global semaphore (with headroom reserved for the web/API process on the same host).

Why:

- **Scales to the launch envelope on one box.** Tens of thousands of checks are I/O-bound network waits; `asyncio` + `httpx`/raw sockets holds that depth comfortably. A single process is dramatically simpler to operate than a distributed queue.
- **State is in the DB, not in memory.** On restart, redeploy, or crash, the worker simply resumes reading `next_check_at` from Postgres — no in-memory job table to rehydrate, no double-fire window to reconcile. A monitor added/paused/deleted via the API is reflected on the next tick with zero coordination.
- **No mandatory Redis.** Keeping the launch stack to {FastAPI, Postgres/Timescale, the shared Telegram bot} minimizes moving parts. We can add a queue later only if we outgrow a single worker, by sharding monitors across worker processes keyed by a hash of monitor id.

Rejected:

- **Per-monitor in-memory timers** (one APScheduler `add_job` per monitor): precise, but splits scheduling state between the DB and RAM, requiring rehydration logic and live scheduler mutation on every config change.
- **Distributed queue (Redis + arq/Celery):** horizontally scalable and crash-resilient, but introduces Redis as a hard dependency and per-check queue overhead the launch scale does not justify.
- **External cron triggers:** wrong fit for sub-minute intervals and high monitor cardinality.

The unit of horizontal scaling, when needed, is **the worker process**: shard the monitor space (e.g. `monitor.id % N == k`) so N workers each own a disjoint partition and run the same tick loop independently.
