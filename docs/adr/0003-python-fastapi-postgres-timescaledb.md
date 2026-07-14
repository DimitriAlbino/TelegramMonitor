# Stack: Python/FastAPI + Postgres + TimescaleDB

We will build the service in **Python**, using **FastAPI** for the HTTP layer (web API + optional status pages), with **async** check execution throughout. Relational state — users, monitors, incidents, reports — lives in **Postgres**. The high-volume stream of check results is stored in a **TimescaleDB** hypertable (a Postgres extension) on the same database, giving us time-series compression and retention policies without running a second datastore.

Why:

- The service is fundamentally *a lot of concurrent outbound network probes* (HTTP/TCP). Python's async ecosystem (`httpx`, `asyncio`) handles this well and matches the lineage of the reference `telegram-status-alerting.md` alerter (Python stdlib).
- FastAPI gives typed request/response models, automatic OpenAPI, and async-native handlers — good for a config-heavy SaaS API.
- Check results are append-only timestamped rows written at high frequency. TimescaleDB's hypertables handle that workload (partitioning by time, automatic compression, drop-chunk retention) far better than a plain Postgres table, while staying inside the same Postgres instance we already operate.
- One datastore = one backup story, one migration tool (Alembic), one set of credentials.

Rejected:

- **Node/TS + Fastify + BullMQ**: also viable and matches part of the reference doc, but splits the codebase across two languages for no gain, and BullMQ adds a mandatory Redis dependency we'd otherwise avoid.
- **Go**: best raw throughput, but the polling scale we target at launch does not justify Go's verbosity and thinner ORM/migration ecosystem.
- **Separate TSDB (InfluxDB/Prometheus)**: over-engineering for launch; TimescaleDB keeps everything in one engine.
