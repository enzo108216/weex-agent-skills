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

    def test_symbol_price_monitor_is_no_longer_supported(self) -> None:
        task_json = {
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

        with self.assertRaisesRegex(monitor.MonitorInputError, "unsupported task_type"):
            monitor.normalize_task(task_json, now_ms=1000)

        parser = monitor.build_parser()
        self.assertIsNone(parser._subparsers._group_actions[0].choices.get("build-price-order"))
        self.assertIsNone(parser._subparsers._group_actions[0].choices.get("submit-price-order"))
        self.assertIsNone(parser._subparsers._group_actions[0].choices.get("reconcile-price-order"))

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

    def test_confirmation_text_mentions_task_details_without_internal_flags(self) -> None:
        task_json = {
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

        text = monitor.render_confirmation_text(task_json, now_ms=1000)

        self.assertIn("自动化监控", text)
        self.assertIn("BTCUSDT", text)
        self.assertIn("多单", text)
        self.assertIn("未实现盈亏 > 50", text)
        self.assertIn("授权使用真实账户", text)
        self.assertNotIn("--confirm-monitor", text)
        self.assertNotIn("--confirm-live", text)

    def test_live_confirmation_text_includes_matched_position_snapshot(self) -> None:
        task_json = {
            "task_id": "mon_live_confirm",
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "frequency_seconds": 5,
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
                    "unrealized_pnl": "12.34",
                }
            ],
            "degraded_reasons": [],
            "partial": False,
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(monitor, "_run_json_command", return_value=account_payload) as runner:
                    prepared = monitor.prepare_live_confirmation(
                        task_json,
                        duration_seconds=3600,
                        now_ms=1000,
                    )
                tasks = monitor.load_tasks()
                events = monitor.load_events(task_json["task_id"])

        runner.assert_called_once()
        self.assertEqual(prepared["task"]["status"], "draft")
        self.assertEqual(prepared["task"]["live_position_confirmation"]["quantity"], "0.01")
        self.assertEqual(prepared["duration_seconds"], 3600.0)
        self.assertIn("已匹配真实持仓", prepared["confirmation_text"])
        self.assertIn("BTCUSDT 多单", prepared["confirmation_text"])
        self.assertIn("持仓数量: 0.01", prepared["confirmation_text"])
        self.assertIn("当前未实现盈亏: 12.34", prepared["confirmation_text"])
        self.assertIn("授权使用真实账户", prepared["confirmation_text"])
        self.assertNotIn("--confirm-live", prepared["confirmation_text"])
        self.assertNotIn("--confirm-monitor", prepared["confirmation_text"])
        self.assertEqual(tasks[0]["live_position_confirmation"]["quantity"], "0.01")
        self.assertEqual(events[0]["payload"]["live_position_confirmation"]["quantity"], "0.01")

    def test_live_confirmation_requires_matching_live_position(self) -> None:
        task_json = {
            "task_id": "mon_live_confirm_missing",
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
            "positions": [],
            "degraded_reasons": [],
            "partial": False,
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(monitor, "_run_json_command", return_value=account_payload):
                    with self.assertRaisesRegex(monitor.MonitorInputError, "live position"):
                        monitor.prepare_live_confirmation(task_json, duration_seconds=3600, now_ms=1000)
                tasks = monitor.load_tasks()

        self.assertEqual(tasks, [])

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
        changed = json.loads(json.dumps(task_json))
        changed["condition"]["threshold"] = "75"

        first = monitor.build_idempotency_key(task_json, "pnl-trigger")
        second = monitor.build_idempotency_key(task_json, "pnl-trigger")
        third = monitor.build_idempotency_key(changed, "pnl-trigger")

        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertTrue(first.startswith("monitor:mon_fixed:pnl-trigger:"))

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
        self.assertIn("授权使用真实账户", loop_result["iterations"][0]["results"][0]["thread_report"])
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
        self.assertEqual(delegate_plan["requires_live_account_authorization"], True)
        self.assertEqual(delegate_plan["mutating_request_submitted"], False)
        self.assertEqual(delegate_plan["close_order"]["side"], "SELL")
        self.assertTrue(delegate_plan["idempotency_key"].startswith("monitor:mon_delegate:pnl-trigger:"))

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

    def test_confirm_and_run_live_loop_uses_duration_seconds_from_one_confirmation(self) -> None:
        task_json = {
            "task_id": "mon_pnl_combined",
            "task_type": "position_pnl_monitor",
            "profile": "demo",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "frequency_seconds": 5,
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
                    "unrealized_pnl": "4.2",
                }
            ],
            "degraded_reasons": [],
            "partial": False,
        }

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_MONITOR_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(
                    monitor,
                    "_run_json_command",
                    return_value={
                        "positions": [
                            {
                                "symbol": "BTCUSDT",
                                "side": "LONG",
                                "quantity": "0.01",
                                "unrealized_pnl": "4.2",
                            }
                        ],
                        "degraded_reasons": [],
                        "partial": False,
                    },
                ):
                    prepared = monitor.prepare_live_confirmation(task_json, duration_seconds=11, now_ms=1000)
                with mock.patch.object(
                    monitor,
                    "_run_json_command",
                    side_effect=[
                        account_payload,
                        account_payload,
                        account_payload,
                    ],
                ) as runner:
                    result = monitor.confirm_and_run_live_loop(
                        prepared["task"],
                        confirm_monitor=True,
                        confirmation_token=prepared["confirmation_token"],
                        confirm_live=True,
                        duration_seconds=11,
                        sleep_seconds=0,
                        now_ms=2000,
                    )
                tasks = monitor.load_tasks()
                events = monitor.load_events(task_json["task_id"])

        self.assertEqual(result["confirmed_task"]["status"], "active")
        self.assertEqual(result["duration_seconds"], 11.0)
        self.assertEqual(result["loop_result"]["live"], True)
        self.assertEqual(result["loop_result"]["iterations_requested"], 3)
        self.assertEqual(result["loop_result"]["submitted_count"], 0)
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(tasks[0]["status"], "active")
        self.assertIn("task_confirmed", [event["event_type"] for event in events])
        self.assertNotIn("live_order_submitted", [event["event_type"] for event in events])

    def test_confirm_and_run_live_loop_requires_finite_duration_before_activation(self) -> None:
        task_json = {
            "task_id": "mon_pnl_combined_guard",
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
                prepared = monitor.prepare_confirmation(task_json, now_ms=1000)
                with mock.patch.object(monitor, "_run_json_command") as runner:
                    with self.assertRaisesRegex(monitor.MonitorInputError, "live position confirmation"):
                        monitor.confirm_and_run_live_loop(
                            prepared["task"],
                            confirm_monitor=True,
                            confirmation_token=prepared["confirmation_token"],
                            confirm_live=True,
                            duration_seconds=3600,
                            sleep_seconds=0,
                            now_ms=2000,
                        )
                tasks = monitor.load_tasks()
                events = monitor.load_events(task_json["task_id"])

        runner.assert_not_called()
        self.assertEqual(tasks[0]["status"], "draft")
        self.assertNotIn("task_confirmed", [event["event_type"] for event in events])

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
