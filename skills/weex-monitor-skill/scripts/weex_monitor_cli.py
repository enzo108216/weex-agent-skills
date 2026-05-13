#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


MIN_FREQUENCY_SECONDS = 3
DEFAULT_FREQUENCY_SECONDS = 5
TASK_STORE_FILENAME = "monitor-tasks.json"
TASK_DB_FILENAME = "monitor-tasks.sqlite3"
VALID_TASK_TYPES = {"position_pnl_monitor", "symbol_price_monitor"}
VALID_POSITION_SIDES = {"LONG", "SHORT"}
VALID_OPERATORS = {">", ">=", "<", "<="}
VALID_CALLBACK_TYPES = {"current_thread"}
VALID_TRIGGER_PRICE_TYPES = {"CONTRACT_PRICE", "MARK_PRICE"}
VALID_MARKETS = {"futures"}


class MonitorInputError(ValueError):
    """Raised when a monitor task cannot be safely normalized or evaluated."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def monitor_home() -> Path:
    configured = os.environ.get("WEEX_MONITOR_SKILL_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".weex-monitor-skill"


def tasks_path() -> Path:
    return monitor_home() / TASK_STORE_FILENAME


def db_path() -> Path:
    return monitor_home() / TASK_DB_FILENAME


def trader_scripts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "weex-trader-skill" / "scripts"


def _trader_script_command(script_name: str, *args: str) -> list[str]:
    return [sys.executable, str(trader_scripts_dir() / script_name), *args]


def _run_json_command(command: list[str]) -> Any:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise MonitorInputError(f"delegated command failed ({completed.returncode}): {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MonitorInputError("delegated command did not return JSON") from exc


def load_tasks() -> list[dict[str, Any]]:
    database = db_path()
    if database.exists():
        with _connect() as conn:
            rows = conn.execute(
                "SELECT task_json FROM monitor_tasks ORDER BY created_at_ms, task_id"
            ).fetchall()
        return [json.loads(row["task_json"]) for row in rows]

    path = tasks_path()
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise MonitorInputError(f"{path} must contain a JSON array")
    return payload


def save_tasks(tasks: list[dict[str, Any]]) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM monitor_tasks")
        for task in tasks:
            _upsert_task(conn, task, updated_at_ms=_now_ms())


def load_events(task_id: str | None = None) -> list[dict[str, Any]]:
    if not db_path().exists():
        return []
    query = "SELECT event_id, task_id, event_type, created_at_ms, payload_json FROM monitor_events"
    params: tuple[Any, ...] = ()
    if task_id is not None:
        query += " WHERE task_id = ?"
        params = (task_id,)
    query += " ORDER BY event_id"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "event_id": row["event_id"],
            "task_id": row["task_id"],
            "event_type": row["event_type"],
            "created_at_ms": row["created_at_ms"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


def normalize_task(raw_task: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    if not isinstance(raw_task, dict):
        raise MonitorInputError("task must be a JSON object")

    task_type = _required_string(raw_task, "task_type")
    if task_type not in VALID_TASK_TYPES:
        raise MonitorInputError(f"unsupported task_type: {task_type}")

    profile = _required_string(raw_task, "profile")
    market = _normalize_market(raw_task.get("market"))
    symbol = _required_string(raw_task, "symbol").upper()
    position_side = _normalize_position_side(raw_task.get("position_side"))
    condition = _normalize_condition(raw_task.get("condition"), task_type)
    action = _normalize_action(raw_task.get("action"), position_side, task_type)
    callback = _normalize_callback(raw_task.get("callback"))
    frequency_seconds = _normalize_frequency(raw_task.get("frequency_seconds"))
    created_at_ms = now_ms if now_ms is not None else _now_ms()

    task_id = str(raw_task.get("task_id") or _new_task_id())
    task: dict[str, Any] = {
        "task_id": task_id,
        "task_type": task_type,
        "profile": profile,
        "market": market,
        "symbol": symbol,
        "position_side": position_side,
        "frequency_seconds": frequency_seconds,
        "condition": condition,
        "action": action,
        "callback": callback,
        "status": str(raw_task.get("status") or "draft"),
        "created_at_ms": created_at_ms,
        "execution_delegate": "weex-trader-skill",
    }

    if task_type == "symbol_price_monitor":
        task["trigger_price_type"] = _normalize_trigger_price_type(
            raw_task.get("trigger_price_type") or raw_task.get("triggerPriceType")
        )

    return task


def build_price_tp_sl_order_body(task: dict[str, Any]) -> dict[str, str]:
    normalized = normalize_task(task)
    if normalized["task_type"] != "symbol_price_monitor":
        raise MonitorInputError("build_price_tp_sl_order_body requires symbol_price_monitor")

    quantity = normalized["action"].get("quantity")
    if not quantity:
        raise MonitorInputError("symbol_price_monitor market_close action requires quantity")

    threshold = normalized["condition"]["threshold"]
    return {
        "symbol": normalized["symbol"],
        "clientAlgoId": _client_algo_id(normalized["task_id"]),
        "planType": _price_plan_type(
            normalized["position_side"],
            normalized["condition"]["operator"],
        ),
        "triggerPrice": threshold,
        "executePrice": "0",
        "quantity": str(quantity),
        "positionSide": normalized["position_side"],
        "triggerPriceType": normalized.get("trigger_price_type", "CONTRACT_PRICE"),
    }


def evaluate_pnl_task(task: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = normalize_task(task)
    if normalized["task_type"] != "position_pnl_monitor":
        raise MonitorInputError("evaluate_pnl_task requires position_pnl_monitor")
    if not isinstance(positions, list):
        raise MonitorInputError("positions must be a JSON array")

    target = _find_position(normalized, positions)
    if target is None:
        return {
            "triggered": False,
            "reason": "position_not_found",
            "execution_delegate": "weex-trader-skill",
        }

    pnl_value = _decimal_from_any(_first_present(target, ("unrealizePnl", "unrealizedPnl", "unrealized_pnl")), "unrealized_pnl")
    threshold = Decimal(normalized["condition"]["threshold"])
    operator = normalized["condition"]["operator"]
    matched = _compare(pnl_value, operator, threshold)
    if not matched:
        return {
            "triggered": False,
            "reason": "condition_not_matched",
            "current_value": str(pnl_value),
            "threshold": str(threshold),
            "execution_delegate": "weex-trader-skill",
        }

    quantity = normalized["action"].get("quantity") or _position_size(target)
    return {
        "triggered": True,
        "reason": "condition_matched",
        "execution_delegate": "weex-trader-skill",
        "trigger_snapshot": {
            "symbol": normalized["symbol"],
            "position_side": normalized["position_side"],
            "unrealized_pnl": str(pnl_value),
            "threshold": normalized["condition"]["threshold"],
            "operator": operator,
        },
        "close_order": {
            "symbol": normalized["symbol"],
            "side": _close_order_side(normalized["position_side"]),
            "position_side": normalized["position_side"],
            "order_type": "MARKET",
            "quantity": str(quantity),
        },
    }


def confirm_task(raw_task: dict[str, Any], *, confirm_monitor: bool, now_ms: int | None = None) -> dict[str, Any]:
    if not confirm_monitor:
        raise MonitorInputError("refusing to activate monitor task without --confirm-monitor")
    confirmed_at_ms = now_ms if now_ms is not None else _now_ms()
    task = normalize_task(raw_task, now_ms=confirmed_at_ms)
    task["status"] = "active"
    task["confirmed_at_ms"] = confirmed_at_ms
    with _connect() as conn:
        _upsert_task(conn, task, updated_at_ms=confirmed_at_ms)
        _append_event(
            conn,
            task["task_id"],
            "task_confirmed",
            {"status": "active", "task": task},
            created_at_ms=confirmed_at_ms,
        )
    return task


def cancel_task(task_id: str, *, now_ms: int | None = None) -> dict[str, Any]:
    cancelled_at_ms = now_ms if now_ms is not None else _now_ms()
    tasks = load_tasks()
    for task in tasks:
        if task.get("task_id") == task_id:
            if task.get("status") in {"completed", "cancelled"}:
                return task
            task["status"] = "cancelled"
            task["cancelled_at_ms"] = cancelled_at_ms
            with _connect() as conn:
                _upsert_task(conn, task, updated_at_ms=cancelled_at_ms)
                _append_event(
                    conn,
                    task_id,
                    "task_cancelled",
                    {"status": "cancelled", "task": task},
                    created_at_ms=cancelled_at_ms,
                )
            return task
    raise MonitorInputError(f"task_id not found: {task_id}")


def render_confirmation_text(raw_task: dict[str, Any], *, now_ms: int | None = None) -> str:
    task = normalize_task(raw_task, now_ms=now_ms)
    condition = task["condition"]
    action = task["action"]
    parts = [
        "WEEX monitor confirmation",
        f"task_id: {task['task_id']}",
        f"profile: {task['profile']}",
        f"symbol: {task['symbol']}",
        f"position_side: {task['position_side']}",
        f"condition: {condition['metric']} {condition['operator']} {condition['threshold']}",
        f"action: {action['type']} {action['target']}",
        f"callback: {task['callback']['type']}",
        "Activate only with --confirm-monitor.",
        "This skill does not submit live orders.",
        "Any live execution must be delegated to weex-trader-skill with --confirm-live.",
    ]
    if task["task_type"] == "symbol_price_monitor":
        parts.insert(7, f"quantity: {action['quantity']}")
        parts.insert(8, f"trigger_price_type: {task.get('trigger_price_type', 'CONTRACT_PRICE')}")
    else:
        parts.insert(7, f"frequency_seconds: {task['frequency_seconds']}")
    return "\n".join(parts)


def build_idempotency_key(raw_task: dict[str, Any], purpose: str) -> str:
    if not purpose or str(purpose).strip() == "":
        raise MonitorInputError("purpose is required")
    task = normalize_task(raw_task)
    fingerprint_payload = {
        "task_type": task["task_type"],
        "symbol": task["symbol"],
        "position_side": task["position_side"],
        "condition": task["condition"],
        "action": task["action"],
        "purpose": str(purpose).strip(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"monitor:{task['task_id']}:{purpose}:{fingerprint}"


def run_once_dry_run(
    positions: list[dict[str, Any]],
    *,
    task_id: str | None = None,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    evaluated_at_ms = now_ms if now_ms is not None else _now_ms()
    if not isinstance(positions, list):
        raise MonitorInputError("positions must be a JSON array")

    results: list[dict[str, Any]] = []
    for task in load_tasks():
        if task.get("status") != "active":
            continue
        if task_id is not None and task.get("task_id") != task_id:
            continue
        if task.get("task_type") != "position_pnl_monitor":
            continue

        result = evaluate_pnl_task(task, positions)
        idempotency_key = build_idempotency_key(task, "dry-run-trigger")
        output = {
            "task_id": task["task_id"],
            "dry_run": True,
            "idempotency_key": idempotency_key,
            "result": result,
        }
        if result.get("triggered"):
            output["live_delegate_plan"] = build_live_delegate_plan(
                task,
                result,
                purpose="dry-run-trigger",
            )
        output["thread_report"] = render_thread_report(output)
        with _connect() as conn:
            _append_event(
                conn,
                task["task_id"],
                "dry_run_evaluated",
                output,
                created_at_ms=evaluated_at_ms,
            )
            if result.get("triggered"):
                updated_task = dict(task)
                updated_task["status"] = "triggered"
                updated_task["triggered_at_ms"] = evaluated_at_ms
                updated_task["trigger_snapshot"] = result.get("trigger_snapshot", {})
                updated_task["last_dry_run"] = True
                _upsert_task(conn, updated_task, updated_at_ms=evaluated_at_ms)
                _append_event(
                    conn,
                    task["task_id"],
                    "dry_run_triggered",
                    output,
                    created_at_ms=evaluated_at_ms,
                )
        results.append(output)
    return results


def run_loop_dry_run(
    positions_sequence: list[list[dict[str, Any]]],
    *,
    iterations: int,
    task_id: str | None = None,
    sleep_seconds: float = 0,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise MonitorInputError("iterations must be >= 1")
    if sleep_seconds < 0:
        raise MonitorInputError("sleep_seconds must be >= 0")
    if not isinstance(positions_sequence, list) or not positions_sequence:
        raise MonitorInputError("positions_sequence must be a non-empty JSON array")

    loop_started_at_ms = now_ms if now_ms is not None else _now_ms()
    iteration_outputs: list[dict[str, Any]] = []
    triggered_count = 0
    for index in range(iterations):
        positions = positions_sequence[min(index, len(positions_sequence) - 1)]
        if not isinstance(positions, list):
            raise MonitorInputError("each positions_sequence item must be a JSON array")
        results = run_once_dry_run(
            positions,
            task_id=task_id,
            now_ms=loop_started_at_ms + index,
        )
        triggered_count += sum(1 for item in results if item.get("result", {}).get("triggered"))
        iteration_outputs.append(
            {
                "iteration": index + 1,
                "results": results,
            }
        )
        if sleep_seconds and index + 1 < iterations:
            time.sleep(sleep_seconds)

    return {
        "dry_run": True,
        "iterations_requested": iterations,
        "iterations_completed": len(iteration_outputs),
        "triggered_count": triggered_count,
        "iterations": iteration_outputs,
        "mutating_request_submitted": False,
    }


def build_live_delegate_plan(
    raw_task: dict[str, Any],
    evaluation_result: dict[str, Any],
    *,
    purpose: str,
) -> dict[str, Any]:
    task = normalize_task(raw_task)
    if not isinstance(evaluation_result, dict):
        raise MonitorInputError("evaluation_result must be a JSON object")
    if not evaluation_result.get("triggered"):
        raise MonitorInputError("live delegate plan requires a triggered evaluation result")
    close_order = evaluation_result.get("close_order")
    if not isinstance(close_order, dict):
        raise MonitorInputError("triggered evaluation result is missing close_order")

    return {
        "delegate_skill": "weex-trader-skill",
        "requires_confirm_live": True,
        "mutating_request_submitted": False,
        "task_id": task["task_id"],
        "profile": task["profile"],
        "market": task["market"],
        "idempotency_key": build_idempotency_key(task, purpose),
        "close_order": close_order,
        "trigger_snapshot": evaluation_result.get("trigger_snapshot", {}),
        "instruction": "Submit only through weex-trader-skill after explicit --confirm-live.",
    }


def submit_price_order(
    raw_task: dict[str, Any],
    *,
    confirm_live: bool,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if not confirm_live:
        raise MonitorInputError("submit-price-order requires --confirm-live")
    submitted_at_ms = now_ms if now_ms is not None else _now_ms()
    task = normalize_task(raw_task)
    if task["task_type"] != "symbol_price_monitor":
        raise MonitorInputError("submit-price-order requires symbol_price_monitor")
    if task.get("status") != "active":
        raise MonitorInputError("submit-price-order requires an active monitor task")

    price_order = build_price_tp_sl_order_body(task)
    preview = _run_json_command(
        _trader_script_command(
            "weex_trade_guard.py",
            "preview-tp-sl",
            "--profile",
            task["profile"],
            "--tp-sl-json",
            json.dumps(price_order, ensure_ascii=False, separators=(",", ":")),
            "--ttl-seconds",
            "300",
            "--pretty",
        )
    )
    intent_id = _required_delegate_field(preview, "intent_id")
    risk_signature = _required_delegate_field(preview, "risk_signature")
    exchange_response = _run_json_command(
        _trader_script_command(
            "weex_trade_guard.py",
            "confirm-tp-sl",
            "--intent-id",
            intent_id,
            "--risk-signature",
            risk_signature,
            "--confirm-live",
            "--pretty",
        )
    )

    updated_task = dict(task)
    updated_task["status"] = "submitted"
    updated_task["submitted_at_ms"] = submitted_at_ms
    updated_task["price_order"] = price_order
    updated_task["exchange_response"] = exchange_response
    output = {
        "task_id": task["task_id"],
        "status": "submitted",
        "price_order": price_order,
        "exchange_response": exchange_response,
        "thread_report": render_price_submission_report(task, price_order, exchange_response),
    }
    with _connect() as conn:
        _append_event(
            conn,
            task["task_id"],
            "live_price_order_previewed",
            {"preview": preview, "price_order": price_order},
            created_at_ms=submitted_at_ms,
        )
        _upsert_task(conn, updated_task, updated_at_ms=submitted_at_ms)
        _append_event(
            conn,
            task["task_id"],
            "live_price_order_submitted",
            output,
            created_at_ms=submitted_at_ms,
        )
    return output


def run_live_once(
    *,
    confirm_live: bool,
    task_id: str | None = None,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    if not confirm_live:
        raise MonitorInputError("run-live-once requires --confirm-live")
    evaluated_at_ms = now_ms if now_ms is not None else _now_ms()
    outputs: list[dict[str, Any]] = []

    for task in load_tasks():
        if task.get("status") != "active":
            continue
        if task_id is not None and task.get("task_id") != task_id:
            continue
        if task.get("task_type") != "position_pnl_monitor":
            continue

        first_payload = _collect_live_account_payload(task)
        first_blocker = _live_payload_blocker(first_payload)
        if first_blocker is not None:
            output = _live_not_executed_output(task, first_blocker)
            _append_live_event(task["task_id"], "live_evaluated", output, evaluated_at_ms)
            outputs.append(output)
            continue

        first_result = evaluate_pnl_task(task, _positions_from_account_payload(first_payload))
        if not first_result.get("triggered"):
            output = {
                "task_id": task["task_id"],
                "status": "active",
                "result": first_result,
                "thread_report": render_live_thread_report(task, first_result, None),
            }
            _append_live_event(task["task_id"], "live_evaluated", output, evaluated_at_ms)
            outputs.append(output)
            continue

        recheck_payload = _collect_live_account_payload(task)
        recheck_blocker = _live_payload_blocker(recheck_payload)
        if recheck_blocker is not None:
            output = _live_not_executed_output(task, f"revalidation_{recheck_blocker}")
            _append_live_event(task["task_id"], "live_revalidation_failed", output, evaluated_at_ms)
            outputs.append(output)
            continue

        recheck_result = evaluate_pnl_task(task, _positions_from_account_payload(recheck_payload))
        if not recheck_result.get("triggered"):
            output = {
                "task_id": task["task_id"],
                "status": "active",
                "result": recheck_result,
                "thread_report": "WEEX monitor live trigger revalidation did not match; no live close order was submitted.",
            }
            _append_live_event(task["task_id"], "live_revalidation_failed", output, evaluated_at_ms)
            outputs.append(output)
            continue

        close_order = dict(recheck_result["close_order"])
        close_order["new_client_order_id"] = _live_client_order_id(task["task_id"])
        preview = _run_json_command(
            _trader_script_command(
                "weex_trade_guard.py",
                "preview-order",
                "--profile",
                task["profile"],
                "--market",
                task["market"],
                "--order-json",
                json.dumps(close_order, ensure_ascii=False, separators=(",", ":")),
                "--ttl-seconds",
                "300",
                "--pretty",
            )
        )
        intent_id = _required_delegate_field(preview, "intent_id")
        risk_signature = _required_delegate_field(preview, "risk_signature")
        exchange_response = _run_json_command(
            _trader_script_command(
                "weex_trade_guard.py",
                "confirm-order",
                "--intent-id",
                intent_id,
                "--risk-signature",
                risk_signature,
                "--confirm-live",
                "--pretty",
            )
        )
        output = {
            "task_id": task["task_id"],
            "status": "completed",
            "result": recheck_result,
            "close_order": close_order,
            "exchange_response": exchange_response,
            "thread_report": render_live_thread_report(task, recheck_result, exchange_response),
        }
        updated_task = dict(task)
        updated_task["status"] = "completed"
        updated_task["triggered_at_ms"] = evaluated_at_ms
        updated_task["completed_at_ms"] = evaluated_at_ms
        updated_task["trigger_snapshot"] = recheck_result.get("trigger_snapshot", {})
        updated_task["close_order"] = close_order
        updated_task["exchange_response"] = exchange_response
        with _connect() as conn:
            _append_event(conn, task["task_id"], "live_evaluated", {"result": first_result}, created_at_ms=evaluated_at_ms)
            _append_event(conn, task["task_id"], "live_triggered", {"result": recheck_result}, created_at_ms=evaluated_at_ms)
            _append_event(conn, task["task_id"], "live_order_previewed", {"preview": preview, "close_order": close_order}, created_at_ms=evaluated_at_ms)
            _upsert_task(conn, updated_task, updated_at_ms=evaluated_at_ms)
            _append_event(conn, task["task_id"], "live_order_submitted", output, created_at_ms=evaluated_at_ms)
        outputs.append(output)
    return outputs


def _required_delegate_field(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        raise MonitorInputError(f"delegated preview response missing {key}")
    return str(value).strip()


def _collect_live_account_payload(task: dict[str, Any]) -> dict[str, Any]:
    payload = _run_json_command(
        _trader_script_command(
            "weex_trade_data_aggregator.py",
            "collect-account-risk",
            "--profile",
            str(task["profile"]),
            "--market",
            str(task["market"]),
            "--symbol",
            str(task["symbol"]),
            "--pretty",
        )
    )
    if not isinstance(payload, dict):
        raise MonitorInputError("live account payload must be a JSON object")
    return payload


def _positions_from_account_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    positions = payload.get("positions")
    if not isinstance(positions, list):
        raise MonitorInputError("live account payload positions must be a JSON array")
    return [item for item in positions if isinstance(item, dict)]


def _live_payload_blocker(payload: dict[str, Any]) -> str | None:
    if payload.get("partial"):
        return "live_data_partial"
    degraded_reasons = payload.get("degraded_reasons")
    if isinstance(degraded_reasons, list) and degraded_reasons:
        return "live_data_degraded"
    return None


def _live_not_executed_output(task: dict[str, Any], reason: str) -> dict[str, Any]:
    result = {
        "triggered": False,
        "reason": reason,
        "execution_delegate": "weex-trader-skill",
    }
    return {
        "task_id": task["task_id"],
        "status": "active",
        "result": result,
        "thread_report": render_live_thread_report(task, result, None),
    }


def _append_live_event(task_id: str, event_type: str, output: dict[str, Any], created_at_ms: int) -> None:
    with _connect() as conn:
        _append_event(conn, task_id, event_type, output, created_at_ms=created_at_ms)


def _live_client_order_id(task_id: str) -> str:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in f"monitor_{task_id}")
    return normalized[:36]


def render_price_submission_report(
    task: dict[str, Any],
    price_order: dict[str, str],
    exchange_response: dict[str, Any],
) -> str:
    return (
        f"WEEX monitor {task['task_id']} price TP/SL submitted: "
        f"{price_order['symbol']} {price_order['positionSide']} {price_order['planType']} "
        f"at {price_order['triggerPrice']} quantity {price_order['quantity']}. "
        f"Exchange response: {exchange_response}."
    )


def render_live_thread_report(
    task: dict[str, Any],
    result: dict[str, Any],
    exchange_response: dict[str, Any] | None,
) -> str:
    if result.get("triggered") and exchange_response is not None:
        snapshot = result.get("trigger_snapshot", {})
        return (
            f"WEEX monitor {task['task_id']} Live close order submitted: "
            f"{snapshot.get('symbol')} {snapshot.get('position_side')} "
            f"{snapshot.get('unrealized_pnl')} {snapshot.get('operator')} {snapshot.get('threshold')}. "
            f"Exchange response: {exchange_response}."
        )
    return (
        f"WEEX monitor {task['task_id']} live check did not submit a close order: "
        f"{result.get('reason', 'unknown_reason')}."
    )


def render_thread_report(output: dict[str, Any]) -> str:
    task_id = str(output.get("task_id", "unknown"))
    result = output.get("result", {})
    if not isinstance(result, dict):
        raise MonitorInputError("result output must be a JSON object")
    if result.get("triggered"):
        snapshot = result.get("trigger_snapshot", {})
        close_order = result.get("close_order", {})
        return (
            f"WEEX monitor {task_id} dry-run triggered: "
            f"{snapshot.get('symbol')} {snapshot.get('position_side')} "
            f"{snapshot.get('unrealized_pnl')} {snapshot.get('operator')} {snapshot.get('threshold')}. "
            f"Planned close order: {close_order}. "
            "Live execution delegate is weex-trader-skill and requires --confirm-live. "
            "No live order was submitted by weex-monitor-skill."
        )
    return (
        f"WEEX monitor {task_id} dry-run not triggered: "
        f"{result.get('reason', 'unknown_reason')}. "
        "No live order was submitted by weex-monitor-skill."
    )


def _connect() -> sqlite3.Connection:
    home = monitor_home()
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            task_type TEXT NOT NULL,
            profile TEXT NOT NULL,
            symbol TEXT NOT NULL,
            position_side TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            task_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monitor_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_monitor_tasks_status ON monitor_tasks(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_monitor_events_task_id ON monitor_events(task_id)")


def _upsert_task(conn: sqlite3.Connection, task: dict[str, Any], *, updated_at_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO monitor_tasks (
            task_id, status, task_type, profile, symbol, position_side, created_at_ms, updated_at_ms, task_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            status = excluded.status,
            task_type = excluded.task_type,
            profile = excluded.profile,
            symbol = excluded.symbol,
            position_side = excluded.position_side,
            updated_at_ms = excluded.updated_at_ms,
            task_json = excluded.task_json
        """,
        (
            task["task_id"],
            task["status"],
            task["task_type"],
            task["profile"],
            task["symbol"],
            task["position_side"],
            int(task.get("created_at_ms", updated_at_ms)),
            updated_at_ms,
            json.dumps(task, ensure_ascii=False, sort_keys=True),
        ),
    )


def _append_event(
    conn: sqlite3.Connection,
    task_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    created_at_ms: int,
) -> None:
    conn.execute(
        """
        INSERT INTO monitor_events (task_id, event_type, created_at_ms, payload_json)
        VALUES (?, ?, ?, ?)
        """,
        (
            task_id,
            event_type,
            created_at_ms,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        raise MonitorInputError(f"{key} is required")
    return str(value).strip()


def _normalize_position_side(value: Any) -> str:
    if value is None:
        raise MonitorInputError("position_side is required")
    side = str(value).strip().upper()
    if side not in VALID_POSITION_SIDES:
        raise MonitorInputError("position_side must be LONG or SHORT")
    return side


def _normalize_market(value: Any) -> str:
    market = str(value or "futures").strip().lower()
    if market not in VALID_MARKETS:
        raise MonitorInputError("market must be futures")
    return market


def _normalize_condition(value: Any, task_type: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise MonitorInputError("condition must be a JSON object")

    metric = _required_string(value, "metric")
    expected_metric = "unrealized_pnl" if task_type == "position_pnl_monitor" else "last_price"
    if metric != expected_metric:
        raise MonitorInputError(f"{task_type} condition metric must be {expected_metric}")

    operator = _required_string(value, "operator")
    if operator not in VALID_OPERATORS:
        raise MonitorInputError("condition.operator must be one of >, >=, <, <=")

    threshold_value = _decimal_from_any(value.get("threshold"), "condition.threshold")
    if not threshold_value.is_finite():
        raise MonitorInputError("condition.threshold must be finite")
    if task_type == "symbol_price_monitor" and threshold_value <= 0:
        raise MonitorInputError("condition.threshold must be > 0 for symbol_price_monitor")
    threshold = str(value.get("threshold")).strip()
    return {
        "metric": metric,
        "operator": operator,
        "threshold": threshold,
    }


def _normalize_action(value: Any, position_side: str, task_type: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise MonitorInputError("action must be a JSON object")
    action_type = _required_string(value, "type")
    if action_type != "market_close":
        raise MonitorInputError("only market_close action is supported")
    target = _normalize_position_side(value.get("target"))
    if target != position_side:
        raise MonitorInputError("action.target must match position_side")

    action = {
        "type": action_type,
        "target": target,
    }
    quantity = value.get("quantity")
    if quantity is not None and str(quantity).strip() != "":
        action["quantity"] = _positive_decimal_text(quantity, "action.quantity")
    elif task_type == "symbol_price_monitor":
        raise MonitorInputError("symbol_price_monitor action.quantity is required")
    return action


def _normalize_callback(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise MonitorInputError("callback must be a JSON object")
    callback_type = _required_string(value, "type")
    if callback_type not in VALID_CALLBACK_TYPES:
        raise MonitorInputError("only current_thread callback is supported")
    return {"type": callback_type}


def _normalize_frequency(value: Any) -> int:
    if value is None:
        return DEFAULT_FREQUENCY_SECONDS
    try:
        frequency = int(value)
    except (TypeError, ValueError) as exc:
        raise MonitorInputError("frequency_seconds must be an integer") from exc
    if frequency < MIN_FREQUENCY_SECONDS:
        raise MonitorInputError(f"frequency_seconds must be >= {MIN_FREQUENCY_SECONDS}")
    return frequency


def _normalize_trigger_price_type(value: Any) -> str:
    if value is None:
        return "CONTRACT_PRICE"
    trigger_price_type = str(value).strip().upper()
    if trigger_price_type not in VALID_TRIGGER_PRICE_TYPES:
        raise MonitorInputError("trigger_price_type must be CONTRACT_PRICE or MARK_PRICE")
    return trigger_price_type


def _positive_decimal_text(value: Any, field_name: str) -> str:
    decimal_value = _decimal_from_any(value, field_name)
    if not decimal_value.is_finite():
        raise MonitorInputError(f"{field_name} must be finite")
    if decimal_value <= 0:
        raise MonitorInputError(f"{field_name} must be > 0")
    return str(value).strip()


def _decimal_from_any(value: Any, field_name: str) -> Decimal:
    if value is None or str(value).strip() == "":
        raise MonitorInputError(f"{field_name} is required")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MonitorInputError(f"{field_name} must be numeric") from exc


def _new_task_id() -> str:
    return f"mon_{uuid.uuid4().hex[:24]}"


def _client_algo_id(task_id: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in task_id)
    return normalized[:36]


def _price_plan_type(position_side: str, operator: str) -> str:
    if position_side == "LONG":
        return "TAKE_PROFIT" if operator in {">", ">="} else "STOP_LOSS"
    return "TAKE_PROFIT" if operator in {"<", "<="} else "STOP_LOSS"


def _compare(left: Decimal, operator: str, right: Decimal) -> bool:
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    raise MonitorInputError(f"unsupported operator: {operator}")


def _find_position(task: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any] | None:
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = str(_first_present(position, ("symbol", "contract", "instId")) or "").upper()
        side = str(_first_present(position, ("side", "positionSide", "position_side", "holdSide")) or "").upper()
        if symbol != task["symbol"] or side != task["position_side"]:
            continue
        try:
            if _decimal_from_any(_position_size(position), "position.size") <= 0:
                continue
        except MonitorInputError:
            continue
        return position
    return None


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _position_size(position: dict[str, Any]) -> str:
    value = _first_present(position, ("size", "positionAmt", "quantity", "qty", "available"))
    _decimal_from_any(value, "position.size")
    return str(value)


def _close_order_side(position_side: str) -> str:
    return "SELL" if position_side == "LONG" else "BUY"


def _read_json_arg(value: str | None, file_path: str | None, *, name: str) -> Any:
    if value and file_path:
        raise MonitorInputError(f"use either --{name}-json or --{name}-file, not both")
    if file_path:
        return json.loads(Path(file_path).read_text(encoding="utf-8"))
    if value:
        return json.loads(value)
    raise MonitorInputError(f"--{name}-json or --{name}-file is required")


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WEEX monitor task helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("preview", "confirm", "build-price-order", "confirm-text"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--task-json")
        sub.add_argument("--task-file")
        if name == "confirm":
            sub.add_argument("--confirm-monitor", action="store_true")

    eval_pnl = subparsers.add_parser("evaluate-pnl")
    eval_pnl.add_argument("--task-json")
    eval_pnl.add_argument("--task-file")
    eval_pnl.add_argument("--positions-json")
    eval_pnl.add_argument("--positions-file")

    subparsers.add_parser("list")

    events = subparsers.add_parser("events")
    events.add_argument("--task-id")

    run_once = subparsers.add_parser("run-once")
    run_once.add_argument("--dry-run", action="store_true")
    run_once.add_argument("--task-id")
    run_once.add_argument("--positions-json")
    run_once.add_argument("--positions-file")

    run_live_once_parser = subparsers.add_parser("run-live-once")
    run_live_once_parser.add_argument("--confirm-live", action="store_true")
    run_live_once_parser.add_argument("--task-id")

    run_loop = subparsers.add_parser("run-loop")
    run_loop.add_argument("--dry-run", action="store_true")
    run_loop.add_argument("--task-id")
    run_loop.add_argument("--iterations", type=int, default=1)
    run_loop.add_argument("--sleep-seconds", type=float, default=0)
    run_loop.add_argument("--positions-sequence-json")
    run_loop.add_argument("--positions-sequence-file")

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("--task-id", required=True)

    submit_price = subparsers.add_parser("submit-price-order")
    submit_price.add_argument("--task-json")
    submit_price.add_argument("--task-file")
    submit_price.add_argument("--confirm-live", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "preview":
            task = _read_json_arg(args.task_json, args.task_file, name="task")
            _print_json(normalize_task(task))
        elif args.command == "confirm":
            task = _read_json_arg(args.task_json, args.task_file, name="task")
            _print_json(confirm_task(task, confirm_monitor=args.confirm_monitor))
        elif args.command == "build-price-order":
            task = _read_json_arg(args.task_json, args.task_file, name="task")
            _print_json(build_price_tp_sl_order_body(task))
        elif args.command == "confirm-text":
            task = _read_json_arg(args.task_json, args.task_file, name="task")
            _print_json({"confirmation_text": render_confirmation_text(task)})
        elif args.command == "evaluate-pnl":
            task = _read_json_arg(args.task_json, args.task_file, name="task")
            positions = _read_json_arg(args.positions_json, args.positions_file, name="positions")
            _print_json(evaluate_pnl_task(task, positions))
        elif args.command == "list":
            _print_json(load_tasks())
        elif args.command == "events":
            _print_json(load_events(args.task_id))
        elif args.command == "run-once":
            if not args.dry_run:
                raise MonitorInputError("run-once currently requires --dry-run")
            positions = _read_json_arg(args.positions_json, args.positions_file, name="positions")
            _print_json(run_once_dry_run(positions, task_id=args.task_id))
        elif args.command == "run-live-once":
            _print_json(run_live_once(confirm_live=args.confirm_live, task_id=args.task_id))
        elif args.command == "run-loop":
            if not args.dry_run:
                raise MonitorInputError("run-loop currently requires --dry-run")
            positions_sequence = _read_json_arg(
                args.positions_sequence_json,
                args.positions_sequence_file,
                name="positions-sequence",
            )
            _print_json(
                run_loop_dry_run(
                    positions_sequence,
                    iterations=args.iterations,
                    task_id=args.task_id,
                    sleep_seconds=args.sleep_seconds,
                )
            )
        elif args.command == "cancel":
            _print_json(cancel_task(args.task_id))
        elif args.command == "submit-price-order":
            task = _read_json_arg(args.task_json, args.task_file, name="task")
            _print_json(submit_price_order(task, confirm_live=args.confirm_live))
        else:
            parser.error(f"unsupported command: {args.command}")
    except (MonitorInputError, json.JSONDecodeError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
