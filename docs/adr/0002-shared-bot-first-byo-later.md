# Shared bot first, BYO bot later

We will deliver Telegram alerts primarily through **one shared bot** owned by the service (e.g. `@TelegramMonitorBot`). Users link their account to their Telegram chat via a tokenized deep-link + `/start` flow, and the bot routes messages by each user's stored `chat_id`.

We will also support **bring-your-own-bot (BYO)**, where an advanced user pastes their own bot token + chat_id into the web UI — but this is a **later** milestone, not the launch feature. The data model must accommodate both from the start: a notification channel is an abstraction that can resolve to either "shared bot + user's chat_id" or "user's own bot token + chat_id".

Why this order: the shared bot is what makes the product feel like a real SaaS (one-click onboarding, no BotFather dance). BYO adds isolation and removes shared rate-limit concerns but has high onboarding friction — a power-user feature, not a launch feature.
