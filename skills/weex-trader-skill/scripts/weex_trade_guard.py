#!/usr/bin/env python3
"""Preview order risk and enforce confirmation before live WEEX order submission."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import weex_trade_risk_review as analysis
from weex_order_intent_state import (
    build_intent,
    clear_intent,
    intent_is_expired,
    load_intent,
    save_intent,
)
from weex_profile_language import resolve_language
from weex_trade_data_aggregator import AggregationInputError, TradeDataAggregator


CONFIRMATION_PROMPTS = {
    "zh": {
        "reply_text": "确认",
        "reply_instruction": "如果你接受上述风险并要继续，请回复：确认",
    },
    "en": {
        "reply_text": "confirm",
        "reply_instruction": "If you accept the risks and want to continue, reply: confirm",
    },
}
TRADING_MODES = ("live", "demo")
DEFAULT_TRADING_MODE = "live"


def _parse_order_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --order-json payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("--order-json must decode to a JSON object.")
    return payload


def _parse_tp_sl_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --tp-sl-json payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("--tp-sl-json must decode to a JSON object.")
    return payload


def _output_json(payload: dict[str, Any], pretty: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))


def _output_error(error: str, pretty: bool) -> None:
    _output_json({"ok": False, "error": error}, pretty)


def _normalize_trading_mode(raw: Any) -> str:
    mode = str(raw or DEFAULT_TRADING_MODE).strip().lower()
    if mode not in TRADING_MODES:
        raise AggregationInputError(f"invalid_trading_mode: expected one of {', '.join(TRADING_MODES)}")
    return mode


def _arg_value(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    return vars(args).get(name, default)


def _environment_for_mode(trading_mode: str, market: str) -> dict[str, Any]:
    mode = _normalize_trading_mode(trading_mode)
    normalized_market = str(market or "").strip().lower()
    if mode == "demo":
        if normalized_market != "futures":
            raise AggregationInputError("demo_spot_unsupported: demo trading_mode is only supported for futures")
        return {
            "trading_mode": "demo",
            "label": "demo",
            "market": "futures",
            "uses_real_funds": False,
            "notice": "This operation targets the WEEX simulated futures account environment.",
        }
    return {
        "trading_mode": "live",
        "label": "live",
        "market": normalized_market or "unknown",
        "uses_real_funds": True,
        "notice": f"This operation targets the real WEEX {normalized_market or 'account'} environment.",
    }


def _environment_from_payload_or_mode(payload: dict[str, Any], trading_mode: str, market: str) -> dict[str, Any]:
    environment = payload.get("environment")
    if isinstance(environment, dict) and environment.get("trading_mode"):
        return dict(environment)
    return _environment_for_mode(trading_mode, market)


def _merge_environment_context(
    result: dict[str, Any],
    *,
    trading_mode: str,
    environment: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(result)
    updated["trading_mode"] = trading_mode
    updated["environment"] = environment
    return updated


def _build_user_confirmation(
    language: str | None,
    *,
    environment: dict[str, Any] | None = None,
) -> dict[str, str]:
    resolved_language = resolve_language(language)
    prompt = CONFIRMATION_PROMPTS[resolved_language]
    reply_instruction = prompt["reply_instruction"]
    if environment is not None:
        mode = str(environment.get("trading_mode") or "").strip() or "unknown"
        uses_real_funds = bool(environment.get("uses_real_funds"))
        if resolved_language == "zh":
            funds_text = "会使用真实资金" if uses_real_funds else "不会使用真实资金"
            reply_instruction = f"当前交易环境：{mode}；本次操作{funds_text}。{reply_instruction}"
        else:
            funds_text = "uses real funds" if uses_real_funds else "does not use real funds"
            reply_instruction = f"Trading environment: {mode}; this operation {funds_text}. {reply_instruction}"
    return {
        "language": resolved_language,
        "reply_text": prompt["reply_text"],
        "reply_instruction": reply_instruction,
    }


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        raise AggregationInputError(f"{key} is required")
    return str(value).strip()


def _positive_decimal_text(payload: dict[str, Any], key: str) -> str:
    value = _required_text(payload, key)
    try:
        decimal_value = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise AggregationInputError(f"{key} must be numeric") from exc
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise AggregationInputError(f"{key} must be > 0")
    return value


def _normalize_tp_sl_order(raw_order: dict[str, Any]) -> dict[str, str]:
    client_algo_id = _required_text(raw_order, "clientAlgoId")
    if len(client_algo_id) > 36 or re.fullmatch(r"[\.\:\/A-Za-z0-9_-]{1,36}", client_algo_id) is None:
        raise AggregationInputError("clientAlgoId must be 1-36 allowed characters")

    plan_type = _required_text(raw_order, "planType").upper()
    if plan_type not in {"TAKE_PROFIT", "STOP_LOSS"}:
        raise AggregationInputError("planType must be TAKE_PROFIT or STOP_LOSS")

    position_side = _required_text(raw_order, "positionSide").upper()
    if position_side not in {"LONG", "SHORT"}:
        raise AggregationInputError("positionSide must be LONG or SHORT")

    trigger_price_type = str(raw_order.get("triggerPriceType") or "CONTRACT_PRICE").strip().upper()
    if trigger_price_type not in {"CONTRACT_PRICE", "MARK_PRICE"}:
        raise AggregationInputError("triggerPriceType must be CONTRACT_PRICE or MARK_PRICE")

    normalized = {
        "symbol": _required_text(raw_order, "symbol").upper(),
        "clientAlgoId": client_algo_id,
        "planType": plan_type,
        "triggerPrice": _positive_decimal_text(raw_order, "triggerPrice"),
        "executePrice": str(raw_order.get("executePrice", "0")).strip() or "0",
        "quantity": _positive_decimal_text(raw_order, "quantity"),
        "positionSide": position_side,
        "triggerPriceType": trigger_price_type,
    }
    return normalized


def _build_contract_client(profile_name: str) -> tuple[Any, Any]:
    import weex_contract_api as contract_api

    contract_api.refresh_agent_records(command="trade-guard.contract")
    contract_api.ensure_private_runtime_ready(command="trade-guard.contract", auto_setup=True, language=None)
    profile = contract_api.resolve_runtime_profile(requested_profile=profile_name, allow_invalid_default=False)
    contract_api.require_private_profile(profile)
    env_base_url = os.getenv("WEEX_CONTRACT_API_BASE") or os.getenv("WEEX_API_BASE")
    base_url = (
        (profile.contract_base_url if profile else "")
        or env_base_url
        or contract_api.DEFAULT_BASE_URL
    )
    locale = os.getenv("WEEX_LOCALE") or contract_api.DEFAULT_LOCALE
    timeout = float(os.getenv("WEEX_API_TIMEOUT", contract_api.DEFAULT_TIMEOUT))
    client = contract_api.WeexContractClient(
        base_url=base_url,
        timeout=timeout,
        locale=locale,
        api_key=None,
        api_secret=None,
        api_passphrase=None,
        profile_name=profile.name if profile else None,
    )
    return contract_api, client


def _build_spot_client(profile_name: str) -> tuple[Any, Any]:
    import weex_spot_api as spot_api

    spot_api.refresh_agent_records(command="trade-guard.spot")
    spot_api.ensure_private_runtime_ready(command="trade-guard.spot", auto_setup=True, language=None)
    profile = spot_api.resolve_runtime_profile(requested_profile=profile_name, allow_invalid_default=False)
    spot_api.require_private_profile(profile)
    env_base_url = os.getenv("WEEX_SPOT_API_BASE") or os.getenv("WEEX_API_BASE")
    base_url = (
        (profile.spot_base_url if profile else "")
        or env_base_url
        or spot_api.DEFAULT_BASE_URL
    )
    locale = os.getenv("WEEX_LOCALE") or spot_api.DEFAULT_LOCALE
    timeout = float(os.getenv("WEEX_API_TIMEOUT", spot_api.DEFAULT_TIMEOUT))
    client = spot_api.WeexSpotClient(
        base_url=base_url,
        timeout=timeout,
        locale=locale,
        api_key=None,
        api_secret=None,
        api_passphrase=None,
        profile_name=profile.name if profile else None,
    )
    return spot_api, client


def _submit_order(
    *,
    market: str,
    profile_name: str,
    trading_mode: str,
    raw_order: dict[str, Any],
) -> dict[str, Any]:
    normalized_market = str(market).strip().lower()
    mode = _normalize_trading_mode(trading_mode)
    if mode == "demo" and normalized_market != "futures":
        raise AggregationInputError("demo_spot_unsupported: demo order submission is only supported for futures")
    if normalized_market == "futures":
        position_side = raw_order.get("position_side") or raw_order.get("positionSide")
        order_type = raw_order.get("order_type") or raw_order.get("type")
        if not position_side:
            raise AggregationInputError("futures order requires positionSide")
        if not order_type:
            raise AggregationInputError("futures order requires type")
        contract_api, client = _build_contract_client(profile_name)
        endpoint_key = (
            "sim.transaction.place_order"
            if mode == "demo"
            else contract_api.find_endpoint_key_by_doc_suffix("PlaceOrder")
        )
        endpoint = contract_api.ENDPOINTS[endpoint_key]
        normalized_symbol = (
            contract_api.normalize_contract_demo_trade_symbol(str(raw_order["symbol"]))
            if mode == "demo"
            else contract_api.normalize_contract_trade_symbol(str(raw_order["symbol"]))
        )
        body = {
            "symbol": normalized_symbol,
            "side": str(raw_order["side"]).upper(),
            "positionSide": str(position_side).upper(),
            "type": str(order_type).upper(),
            "quantity": raw_order["quantity"],
            "price": raw_order.get("price"),
            "timeInForce": raw_order.get("time_in_force") or raw_order.get("timeInForce"),
            "newClientOrderId": raw_order.get("new_client_order_id")
            or raw_order.get("newClientOrderId")
            or contract_api.generate_client_oid(),
            "tpTriggerPrice": raw_order.get("tp_trigger_price") or raw_order.get("tpTriggerPrice"),
            "slTriggerPrice": raw_order.get("sl_trigger_price") or raw_order.get("slTriggerPrice"),
            "TpWorkingType": raw_order.get("tp_working_type") or raw_order.get("TpWorkingType"),
            "SlWorkingType": raw_order.get("sl_working_type") or raw_order.get("SlWorkingType"),
        }
        body = {key: value for key, value in body.items() if value not in (None, "")}
        contract_api.validate_endpoint_trading_mode(endpoint, mode)
        prepared = client.prepare_request(endpoint, query={}, body=body)
        response = client.send(prepared)
    elif normalized_market == "spot":
        if mode != "live":
            raise AggregationInputError("demo_spot_unsupported: demo order submission is only supported for futures")
        order_type = raw_order.get("order_type") or raw_order.get("type")
        if not order_type:
            raise AggregationInputError("spot order requires type")
        spot_api, client = _build_spot_client(profile_name)
        endpoint = spot_api.ENDPOINTS[spot_api.find_endpoint_key_by_doc_suffix("PlaceOrder")]
        body = {
            "symbol": spot_api.normalize_spot_symbol(str(raw_order["symbol"])),
            "side": str(raw_order["side"]).upper(),
            "type": str(order_type).upper(),
            "quantity": raw_order["quantity"],
            "price": raw_order.get("price"),
            "timeInForce": raw_order.get("time_in_force") or raw_order.get("timeInForce"),
        }
        body = {key: value for key, value in body.items() if value not in (None, "")}
        prepared = client.prepare_request(endpoint, query={}, body=body)
        response = client.send(prepared)
    else:
        raise AggregationInputError(f"Unsupported market for live order submission: {market}")

    if not response.get("ok"):
        raise AggregationInputError(f"{mode} order submission failed: {response.get('error')}")
    return response.get("data") if isinstance(response.get("data"), dict) else {"result": response.get("data")}


def _submit_live_order(*, market: str, profile_name: str, raw_order: dict[str, Any]) -> dict[str, Any]:
    return _submit_order(
        market=market,
        profile_name=profile_name,
        trading_mode="live",
        raw_order=raw_order,
    )


def _submit_live_tp_sl_order(*, profile_name: str, raw_order: dict[str, Any]) -> dict[str, Any]:
    contract_api, client = _build_contract_client(profile_name)
    endpoint = contract_api.ENDPOINTS[contract_api.find_endpoint_key_by_doc_suffix("PlaceTpSlOrder")]
    normalized = _normalize_tp_sl_order(raw_order)
    normalized["symbol"] = contract_api.normalize_contract_trade_symbol(normalized["symbol"])
    prepared = client.prepare_request(endpoint, query={}, body=normalized)
    response = client.send(prepared)
    if not response.get("ok"):
        raise AggregationInputError(f"Live TP/SL submission failed: {response.get('error')}")
    return response.get("data") if isinstance(response.get("data"), dict) else {"result": response.get("data")}


def cmd_preview_order(args: argparse.Namespace, *, now_ms: int | None = None) -> int:
    raw_order = _parse_order_json(args.order_json)
    trading_mode = _normalize_trading_mode(_arg_value(args, "trading_mode", DEFAULT_TRADING_MODE))
    trade_aggregator = TradeDataAggregator()
    risk_payload = trade_aggregator.collect_order_risk_payload(
        profile_name=args.profile,
        market=args.market,
        trading_mode=trading_mode,
        raw_order=raw_order,
    )
    environment = _environment_from_payload_or_mode(risk_payload, trading_mode, args.market)
    analysis_output = analysis.analyze_order_risk(risk_payload)
    analysis_output = _merge_environment_context(
        analysis_output,
        trading_mode=trading_mode,
        environment=environment,
    )
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    intent = build_intent(
        profile_name=args.profile,
        market=args.market,
        trading_mode=trading_mode,
        environment=environment,
        order_preview=analysis_output.get("order_preview") or risk_payload.get("order_preview", {}),
        raw_order=raw_order,
        analysis_output=analysis_output,
        now_ms=current_ms,
        ttl_seconds=args.ttl_seconds,
    )
    save_intent(intent)
    response = dict(analysis_output)
    response["intent_id"] = intent["intent_id"]
    response["expires_at"] = intent["expires_at"]
    response["risk_signature"] = intent["risk_signature"]
    response["user_confirmation"] = _build_user_confirmation(
        _arg_value(args, "language", None),
        environment=environment,
    )
    _output_json(response, args.pretty)
    return 0


def cmd_preview_tp_sl(args: argparse.Namespace, *, now_ms: int | None = None) -> int:
    trading_mode = _normalize_trading_mode(_arg_value(args, "trading_mode", DEFAULT_TRADING_MODE))
    if trading_mode != "live":
        _output_json({"ok": False, "error": "demo_tp_sl_unsupported: demo TP/SL preview is not supported."}, args.pretty)
        return 1
    tp_sl_order = _normalize_tp_sl_order(_parse_tp_sl_json(args.tp_sl_json))
    trade_aggregator = TradeDataAggregator()
    risk_payload = trade_aggregator.collect_account_risk_payload(
        profile_name=args.profile,
        market="futures",
        trading_mode=trading_mode,
        symbol=tp_sl_order["symbol"],
    )
    environment = _environment_from_payload_or_mode(risk_payload, trading_mode, "futures")
    analysis_output = analysis.analyze_account_risk(risk_payload)
    analysis_output = _merge_environment_context(
        analysis_output,
        trading_mode=trading_mode,
        environment=environment,
    )
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    intent = build_intent(
        profile_name=args.profile,
        market="futures",
        trading_mode=trading_mode,
        environment=environment,
        order_preview=tp_sl_order,
        raw_order=tp_sl_order,
        analysis_output=analysis_output,
        now_ms=current_ms,
        ttl_seconds=args.ttl_seconds,
        intent_type="tp_sl_order",
        tp_sl_order=tp_sl_order,
    )
    save_intent(intent)
    response = dict(analysis_output)
    response["intent_type"] = "tp_sl_order"
    response["tp_sl_order"] = tp_sl_order
    response["intent_id"] = intent["intent_id"]
    response["expires_at"] = intent["expires_at"]
    response["risk_signature"] = intent["risk_signature"]
    response["user_confirmation"] = _build_user_confirmation(
        _arg_value(args, "language", None),
        environment=environment,
    )
    _output_json(response, args.pretty)
    return 0


def _confirm_flags_match_mode(args: argparse.Namespace, trading_mode: str) -> bool:
    confirm_live = bool(_arg_value(args, "confirm_live", False))
    confirm_demo = bool(_arg_value(args, "confirm_demo", False))
    if confirm_live and confirm_demo:
        return False
    if trading_mode == "demo":
        return confirm_demo and not confirm_live
    return confirm_live and not confirm_demo


def cmd_confirm_order(args: argparse.Namespace, *, now_ms: int | None = None) -> int:
    intent = load_intent()
    if intent is None:
        _output_json({"ok": False, "error": "No pending order intent was found."}, args.pretty)
        return 1
    if intent.get("intent_type", "order") != "order":
        _output_json({"ok": False, "error": "Pending intent is not a regular order. Use confirm-tp-sl for TP/SL intents."}, args.pretty)
        return 1
    if args.intent_id and args.intent_id != intent.get("intent_id"):
        _output_json({"ok": False, "error": "Intent id does not match the saved pending order."}, args.pretty)
        return 1
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if intent_is_expired(intent, now_ms=current_ms):
        clear_intent()
        _output_json({"ok": False, "error": "Pending order intent has expired. Generate a new preview first."}, args.pretty)
        return 1
    intent_mode = _normalize_trading_mode(intent.get("trading_mode", DEFAULT_TRADING_MODE))
    requested_mode = _normalize_trading_mode(_arg_value(args, "trading_mode", intent_mode))
    if requested_mode != intent_mode:
        _output_json(
            {
                "ok": False,
                "error": "intent_trading_mode_mismatch: requested trading mode does not match the saved pending order.",
                "requested_trading_mode": requested_mode,
                "intent_trading_mode": intent_mode,
            },
            args.pretty,
        )
        return 1
    if not _confirm_flags_match_mode(args, intent_mode):
        required_flag = "--confirm-demo" if intent_mode == "demo" else "--confirm-live"
        _output_json(
            {
                "ok": False,
                "error": f"confirm_flag_mode_mismatch: confirm-order for {intent_mode} requires {required_flag}.",
                "trading_mode": intent_mode,
            },
            args.pretty,
        )
        return 1
    if not args.intent_id or not args.risk_signature:
        _output_json(
            {
                "ok": False,
                "error": "confirm-order requires both --intent-id and --risk-signature from preview-order.",
            },
            args.pretty,
        )
        return 1
    if args.risk_signature and args.risk_signature != intent.get("risk_signature"):
        _output_json({"ok": False, "error": "Risk signature does not match the saved pending order."}, args.pretty)
        return 1

    if intent_mode == "live":
        execution_payload = _submit_live_order(
            market=str(intent["market"]),
            profile_name=str(intent["profile_name"]),
            raw_order=dict(intent["raw_order"]),
        )
    else:
        execution_payload = _submit_order(
            market=str(intent["market"]),
            profile_name=str(intent["profile_name"]),
            trading_mode=intent_mode,
            raw_order=dict(intent["raw_order"]),
        )
    clear_intent()
    environment = intent.get("environment")
    if not isinstance(environment, dict):
        environment = _environment_for_mode(intent_mode, str(intent["market"]))
    _output_json({"ok": True, **execution_payload, "environment": environment, "trading_mode": intent_mode}, args.pretty)
    return 0


def cmd_confirm_tp_sl(args: argparse.Namespace, *, now_ms: int | None = None) -> int:
    intent = load_intent()
    if intent is None:
        _output_json({"ok": False, "error": "No pending TP/SL intent was found."}, args.pretty)
        return 1
    if intent.get("intent_type") != "tp_sl_order":
        _output_json({"ok": False, "error": "Pending intent is not a TP/SL order."}, args.pretty)
        return 1
    if args.intent_id and args.intent_id != intent.get("intent_id"):
        _output_json({"ok": False, "error": "Intent id does not match the saved pending TP/SL order."}, args.pretty)
        return 1
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if intent_is_expired(intent, now_ms=current_ms):
        clear_intent()
        _output_json({"ok": False, "error": "Pending TP/SL intent has expired. Generate a new preview first."}, args.pretty)
        return 1
    intent_mode = _normalize_trading_mode(intent.get("trading_mode", DEFAULT_TRADING_MODE))
    requested_mode = _normalize_trading_mode(_arg_value(args, "trading_mode", intent_mode))
    if intent_mode != "live" or requested_mode != intent_mode:
        _output_json({"ok": False, "error": "demo_tp_sl_unsupported: TP/SL confirmation is only supported for live futures."}, args.pretty)
        return 1
    if not _confirm_flags_match_mode(args, intent_mode):
        _output_json({"ok": False, "error": "confirm-tp-sl still requires --confirm-live before sending a real TP/SL order."}, args.pretty)
        return 1
    if not args.intent_id or not args.risk_signature:
        _output_json(
            {
                "ok": False,
                "error": "confirm-tp-sl requires both --intent-id and --risk-signature from preview-tp-sl.",
            },
            args.pretty,
        )
        return 1
    if args.risk_signature and args.risk_signature != intent.get("risk_signature"):
        _output_json({"ok": False, "error": "Risk signature does not match the saved pending TP/SL order."}, args.pretty)
        return 1

    tp_sl_order = intent.get("tp_sl_order")
    if not isinstance(tp_sl_order, dict):
        _output_json({"ok": False, "error": "Pending TP/SL intent is missing tp_sl_order."}, args.pretty)
        return 1

    execution_payload = _submit_live_tp_sl_order(
        profile_name=str(intent["profile_name"]),
        raw_order=dict(tp_sl_order),
    )
    clear_intent()
    _output_json({"ok": True, **execution_payload}, args.pretty)
    return 0


def cmd_account_scan(args: argparse.Namespace) -> int:
    trading_mode = _normalize_trading_mode(_arg_value(args, "trading_mode", DEFAULT_TRADING_MODE))
    trade_aggregator = TradeDataAggregator()
    payload = trade_aggregator.collect_account_risk_payload(
        profile_name=args.profile,
        market=args.market,
        trading_mode=trading_mode,
        symbol=args.symbol,
    )
    environment = _environment_from_payload_or_mode(payload, trading_mode, args.market)
    analysis_output = analysis.analyze_account_risk(payload)
    analysis_output = _merge_environment_context(
        analysis_output,
        trading_mode=trading_mode,
        environment=environment,
    )
    _output_json(analysis_output, args.pretty)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview order risk and confirm WEEX orders.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser("preview-order", help="Preview risk before placing an order.")
    preview.add_argument("--profile", required=True, help="Saved profile name.")
    preview.add_argument("--market", required=True, choices=("futures", "spot"))
    preview.add_argument("--trading-mode", choices=TRADING_MODES, default=DEFAULT_TRADING_MODE)
    preview.add_argument("--order-json", required=True, help="JSON order payload.")
    preview.add_argument("--ttl-seconds", type=int, default=300, help="Intent TTL in seconds.")
    preview.add_argument("--language", choices=("zh", "en"), default=None, help="Language for human confirmation prompt.")
    preview.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    preview_tp_sl = subparsers.add_parser("preview-tp-sl", help="Preview risk before placing a futures TP/SL conditional order.")
    preview_tp_sl.add_argument("--profile", required=True, help="Saved profile name.")
    preview_tp_sl.add_argument("--trading-mode", choices=TRADING_MODES, default=DEFAULT_TRADING_MODE)
    preview_tp_sl.add_argument("--tp-sl-json", required=True, help="JSON TP/SL conditional order payload.")
    preview_tp_sl.add_argument("--ttl-seconds", type=int, default=300, help="Intent TTL in seconds.")
    preview_tp_sl.add_argument("--language", choices=("zh", "en"), default=None, help="Language for human confirmation prompt.")
    preview_tp_sl.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    confirm = subparsers.add_parser("confirm-order", help="Submit the last previewed order.")
    confirm.add_argument("--intent-id", default=None, help="Optional explicit intent id to confirm.")
    confirm.add_argument("--risk-signature", default=None, help="Risk signature returned by preview-order.")
    confirm.add_argument("--trading-mode", choices=TRADING_MODES, default=DEFAULT_TRADING_MODE)
    confirm.add_argument("--confirm-live", action="store_true", help="Required before sending a real order.")
    confirm.add_argument("--confirm-demo", action="store_true", help="Required before sending a demo futures order.")
    confirm.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    confirm_tp_sl = subparsers.add_parser("confirm-tp-sl", help="Submit the last previewed futures TP/SL conditional order.")
    confirm_tp_sl.add_argument("--intent-id", default=None, help="Optional explicit intent id to confirm.")
    confirm_tp_sl.add_argument("--risk-signature", default=None, help="Risk signature returned by preview-tp-sl.")
    confirm_tp_sl.add_argument("--trading-mode", choices=TRADING_MODES, default=DEFAULT_TRADING_MODE)
    confirm_tp_sl.add_argument("--confirm-live", action="store_true", help="Required before sending a real TP/SL order.")
    confirm_tp_sl.add_argument("--confirm-demo", action="store_true", help="Rejected for unsupported demo TP/SL orders.")
    confirm_tp_sl.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    account_scan = subparsers.add_parser("account-scan", help="Review current account-level risk without an order preview.")
    account_scan.add_argument("--profile", required=True, help="Saved profile name.")
    account_scan.add_argument("--market", required=True, choices=("futures", "spot"))
    account_scan.add_argument("--trading-mode", choices=TRADING_MODES, default=DEFAULT_TRADING_MODE)
    account_scan.add_argument("--symbol", default=None, help="Optional trading pair focus.")
    account_scan.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "preview-order":
            return cmd_preview_order(args)
        if args.command == "preview-tp-sl":
            return cmd_preview_tp_sl(args)
        if args.command == "confirm-order":
            return cmd_confirm_order(args)
        if args.command == "confirm-tp-sl":
            return cmd_confirm_tp_sl(args)
        if args.command == "account-scan":
            return cmd_account_scan(args)
        raise SystemExit(f"Unsupported command: {args.command}")
    except AggregationInputError as exc:
        _output_error(str(exc), bool(getattr(args, "pretty", False)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
