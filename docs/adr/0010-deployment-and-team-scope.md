# Deployment: single VPS + Docker Compose. Team scope: single-user accounts.

Two related launch decisions about the shape of the running system.

## Deployment

The hosted instance runs on **a single VPS** (Hetzner / DigitalOcean / equivalent) under **Docker Compose**, with services:

- `api` — FastAPI app (web UI + JSON API + Telegram webhook receiver).
- `worker` — the single async check-engine process (ADR 0004) and the scheduled-Reports dispatcher (ADR 0006).
- `db` — Postgres with the TimescaleDB extension (one container, one volume).

Why:

- Consistent with the reference `telegram-status-alerting.md` Docker Compose heritage.
- Matches the Q6 decision (one async worker) — there is no distributed queue to require multi-node orchestration.
- Cheapest path to a real running product, full control, one box to debug, one volume to back up.
- Vertical scaling (bigger VPS) carries us a long way; horizontal sharding of the worker (per ADR 0004's escape hatch) is the migration trigger, not user count.

Secrets (Telegram bot token, DB credentials, session secret, email/SMS provider keys) live in `.env` on the host, never in the image. Off-box coverage of our own service uses a free external uptime monitor hitting our own health endpoint — the same complementary pattern the reference doc recommends (§8).

## Team scope: single-user, no teams

A User is the sole owner of their Monitors, Incidents, Reports, Status Page, and Notification Channel. There is **no Team/workspace abstraction** at launch:

- `Monitor.user_id` is a direct foreign key to `User`. No `team_id`, no membership table, no role checks.
- Sharing one set of monitors between multiple humans is explicitly out of scope.

Why:

- Teams add a large, cross-cutting amount of data-model and authorization complexity (ownership transfer, invitations, per-resource role checks, billing-per-team) for a benefit that the launch audience (personal / small-team operators) does not yet need.
- The cost of adding Teams later is a bounded migration (introduce an `Account`/`Team` layer above `User`, move ownership up) — not a rewrite — *provided* we keep resource ownership as `user_id` foreign keys today rather than scattering owner logic through the code.

Rejected: the "design for teams later" middle option was considered but rejected as premature abstraction — the migration cost is low enough that we'd rather ship the simpler thing and refactor if/when Teams become a real demand.
