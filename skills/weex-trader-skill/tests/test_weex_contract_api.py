#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import weex_contract_api as contract


class ContractManualMutationTests(unittest.TestCase):
    def client(self, facade):
        return contract.WeexContractClient(
            base_url=contract.DEFAULT_BASE_URL,
            timeout=15.0,
            locale="en-US",
            api_key=None,
            api_secret=None,
            api_passphrase=None,
            profile_name="main",
            mcp_trade_facade=facade,
        )

    def test_all_16_contract_writes_use_preview_then_same_facade_confirm(self) -> None:
        mutating = [
            endpoint for endpoint in contract.ENDPOINTS.values() if endpoint.mutating
        ]
        self.assertEqual(len(mutating), 16)
        for endpoint in mutating:
            with self.subTest(endpoint=endpoint.key):
                facade = mock.Mock()
                preview_record = {
                    "version": 2,
                    "intent_id": f"intent-{endpoint.key}",
                    "product": "contract",
                }
                facade.preview.return_value = {
                    "ok": True,
                    "preview": preview_record,
                }
                facade.confirm.return_value = {
                    "ok": True,
                    "data": {"status": "accepted"},
                }
                result = self.client(facade).trade_endpoint(
                    endpoint=endpoint,
                    query={},
                    body={},
                )
                self.assertTrue(result["ok"])
                facade.preview.assert_called_once_with(
                    product="contract",
                    endpoint=endpoint,
                    query={},
                    body={},
                )
                facade.confirm.assert_called_once_with(preview_record)

    def test_all_16_contract_cli_writes_prepare_and_show_exact_risk_without_sending(self) -> None:
        mutating = [
            endpoint for endpoint in contract.ENDPOINTS.values() if endpoint.mutating
        ]
        self.assertEqual(len(mutating), 16)
        for endpoint in mutating:
            with self.subTest(endpoint=endpoint.key):
                client = mock.Mock()
                risk = {
                    "scope": "all",
                    "observed_count": 2,
                    "observed_at": 1_000,
                    "dynamic_scope": True,
                }
                client.preview_trade_endpoint.return_value = {
                    "ok": True,
                    "preview": {"intent_id": f"intent-{endpoint.key}"},
                    "summary": {"tool_name": "trade_cancel_open_orders"},
                    "risk": risk,
                    "intent_id": f"intent-{endpoint.key}",
                    "risk_signature": "sha256:" + "1" * 64,
                    "expires_at": 10_000,
                }
                client.trade_endpoint.return_value = {
                    "ok": True,
                    "status": 200,
                    "data": {"status": "accepted"},
                }
                with (
                    mock.patch.object(contract, "validate_pending_order_routing"),
                    mock.patch.object(contract, "validate_endpoint_constraints"),
                    mock.patch.object(contract, "output_json") as output,
                    mock.patch.object(contract, "resolve_language", return_value="en"),
                ):
                    code = contract.execute_endpoint(
                        client=client,
                        endpoint_key=endpoint.key,
                        query={},
                        body={},
                        dry_run=False,
                        confirm_live=False,
                        confirm_demo=False,
                        trading_mode=(
                            "demo" if endpoint.key.startswith("sim.") else "live"
                        ),
                        pretty=False,
                    )
                payload = output.call_args.args[0]
                self.assertEqual(code, 0)
                client.preview_trade_endpoint.assert_called_once_with(
                    endpoint=endpoint,
                    query={},
                    body={},
                )
                client.trade_endpoint.assert_not_called()
                self.assertEqual(payload["mutation_preview"]["risk"], risk)
                self.assertTrue(payload["confirmation_required"])
                self.assertEqual(payload["intent_id"], f"intent-{endpoint.key}")
                self.assertEqual(payload["risk_signature"], "sha256:" + "1" * 64)
                self.assertEqual(payload["trading_mode"], payload["environment"]["trading_mode"])
                self.assertIn("execution time", payload["dynamic_scope_notice"])
                self.assertIn("not a snapshot", payload["dynamic_scope_notice"])

    def test_contract_one_shot_confirm_flag_fails_before_prepare(self) -> None:
        endpoint = contract.ENDPOINTS["transaction.place_order"]
        client = mock.Mock()
        client.trade_endpoint.return_value = {
            "ok": True,
            "status": 200,
            "data": {"status": "accepted"},
        }
        with (
            mock.patch.object(contract, "validate_pending_order_routing"),
            mock.patch.object(contract, "validate_endpoint_constraints"),
            self.assertRaisesRegex(SystemExit, "two_phase_confirmation_required"),
        ):
            contract.execute_endpoint(
                client=client,
                endpoint_key=endpoint.key,
                query={},
                body={},
                dry_run=False,
                confirm_live=True,
                confirm_demo=False,
                trading_mode="live",
                pretty=False,
            )
        client.preview_trade_endpoint.assert_not_called()
        client.trade_endpoint.assert_not_called()

    def test_confirm_and_reconcile_intent_forward_only_durable_identity(self) -> None:
        facade = mock.Mock()
        facade.confirm_intent.return_value = {"ok": True}
        facade.reconcile_intent.return_value = {
            "ok": False,
            "review_required": True,
        }
        client = self.client(facade)

        client.confirm_trade_intent(
            intent_id="intent-1",
            prepared_artifact_digest="sha256:" + "1" * 64,
        )
        client.reconcile_trade_intent(intent_id="intent-1")

        facade.confirm_intent.assert_called_once_with(
            intent_id="intent-1",
            prepared_artifact_digest="sha256:" + "1" * 64,
        )
        facade.reconcile_intent.assert_called_once_with("intent-1")

    def test_confirm_review_result_preserves_reconciliation_identity(self) -> None:
        facade = mock.Mock()
        facade.confirm_intent.return_value = {
            "ok": False,
            "review_required": True,
            "trade_status": "REVIEW_REQUIRED",
            "intent_id": "intent-1",
            "risk_signature": "sha256:" + "1" * 64,
            "expires_at": 10_000,
        }
        result = self.client(facade).confirm_trade_intent(
            intent_id="intent-1",
            prepared_artifact_digest="sha256:" + "1" * 64,
        )
        self.assertEqual(result["intent_id"], "intent-1")
        self.assertEqual(result["risk_signature"], "sha256:" + "1" * 64)
        self.assertEqual(result["expires_at"], 10_000)

    def test_failed_cli_preparation_preserves_error_envelope(self) -> None:
        client = mock.Mock()
        client.preview_trade_endpoint.return_value = {
            "ok": False,
            "status": None,
            "error": {"code": "mcp_trade_unavailable"},
            "outcome": {
                "class": "invocation_rejected",
                "write_attempted": False,
                "grant_disposition": "unchanged",
            },
            "trade_status": "NOT_SENT",
            "review_required": False,
        }
        body = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "positionSide": "LONG",
            "type": "MARKET",
            "quantity": "1",
        }
        with mock.patch.object(contract, "output_json") as output:
            result = contract.execute_endpoint(
                client=client,
                endpoint_key="transaction.place_order",
                query={},
                body=body,
                dry_run=False,
                confirm_live=False,
                confirm_demo=False,
                trading_mode="live",
                pretty=False,
            )
        payload = output.call_args.args[0]
        self.assertEqual(result, 1)
        self.assertEqual(payload["result"], {"code": "mcp_trade_unavailable"})
        self.assertFalse(payload["outcome"]["write_attempted"])
        self.assertNotIn("confirmation_required", payload)
        client.trade_endpoint.assert_not_called()

    def test_mutating_describe_uses_zero_write_facade_only(self) -> None:
        endpoint = next(
            item for item in contract.ENDPOINTS.values() if item.mutating
        )
        facade = mock.Mock()
        facade.describe.return_value = {
            "method": endpoint.method,
            "url": contract.DEFAULT_BASE_URL + endpoint.path,
            "headers": {},
            "query": {},
            "body": {},
        }
        client = self.client(facade)

        result = client.describe_request(endpoint, {}, {})

        self.assertEqual(result["method"], endpoint.method)
        facade.describe.assert_called_once()
        facade.preview.assert_not_called()
        facade.confirm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
