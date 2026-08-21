#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
BOT_FILE = ROOT / "examples" / "btc_ma_bot" / "btc_spot_interval_bot.py"


def load_bot_module():
    spec = importlib.util.spec_from_file_location("btc_spot_interval_bot", BOT_FILE)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load btc_spot_interval_bot")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeGateway:
    def __init__(self, auth=None, balance="100"):
        self.auth = auth or {
            "ok": True,
            "status": "ACTIVE",
            "authorization_id": "auth-spot",
            "next_action": "SUBMIT_ALLOWED",
        }
        self.balance = Decimal(balance)
        self.calls = []

    def ensure_authorization(self, config):
        self.calls.append(("ensure_authorization", config))
        return self.auth

    def fetch_product(self, config):
        self.calls.append(("fetch_product", config))
        return {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "enableTrade": True,
            "stepSize": "0.000001",
            "minTradeAmount": "0.000001",
            "maxTradeAmount": "100",
            "marketBuyLimitSize": "100",
        }

    def fetch_book_ticker(self, config):
        self.calls.append(("fetch_book_ticker", config))
        return {"symbol": "BTCUSDT", "askPrice": "70000"}

    def fetch_quote_balance(self, config):
        self.calls.append(("fetch_quote_balance", config))
        return self.balance

    def submit_market_buy(self, config, quantity, cycle_id):
        self.calls.append(("submit_market_buy", quantity, cycle_id))
        return {"ok": True, "status": "ACCEPTED", "legs": [{"status": "ACCEPTED"}]}


class SpotIntervalBotTests(unittest.TestCase):
    def setUp(self):
        self.bot = load_bot_module()

    def config(self, **overrides):
        values = {
            "symbol": "BTCUSDT",
            "poll_interval_seconds": 60,
            "quote_amount_usdt": "19.9",
            "profile": "生产密钥1",
            "strategy_id": "strategy-spot",
            "authorization_id": "auth-spot",
            "authorization_max_single_amount_u": "20.1",
            "authorization_max_total_amount_u": "100",
            "authorization_valid_hours": "24",
            "live_trading_enabled": True,
            "log_file": "spot-interval.log",
        }
        values.update(overrides)
        return self.bot.BotConfig.from_mapping(values)

    def test_quantity_rounds_down_to_spot_step_size(self):
        quantity = self.bot.calculate_market_buy_quantity(
            quote_amount=Decimal("19.9"),
            ask_price=Decimal("70000"),
            step_size=Decimal("0.000001"),
            minimum=Decimal("0.000001"),
            maximum=Decimal("100"),
        )
        self.assertEqual(quantity, Decimal("0.000284"))

    def test_config_requires_one_minute_polling_and_positive_scope(self):
        self.assertEqual(self.config().poll_interval_seconds, 60)
        with self.assertRaises(self.bot.BotConfigError):
            self.config(poll_interval_seconds=59)
        with self.assertRaises(self.bot.BotConfigError):
            self.config(quote_amount_usdt="0")

    def test_startup_authorization_must_be_spot_and_exactly_active(self):
        gateway = FakeGateway(
            auth={
                "ok": True,
                "status": "ACTIVE",
                "authorization_id": "other",
                "next_action": "SUBMIT_ALLOWED",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "authorization"):
            self.bot.run_bot(self.config(), gateway, confirm_live=True, max_cycles=1)
        self.assertEqual([call[0] for call in gateway.calls], ["ensure_authorization"])

    def test_one_cycle_checks_balance_and_submits_market_buy(self):
        gateway = FakeGateway()
        result = self.bot.run_bot(
            self.config(),
            gateway,
            confirm_live=True,
            max_cycles=1,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(result, 0)
        self.assertEqual(gateway.calls[-1][0], "submit_market_buy")
        self.assertEqual(gateway.calls[-1][1], Decimal("0.000284"))

    def test_insufficient_quote_balance_blocks_submission(self):
        gateway = FakeGateway(balance="10")
        result = self.bot.run_bot(
            self.config(),
            gateway,
            confirm_live=True,
            max_cycles=1,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(result, 1)
        self.assertFalse(any(call[0] == "submit_market_buy" for call in gateway.calls))


if __name__ == "__main__":
    unittest.main()
