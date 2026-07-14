# No billing system at launch

TelegramMonitor launches as a **free product with no payment infrastructure**. There are no paid tiers, no Stripe integration, no subscription lifecycle, no invoices, no dunning. The only limits in place are **abuse-prevention caps** (e.g. a maximum number of Monitors per User, rate-limited signup) and infrastructure cost controls — not monetization.

## Why

- **Validate demand first.** Adding billing before proving the product retains users is premature scope. The deferred features below become the raw material of a future paid tier once we know what people will pay for.
- **Strip a subsystem from launch scope.** Billing is a large, cross-cutting concern (subscription state machine, payment webhooks, invoice generation, failed-payment flows, plan-change proration). Omitting it is a meaningful scope reduction.

## What this means for the deferred features

These features — repeatedly flagged as "paid-tier candidate" in earlier ADRs — are **not built at launch**, and are explicitly collected here as the future Pro tier's raw material:

- **BYO bot** (ADR 0002) — per-user bot isolation.
- **On-demand historical Report** with arbitrary date ranges (ADR 0006).
- **Multiple Status Pages** per User, custom branding, custom domains (ADR 0009).
- **Teams / shared workspaces** (ADR 0010).
- **Raw retention beyond 30 days / hourly beyond 1 year** (ADR 0008).

When/if we monetize, these become the natural Pro features. The data model and architecture are chosen so that adding a `plan` flag and gating these features behind it requires no rework — only the gating logic itself.

## Abuse-prevention limits (the only limits that exist)

These exist to protect the hosting bill, not to drive revenue. Concrete numbers decided at implementation time:

- Max Monitors per User (e.g. 50).
- Min check interval (e.g. 30 seconds) — protects the worker's concurrency budget.
- Rate-limited signup (per-IP and per-email).
- Outbound request budget per User (prevents one user monopolizing the worker).
