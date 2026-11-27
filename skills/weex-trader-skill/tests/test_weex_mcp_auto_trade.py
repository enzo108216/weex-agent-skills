#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "weex_mcp_auto_trade.py"
MAPPING_PATH = ROOT / "references" / "mcp-capability-mapping.json"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("weex_mcp_auto_trade", MODULE_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AutoTradeDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auto = load_module()
        self.assertIsNotNone(
            self.auto,
            "weex_mcp_auto_trade.py is missing",
        )
        self.state = mock.Mock()
        self.state.read_delegated_submission_group.return_value = {
            "authorization_id": "auth-1",
            "strategy_id": "strategy-1",
            "profile_id": "profile-1",
            "scope_signature": "scope-signature",
            "expires_at": 5000,
            "submission_group_id": "group-1",
            "usage_count": 1,
            "usage_ids": ["usage-1"],
            "client_order_ids": ["client-1"],
        }
        self.request_orders = [
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quantity": "1",
                "newClientOrderId": "client-1",
            }
        ]
        self.request_fingerprint = self.auto._request_fingerprint(
            "spot.order.place_order",
            self.request_orders,
        )
        self.delegation = self.auto.AutoTradeDelegation(
            state=self.state,
            authorization_id="auth-1",
            strategy_id="strategy-1",
            submission_group_id="group-1",
            request_fingerprint=self.request_fingerprint,
            usage_ids=["usage-1"],
            client_order_ids=["client-1"],
            expected_orders=self.request_orders,
            operation_key="spot.order.place_order",
            now_epoch=lambda: 1000,
        )

        from weex_spot_api import ENDPOINTS
        from weex_mcp_trade_compat import LocalMcpTradeSettings

        self.settings = LocalMcpTradeSettings(
            profile_id="profile-1",
            spot_base_url="https://api-spot.weex.com",
            futures_base_url="https://api-contract.weex.com",
            timeout_seconds=5,
            locale="zh-CN",
        )
        self.endpoint = ENDPOINTS["spot.order.place_order"]
        self.arguments = {
            "market": "spot", "trading_mode": "live", "symbol": "BTCUSDT",
            "side": "BUY", "order_type": "MARKET", "quantity": "1",
            "client_order_id": "client-1",
        }
        self.prepared = {
            "tool_name": "trade_place_order", "canonical_arguments": self.arguments,
            "market": "spot", "trading_mode": "live",
        }
        self.response = {
            "action": "place_order", "ok": True,
            "outcome": {"class": "accepted", "write_attempted": True, "grant_disposition": "accepted"},
        }
        self.gateway = mock.Mock()
        self.gateway.prepare_mutation.return_value = self.prepared
        self.gateway.build_execution_grant.return_value = mock.sentinel.generated_grant
        self.gateway.call_typed_tool.return_value = {
            "structured_content": self.response, "is_error": False, "transport_meta": None,
        }
        self.state.issue_execution_grant.return_value = mock.sentinel.issued_grant
        self.state.execution_grant_outcome_matches.return_value = True

    def execute(self, body=None):
        with mock.patch.object(self.auto, "LocalMcpGateway", return_value=self.gateway) as gateway_factory:
            result = self.delegation.execute(
                settings=self.settings, endpoint=self.endpoint,
                query={}, body=self.request_orders[0] if body is None else body,
            )
        self.assertIs(gateway_factory.call_args.kwargs["grant_consumer"], self.state)
        return result

    def test_executes_durable_grant_after_prepare_and_exposes_only_bound_outcome(self) -> None:
        calls = mock.Mock()
        calls.attach_mock(self.gateway, "gateway")
        calls.attach_mock(self.state, "state")
        self.assertEqual(self.execute(), self.response)
        self.assertEqual(
            [call[0] for call in calls.mock_calls],
            ["state.read_delegated_submission_group", "gateway.prepare_mutation",
             "gateway.build_execution_grant", "state.issue_execution_grant",
             "gateway.call_typed_tool", "state.execution_grant_outcome_matches"],
        )
        self.gateway.call_typed_tool.assert_called_once_with(
            "trade_place_order", self.arguments, execution_grant=mock.sentinel.issued_grant,
        )
        self.assertEqual(self.state.issue_execution_grant.call_args.kwargs["prepared"], self.prepared)
        self.state.execution_grant_outcome_matches.assert_called_once_with(
            submission_group_id="group-1", grant=mock.sentinel.issued_grant,
            outcome=self.response["outcome"],
        )

    def test_rejects_wrong_body_or_prepared_id_before_issue(self) -> None:
        for body, prepared in (
            ({**self.request_orders[0], "quantity": "2"}, self.prepared),
            (self.request_orders[0], {**self.prepared, "canonical_arguments": {**self.arguments, "client_order_id": "wrong"}}),
            (self.request_orders[0], {**self.prepared, "canonical_arguments": {**self.arguments, "quantity": "2"}}),
        ):
            with self.subTest(body=body, prepared=prepared):
                self.gateway.prepare_mutation.return_value = prepared
                with mock.patch.object(self.auto, "LocalMcpGateway", return_value=self.gateway):
                    with self.assertRaises(self.auto.AutoTradeDelegationError):
                        self.delegation.execute(settings=self.settings, endpoint=self.endpoint, query={}, body=body)
        self.state.issue_execution_grant.assert_not_called()
        self.gateway.call_typed_tool.assert_not_called()

    def test_unmatched_aggregate_digest_never_yields_leg_evidence(self) -> None:
        self.state.execution_grant_outcome_matches.return_value = False
        with self.assertRaises(self.auto.AutoTradeDelegationError):
            self.execute()
        self.gateway.call_typed_tool.assert_called_once()

    def test_t8_auto_delegated_gate_is_frozen_without_monitor_delegation(self) -> None:
        mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            mapping["compatibility_gates"]["t8_auto_delegated"],
            {
                "status": "golden_verified",
                "operations": 6,
                "manual_trade_matrix_sha256": "d707c666dda2bbe315e68319b336bedd79505f5f76ca2a8758182e446762ff24",
                "tests": [
                    "tests/test_weex_mcp_auto_trade.py",
                    "tests/test_weex_auto_delegated_state.py",
                ],
                "direct_rest_fallback": False,
                "monitor_delegated_authorization": False,
            },
        )

    def test_constructor_rejects_orders_not_matching_persisted_fingerprint(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserved request fingerprint"):
            self.auto.AutoTradeDelegation(
                state=self.state,
                authorization_id="auth-1",
                strategy_id="strategy-1",
                submission_group_id="group-1",
                request_fingerprint=self.request_fingerprint,
                usage_ids=["usage-1"],
                client_order_ids=["client-1"],
                expected_orders=[
                    {
                        **self.request_orders[0],
                        "quantity": "2",
                    }
                ],
                operation_key="spot.order.place_order",
            )


class AutoOutcomeMappingTests(unittest.TestCase):
    def test_fixed_mcp_partial_projects_bound_rejection_without_inventing_order_id(self) -> None:
        import weex_auto_trade_runtime as runtime

        result = runtime._normalize_mcp_submission_results(
            operation_key="spot.order.bulk_order",
            prepared_legs=[
                {"leg_id": "leg-0", "client_order_id": "client-1"},
                {"leg_id": "leg-1", "client_order_id": "client-2"},
            ],
            response={
                "action": "place_batch_orders",
                "ok": False,
                "outcome": {"class": "partial", "write_attempted": True, "grant_disposition": "review_required"},
                "error": {"code": "partial_write", "message": "Partial.", "category": "transport", "retryable": False},
                "reconciliation": {
                    "strategy": "per_item",
                    "completeness": "partial",
                    "items": [
                        {
                            "index": index,
                            "identity": {"client_order_id": client_id},
                            "reconciliation": {
                                "strategy": "query_exact_identity",
                                "read_tool": "account_get_orders",
                                "arguments": {"market": "spot", "trading_mode": "live", "symbol": "BTCUSDT", "client_order_id": client_id},
                                "expected": {"symbol": "BTCUSDT", "client_order_id": client_id},
                                "identity": {"client_order_id": client_id},
                                "completeness": "deterministic",
                            },
                        }
                        for index, client_id in enumerate(("client-1", "client-2"))
                    ],
                },
                "result": {
                    "items": [
                        {
                            "index": 0,
                            "request_identity": {"client_order_id": "client-1"},
                            "outcome": {"class": "accepted", "write_attempted": True},
                            "status": "accepted",
                        },
                        {
                            "index": 1,
                            "request_identity": {"client_order_id": "client-2"},
                            "outcome": {"class": "definitive_rejection", "write_attempted": True},
                            "status": "definitive_rejection",
                            "error": {"code": "upstream_rejection", "message": "Rejected.", "category": "upstream_rejection", "retryable": False},
                        },
                    ],
                    "counts": {"accepted": 1, "rejected": 1, "unknown": 0},
                },
            },
        )
        self.assertEqual([leg["status"] for leg in result], ["REVIEW_REQUIRED", "RELEASED"])
        self.assertNotIn("weex_order_id", result[0])
        self.assertEqual(result[1]["client_order_id"], "client-2")
        self.assertEqual(result[1]["error_code"], "upstream_rejection")

    def test_fixed_mcp_not_sent_proof_releases_exact_bound_group(self) -> None:
        import weex_auto_trade_runtime as runtime

        result = runtime._normalize_mcp_submission_results(
            operation_key="transaction.place_orders_batch",
            prepared_legs=[
                {"leg_id": "leg-0", "client_order_id": "client-1"},
                {"leg_id": "leg-1", "client_order_id": "client-2"},
            ],
            response={
                "action": "place_batch_orders",
                "ok": False,
                "outcome": {"class": "not_sent", "write_attempted": False, "grant_disposition": "not_sent_terminal"},
                "error": {"code": "stale_precondition", "message": "Not sent.", "category": "stale_precondition", "retryable": False},
            },
        )
        self.assertEqual([leg["status"] for leg in result], ["RELEASED", "RELEASED"])
        self.assertEqual([leg["client_order_id"] for leg in result], ["client-1", "client-2"])

    def test_fixed_mcp_issued_invocation_rejected_is_not_a_release_proof(self) -> None:
        import weex_auto_trade_runtime as runtime

        response = {
            "action": "place_order",
            "ok": False,
            "outcome": {"class": "invocation_rejected", "write_attempted": False, "grant_disposition": "unchanged"},
            "error": {"code": "execution_not_authorized", "message": "Rejected.", "category": "authorization", "retryable": False},
        }
        malformed = {
            **response,
            "outcome": {**response["outcome"], "action": "order"},
            "review_required": False,
        }
        for value in (response, malformed):
            with self.subTest(response=value):
                result = runtime._normalize_mcp_submission_results(
                    operation_key="spot.order.place_order",
                    prepared_legs=[{"leg_id": "leg-0", "client_order_id": "client-1"}],
                    response=value,
                )
                self.assertEqual(result[0]["status"], "REVIEW_REQUIRED")

    def test_fixed_mcp_boundary_uses_durable_delegate_without_legacy_write(self) -> None:
        import weex_auto_trade_runtime as runtime

        endpoint = mock.Mock(mutating=True, key="transaction.place_order")
        api_module = mock.Mock(ENDPOINTS={endpoint.key: endpoint})
        client = mock.Mock()
        settings = client.mcp_trade_settings_factory.return_value
        body = {"symbol": "BTCUSDT", "newClientOrderId": "client-1"}
        response = {
            "action": "place_order",
            "ok": True,
            "outcome": {"class": "accepted", "write_attempted": True, "grant_disposition": "accepted"},
            "result": {"status": "accepted", "request_identity": {"client_order_id": "client-1"}, "symbol": "BTCUSDT", "market": "futures"},
        }
        delegate = mock.Mock()
        delegate.execute.return_value = response
        boundary = runtime.OfficialApiBoundary(profile_name="strategy-live")
        with mock.patch.object(boundary, "_client", return_value=(api_module, client)):
            self.assertEqual(
                boundary.call(module="FUTURES", endpoint_key=endpoint.key, body=body, mutating=True, delegation=delegate),
                {"_mcp_mutation": True, "response": response},
            )
        delegate.execute.assert_called_once_with(settings=settings, endpoint=endpoint, query={}, body=body)
        client.read_endpoint.assert_not_called()

    def test_fixed_mcp_single_accepted_requires_exact_client_identity_and_real_order_id(self) -> None:
        import weex_auto_trade_runtime as runtime

        for operation_key, action, identity_field, market in (
            ("spot.order.place_order", "place_order", "client_order_id", "spot"),
            ("transaction.place_order", "place_order", "client_order_id", "futures"),
            ("transaction.place_pending_order", "place_conditional_order", "client_algo_id", None),
        ):
            payload = {
                "status": "accepted",
                "symbol": "BTCUSDT",
                "request_identity": {identity_field: "client-1"},
            }
            if market is not None:
                payload["market"] = market
            for overrides, expected in (
                ({"upstream_order_id": "101"}, "ACCEPTED"),
                ({}, "REVIEW_REQUIRED"),
                ({"upstream_order_id": "not-an-id"}, "REVIEW_REQUIRED"),
                ({"upstream_order_id": "101", "request_identity": {identity_field: "other"}}, "REVIEW_REQUIRED"),
            ):
                with self.subTest(operation_key=operation_key, overrides=overrides):
                    response = {
                        "ok": True,
                        "action": action,
                        "outcome": {"class": "accepted", "write_attempted": True, "grant_disposition": "accepted"},
                        "result": {**payload, **overrides},
                    }
                    normalized = runtime._normalize_mcp_submission_results(
                        operation_key=operation_key,
                        response=response,
                        prepared_legs=[{"leg_id": "leg-0", "client_order_id": "client-1"}],
                    )
                    self.assertEqual(normalized[0]["status"], expected)
                    self.assertEqual(normalized[0].get("weex_order_id"), "101" if expected == "ACCEPTED" else None)

    def test_fixed_mcp_batch_checks_identity_counts_and_mixed_evidence(self) -> None:
        import weex_auto_trade_runtime as runtime

        prepared = [
            {"leg_id": "leg-0", "client_order_id": "client-1"},
            {"leg_id": "leg-1", "client_order_id": "client-2"},
        ]
        first = {
            "index": 0,
            "request_identity": {"client_order_id": "client-1"},
            "outcome": {"class": "accepted", "write_attempted": True},
            "status": "accepted",
            "upstream_order_id": "101",
        }
        second = {**first, "index": 1, "request_identity": {"client_order_id": "client-2"}, "upstream_order_id": "102"}
        counts = {"accepted": 2, "rejected": 0, "unknown": 0}
        for items, item_counts, expected in (
            ([second, first], counts, ("101", "102")),
            ([{**second, "request_identity": first["request_identity"]}, first], counts, (None, None)),
            ([second, {**first, "index": 1}], counts, (None, None)),
            ([second, first], {**counts, "rejected": 1}, (None, None)),
            ([second], counts, (None, None)),
        ):
            with self.subTest(items=items, counts=item_counts):
                normalized = runtime._normalize_mcp_submission_results(
                    operation_key="spot.order.bulk_order",
                    prepared_legs=prepared,
                    response={
                        "ok": True,
                        "action": "place_batch_orders",
                        "outcome": {"class": "accepted", "write_attempted": True, "grant_disposition": "accepted"},
                        "result": {"status": "accepted", "items": items, "counts": item_counts},
                    },
                )
                self.assertEqual(tuple(leg.get("weex_order_id") for leg in normalized), expected)
                self.assertEqual([leg["status"] for leg in normalized], ["ACCEPTED" if order_id else "REVIEW_REQUIRED" for order_id in expected])

    def test_spot_batch_optional_symbol_must_bind_to_the_reserved_leg(self) -> None:
        import weex_auto_trade_runtime as runtime

        reserved = {"leg_id": "leg-0", "client_order_id": "client-1", "order": {"symbol": " btcusdt "}}
        identity = {"client_order_id": "client-1", "symbol": "BTCUSDT"}
        for operation_key, leg, observed_identity, expected in (
            ("spot.order.bulk_order", reserved, identity, "ACCEPTED"),
            ("spot.order.bulk_order", reserved, {**identity, "symbol": "ETHUSDT"}, "REVIEW_REQUIRED"),
            ("spot.order.bulk_order", reserved, {**identity, "order_id": "101"}, "REVIEW_REQUIRED"),
            ("spot.order.bulk_order", {**reserved, "order": {}}, identity, "REVIEW_REQUIRED"),
            ("transaction.place_orders_batch", reserved, identity, "REVIEW_REQUIRED"),
        ):
            with self.subTest(operation_key=operation_key, leg=leg, identity=observed_identity):
                normalized = runtime._normalize_mcp_submission_results(
                    operation_key=operation_key,
                    prepared_legs=[leg],
                    response={
                        "ok": True,
                        "action": "place_batch_orders",
                        "outcome": {"class": "accepted", "write_attempted": True, "grant_disposition": "accepted"},
                        "result": {
                            "status": "accepted", "counts": {"accepted": 1, "rejected": 0, "unknown": 0},
                            "items": [{
                                "index": 0, "request_identity": observed_identity, "upstream_order_id": "101",
                                "status": "accepted", "outcome": {"class": "accepted", "write_attempted": True},
                            }],
                        },
                    },
                )
                self.assertEqual(normalized[0]["status"], expected)
                self.assertEqual(normalized[0].get("weex_order_id"), "101" if expected == "ACCEPTED" else None)

    def test_fixed_mcp_malformed_not_sent_or_definitive_rejection_never_release(self) -> None:
        import weex_auto_trade_runtime as runtime

        not_sent = {
            "ok": False,
            "action": "place_order",
            "outcome": {"class": "not_sent", "write_attempted": False, "grant_disposition": "not_sent_terminal"},
            "error": {"code": "stale_precondition", "message": "Not sent.", "category": "stale_precondition", "retryable": False},
        }
        for response in (
            {**not_sent, "data": {"orderId": "101"}},
            {**not_sent, "result": {"upstream_order_id": "101"}},
            {**not_sent, "outcome": {**not_sent["outcome"], "write_attempted": True}},
            {**not_sent, "outcome": {**not_sent["outcome"], "grant_disposition": "unchanged"}},
            {**not_sent, "action": "place_tp_sl"},
            {**not_sent, "error": {"code": "stale_precondition"}},
        ):
            with self.subTest(response=response):
                normalized = runtime._normalize_mcp_submission_results(
                    operation_key="spot.order.place_order",
                    response=response,
                    prepared_legs=[{"leg_id": "leg-0", "client_order_id": "client-1"}],
                )
                self.assertEqual(normalized[0]["status"], "REVIEW_REQUIRED")

    def test_fixed_mcp_tp_sl_checks_typed_algo_identity_and_conflicting_exchange_id(self) -> None:
        import weex_auto_trade_runtime as runtime

        identity = {"client_algo_id": "client-1"}
        result = {
            "status": "accepted",
            "request_identity": identity,
            "symbol": "BTCUSDT",
            "plan_type": "TAKE_PROFIT",
            "quantity_mode": "exact",
            "items": [
                {
                    "index": 0,
                    "request_identity": identity,
                    "outcome": {"class": "accepted", "write_attempted": True},
                    "status": "accepted",
                    "upstream_order_id": "101",
                },
            ],
            "counts": {"accepted": 1, "rejected": 0, "unknown": 0},
        }
        for override, expected in (({}, "ACCEPTED"), ({"upstream_order_id": "102"}, "REVIEW_REQUIRED")):
            with self.subTest(override=override):
                normalized = runtime._normalize_mcp_submission_results(
                    operation_key="transaction.place_tp_sl_order",
                    prepared_legs=[{"leg_id": "leg-0", "client_order_id": "client-1"}],
                    response={
                        "ok": True,
                        "action": "place_tp_sl",
                        "outcome": {"class": "accepted", "write_attempted": True, "grant_disposition": "accepted"},
                        "result": {**result, **override},
                    },
                )
                self.assertEqual(normalized[0]["status"], expected)


if __name__ == "__main__":
    unittest.main()
