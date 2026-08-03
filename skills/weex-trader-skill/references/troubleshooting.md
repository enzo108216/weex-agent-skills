# Troubleshooting

Use this reference for common operator-visible failures, likely causes, and the next command or check to run.

## Contents

- Common issues
- Debugging inputs

## Common Issues

### Missing Python Dependency

Symptom:

- `ModuleNotFoundError: cryptography`
- other missing Python dependency failures

Action:

- run `scripts/weex_runtime_setup.py --pretty` with the same launcher when you want one command to recover `pip` + install `requirements.lock` with hash verification
- private contract and spot CLIs auto-attempt that helper only for the saved-profile path; the complete fixed environment credential set does not need profile/Vault dependencies
- install `requirements.lock` with `--require-hashes` using the same interpreter used to launch the scripts
- retry with the same launcher (`py -3` on Windows, `python3` on macOS/Linux)

### Invalid Runtime Environment Override

Symptom:

- `Private WEEX command preflight failed`
- `WEEX_API_TIMEOUT must be a positive number of seconds`
- `WEEX_API_BASE` / `WEEX_CONTRACT_API_BASE` / `WEEX_SPOT_API_BASE` must be full `https://` URLs on `weex.com`, `*.weex.com`, `weex.tech`, or `*.weex.tech`

Action:

- fix or unset the invalid environment variable override
- rerun `scripts/weex_agent_state.py --command skill.preflight --pretty`
- retry the same private command after `runtime.env_validation.ok` becomes `true`

### Windows Or macOS GUI Crashes Before Opening

Symptom:

- the profile manager or vault UI exits immediately
- macOS shows a `Python quit unexpectedly` dialog
- the current interpreter can import `tkinter`, but creating a window fails
- preflight says the system/miniforge Python can launch Tk but the managed GUI runtime is missing

Action:

- ask the AI assistant to install the managed GUI runtime; after confirmation it will run the bootstrap with pinned installer checksum verification and locked dependency hashes:

```bash
python3 scripts/weex_gui_bootstrap.py probe --pretty
python3 scripts/weex_gui_bootstrap.py ensure --accept-managed-runtime --pretty
python3 scripts/weex_doctor.py gui
```

- retry the same GUI entrypoint after `ensure` succeeds; Windows/macOS GUI launch requires the managed runtime even if the system interpreter has Tk
- if the GUI was launched through `scripts/weex_gui_launcher.py`, inspect the newest file under `~/.weex-trader-skill/gui-launchers/*.log`; detached-launch logs are capped at 256 KiB and old launch records are pruned automatically
- if `WEEX_GUI_FORCE_FOREGROUND=1` is set, clear it unless you intentionally want the GUI attached to the current shell for debugging
- if you intentionally do not want the managed runtime, use the terminal profile commands instead of launching the GUI

### Authentication Or Signature Error

Symptom:

- request rejected by WEEX auth or signing checks

Action:

- for direct environment-authenticated contract/spot calls, verify `WEEX_API_KEY`, `WEEX_API_SECRET`, and `WEEX_API_PASSPHRASE` are all present and belong to the same API credential
- if using `--profile`, verify the intended saved profile is selected and re-check its API Key, Secret Key, and Passphrase
- verify that any optional `WEEX_CONTRACT_API_BASE`, `WEEX_SPOT_API_BASE`, or `WEEX_API_BASE` points to the environment where the credential was issued
- review `references/auth-and-signing.md`

### Incomplete Direct Environment Credentials

Symptom:

- a direct private contract/spot command reports that the three WEEX environment credentials must be set together

Action:

- set `WEEX_API_KEY`, `WEEX_API_SECRET`, and `WEEX_API_PASSPHRASE` together, or unset all three and use a saved profile
- do not mix credentials from different API keys or accounts
- remember that base URL, timeout, and locale environment variables are optional and do not replace any of the three credentials

### Broken Or Missing Default Profile

Symptom:

- a saved-profile private command fails because no usable default profile is available and no explicit `--profile` was selected

Action:

- inspect profiles with `scripts/weex_profiles_zh.py list --pretty` or `scripts/weex_profiles_en.py list --pretty`
- use `show --profile <name-or-id> --pretty` on the intended profile
- for direct contract/spot REST only, a complete fixed environment credential set is an alternative to the default profile
- public commands such as `ticker` and `list-endpoints` should still work without a valid default profile

### Linux Vault Is Locked

Symptom:

- `list` and `show` return metadata, but `has_credentials` is `null`
- `credentials_status` is `unknown_locked`

Action:

- if mode is `manual_once`, unlock first:

```bash
python3 scripts/weex_vault_zh.py unlock --pretty

# For non-Chinese users
python3 scripts/weex_vault_en.py unlock --pretty
```

- after the sensitive operation is complete, lock it again:

```bash
python3 scripts/weex_vault_zh.py lock --pretty

# For non-Chinese users
python3 scripts/weex_vault_en.py lock --pretty
```

### Linux Vault Locked

Symptom:

- vault status reports `action_required: "unlock"`
- profile commands report that the vault is locked

Action:

- ask the user to unlock the vault with the current passphrase
- do not generate the passphrase on the user's behalf
- retry the profile command only after unlock succeeds

### macOS Credential Check Looks Inconsistent

Symptom:

- profile manager says save succeeded
- a later sandboxed check reports `has_credentials: false`

Action:

- re-run the check from an OS-permitted context
- do not treat a sandbox-only false negative as authoritative

### Order Rejected

Symptom:

- order placement succeeds syntactically but WEEX rejects it

Action:

- verify account balance
- verify API key permissions
- verify market symbol and order parameters

## Debugging Inputs

Useful first checks:

```bash
python3 scripts/weex_profiles_en.py list --pretty
python3 scripts/weex_vault_en.py status --pretty
python3 scripts/weex_contract_api.py list-endpoints --pretty
python3 scripts/weex_spot_api.py list-endpoints --pretty
```
