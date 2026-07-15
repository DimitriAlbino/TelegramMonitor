## Agent skills

### Issue tracker

Issues live as GitHub issues in DimitriAlbino/TelegramMonitor (uses `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Deployment

Our live instance runs at **`telegrammonitor.com`**. Deployment is standard
Docker Compose (see the README "Run locally" section and `docker-compose.yml`):
the worker applies migrations on boot, Caddy terminates TLS in front of the api,
and the Telegram webhook points at `/telegram/webhook`.

Real deployment credentials (SSH alias, bot token, generated secrets) are
**never committed**. They live only on the operator's machine and the server.
If you need a value to perform a deployment action, ask the operator — do not
invent values or look for them in this repo.
