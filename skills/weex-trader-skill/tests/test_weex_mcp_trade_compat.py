#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import importlib.util
import inspect
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MAPPING_PATH = ROOT / "references" / "mcp-capability-mapping.json"
BOUNDARY_PATH = ROOT / "references" / "mcp-boundary-contract-v2.json"
MODULE_PATH = SCRIPTS / "weex_mcp_trade_compat.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_mapping() -> dict[str, object]:
    return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))


def load_boundary() -> dict[str, object]:
    return json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))


def mapping_entry(mapping: dict[str, object], key: str) -> dict[str, object]:
    matches = [item for item in mapping["capabilities"] if item["legacy_key"] == key]
    if len(matches) != 1:
        raise AssertionError(f"expected one mapping for {key}, got {len(matches)}")
    return matches[0]


def load_trade_module():
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
    def test_21_legacy_writes_keep_field_decisions_and_map_to_14_direct_tools(self) -> None:
        mapping = load_mapping()
        boundary = load_boundary()
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
        legacy_direct = boundary["legacy_write_mapping"]
        self.assertEqual(
            {item["legacy_key"] for item in writes},
            set(legacy_direct),
        )
        self.assertEqual(len(set(legacy_direct.values())), 14)
        direct_names = {item["name"] for item in boundary["direct_tools"]}
        self.assertEqual(set(legacy_direct.values()), direct_names)

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
        bulk_order = mapping_entry(mapping, "spot.order.bulk_order")
        self.assertEqual(
            bulk_order["field_decisions"]["symbol"]["target"],
            "orders[].symbol",
        )

    def test_legacy_mapping_golden_and_capability_digest_remain_unchanged(self) -> None:
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
            {item["legacy_key"] for item in writes if item["product"] == "contract"},
            {key for key, endpoint in contract.ENDPOINTS.items() if endpoint.mutating},
        )
        self.assertEqual(
            {item["legacy_key"] for item in writes if item["product"] == "spot"},
            {key for key, endpoint in spot.ENDPOINTS.items() if spot.is_mutating(endpoint)},
        )
        capabilities_digest = hashlib.sha256(
            json.dumps(
                mapping["capabilities"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            capabilities_digest,
            "3d5b9c2ad3bdd3f85f3c64524b551512a0986284b5bee07b9b4d4e075a2381a8",
        )


class FakeGateway:
    def __init__(
        self,
        responses: list[dict[str, object]] | None = None,
        *,
        transport_metas: list[dict[str, object] | None] | None = None,
        prepare_error: Exception | None = None,
        call_error: Exception | None = None,
        on_call=None,
        **kwargs: object,
    ):
        self.responses = list(responses or [])
        self.transport_metas = list(
            transport_metas or [None] * len(self.responses)
        )
        self.prepare_error = prepare_error
        self.call_error = call_error
        self.on_call = on_call
        self.init_kwargs = kwargs
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.prepares: list[tuple[str, dict[str, object]]] = []
        self.grants: list[tuple[dict[str, object], str, int]] = []
        self.execution_grants: list[object] = []
        self.recoveries: list[tuple[str, object, int | None]] = []
        self.reconciliations: list[tuple[dict[str, object], int]] = []
        self.reconciliation_status = "UNRESOLVED"

    def reconcile_mutation(self, prepared, *, now_ms):
        self.reconciliations.append((copy.deepcopy(prepared), now_ms))
        return {
            "status": self.reconciliation_status,
            "observed_at": now_ms,
            "evidence_digest": "sha256:" + "9" * 64,
        }

    def prepare_mutation(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        if self.prepare_error is not None:
            raise self.prepare_error
        self.prepares.append((name, copy.deepcopy(arguments)))
        action = name.removeprefix("trade_")
        return {
            "contract_version": "1",
            "tool_name": name,
            "action": action,
            "market": arguments["market"],
            "trading_mode": arguments["trading_mode"],
            "canonical_arguments": copy.deepcopy(arguments),
            "canonical_arguments_digest": "sha256:" + "1" * 64,
            "risk_facts_digest": "sha256:" + "2" * 64,
            "route_fingerprint": "sha256:" + "3" * 64,
            "credential_fingerprint": "credential-fingerprint",
            "precondition": {
                "policy": "STATIC",
                "digest": "sha256:" + "4" * 64,
                "observed_at": 100,
                "expires_at": 10_000,
                "facts": {},
            },
            "risk_facts": {"level": "normal"},
            "reconciliation_template": {
                "strategy": "query_exact_identity",
                "read_tool": "account_get_orders",
                "arguments": {},
                "expected": {},
                "identity": {
                    "client_order_id": arguments.get(
                        "client_order_id",
                        "client-1",
                    )
                },
                "completeness": "deterministic",
            },
            "expires_at": 10_000,
            "execution_binding_digest": "sha256:" + "5" * 64,
            "prepared_artifact_digest": "sha256:" + "6" * 64,
        }

    def build_execution_grant(
        self,
        prepared: dict[str, object],
        *,
        grant_id: str,
        issued_at: int,
    ):
        self.grants.append((copy.deepcopy(prepared), grant_id, issued_at))
        return SimpleNamespace(
            grant_id=grant_id,
            state="ISSUED",
            principal_id="profile-a",
            account_binding_id="account-a",
            credential_fingerprint=prepared["credential_fingerprint"],
            tool_name=prepared["tool_name"],
            prepared_artifact_digest=prepared["prepared_artifact_digest"],
            execution_binding_digest=prepared["execution_binding_digest"],
            issued_at=issued_at,
            expires_at=prepared["expires_at"],
        )

    def call_typed_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        execution_grant: object = None,
    ) -> dict[str, object]:
        self.calls.append((name, copy.deepcopy(arguments)))
        self.execution_grants.append(execution_grant)
        if self.on_call is not None:
            self.on_call()
        if self.call_error is not None:
            raise self.call_error
        payload = copy.deepcopy(self.responses.pop(0))
        ok = payload.get("ok") if isinstance(payload, dict) else None
        return {
            "structured_content": payload,
            "is_error": isinstance(ok, bool) and not ok,
            "transport_meta": copy.deepcopy(self.transport_metas.pop(0)),
        }

    def recover_mutation(
        self,
        *,
        tool_name: str,
        execution_grant: object,
        now_ms: int | None = None,
    ):
        self.recoveries.append((tool_name, execution_grant, now_ms))
        return SimpleNamespace(state="REVIEW_REQUIRED")


class MemoryIntentKernel:
    def __init__(self):
        self.intent: dict[str, object] | None = None
        self.archived: dict[str, dict[str, object]] = {}
        self.grant: object | None = None
        self.store_marker = self
        self.reconciliation_calls: list[dict[str, object]] = []

    def save(self, intent, **_kwargs):
        self.intent = copy.deepcopy(intent)

    def load(self, *, expected_intent_id=None, expected_prepared_artifact_digest=None, **_kwargs):
        candidate = self.intent
        if expected_intent_id is not None:
            if candidate is None or candidate["intent_id"] != expected_intent_id:
                candidate = self.archived.get(expected_intent_id)
            if candidate is None or candidate.get("prepared_artifact_digest") != expected_prepared_artifact_digest:
                return None
        return copy.deepcopy(candidate)

    def confirm(self, *, execution_grant, confirmed_at, **_kwargs):
        if self.intent is None:
            raise AssertionError("intent was not saved")
        self.grant = execution_grant
        self.intent["state"] = "CONFIRMED"
        self.intent["confirmed_at"] = confirmed_at
        return copy.deepcopy(self.intent)

    def grant_for_dispatch(self, _intent_id, **_kwargs):
        if self.grant is None:
            raise AssertionError("grant was not issued")
        return self.grant

    def set_state(self, state: str):
        if self.intent is not None:
            self.intent["state"] = state

    def record_reconciliation_result(
        self,
        _intent_id,
        *,
        status,
        evidence_digest,
        observed_at,
    ):
        self.reconciliation_calls.append(
            {
                "intent_id": _intent_id,
                "status": status,
                "evidence_digest": evidence_digest,
                "observed_at": observed_at,
            }
        )
        if self.intent is None:
            raise AssertionError("intent was not saved")
        self.intent["reconciliation"]["status"] = status
        self.intent["reconciliation"]["observed_at"] = observed_at
        self.intent["reconciliation"]["evidence_digest"] = evidence_digest
        self.intent["reconciliation"]["result"] = {
            "RESOLVED_ACCEPTED": "ACCEPTED",
            "RESOLVED_REJECTED": "DEFINITIVE_REJECTION",
            "RESOLVED_NOT_SENT": "NOT_SENT",
        }.get(status)
        return copy.deepcopy(self.intent)


class McpTradeCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trade = load_trade_module()
        self.assertIsNotNone(self.trade)
        temp_home = tempfile.TemporaryDirectory()
        self.addCleanup(temp_home.cleanup)
        environment = mock.patch.dict("os.environ", {"WEEX_TRADER_SKILL_HOME": temp_home.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.mapping = load_mapping()
        self.boundary = load_boundary()
        self.settings = self.trade.LocalMcpTradeSettings(
            profile_id="profile-a",
            profile_name="main",
            spot_base_url="https://api-spot.weex.com",
            futures_base_url="https://api-contract.weex.com",
            timeout_seconds=15.0,
            locale="zh-CN",
        )

    @contextmanager
    def facade(
        self,
        gateway: FakeGateway,
    ):
        kernel = MemoryIntentKernel()
        ids = iter(("stable-id-1", "stable-id-2", "stable-id-3"))

        def gateway_factory(**kwargs):
            gateway.init_kwargs = dict(kwargs)
            return gateway

        if gateway.on_call is None:
            def update_durable_state():
                payload = gateway.responses[0] if gateway.responses else {}
                outcome = payload.get("outcome") if isinstance(payload, dict) else None
                outcome_class = (
                    outcome.get("class") if isinstance(outcome, dict) else None
                )
                kernel.set_state(
                    {
                        "accepted": "ACCEPTED",
                        "definitive_rejection": "DEFINITIVE_REJECTION",
                        "not_sent": "NOT_SENT_TERMINAL",
                        "unknown": "REVIEW_REQUIRED",
                        "partial": "REVIEW_REQUIRED",
                        "invocation_rejected": "CONFIRMED",
                    }.get(outcome_class, "CLAIMED")
                )

            gateway.on_call = update_durable_state

        with (
            mock.patch.object(self.trade, "load_manual_intent", side_effect=kernel.load),
            mock.patch.object(
                self.trade,
                "confirm_and_issue_intent",
                side_effect=kernel.confirm,
            ),
            mock.patch.object(
                self.trade,
                "execution_grant_for_dispatch",
                side_effect=kernel.grant_for_dispatch,
            ),
            mock.patch.object(
                self.trade,
                "claimed_execution_grant_for_recovery",
                side_effect=kernel.grant_for_dispatch,
            ),
        ):
            yield (
                self.trade.McpTradeFacade(
                    settings=self.settings,
                    gateway_factory=gateway_factory,
                    mapping_loader=lambda: copy.deepcopy(self.mapping),
                    boundary_contract_loader=lambda: copy.deepcopy(self.boundary),
                    client_id_factory=lambda: next(ids),
                    intent_id_factory=lambda: "intent-1",
                    intent_store_factory=lambda: kernel.store_marker,
                    intent_saver=kernel.save,
                    clock=lambda: 200,
                ),
                kernel,
            )

    def preview(
        self,
        facade,
        legacy_key: str,
        body: dict[str, object],
        *,
        product: str = "contract",
        query: dict[str, object] | None = None,
    ):
        entry = mapping_entry(self.mapping, legacy_key)
        return facade.preview(
            product=product,
            endpoint=endpoint_from_mapping(entry),
            query=query or {},
            body=body,
        )

    def test_describe_uses_direct_tool_without_gateway_or_intent(self) -> None:
        entry = mapping_entry(self.mapping, "spot.order.place_order")
        calls = []
        facade = self.trade.McpTradeFacade(
            settings=self.settings,
            gateway_factory=lambda **_kwargs: self.fail(
                "describe must not construct a gateway"
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
            boundary_contract_loader=lambda: copy.deepcopy(self.boundary),
            client_id_factory=lambda: "stable-id",
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
        self.assertEqual(calls[0]["name"], "trade_place_order")
        self.assertEqual(calls[0]["arguments"]["client_order_id"], "stable-id")

    def test_preview_prepares_direct_and_persists_complete_unclaimed_intent(self) -> None:
        gateway = FakeGateway([])
        with self.facade(gateway) as (facade, kernel):
            result = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(gateway.prepares[0][0], "trade_place_order")
        self.assertEqual(gateway.calls, [])
        self.assertEqual(kernel.intent["intent_version"], 2)
        self.assertEqual(kernel.intent["state"], "UNCLAIMED")
        self.assertEqual(
            kernel.intent["prepared_mutation"],
            result["preview"]["prepared_mutation"],
        )
        self.assertEqual(
            result["risk_signature"],
            kernel.intent["prepared_artifact_digest"],
        )
        self.assertIs(gateway.init_kwargs["grant_consumer"], kernel.store_marker)

    def test_spot_batch_maps_once_with_stable_ids_and_symbol_fanout(self) -> None:
        gateway = FakeGateway([])
        with self.facade(gateway) as (facade, _kernel):
            result = self.preview(
                facade,
                "spot.order.bulk_order",
                {
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
                product="spot",
            )
        self.assertTrue(result["ok"])
        name, arguments = gateway.prepares[0]
        self.assertEqual(name, "trade_place_batch_orders")
        self.assertEqual(
            arguments["orders"],
            [
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
        )

    def test_direct_argument_derivations_remove_all_public_acknowledgements(self) -> None:
        cases = (
            (
                "account.change_margin_mode_trade",
                {
                    "symbol": "BTCUSDT",
                    "marginType": "CROSSED",
                    "separatedType": "COMBINED",
                },
                "trade_set_margin_mode",
                {
                    "margin_mode": "CROSS",
                    "separated_type": "COMBINED",
                },
            ),
            (
                "account.update_leverage_trade",
                {
                    "symbol": "BTCUSDT",
                    "marginType": "ISOLATED",
                    "isolatedLongLeverage": 5,
                    "isolatedShortLeverage": 6,
                },
                "trade_update_leverage",
                {
                    "margin_mode": "ISOLATED",
                    "isolated_long_leverage": "5",
                    "isolated_short_leverage": "6",
                },
            ),
            (
                "account.adjust_position_margin_trade",
                {"isolatedPositionId": 7, "amount": 10, "type": 1},
                "trade_adjust_position_margin",
                {"isolated_position_id": "7", "amount": "10", "adjustment": "increase"},
            ),
            (
                "transaction.close_positions",
                {},
                "trade_close_positions",
                {"scope": "all"},
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
                "trade_place_tp_sl",
                {"quantity_mode": "full_position"},
            ),
            (
                "transaction.modify_tp_sl_order",
                {
                    "orderId": 7,
                    "triggerPrice": 100,
                    "executePrice": 0,
                    "triggerPriceType": "MARK_PRICE",
                },
                "trade_modify_tp_sl",
                {"order_id": "7", "trigger_price": "100"},
            ),
        )
        for legacy_key, body, expected_tool, expected in cases:
            with self.subTest(legacy_key=legacy_key):
                gateway = FakeGateway([])
                with self.facade(gateway) as (facade, _kernel):
                    result = self.preview(facade, legacy_key, body)
                self.assertTrue(result["ok"], result)
                name, arguments = gateway.prepares[0]
                self.assertEqual(name, expected_tool)
                for key, value in expected.items():
                    self.assertEqual(arguments[key], value)
                serialized = json.dumps(arguments, sort_keys=True)
                self.assertNotIn("acknowledge", serialized)
                self.assertNotIn("confirmation", serialized)
                self.assertNotIn("delegated_authorization", serialized)

    def test_confirm_issues_grant_then_dispatches_direct_with_typed_result(self) -> None:
        gateway = FakeGateway(
            [
                {
                    "ok": True,
                    "result": {
                        "request_identity": {
                            "client_order_id": "stable-id-1"
                        },
                        "status": "accepted",
                    },
                    "outcome": {
                        "class": "accepted",
                        "write_attempted": True,
                        "grant_disposition": "accepted",
                    },
                }
            ],
            transport_metas=[
                {
                    "version": 1,
                    "request_id": "direct",
                    "attempt_count": 1,
                    "http_status": 202,
                }
            ],
        )
        with self.facade(gateway) as (facade, kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            confirmed = facade.confirm_intent(
                intent_id=preview["intent_id"],
                prepared_artifact_digest=preview["risk_signature"],
            )
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["status"], 202)
        self.assertEqual(
            confirmed["data"]["request_identity"]["client_order_id"],
            "stable-id-1",
        )
        self.assertNotIn("raw", confirmed["data"])
        self.assertEqual(gateway.calls[-1][0], "trade_place_order")
        self.assertIs(gateway.execution_grants[-1], kernel.grant)
        self.assertFalse(
            {
                "preview_id",
                "confirmation",
                "delegated_authorization",
                "acknowledge",
            }
            & set(gateway.calls[-1][1])
        )

    def test_new_facade_process_can_confirm_using_only_intent_id_and_digest(self) -> None:
        gateway = FakeGateway(
            [
                {
                    "ok": True,
                    "result": {"status": "accepted"},
                    "outcome": {
                        "class": "accepted",
                        "write_attempted": True,
                        "grant_disposition": "accepted",
                    },
                }
            ]
        )
        with self.facade(gateway) as (first, kernel):
            preview = self.preview(
                first,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            second = self.trade.McpTradeFacade(
                settings=self.settings,
                gateway_factory=lambda **kwargs: gateway,
                mapping_loader=lambda: copy.deepcopy(self.mapping),
                boundary_contract_loader=lambda: copy.deepcopy(self.boundary),
                intent_store_factory=lambda: kernel.store_marker,
                clock=lambda: 200,
            )
            result = second.confirm_intent(
                intent_id=preview["intent_id"],
                prepared_artifact_digest=preview["risk_signature"],
            )
        self.assertTrue(result["ok"])
        self.assertEqual(gateway.calls[-1][0], "trade_place_order")

    def test_direct_outcome_classes_keep_review_and_not_sent_boundaries(self) -> None:
        cases = (
            ("unknown", True, True, "REVIEW_REQUIRED"),
            ("partial", True, True, "REVIEW_REQUIRED"),
            ("definitive_rejection", True, False, "REJECTED"),
            ("not_sent", False, False, "NOT_SENT"),
            ("invocation_rejected", False, False, "NOT_SENT"),
        )
        for outcome_class, attempted, review, trade_status in cases:
            with self.subTest(outcome_class=outcome_class):
                gateway = FakeGateway(
                    [
                        {
                            "ok": False,
                            "error": {
                                "code": "typed_failure",
                                "message": "failed",
                            },
                            "outcome": {
                                "class": outcome_class,
                                "write_attempted": attempted,
                                "grant_disposition": (
                                    "review_required"
                                    if review
                                    else (
                                        "definitive_rejection"
                                        if outcome_class == "definitive_rejection"
                                        else (
                                            "unchanged"
                                            if outcome_class == "invocation_rejected"
                                            else "not_sent_terminal"
                                        )
                                    )
                                ),
                            },
                        }
                    ]
                )
                with self.facade(gateway) as (facade, _kernel):
                    preview = self.preview(
                        facade,
                        "spot.order.cancel_order",
                        {},
                        product="spot",
                        query={"orderId": 7},
                    )
                    result = facade.confirm_intent(
                        intent_id=preview["intent_id"],
                        prepared_artifact_digest=preview["risk_signature"],
                    )
                self.assertFalse(result["ok"])
                self.assertEqual(result["review_required"], review)
                self.assertEqual(result["trade_status"], trade_status)
                if review or outcome_class == "invocation_rejected":
                    self.assertEqual(result["intent_id"], "intent-1")
                    self.assertEqual(
                        result["risk_signature"],
                        "sha256:" + "6" * 64,
                    )

    def test_malformed_wrapper_uses_durable_claimed_state_instead_of_guessing(self) -> None:
        gateway = FakeGateway([{"ok": True}], on_call=lambda: None)
        with self.facade(gateway) as (facade, kernel):
            gateway.on_call = lambda: kernel.set_state("CLAIMED")
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            result = facade.confirm_intent(
                intent_id=preview["intent_id"],
                prepared_artifact_digest=preview["risk_signature"],
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["review_required"])
        self.assertEqual(result["trade_status"], "REVIEW_REQUIRED")
        self.assertEqual(result["outcome"]["class"], "unknown")
        self.assertEqual(
            result["outcome"]["grant_disposition"],
            "review_required",
        )

    def test_malformed_typed_payload_preserves_durable_accepted_truth(self) -> None:
        gateway = FakeGateway(
            [{"ok": True, "result": {"status": "accepted"}}]
        )
        with self.facade(gateway) as (facade, kernel):
            gateway.on_call = lambda: kernel.set_state("ACCEPTED")
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            result = facade.confirm_intent(
                intent_id=preview["intent_id"],
                prepared_artifact_digest=preview["risk_signature"],
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["trade_status"], "ACCEPTED")
        self.assertEqual(
            result["warning"]["code"],
            "internal_projection_error",
        )
        self.assertEqual(
            result["warning"]["message"],
            "Accepted, but the full result projection was unavailable.",
        )

    def test_confirm_keeps_accepted_outcome_when_another_preview_replaces_the_slot(self) -> None:
        gateway = FakeGateway([
            {
                "ok": True,
                "result": {"status": "accepted"},
                "outcome": {"class": "accepted", "write_attempted": True, "grant_disposition": "accepted"},
            }
        ])
        with self.facade(gateway) as (facade, kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"},
                product="spot",
            )

            def finalize_and_replace():
                archived = copy.deepcopy(kernel.intent)
                archived["state"] = "ACCEPTED"
                kernel.archived[archived["intent_id"]] = archived
                kernel.intent = {
                    "intent_id": "intent-2",
                    "state": "UNCLAIMED",
                    "prepared_artifact_digest": "sha256:" + "7" * 64,
                    "expires_at": 10_000,
                }

            gateway.on_call = finalize_and_replace
            result = facade.confirm_intent(
                intent_id=preview["intent_id"],
                prepared_artifact_digest=preview["risk_signature"],
            )

        self.assertEqual(result["trade_status"], "ACCEPTED")
        self.assertEqual(result["data"], {"status": "accepted"})

    def test_invocation_rejected_envelope_cannot_override_durable_not_sent(self) -> None:
        gateway = FakeGateway(
            [
                {
                    "ok": False,
                    "error": {"code": "rejected", "message": "rejected"},
                    "outcome": {
                        "class": "invocation_rejected",
                        "write_attempted": False,
                        "grant_disposition": "unchanged",
                    },
                }
            ]
        )
        with self.facade(gateway) as (facade, kernel):
            gateway.on_call = lambda: kernel.set_state("NOT_SENT_TERMINAL")
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            result = facade.confirm_intent(
                intent_id=preview["intent_id"],
                prepared_artifact_digest=preview["risk_signature"],
            )
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["outcome"]["class"],
            "not_sent",
        )
        self.assertEqual(
            result["outcome"]["grant_disposition"],
            "not_sent_terminal",
        )

    def test_preview_binding_mismatch_stops_before_dispatch(self) -> None:
        gateway = FakeGateway([{"ok": True, "data": []}])
        with self.facade(gateway) as (facade, _kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            tampered = {
                **preview["preview"],
                "prepared_artifact_digest": "sha256:" + "0" * 64,
            }
            with self.assertRaises(self.trade.McpTradeCompatibilityError):
                facade.confirm_intent(
                    intent_id=tampered["intent_id"],
                    prepared_artifact_digest=tampered["prepared_artifact_digest"],
                )
        self.assertEqual(gateway.calls, [])

    def test_preview_record_confirm_compatibility_method_is_fail_closed(self) -> None:
        gateway = FakeGateway([{"ok": True, "data": []}])
        with self.facade(gateway) as (facade, kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            result = facade.confirm(preview["preview"])

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "two_phase_confirmation_required")
        self.assertEqual(kernel.intent["state"], "UNCLAIMED")
        self.assertEqual(gateway.calls, [])

    def test_preview_and_confirm_compatibility_method_never_dispatches(self) -> None:
        gateway = FakeGateway([{"ok": True, "data": []}])
        with self.facade(gateway) as (facade, kernel):
            result = facade.preview_and_confirm(
                product="spot",
                endpoint=endpoint_from_mapping(
                    mapping_entry(self.mapping, "spot.order.place_order")
                ),
                query={},
                body={
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "two_phase_confirmation_required")
        self.assertEqual(kernel.intent["state"], "UNCLAIMED")
        self.assertEqual(len(gateway.prepares), 1)
        self.assertEqual(gateway.calls, [])

    def test_confirm_projects_gateway_construction_failure_against_durable_state(self) -> None:
        gateway = FakeGateway([{"ok": True, "data": []}])
        with self.facade(gateway) as (facade, _kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            facade._gateway = None

            def unavailable_gateway(**_kwargs):
                raise self.trade.McpGatewayError(
                    "mcp_preflight_failed",
                    "injected gateway construction failure",
                )

            facade._gateway_factory = unavailable_gateway
            result = facade.confirm_intent(
                intent_id=preview["intent_id"],
                prepared_artifact_digest=preview["risk_signature"],
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"]["class"], "invocation_rejected")
        self.assertEqual(result["outcome"]["grant_disposition"], "unchanged")
        self.assertEqual(result["intent_id"], preview["intent_id"])

    def test_delegated_path_is_stably_not_sent_until_t13b(self) -> None:
        parameters = inspect.signature(self.trade.McpTradeFacade).parameters
        self.assertNotIn("delegated_authorizer", parameters)
        self.assertFalse(hasattr(self.trade.McpTradeFacade, "confirm_delegated"))

    def test_prepare_error_is_not_sent_and_does_not_persist_intent(self) -> None:
        trade = self.trade
        gateway = FakeGateway(
            prepare_error=trade.McpGatewayError(
                "stale_precondition",
                "stale",
            )
        )
        with self.facade(gateway) as (facade, kernel):
            result = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["trade_status"], "NOT_SENT")
        self.assertIsNone(kernel.intent)

    def test_reconcile_consumes_stored_prepared_mutation_and_preserves_mcp_digest(self) -> None:
        gateway = FakeGateway([{"ok": True, "data": []}])
        gateway.on_call = lambda: None
        gateway.reconciliation_status = "RESOLVED_ACCEPTED"
        with self.facade(gateway) as (facade, kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            kernel.intent["state"] = "REVIEW_REQUIRED"
            kernel.intent["reconciliation"]["status"] = "PENDING"
            kernel.intent["reconciliation"]["template"] = {
                "strategy": "query_exact_identity",
                "read_tool": "account_get_orders",
                "arguments": {
                    "market": "spot",
                    "trading_mode": "live",
                    "status": "single",
                    "client_order_id": "stable-id-1",
                },
                "expected": {"client_order_id": "stable-id-1"},
                "identity": {"client_order_id": "stable-id-1"},
                "completeness": "deterministic",
            }
            result = facade.reconcile_intent(preview["intent_id"])
        self.assertTrue(result["ok"])
        self.assertFalse(result["review_required"])
        self.assertEqual(
            kernel.reconciliation_calls[-1]["evidence_digest"],
            "sha256:" + "9" * 64,
        )
        self.assertEqual(len(gateway.reconciliations), 1)
        self.assertEqual(gateway.reconciliations[0][0], kernel.intent["prepared_mutation"])
        self.assertEqual(gateway.calls, [])
        self.assertEqual(
            tuple(inspect.signature(facade.reconcile_intent).parameters),
            ("intent_id",),
        )

    def test_reconcile_resolved_intent_is_read_only_and_does_not_query_again(self) -> None:
        gateway = FakeGateway([{"ok": True, "result": []}])
        with self.facade(gateway) as (facade, kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            kernel.intent["state"] = "REVIEW_REQUIRED"
            kernel.intent["reconciliation"].update(
                {
                    "status": "RESOLVED_ACCEPTED",
                    "result": "ACCEPTED",
                    "observed_at": 150,
                    "evidence_digest": "sha256:" + "8" * 64,
                }
            )
            result = facade.reconcile_intent(preview["intent_id"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["trade_status"], "ACCEPTED")
        self.assertEqual(gateway.calls, [])
        self.assertEqual(kernel.reconciliation_calls, [])

    def test_reconcile_per_item_remains_blocked_when_mcp_reports_unresolved(self) -> None:
        gateway = FakeGateway(
            [
                {
                    "ok": True,
                    "result": [
                        {
                            "symbol": "BTCUSDT",
                            "request_identity": {"client_order_id": "client-1"},
                        }
                    ],
                },
                {
                    "ok": True,
                    "result": [
                        {
                            "symbol": "BTCUSDT",
                            "request_identity": {"client_order_id": "client-2"},
                        }
                    ],
                },
            ]
        )
        with self.facade(gateway) as (facade, kernel):
            preview = self.preview(
                facade,
                "spot.order.bulk_order",
                {
                    "symbol": "BTCUSDT",
                    "orderList": [
                        {
                            "side": "BUY",
                            "type": "MARKET",
                            "quantity": "1",
                            "newClientOrderId": "client-1",
                        },
                        {
                            "side": "BUY",
                            "type": "MARKET",
                            "quantity": "1",
                            "newClientOrderId": "client-2",
                        },
                    ]
                },
                product="spot",
            )
            kernel.intent["state"] = "REVIEW_REQUIRED"
            kernel.intent["reconciliation"]["status"] = "PENDING"
            kernel.intent["reconciliation"]["template"] = {
                "strategy": "per_item",
                "completeness": "partial",
                "items": [
                    {
                        "index": index,
                        "identity": {"client_order_id": client_id},
                        "reconciliation": {
                            "strategy": "query_exact_identity",
                            "read_tool": "account_get_orders",
                            "arguments": {
                                "market": "spot",
                                "trading_mode": "live",
                                "status": "single",
                                "client_order_id": client_id,
                            },
                            "expected": {
                                "symbol": "BTCUSDT",
                                "client_order_id": client_id,
                            },
                            "identity": {"client_order_id": client_id},
                            "completeness": "deterministic",
                        },
                    }
                    for index, client_id in enumerate(("client-1", "client-2"))
                ],
            }
            result = facade.reconcile_intent(preview["intent_id"])

        self.assertFalse(result["ok"])
        self.assertTrue(result["review_required"])
        self.assertEqual(gateway.calls, [])
        self.assertEqual(len(gateway.reconciliations), 1)
        self.assertEqual(kernel.reconciliation_calls[-1]["status"], "UNRESOLVED")

    def test_reconcile_projects_gateway_construction_failure_without_raising(self) -> None:
        gateway = FakeGateway([])
        with self.facade(gateway) as (facade, kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            kernel.intent["state"] = "REVIEW_REQUIRED"
            kernel.intent["reconciliation"]["status"] = "PENDING"
            facade._gateway = None

            def unavailable_gateway(**_kwargs):
                raise self.trade.McpGatewayError(
                    "mcp_preflight_failed",
                    "injected gateway construction failure",
                )

            facade._gateway_factory = unavailable_gateway
            result = facade.reconcile_intent(preview["intent_id"])

        self.assertFalse(result["ok"])
        self.assertTrue(result["review_required"])
        self.assertEqual(kernel.reconciliation_calls, [])

    def test_reconcile_recovers_claimed_grant_through_gateway_before_read(self) -> None:
        gateway = FakeGateway([{"ok": True, "data": []}])
        gateway.on_call = lambda: None
        with self.facade(gateway) as (facade, kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "1",
                },
                product="spot",
            )
            kernel.grant = SimpleNamespace(state="CLAIMED")
            kernel.intent["state"] = "CLAIMED"
            original_load = kernel.load

            def load_after_recovery(**kwargs):
                current = original_load(**kwargs)
                if gateway.recoveries:
                    current["state"] = "REVIEW_REQUIRED"
                    current["reconciliation"]["status"] = "PENDING"
                    kernel.intent = copy.deepcopy(current)
                return current

            with mock.patch.object(
                self.trade,
                "load_manual_intent",
                side_effect=load_after_recovery,
            ):
                result = facade.reconcile_intent(preview["intent_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(len(gateway.recoveries), 1)
        self.assertEqual(gateway.recoveries[0][0], "trade_place_order")
        self.assertEqual(len(gateway.reconciliations), 1)

    def test_reconciliation_refuses_a_claim_while_direct_dispatch_is_in_flight(self) -> None:
        dispatch_gateway = FakeGateway([
            {
                "ok": True,
                "result": {"status": "accepted"},
                "outcome": {"class": "accepted", "write_attempted": True, "grant_disposition": "accepted"},
            }
        ])
        reconcile_gateway = FakeGateway([])
        reconcile_gateway.reconciliation_status = "RESOLVED_ACCEPTED"
        with self.facade(dispatch_gateway) as (dispatch_facade, kernel):
            reconcile_facade = self.trade.McpTradeFacade(
                settings=self.settings,
                gateway_factory=lambda **_kwargs: reconcile_gateway,
                intent_store_factory=lambda: kernel,
                clock=lambda: 200,
            )
            preview = self.preview(
                dispatch_facade,
                "spot.order.place_order",
                {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"},
                product="spot",
            )
            reconciliations = []
            recover_mutation = reconcile_gateway.recover_mutation

            def simulated_recovery(**kwargs):
                recovery = recover_mutation(**kwargs)
                kernel.set_state("REVIEW_REQUIRED")
                kernel.intent["reconciliation"]["status"] = "PENDING"
                return recovery

            def reconcile_before_the_write_returns():
                kernel.set_state("CLAIMED")
                kernel.grant.state = "CLAIMED"
                reconciliations.append(reconcile_facade.reconcile_intent(preview["intent_id"]))
                self.assertEqual(kernel.intent["state"], "CLAIMED")
                kernel.set_state("ACCEPTED")

            reconcile_gateway.recover_mutation = simulated_recovery
            dispatch_gateway.on_call = reconcile_before_the_write_returns
            confirmed = dispatch_facade.confirm_intent(
                intent_id=preview["intent_id"],
                prepared_artifact_digest=preview["risk_signature"],
            )

        self.assertEqual(confirmed["trade_status"], "ACCEPTED")
        self.assertEqual(reconciliations[0]["trade_status"], "REVIEW_REQUIRED")
        self.assertTrue(reconciliations[0]["review_required"])
        self.assertEqual(reconcile_gateway.recoveries, [])
        self.assertEqual(reconcile_gateway.reconciliations, [])
        self.assertEqual(len(dispatch_gateway.calls), 1)

    def test_concurrent_confirmation_preserves_the_original_in_flight_claim(self) -> None:
        gateway = FakeGateway([
            {
                "ok": True,
                "result": {"status": "accepted"},
                "outcome": {"class": "accepted", "write_attempted": True, "grant_disposition": "accepted"},
            }
        ])
        with self.facade(gateway) as (facade, kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"},
                product="spot",
            )
            confirmed = []

            def claim_before_the_second_confirmation_reads_the_grant(*_args, **_kwargs):
                kernel.set_state("CLAIMED")
                raise self.trade.IntentStateError("grant_unavailable", "Issued grant was already claimed.")

            def confirm_again_while_the_first_call_is_active():
                with mock.patch.object(
                    self.trade,
                    "execution_grant_for_dispatch",
                    side_effect=claim_before_the_second_confirmation_reads_the_grant,
                ):
                    confirmed.append(facade.confirm_intent(
                        intent_id=preview["intent_id"],
                        prepared_artifact_digest=preview["risk_signature"],
                    ))
                kernel.set_state("ACCEPTED")

            gateway.on_call = confirm_again_while_the_first_call_is_active
            result = facade.confirm_intent(
                intent_id=preview["intent_id"],
                prepared_artifact_digest=preview["risk_signature"],
            )

        self.assertEqual(result["trade_status"], "ACCEPTED")
        self.assertEqual(confirmed[0]["trade_status"], "REVIEW_REQUIRED")
        self.assertEqual(confirmed[0]["intent_id"], preview["intent_id"])
        self.assertEqual(len(gateway.calls), 1)

    def test_recovery_error_does_not_report_the_replacement_intents_outcome(self) -> None:
        gateway = FakeGateway([])
        with self.facade(gateway) as (facade, kernel):
            preview = self.preview(
                facade,
                "spot.order.place_order",
                {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"},
                product="spot",
            )
            kernel.grant = SimpleNamespace(state="CLAIMED")
            kernel.intent["state"] = "CLAIMED"

            def fail_after_replacement(**_kwargs):
                archived = copy.deepcopy(kernel.intent)
                archived["state"] = "DEFINITIVE_REJECTION"
                kernel.archived[archived["intent_id"]] = archived
                kernel.intent = {
                    "intent_id": "intent-2",
                    "state": "ACCEPTED",
                    "prepared_artifact_digest": "sha256:" + "7" * 64,
                    "expires_at": 10_000,
                }
                raise self.trade.McpGatewayError("mcp_protocol_error", "injected recovery conflict")

            gateway.recover_mutation = fail_after_replacement
            result = facade.reconcile_intent(preview["intent_id"])

        self.assertEqual(result["trade_status"], "REJECTED")
        self.assertEqual(gateway.reconciliations, [])


if __name__ == "__main__":
    unittest.main()
