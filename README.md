# TelegramMonitor

A hosted, configurable monitoring service that watches your online services — APIs, websites,
TCP endpoints — and talks to you over Telegram: instant alerts when something goes down (and when
it recovers), scheduled uptime reports, and on-demand status queries right from the chat.

> **Status:** in design. The architecture and product scope are settled (see
> [`docs/adr/`](./docs/adr/) and [`CONTEXT.md`](./CONTEXT.md)); implementation has not started.
> This README describes the intended product.

---

## What it does

- **Monitors anything that speaks HTTP or TCP.** Configure a Monitor (a URL or a `host:port`),
  an interval, and failure/recovery thresholds. The engine probes it on your behalf.
- **Alerts you on Telegram — only on transitions.** You get paged once when an Incident opens and
  once when it recovers, never repeatedly. N consecutive failures must occur before paging, so a
  single dropped request stays quiet.
- **Sends scheduled reports.** A daily/weekly/monthly digest of uptime, incidents, and latency,
  delivered to your Telegram on a cadence you set.
- **Answers you on demand.** Send `/status` to the bot and get the live state of every Monitor
  instantly — no app to open.
- **Self-heals and stays honest.** The engine separates *liveness* from *freshness* and runs an
  in-process watchdog, inheriting the discipline of the
  [reference pattern](./telegram-status-alerting.md) this project generalizes.
- **Publishes a status page.** Opt any subset of your Monitors into a public, auto-generated
  status page at a stable URL.

## Why this exists

It started as a single-process Telegram alerter for one self-hosted service
([the origin doc](./telegram-status-alerting.md)). This project generalizes that proven, lean
pattern into a configurable, multi-user SaaS — without inventing anything that wasn't already
battle-tested in the original.

## Architecture (summary)

| Concern | Choice | Why |
|---|---|---|
| Deployment | Hosted multi-tenant SaaS, single VPS + Docker Compose | One box, one backup, cheap; vertical-scale carries us far |
| Backend | Python · FastAPI · async | Matches the lineage; async-native for thousands of concurrent probes |
| Storage | Postgres + TimescaleDB | One engine; hypertables for the high-volume Check stream |
| Check engine | Single async worker, DB-driven tick loop | State in the DB, survives restarts; no mandatory Redis |
| Telegram delivery | One shared bot now, BYO bot later | Low-friction onboarding; power-user isolation deferred |
| Telegram inbound | Webhook (`/telegram/webhook`) | Natural fit for FastAPI; symmetric with outbound pushes |
| Auth | Email/password + Telegram Login | Self-contained base; Telegram Login also yields the chat_id for free |
| Reports | Scheduled digest · on-demand snapshot · post-incident summary | See [ADR 0006](./docs/adr/0006-report-kinds.md) |
| Public surface | Optional per-user Status Page | One per user, opt-in monitors, auto-generated |
| Pricing | Free at launch, no billing | See [ADR 0011](./docs/adr/0011-no-billing-at-launch.md) |

The full reasoning behind every choice lives in [`docs/adr/`](./docs/adr/). The project's
ubiquitous language (Monitor, Check, Incident, Alert, Report, Status Page, …) is defined in
[`CONTEXT.md`](./CONTEXT.md).

## Project layout

```
.
├── CONTEXT.md                      # domain glossary (the nouns)
├── README.md                       # this file
├── telegram-status-alerting.md     # the reference pattern this project generalizes
└── docs/
    └── adr/                        # architectural decision records
        ├── 0001-hosted-multi-tenant-saas.md
        ├── 0002-shared-bot-first-byo-later.md
        ├── …
        └── 0012-telegram-webhook-inbound.md
```

Implementation directories (`api/`, `worker/`, etc.) will appear here once build begins.

## Status & roadmap

The design phase is complete. Implementation milestones (in dependency order):

1. **Foundation** — repo, Docker Compose skeleton, Postgres+Timescale, Alembic, the core
   domain tables (User, Monitor, Check, Incident).
2. **Check engine** — the async tick-loop worker, HTTP and TCP Check executors.
3. **Auth + web API** — email/password + Telegram Login; CRUD for Monitors.
4. **Telegram integration** — shared bot, webhook receiver, account-linking flow, `/start`.
5. **Alerting** — debounced Incident state machine, flapping suppression, quiet hours.
6. **Reports** — scheduled digest dispatcher + on-demand snapshot + post-incident summary;
   `/status`, `/mute`, `/incidents`, `/help`.
7. **Status Page** — public, cached, opt-in per Monitor.

Deferred Pro-tier candidates (collected in
[ADR 0011](./docs/adr/0011-no-billing-at-launch.md)): BYO bot, historical date-range reports,
multiple/branded/custom-domain status pages, teams, extended retention.

## License

TBD (a permissive license will be chosen before the first public release).
