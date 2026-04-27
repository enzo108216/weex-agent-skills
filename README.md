# weex-trader-skill

Use this skill in Codex / Openclaw / Claude Code to automate WEEX futures and spot workflows with natural language.

It supports:

- public market data
- private account and position queries
- spot and futures order placement
- raw endpoint access through local endpoint catalogs
- secure saved-profile management across Windows, macOS, and Linux

## Contents

- Get API credentials
- Critical secret warning
- Install in Codex
- How to use this skill in Codex / Openclaw / Claude Code
- Module quick-reference
- Saved profile setup
- Security notes
- Troubleshooting
- References

## Get API Credentials

Access to private endpoints such as account and trading APIs requires a WEEX API key. Public market data endpoints are available without authentication.

Create an API key from [API Management](https://www.weex.com/account/newapi/).

Keep these values secure:

- API Key
- Secret Key
- Passphrase

Never share your Secret Key or Passphrase. Anyone with those credentials can control the account.

## Critical Secret Warning

This skill allows AI-assisted handling of API keys, API secrets, passphrases, and vault passwords, but doing so creates exposure risk. If you choose to paste secrets into AI chat or let the AI operate on them directly, assume they may be retained or leaked later. This includes any secret entered through the profile manager or vault manager.

Openclaw Telegram conversations can leave server-side chat logs and relay history. Treat anything sent through that path as potentially retained, reviewed, or recovered later.

Prefer local secret-entry flows such as the GUI profile manager, the vault UI, `--prompt-secrets`, or a trusted local stdin pipe instead of typing secrets into a chatbot window.

## Install In Codex

Install from the checkout you plan to use, or from a published repo URL that includes the current `README.md`, `SKILL.md`, `manifest.json`, `file-index.json`, `scripts/`, `references/`, and `requirements.txt`.

Saved profiles and vault files are runtime state under the local WEEX config directory. Do not ship, version, or share that state as part of the skill checkout.
AI helper cache files `agent-init.json` and `agent-runtime.json` may also appear there. They help route later AI actions faster, but they are not secret storage.
AI agents using this skill should run `py -3 scripts/weex_agent_state.py --command skill.preflight --language <zh|en> --pretty` on Windows or `python3 scripts/weex_agent_state.py --command skill.preflight --language <zh|en> --pretty` on macOS/Linux before each routed task so the cache is always present and fresh.
On Windows and macOS, that preflight step can auto-provision the managed GUI runtime when the current interpreter cannot initialize Tk safely.
When a GUI must be launched from an AI/tool-managed shell, the detached launcher tries to show only the WEEX window: macOS uses a transient `.app` wrapper, while Windows prefers `pythonw.exe` or another hidden background process instead of a visible console window.
If `agent-init.json` is missing and the AI is about to use an auto-language wrapper such as `scripts/weex_vault.py`, the AI should refresh the cache first instead of guessing.

- Never share or commit API credentials.
- Use least-privilege API keys for this workflow.
- If credentials are exposed, revoke/rotate them immediately.

## How to Use This Skill in Codex / Openclaw / Claude Code

Mention `$weex-trader-skill`, then say what you want in plain language.
You do not need rigid command-style wording.

| Scenario | Natural-language example |
|---|---|
| Check market price | `"What's the latest BTCUSDT spot price?"` |
| Review account or positions | `"Show me my current futures positions and available balance."` |
| Place a spot market order | `"Buy 200 USDT worth of BTC at market."` |
| Place a futures limit order | `"Open a small ETHUSDT short with a limit order at 2500."` |
| Cancel open orders | `"Cancel my open ETHUSDT futures orders."` |
| Check order status | `"Did my BTCUSDT order fill yet?"` |

## How to Use This Skill in Codex / Openclaw / Claude Code

## Module quick-reference

| Module | What it covers | Auth |
|---|---|---|
| `Spot` | Spot market data, balances, orders, trade history, and related endpoints. | Public + private |
| `Futures` | Futures market data, account state, orders, positions, and leverage/margin endpoints. | Public + private |

If you need the underlying Python/runtime setup, shell command context, or direct CLI examples, open [Script operations](references/script-operations.md).

## Saved Profile Setup

Private account and trading operations require a saved profile.

Choose the setup guide that matches how you want to work:

- Windows/macOS account manager workflow: [Profile manager guide](references/profile-manager.md)
- Full OS matrix and terminal-based profile commands: [Profile onboarding](references/profile-onboarding.md)
- Linux vault modes, password handling, and lock/unlock flows: [Linux vault](references/linux-vault.md)

Use the profile manager guide when you want the GUI flow. Use the onboarding guide when you need exact terminal commands or server automation patterns. On Linux, use the `manual_once` vault flow and handle vault passwords, env vars, and temporary secret files carefully. `unlock` only needs one passphrase entry for the existing vault password, while `setup` and `change-password` still confirm the new passphrase twice.
On Windows and macOS, the GUI entrypoints can self-bootstrap a managed CPython 3.12 runtime if the current interpreter cannot initialize Tk correctly.
If an AI or automation host launches the GUI, prefer `scripts/weex_gui_launcher.py ...`; launcher records and logs are written under `~/.weex-trader-skill/gui-launchers`, keep only the most recent launches, and trim each log to a bounded size.

## Security Notes

- AI-assisted secret handling is supported by this skill, but it increases leakage risk. Openclaw Telegram is especially sensitive because it can leave server-side chat logs.
- Never share or commit API credentials.
- Use least-privilege API keys for this workflow.
- If credentials are exposed, revoke or rotate them immediately.
- Prefer saved profiles over ad hoc secret-passing shell commands.
- For server automation, avoid `--api-key`, `--api-secret`, and `--api-passphrase` on argv; prefer environment variables or `--secrets-stdin-json`.
- Raw argv secrets and literal vault passwords can leak through shell history, the process list, terminal scrollback, audit logs, and crash reports.
- temporary password files and secret JSON files can leak through backups, sync folders, editors' recent-file lists, and filesystem forensics. Delete them immediately and keep them outside the repo.
- `profiles.meta.json` is not the encrypted vault. It can still reveal account names, descriptions, default-profile choices, and custom base URLs.
- `manual_once` is the supported Linux vault mode. Lock it again after sensitive work when appropriate.
- `--confirm-live` sends real order or cancel requests to the real account. Start with least-privilege keys and a small or non-critical account whenever possible.

## Troubleshooting

For common operator issues and recovery paths, open [Troubleshooting](references/troubleshooting.md).
If a Windows or macOS GUI entrypoint fails before opening, `python3 scripts/weex_doctor.py gui --fix` provides a concise diagnosis plus the managed-runtime repair path.
For detached-launch failures, inspect the newest file under `~/.weex-trader-skill/gui-launchers/*.log`; those logs are capped so they stay useful without growing forever.

## References

- [Script operations](references/script-operations.md)
- [Profile manager guide](references/profile-manager.md)
- [Profile onboarding](references/profile-onboarding.md)
- [Linux vault](references/linux-vault.md)
- [Troubleshooting](references/troubleshooting.md)
- [Auth and signing](references/auth-and-signing.md)
- [Spot endpoints](references/spot-endpoints.md)
- [Contract endpoints](references/contract-endpoints.md)
- [Spot API definitions](references/spot-api-definitions.md)
- [Contract API definitions](references/contract-api-definitions.md)
- [WebSocket notes](references/websocket.md)
