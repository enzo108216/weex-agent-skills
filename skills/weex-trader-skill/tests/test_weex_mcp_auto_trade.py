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
            "weex_mcp_auto_trade.py is missing; expected T8 RED.",
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
            ref_factory=lambda: "opaque-ref",
        )
        self.preview = {
            "version": 1,
            "product": "spot",
            "legacy_key": "spot.order.place_order",
            "preview_tool": "trade_preview_order",
            "confirm_tool": "trade_confirm_order",
            "arguments": {"client_order_id": "client-1"},
            "preview_id": "preview-id",
            "expires_at": 2000,
            "profile_id": "profile-1",
            "spot_base_url": "https://api-spot.weex.com",
            "futures_base_url": "https://api-contract.weex.com",
        }
        self.summary = {
            "action": "order",
            "market": "spot",
            "trading_mode": "live",
            "request_body": dict(self.request_orders[0]),
        }
        self.risk = {"level": "high"}

    def test_bound_ref_authorizes_once_after_read_only_state_check(self) -> None:
        self.delegation.bind_preview(
            profile_id="profile-1",
            preview=self.preview,
            summary=self.summary,
            risk=self.risk,
        )
        result = self.delegation.authorize(
            {
                "context": {"user_id": "profile-1"},
                "credential_fingerprint": "fp-1",
                "execution": {
                    "action": "order",
                    "market": "spot",
                    "trading_mode": "live",
                    "summary": self.summary,
                    "risk": self.risk,
                    "risk_signature": "risk-signature",
                },
                "delegated_ref": {"ref": "opaque-ref"},
            }
        )

        self.assertEqual(result["authorization_id"], "auth-1")
        self.assertEqual(result["expires_at"], 2000)
        self.assertEqual(result["audit_metadata"]["usage_count"], 1)
        self.state.read_delegated_submission_group.assert_called_once_with(
            authorization_id="auth-1",
            submission_group_id="group-1",
            request_fingerprint=self.request_fingerprint,
            usage_ids=["usage-1"],
            client_order_ids=["client-1"],
            now=datetime.fromtimestamp(1000, tz=UTC),
        )
        with self.assertRaisesRegex(self.auto.AutoTradeDelegationError, "used"):
            self.delegation.authorize(
                {
                    "context": {"user_id": "profile-1"},
                    "credential_fingerprint": "fp-1",
                    "execution": {
                        "action": "order",
                        "market": "spot",
                        "trading_mode": "live",
                        "summary": self.summary,
                        "risk": self.risk,
                        "risk_signature": "risk-signature",
                    },
                    "delegated_ref": {"ref": "opaque-ref"},
                }
            )

    def test_ref_profile_action_or_preview_mismatch_fails_before_state_read(self) -> None:
        self.delegation.bind_preview(
            profile_id="profile-1",
            preview=self.preview,
            summary=self.summary,
            risk=self.risk,
        )
        base = {
            "context": {"user_id": "profile-1"},
            "credential_fingerprint": "fp-1",
            "execution": {
                "action": "order",
                "market": "spot",
                "trading_mode": "live",
                "summary": self.summary,
                "risk": self.risk,
                "risk_signature": "risk-signature",
            },
            "delegated_ref": {"ref": "opaque-ref"},
        }
        mutations = (
            {**base, "delegated_ref": {"ref": "wrong"}},
            {**base, "context": {"user_id": "other"}},
            {
                **base,
                "execution": {**base["execution"], "action": "batch_order"},
            },
            {
                **base,
                "execution": {**base["execution"], "summary": {"action": "other"}},
            },
        )
        for payload in mutations:
            with self.subTest(payload=payload):
                with self.assertRaises(self.auto.AutoTradeDelegationError):
                    self.delegation.authorize(payload)
        self.state.read_delegated_submission_group.assert_not_called()

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

    def test_preview_order_fields_must_match_reserved_request_fingerprint(self) -> None:
        with self.assertRaisesRegex(
            self.auto.AutoTradeDelegationError,
            "request",
        ):
            self.delegation.bind_preview(
                profile_id="profile-1",
                preview=self.preview,
                summary={
                    **self.summary,
                    "request_body": {
                        **self.summary["request_body"],
                        "quantity": "2",
                    },
                },
                risk=self.risk,
            )
        self.state.read_delegated_submission_group.assert_not_called()

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
    def test_single_outcome_classes_map_to_existing_guard_statuses(self) -> None:
        import weex_auto_trade_runtime as runtime

        prepared = [
            {
                "leg_id": "leg-0",
                "client_order_id": "client-1",
                "order": {"newClientOrderId": "client-1"},
            }
        ]
        cases = (
            (
                {
                    "ok": True,
                    "data": {"orderId": "order-1", "clientOrderId": "client-1"},
                    "outcome": {
                        "class": "accepted",
                        "write_attempted": True,
                        "action": "order",
                        "client_order_id": "client-1",
                    },
                },
                "ACCEPTED",
            ),
            (
                {
                    "ok": False,
                    "error": {"code": "upstream_error", "details": {"upstream_code": "-1001"}},
                    "outcome": {
                        "class": "definitive_rejection",
                        "write_attempted": True,
                        "action": "order",
                        "client_order_id": "client-1",
                    },
                },
                "RELEASED",
            ),
            (
                {
                    "ok": False,
                    "error": {"code": "approval_mismatch"},
                    "outcome": {
                        "class": "not_sent",
                        "write_attempted": False,
                        "action": "order",
                    },
                },
                "RELEASED",
            ),
            (
                {
                    "ok": False,
                    "error": {"code": "upstream_error"},
                    "outcome": {
                        "class": "unknown",
                        "write_attempted": True,
                        "action": "order",
                        "client_order_id": "client-1",
                    },
                },
                "REVIEW_REQUIRED",
            ),
        )
        for response, expected in cases:
            with self.subTest(expected=expected):
                result = runtime._normalize_mcp_submission_results(
                    operation_key="spot.order.place_order",
                    response=response,
                    prepared_legs=prepared,
                )
                self.assertEqual(result[0]["status"], expected)

    def test_batch_partial_maps_each_leg_by_client_order_id(self) -> None:
        import weex_auto_trade_runtime as runtime

        prepared = [
            {"leg_id": "leg-0", "client_order_id": "client-1", "order": {}},
            {"leg_id": "leg-1", "client_order_id": "client-2", "order": {}},
        ]
        response = {
            "ok": False,
            "data": [
                {
                    "orderId": "order-1",
                    "clientOrderId": "client-1",
                    "success": True,
                },
                {
                    "clientOrderId": "client-2",
                    "success": False,
                    "errorCode": "-1001",
                },
            ],
            "error": {"code": "upstream_error"},
            "outcome": {
                "class": "partial",
                "write_attempted": True,
                "action": "batch_order",
                "items": [
                    {
                        "class": "accepted",
                        "write_attempted": True,
                        "action": "batch_order",
                        "client_order_id": "client-1",
                    },
                    {
                        "class": "definitive_rejection",
                        "write_attempted": True,
                        "action": "batch_order",
                        "client_order_id": "client-2",
                        "error_code": "-1001",
                    },
                ],
            },
        }
        result = runtime._normalize_mcp_submission_results(
            operation_key="spot.order.bulk_order",
            response=response,
            prepared_legs=prepared,
        )
        self.assertEqual([item["status"] for item in result], ["ACCEPTED", "RELEASED"])
        self.assertEqual(result[0]["orderId"], "order-1")
        self.assertEqual(result[1]["error_code"], "-1001")

    def test_aggregate_not_sent_without_identities_releases_all_prepared_legs(self) -> None:
        import weex_auto_trade_runtime as runtime

        cases = (
            (
                "spot.order.place_order",
                [{"leg_id": "leg-0", "client_order_id": "client-1", "order": {}}],
                "order",
            ),
            (
                "spot.order.bulk_order",
                [
                    {"leg_id": "leg-0", "client_order_id": "client-1", "order": {}},
                    {"leg_id": "leg-1", "client_order_id": "client-2", "order": {}},
                ],
                "batch_order",
            ),
            (
                "transaction.place_tp_sl_order",
                [{"leg_id": "leg-0", "client_order_id": "client-1", "order": {}}],
                "tp_sl",
            ),
        )
        for operation_key, prepared, action in cases:
            with self.subTest(operation_key=operation_key):
                result = runtime._normalize_mcp_submission_results(
                    operation_key=operation_key,
                    response={
                        "ok": False,
                        "error": {"code": "missing_fields"},
                        "outcome": {
                            "class": "not_sent",
                            "write_attempted": False,
                            "action": action,
                        },
                    },
                    prepared_legs=prepared,
                )
                self.assertEqual(
                    [item["status"] for item in result],
                    ["RELEASED"] * len(prepared),
                )

    def test_malformed_outcome_invariants_fail_all_legs_to_review_required(self) -> None:
        import weex_auto_trade_runtime as runtime

        prepared = [
            {"leg_id": "leg-0", "client_order_id": "client-1", "order": {}},
            {"leg_id": "leg-1", "client_order_id": "client-2", "order": {}},
        ]
        malformed = (
            {
                "ok": False,
                "outcome": {
                    "class": "accepted",
                    "write_attempted": True,
                    "action": "batch_order",
                    "items": [
                        {"class": "accepted", "write_attempted": True, "action": "batch_order", "client_order_id": "client-1"},
                        {"class": "accepted", "write_attempted": True, "action": "batch_order", "client_order_id": "client-2"},
                    ],
                },
            },
            {
                "ok": False,
                "outcome": {
                    "class": "not_sent",
                    "write_attempted": True,
                    "action": "batch_order",
                    "items": [
                        {"class": "not_sent", "write_attempted": False, "action": "batch_order", "client_order_id": "client-1"},
                        {"class": "not_sent", "write_attempted": False, "action": "batch_order", "client_order_id": "client-2"},
                    ],
                },
            },
            {
                "ok": True,
                "outcome": {
                    "class": "accepted",
                    "write_attempted": True,
                    "action": "batch_order",
                    "items": [
                        {"class": "accepted", "write_attempted": True, "action": "batch_order", "client_order_id": "client-1"},
                        {"class": "definitive_rejection", "write_attempted": True, "action": "batch_order", "client_order_id": "client-2", "error_code": "-1"},
                    ],
                },
            },
            {
                "ok": False,
                "data": {"orderId": "contradictory"},
                "outcome": {
                    "class": "not_sent",
                    "write_attempted": False,
                    "action": "batch_order",
                },
            },
        )
        for response in malformed:
            with self.subTest(response=response):
                result = runtime._normalize_mcp_submission_results(
                    operation_key="spot.order.bulk_order",
                    response=response,
                    prepared_legs=prepared,
                )
                self.assertEqual(
                    [item["status"] for item in result],
                    ["REVIEW_REQUIRED", "REVIEW_REQUIRED"],
                )

    def test_tp_sl_single_item_outcome_maps_algo_identity_to_accepted(self) -> None:
        import weex_auto_trade_runtime as runtime

        prepared = [
            {
                "leg_id": "leg-0",
                "client_order_id": "client-1",
                "order": {"clientAlgoId": "client-1"},
            }
        ]
        response = {
            "ok": True,
            "outcome": {
                "class": "accepted",
                "write_attempted": True,
                "action": "tp_sl",
                "items": [
                    {
                        "class": "accepted",
                        "write_attempted": True,
                        "action": "tp_sl",
                        "operation_reference": "algo-1",
                    }
                ],
            },
        }
        result = runtime._normalize_mcp_submission_results(
            operation_key="transaction.place_tp_sl_order",
            response=response,
            prepared_legs=prepared,
        )
        self.assertEqual(result[0]["status"], "ACCEPTED")
        self.assertEqual(result[0]["weex_order_id"], "algo-1")

        conflicting = runtime._normalize_mcp_submission_results(
            operation_key="transaction.place_tp_sl_order",
            response={
                **response,
                "data": [
                    {
                        "algoId": "algo-2",
                        "success": True,
                    }
                ],
            },
            prepared_legs=prepared,
        )
        self.assertEqual(conflicting[0]["status"], "REVIEW_REQUIRED")

        for item_class, expected in (
            ("definitive_rejection", "RELEASED"),
            ("unknown", "REVIEW_REQUIRED"),
        ):
            with self.subTest(item_class=item_class):
                item = {
                    "class": item_class,
                    "write_attempted": True,
                    "action": "tp_sl",
                    "operation_reference": "algo-1",
                }
                if item_class == "definitive_rejection":
                    item["error_code"] = "-1001"
                mapped = runtime._normalize_mcp_submission_results(
                    operation_key="transaction.place_tp_sl_order",
                    response={
                        "ok": False,
                        "error": {"code": "upstream_error"},
                        "outcome": {
                            "class": item_class,
                            "write_attempted": True,
                            "action": "tp_sl",
                            "items": [item],
                        },
                    },
                    prepared_legs=prepared,
                )
                self.assertEqual(mapped[0]["status"], expected)

        malformed = runtime._normalize_mcp_submission_results(
            operation_key="transaction.place_tp_sl_order",
            response={
                "ok": True,
                "outcome": {
                    "class": "accepted",
                    "write_attempted": True,
                    "action": "tp_sl",
                    "items": [
                        {
                            "class": "accepted",
                            "write_attempted": True,
                            "action": "tp_sl",
                            "operation_reference": {"bad": "identity"},
                        }
                    ],
                },
            },
            prepared_legs=prepared,
        )
        self.assertEqual(malformed[0]["status"], "REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
