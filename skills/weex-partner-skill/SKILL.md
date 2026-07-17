---
name: weex-partner-skill
description: Use when the user wants WEEX Partner referral, commission, referral asset, direct-user trade, internal transfer status, sub-agent, or referral relationship queries.
---

# WEEX Partner Skill

Use this skill as the natural-language entry point for the eight read-only WEEX Partner query capabilities. REST access, profile resolution, Vault credentials, signing, HTTPS, exact-host enforcement, and transport remain owned by `weex-trader-skill` through `scripts/weex_partner_cli.py`.

## Route First

- Partner data query: use this skill and one of the eight CLI operations below.
- Normal order, cancel, or conditional-order request: route to `weex-trader-skill`. A profile that can call Partner APIs can still trade normally. Preserve trader risk preview and the existing `--confirm-live` gate for live mutation.
- PnL, fills, exposure, or account-risk analysis: route to `weex-analysis-skill` after trader collects normalized live data.
- PnL monitoring: route to `weex-monitor-skill`, which delegates any live action to trader.
- Partner transfer creation or any other Partner write: explain that it is unsupported. Never translate it into the read-only Partner POST.

Do not create a separate account category for this skill. Reuse the user's existing saved profile and Application Vault. If setup is missing, follow `weex-trader-skill` profile/Vault guidance; never ask for secrets in chat or argv.

## Operations

- `list-referral-uids`
- `get-direct-trade-asset`
- `get-commission`
- `get-internal-withdrawals`
- `get-sub-agent-stats`
- `verify-referrals`
- `get-referral-assets`
- `get-referral-deal-data`

Open `references/partner-query-policy.md` before constructing a request. Pass structured JSON by stdin or a non-secret request file to `scripts/weex_partner_cli.py <operation>`. Do not call Partner REST directly from this skill.

## Required Gates

- Require a saved profile reference. Let trader load credentials from Vault.
- Missing UID must never silently mean all referrals. Require `scope.mode=all` and `all_confirmed=true` for an all-referrals query.
- Ask only for missing required fields, such as `product_type`, UID, or explicit start/end.
- When the official contract supplies multiple legal ranges and time is omitted, use its smallest range. State the default basis and actual UTC start/end. Explicit valid user time takes priority.
- If only a maximum/history limit is known and no minimum is published, require explicit start/end.
- Default to `summary_with_first_20`. Fetch all pages only after an explicit complete-list or aggregate request.
- Treat `complete`, `partial`, and pagination fields as authoritative. Never convert a partial aggregate into a total.

The trader executor allows only eight `operation_class=read` entries and only `https://api-spot.weex.com`. The query-only POST is not a mutation. Unknown endpoints, the internal-withdrawal write endpoint, base URL overrides, redirects, and credential disclosure are forbidden.

## Explain Results

Open `references/partner-output-schema.md`. In the user's language, present:

1. WEEX Partner production data and read-only query capability (not a claim that the API key itself is read-only).
2. Profile identity without credentials, query scope, and actual UTC range.
3. Complete or partial coverage.
4. Summary and up to the first 20 records by default.
5. If the display is incomplete: `has_more`, remaining count or next page when available, and the exact way to continue.
6. Rate-limit/error category and a safe recovery action.

Unknown response field values stay hidden. A 429 stops immediately with no automatic retry. Interrupted offset pagination must restart from page 1 for any complete result.
