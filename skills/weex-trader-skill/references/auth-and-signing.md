# Auth and Signing Boundary (Compact)

Trader resolves the selected profile and injects an opaque `credential_ref`
through the Local MCP host. MCP is the only owner of WEEX REST request
construction, timestamp generation, exchange signatures, and HTTPS transport.
Trader compatibility CLIs must not construct private headers or signatures.

REST base URLs:
- contract: `https://api-contract.weex.com`
- spot: `https://api-spot.weex.com`
- custom base URLs must use `https://` and a host under `weex.com` or `weex.tech`

MCP generates the private headers only at send time:
- `ACCESS-KEY`
- `ACCESS-PASSPHRASE`
- `ACCESS-TIMESTAMP` (ms)
- `ACCESS-SIGN`

The exchange signing contract implemented by MCP is:
- no query: `timestamp + METHOD + requestPath + body`
- with query: `timestamp + METHOD + requestPath + "?" + queryString + body`

Signature contract:
- `Base64(HMAC_SHA256(secret, message))`

Recommended credential source:
- profile metadata in `~/.weex-trader-skill/profiles.meta.json`
- secrets in the Application Vault
  - Windows/macOS: application vault with UI-first unlock/setup flows
  - Linux: application vault with terminal/manual_once flows

Optional environment overrides still supported:
- `WEEX_TRADER_SKILL_HOME`: override the runtime state directory for profiles, vault files, and agent cache
- `WEEX_API_TIMEOUT`: override MCP upstream timeout in seconds. Partner defaults to 30 seconds; contract and spot keep their existing defaults. A timeout never authorizes an automatic retry.

Credential source policy:
- prefer saved profiles
- private commands require a saved profile
- if private credentials are missing, fail fast and ask user to configure or fix a profile
- for server automation, save/rotate profile secrets with `--secrets-stdin-json` or `--api-key-env` / `--api-secret-env` / `--api-passphrase-env` instead of raw argv secrets
- public endpoints and endpoint-listing commands do not require a valid default profile
- an explicitly requested `--profile` must still resolve successfully
- on Linux `manual_once`, `list/show` may return `has_credentials: null` with `credentials_status: "unknown_locked"` until the vault is unlocked

Dry-run policy:

- `--dry-run` calls the MCP Core typed request-description library API
- it does not load Vault credentials, generate a real timestamp or signature, construct a Local service, or send network traffic
- authentication headers in the description are inert placeholders only
- Partner, Broker OAuth, Trade confirmation, and state-dependent simple leverage are not exposed through this descriptor

Main reference:
- https://www.weex.com/api-doc/spot/QuickStart/Signature
