# Public Status Page: one per user, opt-in monitors, auto-generated

Each User can have **one optional public Status Page** at a stable URL (e.g. `status.telegrammonitor.app/u/<slug>`, or a custom domain in a future paid tier). The page is **unauthenticated** and reflects the **live, automated** state of the user's Monitors — it is *not* a manual incident-publishing surface (no "we're aware of degraded performance" posts; that workflow is out of scope at launch).

## Data model implications

- A Monitor gains a `show_on_status_page: bool` flag (default false). Only Monitors with the flag set appear on the page.
- A `StatusPage` entity per User carries the slug, an optional title/subtitle, and a reference to the owning User. One per User at launch.
- The page reads from the same Monitor/Incident/rollup data the rest of the product uses — no separate "public" copy of the data.

## Operational implications

- The page is **read-only and unauthenticated**, but heavily **cached** (edge cache + short server-side TTL, ~60s) so a traffic spike on a popular page cannot hammer the DB.
- A Monitor's `show_on_status_page` flag is the single lever a User pulls to control what the world sees. There is no per-page privacy gradation at launch.
- Incident history shown on the page is derived from the automated Incident record (open time, close time, duration) — never manual text.

## Deferred (paid-tier candidates)

- Multiple pages per User (statuspage.io-style segmentation by product).
- Custom branding (logo, colors, CSS).
- Custom domains.
- Manual incident/maintenance-post publishing workflow.

These are explicitly *not* at launch to keep scope honest; the data model and URL scheme are chosen so they can be added without rework.
