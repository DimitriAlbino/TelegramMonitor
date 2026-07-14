# Hosted multi-tenant SaaS deployment model

We will run a single hosted deployment of TelegramMonitor that any user can sign up for, configure their own monitors, and receive Telegram alerts. Operators (us) pay the hosting bill; users get a product without self-deploying.

This is the foundational architecture decision. It drives: account-based authentication, tenant data isolation by `User`, a shared Telegram bot whose messages are routed to each user by their linked `chat_id`, and horizontal scaling of the polling engine as total monitor count grows.

Rejected alternatives:

- **Self-hosted multi-user** (Uptime Kuma model): simpler ops, no cross-tenant scaling concerns, but every user must self-deploy and self-manage a Telegram bot — a high friction bar that defeats the goal of a broad, public, configurable service.
- **Self-hosted single-admin**: closest to the reference `telegram-status-alerting.md` pattern, but it is not really a shared service and contradicts the explicit goal of user authentication for multiple users.
