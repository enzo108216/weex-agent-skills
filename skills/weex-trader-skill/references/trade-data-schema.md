# Trade Data Schema

The trader aggregation layer emits normalized JSON for replay, profile, order-risk, and account-risk payloads.

## Replay Payload

```json
{
  "analysis_type": "replay",
  "market": "futures",
  "period": "30d",
  "symbol": "BTCUSDT",
  "focus": "losses",
  "time_range": {
    "start_ms": 1710000000000,
    "end_ms": 1712592000000
  },
  "closed_trade_count": 12,
  "orders": [],
  "fills": [],
  "positions": [],
  "balances": [],
  "bills": [],
  "price_series": [],
  "constraints": [],
  "partial": false,
  "degraded_reasons": []
}
```

Notes:

- `market`: `futures`, `spot`, or `all`
- `period`: replay accepts `7d`, `30d`, `90d`; profile fallback windows may also use `180d` or `360d`
- `constraints`: explicit limits such as `spot_symbol_required`
- `partial`: whether the aggregation layer could not prove the dataset is complete for the requested window
- `degraded_reasons`: machine-readable reasons such as `spot_kline_window_unbounded`, `futures_fills_limit_hit`, `spot_tp_sl_state_unavailable`, or `spot_history_skipped_without_symbol`
- `balances[*].account_scope`, `positions[*].account_scope`, `orders[*].account_scope`, `fills[*].account_scope`, and `bills[*].account_scope` default to `personal_futures` or `personal_spot`
- `positions[*].margin_type` is normalized from upstream `marginType` when available
- `positions[*].position_mode` is normalized from `positionMode` / `separatedMode`; `ONE_WAY` maps to `COMBINED`, `HEDGE` maps to `SEPARATED`
- `orders[*].margin_type`, `orders[*].position_mode`, `fills[*].margin_type`, and `fills[*].position_mode` are best-effort fields in Phase 1: they are preserved when upstream context already includes them, otherwise they remain `null`
- `market=all` without `symbol` runs in degraded mode in Phase 1: futures history is still collected, while spot symbol-specific history is skipped, `partial=true`, and the skip is surfaced through `constraints` plus `spot_history_skipped_without_symbol`

## Normalized Row Examples

### Position Row

```json
{
  "account_scope": "personal_futures",
  "market": "futures",
  "symbol": "BTCUSDT",
  "side": "long",
  "margin_type": "CROSSED",
  "position_mode": "COMBINED",
  "quantity": 0.01,
  "notional": 650.0,
  "leverage": 10.0,
  "created_time": 1710000000000,
  "updated_time": 1710003600000
}
```

### Order / Fill Context Fields

- `account_scope` is always present on normalized orders and fills
- `margin_type` and `position_mode` are nullable on historical orders/fills in the current minimal implementation because the WEEX history endpoints do not consistently expose that context on every row

## Profile Payload

`collect-profile` reuses the replay schema and adds:

- `analysis_type = "profile"`
- `selected_period`
- `fallback_applied`
- `sample_quality`

Notes:

- the trader skill does not embed downstream profile metrics in the payload
- downstream analysis can derive profile metrics, risk scoring, and persona labels from the replay rows when needed

## Order-Risk Payload

```json
{
  "order_preview": {
    "market": "futures",
    "symbol": "BTCUSDT",
    "side": "BUY",
    "position_side": "LONG",
    "order_type": "LIMIT",
    "quantity": 0.01,
    "price": 65000
  },
  "tp_sl": {
    "has_take_profit": false,
    "has_stop_loss": false
  },
  "account_snapshot": {
    "equity": 1000,
    "available_balance": 250
  },
  "positions": [],
  "recent_orders": [],
  "open_orders": [],
  "conditional_orders": [],
  "market_snapshot": {
    "current_price": 64850
  },
  "partial": false,
  "degraded_reasons": []
}
```

Notes:

- `order_preview` mirrors the order that will later be submitted if the user confirms
- `recent_orders` is used to detect short-window overtrading
- `market_snapshot.current_price` is the price anchor for limit-price distance checks
- futures order-risk payloads can include `conditional_orders` so `missing_tp_sl` is based on live protection state instead of only the order preview

## Account-Risk Payload

```json
{
  "mode": "account_scan",
  "market": "futures",
  "symbol": "BTCUSDT",
  "account_snapshot": {
    "equity": 1000,
    "available_balance": 120
  },
  "positions": [],
  "recent_orders": [],
  "open_orders": [],
  "conditional_orders": [],
  "market_snapshot": {
    "current_price": 64850
  },
  "partial": false,
  "degraded_reasons": [],
  "constraints": []
}
```

Notes:

- `mode = account_scan` is intentionally separate from pre-order risk payloads
- account-risk payloads do not require `order_preview`
