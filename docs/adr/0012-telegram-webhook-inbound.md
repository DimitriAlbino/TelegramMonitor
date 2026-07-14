# Telegram inbound: webhook (Telegram pushes to /telegram/webhook)

The bot receives user commands (`/start`, `/status`, `/mute`, `/incidents`, `/help`) over a **Telegram webhook**: Telegram POSTs each Update to our public HTTPS endpoint `POST /telegram/webhook`, and a FastAPI handler processes it. We do **not** run a `getUpdates` long-poller.

Why:

- **Natural fit for the stack.** We already run a FastAPI app serving HTTPS on a public VPS (ADR 0010). Adding one more POST route is trivial; a long-poller would be a separate persistent loop competing for the worker's asyncio budget.
- **Lower latency, no single-poller constraint.** Webhooks deliver each Update immediately. `getUpdates` permits only one concurrent poller per bot token, which complicates debugging and local dev.
- **Aligns with outbound delivery.** Alerts and Reports are already pushed by us via the Bot API; inbound-over-webhook makes the whole Telegram integration symmetric (both directions are Telegram-initiated-or-resolved HTTP, no persistent connections of our own).

## Trade-off accepted

A **VPS-side outage** (whole box down) means inbound commands are not just unhandled — Telegram will retry delivery for its standard window (~24h), so most commands sent during a brief outage are recovered when we return. Outbound Alerts during the outage are lost regardless (the worker is dead); this is the same limitation the reference doc §8 calls out, mitigated by a complementary **off-box external uptime monitor** hitting our own `/healthz`.

## Operational requirements

- **Public, stable HTTPS URL** with a valid certificate (Let's Encrypt on the VPS; or Telegram's self-signed-cert upload flow as a fallback).
- **Secret token** in the webhook registration (`X-Telegram-Bot-Api-Secret-Token` header) so we can reject spoofed requests that aren't from Telegram.
- **Idempotent handlers.** Telegram retries on non-2xx, so command handlers must be safe to receive twice (e.g. `/mute` twice = still muted; `/status` re-renders).
- **Local dev** uses Telegram's `getUpdates` via a scratch script, or ngrok + webhook — a dev concern, not an architectural one.
