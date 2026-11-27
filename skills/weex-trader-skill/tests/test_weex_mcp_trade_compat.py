#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MAPPING_PATH = ROOT / "references" / "mcp-capability-mapping.json"
MODULE_PATH = SCRIPTS / "weex_mcp_trade_compat.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_mapping() -> dict[str, object]:
    return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))


def mapping_entry(mapping: dict[str, object], key: str) -> dict[str, object]:
    matches = [item for item in mapping["capabilities"] if item["legacy_key"] == key]
    if len(matches) != 1:
        raise AssertionError(f"expected one mapping for {key}, got {len(matches)}")
    return matches[0]


def load_trade_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("weex_mcp_trade_compat", MODULE_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def endpoint_from_mapping(entry: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        key=entry["legacy_key"],
        method=entry["method"],
        path=entry["path"],
        permission=entry["auth"],
        request_transport=entry["request_transport"],
    )


class TradeMappingContractTests(unittest.TestCase):
    def test_21_write_mapping_matches_typed_schema_contract(self) -> None:
        mapping = load_mapping()
        writes = [
            item
            for item in mapping["capabilities"]
            if item["product"] in {"contract", "spot"}
            and item["target"]["variant"] == "preview_confirm"
        ]
        self.assertEqual(
            {
                product: sum(item["product"] == product for item in writes)
                for product in ("contract", "spot")
            },
            {"contract": 16, "spot": 5},
        )

        leverage = mapping_entry(mapping, "account.update_leverage_trade")
        self.assertEqual(
            {
                field: leverage["field_decisions"][field]["target"]
                for field in (
                    "crossLeverage",
                    "isolatedLongLeverage",
                    "isolatedShortLeverage",
                )
            },
            {
                "crossLeverage": "cross_leverage",
                "isolatedLongLeverage": "isolated_long_leverage",
                "isolatedShortLeverage": "isolated_short_leverage",
            },
        )
        bulk_cancel = mapping_entry(mapping, "spot.order.bulk_cancel")
        self.assertEqual(
            bulk_cancel["field_decisions"]["origClientOrderIds"]["target"],
            "client_order_ids",
        )
        bulk_order = mapping_entry(mapping, "spot.order.bulk_order")
        self.assertEqual(
            bulk_order["field_decisions"]["symbol"]["target"],
            "orders[].symbol",
        )
        for field, target in {
            "side": "side",
            "type": "order_type",
            "timeInForce": "time_in_force",
            "quantity": "quantity",
            "price": "price",
            "newClientOrderId": "client_order_id",
        }.items():
            self.assertEqual(
                bulk_order["field_decisions"][f"orderList[].{field}"]["target"],
                f"orders[].{target}",
            )

    def test_all_21_write_operations_match_catalog_and_frozen_golden(self) -> None:
        import weex_contract_api as contract
        import weex_spot_api as spot

        mapping = load_mapping()
        writes = sorted(
            (
                item
                for item in mapping["capabilities"]
                if item["product"] in {"contract", "spot"}
                and item["target"]["variant"] == "preview_confirm"
            ),
            key=lambda item: (item["product"], item["legacy_key"]),
        )
        matrix = [
            {
                "legacy_key": item["legacy_key"],
                "product": item["product"],
                "method": item["method"],
                "path": item["path"],
                "request_transport": item["request_transport"],
                "catalog_request_fields": item["catalog_request_fields"],
                "field_decisions": item["field_decisions"],
                "nested_field_decisions": item.get("nested_field_decisions", {}),
                "target": {
                    "tool": item["target"]["tool"],
                    "confirm_tool": item["target"]["confirm_tool"],
                    "fixed_arguments": item["target"]["fixed_arguments"],
                    "derived_arguments": item["target"].get("derived_arguments", {}),
                },
                "legacy_container": item["response"]["legacy_container"],
            }
            for item in writes
        ]
        digest = hashlib.sha256(
            json.dumps(
                matrix,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            digest,
            "d707c666dda2bbe315e68319b336bedd79505f5f76ca2a8758182e446762ff24",
        )
        self.assertEqual(
            mapping["compatibility_gates"]["t6_manual_trade"],
            {
                "status": "golden_verified",
                "contract_operations": 16,
                "spot_operations": 5,
                "operation_matrix_sha256": digest,
                "test": "tests/test_weex_mcp_trade_compat.py",
                "direct_rest_fallback": False,
                "delegated_authorization": False,
            },
        )
        self.assertEqual(
            {item["legacy_key"] for item in writes if item["product"] == "contract"},
            {key for key, endpoint in contract.ENDPOINTS.items() if endpoint.mutating},
        )
        self.assertEqual(
            {item["legacy_key"] for item in writes if item["product"] == "spot"},
            {key for key, endpoint in spot.ENDPOINTS.items() if spot.is_mutating(endpoint)},
        )


class FakeGateway:
    def __init__(
        self,
        responses: list[dict[str, object]],
        *,
        transport_metas: list[dict[str, object] | None] | None = None,
        **kwargs: object,
    ):
        self.responses = list(responses)
        self.transport_metas = list(transport_metas or [None] * len(responses))
        self.init_kwargs = kwargs
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_typed_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, copy.deepcopy(arguments)))
        payload = copy.deepcopy(self.responses.pop(0))
        ok = payload.get("ok") if isinstance(payload, dict) else None
        return {
            "structured_content": payload,
            "is_error": isinstance(ok, bool) and not ok,
            "transport_meta": copy.deepcopy(self.transport_metas.pop(0)),
        }


class McpTradeCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trade = load_trade_module()
        self.assertIsNotNone(
            self.trade,
            "weex_mcp_trade_compat.py is missing; expected T6 RED.",
        )
        self.mapping = load_mapping()
        self.settings = self.trade.LocalMcpTradeSettings(
            profile_id="profile-a",
            spot_base_url="https://api-spot.weex.com",
            futures_base_url="https://api-contract.weex.com",
            timeout_seconds=15.0,
            locale="zh-CN",
        )

    def facade(self, gateway: FakeGateway):
        ids = iter(("stable-id-1", "stable-id-2", "stable-id-3"))
        return self.trade.McpTradeFacade(
            settings=self.settings,
            gateway_factory=lambda **kwargs: gateway,
            mapping_loader=lambda: copy.deepcopy(self.mapping),
            client_id_factory=lambda: next(ids),
        )

    def test_describe_maps_typed_arguments_without_building_gateway(self) -> None:
        entry = mapping_entry(self.mapping, "spot.order.place_order")
        calls = []
        facade = self.trade.McpTradeFacade(
            settings=self.settings,
            gateway_factory=lambda **_kwargs: self.fail(
                "request description must not construct a gateway"
            ),
            request_describer=lambda **kwargs: calls.append(kwargs)
            or {
                "structured_content": {
                    "ok": True,
                    "result": {
                        "request": {
                            "method": "POST",
                            "url": "https://api-spot.weex.com/api/v3/order",
                            "headers": {},
                            "query": {},
                            "body": {"symbol": "BTCUSDT"},
                        }
                    },
                },
                "is_error": False,
                "transport_meta": None,
            },
            mapping_loader=lambda: copy.deepcopy(self.mapping),
            client_id_factory=lambda: "stable-dry-run-id",
        )

        description = facade.describe(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quantity": "1",
            },
        )

        self.assertEqual(description["method"], "POST")
        self.assertEqual(calls[0]["name"], "trade_preview_order")
        self.assertEqual(
            calls[0]["arguments"]["client_order_id"],
            "stable-dry-run-id",
        )
        self.assertTrue(calls[0]["trade_enabled"])

    def test_preview_maps_spot_batch_once_with_nested_aliases_and_stable_ids(self) -> None:
        entry = mapping_entry(self.mapping, "spot.order.bulk_order")
        gateway = FakeGateway(
            [
                {
                    "ok": True,
                    "preview_id": "opaque-preview",
                    "expires_at": 1234,
                    "summary": {"action": "batch_order"},
                    "risk": {"level": "high"},
                }
            ]
        )
        preview = self.facade(gateway).preview(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={
                "symbol": "BTCUSDT",
                "orderList": [
                    {
                        "side": "BUY",
                        "type": "LIMIT",
                        "timeInForce": "GTC",
                        "quantity": 1,
                        "price": 100,
                    },
                    {
                        "side": "SELL",
                        "type": "MARKET",
                        "quantity": "2",
                        "newClientOrderId": "existing-id",
                    },
                ],
            },
        )

        self.assertTrue(preview["ok"])
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(
            gateway.calls[0],
            (
                "trade_preview_batch_order",
                {
                    "market": "spot",
                    "trading_mode": "live",
                    "orders": [
                        {
                            "symbol": "BTCUSDT",
                            "side": "BUY",
                            "order_type": "LIMIT",
                            "time_in_force": "GTC",
                            "quantity": "1",
                            "price": "100",
                            "client_order_id": "stable-id-1",
                        },
                        {
                            "symbol": "BTCUSDT",
                            "side": "SELL",
                            "order_type": "MARKET",
                            "quantity": "2",
                            "client_order_id": "existing-id",
                        },
                    ],
                },
            ),
        )
        record = preview["preview"]
        self.assertNotIn("credential", json.dumps(record))
        self.assertEqual(record["preview_id"], "opaque-preview")
        self.assertEqual(record["confirm_tool"], "trade_confirm_batch_order")

    def test_contract_batch_preserves_tp_sl_fields_and_spot_rejects_them(self) -> None:
        contract_entry = mapping_entry(self.mapping, "transaction.place_orders_batch")
        gateway = FakeGateway(
            [{"ok": True, "preview_id": "p", "expires_at": 10, "summary": {}, "risk": {}}]
        )
        preview = self.facade(gateway).preview(
            product="contract",
            endpoint=endpoint_from_mapping(contract_entry),
            query={},
            body={
                "batchOrders": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "positionSide": "LONG",
                        "type": "LIMIT",
                        "timeInForce": "GTC",
                        "quantity": "1",
                        "price": "100",
                        "tpTriggerPrice": "120",
                        "slTriggerPrice": "80",
                        "TpWorkingType": "MARK_PRICE",
                        "SlWorkingType": "CONTRACT_PRICE",
                    }
                ]
            },
        )
        self.assertTrue(preview["ok"])
        item = gateway.calls[0][1]["orders"][0]
        self.assertEqual(item["tp_trigger_price"], "120")
        self.assertEqual(item["sl_trigger_price"], "80")
        self.assertEqual(item["tp_working_type"], "MARK_PRICE")
        self.assertEqual(item["sl_working_type"], "CONTRACT_PRICE")

        spot_entry = mapping_entry(self.mapping, "spot.order.bulk_order")
        with self.assertRaises(self.trade.McpTradeCompatibilityError):
            self.facade(FakeGateway([])).preview(
                product="spot",
                endpoint=endpoint_from_mapping(spot_entry),
                query={},
                body={
                    "symbol": "BTCUSDT",
                    "orderList": [
                        {
                            "side": "BUY",
                            "type": "MARKET",
                            "quantity": "1",
                            "tpTriggerPrice": "120",
                        }
                    ],
                },
            )

    def test_value_and_derived_rules_are_explicit_and_fail_closed(self) -> None:
        cases = (
            (
                "account.adjust_position_margin_trade",
                {"isolatedPositionId": 7, "amount": 10, "type": 1},
                {"isolated_position_id": "7", "amount": "10", "adjustment": "increase"},
            ),
            (
                "account.change_margin_mode_trade",
                {"symbol": "BTCUSDT", "marginType": "CROSSED"},
                {"symbol": "BTCUSDT", "margin_mode": "cross"},
            ),
            (
                "transaction.close_positions",
                {"symbol": "BTCUSDT", "positionId": 9},
                {"symbol": "BTCUSDT", "position_id": "9", "scope": "position"},
            ),
            (
                "transaction.place_tp_sl_order",
                {
                    "symbol": "BTCUSDT",
                    "clientAlgoId": "algo-1",
                    "planType": "TAKE_PROFIT",
                    "triggerPrice": 100,
                    "executePrice": 0,
                    "quantity": 0,
                    "positionSide": "LONG",
                    "triggerPriceType": "MARK_PRICE",
                },
                {
                    "symbol": "BTCUSDT",
                    "client_algo_id": "algo-1",
                    "plan_type": "TAKE_PROFIT",
                    "trigger_price": "100",
                    "execute_price": "0",
                    "quantity": "0",
                    "position_side": "LONG",
                    "trigger_price_type": "MARK_PRICE",
                    "acknowledge_full_position": True,
                },
            ),
        )
        for legacy_key, body, expected_dynamic in cases:
            with self.subTest(legacy_key=legacy_key):
                entry = mapping_entry(self.mapping, legacy_key)
                gateway = FakeGateway(
                    [{"ok": True, "preview_id": "p", "expires_at": 10, "summary": {}, "risk": {}}]
                )
                result = self.facade(gateway).preview(
                    product="contract",
                    endpoint=endpoint_from_mapping(entry),
                    query={},
                    body=body,
                )
                self.assertTrue(result["ok"])
                arguments = gateway.calls[0][1]
                for key, value in expected_dynamic.items():
                    self.assertEqual(arguments[key], value)

        close_entry = mapping_entry(self.mapping, "transaction.close_positions")
        gateway = FakeGateway(
            [{"ok": True, "preview_id": "p", "expires_at": 10, "summary": {}, "risk": {}}]
        )
        empty_close = self.facade(gateway).preview(
            product="contract",
            endpoint=endpoint_from_mapping(close_entry),
            query={},
            body={},
        )
        self.assertTrue(empty_close["ok"])
        self.assertEqual(
            gateway.calls[0][1],
            {
                "market": "futures",
                "trading_mode": "live",
                "scope": "all",
                "acknowledge_all_positions": True,
            },
        )

    def test_confirm_replays_exact_arguments_and_projects_accepted_raw(self) -> None:
        entry = mapping_entry(self.mapping, "spot.order.place_order")
        gateway = FakeGateway(
            [
                {
                    "ok": True,
                    "preview_id": "opaque-preview",
                    "expires_at": 1234,
                    "summary": {},
                    "risk": {},
                },
                {
                    "ok": True,
                    "result": {"raw": {"orderId": "o-1"}},
                    "outcome": {
                        "class": "accepted",
                        "write_attempted": True,
                        "action": "order",
                        "client_order_id": "stable-id-1",
                    },
                },
            ],
            transport_metas=[
                {
                    "version": 1,
                    "request_id": "preview",
                    "attempt_count": 1,
                    "http_status": 200,
                },
                {
                    "version": 1,
                    "request_id": "confirm",
                    "attempt_count": 1,
                    "http_status": 202,
                },
            ],
        )
        facade = self.facade(gateway)
        preview = facade.preview(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quantity": 1,
            },
        )
        confirmed = facade.confirm(preview["preview"])

        self.assertEqual(
            gateway.calls[1][1],
            {
                **gateway.calls[0][1],
                "preview_id": "opaque-preview",
                "confirmation": {"accepted": True},
            },
        )
        self.assertEqual(confirmed["ok"], True)
        self.assertEqual(preview["status"], 200)
        self.assertEqual(confirmed["status"], 202)
        self.assertEqual(confirmed["data"], {"orderId": "o-1"})
        self.assertEqual(confirmed["outcome"]["class"], "accepted")

    def test_preview_error_preserves_transport_status(self) -> None:
        entry = mapping_entry(self.mapping, "spot.order.place_order")
        gateway = FakeGateway(
            [{"ok": False, "error": {"code": "rejected", "message": "Rejected."}}],
            transport_metas=[
                {
                    "version": 1,
                    "request_id": "preview-error",
                    "attempt_count": 1,
                    "http_status": 400,
                }
            ],
        )

        result = self.facade(gateway).preview(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quantity": "1",
            },
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 400)
        self.assertEqual(result["error"]["code"], "rejected")

    def test_unknown_and_partial_are_review_required_and_binding_mismatch_is_not_called(self) -> None:
        entry = mapping_entry(self.mapping, "transaction.place_orders_batch")
        for outcome_class in ("unknown", "partial"):
            with self.subTest(outcome_class=outcome_class):
                gateway = FakeGateway(
                    [
                        {"ok": True, "preview_id": "p", "expires_at": 10, "summary": {}, "risk": {}},
                        {
                            "ok": False,
                            "error": {"code": "upstream_error", "message": "uncertain"},
                            "outcome": {
                                "class": outcome_class,
                                "write_attempted": True,
                                "action": "batch_order",
                                "items": [],
                            },
                        },
                    ],
                    transport_metas=[
                        None,
                        {
                            "version": 1,
                            "request_id": f"{outcome_class}-confirm",
                            "attempt_count": 1,
                            "http_status": 504 if outcome_class == "unknown" else 207,
                        },
                    ],
                )
                facade = self.facade(gateway)
                preview = facade.preview(
                    product="contract",
                    endpoint=endpoint_from_mapping(entry),
                    query={},
                    body={
                        "batchOrders": [
                            {
                                "symbol": "BTCUSDT",
                                "side": "BUY",
                                "positionSide": "LONG",
                                "type": "MARKET",
                                "quantity": "1",
                            }
                        ]
                    },
                )
                confirmed = facade.confirm(preview["preview"])
                self.assertFalse(confirmed["ok"])
                self.assertTrue(confirmed["review_required"])
                self.assertEqual(
                    confirmed["status"],
                    504 if outcome_class == "unknown" else 207,
                )
                self.assertEqual(confirmed["trade_status"], "REVIEW_REQUIRED")

        gateway = FakeGateway([])
        facade = self.facade(gateway)
        bad_record = {
            "version": 1,
            "product": "spot",
            "legacy_key": "spot.order.place_order",
            "preview_tool": "trade_preview_order",
            "confirm_tool": "trade_confirm_order",
            "arguments": {},
            "preview_id": "p",
            "expires_at": 10,
            "profile_id": "other-profile",
            "spot_base_url": self.settings.spot_base_url,
            "futures_base_url": self.settings.futures_base_url,
        }
        with self.assertRaises(self.trade.McpTradeCompatibilityError):
            facade.confirm(bad_record)
        self.assertEqual(gateway.calls, [])

    def test_confirm_malformed_envelope_after_tool_call_is_unknown(self) -> None:
        entry = mapping_entry(self.mapping, "spot.order.place_order")
        gateway = FakeGateway(
            [
                {
                    "ok": True,
                    "preview_id": "opaque-preview",
                    "expires_at": 1234,
                    "summary": {},
                    "risk": {},
                },
                {"ok": True, "result": {"raw": {"orderId": "maybe-written"}}},
            ]
        )
        facade = self.facade(gateway)
        preview = facade.preview(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quantity": "1",
            },
        )

        confirmed = facade.confirm(preview["preview"])

        self.assertFalse(confirmed["ok"])
        self.assertTrue(confirmed["review_required"])
        self.assertEqual(confirmed["trade_status"], "REVIEW_REQUIRED")
        self.assertEqual(
            confirmed["outcome"],
            {
                "class": "unknown",
                "write_attempted": True,
                "action": "order",
            },
        )

    def test_futures_cancel_scope_derives_symbol_or_confirmed_all(self) -> None:
        for legacy_key in (
            "transaction.cancel_all_orders",
            "transaction.cancel_all_pending_orders",
        ):
            with self.subTest(legacy_key=legacy_key):
                entry = mapping_entry(self.mapping, legacy_key)
                all_gateway = FakeGateway(
                    [{"ok": True, "preview_id": "p", "expires_at": 10, "summary": {}, "risk": {}}]
                )
                self.facade(all_gateway).preview(
                    product="contract",
                    endpoint=endpoint_from_mapping(entry),
                    query={},
                    body={},
                )
                self.assertEqual(all_gateway.calls[0][1]["scope"], "all")
                self.assertTrue(all_gateway.calls[0][1]["acknowledge_all_symbols"])

                symbol_gateway = FakeGateway(
                    [{"ok": True, "preview_id": "p", "expires_at": 10, "summary": {}, "risk": {}}]
                )
                self.facade(symbol_gateway).preview(
                    product="contract",
                    endpoint=endpoint_from_mapping(entry),
                    query={"symbol": "BTCUSDT"},
                    body={},
                )
                self.assertEqual(symbol_gateway.calls[0][1]["scope"], "symbol")
                self.assertEqual(symbol_gateway.calls[0][1]["symbol"], "BTCUSDT")
                self.assertNotIn("acknowledge_all_symbols", symbol_gateway.calls[0][1])

    def test_delegated_confirm_does_not_inject_authorizer_into_v2_gateway(self) -> None:
        entry = mapping_entry(self.mapping, "spot.order.place_order")
        gateway = FakeGateway(
            [
                {"ok": True, "preview_id": "p", "expires_at": 10, "summary": {}, "risk": {}},
                {
                    "ok": True,
                    "result": {"raw": {"orderId": "o-1"}},
                    "outcome": {
                        "class": "accepted",
                        "write_attempted": True,
                        "action": "order",
                        "client_order_id": "stable-id-1",
                    },
                },
            ]
        )
        captured: dict[str, object] = {}
        authorizer = mock.Mock()

        def gateway_factory(**kwargs):
            captured.update(kwargs)
            return gateway

        ids = iter(("stable-id-1",))
        facade = self.trade.McpTradeFacade(
            settings=self.settings,
            gateway_factory=gateway_factory,
            mapping_loader=lambda: copy.deepcopy(self.mapping),
            client_id_factory=lambda: next(ids),
            delegated_authorizer=authorizer,
        )
        preview = facade.preview(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quantity": "1",
            },
        )
        confirmed = facade.confirm_delegated(
            preview["preview"],
            delegated_ref="opaque-ref",
        )

        self.assertTrue(confirmed["ok"])
        self.assertNotIn("delegated_authorizer", captured)
        confirm_arguments = gateway.calls[1][1]
        self.assertEqual(
            confirm_arguments["delegated_authorization"],
            {"ref": "opaque-ref"},
        )
        self.assertNotIn("confirmation", confirm_arguments)

    def test_definitive_rejection_and_not_sent_are_not_review_required(self) -> None:
        entry = mapping_entry(self.mapping, "spot.order.cancel_order")
        cases = (
            ("definitive_rejection", True, "REJECTED"),
            ("not_sent", False, "NOT_SENT"),
        )
        for outcome_class, write_attempted, expected_status in cases:
            with self.subTest(outcome_class=outcome_class):
                gateway = FakeGateway(
                    [
                        {"ok": True, "preview_id": "p", "expires_at": 10, "summary": {}, "risk": {}},
                        {
                            "ok": False,
                            "error": {"code": "upstream_error", "message": "rejected"},
                            "outcome": {
                                "class": outcome_class,
                                "write_attempted": write_attempted,
                                "action": "cancel_order",
                            },
                        },
                    ],
                    transport_metas=[
                        None,
                        {
                            "version": 1,
                            "request_id": f"{outcome_class}-confirm",
                            "attempt_count": 1,
                            "http_status": 409 if outcome_class == "definitive_rejection" else 400,
                        },
                    ],
                )
                facade = self.facade(gateway)
                preview = facade.preview(
                    product="spot",
                    endpoint=endpoint_from_mapping(entry),
                    query={"orderId": 7},
                    body={},
                )
                confirmed = facade.confirm(preview["preview"])
                self.assertFalse(confirmed["ok"])
                self.assertFalse(confirmed["review_required"])
                self.assertEqual(confirmed["trade_status"], expected_status)
                self.assertEqual(
                    confirmed["status"],
                    409 if outcome_class == "definitive_rejection" else 400,
                )


if __name__ == "__main__":
    unittest.main()
