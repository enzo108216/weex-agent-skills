# Script Operations

Use this reference only when direct local script execution, dependency setup, or repo maintenance is needed instead of the normal natural-language skill flow.

## Python Prerequisites

Profile, vault, private-trading, and API-definition regeneration commands require the dependencies in [requirements.txt](../requirements.txt).

```bash
# Windows
py -3 -m pip install -r requirements.txt

# macOS / Linux
python3 -m pip install -r requirements.txt
```

Before private contract or spot commands, run `scripts/weex_agent_state.py --command skill.preflight ...` and inspect `runtime.host.requirements_ready`, `runtime.host.missing_modules`, and `runtime.env_validation`. The private REST CLIs now stop immediately when those checks fail instead of waiting until profile or order execution.

One-command runtime setup:

```bash
# Windows
py -3 scripts/weex_runtime_setup.py --pretty

# macOS / Linux
python3 scripts/weex_runtime_setup.py --pretty
```

This helper installs `requirements.txt` into the current interpreter, attempts `ensurepip` first if `pip` is missing, refreshes `agent-init.json` / `agent-runtime.json`, and reports whether the interpreter is actually ready for private WEEX CLI flows.

Private contract and spot CLIs also auto-attempt this helper when the current interpreter is missing required Python dependencies. Invalid runtime overrides such as a bad `WEEX_API_TIMEOUT` value still stop immediately because the helper does not modify environment variables for you.

Command launcher policy:

- Windows: use `py -3`
- macOS / Linux: use `python3`
- GUI profile management also needs `tkinter`
- On macOS and Windows tool-managed shells, prefer `scripts/weex_gui_launcher.py` for detached GUI launch, or rely on the GUI entrypoints' built-in auto-detach behavior when you want the GUI without an extra Terminal/cmd window

## Managed GUI Runtime

On Windows and macOS, the GUI entrypoints can self-bootstrap a managed Python runtime when the current interpreter cannot initialize Tk or is missing GUI-side dependencies.

Manual repair commands:

```bash
# Windows users: replace python3 with py -3
python3 scripts/weex_gui_bootstrap.py probe --pretty
python3 scripts/weex_gui_bootstrap.py ensure --pretty
python3 scripts/weex_doctor.py gui --fix
```

Notes:

- the bootstrap stores a user-local runtime under the WEEX config directory such as `~/.weex-trader-skill/gui-runtime`
- it uses `uv` to provision a managed CPython 3.12 virtual environment and install `requirements.txt`
- the profile and vault GUI entrypoints will automatically re-launch themselves inside that managed runtime when the current interpreter is not GUI-safe
- this managed bootstrap is for the Windows/macOS GUI flows; terminal/private REST commands still run on the interpreter you launched and therefore still need their own preflight/runtime checks
- the profile and vault GUI entrypoints also auto-detach when they are started from a non-interactive/tool-managed shell on macOS or Windows
- explicit detached launch uses a transient `.app` wrapper on macOS and prefers `pythonw.exe` or another hidden background process on Windows
- detached-launch records and logs are stored under `~/.weex-trader-skill/gui-launchers`; the launcher keeps only recent records and trims each `.log` file to 256 KiB
- `scripts/weex_agent_state.py --command skill.preflight ...` will also warm this runtime automatically when GUI routing is expected but the current interpreter is not GUI-safe
- use `WEEX_GUI_RUNTIME_DISABLE=1` only when you explicitly want to suppress the bootstrap path
- use `WEEX_GUI_FORCE_FOREGROUND=1` only when you explicitly want the GUI to stay attached to the current shell, which can reintroduce a Terminal/cmd window

Detached GUI launch examples:

```bash
# Windows users: replace python3 with py -3
python3 scripts/weex_gui_launcher.py profile-manager --language zh --pretty
python3 scripts/weex_gui_launcher.py vault-manager --language zh --requested-action setup --pretty
```

Vault `--requested-action` values:

- `setup`: open the vault UI focused on initialization; if the vault is currently uninitialized, the window immediately starts the passphrase flow
- `unlock`: open the vault UI focused on unlocking; if the vault is currently locked, the window immediately starts the passphrase flow
- `status`: open the vault UI focused on reviewing the current state only; it does not unlock or lock by itself
- `lock`: open the vault UI focused on the lock workflow; it does not lock by itself until the user presses the button in the window

Windows/macOS vault command routing:

- `python3 scripts/weex_vault.py` with no subcommand opens the vault UI
- bare `setup` and bare `unlock` also open the vault UI by default
- `status`, `lock`, `mode`, `change-password`, and any command that includes extra CLI flags stay in the terminal unless you explicitly use `scripts/weex_gui_launcher.py vault-manager ...`
- use `--cli` when you explicitly want the terminal flow for `setup` or `unlock` on Windows/macOS

## Command Context

Run the shell commands below from the skill root.

If you stay outside the skill root, prefix repo-relative paths with the full skill path. For example:

```text
py -3 E:\path\to\weex-trader-skill\scripts\weex_spot_api.py --help
python3 /path/to/weex-trader-skill/scripts/weex_spot_api.py --help
```

The examples below are written as single-line commands so they can be pasted into PowerShell, bash, or zsh without changing the line continuation style.

## Quick Start

Public market data works without any API credentials:

```bash
# Windows users: replace python3 with py -3
python3 scripts/weex_contract_api.py ticker --symbol BTCUSDT --pretty
python3 scripts/weex_spot_api.py ticker --symbol BTCUSDT --pretty
```

List bundled endpoints:

```bash
# Windows users: replace python3 with py -3
python3 scripts/weex_contract_api.py list-endpoints --pretty
python3 scripts/weex_spot_api.py list-endpoints --pretty
```

## Trading Commands

Representative futures order:

```bash
# Windows users: replace python3 with py -3
python3 scripts/weex_contract_api.py --profile main place-order --symbol ETHUSDT --side SELL --position-side SHORT --type LIMIT --quantity 0.001 --price 10000 --time-in-force GTC --confirm-live --pretty
```

Representative spot order:

```bash
# Windows users: replace python3 with py -3
python3 scripts/weex_spot_api.py --profile main place-order --symbol ETHUSDT --side BUY --order-type LIMIT --quantity 0.001 --price 999 --time-in-force GTC --confirm-live --pretty
```

Current convenience wrappers:

- Spot: `ticker`, `place-order`
- Futures: `ticker`, `poll-ticker`, `place-order`, `cancel-order`

For broader spot or futures cancel/query/history flows, use the generic `call` command with the bundled endpoint catalogs.

## Regenerate Definitions

To rebuild local spot and futures REST definitions from the current WEEX V3 docs:

```bash
# Windows users: replace python3 with py -3
python3 scripts/generate_weex_api_definitions.py --product all
```
