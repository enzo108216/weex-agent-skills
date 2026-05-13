---
name: weex-monitor-skill
description: Use when the user wants a WEEX automated monitor for position PnL or symbol price conditions that drafts, confirms, stores, evaluates, and reports monitor tasks while delegating live execution to weex-trader-skill.
compatibility: Requires Python for the bundled monitor task scripts. Live WEEX REST access, profile storage, vault access, signing, and order submission belong to weex-trader-skill.
---

# WEEX Monitor Skill

Read `manifest.json` for routing rules. Open `file-index.json` only when you need file-level guidance.

This skill owns WEEX automated monitor orchestration. It converts a constrained monitor intent into a task DSL, requires explicit monitor confirmation, stores local task state in SQLite, appends audit events, evaluates local PnL triggers, builds and submits native price TP/SL plans through `weex-trader-skill`, can run a live PnL check through `weex-trader-skill`, and reports results to the current thread.

This skill does not own API credentials, profile storage, vault unlock, REST signing, or direct REST submission. Those capabilities stay in `weex-trader-skill`. Live monitor commands are orchestration wrappers around `weex-trader-skill` guard flows and require `--confirm-live`.

## AI natural-language parsing

When the user gives a natural-language monitor request, the AI layer of this skill must convert the user's monitor instruction into the Task DSL before calling the deterministic script. The script intentionally accepts JSON only; do not pass raw chat text to the script and treat a missing DSL field as a clarification need, not as permission to infer a risky value.

Required extraction fields:

- monitor object: `profile`, `symbol`, and `position_side`; profile is always required before calling the CLI because the monitor task must bind to a saved WEEX profile name
- monitor frequency: `frequency_seconds` for PnL monitors, defaulting to `5` when omitted
- trigger condition: metric, operator, and numeric threshold
- execution action: only `market_close` targeting the same `position_side`; symbol_price_monitor requires action.quantity because the exchange-native TP/SL body needs a positive close quantity
- callback: only `current_thread`

If any required field is absent or ambiguous, ask for the missing field and keep the task as a draft. Do not create a monitor for open, add, reverse, leverage, transfer, arbitrary script, or spot actions. 不要下单 by bypassing the trader guard; live execution must be delegated to `weex-trader-skill` and requires `--confirm-live`.

Examples:

- User: `BTCUSDT 多单未实现盈利大于 50 时自动平多，每 5 秒检查一次。`
  - DSL: `position_pnl_monitor`, `symbol=BTCUSDT`, `position_side=LONG`, `condition.metric=unrealized_pnl`, `condition.operator=>`, `condition.threshold=50`, `action.target=LONG`, `frequency_seconds=5`.
- User: `ETHUSDT 空单价格小于 2500 时市价平空，数量 0.2。`
  - DSL: `symbol_price_monitor`, `symbol=ETHUSDT`, `position_side=SHORT`, `condition.metric=last_price`, `condition.operator=<`, `condition.threshold=2500`, `action.quantity=0.2`, `action.target=SHORT`.

## Supported Scenarios

- `position_pnl_monitor`: monitor one futures position by `symbol` and `position_side`, compare `unrealized_pnl` against a threshold, then use `weex-trader-skill` to preview and confirm a direction-specific market-close order when `run-live-once --confirm-live` is used.
- `symbol_price_monitor`: build a native WEEX TP/SL conditional order body for direction-specific close by `symbol`, `position_side`, trigger price, and quantity; `submit-price-order --confirm-live` submits it through `weex-trader-skill` TP/SL guard.

Do not expand this skill to open positions, add margin, change leverage, reverse positions, spot trading, grid trading, trailing stops, multi-account tasks, or arbitrary script execution unless the skill policy and tests are updated first.

## Core Entry Point

- `scripts/weex_monitor_cli.py`: normalize monitor tasks, render confirmation text, require `--confirm-monitor`, persist active tasks, append events, build price TP/SL bodies, submit price TP/SL through `weex-trader-skill`, evaluate PnL snapshots, run dry-run checks and dry-run loops, run live PnL checks through `weex-trader-skill`, list tasks/events, and cancel local tasks.

## Safety Policy

- Never send mutating requests directly from this skill's REST code path; live execution must go through `weex-trader-skill` guard commands.
- Never send mutating requests without explicit user confirmation and `weex-trader-skill` `--confirm-live`.
- Use this skill to create, confirm, store, evaluate, and run monitor tasks; use `weex-trader-skill` for any live REST call, profile lookup, vault operation, signing, or order submission.
- Do not import trader internals such as `weex_contract_api`, `weex_trade_guard`, or `weex_profile_store` into this skill's deterministic monitor script.
- Do not default to WEEX `closePositions(symbol)` for "close long" or "close short"; that endpoint cannot express `positionSide`.
- Directional close is mandatory. If a directional close cannot be represented or verified, report the trigger and ask for manual handling instead of submitting a live order.
- The first callback channel is `current_thread` only.

## Task DSL

```json
{
  "task_type": "position_pnl_monitor",
  "profile": "demo",
  "market": "futures",
  "symbol": "BTCUSDT",
  "position_side": "LONG",
  "frequency_seconds": 5,
  "condition": {
    "metric": "unrealized_pnl",
    "operator": ">",
    "threshold": "50"
  },
  "action": {
    "type": "market_close",
    "target": "LONG"
  },
  "callback": {
    "type": "current_thread"
  }
}
```

## Operating Rules

- A task starts as `draft`; it becomes `active` only after `--confirm-monitor`.
- PnL monitors default to `5` seconds and reject values below `3` seconds.
- Price monitors should prefer exchange-native TP/SL conditional orders over local polling.
- Price TP/SL bodies must include `positionSide`; do not emit `closePositions`.
- One task should trigger at most once. Persist the final task state and report the outcome.
- Treat missing fields, missing positions, zero size, degraded input, or ambiguous direction as non-executable.
- Local task state is stored in `monitor-tasks.sqlite3`; legacy `monitor-tasks.json` may only be read as a fallback.
- dry-run commands still write local SQLite task state and events so trigger handling, one-shot behavior, and reporting can be audited.
- `run-once` requires `--dry-run` and must never submit a live order.
- `run-loop` requires `--dry-run`, consumes caller-supplied position snapshots, and must never submit a live order.
- Triggered dry-runs may produce a live delegate plan, but that plan is only a summary for `weex-trader-skill` and is not an execution request.
- `submit-price-order` requires an active `symbol_price_monitor` task and `--confirm-live`; it submits the native TP/SL conditional order through `weex-trader-skill preview-tp-sl` and `confirm-tp-sl`.
- `run-live-once` requires `--confirm-live`; it collects live account risk data through `weex-trader-skill`, evaluates active PnL monitors, re-collects and revalidates the target position before submission, then executes the market close through `weex-trader-skill preview-order` and `confirm-order`.
- Live PnL execution writes `completed` and exchange response details only after trader guard returns a successful response; failed delegated commands leave the task available for operator review instead of silently retrying.
