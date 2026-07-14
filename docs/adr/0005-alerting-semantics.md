# Alerting semantics: debounced transitions, flapping suppression, quiet hours

Inherits the reference doc's core discipline — **alert only on Incident state transitions, never repeatedly** — and extends it for a multi-user SaaS.

## Thresholds (per-Monitor, with global defaults)

- **`failure_threshold`**: consecutive failed Checks before an Incident *opens*. Default 2. Drives the opened-Alert.
- **`recovery_threshold`**: consecutive successful Checks before an Incident *closes*. Default 2. Drives the recovered-Alert.
- Defaults are overridable per Monitor (a 5-min-interval monitor and a 30-s-interval monitor want different sensitivities).

An Incident is the canonical record of "this Monitor was down from T1 to T2." The threshold counters reset on each transition.

## Flapping suppression

If a Monitor opens ≥3 Incidents within a rolling 10-minute window, it enters a **`flapping`** state:

- No new Incidents are opened while flapping.
- Exactly one Alert is sent: "🔴 X is flapping — suppressing further alerts."
- Normal evaluation resumes only after the Monitor stays healthy for a stabilization period (e.g. 5 consecutive successes), after which a single recovery Alert is sent.

This protects users from a flood of open/close Alerts when a monitor is oscillating.

## Quiet hours

Per-User `quiet_hours` (start/end, in the user's timezone). During the window:

- Non-critical Alert delivery is **deferred** (not dropped): queued Alerts are coalesced into a single digest delivered when the window ends.
- Monitors the User has marked **`critical`** bypass quiet hours and page immediately.

Recovery Alerts during quiet hours follow the same rule as the corresponding opening Alert (critical recovers immediately; non-critical is folded into the digest).

## Notification routing

All Alerts and Reports flow through the user's **Notification Channel** abstraction (see glossary), so the shared-bot-vs-BYO-bot choice is invisible to this policy.
