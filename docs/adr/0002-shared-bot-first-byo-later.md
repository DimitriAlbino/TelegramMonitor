# Shared bot first, BYO bot later

> **Status: Superseded (2026-07-15).** The tokenized deep-link + `/start`
> binding flow described below never worked reliably in production. Linking
> is now done via a **manual chat-ID entry** on the Settings page (the user
> messages `@userinfobot` to find their chat ID, then pastes it into
> `/ui/settings`). The shared-bot + `chat_id` routing model still holds; only
> the *linking mechanism* changed. See ADR-0007 for the updated command
> surface.

We will deliver Telegram alerts primarily through **one shared bot** owned by the service (e.g. `@TelegramMonitorBot`). ~~Users link their account to their Telegram chat via a tokenized deep-link + `/start` flow~~ Users link their Telegram chat by entering their numeric chat ID on the Settings page, and the bot routes messages by each user's stored `chat_id`.

We will also support **bring-your-own-bot (BYO)**, where an advanced user pastes their own bot token + chat_id into the web UI — but this is a **later** milestone, not the launch feature. The data model must accommodate both from the start: a notification channel is an abstraction that can resolve to either "shared bot + user's chat_id" or "user's own bot token + chat_id".

Why this order: the shared bot is what makes the product feel like a real SaaS (one-click onboarding, no BotFather dance). BYO adds isolation and removes shared rate-limit concerns but has high onboarding friction — a power-user feature, not a launch feature.
