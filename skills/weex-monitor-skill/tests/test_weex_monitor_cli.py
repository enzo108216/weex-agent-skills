#!/usr/bin/env python3
from __future__ import annotations

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

import weex_monitor_cli as monitor  # noqa: E402


class MonitorTaskTests(unittest.TestCase):
    def test_price_condition_builds_directional_tp_sl_market_order(self) -> None:
        task = monitor.normalize_task(
            {
                "task_type": "symbol_price_monitor",
                "profile": "demo",
                "symbol": "BTCUSDT",
                "position_side": "LONG",
                "condition": {
                    "metric": "last_price",
                    "operator": ">",
                    "threshold": "70000",
                },
                "action": {
                    "type": "market_close",
                    "target": "LONG",
                    "quantity": "0.01",
                },
                "callback": {"type": "current_thread"},
            },
            now_ms=1000,
        )

        body = monitor.build_price_tp_sl_order_body(task)

        self.assertEqual(body["symbol"], "BTCUSDT")
        self.assertEqual(body["planType"], "TAKE_PROFIT")
        self.assertEqual(body["triggerPrice"], "70000")
        self.assertEqual(body["executePrice"], "0")
        self.assertEqual(body["quantity"], "0.01")
        self.assertEqual(body["positionSide"], "LONG")
        self.assertEqual(body["triggerPriceType"], "CONTRACT_PRICE")
        self.assertNotIn("closePositions", json.dumps(body))

    def test_position_pnl_monitor_defaults_to_five_seconds_and_rejects_too_fast(self) -> None:
        base_task = {
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "ETHUSDT",
            "position_side": "SHORT",
            "condition": {
                "metric": "unrealized_pnl",
                "operator": ">=",
                "threshold": "50",
            },
            "action": {
                "type": "market_close",
                "target": "SHORT",
            },
            "callback": {"type": "current_thread"},
        }

        task = monitor.normalize_task(base_task, now_ms=1000)

        self.assertEqual(task["frequency_seconds"], 5)

        too_fast = dict(base_task)
        too_fast["frequency_seconds"] = 2
        with self.assertRaisesRegex(monitor.MonitorInputError, "frequency_seconds"):
            monitor.normalize_task(too_fast, now_ms=1000)

    def test_pnl_trigger_builds_directional_market_close_plan_without_live_execution(self) -> None:
        task = monitor.normalize_task(
            {
                "task_type": "position_pnl_monitor",
                "profile": "demo",
                "symbol": "ETHUSDT",
                "position_side": "SHORT",
                "condition": {
                    "metric": "unrealized_pnl",
                    "operator": ">",
                    "threshold": "25",
                },
                "action": {
                    "type": "market_close",
                    "target": "SHORT",
                },
                "callback": {"type": "current_thread"},
            },
            now_ms=1000,
        )
        positions = [
            {
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "size": "0.2",
                "unrealizePnl": "31.5",
            }
        ]

        result = monitor.evaluate_pnl_task(task, positions)

        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason"], "condition_matched")
        self.assertEqual(result["execution_delegate"], "weex-trader-skill")
        self.assertEqual(
            result["close_order"],
            {
                "symbol": "ETHUSDT",
                "side": "BUY",
                "position_side": "SHORT",
                "order_type": "MARKET",
                "quantity": "0.2",
            },
        )

    def test_confirm_requires_explicit_monitor_confirmation_before_active_task_is_saved(self) -> None:
        task_json = {
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "condition": {
                "metric": "unrealized_pnl",
                "operator": ">",
                "threshold": "10",
            },
            "action": {
                "type": "market_close",
                "target": "LONG",
            },
            "callback": {"type": "current_thread"},
        }
        with tempfile.TemporaryDirectory() as tempdir:
            with self.assertRaisesRegex(monitor.MonitorInputError, "confirm-monitor"):
                with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                    monitor.confirm_task(task_json, confirm_monitor=False, now_ms=1000)
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                confirmed = monitor.confirm_task(task_json, confirm_monitor=True, now_ms=1000)
                tasks = monitor.load_tasks()

        self.assertEqual(confirmed["status"], "active")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], confirmed["task_id"])

    def test_normalized_looking_task_is_still_revalidated(self) -> None:
        task_json = {
            "task_id": "mon_review_regression",
            "execution_delegate": "weex-trader-skill",
            "task_type": "symbol_price_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "BOGUS",
            "condition": {
                "metric": "last_price",
                "operator": "BAD",
                "threshold": "70000",
            },
            "action": {
                "type": "market_close",
                "target": "BOGUS",
                "quantity": "0.01",
            },
            "callback": {"type": "current_thread"},
        }

        with self.assertRaisesRegex(monitor.MonitorInputError, "position_side|operator"):
            monitor.build_price_tp_sl_order_body(task_json)

    def test_revalidated_task_preserves_task_id_for_client_algo_id(self) -> None:
        task_json = {
            "task_id": "mon_review_regression",
            "execution_delegate": "weex-trader-skill",
            "task_type": "symbol_price_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "SHORT",
            "condition": {
                "metric": "last_price",
                "operator": "<",
                "threshold": "65000",
            },
            "action": {
                "type": "market_close",
                "target": "SHORT",
                "quantity": "0.01",
            },
            "callback": {"type": "current_thread"},
        }

        body = monitor.build_price_tp_sl_order_body(task_json)

        self.assertEqual(body["clientAlgoId"], "mon_review_regression")
        self.assertEqual(body["planType"], "TAKE_PROFIT")

    def test_price_monitor_rejects_zero_threshold_or_quantity(self) -> None:
        base_task = {
            "task_type": "symbol_price_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "condition": {
                "metric": "last_price",
                "operator": ">",
                "threshold": "70000",
            },
            "action": {
                "type": "market_close",
                "target": "LONG",
                "quantity": "0.01",
            },
            "callback": {"type": "current_thread"},
        }

        zero_threshold = json.loads(json.dumps(base_task))
        zero_threshold["condition"]["threshold"] = "0"
        with self.assertRaisesRegex(monitor.MonitorInputError, "condition.threshold"):
            monitor.normalize_task(zero_threshold, now_ms=1000)

        zero_quantity = json.loads(json.dumps(base_task))
        zero_quantity["action"]["quantity"] = "0"
        with self.assertRaisesRegex(monitor.MonitorInputError, "action.quantity"):
            monitor.normalize_task(zero_quantity, now_ms=1000)

    def test_explicit_pnl_close_quantity_must_be_positive(self) -> None:
        task_json = {
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "ETHUSDT",
            "position_side": "SHORT",
            "condition": {
                "metric": "unrealized_pnl",
                "operator": ">",
                "threshold": "25",
            },
            "action": {
                "type": "market_close",
                "target": "SHORT",
                "quantity": "0",
            },
            "callback": {"type": "current_thread"},
        }

        with self.assertRaisesRegex(monitor.MonitorInputError, "action.quantity"):
            monitor.normalize_task(task_json, now_ms=1000)

    def test_monitor_market_scope_is_futures_only(self) -> None:
        task_json = {
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "market": "spot",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "condition": {
                "metric": "unrealized_pnl",
                "operator": ">",
                "threshold": "10",
            },
            "action": {
                "type": "market_close",
                "target": "LONG",
            },
            "callback": {"type": "current_thread"},
        }

        with self.assertRaisesRegex(monitor.MonitorInputError, "market"):
            monitor.normalize_task(task_json, now_ms=1000)

    def test_confirm_persists_to_sqlite_and_writes_event(self) -> None:
        task_json = {
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "condition": {
                "metric": "unrealized_pnl",
                "operator": ">",
                "threshold": "10",
            },
            "action": {
                "type": "market_close",
                "target": "LONG",
            },
            "callback": {"type": "current_thread"},
        }
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                confirmed = monitor.confirm_task(task_json, confirm_monitor=True, now_ms=1000)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

                self.assertTrue(monitor.db_path().exists())

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "active")
        self.assertEqual(events[0]["event_type"], "task_confirmed")
        self.assertEqual(events[0]["payload"]["status"], "active")

    def test_cancel_updates_sqlite_status_and_writes_event(self) -> None:
        task_json = {
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "condition": {
                "metric": "unrealized_pnl",
                "operator": ">",
                "threshold": "10",
            },
            "action": {
                "type": "market_close",
                "target": "LONG",
            },
            "callback": {"type": "current_thread"},
        }
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                confirmed = monitor.confirm_task(task_json, confirm_monitor=True, now_ms=1000)
                cancelled = monitor.cancel_task(confirmed["task_id"], now_ms=2000)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(tasks[0]["status"], "cancelled")
        self.assertEqual([event["event_type"] for event in events], ["task_confirmed", "task_cancelled"])

    def test_confirmation_text_mentions_task_details_and_live_boundary(self) -> None:
        task_json = {
            "task_type": "symbol_price_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "SHORT",
            "condition": {
                "metric": "last_price",
                "operator": "<",
                "threshold": "65000",
            },
            "action": {
                "type": "market_close",
                "target": "SHORT",
                "quantity": "0.01",
            },
            "callback": {"type": "current_thread"},
        }

        text = monitor.render_confirmation_text(task_json, now_ms=1000)

        self.assertIn("BTCUSDT", text)
        self.assertIn("SHORT", text)
        self.assertIn("last_price < 65000", text)
        self.assertIn("--confirm-monitor", text)
        self.assertIn("weex-trader-skill", text)
        self.assertIn("--confirm-live", text)

    def test_idempotency_key_is_deterministic_and_changes_with_condition(self) -> None:
        task_json = {
            "task_id": "mon_fixed",
            "task_type": "symbol_price_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "condition": {
                "metric": "last_price",
                "operator": ">",
                "threshold": "70000",
            },
            "action": {
                "type": "market_close",
                "target": "LONG",
                "quantity": "0.01",
            },
            "callback": {"type": "current_thread"},
        }
        changed = json.loads(json.dumps(task_json))
        changed["condition"]["threshold"] = "71000"

        first = monitor.build_idempotency_key(task_json, "price-plan")
        second = monitor.build_idempotency_key(task_json, "price-plan")
        third = monitor.build_idempotency_key(changed, "price-plan")

        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertTrue(first.startswith("monitor:mon_fixed:price-plan:"))

    def test_run_once_dry_run_records_trigger_without_live_execution(self) -> None:
        task_json = {
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "ETHUSDT",
            "position_side": "SHORT",
            "condition": {
                "metric": "unrealized_pnl",
                "operator": ">",
                "threshold": "25",
            },
            "action": {
                "type": "market_close",
                "target": "SHORT",
            },
            "callback": {"type": "current_thread"},
        }
        positions = [
            {
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "size": "0.2",
                "unrealizePnl": "31.5",
            }
        ]
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                confirmed = monitor.confirm_task(task_json, confirm_monitor=True, now_ms=1000)
                results = monitor.run_once_dry_run(positions, now_ms=2000)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["result"]["triggered"])
        self.assertEqual(results[0]["dry_run"], True)
        self.assertEqual(tasks[0]["status"], "triggered")
        self.assertEqual(results[0]["result"]["execution_delegate"], "weex-trader-skill")
        self.assertIn("idempotency_key", results[0])
        self.assertNotIn("exchange_response", results[0])
        self.assertEqual(
            [event["event_type"] for event in events],
            ["task_confirmed", "dry_run_evaluated", "dry_run_triggered"],
        )


if __name__ == "__main__":
    unittest.main()
