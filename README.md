# TelegramMonitor

A hosted, configurable monitoring service that watches your online services — APIs, websites,
TCP endpoints, JSON fields — and talks to you over Telegram: instant alerts when something goes
down (and when it recovers), scheduled uptime reports, and on-demand status queries right from
the chat.

> **Status:** live and running at **[telegrammonitor.com](https://telegrammonitor.com)**.
> The full stack is implemented: web UI, check engine, incident state machine, Telegram bot,
> scheduled reports, and a public status page. Licensed under the MIT License — see
> [`LICENSE`](./LICENSE).

---

## What it does

- **Monitors anything that speaks HTTP, TCP, or JSON.** Configure a Monitor (a URL, a
  `host:port`, or a JSON API endpoint), an interval, and failure/recovery thresholds. The engine
  probes it on your behalf.
  - **HTTP** — GET a URL, assert status code / keyword / max latency. Follow-redirects is
    per-monitor (so a 301 → 200 is correctly reported as UP).
  - **TCP** — open a socket to `host:port`, succeed on connect.
  - **API content** — GET a JSON URL, read a field by dot-path (e.g. `mode` or `data.status`),
    and fail if the field contains an alarm keyword (e.g. `stale`).
- **Alerts you on Telegram — only on transitions.** You get paged once when an Incident opens
  and once when it recovers, never repeatedly. N consecutive failures must occur before paging,
  so a single dropped request stays quiet. A post-incident summary follows each recovery with
  duration and failed-check count.
- **Sends scheduled reports.** A daily/weekly/monthly digest of uptime, incidents, and latency,
  delivered to your Telegram on a cadence you set.
- **Answers you on demand.** Send `/status` to the bot and get the live state of every Monitor
  instantly — no app to open. `/mute`, `/unmute`, `/incidents`, and `/help` round out the
  command surface.
- **Stays honest and restarts cleanly.** The engine separates *liveness* from *freshness*
  (the `/healthz` probe reports DB reachability separately from probe volume), and scheduling
  state lives in the database so a worker restart resumes exactly where it left off with no
  double-fire window. Containers run under `restart: unless-stopped`. The incident state
  machine is a pure function — debounce, recovery, and flapping suppression are fully
  unit-tested.
- **Publishes a public status page.** Opt any subset of your Monitors into a public,
  auto-generated status page at a stable URL (`/status/{slug}`) — read-only, cached, with
  incident history derived from the automated record.

## Architecture (summary)

| Concern | Choice | Why |
|---|---|---|
| Deployment | Hosted multi-tenant SaaS, single VPS + Docker Compose | One box, one backup, cheap; vertical-scale carries us far |
| Backend | Python · FastAPI · async | Matches the lineage; async-native for thousands of concurrent probes |
| Storage | Postgres + TimescaleDB | One engine; hypertables for the high-volume Check stream |
| Check engine | Single async worker, DB-driven tick loop | State in the DB, survives restarts; no mandatory Redis |
| Telegram delivery | One shared bot (BYO bot later) | Low-friction onboarding; power-user isolation deferred |
| Telegram linking | Manual chat-ID entry on the Settings page | Simple, reliable; the tokenized `/start` flow was removed (ADR-0002 superseded) |
| Telegram inbound | Webhook (`POST /telegram/webhook`) | Natural fit for FastAPI; symmetric with outbound pushes |
| Auth | Email/password (Telegram Login planned) | Self-contained base; sessions are signed JWTs |
| Reports | Scheduled digest · on-demand snapshot · post-incident summary | See [ADR 0006](./docs/adr/0006-report-kinds.md) |
| Public surface | Optional per-user Status Page | One per user, opt-in monitors, auto-generated, cached |
| Pricing | Free at launch, no billing | See [ADR 0011](./docs/adr/0011-no-billing-at-launch.md) |
| License | MIT | Open source |

The full reasoning behind every choice lives in [`docs/adr/`](./docs/adr/). The project's
ubiquitous language (Monitor, Check, Result, Incident, Alert, Report, Status Page, …) is defined
in [`CONTEXT.md`](./CONTEXT.md).

## Project layout

```
.
├── CONTEXT.md                      # domain glossary (the nouns)
├── README.md                       # this file
├── LICENSE                         # MIT
├── telegram-status-alerting.md     # the reference pattern this project generalizes
├── pyproject.toml                  # project + tooling config (pytest, ruff, mypy)
├── Dockerfile                      # one image; api + worker pick the entrypoint
├── docker-compose.yml              # api, worker, db (Postgres+Timescale)
├── alembic.ini + alembic/          # migrations (8 revisions: schema + seed + rollups)
├── tgmonitor/                      # the package
│   ├── config.py                   # env-driven settings (single source of truth)
│   ├── db.py                       # async SQLAlchemy engine/session
│   ├── models.py                   # ORM: User, Monitor, Check (hypertable)
│   ├── orm.py                      # declarative base
│   ├── results.py                  # pure Result + classifiers (HTTP, API content)
│   ├── incident_model.py           # Incident ORM model
│   ├── incidents.py                # pure Incident state machine (Seam A)
│   ├── reports.py                  # scheduled-report renderer + scheduler
│   ├── report_model.py             # Report ORM model
│   ├── statuspage_model.py         # StatusPage ORM model
│   ├── executors/                  # Check executors (Seam B)
│   │   ├── base.py                 # CheckConfig, Transport protocol, dispatcher
│   │   ├── http.py                 # HTTP executor (httpx)
│   │   ├── tcp.py                  # TCP executor (asyncio sockets)
│   │   └── api_content.py          # JSON-field executor
│   ├── auth/                       # auth: hashing, JWT tokens, rate limiting, routes
│   ├── api/                        # FastAPI app: JSON API + status page + healthz
│   │   ├── main.py                 # app factory + routers + static mounts
│   │   ├── monitors.py             # Monitor CRUD + incidents + test-alert
│   │   ├── reports.py              # Scheduled-report CRUD
│   │   └── statuspage.py           # Public status page (HTML + JSON)
│   ├── telegram/                   # Telegram bot: client, webhook, commands, alerting
│   ├── ui/                         # Web UI: cookie-session auth + HTML routes
│   │   ├── session.py              # cookie-based session dependency
│   │   ├── auth_routes.py          # signup/login/logout/verify/reset pages
│   │   └── app_routes.py           # dashboard/detail/settings/status-page pages
│   └── worker/                     # the async Check engine
│       ├── engine.py               # tick loop + claim-then-run + alert sink
│       ├── alerting.py             # SM → Incident persistence + alert dispatch
│       └── __main__.py             # entrypoint: DB wait + migrations + engine.run()
├── templates/                      # Jinja2 templates (base layout, auth, dashboard, etc.)
├── static/                         # CSS (dark/light themes, responsive)
└── docs/
    ├── adr/                        # 12 architectural decision records
    └── agents/                     # agent skill configs (issue tracker, triage, domain)
```

## Run locally

The stack is three Docker Compose services: `db` (Postgres with TimescaleDB),
`worker` (the Check engine), and `api` (FastAPI web UI + JSON API + Telegram webhook).
The worker applies migrations on startup, so a fresh `docker compose up` is immediately runnable.

```bash
cp .env.example .env          # then edit real secrets into .env (never committed)
docker compose up --build     # db healthy → api on :8000 → worker probing
```

Open `http://localhost:8000` → redirects to the login page. Sign up, verify your
email (links printed to the logs in dev), and start adding monitors.

### Development (without Docker)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"        # or: uv sync --extra dev (uses the committed uv.lock)
pytest                         # unit tests (executors, state machine, auth, classifiers)
mypy tgmonitor                 # typecheck
ruff check && ruff format --check   # lint
alembic upgrade head --sql     # emit migration SQL offline (no DB needed)
```

Dependencies are pinned in `uv.lock` so the deployed image is reproducible.
Regenerate it with `uv lock` after changing `pyproject.toml`.

## Demo

A seed HTTP Monitor probing `https://example.com` every 30s is created on first
migrate so the worker immediately has something to probe. The historical
seeded demo account with a known password was removed for security — sign up
with your own email and add monitors immediately.

## Status & roadmap

The launch spec is fully implemented and deployed:

1. ✅ **Foundation** — repo, Docker Compose, Postgres+Timescale, Alembic, core tables
2. ✅ **Check engine** — async tick-loop worker, HTTP + TCP + API-content executors
3. ✅ **Auth + web API** — email/password, JWT sessions, Monitor CRUD
4. ✅ **Telegram integration** — shared bot, webhook, manual chat-ID linking, alerts
5. ✅ **Alerting** — debounced Incident state machine, flapping suppression, quiet hours
6. ✅ **Reports** — scheduled digest + on-demand snapshot + post-incident summary
7. ✅ **Status Page** — public, cached, opt-in per Monitor
8. ✅ **Web UI** — responsive dashboard, monitor detail, settings, status-page config

Deferred Pro-tier candidates (collected in
[ADR 0011](./docs/adr/0011-no-billing-at-launch.md)): BYO bot, historical date-range reports,
multiple/branded/custom-domain status pages, teams, extended retention, Telegram Login widget.

## License

[MIT](./LICENSE) — © 2026 Dimitri Albino
