# WEEX Partner Output Schema

Every query returns one JSON envelope. The skill must preserve these values when explaining the result.

## Completion and identity

- `ok`: whether the current query succeeded.
- `complete`: whether the requested page/time coverage is complete.
- `partial`: whether usable records exist despite an interrupted query.
- `operation`, `profile.resolved_profile_id`, `api_domain`, `environment`, and `capability_mode` identify the source and read-only query capability.
- `query_scope` binds the UID or explicitly confirmed all-referrals scope.

## Time and pagination

- `time_range.source`, `actual_start`, and `actual_end` disclose the effective UTC range.
- `pagination.pages_fetched`, `pages_total`, `records_fetched`, and `records_total` disclose coverage.
- `pagination.displayed_count`, `has_more`, `remaining_count`, and `next_page` disclose truncation and the next available page.
- `continuation.can_continue` and `stop_reason` explain whether and how to continue.
- Interrupted offset pagination sets `restart_required=true` and `restart_from_page=1`.

## Results and errors

- `summary` is present only when its source coverage is complete enough for that summary. Partial aggregates are never labeled as totals.
- `records` contains at most 20 items by default and can contain a complete list only after an explicit request.
- `warnings` can report unknown field names but not unknown values.
- `error.category` distinguishes local validation, credentials, authentication, permission, rate limit, upstream, transport, schema, and completeness failures.
- `rate_limit` preserves used and remaining WEEX weight headers. Automatic retry remains disabled.
