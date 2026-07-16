# WEEX Partner Query Policy

This reference defines deterministic orchestration above the trader-owned REST executor.

## Operations

- `list-referral-uids`: one UID or explicitly confirmed all-referrals scope; 90-day official minimum/default segment, 365-day history boundary.
- `get-direct-trade-asset`: one UID or explicitly confirmed all-referrals scope; 90-day official minimum/default segment, 365-day history boundary.
- `get-commission`: one UID or explicitly confirmed all-referrals scope; official minimum/default is 7 days; `product_type` defaults to `SPOT`.
- `get-internal-withdrawals`: official recent-month boundary; no older continuation.
- `get-sub-agent-stats`: one sub-UID or explicitly confirmed all scope; time and `product_type` are required.
- `verify-referrals`: one or more UIDs; groups larger than 100 are split without overlap.
- `get-referral-assets`: exactly one UID and explicit `YYYY-MM-DD` start/end dates.
- `get-referral-deal-data`: one or more repeated `userIds`, or explicitly confirmed all scope, plus explicit dates.

## Time rules

When official documentation lists legal ranges such as 30, 90, and 365 days and the user omits time, select the official minimum: 30 days in that example. Always return the actual UTC start/end and `source=official_minimum_default`. Explicit valid user time wins. If only a maximum or history limit is known, require explicit start/end instead of treating the maximum as a default.

Only endpoints with complete segment and history boundaries may continue backward. The next segment uses the same minimum range and cannot overlap or leave a gap. Commission, sub-agent, asset, and deal-data queries do not auto-segment under the current contract.

## Scope and paging

An omitted singular UID never silently becomes all referrals. `scope.mode=all` requires `all_confirmed=true`. Default output is a summary plus the first 20 records. Cross-page fetching is reserved for an explicit complete-list or aggregate request. Offset pagination interrupted by an error restarts from page 1; old and new pages are never joined into a complete result.

All money aggregation uses decimal arithmetic. Unknown response field names may be reported for schema diagnosis, but their values remain hidden until classified.
