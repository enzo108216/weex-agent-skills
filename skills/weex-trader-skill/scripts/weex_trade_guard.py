#!/usr/bin/env python3
"""Preview order risk and enforce confirmation before live WEEX order submission."""

from __future__ import annotations

import argparse
import json
import os
import time
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


def _parse_order_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --order-json payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("--order-json must decode to a JSON object.")
    return payload


def _output_json(payload: dict[str, Any], pretty: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))


def _output_error(error: str, pretty: bool) -> None:
    _output_json({"ok": False, "error": error}, pretty)


def _build_user_confirmation(language: str | None) -> dict[str, str]:
    resolved_language = resolve_language(language)
    prompt = CONFIRMATION_PROMPTS[resolved_language]
    return {
        "language": resolved_language,
        "reply_text": prompt["reply_text"],
        "reply_instruction": prompt["reply_instruction"],
    }


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


def _submit_live_order(*, market: str, profile_name: str, raw_order: dict[str, Any]) -> dict[str, Any]:
    normalized_market = str(market).strip().lower()
    if normalized_market == "futures":
        contract_api, client = _build_contract_client(profile_name)
        endpoint = contract_api.ENDPOINTS[contract_api.find_endpoint_key_by_doc_suffix("PlaceOrder")]
        body = {
            "symbol": contract_api.normalize_contract_trade_symbol(str(raw_order["symbol"])),
            "side": str(raw_order["side"]).upper(),
            "positionSide": str(raw_order["position_side"]).upper(),
            "type": str(raw_order["order_type"]).upper(),
            "quantity": raw_order["quantity"],
            "price": raw_order.get("price"),
            "timeInForce": raw_order.get("time_in_force"),
            "tpTriggerPrice": raw_order.get("tp_trigger_price"),
            "slTriggerPrice": raw_order.get("sl_trigger_price"),
        }
        body = {key: value for key, value in body.items() if value not in (None, "")}
        prepared = client.prepare_request(endpoint, query={}, body=body)
        response = client.send(prepared)
    elif normalized_market == "spot":
        spot_api, client = _build_spot_client(profile_name)
        endpoint = spot_api.ENDPOINTS[spot_api.find_endpoint_key_by_doc_suffix("PlaceOrder")]
        body = {
            "symbol": spot_api.normalize_spot_symbol(str(raw_order["symbol"])),
            "side": str(raw_order["side"]).upper(),
            "type": str(raw_order["order_type"]).upper(),
            "quantity": raw_order["quantity"],
            "price": raw_order.get("price"),
            "timeInForce": raw_order.get("time_in_force"),
        }
        body = {key: value for key, value in body.items() if value not in (None, "")}
        prepared = client.prepare_request(endpoint, query={}, body=body)
        response = client.send(prepared)
    else:
        raise AggregationInputError(f"Unsupported market for live order submission: {market}")

    if not response.get("ok"):
        raise AggregationInputError(f"Live order submission failed: {response.get('error')}")
    return response.get("data") if isinstance(response.get("data"), dict) else {"result": response.get("data")}


def cmd_preview_order(args: argparse.Namespace, *, now_ms: int | None = None) -> int:
    raw_order = _parse_order_json(args.order_json)
    trade_aggregator = TradeDataAggregator()
    risk_payload = trade_aggregator.collect_order_risk_payload(
        profile_name=args.profile,
        market=args.market,
        raw_order=raw_order,
    )
    analysis_output = analysis.analyze_order_risk(risk_payload)
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    intent = build_intent(
        profile_name=args.profile,
        market=args.market,
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
    response["user_confirmation"] = _build_user_confirmation(getattr(args, "language", None))
    _output_json(response, args.pretty)
    return 0


def cmd_confirm_order(args: argparse.Namespace, *, now_ms: int | None = None) -> int:
    intent = load_intent()
    if intent is None:
        _output_json({"ok": False, "error": "No pending order intent was found."}, args.pretty)
        return 1
    if args.intent_id and args.intent_id != intent.get("intent_id"):
        _output_json({"ok": False, "error": "Intent id does not match the saved pending order."}, args.pretty)
        return 1
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if intent_is_expired(intent, now_ms=current_ms):
        clear_intent()
        _output_json({"ok": False, "error": "Pending order intent has expired. Generate a new preview first."}, args.pretty)
        return 1
    if not args.confirm_live:
        _output_json({"ok": False, "error": "confirm-order still requires --confirm-live before sending a real order."}, args.pretty)
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

    execution_payload = _submit_live_order(
        market=str(intent["market"]),
        profile_name=str(intent["profile_name"]),
        raw_order=dict(intent["raw_order"]),
    )
    clear_intent()
    _output_json({"ok": True, **execution_payload}, args.pretty)
    return 0


def cmd_account_scan(args: argparse.Namespace) -> int:
    trade_aggregator = TradeDataAggregator()
    payload = trade_aggregator.collect_account_risk_payload(
        profile_name=args.profile,
        market=args.market,
        symbol=args.symbol,
    )
    analysis_output = analysis.analyze_account_risk(payload)
    _output_json(analysis_output, args.pretty)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview order risk and confirm WEEX orders.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preview = subparsers.add_parser("preview-order", help="Preview risk before placing an order.")
    preview.add_argument("--profile", required=True, help="Saved profile name.")
    preview.add_argument("--market", required=True, choices=("futures", "spot"))
    preview.add_argument("--order-json", required=True, help="JSON order payload.")
    preview.add_argument("--ttl-seconds", type=int, default=300, help="Intent TTL in seconds.")
    preview.add_argument("--language", choices=("zh", "en"), default=None, help="Language for human confirmation prompt.")
    preview.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    confirm = subparsers.add_parser("confirm-order", help="Submit the last previewed order.")
    confirm.add_argument("--intent-id", default=None, help="Optional explicit intent id to confirm.")
    confirm.add_argument("--risk-signature", default=None, help="Risk signature returned by preview-order.")
    confirm.add_argument("--confirm-live", action="store_true", help="Required before sending a real order.")
    confirm.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    account_scan = subparsers.add_parser("account-scan", help="Review current account-level risk without an order preview.")
    account_scan.add_argument("--profile", required=True, help="Saved profile name.")
    account_scan.add_argument("--market", required=True, choices=("futures", "spot"))
    account_scan.add_argument("--symbol", default=None, help="Optional trading pair focus.")
    account_scan.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "preview-order":
            return cmd_preview_order(args)
        if args.command == "confirm-order":
            return cmd_confirm_order(args)
        if args.command == "account-scan":
            return cmd_account_scan(args)
        raise SystemExit(f"Unsupported command: {args.command}")
    except AggregationInputError as exc:
        _output_error(str(exc), bool(getattr(args, "pretty", False)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
