# Auth and Signing (Compact)

REST base URLs:
- contract: `https://api-contract.weex.com`
- spot: `https://api-spot.weex.com`
- custom base URLs must use `https://` and a host under `weex.com` or `weex.tech`

Private headers:
- `ACCESS-KEY`
- `ACCESS-PASSPHRASE`
- `ACCESS-TIMESTAMP` (ms)
- `ACCESS-SIGN`

Signing message:
- no query: `timestamp + METHOD + requestPath + body`
- with query: `timestamp + METHOD + requestPath + "?" + queryString + body`

Signature:
- `Base64(HMAC_SHA256(secret, message))`

Supported credential sources for direct contract/spot REST:
- container/runtime environment: `WEEX_API_KEY`, `WEEX_API_SECRET`, and `WEEX_API_PASSPHRASE`
- profile metadata in `~/.weex-trader-skill/profiles.meta.json` with secrets in the Application Vault
  - Windows/macOS: application vault with UI-first unlock/setup flows
  - Linux: application vault with terminal/manual_once flows

Credential precedence:
- an explicit `--profile` uses that saved profile
- without `--profile`, a complete fixed environment set is used before the configured default profile
- the three fixed environment variables must be provided together; a partial set fails closed
- direct contract/spot private calls can therefore run without a saved profile
- Partner, aggregation, trade-guard, and profile-management flows still use saved profiles

Optional environment overrides still supported:
- `WEEX_TRADER_SKILL_HOME`: override the runtime state directory for profiles, vault files, and agent cache
- `WEEX_API_TIMEOUT`: override HTTP timeout in seconds for API calls. Partner REST defaults to 30 seconds; contract and spot keep their existing defaults. A timeout never authorizes an automatic retry.
- `WEEX_CONTRACT_API_BASE` / `WEEX_SPOT_API_BASE`: select product-specific contract/spot hosts; use these for staging because the two products have different hosts
- `WEEX_API_BASE`: shared fallback base URL when a product-specific base is not set
- `WEEX_LOCALE`: override the locale header for direct contract/spot calls

Only the three credential variables are required for environment-authenticated direct contract/spot REST. Every variable in this optional list may be omitted; the official production hosts, default timeout, and default locale are then used.

Credential source policy:
- prefer runtime secret injection for containerized direct contract/spot calls and saved profiles for interactive use
- if neither a complete fixed environment set nor a usable saved profile exists, fail fast
- for server automation, save/rotate profile secrets with `--secrets-stdin-json` or `--api-key-env` / `--api-secret-env` / `--api-passphrase-env` instead of raw argv secrets
- public endpoints and endpoint-listing commands do not require a valid default profile
- an explicitly requested `--profile` must still resolve successfully
- on Linux `manual_once`, `list/show` may return `has_credentials: null` with `credentials_status: "unknown_locked"` until the vault is unlocked

Main reference:
- https://www.weex.com/api-doc/spot/QuickStart/Signature
