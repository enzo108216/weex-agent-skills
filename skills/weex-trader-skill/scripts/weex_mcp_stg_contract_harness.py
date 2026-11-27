#!/usr/bin/env python3
"""Exercise the ordered T17 gate with offline fixtures only."""

from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable


STG_SPOT_ORIGIN = "https://stg-spotpro-openapi.weex.tech"
STG_FUTURES_ORIGIN = "https://stg-api-contract.weex.tech"
DIRECT_ACTIONS = (
    "trade_place_order", "trade_cancel_order", "trade_update_leverage",
    "trade_set_margin_mode", "trade_place_batch_orders", "trade_cancel_batch_orders",
    "trade_cancel_open_orders", "trade_place_conditional_order",
    "trade_cancel_conditional_order", "trade_place_tp_sl", "trade_modify_tp_sl",
    "trade_close_positions", "trade_adjust_position_margin", "trade_set_auto_append_margin",
)

_PHASES = ("seed", "test", "cleanup")
_BATCH = {"trade_place_batch_orders", "trade_cancel_batch_orders"}
_SCOPED = {"trade_cancel_open_orders", "trade_close_positions"}
_PLACEMENTS = {"trade_place_order", "trade_place_conditional_order", "trade_place_tp_sl", "trade_place_batch_orders"}
_CONFIG = {"trade_update_leverage", "trade_set_margin_mode"}
_SPOT_ACTIONS = {
    "trade_place_order", "trade_cancel_order", "trade_place_batch_orders",
    "trade_cancel_batch_orders", "trade_cancel_open_orders",
}
_CLEANUP_PARENTS = {
    "trade_cancel_order": {"trade_place_order", "trade_place_batch_orders"},
    "trade_cancel_batch_orders": {"trade_place_batch_orders"},
    "trade_cancel_conditional_order": {"trade_place_conditional_order", "trade_place_tp_sl"},
}
_PLACED_STATUSES = ("OPEN", "NEW", "UNTRIGGERED", "UNACTIVATED", "FILLED")
_CANCELED_STATUSES = ("CANCELED", "CANCELLED")
_LEVERAGE_FIELDS = {"leverage", "cross_leverage", "isolated_long_leverage", "isolated_short_leverage"}
_ORDER_IDENTITIES = ({"order_id"}, {"client_order_id"})
_CASE_FIELDS = {
    "case_id", "profile", "owner", "phase", "tool_name", "market", "expected_identity", "expected_state",
}
_IDENTITY_KEYS = {
    "trade_place_order": ({"client_order_id"},),
    "trade_place_batch_orders": ({"client_order_id"},),
    "trade_cancel_order": _ORDER_IDENTITIES,
    "trade_cancel_batch_orders": _ORDER_IDENTITIES,
    "trade_update_leverage": ({"symbol"},),
    "trade_set_margin_mode": ({"symbol"},),
    "trade_cancel_open_orders": ({"scope", "symbol", "order_kind"},),
    "trade_close_positions": ({"scope", "position_id"},),
    "trade_place_conditional_order": ({"client_algo_id"},),
    "trade_place_tp_sl": ({"client_algo_id"},),
    "trade_cancel_conditional_order": ({"order_id"},),
    "trade_modify_tp_sl": ({"order_id"},),
    "trade_adjust_position_margin": ({"position_id"},),
    "trade_set_auto_append_margin": ({"position_id"},),
}
_IDENTITY_FIELDS = {"client_order_id", "client_algo_id", "order_id", "position_id", "symbol", "scope", "order_kind"}
_OUTCOME_FIELDS = {"class", "write_attempted", "grant_disposition"}
_STOP_OUTCOMES = {"unknown", "partial", "definitive_rejection", "not_sent", "invocation_rejected"}


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value.strip() == value


def _order_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) is not None


def _identity(value: Any) -> bool:
    return (
        isinstance(value, dict) and bool(value)
        and set(value) <= _IDENTITY_FIELDS
        and all((_order_id(item) if key in {"order_id", "position_id"} else _nonempty(item))
                for key, item in value.items())
    )


def _unique_identities(entries: list[dict[str, str]]) -> bool:
    return len({frozenset(entry.items()) for entry in entries}) == len(entries)


def _identities(value: dict[str, str] | list[dict[str, str]]) -> list[dict[str, str]]:
    return value if isinstance(value, list) else [value]


def _valid_case_identity(tool: str, expected: Any) -> bool:
    if isinstance(expected, list) != (tool in _BATCH):
        return False
    entries = _identities(expected)
    if not entries or not all(
        _identity(entry) and set(entry) in _IDENTITY_KEYS[tool] for entry in entries
    ) or not _unique_identities(entries):
        return False
    if tool in _BATCH:
        return all(set(entry) == set(entries[0]) for entry in entries)
    if tool == "trade_cancel_open_orders":
        return expected["scope"] == "symbol" and expected["order_kind"] in {"normal", "conditional"}
    if tool == "trade_close_positions":
        return expected["scope"] == "position"
    return True


def _numeric_text(value: Any, *, positive: bool) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) is not None
        and (not positive or bool(value.strip("0.")))
    )


def _valid_state(case: dict[str, Any], state: Any) -> bool:
    if not isinstance(state, dict) or not state:
        return False
    tool = case["tool_name"]
    if tool in _BATCH:
        statuses = state.get("statuses")
        allowed = _PLACED_STATUSES if tool in _PLACEMENTS else _CANCELED_STATUSES
        return (
            set(state) == {"statuses"} and isinstance(statuses, list)
            and len(statuses) == len(case["expected_identity"])
            and all(status in allowed for status in statuses)
        )
    if tool in _PLACEMENTS or tool in _CLEANUP_PARENTS:
        allowed = _PLACED_STATUSES if tool in _PLACEMENTS else _CANCELED_STATUSES
        return set(state) == {"status"} and state["status"] in allowed
    if tool == "trade_update_leverage":
        return (
            bool(_LEVERAGE_FIELDS.intersection(state))
            and set(state) <= _LEVERAGE_FIELDS | {"margin_mode", "position_side"}
            and all(_order_id(value) for key, value in state.items() if key in _LEVERAGE_FIELDS)
            and state.get("margin_mode", "CROSS") in ("CROSS", "ISOLATED")
            and state.get("position_side", "LONG") in ("LONG", "SHORT")
        )
    if tool == "trade_set_margin_mode":
        return (
            set(state) <= {"margin_mode", "separated_type"}
            and state.get("margin_mode") in ("CROSS", "ISOLATED")
            and state.get("separated_type", "COMBINED") in ("COMBINED", "SEPARATED")
        )
    if tool == "trade_modify_tp_sl":
        return set(state) == {"trigger_price"} and _numeric_text(state["trigger_price"], positive=True)
    if tool == "trade_adjust_position_margin":
        return set(state) == {"margin"} and _numeric_text(state["margin"], positive=False)
    if tool == "trade_set_auto_append_margin":
        return set(state) == {"enabled"} and type(state["enabled"]) is bool
    if tool == "trade_cancel_open_orders":
        return set(state) == {"open_count"} and type(state["open_count"]) is int and state["open_count"] == 0
    if tool == "trade_close_positions":
        return set(state) == {"size"} and state["size"] == "0"
    return False


def _preflight(
    cases: Any, budgets: Any, *, profile: str, owner: str,
) -> bool:
    if (
        not isinstance(cases, list) or not cases
        or not isinstance(budgets, dict) or set(budgets) != set(_PHASES)
        or any(type(count) is not int or count < 0 for count in budgets.values())
    ):
        return False
    seen: dict[str, dict[str, Any]] = {}
    targets: set[tuple[str, frozenset[tuple[str, str]]]] = set()
    counts = dict.fromkeys(_PHASES, 0)
    for case in cases:
        if not isinstance(case, dict) or not _CASE_FIELDS <= case.keys() or set(case) - (
            _CASE_FIELDS | {"allow_scoped_noop", "cleanup_of", "expected_items"}
        ):
            return False
        case_id, tool, phase, expected = (
            case[key] for key in ("case_id", "tool_name", "phase", "expected_identity")
        )
        if (
            not _nonempty(case_id) or case_id in seen or case["owner"] != owner
            or case["profile"] != profile or tool not in DIRECT_ACTIONS or phase not in _PHASES
            or case["market"] not in ("spot", "futures")
            or (case["market"] == "spot" and tool not in _SPOT_ACTIONS)
            or not _valid_case_identity(tool, expected)
            or not _valid_state(case, case["expected_state"])
            or type(case.get("allow_scoped_noop", False)) is not bool
            or (case.get("allow_scoped_noop") and tool != "trade_cancel_open_orders")
        ):
            return False
        if "expected_items" in case:
            items = case["expected_items"]
            exact_fields = {"position_id"} if tool == "trade_close_positions" else {"order_id", "client_order_id"}
            if (
                tool not in _SCOPED or not isinstance(items, list) or not items
                or not all(
                    _identity(item) and bool(exact_fields.intersection(item)) and item.get("scope") == expected["scope"]
                    for item in items
                )
                or not _unique_identities(items)
            ):
                return False
        if phase == "cleanup":
            link = case.get("cleanup_of")
            original = seen.get(link) if isinstance(link, str) else None
            if (
                original is None or original["phase"] == "cleanup"
                or original["tool_name"] not in _CLEANUP_PARENTS.get(tool, ())
                or original["market"] != case["market"]
            ):
                return False
            original_ids = _identities(original["expected_identity"])
            for identity in _identities(expected):
                target = (link, frozenset(identity.items()))
                if ("client_order_id" in identity and identity not in original_ids) or target in targets:
                    return False
                targets.add(target)
        elif "cleanup_of" in case:
            return False
        counts[phase] += 1
        if counts[phase] > budgets[phase]:
            return False
        seen[case_id] = case
    return True


def _baseline_digest(baseline: Callable[[], Any]) -> str | None:
    try:
        proof = baseline()
    except Exception:
        return None
    if not isinstance(proof, dict) or proof.get("complete") is not True or proof.get("clean") is not True:
        return None
    digest = proof.get("snapshot_digest")
    return digest if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) else None


def _accepted_items(result: dict[str, Any], identities: list[dict[str, str]]) -> list[dict[str, Any]] | None:
    items = result.get("items")
    if not isinstance(items, list) or len(items) != len(identities):
        return None
    for index, (item, identity) in enumerate(zip(items, identities, strict=True)):
        if (
            not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] != index
            or item.get("request_identity") != identity or item.get("status") != "accepted"
            or "error" in item or "reconciliation" in item
        ):
            return None
        outcome = item.get("outcome")
        if not isinstance(outcome, dict) or set(outcome) != {"class", "write_attempted"} or (
            outcome["class"] != "accepted" or outcome["write_attempted"] is not True
        ):
            return None
    return items


def _receipt(case: dict[str, Any], wrapped: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(wrapped, dict) or type(wrapped.get("is_error")) is not bool:
        return "invalid_envelope", {}
    payload = wrapped.get("structured_content")
    tool = case["tool_name"]
    if not isinstance(payload, dict) or payload.get("action") != tool.removeprefix("trade_"):
        return "invalid_envelope", {}
    outcome = payload.get("outcome")
    if not isinstance(outcome, dict) or set(outcome) != _OUTCOME_FIELDS:
        return "invalid_envelope", {}
    outcome_class = outcome["class"]
    if isinstance(outcome_class, str) and outcome_class in _STOP_OUTCOMES:
        return outcome_class, {}
    if (
        wrapped["is_error"] or payload.get("ok") is not True
        or outcome_class != "accepted" or outcome["write_attempted"] is not True
        or outcome["grant_disposition"] != "accepted"
        or set(payload) != {"ok", "action", "outcome", "result"}
    ):
        return "invalid_envelope", {}
    result = payload["result"]
    if not isinstance(result, dict):
        return "invalid_receipt", {}
    if (
        ("market" in result or tool in {"trade_place_order", "trade_cancel_order"})
        and result.get("market") != case["market"]
    ):
        return "unproved_market", {}
    expected = case["expected_identity"]
    expected_state = case["expected_state"]
    status = result.get("status")
    if status == "accepted_noop":
        if (
            tool != "trade_cancel_open_orders" or case.get("allow_scoped_noop") is not True
            or "expected_items" in case or result.get("request_identity") != expected
            or result.get("noop") is not True or type(result.get("affected_count")) is not int
            or result["affected_count"] != 0 or result.get("items", []) != []
        ):
            return "unproved_noop", {}
        return "accepted_noop", result
    if status != "accepted" or result.get("noop") is True:
        return "invalid_receipt", {}
    if tool in _CONFIG:
        if result.get("symbol") != expected["symbol"] or result.get("target") != expected_state:
            return "unproved_target", {}
    elif tool not in _BATCH and result.get("request_identity") != expected:
        return "unproved_identity", {}
    if tool == "trade_modify_tp_sl" and result.get("trigger_price") != expected_state["trigger_price"]:
        return "unproved_target", {}
    if tool == "trade_set_auto_append_margin" and result.get("enabled") is not expected_state["enabled"]:
        return "unproved_target", {}
    if tool in {"trade_cancel_order", "trade_cancel_conditional_order"} and (
        "final_order_status" in result and result["final_order_status"] not in _CANCELED_STATUSES
    ):
        return "unproved_target", {}

    if tool in _BATCH or tool == "trade_place_tp_sl":
        items = _accepted_items(result, _identities(expected))
        if items is None or result.get("counts") != {"accepted": len(items), "rejected": 0, "unknown": 0}:
            return "unproved_items", {}
        if tool == "trade_place_batch_orders":
            order_ids = [item.get("upstream_order_id") for item in items]
            if not all(_order_id(value) for value in order_ids) or len(set(order_ids)) != len(order_ids):
                return "unproved_order_id", {}
        if tool == "trade_place_tp_sl" and items[0].get("upstream_order_id") != result.get("upstream_order_id"):
            return "unproved_order_id", {}
    elif tool in _SCOPED:
        candidates = case.get("expected_items")
        if not candidates or _accepted_items(result, candidates) is None or (
            type(result.get("affected_count")) is not int or result["affected_count"] != len(candidates)
        ):
            return "unproved_items", {}
    if tool in _PLACEMENTS - _BATCH and not _order_id(result.get("upstream_order_id")):
        return "unproved_order_id", {}
    return "accepted", result


def _read_back_proved(
    case: dict[str, Any], result: dict[str, Any], receipt: str, proof: Any,
    *, cleanup_order_ids: list[str] | None = None,
) -> bool:
    if (
        not isinstance(proof, dict) or proof.get("complete") is not True
        or proof.get("matches") is not True or proof.get("identity") != case["expected_identity"]
        or proof.get("market") != case["market"]
        or not _valid_state(case, proof.get("state")) or proof["state"] != case["expected_state"]
    ):
        return False
    if receipt == "accepted_noop":
        return proof.get("scope_empty") is True
    tool = case["tool_name"]
    if cleanup_order_ids is not None:
        # Numeric identities already appeared in the independent read-back above.
        # A client alias alone cannot establish the exchange order being cleaned up.
        if tool == "trade_cancel_batch_orders":
            observed = proof.get(
                "upstream_order_ids",
                [item.get("order_id") for item in _identities(proof["identity"])],
            )
            return observed == cleanup_order_ids
        return proof.get("upstream_order_id", proof["identity"].get("order_id")) == cleanup_order_ids[0]
    if tool == "trade_place_batch_orders":
        return proof.get("upstream_order_ids") == [item["upstream_order_id"] for item in result["items"]]
    if tool in _PLACEMENTS:
        return proof.get("upstream_order_id") == result["upstream_order_id"]
    if tool in _SCOPED:
        return proof.get("affected_identities") == case["expected_items"]
    return True


def _proved_order_ids(case: dict[str, Any], result: dict[str, Any]) -> dict[str, dict[str, str]]:
    # The caller records these only after the independent read-back matched the receipt.
    if case["tool_name"] == "trade_place_batch_orders":
        return {item["upstream_order_id"]: item["request_identity"] for item in result["items"]}
    return {result["upstream_order_id"]: case["expected_identity"]}


def _cleanup_order_ids(
    case: dict[str, Any], proved: dict[str, dict[str, str]],
) -> list[str] | None:
    order_ids = []
    for identity in _identities(case["expected_identity"]):
        if "order_id" in identity:
            matches = [identity["order_id"]] if identity["order_id"] in proved else []
        else:
            matches = [order_id for order_id, bound in proved.items() if bound == identity]
        if len(matches) != 1 or matches[0] in order_ids:
            return None
        order_ids.append(matches[0])
    return order_ids


def _cleanup_receipt_matches(
    case: dict[str, Any], result: dict[str, Any], resolved_ids: list[str],
) -> bool:
    if case["tool_name"] == "trade_cancel_batch_orders":
        receipts = result["items"]
    else:
        receipts = [result]
    return all(
        "upstream_order_id" not in receipt or receipt["upstream_order_id"] == expected
        for receipt, expected in zip(receipts, resolved_ids, strict=True)
    )


def run_offline_gate(
    *,
    profile: str,
    owner: str,
    spot_base_url: str,
    futures_base_url: str,
    cases: list[dict[str, Any]],
    budgets: dict[str, int],
    dispatch: Callable[[dict[str, Any]], Any],
    read_back: Callable[[dict[str, Any], dict[str, Any]], Any],
    baseline: Callable[[], Any],
    reconcile: Callable[[dict[str, Any], Any], Any] | None = None,
) -> dict[str, Any]:
    """Run only locally supplied fixture callbacks; report proof, never payloads."""
    report: dict[str, Any] = {
        "mode": "offline_fake",
        "ok": False,
        "status": "blocked",
        "stop_reason": "preflight_failed",
        "dispatch_counts": dict.fromkeys(_PHASES, 0),
        "events": [],
        "baseline_proven": False,
        "manifest_complete": False,
        "reconcile_attempted": False,
    }
    if (
        owner != "normal" or not _nonempty(profile)
        or (spot_base_url, futures_base_url) != (STG_SPOT_ORIGIN, STG_FUTURES_ORIGIN)
        or not _preflight(cases, budgets, profile=profile, owner=owner)
    ):
        return report
    plan = deepcopy(cases)
    initial = _baseline_digest(baseline)
    if initial is None:
        report["stop_reason"] = "initial_baseline_unproved"
        return report
    report["status"] = "review_required"
    proved_placements: dict[str, dict[str, dict[str, str]]] = {}
    cleanup_targets: set[tuple[str, bool, str]] = set()
    covered = set()
    for index, case in enumerate(plan):
        phase, tool = case["phase"], case["tool_name"]
        resolved_ids = None
        if phase == "cleanup":
            resolved_ids = _cleanup_order_ids(case, proved_placements.get(case["cleanup_of"], {}))
            targets = {
                (case["market"], tool == "trade_cancel_conditional_order", order_id)
                for order_id in resolved_ids or ()
            }
            if not resolved_ids or targets.intersection(cleanup_targets):
                report["stop_reason"] = "cleanup_ownership_unproved"
                return report
            cleanup_targets.update(targets)
        report["dispatch_counts"][phase] += 1
        wrapped = None
        try:
            wrapped = dispatch(deepcopy(case))
            receipt, result = _receipt(case, wrapped)
        except Exception:
            receipt, result = "dispatch_failed", {}
        status = receipt
        if receipt in {"accepted", "accepted_noop"}:
            try:
                receipt_matches = resolved_ids is None or _cleanup_receipt_matches(case, result, resolved_ids)
                proved = receipt_matches and _read_back_proved(
                    case, result, receipt, read_back(deepcopy(case), deepcopy(result)),
                    cleanup_order_ids=resolved_ids,
                )
            except Exception:
                proved = False
            if not proved:
                status = "read_back_unproved"
        report["events"].append({"index": index, "phase": phase, "tool_name": tool, "status": status})
        if status not in {"accepted", "accepted_noop"}:
            report["stop_reason"] = status
            if reconcile is not None and status not in {"definitive_rejection", "not_sent", "invocation_rejected"}:
                report["reconcile_attempted"] = True
                try:
                    reconcile(deepcopy(case), wrapped)
                except Exception:
                    pass
            return report
        if tool in _PLACEMENTS:
            proved_placements[case["case_id"]] = _proved_order_ids(case, result)
        if phase == "test":
            covered.add(tool)
    report["manifest_complete"] = covered == set(DIRECT_ACTIONS)
    if _baseline_digest(baseline) != initial:
        report["stop_reason"] = "final_baseline_unproved"
        return report
    report.update(ok=True, status="ready", stop_reason=None, baseline_proven=True)
    return report


def _fake_plan() -> list[dict[str, Any]]:
    identities = {
        "trade_place_order": {"client_order_id": "fixture-single"},
        "trade_cancel_order": {"order_id": "7"},
        "trade_update_leverage": {"symbol": "FIXTURE"},
        "trade_set_margin_mode": {"symbol": "FIXTURE"},
        "trade_place_batch_orders": [{"client_order_id": "fixture-batch-1"}, {"client_order_id": "fixture-batch-2"}],
        "trade_cancel_batch_orders": [{"order_id": "8"}, {"order_id": "9"}],
        "trade_cancel_open_orders": {"scope": "symbol", "symbol": "FIXTURE", "order_kind": "normal"},
        "trade_place_conditional_order": {"client_algo_id": "fixture-conditional"},
        "trade_cancel_conditional_order": {"order_id": "10"},
        "trade_place_tp_sl": {"client_algo_id": "fixture-tp-sl"},
        "trade_modify_tp_sl": {"order_id": "11"},
        "trade_close_positions": {"scope": "position", "position_id": "12"},
        "trade_adjust_position_margin": {"position_id": "13"},
        "trade_set_auto_append_margin": {"position_id": "14"},
    }
    states = {
        "trade_place_order": {"status": "OPEN"},
        "trade_cancel_order": {"status": "CANCELED"},
        "trade_update_leverage": {"leverage": "2"},
        "trade_set_margin_mode": {"margin_mode": "ISOLATED"},
        "trade_place_batch_orders": {"statuses": ["OPEN", "OPEN"]},
        "trade_cancel_batch_orders": {"statuses": ["CANCELED", "CANCELED"]},
        "trade_cancel_open_orders": {"open_count": 0},
        "trade_place_conditional_order": {"status": "UNTRIGGERED"},
        "trade_cancel_conditional_order": {"status": "CANCELED"},
        "trade_place_tp_sl": {"status": "UNTRIGGERED"},
        "trade_modify_tp_sl": {"trigger_price": "1"},
        "trade_close_positions": {"size": "0"},
        "trade_adjust_position_margin": {"margin": "1"},
        "trade_set_auto_append_margin": {"enabled": True},
    }
    cases = [
        {
            "case_id": str(index), "owner": "normal", "profile": "offline-fixture", "phase": "test",
            "tool_name": tool, "market": "futures",
            "expected_identity": identities[tool], "expected_state": states[tool],
            "allow_scoped_noop": tool == "trade_cancel_open_orders",
        }
        for index, tool in enumerate(DIRECT_ACTIONS)
    ]
    for case in cases:
        if case["tool_name"] == "trade_close_positions":
            case["expected_items"] = [case["expected_identity"]]
    cases.append({
        **cases[0], "case_id": "cleanup", "phase": "cleanup",
        "tool_name": "trade_cancel_order", "expected_identity": {"order_id": "41"},
        "expected_state": states["trade_cancel_order"],
        "cleanup_of": cases[0]["case_id"],
    })
    return cases


def _fake_dispatch(case: dict[str, Any]) -> dict[str, Any]:
    tool, identity = case["tool_name"], case["expected_identity"]
    result: dict[str, Any] = {"status": "accepted"}
    if tool in _CONFIG:
        result.update(symbol=identity["symbol"], target=case["expected_state"])
    elif tool not in _BATCH:
        result["request_identity"] = identity
    if tool == "trade_cancel_open_orders":
        result.update(identity, status="accepted_noop", noop=True, affected_count=0)
    elif tool == "trade_close_positions":
        result.update(scope="position", noop=False, affected_count=1)
    if tool in {"trade_place_order", "trade_cancel_order"}:
        result["market"] = case["market"]
    if tool in {"trade_place_order", "trade_place_conditional_order", "trade_place_tp_sl"}:
        result["symbol"] = "FIXTURE"
    if tool == "trade_place_tp_sl":
        result.update(plan_type="STOP_LOSS", quantity_mode="exact", quantity="1")
    elif tool == "trade_modify_tp_sl":
        result["trigger_price"] = "1"
    elif tool == "trade_adjust_position_margin":
        result.update(amount="1", adjustment="increase")
    elif tool == "trade_set_auto_append_margin":
        result["enabled"] = True
    if tool in _PLACEMENTS - _BATCH:
        result["upstream_order_id"] = "41"
    if tool in _BATCH or tool in {"trade_place_tp_sl", "trade_close_positions"}:
        identities = case.get("expected_items") or _identities(identity)
        if tool != "trade_close_positions":
            result["counts"] = {"accepted": len(identities), "rejected": 0, "unknown": 0}
        result["items"] = [
            {
                "index": index, "request_identity": item, "status": "accepted",
                "outcome": {"class": "accepted", "write_attempted": True},
                "upstream_order_id": str(41 + index),
            }
            for index, item in enumerate(identities)
        ]
    return {
        "is_error": False,
        "structured_content": {
            "ok": True,
            "action": tool.removeprefix("trade_"),
            "outcome": {"class": "accepted", "write_attempted": True, "grant_disposition": "accepted"},
            "result": result,
        },
    }


def _fake_read_back(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    proof = {
        "complete": True, "matches": True, "identity": case["expected_identity"],
        "market": case["market"], "state": case["expected_state"],
    }
    if result["status"] == "accepted_noop":
        proof["scope_empty"] = True
    if case["tool_name"] == "trade_place_batch_orders":
        proof["upstream_order_ids"] = [item["upstream_order_id"] for item in result["items"]]
    if "upstream_order_id" in result:
        proof["upstream_order_id"] = result["upstream_order_id"]
    if "expected_items" in case:
        proof["affected_identities"] = case["expected_items"]
    return proof


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake-only", action="store_true", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    cases = _fake_plan()
    options = {
        "profile": "offline-fixture", "owner": "normal",
        "spot_base_url": STG_SPOT_ORIGIN, "futures_base_url": STG_FUTURES_ORIGIN,
        "cases": cases, "budgets": {"seed": 0, "test": len(DIRECT_ACTIONS), "cleanup": 1},
        "read_back": _fake_read_back,
        "baseline": lambda: {"complete": True, "clean": True, "snapshot_digest": "sha256:" + "a" * 64},
    }
    positive = run_offline_gate(**options, dispatch=_fake_dispatch)

    def unknown(case: dict[str, Any]) -> dict[str, Any]:
        return {
            "is_error": True,
            "structured_content": {
                "ok": False, "action": case["tool_name"].removeprefix("trade_"),
                "outcome": {"class": "unknown", "write_attempted": True, "grant_disposition": "review_required"},
            },
        }

    stopped = run_offline_gate(**options, dispatch=unknown)
    ok = (
        positive["ok"] and positive["manifest_complete"]
        and stopped["status"] == "review_required" and stopped["stop_reason"] == "unknown"
        and stopped["dispatch_counts"] == {"seed": 0, "test": 1, "cleanup": 0}
        and not stopped["baseline_proven"]
    )
    report = {"mode": "offline_fake", "ok": ok, "verified_sequence": positive, "unknown_stop": stopped}
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    with (args.evidence_dir / "mcp-stg-offline-gate.json").open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    print(json.dumps({"mode": "offline_fake", "ok": ok}, separators=(",", ":")))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
