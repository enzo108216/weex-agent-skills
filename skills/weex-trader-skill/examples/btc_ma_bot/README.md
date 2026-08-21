# BTCUSDT Moving-Average Test Program

This is a small WEEX futures strategy example. Its default configuration permits
offline validation only and cannot start real trading.

On each cycle, the program reads `kline_interval` candles and compares the close
price averages over `fast_ma_period` and `slow_ma_period`. A higher fast average
produces `LONG`, a lower one produces `SHORT`, and equal averages skip the cycle.
Set `direction` to `both`, `long_only`, or `short_only`. The polling interval and
target margin are configured through `poll_interval_seconds` and
`margin_amount_usdt` in `config.json`.

The requested quantity is calculated from the configured margin, the account's
current leverage, a fresh mark price, and official contract metadata, then rounded
down to the advertised quantity precision. Missing, stale, mismatched, or invalid
market/account facts stop the process before submission.

The real-trading path also requires a saved profile, authorized `strategy_id` and
`authorization_id`, the exact granted scope in
`authorization_max_single_amount_u`, `authorization_max_total_amount_u`, and
`authorization_valid_hours`, `live_trading_enabled` set to `true`, and the
explicit `--confirm-live` startup flag. The two amount fields are the official
conservative U-value authorization limits, not the strategy's target margin.

Before reading any market or account data, the program calls the official
`ensure-authorization` facade for the configured saved profile, strategy, Futures
module, BTCUSDT symbol, limits, and validity. It requires the full configured
authorization ID, `ACTIVE` status, and `SUBMIT_ALLOWED` action. It repeats the
same check when an actionable signal first reaches trade preparation. A missing,
expired, revoked, differently scoped, mismatched, or restore-blocked authorization
stops the process before account and sizing reads. If no exact authorization is
active, the official facade can create a local pending authorization request, but
the example never grants it automatically.

The program skips opening when it finds any nonzero BTCUSDT position. A process
submits at most one order and exits after any result, including `REVIEW_REQUIRED`,
without retrying. `submit-auto` independently revalidates authorization, scope,
risk facts, and quota immediately before any WEEX write.

The program invokes the official `weex_contract_api.py` and `weex_auto_trade.py`
CLIs as subprocesses. It neither accepts nor stores an API key, secret, or
passphrase, and it does not import the automatic-trading state database. The user
must separately configure a saved profile and grant strategy authorization before
first use; this example does not register or grant authorization itself. The
startup check does not read or modify Vault credentials and does not access the
authorization database directly.

This example only opens one position. It does not close or reverse positions,
change leverage, or create take-profit or stop-loss orders.

Validate the configuration without network or account access:

```bash
python3 skills/weex-trader-skill/examples/btc_ma_bot/btc_ma_bot.py --check-config
```

The validation command above does not request market data, read an account, or
submit an order.
