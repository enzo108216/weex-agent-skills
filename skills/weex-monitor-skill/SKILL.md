---
name: weex-monitor-skill
description: Use when the user wants a WEEX automated monitor for position PnL or symbol price conditions that drafts, confirms, stores, evaluates, and reports monitor tasks while delegating live execution to weex-trader-skill.
compatibility: Requires Python for the bundled monitor task scripts. Live WEEX REST access, profile storage, vault access, signing, and order submission belong to weex-trader-skill.
---

# WEEX Monitor Skill

Read `manifest.json` for routing rules. Open `file-index.json` only when you need file-level guidance.

This skill owns WEEX automated monitor orchestration. It converts a constrained monitor intent into a task DSL, requires explicit monitor confirmation, stores local task state in SQLite, appends audit events, evaluates local PnL triggers, builds native price TP/SL request bodies, and reports results to the current thread.

This skill does not own API credentials, profile storage, vault unlock, REST signing, or live order submission. Those capabilities stay in `weex-trader-skill`.

## Supported MVP Scenarios

- `position_pnl_monitor`: monitor one futures position by `symbol` and `position_side`, compare `unrealized_pnl` against a threshold, then prepare a direction-specific market-close plan.
- `symbol_price_monitor`: build a native WEEX TP/SL conditional order body for direction-specific close by `symbol`, `position_side`, trigger price, and quantity.

Do not expand this skill to open positions, add margin, change leverage, reverse positions, spot trading, grid trading, trailing stops, multi-account tasks, or arbitrary script execution unless the skill policy and tests are updated first.

## Core Entry Point

- `scripts/weex_monitor_cli.py`: normalize monitor tasks, render confirmation text, require `--confirm-monitor`, persist active tasks, append events, build price TP/SL bodies, evaluate PnL snapshots, run dry-run checks, list tasks/events, and cancel local tasks.

## Safety Policy

- Never send mutating requests from this skill.
- Never send mutating requests without explicit user confirmation and `weex-trader-skill` `--confirm-live`.
- Use this skill to create a monitor plan or evaluate a trigger; use `weex-trader-skill` for any live REST call, profile lookup, vault operation, signing, or order submission.
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
- `run-once` requires `--dry-run` and must never submit a live order.
