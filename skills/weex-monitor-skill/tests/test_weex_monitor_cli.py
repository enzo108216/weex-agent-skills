#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SCRIPT = SCRIPTS / "weex_monitor_cli.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import weex_monitor_cli as monitor  # noqa: E402


class MonitorTaskTests(unittest.TestCase):
    def _prepare_and_confirm(
        self,
        task_json: dict[str, object],
        *,
        now_ms: int = 1000,
    ) -> dict[str, object]:
        prepared = monitor.prepare_confirmation(task_json, now_ms=now_ms)
        return monitor.confirm_task(
            prepared["task"],
            confirm_monitor=True,
            confirmation_token=prepared["confirmation_token"],
            now_ms=now_ms + 1,
        )

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
                with self.assertRaisesRegex(monitor.MonitorInputError, "confirmation-token"):
                    monitor.confirm_task(task_json, confirm_monitor=True, now_ms=1000)
                prepared = monitor.prepare_confirmation(task_json, now_ms=1000)
                confirmed = monitor.confirm_task(
                    prepared["task"],
                    confirm_monitor=True,
                    confirmation_token=prepared["confirmation_token"],
                    now_ms=1001,
                )
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertEqual(confirmed["status"], "active")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], confirmed["task_id"])
        self.assertEqual(
            [event["event_type"] for event in events],
            ["task_confirmation_rendered", "task_confirmed"],
        )

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
                confirmed = self._prepare_and_confirm(task_json, now_ms=1000)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

                self.assertTrue(monitor.db_path().exists())

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "active")
        self.assertEqual([event["event_type"] for event in events], ["task_confirmation_rendered", "task_confirmed"])
        self.assertEqual(events[-1]["payload"]["status"], "active")

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
                confirmed = self._prepare_and_confirm(task_json, now_ms=1000)
                cancelled = monitor.cancel_task(confirmed["task_id"], now_ms=2000)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(tasks[0]["status"], "cancelled")
        self.assertEqual(
            [event["event_type"] for event in events],
            ["task_confirmation_rendered", "task_confirmed", "task_cancelled"],
        )

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

        self.assertIn("自动化监控", text)
        self.assertIn("BTCUSDT", text)
        self.assertIn("SHORT", text)
        self.assertIn("last_price < 65000", text)
        self.assertIn("--confirm-monitor", text)
        self.assertIn("weex-trader-skill", text)
        self.assertIn("--confirm-live", text)

    def test_confirm_text_cli_persists_draft_and_confirm_requires_token(self) -> None:
        task_json = {
            "task_id": "mon_cli_confirm",
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
            env = {**os.environ, "WEEX_MONITOR_SKILL_HOME": tempdir}
            rendered = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "confirm-text",
                    "--task-json",
                    json.dumps(task_json),
                ],
                text=True,
                capture_output=True,
                check=True,
                env=env,
            )
            rendered_payload = json.loads(rendered.stdout)
            self.assertIn("自动化监控", rendered_payload["confirmation_text"])
            self.assertIn("confirmation_token", rendered_payload)
            self.assertEqual(rendered_payload["task"]["status"], "draft")

            rejected = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "confirm",
                    "--task-json",
                    json.dumps(rendered_payload["task"]),
                    "--confirm-monitor",
                ],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("confirmation-token", rejected.stderr)

            confirmed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "confirm",
                    "--task-json",
                    json.dumps(rendered_payload["task"]),
                    "--confirm-monitor",
                    "--confirmation-token",
                    rendered_payload["confirmation_token"],
                ],
                text=True,
                capture_output=True,
                check=True,
                env=env,
            )
            confirmed_payload = json.loads(confirmed.stdout)

        self.assertEqual(confirmed_payload["status"], "active")

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
                confirmed = self._prepare_and_confirm(task_json, now_ms=1000)
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
            ["task_confirmation_rendered", "task_confirmed", "dry_run_evaluated", "dry_run_triggered"],
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
                confirmed = self._prepare_and_confirm(task_json, now_ms=1000)
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
            ["task_confirmation_rendered", "task_confirmed", "dry_run_evaluated", "dry_run_triggered"],
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

    def test_submit_price_order_rejects_forged_active_task_without_persisted_confirmation(self) -> None:
        forged_active_task = {
            "task_id": "mon_price_forged",
            "task_type": "symbol_price_monitor",
            "profile": "demo",
            "symbol": "ETHUSDT",
            "position_side": "SHORT",
            "status": "active",
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
                with mock.patch.object(
                    monitor,
                    "_run_json_command",
                    return_value={"positions": [], "degraded_reasons": [], "partial": False},
                ) as runner:
                    with self.assertRaisesRegex(monitor.MonitorInputError, "confirmed active monitor task"):
                        monitor.submit_price_order(forged_active_task, confirm_live=True, now_ms=2000)

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
                confirmed = self._prepare_and_confirm(task_json, now_ms=1000)
                with mock.patch.object(
                    monitor,
                    "_run_json_command",
                    side_effect=[
                        {
                            "positions": [
                                {
                                    "symbol": "ETHUSDT",
                                    "side": "SHORT",
                                    "quantity": "0.2",
                                }
                            ],
                            "degraded_reasons": [],
                            "partial": False,
                        },
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
        third_command = runner.call_args_list[2].args[0]
        self.assertIn("collect-account-risk", first_command)
        self.assertIn("preview-tp-sl", second_command)
        self.assertIn("confirm-tp-sl", third_command)
        self.assertIn("--confirm-live", third_command)

    def test_submit_price_order_revalidates_live_position_before_trader_guard(self) -> None:
        task_json = {
            "task_id": "mon_price_size_guard",
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
        account_payload = {
            "positions": [
                {
                    "symbol": "ETHUSDT",
                    "side": "SHORT",
                    "quantity": "0.1",
                }
            ],
            "degraded_reasons": [],
            "partial": False,
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                confirmed = self._prepare_and_confirm(task_json, now_ms=1000)
                with mock.patch.object(
                    monitor,
                    "_run_json_command",
                    return_value=account_payload,
                ) as runner:
                    result = monitor.submit_price_order(confirmed, confirm_live=True, now_ms=2000)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["result"]["reason"], "live_position_size_too_small")
        self.assertEqual(tasks[0]["status"], "review_required")
        self.assertEqual(runner.call_count, 1)
        self.assertIn("live_price_order_failed", [event["event_type"] for event in events])

    def test_submit_price_order_claims_task_and_rejects_duplicate_submission(self) -> None:
        task_json = {
            "task_id": "mon_price_duplicate",
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
                confirmed = self._prepare_and_confirm(task_json, now_ms=1000)
                with mock.patch.object(
                    monitor,
                    "_run_json_command",
                    side_effect=[
                        {
                            "positions": [
                                {
                                    "symbol": "ETHUSDT",
                                    "side": "SHORT",
                                    "quantity": "0.2",
                                }
                            ],
                            "degraded_reasons": [],
                            "partial": False,
                        },
                        {
                            "intent_id": "intent-tpsl",
                            "risk_signature": "sig-tpsl",
                            "tp_sl_order": monitor.build_price_tp_sl_order_body(task_json),
                        },
                        {"ok": True, "algoId": "7001", "clientAlgoId": "mon_price_duplicate"},
                        {
                            "positions": [
                                {
                                    "symbol": "ETHUSDT",
                                    "side": "SHORT",
                                    "quantity": "0.2",
                                }
                            ],
                            "degraded_reasons": [],
                            "partial": False,
                        },
                        {
                            "intent_id": "intent-tpsl-2",
                            "risk_signature": "sig-tpsl-2",
                            "tp_sl_order": monitor.build_price_tp_sl_order_body(task_json),
                        },
                        {"ok": True, "algoId": "7002", "clientAlgoId": "mon_price_duplicate"},
                    ],
                ) as runner:
                    first = monitor.submit_price_order(confirmed, confirm_live=True, now_ms=2000)
                    with self.assertRaisesRegex(monitor.MonitorInputError, "confirmed active monitor task"):
                        monitor.submit_price_order(confirmed, confirm_live=True, now_ms=2001)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertEqual(first["status"], "submitted")
        self.assertEqual(tasks[0]["status"], "submitted")
        self.assertEqual(runner.call_count, 3)
        self.assertIn("live_execution_claimed", [event["event_type"] for event in events])
        self.assertIn("live_price_order_submitted", [event["event_type"] for event in events])

    def test_run_live_once_rejects_forged_active_task_without_persisted_confirmation(self) -> None:
        forged_active_task = {
            "task_id": "mon_pnl_forged",
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "status": "active",
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

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                task = monitor.normalize_task(forged_active_task, now_ms=1000)
                task["status"] = "active"
                with monitor._connect() as conn:
                    monitor._upsert_task(conn, task, updated_at_ms=1000)
                with mock.patch.object(monitor, "_run_json_command") as runner:
                    results = monitor.run_live_once(confirm_live=True, now_ms=2000)
                tasks = monitor.load_tasks()
                events = monitor.load_events(task["task_id"])

        self.assertEqual(results[0]["status"], "review_required")
        self.assertEqual(results[0]["result"]["reason"], "missing_monitor_confirmation")
        self.assertEqual(tasks[0]["status"], "review_required")
        runner.assert_not_called()
        self.assertIn("live_order_failed", [event["event_type"] for event in events])

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
                confirmed = self._prepare_and_confirm(task_json, now_ms=1000)
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

    def test_run_live_loop_uses_confirm_live_and_reuses_active_frequency(self) -> None:
        task_json = {
            "task_id": "mon_pnl_loop",
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "frequency_seconds": 3,
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
                self._prepare_and_confirm(task_json, now_ms=1000)
                with mock.patch.object(
                    monitor,
                    "_run_json_command",
                    side_effect=[
                        account_payload,
                        account_payload,
                        {"intent_id": "intent-close", "risk_signature": "sig-close"},
                        {"ok": True, "orderId": "9001", "clientOrderId": "monitor_mon_pnl_loop"},
                    ],
                ):
                    loop_result = monitor.run_live_loop(
                        confirm_live=True,
                        iterations=2,
                        sleep_seconds=0,
                        now_ms=2000,
                    )
                tasks = monitor.load_tasks()

        self.assertEqual(loop_result["live"], True)
        self.assertEqual(loop_result["iterations_requested"], 2)
        self.assertEqual(loop_result["iterations_completed"], 2)
        self.assertEqual(loop_result["submitted_count"], 1)
        self.assertEqual(loop_result["effective_sleep_seconds"], 0)
        self.assertEqual(tasks[0]["status"], "completed")

    def test_live_delegate_failure_is_recorded_and_moves_task_to_review_required(self) -> None:
        task_json = {
            "task_id": "mon_pnl_fail",
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
                confirmed = self._prepare_and_confirm(task_json, now_ms=1000)
                with mock.patch.object(
                    monitor,
                    "_run_json_command",
                    side_effect=[
                        account_payload,
                        account_payload,
                        monitor.MonitorInputError("preview failed"),
                    ],
                ):
                    results = monitor.run_live_once(confirm_live=True, now_ms=2000)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertEqual(results[0]["status"], "review_required")
        self.assertEqual(results[0]["result"]["reason"], "live_order_preview_failed")
        self.assertEqual(tasks[0]["status"], "review_required")
        self.assertIn("live_order_failed", [event["event_type"] for event in events])

    def test_execution_claim_is_atomic_for_active_task(self) -> None:
        task_json = {
            "task_id": "mon_claim",
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

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                confirmed = self._prepare_and_confirm(task_json, now_ms=1000)
                first_claim = monitor.claim_task_for_execution(confirmed, now_ms=2000)
                second_claim = monitor.claim_task_for_execution(confirmed, now_ms=2001)
                tasks = monitor.load_tasks()
                events = monitor.load_events(confirmed["task_id"])

        self.assertTrue(first_claim)
        self.assertFalse(second_claim)
        self.assertEqual(tasks[0]["status"], "executing")
        self.assertIn("live_execution_claimed", [event["event_type"] for event in events])


if __name__ == "__main__":
    unittest.main()
