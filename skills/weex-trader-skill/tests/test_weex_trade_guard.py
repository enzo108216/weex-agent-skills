#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
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
    persist: bool = False,
    profile_name: str = "demo",
    trading_mode: str = "live",
    risk_facts: dict[str, object] | None = None,
) -> dict[str, object]:
    direct_tool = (
        "trade_place_tp_sl"
        if legacy_key == "transaction.place_tp_sl_order"
        else "trade_place_order"
    )
    record = {
        "version": 2,
        "intent_id": "intent-v2",
        "product": product,
        "legacy_key": legacy_key,
        "direct_tool": direct_tool,
        "prepared_mutation": {
            "tool_name": direct_tool,
            "prepared_artifact_digest": "sha256:" + "6" * 64,
        },
        "prepared_artifact_digest": "sha256:" + "6" * 64,
        "execution_binding_digest": "sha256:" + "5" * 64,
        "expires_at": expires_at,
        "profile_id": "profile-id",
        "spot_base_url": "https://api-spot.weex.com",
        "futures_base_url": "https://api-contract.weex.com",
    }
    if persist:
        intent_state.save_unclaimed_intent(
            v2_manual_intent(
                tool_name=direct_tool,
                market="spot" if product == "spot" else "futures",
                trading_mode=trading_mode,
                profile_name=profile_name,
                expires_at=expires_at,
                risk_facts=risk_facts,
            ),
            now_ms=100,
        )
    return record


def v2_manual_intent(
    *,
    intent_id: str = "intent-v2",
    tool_name: str = "trade_place_order",
    market: str = "futures",
    trading_mode: str = "live",
    profile_name: str = "demo",
    expires_at: int = 10_000,
    risk_facts: dict[str, object] | None = None,
) -> dict[str, object]:
    action = tool_name.removeprefix("trade_")
    canonical_arguments = {
        "market": market,
        "trading_mode": trading_mode,
        "symbol": "BTCUSDT",
    }
    prepared = {
        "contract_version": "1",
        "tool_name": tool_name,
        "action": action,
        "market": market,
        "trading_mode": trading_mode,
        "canonical_arguments": canonical_arguments,
        "canonical_arguments_digest": "sha256:" + "1" * 64,
        "risk_facts_digest": "sha256:" + "2" * 64,
        "route_fingerprint": "sha256:" + "3" * 64,
        "credential_fingerprint": "credential-fingerprint",
        "precondition": {
            "policy": "STATIC",
            "digest": "sha256:" + "4" * 64,
            "observed_at": 100,
            "expires_at": expires_at,
            "facts": {},
        },
        "risk_facts": dict(risk_facts or {}),
        "reconciliation_template": {
            "strategy": "manual_review",
            "reason": "missing_unique_operation_identity",
            "selector": {"market": market, "symbol": "BTCUSDT"},
            "identity": {"symbol": "BTCUSDT"},
            "completeness": "unprovable",
        },
        "expires_at": expires_at,
        "execution_binding_digest": "sha256:" + "5" * 64,
        "prepared_artifact_digest": "sha256:" + "6" * 64,
    }
    return intent_state.build_unclaimed_intent(
        prepared_mutation=prepared,
        profile_binding={
            "profile_id": "profile-id",
            "profile_name": profile_name,
            "principal_id": "principal-id",
            "account_binding_id": "account-id",
            "spot_base_url": "https://api-spot.weex.com",
            "futures_base_url": "https://api-contract.weex.com",
            "route_fingerprint": "sha256:" + "3" * 64,
            "credential_fingerprint": "credential-fingerprint",
        },
        contract_versions={
            "package": "0.3.3",
            "tool_contract": "2",
            "prepare": "1",
            "intent": "2",
            "grant": "2",
            "boundary_contract_digest": (
                "sha256:23c0fa218f2dcb3024b3067f8456d17e3f968ca63a7d9409e7b5a36ed9b0fc53"
            ),
        },
        intent_id=intent_id,
        created_at=100,
    )


def write_raw_legacy_intent(payload: dict[str, object]) -> None:
    path = intent_state.intent_path()
    path.write_text(
        json.dumps(payload, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        path.chmod(0o600)

class TradeGuardTests(unittest.TestCase):
    def test_obsolete_manual_submission_helpers_are_removed(self) -> None:
        import weex_auto_trade_runtime as runtime_module

        for name in (
            "_submit_order",
            "_confirm_saved_trade_preview",
            "_submit_live_order",
            "_submit_live_auto_fallback_order",
            "_submit_live_tp_sl_order",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(trade_guard, name))
        self.assertFalse(
            hasattr(runtime_module.OfficialAutoTradeRuntime, "manual_submitter")
        )

    def test_prepare_order_routes_exact_position_close_without_submitting(self) -> None:
        fake_contract_api = mock.Mock()
        close_endpoint = mock.Mock()
        fake_contract_api.find_endpoint_key_by_doc_suffix.return_value = (
            "transaction.close_positions"
        )
        fake_contract_api.ENDPOINTS = {
            "transaction.close_positions": close_endpoint,
        }
        fake_contract_api.normalize_contract_trade_symbol.side_effect = str.upper
        fake_client = mock.Mock()
        fresh_account = {
            "partial": False,
            "degraded_reasons": [],
            "positions": [
                {
                    "position_id": "1001",
                    "symbol": "ETHUSDT",
                    "position_side": "LONG",
                    "quantity": 0.001,
                    "position_mode": "SEPARATED",
                }
            ],
        }

        with mock.patch.object(
            trade_guard,
            "_build_contract_client",
            return_value=(fake_contract_api, fake_client),
        ):
            _api, returned_client, endpoint, body = (
                trade_guard._prepare_order_trade_request(
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
                    risk_payload=fresh_account,
                )
            )

        self.assertIs(returned_client, fake_client)
        self.assertIs(endpoint, close_endpoint)
        self.assertEqual(body, {"symbol": "ETHUSDT", "positionId": 1001})
        fake_client.preview_trade_endpoint.assert_not_called()
        fake_client.confirm_trade_preview.assert_not_called()

    def test_prepare_order_normalizes_futures_and_spot_without_submitting(self) -> None:
        futures_endpoint = mock.Mock()
        futures_api = mock.Mock()
        futures_api.ENDPOINTS = {"transaction.place_order": futures_endpoint}
        futures_api.normalize_contract_trade_symbol.side_effect = str.upper
        futures_client = mock.Mock()
        with mock.patch.object(
            trade_guard,
            "_build_contract_client",
            return_value=(futures_api, futures_client),
        ):
            _api, _client, endpoint, futures_body = (
                trade_guard._prepare_order_trade_request(
                    market="futures",
                    profile_name="main",
                    trading_mode="live",
                    raw_order={
                        "symbol": "btcusdt",
                        "side": "SELL",
                        "positionSide": "LONG",
                        "type": "MARKET",
                        "quantity": "0.01",
                        "newClientOrderId": "futures-client-1",
                    },
                )
            )
        self.assertIs(endpoint, futures_endpoint)
        self.assertEqual(futures_body["symbol"], "BTCUSDT")
        self.assertEqual(futures_body["positionSide"], "LONG")
        self.assertEqual(futures_body["newClientOrderId"], "futures-client-1")
        futures_client.preview_trade_endpoint.assert_not_called()

        spot_endpoint = mock.Mock()
        spot_api = mock.Mock()
        spot_api.ENDPOINTS = {"place": spot_endpoint}
        spot_api.find_endpoint_key_by_doc_suffix.return_value = "place"
        spot_api.normalize_spot_symbol.side_effect = str.upper
        spot_client = mock.Mock()
        with mock.patch.object(
            trade_guard,
            "_build_spot_client",
            return_value=(spot_api, spot_client),
        ):
            _api, _client, endpoint, spot_body = (
                trade_guard._prepare_order_trade_request(
                    market="spot",
                    profile_name="main",
                    trading_mode="live",
                    raw_order={
                        "symbol": "btcusdt",
                        "side": "BUY",
                        "type": "LIMIT",
                        "quantity": "0.01",
                        "price": "65000",
                        "timeInForce": "GTC",
                        "new_client_order_id": "spot-client-1",
                    },
                )
            )
        self.assertIs(endpoint, spot_endpoint)
        self.assertEqual(spot_body["symbol"], "BTCUSDT")
        self.assertEqual(spot_body["timeInForce"], "GTC")
        self.assertEqual(spot_body["newClientOrderId"], "spot-client-1")
        spot_client.preview_trade_endpoint.assert_not_called()

    def test_prepare_demo_order_maps_official_simulated_symbol_without_submitting(self) -> None:
        endpoint = mock.Mock()
        fake_api = mock.Mock()
        fake_api.ENDPOINTS = {"sim.transaction.place_order": endpoint}
        fake_api.normalize_contract_demo_trade_symbol.return_value = "BTCSUSDT"
        fake_client = mock.Mock()
        with mock.patch.object(
            trade_guard,
            "_build_contract_client",
            return_value=(fake_api, fake_client),
        ):
            _api, _client, returned_endpoint, body = (
                trade_guard._prepare_order_trade_request(
                    market="futures",
                    profile_name="main",
                    trading_mode="demo",
                    raw_order={
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "position_side": "LONG",
                        "order_type": "MARKET",
                        "quantity": "0.001",
                        "new_client_order_id": "demo-client-1",
                    },
                )
            )
        self.assertIs(returned_endpoint, endpoint)
        self.assertEqual(body["symbol"], "BTCSUSDT")
        fake_client.preview_trade_endpoint.assert_not_called()

    def test_prepare_order_rejects_missing_required_fields(self) -> None:
        cases = (
            (
                "futures",
                {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "1"},
                "positionSide",
            ),
            (
                "futures",
                {"symbol": "BTCUSDT", "side": "SELL", "positionSide": "LONG", "quantity": "1"},
                "type",
            ),
            (
                "spot",
                {"symbol": "BTCUSDT", "side": "BUY", "quantity": "1"},
                "type",
            ),
        )
        for market, raw_order, message in cases:
            with self.subTest(market=market, message=message):
                with self.assertRaisesRegex(
                    trade_guard.AggregationInputError,
                    message,
                ):
                    trade_guard._prepare_order_trade_request(
                        market=market,
                        profile_name="main",
                        trading_mode="live",
                        raw_order=raw_order,
                    )

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

    def test_preview_order_uses_facade_persisted_intent_and_returns_analysis_output(self) -> None:
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
                    side_effect=lambda **_kwargs: fake_mcp_trade_preview(
                        expires_at=600_000,
                        persist=True,
                        risk_facts={
                            "market": "futures",
                            "trading_mode": "live",
                            "symbol": "BTCUSDT",
                            "side": "BUY",
                            "order_type": "MARKET",
                            "quantity": "0.01",
                        },
                    ),
                ):
                    with mock.patch.object(trade_guard.analysis, "analyze_order_risk", return_value=analysis_payload):
                        stream = io.StringIO()
                        with mock.patch.object(sys, "stdout", stream):
                            exit_code = trade_guard.cmd_preview_order(args, now_ms=1000)
                        saved_intent = intent_state.load_manual_intent(now_ms=1000)

        self.assertEqual(exit_code, 0)
        self.assertEqual(saved_intent["intent_version"], 2)
        self.assertNotIn("risk_signature", saved_intent)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["intent_id"], "intent-v2")
        self.assertEqual(payload["risk_signature"], "sha256:" + "6" * 64)
        self.assertEqual(
            payload["mutation_preview"]["risk"],
            saved_intent["risk_facts"],
        )
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
                    side_effect=lambda **_kwargs: fake_mcp_trade_preview(
                        expires_at=600_000,
                        persist=True,
                        profile_name="demo-profile",
                        trading_mode="demo",
                    ),
                ):
                    with mock.patch.object(trade_guard.analysis, "analyze_order_risk", return_value=analysis_payload):
                        stream = io.StringIO()
                        with mock.patch.object(sys, "stdout", stream):
                            exit_code = trade_guard.cmd_preview_order(args, now_ms=1000)
                        saved_intent = intent_state.load_manual_intent(now_ms=1000)

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(saved_intent["trading_mode"], "demo")
        self.assertEqual(
            saved_intent["profile_binding"]["profile_name"],
            "demo-profile",
        )
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
                    side_effect=lambda **_kwargs: fake_mcp_trade_preview(
                        expires_at=600_000,
                        persist=True,
                    ),
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
                    side_effect=lambda **_kwargs: fake_mcp_trade_preview(
                        expires_at=600_000,
                        persist=True,
                    ),
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
                write_raw_legacy_intent(
                    {
                        "intent_id": "legacy-unknown",
                        "risk_signature": "legacy-risk",
                        "trade_status": "UNKNOWN",
                        "mcp_trade_confirmation_started_at": 2000,
                    }
                )
                with mock.patch.object(trade_guard, "TradeDataAggregator") as aggregator_mock:
                    with self.assertRaisesRegex(
                        trade_guard.AggregationInputError,
                        "unresolved",
                    ):
                        trade_guard.cmd_preview_order(args, now_ms=700000)
                remaining = intent_state.load_manual_intent(now_ms=700000)

        aggregator_mock.assert_not_called()
        self.assertEqual(remaining["state"], "REVIEW_REQUIRED")

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

    def test_preview_tp_sl_uses_facade_persisted_intent_and_returns_confirmation_fields(self) -> None:
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
                    "quantity": "0",
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
                    side_effect=lambda **_kwargs: fake_mcp_trade_preview(
                        legacy_key="transaction.place_tp_sl_order",
                        expires_at=600_000,
                        persist=True,
                        risk_facts={"quantity_mode": "full_position"},
                    ),
                ):
                    with mock.patch.object(trade_guard.analysis, "analyze_account_risk", return_value=analysis_payload):
                        stream = io.StringIO()
                        with mock.patch.object(sys, "stdout", stream):
                            exit_code = trade_guard.cmd_preview_tp_sl(args, now_ms=1000)
                        saved_intent = intent_state.load_manual_intent(now_ms=1000)

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(saved_intent["intent_version"], 2)
        self.assertEqual(saved_intent["tool_name"], "trade_place_tp_sl")
        self.assertEqual(payload["intent_id"], "intent-v2")
        self.assertEqual(payload["risk_signature"], "sha256:" + "6" * 64)
        self.assertEqual(payload["mutation_preview"]["risk"], saved_intent["risk_facts"])
        self.assertIn("risk_signature", payload)
        self.assertEqual(payload["user_environment_prefix"], "当前交易环境：真实盘")
        self.assertEqual(payload["user_confirmation"]["reply_text"], "确认")
        self.assertIn("全部仓位", payload["user_confirmation"]["reply_instruction"])
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

    def test_v2_confirm_order_uses_only_intent_id_and_prepared_digest(self) -> None:
        args = mock.Mock(
            intent_id="intent-v2",
            risk_signature="sha256:" + "6" * 64,
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            language="en",
            pretty=False,
        )
        client = mock.Mock()
        client.confirm_trade_intent.return_value = {
            "ok": True,
            "data": {"status": "accepted"},
            "outcome": {
                "class": "accepted",
                "write_attempted": True,
                "grant_disposition": "accepted",
            },
            "trade_status": "ACCEPTED",
            "review_required": False,
        }
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(
                os.environ,
                {"WEEX_TRADER_SKILL_HOME": tempdir},
                clear=False,
            ):
                intent_state.save_unclaimed_intent(
                    v2_manual_intent(),
                    now_ms=100,
                )
                stream = io.StringIO()
                with (
                    mock.patch.object(
                        trade_guard,
                        "_build_contract_client",
                        return_value=(mock.Mock(), client),
                    ),
                    mock.patch.object(sys, "stdout", stream),
                ):
                    exit_code = trade_guard.cmd_confirm_order(args, now_ms=200)
        self.assertEqual(exit_code, 0)
        client.confirm_trade_intent.assert_called_once_with(
            intent_id="intent-v2",
            prepared_artifact_digest="sha256:" + "6" * 64,
        )
        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["user_environment_prefix"], "Current trading mode: real trading")

    def test_v2_confirm_tp_sl_uses_same_durable_intent_kernel(self) -> None:
        args = mock.Mock(
            intent_id="intent-tpsl",
            risk_signature="sha256:" + "6" * 64,
            trading_mode="live",
            confirm_live=True,
            confirm_demo=False,
            language="zh",
            pretty=False,
        )
        client = mock.Mock()
        client.confirm_trade_intent.return_value = {
            "ok": False,
            "error": {"code": "unknown", "message": "uncertain"},
            "outcome": {
                "class": "unknown",
                "write_attempted": True,
                "grant_disposition": "review_required",
            },
            "trade_status": "REVIEW_REQUIRED",
            "review_required": True,
        }
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(
                os.environ,
                {"WEEX_TRADER_SKILL_HOME": tempdir},
                clear=False,
            ):
                intent_state.save_unclaimed_intent(
                    v2_manual_intent(
                        intent_id="intent-tpsl",
                        tool_name="trade_place_tp_sl",
                    ),
                    now_ms=100,
                )
                stream = io.StringIO()
                with (
                    mock.patch.object(
                        trade_guard,
                        "_build_contract_client",
                        return_value=(mock.Mock(), client),
                    ),
                    mock.patch.object(sys, "stdout", stream),
                ):
                    exit_code = trade_guard.cmd_confirm_tp_sl(args, now_ms=200)
        self.assertEqual(exit_code, 1)
        client.confirm_trade_intent.assert_called_once()
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["status"], "REVIEW_REQUIRED")
        self.assertTrue(payload["review_required"])

    def test_confirm_mutation_supports_all_14_direct_tools_by_durable_identity(self) -> None:
        direct_tools = {
            "trade_place_order",
            "trade_cancel_order",
            "trade_update_leverage",
            "trade_set_margin_mode",
            "trade_place_batch_orders",
            "trade_cancel_batch_orders",
            "trade_cancel_open_orders",
            "trade_place_conditional_order",
            "trade_cancel_conditional_order",
            "trade_place_tp_sl",
            "trade_modify_tp_sl",
            "trade_close_positions",
            "trade_adjust_position_margin",
            "trade_set_auto_append_margin",
        }
        for tool_name in sorted(direct_tools):
            with self.subTest(tool_name=tool_name):
                intent = {
                    "intent_version": 2,
                    "intent_id": "intent-v2",
                    "state": "UNCLAIMED",
                    "tool_name": tool_name,
                    "market": "futures",
                    "trading_mode": "live",
                    "prepared_artifact_digest": "sha256:" + "6" * 64,
                    "profile_binding": {"profile_name": "main"},
                    "legacy": None,
                }
                args = mock.Mock(
                    intent_id="intent-v2",
                    risk_signature="sha256:" + "6" * 64,
                    trading_mode="live",
                    confirm_live=True,
                    confirm_demo=False,
                    language="en",
                    pretty=False,
                )
                client = mock.Mock()
                client.confirm_trade_intent.return_value = {
                    "ok": True,
                    "data": {"status": "accepted"},
                    "outcome": {
                        "class": "accepted",
                        "write_attempted": True,
                        "grant_disposition": "accepted",
                    },
                    "trade_status": "ACCEPTED",
                    "review_required": False,
                }
                with (
                    mock.patch.object(
                        trade_guard,
                        "load_manual_intent",
                        return_value=intent,
                    ),
                    mock.patch.object(
                        trade_guard,
                        "_build_contract_client",
                        return_value=(mock.Mock(), client),
                    ),
                    mock.patch.object(trade_guard, "_output_json"),
                ):
                    code = trade_guard.cmd_confirm_mutation(args, now_ms=200)
                self.assertEqual(code, 0)
                client.confirm_trade_intent.assert_called_once_with(
                    intent_id="intent-v2",
                    prepared_artifact_digest="sha256:" + "6" * 64,
                )

    def test_dynamic_scope_notice_uses_execution_time_semantics_only_when_flagged(self) -> None:
        notice = trade_guard._dynamic_scope_notice(
            {
                "scope": "all",
                "order_kind": "normal",
                "observed_count": 3,
                "observed_at": 1_000,
                "dynamic_scope": True,
            },
            action="cancel_open_orders",
            language="en",
        )
        self.assertIn("execution time", notice)
        self.assertIn("not a snapshot", notice)
        self.assertIn("3", notice)
        self.assertIsNone(
            trade_guard._dynamic_scope_notice(
                {"scope": "position", "position_id": "1"},
                action="close_positions",
                language="en",
            )
        )

    def test_nondefault_manual_ttl_is_rejected_before_risk_or_mcp_work(self) -> None:
        args = mock.Mock(
            profile="demo",
            market="futures",
            trading_mode="live",
            language="en",
            order_json="{}",
            ttl_seconds=60,
            pretty=False,
        )
        stream = io.StringIO()
        with (
            mock.patch.object(trade_guard, "TradeDataAggregator") as aggregator,
            mock.patch.object(
                trade_guard,
                "_preview_manual_order_trade",
            ) as preview,
            mock.patch.object(sys, "stdout", stream),
        ):
            exit_code = trade_guard.cmd_preview_order(args, now_ms=100)
        self.assertEqual(exit_code, 1)
        self.assertIn("intent_ttl_unsupported", stream.getvalue())
        aggregator.assert_not_called()
        preview.assert_not_called()

    def test_reconcile_command_accepts_only_intent_id_and_uses_facade(self) -> None:
        args = mock.Mock(
            intent_id="intent-v2",
            language="en",
            pretty=False,
        )
        client = mock.Mock()
        client.reconcile_trade_intent.return_value = {
            "ok": False,
            "status": None,
            "error": {"code": "manual_review_required"},
            "trade_status": "REVIEW_REQUIRED",
            "review_required": True,
        }
        intent = v2_manual_intent()
        intent["state"] = "REVIEW_REQUIRED"
        intent["confirmed_at"] = 150
        intent["grant"] = {
            "grant_id": "grant-1",
            "state": "REVIEW_REQUIRED",
            "principal_id": "principal-id",
            "account_binding_id": "account-id",
            "credential_fingerprint": "credential-fingerprint",
            "tool_name": "trade_place_order",
            "prepared_artifact_digest": "sha256:" + "6" * 64,
            "execution_binding_digest": "sha256:" + "5" * 64,
            "issued_at": 120,
            "expires_at": 10_000,
            "claim_id": "claim-1",
            "terminal_state": "REVIEW_REQUIRED",
            "outcome_digest": "sha256:" + "9" * 64,
        }
        intent["reconciliation"]["status"] = "MANUAL_REVIEW_REQUIRED"
        intent["reconciliation"]["observed_at"] = 160
        intent["reconciliation"]["evidence_digest"] = "sha256:" + "8" * 64
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(
                os.environ,
                {"WEEX_TRADER_SKILL_HOME": tempdir},
                clear=False,
            ):
                intent_state._atomic_write_json(intent_state.intent_path(), intent)
                stream = io.StringIO()
                with (
                    mock.patch.object(
                        trade_guard,
                        "_build_contract_client",
                        return_value=(mock.Mock(), client),
                    ),
                    mock.patch.object(sys, "stdout", stream),
                ):
                    exit_code = trade_guard.cmd_reconcile_intent(args, now_ms=200)
        self.assertEqual(exit_code, 1)
        client.reconcile_trade_intent.assert_called_once_with(
            intent_id="intent-v2"
        )

    def test_legacy_reconcile_is_stable_and_never_uses_network(self) -> None:
        for started, expected_status in (
            (False, "LEGACY_INVALIDATED"),
            (True, "MANUAL_REVIEW_REQUIRED"),
        ):
            with self.subTest(started=started):
                with tempfile.TemporaryDirectory() as tempdir:
                    with mock.patch.dict(
                        os.environ,
                        {"WEEX_TRADER_SKILL_HOME": tempdir},
                        clear=False,
                    ):
                        raw = {
                            "intent_id": f"legacy-{started}",
                            "risk_signature": "legacy",
                        }
                        if started:
                            raw["mcp_trade_confirmation_started_at"] = 100
                        write_raw_legacy_intent(raw)
                        migrated = intent_state.load_manual_intent(now_ms=200)
                        args = mock.Mock(
                            intent_id=migrated["intent_id"],
                            language="en",
                            pretty=False,
                        )
                        stream = io.StringIO()
                        with (
                            mock.patch.object(
                                trade_guard,
                                "_build_contract_client",
                            ) as contract_client,
                            mock.patch.object(
                                trade_guard,
                                "_build_spot_client",
                            ) as spot_client,
                            mock.patch.object(sys, "stdout", stream),
                        ):
                            exit_code = trade_guard.cmd_reconcile_intent(
                                args,
                                now_ms=300,
                            )
                self.assertEqual(exit_code, 1)
                self.assertEqual(
                    json.loads(stream.getvalue())["status"],
                    expected_status,
                )
                contract_client.assert_not_called()
                spot_client.assert_not_called()

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
