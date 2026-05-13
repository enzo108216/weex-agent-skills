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

    def test_run_loop_dry_run_is_one_shot_and_returns_thread_report(self) -> None:
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
        positions_sequence = [
            [
                {
                    "symbol": "ETHUSDT",
                    "side": "SHORT",
                    "size": "0.2",
                    "unrealizePnl": "31.5",
                }
            ],
            [
                {
                    "symbol": "ETHUSDT",
                    "side": "SHORT",
                    "size": "0.2",
                    "unrealizePnl": "50",
                }
            ],
        ]
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                confirmed = monitor.confirm_task(task_json, confirm_monitor=True, now_ms=1000)
                loop_result = monitor.run_loop_dry_run(
                    positions_sequence,
                    iterations=2,
                    sleep_seconds=0,
                    now_ms=2000,
                )
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertEqual(loop_result["dry_run"], True)
        self.assertEqual(loop_result["iterations_requested"], 2)
        self.assertEqual(loop_result["iterations_completed"], 2)
        self.assertEqual(loop_result["triggered_count"], 1)
        self.assertEqual(tasks[0]["status"], "triggered")
        self.assertIn("thread_report", loop_result["iterations"][0]["results"][0])
        self.assertIn("requires --confirm-live", loop_result["iterations"][0]["results"][0]["thread_report"])
        self.assertEqual(
            [event["event_type"] for event in events],
            ["task_confirmed", "dry_run_evaluated", "dry_run_triggered"],
        )

    def test_trigger_result_builds_live_delegate_plan_without_submitting(self) -> None:
        task_json = {
            "task_id": "mon_delegate",
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "ETHUSDT",
            "position_side": "LONG",
            "condition": {
                "metric": "unrealized_pnl",
                "operator": ">",
                "threshold": "25",
            },
            "action": {
                "type": "market_close",
                "target": "LONG",
            },
            "callback": {"type": "current_thread"},
        }
        result = monitor.evaluate_pnl_task(
            task_json,
            [{"symbol": "ETHUSDT", "side": "LONG", "size": "0.3", "unrealizePnl": "31.5"}],
        )

        delegate_plan = monitor.build_live_delegate_plan(task_json, result, purpose="pnl-trigger")

        self.assertEqual(delegate_plan["delegate_skill"], "weex-trader-skill")
        self.assertEqual(delegate_plan["requires_confirm_live"], True)
        self.assertEqual(delegate_plan["mutating_request_submitted"], False)
        self.assertEqual(delegate_plan["close_order"]["side"], "SELL")
        self.assertTrue(delegate_plan["idempotency_key"].startswith("monitor:mon_delegate:pnl-trigger:"))

    def test_submit_price_order_requires_confirm_live(self) -> None:
        task_json = {
            "task_id": "mon_price_live",
            "task_type": "symbol_price_monitor",
            "profile": "demo",
            "symbol": "ETHUSDT",
            "position_side": "SHORT",
            "condition": {
                "metric": "last_price",
                "operator": "<",
                "threshold": "2500",
            },
            "action": {
                "type": "market_close",
                "target": "SHORT",
                "quantity": "0.2",
            },
            "callback": {"type": "current_thread"},
        }

        with mock.patch.object(monitor, "_run_json_command") as runner:
            with self.assertRaisesRegex(monitor.MonitorInputError, "confirm-live"):
                monitor.submit_price_order(task_json, confirm_live=False, now_ms=2000)

        runner.assert_not_called()

    def test_submit_price_order_uses_trader_tp_sl_guard_and_records_submission(self) -> None:
        task_json = {
            "task_id": "mon_price_live",
            "task_type": "symbol_price_monitor",
            "profile": "demo",
            "symbol": "ETHUSDT",
            "position_side": "SHORT",
            "condition": {
                "metric": "last_price",
                "operator": "<",
                "threshold": "2500",
            },
            "action": {
                "type": "market_close",
                "target": "SHORT",
                "quantity": "0.2",
            },
            "callback": {"type": "current_thread"},
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                confirmed = monitor.confirm_task(task_json, confirm_monitor=True, now_ms=1000)
                with mock.patch.object(
                    monitor,
                    "_run_json_command",
                    side_effect=[
                        {
                            "intent_id": "intent-tpsl",
                            "risk_signature": "sig-tpsl",
                            "tp_sl_order": monitor.build_price_tp_sl_order_body(task_json),
                        },
                        {"ok": True, "algoId": "7001", "clientAlgoId": "mon_price_live"},
                    ],
                ) as runner:
                    result = monitor.submit_price_order(confirmed, confirm_live=True, now_ms=2000)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["exchange_response"]["algoId"], "7001")
        self.assertIn("thread_report", result)
        self.assertEqual(tasks[0]["status"], "submitted")
        self.assertIn("live_price_order_submitted", [event["event_type"] for event in events])
        first_command = runner.call_args_list[0].args[0]
        second_command = runner.call_args_list[1].args[0]
        self.assertIn("preview-tp-sl", first_command)
        self.assertIn("confirm-tp-sl", second_command)
        self.assertIn("--confirm-live", second_command)

    def test_run_live_once_executes_triggered_pnl_close_through_trader_guard(self) -> None:
        task_json = {
            "task_id": "mon_pnl_live",
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "condition": {
                "metric": "unrealized_pnl",
                "operator": ">",
                "threshold": "50",
            },
            "action": {
                "type": "market_close",
                "target": "LONG",
            },
            "callback": {"type": "current_thread"},
        }
        account_payload = {
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "quantity": "0.01",
                    "unrealized_pnl": "51.2",
                }
            ],
            "degraded_reasons": [],
            "partial": False,
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                confirmed = monitor.confirm_task(task_json, confirm_monitor=True, now_ms=1000)
                with mock.patch.object(
                    monitor,
                    "_run_json_command",
                    side_effect=[
                        account_payload,
                        account_payload,
                        {"intent_id": "intent-close", "risk_signature": "sig-close"},
                        {"ok": True, "orderId": "9001", "clientOrderId": "monitor_mon_pnl_live"},
                    ],
                ) as runner:
                    results = monitor.run_live_once(confirm_live=True, now_ms=2000)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["result"]["triggered"])
        self.assertEqual(results[0]["status"], "completed")
        self.assertEqual(results[0]["exchange_response"]["orderId"], "9001")
        self.assertIn("Live close order submitted", results[0]["thread_report"])
        self.assertEqual(tasks[0]["status"], "completed")
        self.assertIn("live_order_submitted", [event["event_type"] for event in events])
        preview_command = runner.call_args_list[2].args[0]
        confirm_command = runner.call_args_list[3].args[0]
        self.assertIn("preview-order", preview_command)
        self.assertIn("confirm-order", confirm_command)
        self.assertIn("--confirm-live", confirm_command)
        preview_order_json = preview_command[preview_command.index("--order-json") + 1]
        preview_order = json.loads(preview_order_json)
        self.assertEqual(preview_order["side"], "SELL")
        self.assertEqual(preview_order["position_side"], "LONG")
        self.assertEqual(preview_order["new_client_order_id"], "monitor_mon_pnl_live")


if __name__ == "__main__":
    unittest.main()
