#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
ANALYSIS_SCRIPTS = ROOT.parent / "weex-analysis-skill" / "scripts"
if str(ANALYSIS_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ANALYSIS_SCRIPTS))

import weex_order_intent_state as intent_state  # noqa: E402
import weex_analysis_cli as analysis_skill  # noqa: E402
import weex_trade_guard as trade_guard  # noqa: E402


EXPECTED_STANDARD_DISCLAIMER = (
    "Disclaimer: This result is generated solely from the current input data and is for reference only. "
    "It does not constitute any investment or trading advice. Please make your own independent judgment "
    "based on real-time data, official rules, and your own risk tolerance. Responsibility for related "
    "decisions and execution rests solely with the user."
)


def fake_mcp_trade_preview(
    *,
    product: str = "contract",
    legacy_key: str = "transaction.place_order",
    expires_at: int = 10_000,
) -> dict[str, object]:
    return {
        "version": 1,
        "product": product,
        "legacy_key": legacy_key,
        "preview_tool": "trade_preview_order",
        "confirm_tool": "trade_confirm_order",
        "arguments": {"client_order_id": "client-1"},
        "preview_id": "opaque-preview",
        "expires_at": expires_at,
        "profile_id": "profile-id",
        "spot_base_url": "https://api-spot.weex.com",
        "futures_base_url": "https://api-contract.weex.com",
    }


def configure_fake_trade_client(
    client: mock.Mock,
    *,
    data: object,
    on_preview: object | None = None,
) -> None:
    def preview(*, endpoint: object, query: dict[str, object], body: dict[str, object]):
        if callable(on_preview):
            on_preview(endpoint, query, body)
        return {"ok": True, "preview": fake_mcp_trade_preview()}

    client.preview_trade_endpoint.side_effect = preview
    client.confirm_trade_preview.return_value = {
        "ok": True,
        "status": None,
        "data": data,
        "outcome": {
            "class": "accepted",
            "write_attempted": True,
            "action": "order",
        },
        "trade_status": "ACCEPTED",
        "review_required": False,
    }


class OrderIntentStateTests(unittest.TestCase):
    def test_save_load_and_expiry_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                    mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                )
                intent_state.save_intent(intent)

                loaded = intent_state.load_intent()
                saved_path = intent_state.intent_path()
                saved_mode = stat.S_IMODE(saved_path.stat().st_mode)
                temp_files = list(saved_path.parent.glob("*.tmp"))

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["intent_id"], intent["intent_id"])
        if os.name != "nt":
            self.assertEqual(saved_mode, 0o600)
        self.assertEqual(temp_files, [])
        self.assertFalse(intent_state.intent_is_expired(intent, now_ms=300999))
        self.assertTrue(intent_state.intent_is_expired(intent, now_ms=301001))

    def test_load_intent_treats_file_removed_during_read_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                )
                intent_state.save_intent(intent)
                with mock.patch.object(Path, "read_text", side_effect=FileNotFoundError):
                    try:
                        loaded = intent_state.load_intent()
                    except FileNotFoundError as exc:
                        self.fail(f"concurrent intent cleanup escaped as FileNotFoundError: {exc}")

        self.assertIsNone(loaded)

    def test_load_intent_maps_other_io_failures_to_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                )
                intent_state.save_intent(intent)
                with mock.patch.object(Path, "read_text", side_effect=PermissionError):
                    with self.assertRaises(intent_state.IntentStateError) as failure:
                        intent_state.load_intent()

        self.assertEqual(failure.exception.code, "intent_state_unavailable")

    def test_claim_intent_for_confirmation_is_single_use_and_preserves_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent_state.save_intent(intent)

                claimed = intent_state.claim_intent_for_confirmation(
                    expected_intent_id=intent["intent_id"],
                    expected_risk_signature=intent["risk_signature"],
                    expected_intent_type="order",
                    started_at_ms=2000,
                )
                with self.assertRaises(intent_state.IntentStateError) as second_claim:
                    intent_state.claim_intent_for_confirmation(
                        expected_intent_id=intent["intent_id"],
                        expected_risk_signature=intent["risk_signature"],
                        expected_intent_type="order",
                        started_at_ms=3000,
                    )
                persisted = intent_state.load_intent()

        self.assertEqual(claimed["mcp_trade_confirmation_started_at"], 2000)
        self.assertEqual(second_claim.exception.code, "confirmation_started")
        self.assertEqual(persisted["mcp_trade_confirmation_started_at"], 2000)

    def test_claim_intent_rejects_changed_identity_without_overwriting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent_state.save_intent(intent)

                with self.assertRaises(intent_state.IntentStateError) as mismatch:
                    intent_state.claim_intent_for_confirmation(
                        expected_intent_id="different-intent",
                        expected_risk_signature=intent["risk_signature"],
                        expected_intent_type="order",
                        started_at_ms=2000,
                    )
                persisted = intent_state.load_intent()

        self.assertEqual(mismatch.exception.code, "intent_changed")
        self.assertEqual(persisted, intent)

    def test_claim_intent_rejects_state_that_expired_before_lock_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=1,
                )
                intent_state.save_intent(intent)

                with self.assertRaises(intent_state.IntentStateError) as expired:
                    intent_state.claim_intent_for_confirmation(
                        expected_intent_id=intent["intent_id"],
                        expected_risk_signature=intent["risk_signature"],
                        expected_intent_type="order",
                        started_at_ms=2000,
                    )
                persisted = intent_state.load_intent()

        self.assertEqual(expired.exception.code, "intent_expired")
        self.assertNotIn("mcp_trade_confirmation_started_at", persisted)

    def test_save_intent_cannot_replace_confirmation_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent_state.save_intent(intent)
                intent_state.claim_intent_for_confirmation(
                    expected_intent_id=intent["intent_id"],
                    expected_risk_signature=intent["risk_signature"],
                    expected_intent_type="order",
                    started_at_ms=2000,
                )
                replacement = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "ETHUSDT"},
                    raw_order={"symbol": "ETHUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=3000,
                    ttl_seconds=300,
                )

                with self.assertRaises(intent_state.IntentStateError) as overwrite:
                    intent_state.save_intent(replacement)
                persisted = intent_state.load_intent()

        self.assertEqual(overwrite.exception.code, "confirmation_started")
        self.assertEqual(persisted["intent_id"], intent["intent_id"])
        self.assertEqual(persisted["mcp_trade_confirmation_started_at"], 2000)

    def test_claim_intent_has_one_winner_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent_state.save_intent(intent)

                script = """
import json
import sys
sys.path.insert(0, sys.argv[1])
import weex_order_intent_state as state
try:
    state.claim_intent_for_confirmation(
        expected_intent_id=sys.argv[2],
        expected_risk_signature=sys.argv[3],
        expected_intent_type="order",
        started_at_ms=2000,
    )
except state.IntentStateError as exc:
    print(json.dumps({"status": exc.code}))
else:
    print(json.dumps({"status": "claimed"}))
"""
                child_env = dict(os.environ)
                child_env["WEEX_TRADER_SKILL_HOME"] = tempdir
                child_env["PYTHONDONTWRITEBYTECODE"] = "1"
                command = [
                    sys.executable,
                    "-c",
                    script,
                    str(SCRIPTS),
                    intent["intent_id"],
                    intent["risk_signature"],
                ]
                processes = [
                    subprocess.Popen(
                        command,
                        env=child_env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for _ in range(2)
                ]
                results = [process.communicate(timeout=10) for process in processes]

        for process, (_stdout, stderr) in zip(processes, results):
            self.assertEqual(process.returncode, 0, stderr)
        statuses = sorted(json.loads(stdout)["status"] for stdout, _stderr in results)
        self.assertEqual(statuses, ["claimed", "confirmation_started"])

    def test_risk_signature_binds_trading_mode(self) -> None:
        live_intent = intent_state.build_intent(
            profile_name="demo",
            market="futures",
            trading_mode="live",
            order_preview={"symbol": "BTCUSDT"},
            raw_order={"symbol": "BTCUSDT"},
            analysis_output={"alerts": []},
            now_ms=1000,
            ttl_seconds=300,
        )
        demo_intent = intent_state.build_intent(
            profile_name="demo",
            market="futures",
            trading_mode="demo",
            order_preview={"symbol": "BTCUSDT"},
            raw_order={"symbol": "BTCUSDT"},
            analysis_output={"alerts": []},
            now_ms=1000,
            ttl_seconds=300,
        )

        self.assertEqual(live_intent["trading_mode"], "live")
        self.assertEqual(demo_intent["trading_mode"], "demo")
        self.assertNotEqual(live_intent["risk_signature"], demo_intent["risk_signature"])

    def test_mcp_preview_is_persisted_without_credentials_and_limits_intent_expiry(self) -> None:
        preview = {
            "version": 1,
            "product": "contract",
            "legacy_key": "transaction.place_order",
            "preview_tool": "trade_preview_order",
            "confirm_tool": "trade_confirm_order",
            "arguments": {"client_order_id": "stable-id"},
            "preview_id": "opaque-preview",
            "expires_at": 200,
            "profile_id": "profile-id",
            "spot_base_url": "https://api-spot.weex.com",
            "futures_base_url": "https://api-contract.weex.com",
        }
        intent = intent_state.build_intent(
            profile_name="demo",
            market="futures",
            order_preview={"symbol": "BTCUSDT"},
            raw_order={"symbol": "BTCUSDT"},
            analysis_output={"alerts": []},
            now_ms=1000,
            ttl_seconds=300,
            mcp_trade_preview=preview,
        )

        self.assertEqual(intent["mcp_trade_preview"], preview)
        self.assertEqual(intent["expires_at"], 200000)
        serialized = json.dumps(intent)
        for forbidden in ("api_secret", "api_passphrase", "credential"):
            self.assertNotIn(forbidden, serialized)


class TradeGuardTests(unittest.TestCase):
    def test_submit_order_routes_exact_position_close_to_close_positions(self) -> None:
        fake_contract_api = mock.Mock()
        fake_contract_api.find_endpoint_key_by_doc_suffix.return_value = "transaction.close_positions"
        close_endpoint = mock.Mock()
        fake_contract_api.ENDPOINTS = {
            "transaction.place_order": mock.Mock(),
            "transaction.close_positions": close_endpoint,
        }
        fake_contract_api.normalize_contract_trade_symbol.side_effect = lambda symbol: symbol.upper()
        captured_body: dict[str, object] = {}
        fake_client = mock.Mock()
        configure_fake_trade_client(
            fake_client,
            data={
                "endpoint": "transaction.close_positions",
                "businessOk": True,
                "exitCode": 0,
                "result": [{"positionId": 9001, "success": True, "successOrderId": 9101}],
            },
            on_preview=lambda _endpoint, _query, body: captured_body.update(body),
        )
        fresh_account = {
            "partial": False,
            "degraded_reasons": [],
            "positions": [
                {
                    "position_id": "1001",
                    "separated_open_order_id": "8001",
                    "symbol": "ETHUSDT",
                    "position_side": "LONG",
                    "quantity": 0.001,
                    "position_mode": "SEPARATED",
                }
            ],
        }

        with mock.patch.object(
            trade_guard.TradeDataAggregator,
            "collect_account_risk_payload",
            return_value=fresh_account,
        ), mock.patch.object(
            trade_guard,
            "_build_contract_client",
            return_value=(fake_contract_api, fake_client),
        ):
            result = trade_guard._submit_order(
                market="futures",
                profile_name="main",
                trading_mode="live",
                raw_order={
                    "symbol": "ETHUSDT",
                    "side": "SELL",
                    "position_side": "LONG",
                    "type": "MARKET",
                    "quantity": "0.001",
                    "position_id": "1001",
                },
            )

        self.assertEqual(result["endpoint"], "transaction.close_positions")
        fake_contract_api.find_endpoint_key_by_doc_suffix.assert_called_once_with("ClosePositions")
        self.assertEqual(captured_body, {"symbol": "ETHUSDT", "positionId": 1001})

    def test_submit_order_rejects_partial_quantity_for_exact_position_close(self) -> None:
        fresh_account = {
            "partial": False,
            "degraded_reasons": [],
            "positions": [
                {
                    "position_id": "1001",
                    "symbol": "ETHUSDT",
                    "position_side": "LONG",
                    "quantity": 0.01,
                    "position_mode": "SEPARATED",
                }
            ],
        }

        fake_contract_api = mock.Mock()
        fake_contract_api.ENDPOINTS = {"transaction.place_order": mock.Mock()}
        fake_contract_api.normalize_contract_trade_symbol.side_effect = lambda symbol: symbol.upper()
        fake_contract_api.generate_client_oid.return_value = "generated-client-id"
        fake_client = mock.Mock()

        with mock.patch.object(
            trade_guard.TradeDataAggregator,
            "collect_account_risk_payload",
            return_value=fresh_account,
        ), mock.patch.object(
            trade_guard,
            "_build_contract_client",
            return_value=(fake_contract_api, fake_client),
        ) as client_mock:
            with self.assertRaisesRegex(
                trade_guard.AggregationInputError,
                "full separated position quantity",
            ):
                trade_guard._submit_order(
                    market="futures",
                    profile_name="main",
                    trading_mode="live",
                    raw_order={
                        "symbol": "ETHUSDT",
                        "side": "SELL",
                        "position_side": "LONG",
                        "type": "MARKET",
                        "quantity": "0.001",
                        "position_id": "1001",
                    },
                )

        client_mock.assert_not_called()

    def test_trader_local_risk_review_adds_standard_disclaimer(self) -> None:
        order_payload = {
            "order_preview": {
                "market": "spot",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": 0.01,
                "price": 65000,
            },
            "tp_sl": {
                "has_take_profit": False,
                "has_stop_loss": False,
            },
            "account_snapshot": {
                "equity": 1000,
                "available_balance": 500,
            },
            "positions": [],
            "recent_orders": [],
            "market_snapshot": {
                "current_price": 65000,
            },
        }
        account_payload = {
            "mode": "account_scan",
            "market": "spot",
            "account_snapshot": {
                "equity": 1000,
                "available_balance": 1000,
            },
            "positions": [],
            "recent_orders": [],
            "conditional_orders": [],
            "open_orders": [],
            "degraded_reasons": [],
        }

        order_result = trade_guard.analysis.analyze_order_risk(order_payload)
        account_result = trade_guard.analysis.analyze_account_risk(account_payload)

        self.assertEqual(order_result["disclaimer"], EXPECTED_STANDARD_DISCLAIMER)
        self.assertEqual(account_result["disclaimer"], EXPECTED_STANDARD_DISCLAIMER)

    def test_trader_and_analysis_skill_agree_on_order_risk_output(self) -> None:
        payload = {
            "order_preview": {
                "market": "futures",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "position_side": "LONG",
                "order_type": "LIMIT",
                "quantity": 0.05,
                "price": 68000,
            },
            "tp_sl": {
                "has_take_profit": False,
                "has_stop_loss": False,
            },
            "account_snapshot": {
                "equity": 1000,
                "available_balance": 120,
            },
            "positions": [
                {"symbol": "BTCUSDT", "market": "futures", "notional": 400, "leverage": 10},
                {"symbol": "ETHUSDT", "market": "futures", "notional": 500, "leverage": 25},
            ],
            "recent_orders": [
                {"symbol": "BTCUSDT", "time": 1710000000000},
                {"symbol": "BTCUSDT", "time": 1710000600000},
                {"symbol": "BTCUSDT", "time": 1710001200000},
                {"symbol": "BTCUSDT", "time": 1710001800000},
                {"symbol": "BTCUSDT", "time": 1710002400000},
            ],
            "open_orders": [],
            "conditional_orders": [],
            "market_snapshot": {
                "current_price": 65000,
            },
        }

        trader_result = trade_guard.analysis.analyze_order_risk(payload)
        analysis_result = analysis_skill.analyze_order_risk(payload)

        self.assertEqual(trader_result, analysis_result)

    def test_trader_and_analysis_skill_agree_on_account_risk_output(self) -> None:
        payload = {
            "mode": "account_scan",
            "market": "futures",
            "account_snapshot": {
                "equity": 1000,
                "available_balance": 120,
            },
            "positions": [
                {"symbol": "BTCUSDT", "market": "futures", "notional": 750, "leverage": 25},
                {"symbol": "ETHUSDT", "market": "futures", "notional": 200, "leverage": 8},
            ],
            "recent_orders": [
                {"symbol": "BTCUSDT", "time": 1710000000000},
                {"symbol": "BTCUSDT", "time": 1710000600000},
                {"symbol": "BTCUSDT", "time": 1710001200000},
                {"symbol": "BTCUSDT", "time": 1710001800000},
                {"symbol": "BTCUSDT", "time": 1710002400000},
            ],
            "conditional_orders": [],
            "open_orders": [],
            "degraded_reasons": [],
            "constraints": [],
        }

        trader_result = trade_guard.analysis.analyze_account_risk(payload)
        analysis_result = analysis_skill.analyze_account_risk(payload)

        self.assertEqual(trader_result, analysis_result)

    def test_closing_order_does_not_require_new_tp_sl_protection(self) -> None:
        payload = {
            "order_preview": {
                "market": "futures",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "position_side": "LONG",
                "order_type": "MARKET",
                "quantity": 0.01,
            },
            "account_snapshot": {
                "equity": 50000,
                "available_balance": 49000,
            },
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "position_side": "LONG",
                    "quantity": 0.01,
                    "notional": 630,
                }
            ],
            "open_orders": [],
            "conditional_orders": [],
            "recent_orders": [],
            "market_snapshot": {"current_price": 63000},
        }

        trader_result = trade_guard.analysis.analyze_order_risk(payload)
        analysis_result = analysis_skill.analyze_order_risk(payload)

        for result in (trader_result, analysis_result):
            self.assertEqual(result["tp_sl_review"]["required_qty"], 0.0)
            self.assertFalse(any(alert["type"] == "missing_tp_sl" for alert in result["alerts"]))

    def test_preview_order_saves_intent_and_returns_analysis_output(self) -> None:
        args = mock.Mock(
            profile="demo",
            market="futures",
            order_json=json.dumps(
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "position_side": "LONG",
                    "order_type": "LIMIT",
                    "quantity": 0.01,
                    "price": 65000,
                }
            ),
            pretty=True,
            ttl_seconds=300,
        )
        risk_payload = {
            "order_preview": {
                "symbol": "BTCUSDT",
                "market": "futures",
            }
        }
        analysis_payload = {
            "has_risk": True,
            "alerts": [{"type": "missing_tp_sl"}],
            "confirmation_required": True,
            "next_action_hint": "continue order",
        }
        aggregator_instance = mock.Mock()
        aggregator_instance.collect_order_risk_payload.return_value = risk_payload

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(trade_guard, "TradeDataAggregator", return_value=aggregator_instance), mock.patch.object(
                    trade_guard,
                    "_preview_manual_order_trade",
                    return_value=fake_mcp_trade_preview(expires_at=600),
                ):
                    with mock.patch.object(trade_guard.analysis, "analyze_order_risk", return_value=analysis_payload):
                        stream = io.StringIO()
                        with mock.patch.object(sys, "stdout", stream):
                            exit_code = trade_guard.cmd_preview_order(args, now_ms=1000)
                        saved_intent = intent_state.load_intent()

        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(saved_intent)
        self.assertEqual(saved_intent["profile_name"], "demo")
        self.assertIn("risk_signature", saved_intent)
        self.assertIn('"risk_signature"', stream.getvalue())
        self.assertIn('"confirmation_required": true', stream.getvalue().lower())

    def test_preview_order_binds_demo_environment_to_intent_and_confirmation(self) -> None:
        args = mock.Mock(
            profile="demo-profile",
            market="futures",
            trading_mode="demo",
            language="en",
            order_json=json.dumps(
                {
                    "symbol": "BTCSUSDT",
                    "side": "BUY",
                    "position_side": "LONG",
                    "order_type": "MARKET",
                    "quantity": "0.01",
                }
            ),
            pretty=True,
            ttl_seconds=300,
        )
        risk_payload = {
            "trading_mode": "demo",
            "environment": {
                "trading_mode": "demo",
                "label": "demo",
                "market": "futures",
                "uses_real_funds": False,
                "notice": "This operation targets WEEX futures demo mode.",
            },
            "order_preview": {
                "symbol": "BTCSUSDT",
                "market": "futures",
            },
        }
        analysis_payload = {
            "has_risk": False,
            "alerts": [],
            "confirmation_required": True,
            "next_action_hint": "continue order",
        }
        aggregator_instance = mock.Mock()
        aggregator_instance.collect_order_risk_payload.return_value = risk_payload

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(trade_guard, "TradeDataAggregator", return_value=aggregator_instance), mock.patch.object(
                    trade_guard,
                    "_preview_manual_order_trade",
                    return_value=fake_mcp_trade_preview(expires_at=600),
                ):
                    with mock.patch.object(trade_guard.analysis, "analyze_order_risk", return_value=analysis_payload):
                        stream = io.StringIO()
                        with mock.patch.object(sys, "stdout", stream):
                            exit_code = trade_guard.cmd_preview_order(args, now_ms=1000)
                        saved_intent = intent_state.load_intent()

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(saved_intent)
        self.assertEqual(saved_intent["trading_mode"], "demo")
        self.assertEqual(saved_intent["environment"]["trading_mode"], "demo")
        self.assertEqual(payload["environment"]["trading_mode"], "demo")
        self.assertFalse(payload["environment"]["uses_real_funds"])
        self.assertEqual(payload["user_environment_prefix"], "Current trading mode: demo trading")
        self.assertIn("Trading mode: demo trading", payload["user_confirmation"]["reply_instruction"])
        self.assertNotIn("simulated futures environment", payload["user_confirmation"]["reply_instruction"].lower())
        self.assertNotIn("simulated account", payload["user_confirmation"]["reply_instruction"].lower())
        self.assertNotIn("Trading mode: demo;", payload["user_confirmation"]["reply_instruction"])
        self.assertNotIn("Trading environment:", payload["user_confirmation"]["reply_instruction"])
        aggregator_instance.collect_order_risk_payload.assert_called_once_with(
            profile_name="demo-profile",
            market="futures",
            trading_mode="demo",
            raw_order={
                "symbol": "BTCSUSDT",
                "side": "BUY",
                "position_side": "LONG",
                "order_type": "MARKET",
                "quantity": "0.01",
            },
        )

    def test_preview_order_adds_chinese_reply_confirmation_prompt(self) -> None:
        args = mock.Mock(
            profile="demo",
            market="futures",
            language="zh",
            order_json=json.dumps(
                {
                    "symbol": "ETHUSDT",
                    "side": "BUY",
                    "position_side": "LONG",
                    "order_type": "MARKET",
                    "quantity": 0.01,
                }
            ),
            pretty=True,
            ttl_seconds=300,
        )
        risk_payload = {"order_preview": {"symbol": "ETHUSDT", "market": "futures"}}
        analysis_payload = {
            "order_preview": {
                "market": "futures",
                "symbol": "ETHUSDT",
                "side": "BUY",
                "position_side": "LONG",
                "order_type": "MARKET",
                "quantity": 0.01,
            },
            "has_risk": True,
            "alerts": [
                {
                    "type": "missing_tp_sl",
                    "level": "high",
                    "reason": "The order is missing take-profit or stop-loss protection.",
                }
            ],
            "confirmation_required": True,
            "next_action_hint": "continue order",
        }
        aggregator_instance = mock.Mock()
        aggregator_instance.collect_order_risk_payload.return_value = risk_payload

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(trade_guard, "TradeDataAggregator", return_value=aggregator_instance), mock.patch.object(
                    trade_guard,
                    "_preview_manual_order_trade",
                    return_value=fake_mcp_trade_preview(expires_at=600),
                ):
                    with mock.patch.object(trade_guard.analysis, "analyze_order_risk", return_value=analysis_payload):
                        stream = io.StringIO()
                        with mock.patch.object(sys, "stdout", stream):
                            exit_code = trade_guard.cmd_preview_order(args, now_ms=1000)

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["user_confirmation"]["language"], "zh")
        self.assertEqual(payload["user_confirmation"]["reply_text"], "确认")
        self.assertEqual(payload["user_confirmation"]["switch_reply_text"], "切换到模拟盘")
        reply_instruction = payload["user_confirmation"]["reply_instruction"]
        self.assertTrue(reply_instruction.startswith("当前交易环境：真实盘\n本次操作将使用真实资金，请谨慎确认。"))
        self.assertIn("真实盘风险预览已生成，订单尚未提交。", reply_instruction)
        self.assertIn("订单：ETHUSDT 合约，市价开多，数量 0.01。", reply_instruction)
        self.assertIn(
            "高风险提示：这笔订单没有止盈或止损保护，需要你明确接受无保护仓位风险后才能继续。",
            reply_instruction,
        )
        self.assertIn("如果确认使用真实资金提交这笔订单，请回复：确认", reply_instruction)
        self.assertTrue(
            reply_instruction.endswith(
                "如需取消二次确认功能，可申请自动交易授权。授权后，在指定交易类型、交易对、"
                "单笔金额和有效期范围内，下单无需逐笔确认。发送“申请自动交易授权”即可开始配置。"
            )
        )
        self.assertIn("如果需要切换为模拟盘，请回复：切换到模拟盘。", reply_instruction)
        self.assertNotIn("当前盘别：live", reply_instruction)
        self.assertNotIn("确认下单", payload["user_confirmation"]["reply_instruction"])

    def test_preview_order_adds_english_reply_confirmation_prompt(self) -> None:
        args = mock.Mock(
            profile="demo",
            market="futures",
            language="en",
            order_json=json.dumps(
                {
                    "symbol": "ETHUSDT",
                    "side": "BUY",
                    "position_side": "LONG",
                    "order_type": "MARKET",
                    "quantity": 0.01,
                }
            ),
            pretty=True,
            ttl_seconds=300,
        )
        risk_payload = {"order_preview": {"symbol": "ETHUSDT", "market": "futures"}}
        analysis_payload = {
            "has_risk": True,
            "alerts": [{"type": "missing_tp_sl"}],
            "confirmation_required": True,
            "next_action_hint": "continue order",
        }
        aggregator_instance = mock.Mock()
        aggregator_instance.collect_order_risk_payload.return_value = risk_payload

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(trade_guard, "TradeDataAggregator", return_value=aggregator_instance), mock.patch.object(
                    trade_guard,
                    "_preview_manual_order_trade",
                    return_value=fake_mcp_trade_preview(expires_at=600),
                ):
                    with mock.patch.object(trade_guard.analysis, "analyze_order_risk", return_value=analysis_payload):
                        stream = io.StringIO()
                        with mock.patch.object(sys, "stdout", stream):
                            exit_code = trade_guard.cmd_preview_order(args, now_ms=1000)

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["user_confirmation"]["language"], "en")
        self.assertEqual(payload["user_confirmation"]["reply_text"], "confirm")
        self.assertIn("Trading mode: real trading", payload["user_confirmation"]["reply_instruction"])
        self.assertNotIn("Trading environment:", payload["user_confirmation"]["reply_instruction"])
        self.assertNotIn("Trading environment: real account", payload["user_confirmation"]["reply_instruction"])
        self.assertIn("reply: confirm", payload["user_confirmation"]["reply_instruction"])
        self.assertNotIn("Trading mode: live", payload["user_confirmation"]["reply_instruction"])
        self.assertNotIn("确认", payload["user_confirmation"]["reply_instruction"])
        self.assertTrue(
            payload["user_confirmation"]["reply_instruction"].endswith(
                'To disable per-order confirmation, you can request automated trading authorization. '
                'After authorization, orders within the specified trade types, symbols, single-order amount, '
                'and validity period can be placed without per-order confirmation. '
                'Send "Request automated trading authorization" to start configuration.'
            )
        )

    def test_english_confirmation_missing_order_fields_use_english_placeholder(self) -> None:
        confirmation = trade_guard._build_user_confirmation(
            "en",
            environment={"trading_mode": "live", "uses_real_funds": True},
            preview_context={
                "order_preview": {
                    "market": "futures",
                    "side": "BUY",
                    "position_side": "LONG",
                    "order_type": "MARKET",
                    "quantity": "",
                },
                "alerts": [],
            },
            include_mode_switch=True,
        )

        self.assertIn("Order: not returned futures, market open long, quantity not returned.", confirmation["reply_instruction"])
        self.assertNotIn("未返回", confirmation["reply_instruction"])

    def test_preview_order_confirmation_lists_frequency_alert_with_top_warning(self) -> None:
        confirmation = trade_guard._build_user_confirmation(
            "zh",
            environment={"trading_mode": "live", "uses_real_funds": True, "market": "futures"},
            preview_context={
                "order_preview": {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "position_side": "LONG",
                    "order_type": "MARKET",
                    "quantity": "0.001",
                    "market": "futures",
                    "trading_mode": "live",
                },
                "alerts": [
                    {
                        "type": "missing_tp_sl",
                        "level": "high",
                        "reason": "The order is missing take-profit or stop-loss protection.",
                    },
                    {
                        "type": "high_trade_frequency",
                        "level": "warning",
                        "reason": "Recent trading frequency is high in the current review window.",
                    },
                ],
            },
        )

        instruction = confirmation["reply_instruction"]
        self.assertIn("高风险提示", instruction)
        self.assertIn("其他风险提示：", instruction)
        self.assertIn("频繁交易", instruction)
        self.assertIn("近期交易频率偏高", instruction)
        self.assertIn("如果确认使用真实资金提交这笔订单，请回复：确认", instruction)

    def test_confirm_order_rejects_expired_intent(self) -> None:
        args = mock.Mock(intent_id=None, risk_signature=None, confirm_live=True, pretty=False)

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(sys, "stdout", stream):
                    with mock.patch.object(trade_guard, "_submit_live_order") as submit_mock:
                        exit_code = trade_guard.cmd_confirm_order(args, now_ms=1000 + (301 * 1000))

        self.assertEqual(exit_code, 1)
        self.assertIn("expired", stream.getvalue().lower())
        submit_mock.assert_not_called()

    def test_confirm_order_executes_live_order_when_intent_is_valid(self) -> None:
        args = mock.Mock(intent_id="intent-1", risk_signature="sig-1", confirm_live=True, pretty=True)
        execution_payload = {"ok": True, "order_id": "9001"}

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "position_side": "LONG",
                        "order_type": "LIMIT",
                        "quantity": 0.01,
                        "price": 65000,
                        "time_in_force": "GTC",
                    },
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent["mcp_trade_preview"] = fake_mcp_trade_preview(expires_at=600)
                intent["intent_id"] = "intent-1"
                intent["risk_signature"] = "sig-1"
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(
                    trade_guard,
                    "_confirm_saved_trade_preview",
                    return_value={
                        "ok": True,
                        "data": execution_payload,
                        "outcome": {
                            "class": "accepted",
                            "write_attempted": True,
                            "action": "order",
                        },
                        "trade_status": "ACCEPTED",
                        "review_required": False,
                    },
                ) as submit_mock:
                    with mock.patch.object(sys, "stdout", stream):
                        exit_code = trade_guard.cmd_confirm_order(args, now_ms=1000 + (60 * 1000))
                remaining_intent = intent_state.load_intent()

        self.assertEqual(exit_code, 0)
        submit_mock.assert_called_once_with(
            profile_name="demo",
            preview=fake_mcp_trade_preview(expires_at=600),
        )
        self.assertIsNone(remaining_intent)
        self.assertIn('"order_id": "9001"', stream.getvalue())

    def test_confirm_order_preserves_accepted_result_when_intent_cleanup_fails(self) -> None:
        args = mock.Mock(
            intent_id="intent-cleanup",
            risk_signature="sig-cleanup",
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            language="en",
            pretty=False,
        )
        accepted = {
            "ok": True,
            "data": {"order_id": "9001"},
            "outcome": {"class": "accepted", "write_attempted": True, "action": "order"},
            "trade_status": "ACCEPTED",
            "review_required": False,
        }

        for failure_point in ("before_unlink", "after_unlink"):
            with self.subTest(failure_point=failure_point):
                with tempfile.TemporaryDirectory() as tempdir:
                    with mock.patch.dict(
                        os.environ,
                        {"WEEX_TRADER_SKILL_HOME": tempdir},
                        clear=False,
                    ):
                        intent = intent_state.build_intent(
                            profile_name="demo",
                            market="futures",
                            order_preview={"symbol": "BTCUSDT"},
                            raw_order={"symbol": "BTCUSDT"},
                            analysis_output={"alerts": []},
                            now_ms=1000,
                            ttl_seconds=300,
                            mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                        )
                        intent["intent_id"] = args.intent_id
                        intent["risk_signature"] = args.risk_signature
                        intent_state.save_intent(intent)

                        if failure_point == "before_unlink":
                            cleanup_patch = mock.patch.object(
                                trade_guard,
                                "clear_intent",
                                side_effect=intent_state.IntentStateError(
                                    "intent_state_unavailable",
                                    "injected cleanup failure",
                                ),
                            )
                        else:
                            real_fsync = intent_state._fsync_directory
                            fsync_calls = 0

                            def fail_second_fsync(path):
                                nonlocal fsync_calls
                                fsync_calls += 1
                                if fsync_calls == 2:
                                    raise OSError("injected directory fsync failure")
                                return real_fsync(path)

                            cleanup_patch = mock.patch.object(
                                intent_state,
                                "_fsync_directory",
                                side_effect=fail_second_fsync,
                            )

                        stream = io.StringIO()
                        with cleanup_patch, mock.patch.object(
                            trade_guard,
                            "_confirm_saved_trade_preview",
                            return_value=accepted,
                        ), mock.patch.object(sys, "stdout", stream):
                            try:
                                exit_code = trade_guard.cmd_confirm_order(args, now_ms=2000)
                            except intent_state.IntentStateError as exc:
                                self.fail(f"known accepted result was replaced by cleanup error: {exc}")
                        remaining = intent_state.load_intent()

                self.assertEqual(exit_code, 0)
                payload = json.loads(stream.getvalue())
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["order_id"], "9001")
                self.assertEqual(
                    payload["intent_cleanup_warning"]["code"],
                    "INTENT_CLEANUP_FAILED",
                )
                if failure_point == "before_unlink":
                    self.assertIsNotNone(remaining)
                else:
                    self.assertIsNone(remaining)

    def test_concurrent_confirm_order_claims_intent_once(self) -> None:
        args = mock.Mock(
            intent_id="intent-concurrent",
            risk_signature="sig-concurrent",
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            language="en",
            pretty=False,
        )
        accepted = {
            "ok": True,
            "data": {"order_id": "9001"},
            "outcome": {"class": "accepted", "write_attempted": True, "action": "order"},
            "trade_status": "ACCEPTED",
            "review_required": False,
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                    mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                )
                intent["intent_id"] = args.intent_id
                intent["risk_signature"] = args.risk_signature
                intent_state.save_intent(intent)
                read_barrier = threading.Barrier(2)
                real_load = trade_guard.load_intent

                def synchronized_load():
                    loaded = real_load()
                    read_barrier.wait(timeout=5)
                    return loaded

                with mock.patch.object(
                    trade_guard,
                    "load_intent",
                    side_effect=synchronized_load,
                ), mock.patch.object(
                    trade_guard,
                    "_confirm_saved_trade_preview",
                    return_value=accepted,
                ) as submit_mock, mock.patch.object(trade_guard, "_output_json"):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        results = list(
                            executor.map(
                                lambda _index: trade_guard.cmd_confirm_order(args, now_ms=2000),
                                range(2),
                            )
                        )

        self.assertEqual(sorted(results), [0, 1])
        submit_mock.assert_called_once()

    def test_confirm_order_executes_bound_auto_fallback_once(self) -> None:
        args = mock.Mock(
            intent_id="fallback-intent",
            risk_signature="fallback-signature",
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            language="zh",
            pretty=True,
        )
        execution_payload = {
            "ok": True,
            "status": "ACCEPTED",
            "results": [{"leg_id": "leg-0", "status": "ACCEPTED", "orderId": "9002"}],
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                order = {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "0.001",
                }
                intent = intent_state.build_intent(
                    profile_name="live-profile",
                    market="spot",
                    trading_mode="live",
                    order_preview={"operation_key": "spot.order.place_order", "orders": [order]},
                    raw_order=order,
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                    mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                )
                intent.update(
                    {
                        "intent_id": "fallback-intent",
                        "risk_signature": "fallback-signature",
                        "auto_fallback_operation_key": "spot.order.place_order",
                        "auto_fallback_orders": [order],
                    }
                )
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(
                    trade_guard,
                    "_submit_live_auto_fallback_order",
                    return_value=execution_payload,
                    create=True,
                ) as submit_mock:
                    with mock.patch.object(sys, "stdout", stream):
                        exit_code = trade_guard.cmd_confirm_order(args, now_ms=2000)
                remaining_intent = intent_state.load_intent()

        self.assertEqual(exit_code, 0)
        submit_mock.assert_called_once()
        self.assertEqual(submit_mock.call_args.args[0]["intent_id"], "fallback-intent")
        self.assertIsNone(remaining_intent)
        self.assertEqual(json.loads(stream.getvalue())["status"], "ACCEPTED")

    def test_concurrent_auto_fallback_claims_intent_once(self) -> None:
        args = mock.Mock(
            intent_id="fallback-concurrent",
            risk_signature="fallback-concurrent-signature",
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            language="en",
            pretty=False,
        )
        order = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.001",
        }
        accepted = {
            "ok": True,
            "status": "ACCEPTED",
            "results": [{"leg_id": "leg-0", "status": "ACCEPTED", "orderId": "9002"}],
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="live-profile",
                    market="spot",
                    trading_mode="live",
                    order_preview={"operation_key": "spot.order.place_order", "orders": [order]},
                    raw_order=order,
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                    mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                )
                intent.update(
                    {
                        "intent_id": args.intent_id,
                        "risk_signature": args.risk_signature,
                        "auto_fallback_operation_key": "spot.order.place_order",
                        "auto_fallback_orders": [order],
                    }
                )
                intent_state.save_intent(intent)
                read_barrier = threading.Barrier(2)
                real_load = trade_guard.load_intent

                def synchronized_load():
                    loaded = real_load()
                    read_barrier.wait(timeout=5)
                    return loaded

                with mock.patch.object(
                    trade_guard,
                    "load_intent",
                    side_effect=synchronized_load,
                ), mock.patch.object(
                    trade_guard,
                    "_submit_live_auto_fallback_order",
                    return_value=accepted,
                ) as submit_mock, mock.patch.object(trade_guard, "_output_json"):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        results = list(
                            executor.map(
                                lambda _index: trade_guard.cmd_confirm_order(args, now_ms=2000),
                                range(2),
                            )
                        )

        self.assertEqual(sorted(results), [0, 1])
        submit_mock.assert_called_once()

    def test_auto_fallback_review_required_preserves_claimed_intent(self) -> None:
        args = mock.Mock(
            intent_id="fallback-review",
            risk_signature="fallback-review-signature",
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            language="en",
            pretty=False,
        )
        order = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.001",
        }

        for status in ("REVIEW_REQUIRED", "SUBMISSION_GROUP_PARTIAL"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tempdir:
                    with mock.patch.dict(
                        os.environ,
                        {"WEEX_TRADER_SKILL_HOME": tempdir},
                        clear=False,
                    ):
                        intent = intent_state.build_intent(
                            profile_name="live-profile",
                            market="spot",
                            trading_mode="live",
                            order_preview={
                                "operation_key": "spot.order.place_order",
                                "orders": [order],
                            },
                            raw_order=order,
                            analysis_output={"alerts": []},
                            now_ms=1000,
                            ttl_seconds=300,
                            mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                        )
                        intent.update(
                            {
                                "intent_id": args.intent_id,
                                "risk_signature": args.risk_signature,
                                "auto_fallback_operation_key": "spot.order.place_order",
                                "auto_fallback_orders": [order],
                            }
                        )
                        intent_state.save_intent(intent)
                        with mock.patch.object(
                            trade_guard,
                            "_submit_live_auto_fallback_order",
                            return_value={
                                "ok": False,
                                "status": status,
                                "results": [],
                                "next_action": "INSPECT_RESULT_BEFORE_ANY_NEW_ORDER",
                            },
                        ), mock.patch.object(trade_guard, "_output_json"):
                            exit_code = trade_guard.cmd_confirm_order(args, now_ms=2000)
                        remaining = intent_state.load_intent()

                self.assertEqual(exit_code, 0)
                self.assertIsNotNone(remaining)
                self.assertEqual(remaining["mcp_trade_confirmation_started_at"], 2000)

    def test_confirm_order_unknown_is_review_required_and_cannot_be_replayed(self) -> None:
        args = mock.Mock(
            intent_id="intent-unknown",
            risk_signature="sig-unknown",
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            language="en",
            pretty=False,
        )
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                    mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                )
                intent["intent_id"] = "intent-unknown"
                intent["risk_signature"] = "sig-unknown"
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(
                    trade_guard,
                    "_confirm_saved_trade_preview",
                    return_value={
                        "ok": False,
                        "error": {"code": "upstream_error", "message": "uncertain"},
                        "outcome": {
                            "class": "unknown",
                            "write_attempted": True,
                            "action": "order",
                            "client_order_id": "client-1",
                        },
                        "trade_status": "REVIEW_REQUIRED",
                        "review_required": True,
                    },
                ):
                    with mock.patch.object(sys, "stdout", stream):
                        exit_code = trade_guard.cmd_confirm_order(args, now_ms=2000)
                remaining = intent_state.load_intent()
                second_stream = io.StringIO()
                with mock.patch.object(sys, "stdout", second_stream):
                    second_exit = trade_guard.cmd_confirm_order(args, now_ms=700000)
                remaining_after_expiry = intent_state.load_intent()

        self.assertEqual(exit_code, 1)
        self.assertEqual(second_exit, 1)
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining["mcp_trade_confirmation_started_at"], 2000)
        self.assertIsNotNone(remaining_after_expiry)
        self.assertEqual(
            remaining_after_expiry["mcp_trade_confirmation_started_at"],
            2000,
        )
        self.assertIn("already submitted", second_stream.getvalue())
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["status"], "REVIEW_REQUIRED")
        self.assertTrue(payload["review_required"])
        self.assertEqual(payload["outcome"]["class"], "unknown")

    def test_expired_confirm_does_not_clear_concurrently_claimed_unknown_intent(self) -> None:
        args = mock.Mock(
            intent_id="intent-expiry-race",
            risk_signature="sig-expiry-race",
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            language="en",
            pretty=False,
        )
        unknown = {
            "ok": False,
            "error": {"code": "upstream_error", "message": "uncertain"},
            "outcome": {
                "class": "unknown",
                "write_attempted": True,
                "action": "order",
                "client_order_id": "client-1",
            },
            "trade_status": "REVIEW_REQUIRED",
            "review_required": True,
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=1,
                    mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                )
                intent["intent_id"] = args.intent_id
                intent["risk_signature"] = args.risk_signature
                intent_state.save_intent(intent)
                read_barrier = threading.Barrier(2)
                transport_started = threading.Event()
                real_load = trade_guard.load_intent

                def synchronized_load():
                    loaded = real_load()
                    read_barrier.wait(timeout=5)
                    return loaded

                def controlled_expiry(_payload, *, now_ms):
                    if now_ms == 2500:
                        self.assertTrue(transport_started.wait(timeout=5))
                        return True
                    return False

                def return_unknown(**_kwargs):
                    transport_started.set()
                    return unknown

                with mock.patch.object(
                    trade_guard,
                    "load_intent",
                    side_effect=synchronized_load,
                ), mock.patch.object(
                    trade_guard,
                    "intent_is_expired",
                    side_effect=controlled_expiry,
                ), mock.patch.object(
                    trade_guard,
                    "_confirm_saved_trade_preview",
                    side_effect=return_unknown,
                ) as submit_mock, mock.patch.object(trade_guard, "_output_json"):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        active = executor.submit(
                            trade_guard.cmd_confirm_order,
                            args,
                            now_ms=1500,
                        )
                        expired = executor.submit(
                            trade_guard.cmd_confirm_order,
                            args,
                            now_ms=2500,
                        )
                        results = [active.result(timeout=5), expired.result(timeout=5)]
                remaining = intent_state.load_intent()

        self.assertEqual(results, [1, 1])
        submit_mock.assert_called_once()
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining["mcp_trade_confirmation_started_at"], 1500)

    def test_unknown_intent_blocks_new_preview_before_risk_or_mcp_calls(self) -> None:
        args = mock.Mock(
            profile="demo",
            market="futures",
            trading_mode="live",
            language="en",
            order_json=json.dumps(
                {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "position_side": "LONG",
                    "order_type": "MARKET",
                    "quantity": "1",
                }
            ),
            pretty=False,
            ttl_seconds=300,
        )
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                    mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                )
                intent["mcp_trade_confirmation_started_at"] = 2000
                intent_state.save_intent(intent)
                with mock.patch.object(trade_guard, "TradeDataAggregator") as aggregator_mock:
                    with self.assertRaisesRegex(
                        trade_guard.AggregationInputError,
                        "unresolved",
                    ):
                        trade_guard.cmd_preview_order(args, now_ms=700000)
                remaining = intent_state.load_intent()

        aggregator_mock.assert_not_called()
        self.assertEqual(remaining["mcp_trade_confirmation_started_at"], 2000)

    def test_preview_race_with_confirmation_returns_structured_error(self) -> None:
        risk_payload = {
            "order_preview": {
                "symbol": "BTCUSDT",
                "market": "futures",
            }
        }
        analysis_payload = {
            "has_risk": False,
            "alerts": [],
            "confirmation_required": True,
            "next_action_hint": "continue order",
        }
        aggregator_instance = mock.Mock()
        aggregator_instance.collect_order_risk_payload.return_value = risk_payload

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                pending = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                    mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                )
                intent_state.save_intent(pending)

                def claim_during_preview(**_kwargs):
                    intent_state.claim_intent_for_confirmation(
                        expected_intent_id=pending["intent_id"],
                        expected_risk_signature=pending["risk_signature"],
                        expected_intent_type="order",
                        started_at_ms=2000,
                    )
                    return fake_mcp_trade_preview(expires_at=600)

                stream = io.StringIO()
                with mock.patch.object(
                    trade_guard,
                    "TradeDataAggregator",
                    return_value=aggregator_instance,
                ), mock.patch.object(
                    trade_guard,
                    "_preview_manual_order_trade",
                    side_effect=claim_during_preview,
                ), mock.patch.object(
                    trade_guard.analysis,
                    "analyze_order_risk",
                    return_value=analysis_payload,
                ), mock.patch.object(sys, "stdout", stream):
                    try:
                        exit_code = trade_guard.main(
                            [
                                "preview-order",
                                "--profile",
                                "demo",
                                "--market",
                                "futures",
                                "--order-json",
                                json.dumps(
                                    {
                                        "symbol": "BTCUSDT",
                                        "side": "BUY",
                                        "position_side": "LONG",
                                        "order_type": "MARKET",
                                        "quantity": "0.001",
                                    }
                                ),
                            ]
                        )
                    except intent_state.IntentStateError as exc:
                        self.fail(f"preview confirmation race escaped the JSON CLI: {exc}")
                remaining = intent_state.load_intent()

        self.assertEqual(exit_code, 1)
        payload = json.loads(stream.getvalue())
        self.assertFalse(payload["ok"])
        self.assertIn("confirmation already started", payload["error"].lower())
        self.assertEqual(remaining["mcp_trade_confirmation_started_at"], 2000)

    def test_rejection_and_not_sent_clear_intent_for_fresh_preview(self) -> None:
        cases = (
            ("definitive_rejection", "REJECTED", True),
            ("not_sent", "NOT_SENT", False),
        )
        for outcome_class, trade_status, write_attempted in cases:
            with self.subTest(outcome_class=outcome_class):
                args = mock.Mock(
                    intent_id="intent-final",
                    risk_signature="sig-final",
                    trading_mode="live",
                    confirm_live=True,
                    confirm_demo=False,
                    language="en",
                    pretty=False,
                )
                with tempfile.TemporaryDirectory() as tempdir:
                    with mock.patch.dict(
                        os.environ,
                        {"WEEX_TRADER_SKILL_HOME": tempdir},
                        clear=False,
                    ):
                        intent = intent_state.build_intent(
                            profile_name="demo",
                            market="futures",
                            order_preview={"symbol": "BTCUSDT"},
                            raw_order={"symbol": "BTCUSDT"},
                            analysis_output={"alerts": []},
                            now_ms=1000,
                            ttl_seconds=300,
                            mcp_trade_preview=fake_mcp_trade_preview(expires_at=600),
                        )
                        intent["intent_id"] = "intent-final"
                        intent["risk_signature"] = "sig-final"
                        intent_state.save_intent(intent)
                        with mock.patch.object(
                            trade_guard,
                            "_confirm_saved_trade_preview",
                            return_value={
                                "ok": False,
                                "error": {"code": "rejected", "message": "not accepted"},
                                "outcome": {
                                    "class": outcome_class,
                                    "write_attempted": write_attempted,
                                    "action": "order",
                                },
                                "trade_status": trade_status,
                                "review_required": False,
                            },
                        ):
                            with mock.patch.object(sys, "stdout", io.StringIO()):
                                exit_code = trade_guard.cmd_confirm_order(args, now_ms=2000)
                        remaining = intent_state.load_intent()

                self.assertEqual(exit_code, 1)
                self.assertIsNone(remaining)

    def test_auto_fallback_submitter_preserves_batch_legs_and_stops_on_uncertain_result(self) -> None:
        import weex_auto_trade_runtime as runtime_module

        orders = [
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": "0.001",
                "price": "60000",
            },
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": "0.001",
                "price": "70000",
            },
        ]
        intent = {
            "intent_id": "batch-fallback",
            "profile_name": "saved-profile",
            "market": "spot",
            "auto_fallback_operation_key": "spot.order.bulk_order",
            "auto_fallback_orders": orders,
        }
        runtime = mock.Mock()
        runtime.manual_submitter.return_value = [
            {"leg_id": "leg-0", "status": "ACCEPTED", "orderId": "batch-1"},
            {"leg_id": "leg-1", "status": "ACCEPTED", "orderId": "batch-2"},
        ]
        with mock.patch.object(runtime_module, "OfficialAutoTradeRuntime", return_value=runtime):
            accepted = trade_guard._submit_live_auto_fallback_order(intent)

        self.assertEqual(accepted["status"], "ACCEPTED")
        runtime.manual_submitter.assert_called_once()
        operation_key, prepared = runtime.manual_submitter.call_args.args
        self.assertEqual(operation_key, "spot.order.bulk_order")
        self.assertEqual([item["order"]["symbol"] for item in prepared], ["BTCUSDT", "BTCUSDT"])
        client_ids = [item["order"]["newClientOrderId"] for item in prepared]
        self.assertEqual(len(set(client_ids)), 2)

        uncertain_runtime = mock.Mock()
        uncertain_runtime.manual_submitter.side_effect = runtime_module.OfficialRequestUncertain(
            "injected uncertain result"
        )
        with mock.patch.object(
            runtime_module, "OfficialAutoTradeRuntime", return_value=uncertain_runtime
        ):
            uncertain = trade_guard._submit_live_auto_fallback_order(intent)

        self.assertEqual(uncertain["status"], "REVIEW_REQUIRED")
        self.assertEqual(uncertain["error"]["code"], "SUBMISSION_STATE_UNCERTAIN")
        uncertain_runtime.manual_submitter.assert_called_once()

    def test_confirm_order_executes_demo_order_with_matching_demo_flag(self) -> None:
        args = mock.Mock(
            intent_id="intent-demo",
            risk_signature="sig-demo",
            trading_mode="demo",
            confirm_live=False,
            confirm_demo=True,
            pretty=True,
        )
        execution_payload = {
            "orderId": "demo-9001",
            "environment": {"trading_mode": "demo", "uses_real_funds": False},
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo-profile",
                    market="futures",
                    trading_mode="demo",
                    order_preview={"symbol": "BTCSUSDT"},
                    raw_order={
                        "symbol": "BTCSUSDT",
                        "side": "BUY",
                        "position_side": "LONG",
                        "order_type": "MARKET",
                        "quantity": "0.01",
                    },
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent["mcp_trade_preview"] = fake_mcp_trade_preview(expires_at=600)
                intent["intent_id"] = "intent-demo"
                intent["risk_signature"] = "sig-demo"
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(
                    trade_guard,
                    "_confirm_saved_trade_preview",
                    return_value={
                        "ok": True,
                        "data": execution_payload,
                        "outcome": {
                            "class": "accepted",
                            "write_attempted": True,
                            "action": "order",
                        },
                        "trade_status": "ACCEPTED",
                        "review_required": False,
                    },
                ) as submit_mock:
                    with mock.patch.object(trade_guard, "resolve_language", return_value="zh"):
                        with mock.patch.object(sys, "stdout", stream):
                            exit_code = trade_guard.cmd_confirm_order(args, now_ms=2000)
                remaining_intent = intent_state.load_intent()

        self.assertEqual(exit_code, 0)
        submit_mock.assert_called_once_with(
            profile_name="demo-profile",
            preview=fake_mcp_trade_preview(expires_at=600),
        )
        self.assertIsNone(remaining_intent)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["environment"]["trading_mode"], "demo")
        self.assertEqual(payload["user_environment_prefix"], "当前交易环境：模拟盘")

    def test_confirm_order_parser_accepts_language_for_environment_prefix(self) -> None:
        args = trade_guard.build_parser().parse_args(
            [
                "confirm-order",
                "--intent-id",
                "intent-demo",
                "--risk-signature",
                "sig-demo",
                "--trading-mode",
                "demo",
                "--confirm-demo",
                "--language",
                "zh",
            ]
        )

        self.assertEqual(args.language, "zh")

    def test_confirm_order_rejects_demo_intent_with_live_flag(self) -> None:
        args = mock.Mock(
            intent_id="intent-demo",
            risk_signature="sig-demo",
            trading_mode="demo",
            confirm_live=True,
            confirm_demo=False,
            pretty=False,
        )

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo-profile",
                    market="futures",
                    trading_mode="demo",
                    order_preview={"symbol": "BTCSUSDT"},
                    raw_order={"symbol": "BTCSUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent["intent_id"] = "intent-demo"
                intent["risk_signature"] = "sig-demo"
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(sys, "stdout", stream):
                    with mock.patch.object(trade_guard, "_submit_order") as submit_mock:
                        exit_code = trade_guard.cmd_confirm_order(args, now_ms=2000)

        self.assertEqual(exit_code, 1)
        self.assertIn("confirm", stream.getvalue().lower())
        self.assertIn("demo", stream.getvalue().lower())
        submit_mock.assert_not_called()

    def test_confirm_order_rejects_cli_mode_that_does_not_match_intent(self) -> None:
        args = mock.Mock(
            intent_id="intent-demo",
            risk_signature="sig-demo",
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            pretty=False,
        )

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo-profile",
                    market="futures",
                    trading_mode="demo",
                    order_preview={"symbol": "BTCSUSDT"},
                    raw_order={"symbol": "BTCSUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent["intent_id"] = "intent-demo"
                intent["risk_signature"] = "sig-demo"
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(sys, "stdout", stream):
                    with mock.patch.object(trade_guard, "_submit_order") as submit_mock:
                        exit_code = trade_guard.cmd_confirm_order(args, now_ms=2000)

        self.assertEqual(exit_code, 1)
        self.assertIn("trading mode", stream.getvalue().lower())
        submit_mock.assert_not_called()

    def test_submit_live_order_passes_new_client_order_id_for_futures(self) -> None:
        prepared_bodies: list[dict[str, object]] = []
        fake_contract_api = mock.Mock()
        fake_contract_api.ENDPOINTS = {"transaction.place_order": {"path": "/capi/v3/order"}}
        fake_contract_api.normalize_contract_trade_symbol.side_effect = lambda symbol: symbol.upper()
        fake_client = mock.Mock()

        def prepare_request(endpoint: dict[str, object], query: dict[str, object], body: dict[str, object]) -> dict[str, object]:
            prepared_bodies.append(body)
            return {"endpoint": endpoint, "query": query, "body": body}

        configure_fake_trade_client(
            fake_client,
            data={"orderId": "1001"},
            on_preview=prepare_request,
        )

        with mock.patch.object(trade_guard, "_build_contract_client", return_value=(fake_contract_api, fake_client)):
            result = trade_guard._submit_live_order(
                market="futures",
                profile_name="demo",
                raw_order={
                    "symbol": "btcusdt",
                    "side": "SELL",
                    "position_side": "LONG",
                    "order_type": "MARKET",
                    "quantity": "0.01",
                    "new_client_order_id": "monitor-mon1-close",
                },
            )

        self.assertEqual(result["orderId"], "1001")
        self.assertEqual(prepared_bodies[0]["newClientOrderId"], "monitor-mon1-close")

    def test_submit_live_order_accepts_camelcase_field_names_for_futures(self) -> None:
        prepared_bodies: list[dict[str, object]] = []
        fake_contract_api = mock.Mock()
        fake_contract_api.ENDPOINTS = {"transaction.place_order": {"path": "/capi/v3/order"}}
        fake_contract_api.normalize_contract_trade_symbol.side_effect = lambda symbol: symbol.upper()
        fake_client = mock.Mock()

        def prepare_request(endpoint: dict[str, object], query: dict[str, object], body: dict[str, object]) -> dict[str, object]:
            prepared_bodies.append(body)
            return {"endpoint": endpoint, "query": query, "body": body}

        configure_fake_trade_client(
            fake_client,
            data={"orderId": "2001"},
            on_preview=prepare_request,
        )

        with mock.patch.object(trade_guard, "_build_contract_client", return_value=(fake_contract_api, fake_client)):
            result = trade_guard._submit_live_order(
                market="futures",
                profile_name="demo",
                raw_order={
                    "symbol": "btcusdt",
                    "side": "SELL",
                    "positionSide": "LONG",
                    "type": "MARKET",
                    "quantity": "0.01",
                    "timeInForce": "GTC",
                    "newClientOrderId": "monitor-camel-close",
                },
            )

        self.assertEqual(result["orderId"], "2001")
        self.assertEqual(prepared_bodies[0]["positionSide"], "LONG")
        self.assertEqual(prepared_bodies[0]["type"], "MARKET")
        self.assertEqual(prepared_bodies[0]["timeInForce"], "GTC")
        self.assertEqual(prepared_bodies[0]["newClientOrderId"], "monitor-camel-close")

    def test_submit_live_order_uses_live_contract_endpoint_when_demo_endpoint_is_present(self) -> None:
        live_endpoint = {"path": "/capi/v3/order"}
        demo_endpoint = {"path": "/capi/v3/sim/order"}
        fake_contract_api = mock.Mock()
        fake_contract_api.ENDPOINTS = {
            "sim.transaction.place_order": demo_endpoint,
            "transaction.place_order": live_endpoint,
        }
        fake_contract_api.find_endpoint_key_by_doc_suffix.return_value = "sim.transaction.place_order"
        fake_contract_api.normalize_contract_trade_symbol.side_effect = lambda symbol: symbol.upper()
        fake_client = mock.Mock()
        configure_fake_trade_client(fake_client, data={"orderId": "live-2002"})

        with mock.patch.object(trade_guard, "_build_contract_client", return_value=(fake_contract_api, fake_client)):
            result = trade_guard._submit_live_order(
                market="futures",
                profile_name="demo",
                raw_order={
                    "symbol": "btcusdt",
                    "side": "BUY",
                    "position_side": "LONG",
                    "order_type": "MARKET",
                    "quantity": "0.01",
                },
            )

        self.assertEqual(result["orderId"], "live-2002")
        self.assertIs(
            fake_client.preview_trade_endpoint.call_args.kwargs["endpoint"],
            live_endpoint,
        )

    def test_submit_demo_order_maps_standard_symbol_to_official_sim_symbol(self) -> None:
        prepared_bodies: list[dict[str, object]] = []
        fake_contract_api = mock.Mock()
        fake_contract_api.ENDPOINTS = {"sim.transaction.place_order": {"path": "/capi/v3/sim/order"}}
        fake_contract_api.normalize_contract_trade_symbol.side_effect = lambda symbol: symbol.upper()
        fake_contract_api.normalize_contract_demo_trade_symbol.side_effect = lambda symbol: f"{symbol.upper()[:-4]}SUSDT"
        fake_client = mock.Mock()

        def prepare_request(endpoint: dict[str, object], query: dict[str, object], body: dict[str, object]) -> dict[str, object]:
            prepared_bodies.append(body)
            return {"endpoint": endpoint, "query": query, "body": body}

        configure_fake_trade_client(
            fake_client,
            data={"orderId": "demo-3001"},
            on_preview=prepare_request,
        )

        with mock.patch.object(trade_guard, "_build_contract_client", return_value=(fake_contract_api, fake_client)):
            result = trade_guard._submit_order(
                market="futures",
                profile_name="demo",
                trading_mode="demo",
                raw_order={
                    "symbol": "btcusdt",
                    "side": "BUY",
                    "position_side": "LONG",
                    "order_type": "MARKET",
                    "quantity": "0.001",
                    "new_client_order_id": "demo-symbol-map",
                },
            )

        self.assertEqual(result["orderId"], "demo-3001")
        self.assertEqual(prepared_bodies[0]["symbol"], "BTCSUSDT")

    def test_submit_live_order_accepts_camelcase_field_names_for_spot(self) -> None:
        prepared_bodies: list[dict[str, object]] = []
        fake_spot_api = mock.Mock()
        fake_spot_api.ENDPOINTS = {"place": {"path": "/sapi/v1/order"}}
        fake_spot_api.find_endpoint_key_by_doc_suffix.return_value = "place"
        fake_spot_api.normalize_spot_symbol.side_effect = lambda symbol: symbol.upper()
        fake_client = mock.Mock()

        def prepare_request(endpoint: dict[str, object], query: dict[str, object], body: dict[str, object]) -> dict[str, object]:
            prepared_bodies.append(body)
            return {"endpoint": endpoint, "query": query, "body": body}

        configure_fake_trade_client(
            fake_client,
            data={"orderId": "3001"},
            on_preview=prepare_request,
        )

        with mock.patch.object(trade_guard, "_build_spot_client", return_value=(fake_spot_api, fake_client)):
            result = trade_guard._submit_live_order(
                market="spot",
                profile_name="demo",
                raw_order={
                    "symbol": "btcusdt",
                    "side": "BUY",
                    "type": "LIMIT",
                    "quantity": "0.01",
                    "price": "65000",
                    "timeInForce": "GTC",
                },
            )

        self.assertEqual(result["orderId"], "3001")
        self.assertEqual(prepared_bodies[0]["type"], "LIMIT")
        self.assertEqual(prepared_bodies[0]["timeInForce"], "GTC")

    def test_submit_live_order_forwards_client_order_id_for_spot(self) -> None:
        prepared_bodies: list[dict[str, object]] = []
        fake_spot_api = mock.Mock()
        fake_spot_api.ENDPOINTS = {"place": {"path": "/sapi/v1/order"}}
        fake_spot_api.find_endpoint_key_by_doc_suffix.return_value = "place"
        fake_spot_api.normalize_spot_symbol.side_effect = lambda symbol: symbol.upper()
        fake_client = mock.Mock()

        def prepare_request(endpoint: dict[str, object], query: dict[str, object], body: dict[str, object]) -> dict[str, object]:
            prepared_bodies.append(body)
            return {"endpoint": endpoint, "query": query, "body": body}

        configure_fake_trade_client(
            fake_client,
            data={"orderId": "3002"},
            on_preview=prepare_request,
        )

        with mock.patch.object(trade_guard, "_build_spot_client", return_value=(fake_spot_api, fake_client)):
            result = trade_guard._submit_live_order(
                market="spot",
                profile_name="demo",
                raw_order={
                    "symbol": "btcusdt",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "0.01",
                    "new_client_order_id": "spot-client-3002",
                },
            )

        self.assertEqual(result["orderId"], "3002")
        self.assertEqual(prepared_bodies[0].get("newClientOrderId"), "spot-client-3002")

    def test_submit_live_order_generates_client_order_id_for_spot(self) -> None:
        prepared_bodies: list[dict[str, object]] = []
        fake_spot_api = mock.Mock()
        fake_spot_api.ENDPOINTS = {"place": {"path": "/sapi/v1/order"}}
        fake_spot_api.find_endpoint_key_by_doc_suffix.return_value = "place"
        fake_spot_api.normalize_spot_symbol.side_effect = lambda symbol: symbol.upper()
        fake_spot_api.generate_client_order_id.return_value = "codex-generated-3003"
        fake_client = mock.Mock()

        def prepare_request(endpoint: dict[str, object], query: dict[str, object], body: dict[str, object]) -> dict[str, object]:
            prepared_bodies.append(body)
            return {"endpoint": endpoint, "query": query, "body": body}

        configure_fake_trade_client(
            fake_client,
            data={"orderId": "3003"},
            on_preview=prepare_request,
        )

        with mock.patch.object(trade_guard, "_build_spot_client", return_value=(fake_spot_api, fake_client)):
            result = trade_guard._submit_live_order(
                market="spot",
                profile_name="demo",
                raw_order={
                    "symbol": "btcusdt",
                    "side": "BUY",
                    "type": "MARKET",
                    "quantity": "0.01",
                },
            )

        self.assertEqual(result["orderId"], "3003")
        self.assertEqual(prepared_bodies[0].get("newClientOrderId"), "codex-generated-3003")
        fake_spot_api.generate_client_order_id.assert_called_once_with()

    def test_submit_live_order_raises_on_missing_position_side_for_futures(self) -> None:
        with mock.patch.object(trade_guard, "_build_contract_client"):
            with self.assertRaises(trade_guard.AggregationInputError) as exc_info:
                trade_guard._submit_live_order(
                    market="futures",
                    profile_name="demo",
                    raw_order={
                        "symbol": "BTCUSDT",
                        "side": "SELL",
                        "type": "MARKET",
                        "quantity": "0.01",
                    },
                )
        self.assertIn("positionSide", str(exc_info.exception))

    def test_submit_live_order_raises_on_missing_type_for_futures(self) -> None:
        with mock.patch.object(trade_guard, "_build_contract_client"):
            with self.assertRaises(trade_guard.AggregationInputError) as exc_info:
                trade_guard._submit_live_order(
                    market="futures",
                    profile_name="demo",
                    raw_order={
                        "symbol": "BTCUSDT",
                        "side": "SELL",
                        "positionSide": "LONG",
                        "quantity": "0.01",
                    },
                )
        self.assertIn("type", str(exc_info.exception))

    def test_submit_live_order_raises_on_missing_type_for_spot(self) -> None:
        with mock.patch.object(trade_guard, "_build_spot_client"):
            with self.assertRaises(trade_guard.AggregationInputError) as exc_info:
                trade_guard._submit_live_order(
                    market="spot",
                    profile_name="demo",
                    raw_order={
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "quantity": "0.01",
                    },
                )
        self.assertIn("type", str(exc_info.exception))

    def test_preview_tp_sl_saves_intent_and_returns_confirmation_fields(self) -> None:
        args = mock.Mock(
            profile="demo",
            language="zh",
            tp_sl_json=json.dumps(
                {
                    "symbol": "ETHUSDT",
                    "clientAlgoId": "mon_price_demo",
                    "planType": "TAKE_PROFIT",
                    "triggerPrice": "2500",
                    "executePrice": "0",
                    "quantity": "0.2",
                    "positionSide": "SHORT",
                    "triggerPriceType": "CONTRACT_PRICE",
                }
            ),
            pretty=True,
            ttl_seconds=300,
        )
        aggregator_instance = mock.Mock()
        aggregator_instance.collect_account_risk_payload.return_value = {
            "mode": "account_scan",
            "market": "futures",
            "account_snapshot": {"equity": 1000, "available_balance": 500},
            "positions": [],
            "recent_orders": [],
            "conditional_orders": [],
            "open_orders": [],
            "degraded_reasons": [],
            "constraints": [],
        }
        analysis_payload = {
            "has_risk": False,
            "alerts": [],
            "confirmation_required": True,
            "next_action_hint": "confirm tp/sl",
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(trade_guard, "TradeDataAggregator", return_value=aggregator_instance), mock.patch.object(
                    trade_guard,
                    "_preview_manual_tp_sl_trade",
                    return_value=fake_mcp_trade_preview(
                        legacy_key="transaction.place_tp_sl_order",
                        expires_at=600,
                    ),
                ):
                    with mock.patch.object(trade_guard.analysis, "analyze_account_risk", return_value=analysis_payload):
                        stream = io.StringIO()
                        with mock.patch.object(sys, "stdout", stream):
                            exit_code = trade_guard.cmd_preview_tp_sl(args, now_ms=1000)
                        saved_intent = intent_state.load_intent()

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(saved_intent)
        self.assertEqual(saved_intent["intent_type"], "tp_sl_order")
        self.assertEqual(saved_intent["tp_sl_order"]["clientAlgoId"], "mon_price_demo")
        self.assertIn("risk_signature", payload)
        self.assertEqual(payload["user_environment_prefix"], "当前交易环境：真实盘")
        self.assertEqual(payload["user_confirmation"]["reply_text"], "确认")
        self.assertNotIn("申请自动交易授权", payload["user_confirmation"]["reply_instruction"])

    def test_tp_sl_normalization_accepts_omitted_and_zero_quantity_as_full_position(self) -> None:
        base = {
            "symbol": "ETHUSDT",
            "clientAlgoId": "full-position-tp",
            "planType": "TAKE_PROFIT",
            "triggerPrice": "2500",
            "executePrice": "0",
            "positionSide": "SHORT",
            "triggerPriceType": "CONTRACT_PRICE",
        }

        try:
            omitted = trade_guard._normalize_tp_sl_order(dict(base))
            zero = trade_guard._normalize_tp_sl_order({**base, "quantity": "0"})
        except trade_guard.AggregationInputError as exc:
            self.fail(f"official full-position TP/SL quantity should be accepted: {exc}")

        self.assertNotIn("quantity", omitted)
        self.assertEqual(zero["quantity"], "0")

    def test_tp_sl_confirmation_explicitly_warns_when_quantity_targets_full_position(self) -> None:
        confirmation = trade_guard._build_user_confirmation(
            "zh",
            environment={
                "trading_mode": "live",
                "uses_real_funds": True,
            },
            preview_context={
                "order_preview": {
                    "symbol": "ETHUSDT",
                    "planType": "TAKE_PROFIT",
                    "positionSide": "SHORT",
                    "quantity": "0",
                },
                "alerts": [],
            },
        )

        self.assertIn("全部仓位", confirmation["reply_instruction"])

    def test_confirm_tp_sl_executes_live_tp_sl_when_intent_is_valid(self) -> None:
        args = mock.Mock(intent_id="intent-tpsl", risk_signature="sig-tpsl", confirm_live=True, pretty=True)
        tp_sl_order = {
            "symbol": "ETHUSDT",
            "clientAlgoId": "mon_price_demo",
            "planType": "TAKE_PROFIT",
            "triggerPrice": "2500",
            "executePrice": "0",
            "quantity": "0.2",
            "positionSide": "SHORT",
            "triggerPriceType": "CONTRACT_PRICE",
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    environment={
                        "trading_mode": "live",
                        "label": "live",
                        "market": "futures",
                        "uses_real_funds": True,
                        "notice": "custom live TP/SL environment",
                    },
                    order_preview=tp_sl_order,
                    raw_order=tp_sl_order,
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                    intent_type="tp_sl_order",
                    tp_sl_order=tp_sl_order,
                )
                intent["mcp_trade_preview"] = fake_mcp_trade_preview(
                    legacy_key="transaction.place_tp_sl_order",
                    expires_at=600,
                )
                intent["intent_id"] = "intent-tpsl"
                intent["risk_signature"] = "sig-tpsl"
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(
                    trade_guard,
                    "_confirm_saved_trade_preview",
                    return_value={
                        "ok": True,
                        "data": {"algoId": "7001", "clientAlgoId": "mon_price_demo"},
                        "outcome": {
                            "class": "accepted",
                            "write_attempted": True,
                            "action": "tp_sl",
                        },
                        "trade_status": "ACCEPTED",
                        "review_required": False,
                    },
                ) as submit_mock:
                    with mock.patch.object(trade_guard, "resolve_language", return_value="zh"):
                        with mock.patch.object(sys, "stdout", stream):
                            exit_code = trade_guard.cmd_confirm_tp_sl(args, now_ms=2000)
                remaining_intent = intent_state.load_intent()

        self.assertEqual(exit_code, 0)
        submit_mock.assert_called_once_with(
            profile_name="demo",
            preview=fake_mcp_trade_preview(
                legacy_key="transaction.place_tp_sl_order",
                expires_at=600,
            ),
        )
        self.assertIsNone(remaining_intent)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["algoId"], "7001")
        self.assertEqual(payload["trading_mode"], "live")
        self.assertEqual(payload["environment"]["trading_mode"], "live")
        self.assertEqual(payload["environment"]["market"], "futures")
        self.assertTrue(payload["environment"]["uses_real_funds"])
        self.assertEqual(payload["environment"]["notice"], "custom live TP/SL environment")
        self.assertEqual(payload["user_environment_prefix"], "当前交易环境：真实盘")

    def test_concurrent_confirm_tp_sl_claims_intent_once(self) -> None:
        args = mock.Mock(
            intent_id="intent-tpsl-concurrent",
            risk_signature="sig-tpsl-concurrent",
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            language="en",
            pretty=False,
        )
        tp_sl_order = {
            "symbol": "ETHUSDT",
            "clientAlgoId": "mon_price_demo",
            "planType": "TAKE_PROFIT",
            "triggerPrice": "2500",
            "executePrice": "0",
            "quantity": "0.2",
            "positionSide": "SHORT",
            "triggerPriceType": "CONTRACT_PRICE",
        }
        accepted = {
            "ok": True,
            "data": {"algoId": "7001", "clientAlgoId": "mon_price_demo"},
            "outcome": {"class": "accepted", "write_attempted": True, "action": "tp_sl"},
            "trade_status": "ACCEPTED",
            "review_required": False,
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview=tp_sl_order,
                    raw_order=tp_sl_order,
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                    intent_type="tp_sl_order",
                    tp_sl_order=tp_sl_order,
                    mcp_trade_preview=fake_mcp_trade_preview(
                        legacy_key="transaction.place_tp_sl_order",
                        expires_at=600,
                    ),
                )
                intent["intent_id"] = args.intent_id
                intent["risk_signature"] = args.risk_signature
                intent_state.save_intent(intent)
                read_barrier = threading.Barrier(2)
                real_load = trade_guard.load_intent

                def synchronized_load():
                    loaded = real_load()
                    read_barrier.wait(timeout=5)
                    return loaded

                with mock.patch.object(
                    trade_guard,
                    "load_intent",
                    side_effect=synchronized_load,
                ), mock.patch.object(
                    trade_guard,
                    "_confirm_saved_trade_preview",
                    return_value=accepted,
                ) as submit_mock, mock.patch.object(trade_guard, "_output_json"):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        results = list(
                            executor.map(
                                lambda _index: trade_guard.cmd_confirm_tp_sl(args, now_ms=2000),
                                range(2),
                            )
                        )

        self.assertEqual(sorted(results), [0, 1])
        submit_mock.assert_called_once()

    def test_confirm_order_requires_intent_id_and_risk_signature(self) -> None:
        args = mock.Mock(intent_id=None, risk_signature=None, confirm_live=True, pretty=False)

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(sys, "stdout", stream):
                    with mock.patch.object(trade_guard, "_submit_live_order") as submit_mock:
                        exit_code = trade_guard.cmd_confirm_order(args, now_ms=2000)

        self.assertEqual(exit_code, 1)
        self.assertIn("intent-id", stream.getvalue().lower())
        self.assertIn("risk-signature", stream.getvalue().lower())
        submit_mock.assert_not_called()

    def test_confirm_order_requires_confirm_live_flag(self) -> None:
        args = mock.Mock(intent_id=None, risk_signature=None, confirm_live=False, pretty=False)

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(sys, "stdout", stream):
                    with mock.patch.object(trade_guard, "_submit_live_order") as submit_mock:
                        exit_code = trade_guard.cmd_confirm_order(args, now_ms=1000 + 1000)

        self.assertEqual(exit_code, 1)
        self.assertIn("confirm-live", stream.getvalue().lower())
        submit_mock.assert_not_called()

    def test_confirm_order_rejects_mismatched_risk_signature(self) -> None:
        args = mock.Mock(intent_id="intent-1", risk_signature="wrong-signature", confirm_live=True, pretty=False)

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = intent_state.build_intent(
                    profile_name="demo",
                    market="futures",
                    order_preview={"symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT"},
                    analysis_output={"alerts": []},
                    now_ms=1000,
                    ttl_seconds=300,
                )
                intent["intent_id"] = "intent-1"
                intent_state.save_intent(intent)
                stream = io.StringIO()
                with mock.patch.object(sys, "stdout", stream):
                    with mock.patch.object(trade_guard, "_submit_live_order") as submit_mock:
                        exit_code = trade_guard.cmd_confirm_order(args, now_ms=2000)

        self.assertEqual(exit_code, 1)
        self.assertIn("risk signature", stream.getvalue().lower())
        submit_mock.assert_not_called()

    def test_account_scan_command_returns_analysis_output(self) -> None:
        args = mock.Mock(profile="demo", market="futures", trading_mode="live", symbol="BTCUSDT", language="zh", pretty=True)
        aggregator_instance = mock.Mock()
        aggregator_instance.collect_account_risk_payload.return_value = {
            "mode": "account_scan",
            "market": "futures",
            "account_snapshot": {"equity": 1000, "available_balance": 120},
            "positions": [],
            "recent_orders": [],
            "conditional_orders": [],
            "open_orders": [],
            "degraded_reasons": [],
            "constraints": [],
        }
        analysis_payload = {
            "has_risk": True,
            "alerts": [{"type": "low_free_balance"}],
        }

        with mock.patch.object(trade_guard, "TradeDataAggregator", return_value=aggregator_instance):
            with mock.patch.object(trade_guard.analysis, "analyze_account_risk", return_value=analysis_payload):
                stream = io.StringIO()
                with mock.patch.object(sys, "stdout", stream):
                    exit_code = trade_guard.cmd_account_scan(args)

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["user_environment_prefix"], "当前交易环境：真实盘")
        self.assertNotIn("user_confirmation", payload)
        self.assertIn("low_free_balance", stream.getvalue())

    def test_main_returns_structured_json_when_account_scan_aggregation_fails(self) -> None:
        aggregator_instance = mock.Mock()
        aggregator_instance.collect_account_risk_payload.side_effect = trade_guard.AggregationInputError(
            "Spot request failed for spot.account.get_account_balance: {'code': -1000, 'msg': 'An unknown error occurred.'}"
        )

        with mock.patch.object(trade_guard, "TradeDataAggregator", return_value=aggregator_instance):
            stream = io.StringIO()
            with mock.patch.object(sys, "stdout", stream):
                exit_code = trade_guard.main(["account-scan", "--profile", "demo", "--market", "spot"])

        self.assertEqual(exit_code, 1)
        self.assertIn('"ok": false', stream.getvalue().lower())
        self.assertIn("spot.account.get_account_balance", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
