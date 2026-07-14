## Agent skills

### Issue tracker

Issues live as GitHub issues in DimitriAlbino/TelegramMonitor (uses `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Deployment

Our live instance runs at **`telegrammonitor.com`**. The full deployment
reference — SSH alias, server specs, Telegram bot token, owner chat id, webhook
secret, generated secrets, and the step-by-step runbook — lives in a
**git-ignored** file at `.deploy/local-deployment.md`. That file is the single
source of truth for deploying our instance; it is intentionally not committed
(real credentials must not enter version control). Read it before any
deployment action. If you don't see it, ask the operator — do not invent
values. See `.deploy/local-deployment.md`.
