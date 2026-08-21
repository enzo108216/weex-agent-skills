#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "examples" / "btc_ma_bot"
BOT_FILE = BOT_DIR / "btc_ma_bot.py"
DEFAULT_CONFIG = BOT_DIR / "config.json"


def load_bot_module():
    spec = importlib.util.spec_from_file_location("btc_ma_bot", BOT_FILE)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load btc_ma_bot")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def runnable_config(module, **overrides):
    values = {
        "symbol": "BTCUSDT",
        "poll_interval_seconds": 10,
        "direction": "both",
        "margin_amount_usdt": "10",
        "kline_interval": "1m",
        "fast_ma_period": 3,
        "slow_ma_period": 5,
        "profile": "test-account",
        "strategy_id": "strategy-test",
        "authorization_id": "authorization-test",
        "authorization_max_single_amount_u": "10",
        "authorization_max_total_amount_u": "100",
        "authorization_valid_hours": "24",
        "live_trading_enabled": True,
    }
    values.update(overrides)
    return module.BotConfig.from_mapping(values)


class FakeGateway:
    def __init__(self, *, positions=None, submit_result=None, authorization_result=None):
        self.positions = [] if positions is None else positions
        self.submit_result = submit_result or {"ok": True, "status": "ACCEPTED"}
        self.authorization_result = authorization_result or {
            "ok": True,
            "status": "ACTIVE",
            "authorization_id": "authorization-test",
            "next_action": "SUBMIT_ALLOWED",
        }
        self.authorization_calls = 0
        self.fetch_closes_calls = 0
        self.fetch_positions_calls = 0
        self.submit_calls = []

    def ensure_authorization(self, config):
        self.authorization_calls += 1
        return self.authorization_result

    def fetch_closes(self, config):
        self.fetch_closes_calls += 1
        return [Decimal(value) for value in ("1", "2", "3", "4", "8")]

    def fetch_positions(self, config):
        self.fetch_positions_calls += 1
        return self.positions

    def fetch_symbol_config(self, config):
        return {
            "symbol": "BTCUSDT",
            "marginType": "CROSSED",
            "crossLeverage": "5",
        }

    def fetch_contract_info(self, config):
        return {
            "symbol": "BTCUSDT",
            "quantityPrecision": 3,
            "contractVal": "0.001",
            "minOrderSize": "0.001",
            "maxOrderSize": "100",
            "marketOpenLimitSize": "10",
        }

    def fetch_price(self, config):
        return Decimal("100")

    def submit_market_order(self, config, signal, quantity):
        self.submit_calls.append((signal, quantity))
        return self.submit_result


class BtcMaBotTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(BOT_FILE.exists(), "btc_ma_bot.py has not been implemented")
        self.bot = load_bot_module()

    def test_default_config_is_offline_safe(self):
        config = self.bot.load_config(DEFAULT_CONFIG)

        self.assertEqual(config.poll_interval_seconds, 10)
        self.assertEqual(config.direction, "both")
        self.assertEqual(config.margin_amount_usdt, Decimal("20"))
        self.assertFalse(config.live_trading_enabled)
        self.assertEqual(config.profile, "")
        self.assertEqual(config.strategy_id, "")
        self.assertEqual(config.authorization_id, "")
        self.assertEqual(config.authorization_max_single_amount_u, Decimal("20"))
        self.assertEqual(config.authorization_max_total_amount_u, Decimal("100"))
        self.assertEqual(config.authorization_valid_hours, Decimal("24"))
        self.assertEqual(
            set(config.live_readiness_issues()),
            {"live_trading_enabled", "profile", "strategy_id", "authorization_id"},
        )

    def test_authorization_scope_configuration_rejects_invalid_limits(self):
        invalid_scopes = (
            {"authorization_max_single_amount_u": "0"},
            {
                "authorization_max_single_amount_u": "20",
                "authorization_max_total_amount_u": "10",
            },
            {"authorization_valid_hours": "720.1"},
            {"authorization_valid_hours": "0.0001"},
        )

        for overrides in invalid_scopes:
            with self.subTest(overrides=overrides):
                with self.assertRaises(self.bot.BotConfigError):
                    runnable_config(self.bot, **overrides)

    def test_integer_config_rejects_booleans_fractions_and_kline_limit_overflow(self):
        for overrides in (
            {"poll_interval_seconds": True},
            {"poll_interval_seconds": 10.9},
            {"fast_ma_period": 2.5},
            {"slow_ma_period": 1001},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(self.bot.BotConfigError):
                    runnable_config(self.bot, **overrides)

    def test_moving_average_signal_uses_current_window_without_waiting_for_cross(self):
        long_signal = self.bot.moving_average_signal(
            [Decimal(value) for value in ("1", "2", "3", "5", "8")], 2, 4
        )
        short_signal = self.bot.moving_average_signal(
            [Decimal(value) for value in ("8", "5", "3", "2", "1")], 2, 4
        )
        neutral_signal = self.bot.moving_average_signal(
            [Decimal("2")] * 5, 2, 4
        )

        self.assertEqual(long_signal, "LONG")
        self.assertEqual(short_signal, "SHORT")
        self.assertEqual(neutral_signal, "NEUTRAL")

    def test_direction_filter_only_allows_configured_side(self):
        self.assertTrue(self.bot.direction_allows("both", "LONG"))
        self.assertTrue(self.bot.direction_allows("long_only", "LONG"))
        self.assertFalse(self.bot.direction_allows("long_only", "SHORT"))
        self.assertTrue(self.bot.direction_allows("short_only", "SHORT"))
        self.assertFalse(self.bot.direction_allows("short_only", "LONG"))

    def test_quantity_uses_margin_leverage_price_contract_value_and_rounds_down(self):
        quantity = self.bot.calculate_quantity(
            margin_amount_usdt=Decimal("10"),
            leverage=Decimal("3"),
            price=Decimal("7"),
            contract_value=Decimal("0.001"),
            quantity_precision=2,
            minimum=Decimal("0.01"),
            maximum=Decimal("10"),
        )

        self.assertEqual(quantity, Decimal("4.28"))

    def test_nonzero_btc_position_blocks_submission(self):
        gateway = FakeGateway(
            positions=[{"symbol": "BTCUSDT", "side": "LONG", "size": "0.01"}]
        )

        result = self.bot.run_bot(
            runnable_config(self.bot),
            gateway,
            confirm_live=True,
            sleep_fn=lambda _seconds: None,
            max_cycles=1,
        )

        self.assertEqual(result, 0)
        self.assertEqual(gateway.submit_calls, [])

    def test_startup_authorization_failure_blocks_market_and_account_access(self):
        gateway = FakeGateway(
            authorization_result={
                "ok": True,
                "status": "AUTHORIZATION_REQUIRED",
                "authorization_id": None,
                "next_action": "WAIT_FOR_AUTHORIZATION",
            }
        )

        with self.assertRaisesRegex(RuntimeError, "authorization"):
            self.bot.run_bot(
                runnable_config(self.bot),
                gateway,
                confirm_live=True,
                max_cycles=1,
            )

        self.assertEqual(gateway.authorization_calls, 1)
        self.assertEqual(gateway.fetch_closes_calls, 0)
        self.assertEqual(gateway.submit_calls, [])

    def test_authorization_is_rechecked_before_first_trade_preparation(self):
        class ExpiringGateway(FakeGateway):
            def ensure_authorization(self, config):
                self.authorization_calls += 1
                if self.authorization_calls == 1:
                    return self.authorization_result
                return {
                    "ok": True,
                    "status": "AUTHORIZATION_REQUIRED",
                    "authorization_id": None,
                    "next_action": "WAIT_FOR_AUTHORIZATION",
                }

        gateway = ExpiringGateway()

        with self.assertRaisesRegex(RuntimeError, "authorization"):
            self.bot.run_bot(
                runnable_config(self.bot),
                gateway,
                confirm_live=True,
                max_cycles=1,
            )

        self.assertEqual(gateway.authorization_calls, 2)
        self.assertEqual(gateway.fetch_closes_calls, 1)
        self.assertEqual(gateway.fetch_positions_calls, 0)
        self.assertEqual(gateway.submit_calls, [])

    def test_authorization_id_and_submit_action_must_match_exactly(self):
        invalid_results = (
            {
                "ok": True,
                "status": "ACTIVE",
                "authorization_id": "another-authorization",
                "next_action": "SUBMIT_ALLOWED",
            },
            {
                "ok": True,
                "status": "ACTIVE",
                "authorization_id": "authorization-test",
                "next_action": "ENABLE_AUTO_TRADING_AFTER_RESTORE",
            },
        )

        for authorization_result in invalid_results:
            with self.subTest(authorization_result=authorization_result):
                gateway = FakeGateway(authorization_result=authorization_result)
                with self.assertRaisesRegex(RuntimeError, "authorization"):
                    self.bot.run_bot(
                        runnable_config(self.bot),
                        gateway,
                        confirm_live=True,
                        max_cycles=1,
                    )
                self.assertEqual(gateway.fetch_closes_calls, 0)
                self.assertEqual(gateway.submit_calls, [])

    def test_malformed_or_mismatched_position_rows_fail_closed(self):
        invalid_positions = (
            [{}],
            [{"symbol": "ETHUSDT", "size": "1"}],
            [{"symbol": "BTCUSDT"}],
            [{"symbol": "BTCUSDT", "size": "NaN"}],
        )

        for positions in invalid_positions:
            with self.subTest(positions=positions):
                gateway = FakeGateway(positions=positions)
                with self.assertRaisesRegex(RuntimeError, "position"):
                    self.bot.run_bot(
                        runnable_config(self.bot),
                        gateway,
                        confirm_live=True,
                        max_cycles=1,
                    )
                self.assertEqual(gateway.submit_calls, [])

    def test_review_required_submission_is_attempted_once_then_process_stops(self):
        gateway = FakeGateway(
            submit_result={"ok": False, "status": "REVIEW_REQUIRED"}
        )

        result = self.bot.run_bot(
            runnable_config(self.bot), gateway, confirm_live=True, sleep_fn=lambda _seconds: None
        )

        self.assertEqual(result, 2)
        self.assertEqual(gateway.submit_calls, [("LONG", Decimal("0.5"))])

    def test_submit_auto_uses_stdin_and_live_confirmation_flag(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok":true,"status":"ACCEPTED"}\n', stderr=""
        )
        with mock.patch.object(self.bot.subprocess, "run", return_value=completed) as run:
            gateway = self.bot.WeexCliGateway(
                python_executable="python-test",
                contract_cli=Path("/tmp/weex_contract_api.py"),
                auto_trade_cli=Path("/tmp/weex_auto_trade.py"),
            )
            result = gateway.submit_market_order(
                runnable_config(self.bot), "SHORT", Decimal("0.125")
            )

        self.assertEqual(result["status"], "ACCEPTED")
        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            [
                "python-test",
                "/tmp/weex_auto_trade.py",
                "submit-auto",
                "--input",
                "-",
                "--confirm-live",
            ],
        )
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["operation_key"], "transaction.place_order")
        self.assertEqual(
            request["orders"],
            [
                {
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "positionSide": "SHORT",
                    "type": "MARKET",
                    "quantity": "0.125",
                }
            ],
        )
        self.assertNotIn("api_key", request)
        self.assertNotIn("api_secret", request)

    def test_startup_authorization_uses_official_facade_scope_over_stdin(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "status": "ACTIVE",
                    "authorization_id": "authorization-test",
                    "next_action": "SUBMIT_ALLOWED",
                }
            ),
            stderr="",
        )
        with mock.patch.object(self.bot.subprocess, "run", return_value=completed) as run:
            gateway = self.bot.WeexCliGateway(
                python_executable="python-test",
                contract_cli=Path("/tmp/weex_contract_api.py"),
                auto_trade_cli=Path("/tmp/weex_auto_trade.py"),
            )
            self.assertTrue(hasattr(gateway, "ensure_authorization"))
            result = gateway.ensure_authorization(runnable_config(self.bot))

        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(
            run.call_args.args[0],
            [
                "python-test",
                "/tmp/weex_auto_trade.py",
                "ensure-authorization",
                "--input",
                "-",
            ],
        )
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(
            request,
            {
                "profile": "test-account",
                "strategy_id": "strategy-test",
                "trade_types": ["FUTURES"],
                "symbols": ["BTCUSDT"],
                "all_symbols": False,
                "max_single_amount": "10",
                "max_total_amount": "100",
                "valid_hours": "24",
            },
        )

    def test_kline_rows_are_sorted_by_open_time_before_signal_calculation(self):
        def row(open_time, close):
            return [open_time, "1", "1", "1", close, "1", open_time + 59_999, "1", 1, "1", "1"]

        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "result": [
                        row(120_000, "3"),
                        row(0, "1"),
                        row(60_000, "2"),
                    ],
                }
            ),
            stderr="",
        )
        gateway = self.bot.WeexCliGateway(
            run=lambda *_args, **_kwargs: completed,
            now_ms=lambda: 150_000,
        )

        closes = gateway.fetch_closes(
            runnable_config(self.bot, fast_ma_period=2, slow_ma_period=3)
        )

        self.assertEqual(closes, [Decimal("1"), Decimal("2"), Decimal("3")])

    def test_stale_incomplete_duplicate_or_nonpositive_klines_fail_closed(self):
        def row(open_time, close):
            return [open_time, "1", "1", "1", close, "1", open_time + 59_999, "1", 1, "1", "1"]

        invalid_rows = (
            [row(0, "1"), row(60_000, "2"), row(120_000, "3")],
            [[120_000, "1", "1", "1", "3"]],
            [row(120_000, "2"), row(120_000, "3")],
            [row(120_000, "0")],
        )
        for index, rows in enumerate(invalid_rows):
            with self.subTest(index=index):
                completed = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps({"ok": True, "result": rows}),
                    stderr="",
                )
                now = 1_000_000 if index == 0 else 150_000
                gateway = self.bot.WeexCliGateway(
                    run=lambda *_args, **_kwargs: completed,
                    now_ms=lambda: now,
                )
                with self.assertRaisesRegex(RuntimeError, "kline"):
                    gateway.fetch_closes(
                        runnable_config(self.bot, fast_ma_period=2, slow_ma_period=3)
                    )

    def test_malformed_position_payload_fails_closed(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"ok": True, "result": {"symbol": "BTCUSDT", "size": "0"}}),
            stderr="",
        )
        gateway = self.bot.WeexCliGateway(run=lambda *_args, **_kwargs: completed)

        with self.assertRaisesRegex(RuntimeError, "position result"):
            gateway.fetch_positions(runnable_config(self.bot))

    def test_contract_payload_without_explicit_success_fails_closed(self):
        for ok_value in (None, "missing"):
            with self.subTest(ok_value=ok_value):
                payload = {"result": []}
                if ok_value != "missing":
                    payload["ok"] = ok_value
                completed = subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=json.dumps(payload), stderr=""
                )
                gateway = self.bot.WeexCliGateway(
                    run=lambda *_args, **_kwargs: completed
                )
                with self.assertRaisesRegex(RuntimeError, "success"):
                    gateway.fetch_positions(runnable_config(self.bot))

    def test_mismatched_symbol_config_fails_closed(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "result": [
                        {"symbol": "ETHUSDT", "marginType": "CROSSED", "crossLeverage": "5"}
                    ],
                }
            ),
            stderr="",
        )
        gateway = self.bot.WeexCliGateway(run=lambda *_args, **_kwargs: completed)

        with self.assertRaisesRegex(RuntimeError, "symbol configuration"):
            gateway.fetch_symbol_config(runnable_config(self.bot))

    def test_wrong_stale_or_nonpositive_price_fails_closed(self):
        invalid_prices = (
            {"symbol": "ETHUSDT", "price": "100", "time": 100_000},
            {"symbol": "BTCUSDT", "price": "100", "time": 1_000},
            {"symbol": "BTCUSDT", "price": "0", "time": 100_000},
        )
        for result in invalid_prices:
            with self.subTest(result=result):
                completed = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps({"ok": True, "result": result}),
                    stderr="",
                )
                gateway = self.bot.WeexCliGateway(
                    run=lambda *_args, **_kwargs: completed,
                    now_ms=lambda: 100_000,
                )
                with self.assertRaisesRegex(RuntimeError, "price"):
                    gateway.fetch_price(runnable_config(self.bot))

    def test_price_freshness_does_not_expand_with_polling_interval(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "result": {"symbol": "BTCUSDT", "price": "100", "time": 50_000},
                }
            ),
            stderr="",
        )
        gateway = self.bot.WeexCliGateway(
            run=lambda *_args, **_kwargs: completed,
            now_ms=lambda: 100_000,
        )

        with self.assertRaisesRegex(RuntimeError, "stale"):
            gateway.fetch_price(
                runnable_config(self.bot, poll_interval_seconds=3_600)
            )

    def test_missing_order_sizing_metadata_fails_before_submission(self):
        for field in (
            "contractVal",
            "quantityPrecision",
            "minOrderSize",
            "maxOrderSize",
            "marketOpenLimitSize",
        ):
            with self.subTest(field=field):
                gateway = FakeGateway()
                facts = gateway.fetch_contract_info(None)
                del facts[field]
                gateway.fetch_contract_info = lambda _config, facts=facts: facts
                with self.assertRaises(self.bot.BotConfigError):
                    self.bot.run_bot(
                        runnable_config(self.bot),
                        gateway,
                        confirm_live=True,
                        max_cycles=1,
                    )
                self.assertEqual(gateway.submit_calls, [])

    def test_missing_or_unknown_margin_mode_fails_before_submission(self):
        for symbol_config in (
            {"symbol": "BTCUSDT", "crossLeverage": "5"},
            {"symbol": "BTCUSDT", "marginType": "UNKNOWN", "crossLeverage": "5"},
        ):
            with self.subTest(symbol_config=symbol_config):
                gateway = FakeGateway()
                gateway.fetch_symbol_config = lambda _config, value=symbol_config: value
                with self.assertRaises(self.bot.BotConfigError):
                    self.bot.run_bot(
                        runnable_config(self.bot),
                        gateway,
                        confirm_live=True,
                        max_cycles=1,
                    )
                self.assertEqual(gateway.submit_calls, [])

    def test_gateway_defaults_point_to_trader_scripts(self):
        gateway = self.bot.WeexCliGateway()

        self.assertEqual(gateway.contract_cli, ROOT / "scripts" / "weex_contract_api.py")
        self.assertEqual(gateway.auto_trade_cli, ROOT / "scripts" / "weex_auto_trade.py")

    def test_programmatic_run_also_requires_explicit_live_confirmation(self):
        gateway = FakeGateway()

        result = self.bot.run_bot(runnable_config(self.bot), gateway, max_cycles=1)

        self.assertEqual(result, 1)
        self.assertEqual(gateway.submit_calls, [])


if __name__ == "__main__":
    unittest.main()
