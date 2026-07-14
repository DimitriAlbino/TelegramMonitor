# TelegramMonitor

A hosted monitoring service. Users configure **Monitors** that the service probes on their behalf on a schedule; results are evaluated and, when something crosses a threshold, the user is notified over **Telegram**. Users can also receive periodic **Reports** summarizing monitor activity, and request reports on demand via Telegram commands.

## Language

**User**:
A person with an account on the service who owns monitors and receives notifications.
_Avoid_: Customer, account, subscriber (an "account" is the authentication container, not the person).

**Monitor**:
A user-configured thing to watch — a URL or a host:port — together with how often to probe it and what counts as a failure. A Monitor is a configuration entity; it does not run continuously by itself.
_Avoid_: Service, probe, check (a "probe" or "check" is a single execution; the Monitor is the standing configuration).

**Paused** vs **Muted**:
Two independent states a Monitor can be in, often confused. **Paused** means the engine stops running Checks for it (no execution, no Results) — set from the web UI for a Monitor you're decommissioning or temporarily not using. **Muted** means Checks keep running and Incidents keep being recorded, but Alert delivery is suppressed — settable from Telegram (`/mute`) for a Monitor you already know is down. A Monitor can be neither, either, or both; the two flags don't interact.

**Check Kind**:
The category of probe a Monitor performs. Launch kinds are **HTTP** (GET a URL, assert status / keyword / latency) and **TCP** (open a socket to host:port).
_Avoid_: Monitor type (the Monitor is the configured instance; the Kind is the verb).

**Check**:
A single execution of a Monitor's probe at a point in time, producing a **Result**. "A check ran at 10:03." Used as both the noun (the execution) and loosely as the Result.
_Avoid_: Probe (we reserve "probe" informally; the canonical record is a Check).

**Result**:
The outcome of one Check: success/failure, latency, status code, and a human-readable reason. Stored as an append-only time series per Monitor.
_Avoid_: Reading, sample.

**Incident**:
A contiguous span during which a Monitor was in a failing state, opened when failures cross a threshold and closed on recovery. The unit an alert fires about.
_Avoid_: Outage, alert (an "alert" is the notification *about* an Incident).

**Alert**:
A Telegram message sent because an Incident transitioned (opened or recovered). One Incident produces at most an opened-alert and a recovered-alert.
_Avoid_: Notification, ping.

**Report**:
A summary of Monitor activity delivered over Telegram. Three kinds: a **Scheduled Report** (periodical digest on a user-configured cadence), an **On-demand Report** (a user requests the current state of their monitors, e.g. via `/status`), and a **Post-incident Summary** (a richer follow-up message sent when an Incident closes — distinct from the recovery Alert, which is a one-liner). Reports are informational; Alerts are event-driven.
_Avoid_: Digest, summary (too generic; "Report" is the product concept).

**Notification Channel**:
The abstract destination for a user's Alerts and Reports — resolves to either the shared bot + the user's linked chat, or (later) the user's own bot. The indirection that lets BYO-bot be added without rewriting delivery.
_Avoid_: Endpoint, hook.

**Status Page**:
An optional, public, unauthenticated web page that reflects the live state of a subset of a User's Monitors, served at a stable URL. A Monitor appears on the page only if its `show_on_status_page` flag is set. The page is auto-generated from Check results — Users do not publish manual incident updates or maintenance notes through it (that is out of scope). One Status Page per User.
_Avoid_: Dashboard (that's the authenticated owner view); statuspage (a competitor name).
