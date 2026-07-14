# Service Health Monitoring + Telegram Alerting — Technical Reference

A small, dependency-light pattern for **watching a self-hosted service and pushing Telegram
messages when it goes down and again when it recovers** — plus a public status endpoint, a
human-readable status page, and self-healing. Built for a Dockerized Node + Python + Postgres
stack, but the design is stack-agnostic and reusable. Zero recurring cost.

> This document is a generalized extract of a working solution, intended as a starting point for a
> new project. Copy the code blocks and swap the marked project-specific bits.

---

## 1. What it gives you

1. **A truthful health signal** that separates *"is the process alive?"* from *"is the data/work
   fresh?"* — so you don't get paged for a quiet period or an upstream outage that isn't your fault.
2. **A JSON status endpoint** (`/v1/status`) for machine consumers / external uptime monitors.
3. **A human status dashboard** (`/status`) — one glance tells you what's wrong.
4. **Self-healing** — a watchdog restarts a hung worker automatically.
5. **Telegram alerts** — an independent watcher pages you on `down` and `recovery`, debounced so
   transient blips and harmless upstream gaps stay quiet.

---

## 2. Core design decisions (the important part)

### 2.1 Separate *liveness* from *freshness*
The single most valuable idea. A naive "is there recent data?" check conflates three different
situations and cries wolf:

| Situation | Naive check | Correct classification |
|---|---|---|
| Worker crashed / hung | "no recent data" → alarm ✅ | **DOWN** — page me |
| Upstream source had a gap | "no recent data" → alarm ❌ | data lag, **not my fault** — stay quiet |
| Genuinely quiet period | "no recent data" → alarm ❌ | healthy, **stay quiet** |

**Solution:** the worker writes a **heartbeat** (a timestamp) on *every* cycle **regardless of
whether it did any work**. Liveness = "heartbeat is fresh". Freshness = "last real work item is
recent" — reported separately, as data, not as an alarm condition.

### 2.2 Alert only on state transitions, and debounce
Page once on the `ok → down` edge and once on `down → ok`, never repeatedly. Require **N
consecutive bad polls** before declaring `down` so a single dropped request or a brief upstream
gap never pages you.

### 2.3 The alerter is independent of the thing it watches
Run the alerter as its **own process/container** that polls the *public* status URL. That way it
can report the main service being down (it isn't sharing its fate). (Caveat: a VPS-side watcher
can't report the whole VPS being down — see §8.)

### 2.4 Self-heal what you can, only for real failures
An in-process **watchdog** force-exits a genuinely *hung* worker so the container restart policy
brings up a fresh one. Crucially it does **not** fire on upstream gaps (those keep the heartbeat
alive). Restarting never fixes an upstream problem, so don't automate that.

### 2.5 Keep it dependency-light and free
The alerter uses only the language standard library (HTTP via `urllib`) and the free Telegram Bot
API. No SaaS, no paid tier, no extra runtime deps.

---

## 3. Architecture

```
┌─────────────┐   heartbeat (every ~60s)     ┌──────────────┐
│   Worker    │ ───────────────────────────▶ │  Shared store │   (Postgres table `status`)
│ (+watchdog) │   last_seen_at, last_work_at  │               │
└─────────────┘                               └──────┬───────┘
      ▲ restart on hang (Docker restart policy)       │ read
      │                                                ▼
      │                                        ┌──────────────┐   GET /v1/status (JSON)
      │                                        │  API server  │   GET /status     (HTML page)
      │                                        └──────┬───────┘
      │                                                │ poll public /v1/status (every ~60s)
┌─────┴───────┐   Docker healthcheck            ┌──────▼───────┐   sendMessage
│   Docker    │ (liveness in `docker ps`)       │   Alerter    │ ──────────────▶  Telegram Bot API
└─────────────┘                                 └──────────────┘                   → your phone
```

**Five pieces:**
1. **Heartbeat** — worker upserts `{last_seen_at, last_work_at, ...}` every cycle.
2. **Status endpoint** — API reads the heartbeat, derives a `mode` (liveness) + freshness fields.
3. **Status page** — self-served HTML that polls the endpoint same-origin.
4. **Watchdog + healthcheck** — in-process stall detector + Docker healthcheck.
5. **Alerter** — independent poller + debounced state machine + Telegram.

---

## 4. Stack

| Concern | Choice used | Why / alternatives |
|---|---|---|
| Worker | Python (asyncio) | Any language; just needs to write a heartbeat each loop |
| Shared store | PostgreSQL (one `status` row) | Any store both worker + API can reach (Redis, a file, etc.) |
| API + status page | Node / Fastify | Any HTTP framework; the page is plain HTML+JS |
| Orchestration | Docker Compose | `restart: unless-stopped` + `healthcheck` do the heavy lifting |
| Alerter | Python **stdlib only** (`urllib`) | No deps; reused the worker's image as a sidecar |
| Notification | Telegram Bot API | Free, instant, mobile push; easy to script |

---

## 5. Implementation

### 5.1 Heartbeat (worker → store)
Upsert on **every** cycle, even when there was nothing to do. `last_seen_at` = liveness;
`last_work_at` (and any counters) = freshness/business signal.

```python
# Postgres example (psycopg 3). Called once per worker loop, always.
def heartbeat(conn, channel: str, *, last_work_at, inserted_delta: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO status (channel, last_seen_at, last_work_at, inserted_total)
            VALUES (%s, now(), %s, %s)
            ON CONFLICT (channel) DO UPDATE SET
              last_seen_at   = now(),
              last_work_at   = COALESCE(EXCLUDED.last_work_at, status.last_work_at),
              inserted_total = status.inserted_total + %s
            """,
            (channel, last_work_at, inserted_delta, inserted_delta),
        )
```

### 5.2 Status endpoint (derive liveness, report freshness separately)
`mode` is driven by the **heartbeat age**, not by work volume.

```ts
// Fastify handler. WORKER_STALE_MS ≈ 5× the heartbeat interval.
const WORKER_STALE_MS = 5 * 60 * 1000;

app.get("/v1/status", async () => {
  const s = await readStatus(sql);            // { lastSeenAt, lastWorkAt } epoch ms | null
  const now = Date.now();
  const alive = s.lastSeenAt != null && now - s.lastSeenAt < WORKER_STALE_MS;
  return {
    mode: alive ? "ok" : "stale",             // LIVENESS — drives alerting
    lastSeenAt: s.lastSeenAt,                  // heartbeat
    lastWorkAt: s.lastWorkAt,                  // FRESHNESS — reported, not alarmed
    serverTime: now,
  };
});
```

### 5.3 In-process watchdog (self-heal a hang)
A hung task never throws, so a try/except supervisor can't catch it. A watchdog that force-exits
on no-progress lets the container restart policy recover it. It's pure/testable.

```python
import asyncio, os, time

class Watchdog:
    def __init__(self, clock=time.monotonic):
        self._clock = clock; self._last = {}
    def kick(self, name):           # call once per completed worker cycle
        self._last[name] = self._clock()
    def stalled(self, max_stale_s):
        now = self._clock()
        return [n for n, t in self._last.items() if now - t > max_stale_s]

async def watchdog_task(wd, max_stale_s, interval_s=30, on_stall=lambda names: os._exit(1)):
    while True:
        await asyncio.sleep(interval_s)
        if wd.stalled(max_stale_s):
            on_stall(wd.stalled(max_stale_s))   # default: force exit → container restarts
            return
```

> Note: an upstream gap keeps the loop cycling (it `kick()`s every cycle), so the watchdog does
> **not** fire on it — exactly the intent.

### 5.4 Docker healthcheck + restart policy
The healthcheck surfaces liveness in `docker ps`; the restart policy recovers crashes and the
watchdog's forced exit. (Plain Compose does not auto-restart on *unhealthy* alone — the watchdog
covers hangs; add an `autoheal` sidecar if you also want unhealthy→restart.)

```yaml
worker:
  restart: unless-stopped
  healthcheck:
    test: ["CMD", "python", "-m", "yourpkg.healthcheck"]  # exit 0 if heartbeat fresh, else 1
    interval: 60s
    timeout: 15s
    retries: 3
    start_period: 180s
```

```python
# healthcheck.py — read-only check on heartbeat freshness.
import os, sys
from datetime import datetime, timezone
import psycopg
def main() -> int:
    stale_s = int(os.environ.get("HEALTHCHECK_STALE_S", "300"))
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5, autocommit=True) as c, c.cursor() as cur:
            cur.execute("SELECT last_seen_at FROM status WHERE channel='main'")
            row = cur.fetchone()
    except Exception as e:
        print(f"db error: {e}", file=sys.stderr); return 1
    if not row or row[0] is None:
        print("no heartbeat yet", file=sys.stderr); return 1
    age = (datetime.now(timezone.utc) - row[0]).total_seconds()
    print(f"{'ok' if age <= stale_s else 'stale'} ({age:.0f}s)")
    return 0 if age <= stale_s else 1
if __name__ == "__main__":
    sys.exit(main())
```

### 5.5 The Telegram alerter (the requested core)

**Telegram Bot API basics:**
- Create a bot via **@BotFather** → `/newbot` → get a token like `123456:AA...`.
- Get your **chat id**: message **@userinfobot** (it replies with your numeric id).
- Send: `POST https://api.telegram.org/bot<TOKEN>/sendMessage` with `chat_id`, `text`
  (`parse_mode=HTML` optional).
- **Critical gotcha:** a bot **cannot** message you until *you* have opened the bot and pressed
  **Start** at least once — otherwise `sendMessage` returns `400 "chat not found"`.

**The alerter** — poll the public status URL, classify, run a debounced state machine, send on
transitions. Pure stdlib. The two pure functions (`evaluate`, `step`) make it unit-testable.

```python
# alerter.py — an independent watcher. stdlib only.
import json, os, time, urllib.parse, urllib.request

def evaluate(code: int, body) -> tuple[bool, str]:
    """Healthy iff HTTP 200 and mode == 'ok'. (Swap the predicate for your service.)"""
    if code != 200 or not isinstance(body, dict):
        return False, f"API unreachable (HTTP {code})" if code else "API unreachable"
    if body.get("mode") != "ok":
        return False, f"worker down (mode={body.get('mode')!r})"
    return True, "ok"

def step(state: str, bad: int, healthy: bool, threshold: int):
    """Pure transition → (state, consecutive_bad, action in {'down','recovered',None})."""
    if healthy:
        return "ok", 0, ("recovered" if state == "down" else None)
    bad += 1
    if bad >= threshold and state == "ok":
        return "down", bad, "down"
    return state, bad, None

def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "alerter"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode())

def _send(token, chat_id, text):
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=10):
        pass

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    url   = os.environ.get("STATUS_URL", "https://example.com/v1/status")
    poll  = int(os.environ.get("ALERT_POLL_S", "60"))
    thresh = int(os.environ.get("ALERT_FAIL_THRESHOLD", "3"))
    if not token or not chat:
        while True: time.sleep(3600)          # idle until configured
    try: _send(token, chat, "✅ alerter online — watching the service.")
    except Exception: pass

    state, bad = "ok", 0
    while True:
        try: code, body = _fetch(url)
        except Exception: code, body = 0, None
        healthy, reason = evaluate(code, body)
        state, bad, action = step(state, bad, healthy, thresh)
        try:
            if action == "down":
                _send(token, chat, f"🔴 <b>Service DOWN</b>\n{reason}")
            elif action == "recovered":
                _send(token, chat, "🟢 <b>Service recovered</b>")
        except Exception: pass                 # never let a send error kill the loop
        time.sleep(poll)

if __name__ == "__main__":
    main()
```

**Run it as a sidecar** (reuse an existing image so there's no separate build):

```yaml
alerter:
  image: ${WORKER_IMAGE}            # reuse the worker image
  restart: unless-stopped
  command: ["python", "-m", "yourpkg.alerter"]
  env_file: .env                    # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, STATUS_URL
```

### 5.6 Status dashboard (optional, high-value)
A self-served HTML page that polls `/v1/status` **same-origin** (so it works where a sandboxed
page can't — no CORS). It shows *worker liveness* and *data freshness* as **separate cards**, with
a banner that names the actionable state ("worker down" vs "upstream lag" vs "operational"). Keep
it a single static file the API serves at `/status`; it needs no framework — `fetch` + a 20 s
`setInterval` + a few DOM updates.

---

## 6. Configuration & setup checklist

1. **Bot:** @BotFather → `/newbot` → copy the token.
2. **Chat id:** message @userinfobot → copy your numeric id. **Then open your new bot and press
   Start** (required, or `sendMessage` → `400 chat not found`).
3. **Env** (never commit these — keep in `.env` / a secrets store):
   ```
   TELEGRAM_BOT_TOKEN=123456:AA...
   TELEGRAM_CHAT_ID=123456789
   STATUS_URL=https://<your-service>/v1/status
   ALERT_POLL_S=60
   ALERT_FAIL_THRESHOLD=3
   HEALTHCHECK_STALE_S=300
   ```
4. **Deploy:** `docker compose up -d`. You should get the "✅ alerter online" ping immediately —
   that confirms the token + chat id + Start are all correct.

---

## 7. How to adapt to a new project

- **Swap the store:** the heartbeat can live in Redis (`SET status:main <ts>`), a file, or any
  shared medium — the API just needs to read `last_seen_at`.
- **Swap the health predicate:** change `evaluate()` and the status endpoint's `mode` logic to
  your definition of healthy (queue depth, error rate, a `/health` field, HTTP 200 alone, etc.).
- **Swap the channel:** replace `_send()` with Slack/Discord webhook, email (SMTP), or SMS — the
  poll/debounce/transition core is unchanged.
- **No worker?** For a plain web service, drop the heartbeat/watchdog and point the alerter at a
  `/healthz`; you lose the liveness-vs-freshness split but keep debounced up/down alerting.

---

## 8. Trade-offs & limitations

- **A VPS-side alerter can't report a total-VPS/network outage** (it dies with the box). Complement
  it with a **free external uptime monitor** (UptimeRobot / BetterStack / healthchecks.io) doing a
  keyword check on `/v1/status` for true off-box coverage. The two are complementary.
- **Telegram requires the recipient to Start the bot once** — fine for a personal/ops channel, not
  for arbitrary end users.
- **Debounce vs latency:** `ALERT_FAIL_THRESHOLD × ALERT_POLL_S` is your detection delay (e.g.
  3 × 60 s = ~3 min). Lower it for faster paging, raise it for fewer false alarms.
- **Docker Compose doesn't restart *unhealthy* containers by itself** — the in-process watchdog
  handles hangs; add an `autoheal` sidecar if you want healthcheck-driven restarts too.
- **Secrets:** the bot token grants send access to your bot; keep it in `.env`/secrets, rotate via
  @BotFather `/revoke` if it leaks.

---

## 9. One-paragraph summary

Have the worker heartbeat into a shared store every cycle regardless of work; expose a status
endpoint whose `mode` reflects **heartbeat freshness (liveness)** and reports **work freshness**
separately; serve a same-origin HTML dashboard off it; recover hangs with an in-process watchdog +
container restart policy; and run a tiny, dependency-free **independent** alerter that polls the
public status URL, debounces N consecutive failures, and pushes a Telegram message on the down and
recovery edges only. Add an external uptime monitor for off-box coverage.
