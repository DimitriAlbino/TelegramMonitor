# Telegram command surface

The shared bot supports a full interactive loop, not just outbound alerts. Launch commands:

| Command | Purpose |
|---|---|
| `/start` | Help only — shows the user their chat ID and points to the Settings page to link. (The tokenized `/start <link_token>` binding was removed; linking is now a manual chat-ID entry on the Settings page.) |
| `/status` | On-demand Report — current live state of all the user's Monitors. |
| `/mute <monitor>` / `/unmute <monitor>` | Suppress Alert delivery for a Monitor (supports `/mute all`). |
| `/incidents` | List the user's recent Incidents. |
| `/help` | Command reference. |

## Mute vs Pause — distinct concepts

This is the load-bearing distinction in the command surface, so it gets called out here:

- **`/mute`** (Telegram-settable) suppresses **Alert delivery only**. Checks keep running, Results keep being recorded, Incidents keep being tracked. On `/unmute`, the user sees the full history of what happened while muted. This is the "I know it's down, I'm working on it" control.
- **Pause** (web-UI-settable) **stops Checks from executing** for that Monitor — no Results, no Incidents, no resource use. This is the "I'm not using this Monitor right now" control.

The two are independent flags on a Monitor and do not affect each other. Conflating them (e.g. making mute also pause) would create blind spots in monitoring history, which defeats the purpose of muting.

## Authorization

Every command authenticates by resolving the incoming Telegram `chat_id` to a User via the stored account link. Unrecognized chat_id → a "please link your account at <url>" reply. No unauthenticated interaction beyond `/start` linking.
