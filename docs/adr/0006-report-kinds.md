# Report kinds: scheduled digest, on-demand snapshot, post-incident summary

The product offers three kinds of **Report** (Telegram-delivered summaries, distinct from event-driven Alerts):

## 1. Scheduled Report (periodical digest)
- User-configured cadence: **daily / weekly / monthly**, with a delivery time.
- Contents: per-Monitor uptime % over the period, number of Incidents, worst observed latency, and current live state. Aggregated from Timescale history.
- Dispatched by the same async worker's scheduler (a separate tick from checks), one job per user-cadence.

## 2. On-demand Report (status snapshot)
- Triggered by the user via a Telegram command (e.g. `/status`) or the web UI.
- Contents: the *current* live state of every Monitor ("all up", or the list of what's down with reason + downtime-so-far). Cheap — reads the latest Check per Monitor, no aggregation.
- Must be fast (sub-second to first byte); it's an interactive request.

## 3. Post-incident Summary
- Sent automatically when an Incident closes, *after* the recovery Alert.
- Contents: a short structured follow-up — incident duration, the failure reason, the affected Monitor, count of failed Checks. Richer than the recovery Alert's one-liner; shorter and more focused than a Scheduled Report.

## Deferred
- **On-demand historical Report** (arbitrary date-range uptime, e.g. "uptime for July"): heavier to compute, a natural paid-tier feature; not at launch. The data model retains the raw history to support it later.

## Scheduling note
Scheduled Reports reuse the check engine's tick-loop + DB-driven scheduling: a `reports` concept carries `next_run_at`, advanced by `now + cadence` on each run. No new infra.
