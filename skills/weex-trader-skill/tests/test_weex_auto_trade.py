#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
MODULE_PATH = ROOT / "scripts" / "weex_auto_trade_state.py"
AMOUNT_MODULE_PATH = ROOT / "scripts" / "weex_auto_trade_amount.py"
CLI_PATH = ROOT / "scripts" / "weex_auto_trade.py"
NOTIFY_MODULE_PATH = ROOT / "scripts" / "weex_auto_trade_notify.py"


def load_state_module():
    if not MODULE_PATH.exists():
        raise AssertionError("weex_auto_trade_state.py has not been implemented")
    spec = importlib.util.spec_from_file_location("weex_auto_trade_state", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load weex_auto_trade_state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_amount_module():
    if not AMOUNT_MODULE_PATH.exists():
        raise AssertionError("weex_auto_trade_amount.py has not been implemented")
    spec = importlib.util.spec_from_file_location("weex_auto_trade_amount", AMOUNT_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load weex_auto_trade_amount.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_notify_module():
    if not NOTIFY_MODULE_PATH.exists():
        raise AssertionError("weex_auto_trade_notify.py has not been implemented")
    spec = importlib.util.spec_from_file_location("weex_auto_trade_notify", NOTIFY_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load weex_auto_trade_notify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cli_module():
    spec = importlib.util.spec_from_file_location("weex_auto_trade", CLI_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load weex_auto_trade.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AutoTradeStateSchemaTests(unittest.TestCase):
    def test_windows_state_storage_fails_closed_until_owner_only_dacl_is_supported(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            state = state_module.AutoTradeState(Path(tempdir) / "state" / "state.sqlite3")
            with patch.object(state_module.os, "name", "nt"):
                with self.assertRaisesRegex(
                    state_module.StateConflictError,
                    "Windows owner-only DACL enforcement is not supported",
                ):
                    state.initialize()

    def test_empty_database_initializes_six_table_schema_with_owner_only_permissions(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir) / "auto-trade"
            db_path = state_dir / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()

            with closing(sqlite3.connect(db_path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]

            self.assertEqual(
                tables,
                {
                    "strategies",
                    "authorization_requests",
                    "authorizations",
                    "authorization_usage",
                    "auto_trade_orders",
                    "authorization_events",
                },
            )
            self.assertEqual(user_version, state_module.CURRENT_SCHEMA_VERSION)
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(db_path.stat().st_mode), 0o600)

    def test_health_checks_fail_closed_without_repairing_permissions_or_invalid_decimals(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state" / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            self.assertTrue(hasattr(state, "health_check"), "state health checks are not implemented")
            healthy = state.health_check(full=True)
            self.assertEqual(healthy["status"], "HEALTHY")
            self.assertEqual(healthy["user_version"], state_module.CURRENT_SCHEMA_VERSION)
            self.assertEqual(healthy["foreign_key_violations"], 0)

            db_path.chmod(0o644)
            with self.assertRaises(state_module.StateConflictError):
                state_module.AutoTradeState(db_path).initialize()
            self.assertEqual(stat.S_IMODE(db_path.stat().st_mode), 0o644)

            db_path.chmod(0o600)
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="health", distribution="official"
            )
            now = datetime(2026, 8, 16, 11, 0, tzinfo=UTC)
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "10",
                    "max_total_amount": "100",
                },
                now=now,
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=now,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "UPDATE authorizations SET accepted_amount_u = 'not-a-decimal' WHERE authorization_id = ?",
                    (authorization["authorization_id"],),
                )
                connection.commit()

            with self.assertRaises(state_module.StateConflictError):
                state.health_check(full=True)

    def test_adjacent_schema_migration_rolls_back_and_unknown_versions_fail_closed(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "state"
            root.mkdir(mode=0o700)
            db_path = root / "state.sqlite3"
            with closing(sqlite3.connect(db_path)) as connection:
                connection.executescript(state_module.SCHEMA_V1)
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            db_path.chmod(0o600)

            self.assertTrue(hasattr(state_module, "MIGRATION_REGISTRY"), "migration registry is not implemented")
            self.assertIn(1, state_module.MIGRATION_REGISTRY)
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    state_module.CURRENT_SCHEMA_VERSION,
                )

            failing_db = Path(tempdir) / "failing" / "state.sqlite3"
            failing_state = state_module.AutoTradeState(failing_db)
            failing_state.initialize()
            with closing(sqlite3.connect(failing_db)) as connection:
                connection.execute("PRAGMA user_version = 1")
                connection.commit()
            original_migration = state_module.MIGRATION_REGISTRY[1]
            state_module.MIGRATION_REGISTRY[1] = lambda connection: (_ for _ in ()).throw(
                RuntimeError("injected migration failure")
            )
            try:
                with self.assertRaises(state_module.StateConflictError):
                    failing_state.initialize()
            finally:
                state_module.MIGRATION_REGISTRY[1] = original_migration
            with closing(sqlite3.connect(failing_db)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

            unknown_db = Path(tempdir) / "unknown" / "state.sqlite3"
            unknown_state = state_module.AutoTradeState(unknown_db)
            unknown_state.initialize()
            with closing(sqlite3.connect(unknown_db)) as connection:
                connection.execute("PRAGMA user_version = 999")
                connection.commit()
            with self.assertRaises(state_module.StateConflictError):
                unknown_state.initialize()

    def test_populated_v2_database_migrates_and_legacy_group_replays_without_binding_digest(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state" / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="legacy-replay", distribution="official"
            )
            now = datetime(2026, 8, 16, 11, 0, tzinfo=UTC)
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "10",
                    "max_total_amount": "100",
                },
                now=now,
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=now,
            )
            state.prepare_submission_group(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="legacy-v2",
                request_fingerprint="a" * 64,
                legs=[
                    {
                        "leg_id": "leg-0",
                        "leg_index": 0,
                        "leg_type": "PRIMARY",
                        "module": "SPOT",
                        "symbol": "BTCUSDT",
                        "estimated_amount_u": "10",
                        "valuation_source": "fixture",
                        "side": "BUY",
                        "order_type": "LIMIT",
                        "quantity": "1",
                        "price": "10",
                    }
                ],
                now=now,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "ALTER TABLE authorization_usage DROP COLUMN request_fingerprint"
                )
                connection.execute("PRAGMA user_version = 2")
                connection.commit()

            state.initialize()
            replay = state.get_submission_group_by_idempotency(
                authorization_id=authorization["authorization_id"],
                idempotency_key="legacy-v2",
                request_fingerprint="b" * 64,
                legacy_legs=[
                    {
                        "leg_id": "leg-0",
                        "leg_index": 0,
                        "leg_type": "PRIMARY",
                        "module": "SPOT",
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "order_type": "LIMIT",
                        "quantity": "1",
                        "price": "10",
                    }
                ],
            )
            self.assertTrue(replay["replayed"])
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT request_fingerprint FROM authorization_usage"
                    ).fetchone()[0]
                )

            with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_CONFLICT"):
                state.get_submission_group_by_idempotency(
                    authorization_id=authorization["authorization_id"],
                    idempotency_key="legacy-v2",
                    request_fingerprint="c" * 64,
                    legacy_legs=[
                        {
                            "leg_id": "leg-0",
                            "leg_index": 0,
                            "leg_type": "PRIMARY",
                            "module": "SPOT",
                            "symbol": "BTCUSDT",
                            "side": "BUY",
                            "order_type": "LIMIT",
                            "quantity": "2",
                            "price": "10",
                        }
                    ],
                )

    def test_foreign_key_break_and_invalid_usage_transition_fail_closed(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state" / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    """
                    INSERT INTO authorization_events (
                        event_id, strategy_id, event_type, event_schema_version, severity,
                        occurred_at, payload_json, notification_status, created_at
                    ) VALUES ('orphan-event', 'missing-strategy', 'BROKEN', 1, 'EXCEPTION',
                              '2026-08-16T00:00:00Z', '{\"event_schema_version\":1}', 'NOT_APPLICABLE',
                              '2026-08-16T00:00:00Z')
                    """
                )
                connection.commit()
            with self.assertRaises(state_module.StateConflictError):
                state.health_check(full=True)

            clean_db = Path(tempdir) / "transition" / "state.sqlite3"
            clean_state = state_module.AutoTradeState(clean_db)
            clean_state.initialize()
            strategy = clean_state.register_strategy(
                profile_id="profile-1", strategy_name="transition", distribution="official"
            )
            now = datetime(2026, 8, 16, 11, 30, tzinfo=UTC)
            request = clean_state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "10",
                    "max_total_amount": "100",
                },
                now=now,
            )
            authorization = clean_state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=now,
            )
            usage = clean_state.reserve_usage(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="transition-order",
                estimated_amount_u="5",
                module="SPOT",
                symbol="BTCUSDT",
                valuation_source="official-depth",
                now=now,
            )
            clean_state.settle_usage(usage_id=usage["usage_id"], outcome="ACCEPTED", now=now)
            with self.assertRaisesRegex(ValueError, "USAGE_STATE_CONFLICT"):
                clean_state.settle_usage(usage_id=usage["usage_id"], outcome="RELEASED", now=now)


class AutoTradeSnapshotTests(unittest.TestCase):
    def test_snapshot_is_owner_only_consistent_and_rotates_only_trusted_oldest_records(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            db_path = config_dir / "auto-trade" / "authorization-state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            state.register_strategy(
                profile_id="profile-1", strategy_name="snapshot", distribution="official"
            )
            snapshots = []
            for minute in range(3):
                snapshots.append(
                    state.snapshot_state(
                        retention_count=2,
                        now=datetime(2026, 8, 16, 16, minute, tzinfo=UTC),
                    )
                )
                if minute == 0:
                    unknown = config_dir / "snapshots" / "unknown-user-file.sqlite3"
                    unknown.write_bytes(b"not a managed snapshot")

            snapshots_dir = config_dir / "snapshots"
            index_path = snapshots_dir / "index.json"
            self.assertEqual(stat.S_IMODE(snapshots_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(index_path.stat().st_mode), 0o600)
            self.assertTrue((snapshots_dir / "unknown-user-file.sqlite3").exists())
            self.assertFalse((snapshots_dir / snapshots[0]["relative_path"]).exists())
            self.assertEqual(snapshots[2]["retention_status"], "COMPLETE")
            self.assertEqual(snapshots[2]["retained_count"], 2)
            retained = snapshots[2]["snapshots"]
            self.assertEqual(
                [item["snapshot_id"] for item in retained],
                [snapshots[1]["snapshot_id"], snapshots[2]["snapshot_id"]],
            )
            for item in retained:
                snapshot_path = snapshots_dir / item["relative_path"]
                self.assertEqual(stat.S_IMODE(snapshot_path.stat().st_mode), 0o600)
                with closing(sqlite3.connect(snapshot_path)) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0],
                        state_module.CURRENT_SCHEMA_VERSION,
                    )

    def test_snapshot_publish_fsyncs_directory_namespace_changes(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            state = state_module.AutoTradeState(
                Path(tempdir) / "config" / "auto-trade" / "authorization-state.sqlite3"
            )
            state.initialize()
            with patch.object(
                state_module,
                "_fsync_directory",
                wraps=state_module._fsync_directory,
            ) as fsync_directory:
                state.snapshot_state(
                    retention_count=10,
                    now=datetime(2026, 8, 16, 16, 30, tzinfo=UTC),
                )
            synced = {Path(call.args[0]) for call in fsync_directory.call_args_list}
            self.assertIn(Path(tempdir) / "config" / "snapshots", synced)

    def test_restore_preserves_current_evidence_and_requires_fresh_authorization(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            db_path = config_dir / "auto-trade" / "authorization-state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            now = datetime(2026, 8, 16, 17, 0, tzinfo=UTC)
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="restore", distribution="official"
            )
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "20",
                    "max_total_amount": "100",
                },
                now=now,
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=now,
            )
            reserved = state.reserve_usage(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="restore-reserved",
                estimated_amount_u="5",
                module="SPOT",
                symbol="BTCUSDT",
                valuation_source="fixture",
                now=now,
            )
            review = state.reserve_usage(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="restore-review",
                estimated_amount_u="6",
                module="SPOT",
                symbol="BTCUSDT",
                valuation_source="fixture",
                now=now,
            )
            state.settle_usage(
                usage_id=review["usage_id"], outcome="REVIEW_REQUIRED", now=now
            )
            snapshot = state.snapshot_state(retention_count=10, now=now)

            state.settle_usage(
                usage_id=reserved["usage_id"], outcome="ACCEPTED", now=now
            )
            state.revoke_authorization(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                now=now,
            )

            restored = state.restore_state(
                snapshot_id=snapshot["snapshot_id"],
                now=datetime(2026, 8, 16, 17, 5, tzinfo=UTC),
            )

            self.assertEqual(restored["status"], "STATE_RESTORED_DISABLED")
            self.assertTrue(restored["kill_switch_enabled"])
            preserved_path = config_dir / restored["preserved_database_relative_path"]
            self.assertTrue(preserved_path.exists())
            self.assertEqual(stat.S_IMODE(preserved_path.stat().st_mode), 0o600)
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT status FROM authorizations").fetchone()[0],
                    "REVOKED",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM authorization_usage ORDER BY idempotency_key"
                    ).fetchall(),
                    [("RESERVED",), ("REVIEW_REQUIRED",)],
                )
            self.assertTrue((config_dir / "auto-trade" / "automatic-trading.disabled").exists())
            with self.assertRaisesRegex(ValueError, "AUTO_TRADE_DISABLED"):
                state.prepare_submission_group(
                    strategy_id=strategy["strategy_id"],
                    authorization_id=authorization["authorization_id"],
                    idempotency_key="blocked-after-restore",
                    request_fingerprint="c" * 64,
                    legs=[
                        {
                            "leg_id": "leg-0",
                            "leg_index": 0,
                            "leg_type": "PRIMARY",
                            "module": "SPOT",
                            "symbol": "BTCUSDT",
                            "estimated_amount_u": "1",
                            "valuation_source": "fixture",
                            "side": "BUY",
                            "order_type": "LIMIT",
                            "quantity": "1",
                            "price": "1",
                        }
                    ],
                    now=now,
                )

            fresh_request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "20",
                    "max_total_amount": "100",
                },
                now=datetime(2026, 8, 16, 17, 6, tzinfo=UTC),
            )
            fresh_authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=fresh_request["request_id"],
                scope_signature=fresh_request["scope_signature"],
                confirm_live=True,
                now=datetime(2026, 8, 16, 17, 6, tzinfo=UTC),
            )
            self.assertEqual(
                fresh_authorization["next_action"],
                "RESOLVE_AUTO_USAGE_AND_ENABLE_AUTO_TRADING_AFTER_RESTORE",
            )
            repeated_grant = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=fresh_request["request_id"],
                scope_signature=fresh_request["scope_signature"],
                confirm_live=True,
                now=datetime(2026, 8, 16, 17, 6, tzinfo=UTC),
            )
            self.assertEqual(
                repeated_grant["next_action"],
                "RESOLVE_AUTO_USAGE_AND_ENABLE_AUTO_TRADING_AFTER_RESTORE",
            )
            self.assertTrue((config_dir / "auto-trade" / "automatic-trading.disabled").exists())
            with self.assertRaisesRegex(ValueError, "UNRESOLVED_USAGE_REQUIRES_RECONCILIATION"):
                state.enable_auto_trading_after_restore(confirm_live=True)
            resolved_reserved = state.resolve_uncertain_usage(
                usage_id=reserved["usage_id"],
                outcome="RELEASED",
                evidence_source="manual-official-order-query:no-order",
                weex_order_id=None,
                confirm_live=True,
                now=datetime(2026, 8, 16, 17, 7, tzinfo=UTC),
            )
            resolved_review = state.resolve_uncertain_usage(
                usage_id=review["usage_id"],
                outcome="RELEASED",
                evidence_source="manual-official-order-query:no-order",
                weex_order_id=None,
                confirm_live=True,
                now=datetime(2026, 8, 16, 17, 7, tzinfo=UTC),
            )
            self.assertEqual(resolved_reserved["status"], "RELEASED")
            self.assertEqual(resolved_review["status"], "RELEASED")
            repeated_ensure = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "20",
                    "max_total_amount": "100",
                },
                now=datetime(2026, 8, 16, 17, 7, tzinfo=UTC),
            )
            self.assertEqual(
                repeated_ensure["next_action"], "ENABLE_AUTO_TRADING_AFTER_RESTORE"
            )
            listed_before_enable = state.list_authorizations(
                strategy_id=strategy["strategy_id"],
                now=datetime(2026, 8, 16, 17, 7, tzinfo=UTC),
            )
            self.assertEqual(
                listed_before_enable[-1]["next_action"],
                "ENABLE_AUTO_TRADING_AFTER_RESTORE",
            )
            enabled = state.enable_auto_trading_after_restore(confirm_live=True)
            self.assertEqual(enabled["status"], "AUTOMATIC_TRADING_ENABLED")
            self.assertFalse((config_dir / "auto-trade" / "automatic-trading.disabled").exists())
            enabled_authorization = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "20",
                    "max_total_amount": "100",
                },
                now=datetime(2026, 8, 16, 17, 8, tzinfo=UTC),
            )
            self.assertEqual(enabled_authorization["next_action"], "SUBMIT_ALLOWED")
            prepared = state.prepare_submission_group(
                strategy_id=strategy["strategy_id"],
                authorization_id=fresh_authorization["authorization_id"],
                idempotency_key="allowed-after-fresh-grant",
                request_fingerprint="d" * 64,
                legs=[
                    {
                        "leg_id": "leg-0",
                        "leg_index": 0,
                        "leg_type": "PRIMARY",
                        "module": "SPOT",
                        "symbol": "BTCUSDT",
                        "estimated_amount_u": "1",
                        "valuation_source": "fixture",
                        "side": "BUY",
                        "order_type": "LIMIT",
                        "quantity": "1",
                        "price": "1",
                    }
                ],
                now=datetime(2026, 8, 16, 17, 6, tzinfo=UTC),
            )
            self.assertEqual(prepared["status"], "RESERVED")

    def test_restore_rejects_snapshot_file_substituted_under_registered_id(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            state = state_module.AutoTradeState(
                config_dir / "auto-trade" / "authorization-state.sqlite3"
            )
            state.initialize()
            state.register_strategy(
                profile_id="profile-1", strategy_name="snapshot-a", distribution="official"
            )
            snapshot_a = state.snapshot_state(
                retention_count=10, now=datetime(2026, 8, 16, 18, 0, tzinfo=UTC)
            )
            state.register_strategy(
                profile_id="profile-1", strategy_name="snapshot-b", distribution="official"
            )
            snapshot_b = state.snapshot_state(
                retention_count=10, now=datetime(2026, 8, 16, 18, 1, tzinfo=UTC)
            )
            snapshots_dir = config_dir / "snapshots"
            path_a = snapshots_dir / snapshot_a["relative_path"]
            path_b = snapshots_dir / snapshot_b["relative_path"]
            path_a.write_bytes(path_b.read_bytes())
            path_a.chmod(0o600)

            with self.assertRaises(state_module.StateConflictError):
                state.restore_state(
                    snapshot_id=snapshot_a["snapshot_id"],
                    now=datetime(2026, 8, 16, 18, 2, tzinfo=UTC),
                )

    def test_snapshot_retention_validation_and_creation_failure_delete_nothing(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            state = state_module.AutoTradeState(
                config_dir / "auto-trade" / "authorization-state.sqlite3"
            )
            state.initialize()
            for invalid in (0, 101, True, "10"):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        state.snapshot_state(retention_count=invalid)
            self.assertFalse((config_dir / "snapshots").exists())

            first = state.snapshot_state(
                retention_count=2,
                now=datetime(2026, 8, 16, 18, 0, tzinfo=UTC),
            )
            second = state.snapshot_state(
                retention_count=2,
                now=datetime(2026, 8, 16, 18, 1, tzinfo=UTC),
            )
            with patch.object(
                state_module,
                "_fsync_regular_file",
                side_effect=OSError("injected snapshot write failure"),
            ):
                with self.assertRaises(state_module.StateConflictError):
                    state.snapshot_state(
                        retention_count=1,
                        now=datetime(2026, 8, 16, 18, 2, tzinfo=UTC),
                    )
            snapshots_dir = config_dir / "snapshots"
            self.assertTrue((snapshots_dir / first["relative_path"]).exists())
            self.assertTrue((snapshots_dir / second["relative_path"]).exists())
            index = json.loads((snapshots_dir / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(len(index["snapshots"]), 2)

            boundary_one = state.snapshot_state(
                retention_count=1,
                now=datetime(2026, 8, 16, 18, 3, tzinfo=UTC),
            )
            boundary_hundred = state.snapshot_state(
                retention_count=100,
                now=datetime(2026, 8, 16, 18, 4, tzinfo=UTC),
            )
            self.assertEqual(boundary_one["retention_count"], 1)
            self.assertEqual(boundary_one["retention_status"], "COMPLETE")
            self.assertEqual(boundary_hundred["retention_count"], 100)
            self.assertEqual(boundary_hundred["retention_status"], "COMPLETE")

    def test_snapshot_delete_failure_keeps_new_snapshot_and_does_not_skip_oldest(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            state = state_module.AutoTradeState(
                config_dir / "auto-trade" / "authorization-state.sqlite3"
            )
            state.initialize()
            existing = [
                state.snapshot_state(
                    retention_count=3,
                    now=datetime(2026, 8, 16, 18, minute, tzinfo=UTC),
                )
                for minute in range(3)
            ]
            real_unlink = os.unlink
            deleting_calls = []

            def fail_oldest_delete(path, *args, **kwargs):
                if str(path).endswith(".deleting"):
                    deleting_calls.append(str(path))
                    raise OSError("injected deletion failure")
                return real_unlink(path, *args, **kwargs)

            with patch.object(state_module.os, "unlink", side_effect=fail_oldest_delete):
                newest = state.snapshot_state(
                    retention_count=1,
                    now=datetime(2026, 8, 16, 18, 3, tzinfo=UTC),
                )

            self.assertEqual(newest["retention_status"], "INCOMPLETE")
            self.assertEqual(newest["retained_count"], 4)
            self.assertEqual(len(deleting_calls), 1)
            snapshots_dir = config_dir / "snapshots"
            for item in [*existing, newest]:
                self.assertTrue((snapshots_dir / item["relative_path"]).exists())

    def test_snapshot_index_publish_failure_does_not_delete_registered_snapshots(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            state = state_module.AutoTradeState(
                config_dir / "auto-trade" / "authorization-state.sqlite3"
            )
            state.initialize()
            existing = [
                state.snapshot_state(
                    retention_count=2,
                    now=datetime(2026, 8, 16, 18, minute, tzinfo=UTC),
                )
                for minute in range(2)
            ]
            snapshots_dir = config_dir / "snapshots"
            original_index = (snapshots_dir / "index.json").read_bytes()

            with patch.object(
                state_module,
                "_write_snapshot_index",
                side_effect=OSError("injected index publish failure"),
            ):
                with self.assertRaises(state_module.StateConflictError):
                    state.snapshot_state(
                        retention_count=1,
                        now=datetime(2026, 8, 16, 18, 2, tzinfo=UTC),
                    )

            self.assertEqual((snapshots_dir / "index.json").read_bytes(), original_index)
            for item in existing:
                self.assertTrue((snapshots_dir / item["relative_path"]).exists())
            self.assertEqual(
                len(list(snapshots_dir.glob("auto-trade-snap_*.sqlite3"))),
                3,
            )

    def test_corrupt_snapshot_restore_leaves_active_database_unchanged_and_disabled(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            db_path = config_dir / "auto-trade" / "authorization-state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            state.register_strategy(
                profile_id="profile-1", strategy_name="before", distribution="official"
            )
            snapshot = state.snapshot_state(
                now=datetime(2026, 8, 16, 18, 30, tzinfo=UTC)
            )
            state.register_strategy(
                profile_id="profile-1", strategy_name="after", distribution="official"
            )
            snapshot_path = config_dir / "snapshots" / snapshot["relative_path"]
            snapshot_path.write_bytes(b"corrupt sqlite snapshot")

            with self.assertRaises(state_module.StateConflictError):
                state.restore_state(
                    snapshot_id=snapshot["snapshot_id"],
                    now=datetime(2026, 8, 16, 18, 31, tzinfo=UTC),
                )

            self.assertEqual(
                [item["strategy_name"] for item in state.list_strategies(profile_id="profile-1")],
                ["before", "after"],
            )
            self.assertTrue((config_dir / "auto-trade" / "automatic-trading.disabled").exists())

    def test_restore_rejects_unknown_and_overnew_snapshots_fail_closed(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            db_path = config_dir / "auto-trade" / "authorization-state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            state.register_strategy(
                profile_id="profile-1", strategy_name="schema-restore", distribution="official"
            )
            snapshot = state.snapshot_state(now=datetime(2026, 8, 16, 18, 40, tzinfo=UTC))

            with self.assertRaisesRegex(ValueError, "UNKNOWN_SNAPSHOT"):
                state.restore_state(
                    snapshot_id="snap_" + "0" * 32,
                    now=datetime(2026, 8, 16, 18, 41, tzinfo=UTC),
                )
            self.assertTrue((config_dir / "auto-trade" / "automatic-trading.disabled").exists())
            with self.assertRaisesRegex(ValueError, "FRESH_ACTIVE_AUTHORIZATION_REQUIRED"):
                state.enable_auto_trading_after_restore(
                    confirm_live=True,
                    now=datetime(2026, 8, 16, 18, 41, tzinfo=UTC),
                )

            snapshot_path = config_dir / "snapshots" / snapshot["relative_path"]
            with closing(sqlite3.connect(snapshot_path)) as connection:
                connection.execute(
                    f"PRAGMA user_version = {state_module.CURRENT_SCHEMA_VERSION + 1}"
                )
                connection.commit()
            with self.assertRaises(state_module.StateConflictError):
                state.restore_state(
                    snapshot_id=snapshot["snapshot_id"],
                    now=datetime(2026, 8, 16, 18, 42, tzinfo=UTC),
                )
            self.assertTrue((config_dir / "auto-trade" / "automatic-trading.disabled").exists())
            self.assertEqual(len(state.list_strategies(profile_id="profile-1")), 1)

    def test_malformed_snapshot_index_attempt_latches_automatic_trading(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            state = state_module.AutoTradeState(
                config_dir / "auto-trade" / "authorization-state.sqlite3"
            )
            state.initialize()
            snapshot = state.snapshot_state(now=datetime(2026, 8, 16, 18, 45, tzinfo=UTC))
            (config_dir / "snapshots" / "index.json").write_text("{", encoding="utf-8")
            with self.assertRaises(state_module.StateConflictError):
                state.restore_state(
                    snapshot_id=snapshot["snapshot_id"],
                    now=datetime(2026, 8, 16, 18, 46, tzinfo=UTC),
                )
            self.assertTrue((config_dir / "auto-trade" / "automatic-trading.disabled").exists())

    def test_restore_accepts_digest_bound_v2_snapshot_and_runs_registered_migration(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            db_path = config_dir / "auto-trade" / "authorization-state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            state.register_strategy(
                profile_id="profile-1", strategy_name="v2-snapshot", distribution="official"
            )
            snapshot = state.snapshot_state(now=datetime(2026, 8, 16, 18, 47, tzinfo=UTC))
            snapshots_dir = config_dir / "snapshots"
            snapshot_path = snapshots_dir / snapshot["relative_path"]
            with closing(sqlite3.connect(snapshot_path)) as connection:
                connection.execute(
                    "ALTER TABLE authorization_usage DROP COLUMN request_fingerprint"
                )
                connection.execute("PRAGMA user_version = 2")
                connection.commit()
            index_path = snapshots_dir / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            record = index["snapshots"][0]
            record["database_schema_version"] = 2
            record["size_bytes"] = snapshot_path.stat().st_size
            record["sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            index_path.write_text(json.dumps(index), encoding="utf-8")

            restored = state.restore_state(
                snapshot_id=snapshot["snapshot_id"],
                now=datetime(2026, 8, 16, 18, 48, tzinfo=UTC),
            )
            self.assertEqual(restored["status"], "STATE_RESTORED_DISABLED")
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    state_module.CURRENT_SCHEMA_VERSION,
                )

    def test_restore_enable_expires_and_rejects_a_fresh_but_elapsed_authorization(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            db_path = config_dir / "auto-trade" / "authorization-state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="expired-unlock", distribution="official"
            )
            snapshot = state.snapshot_state(now=datetime(2026, 8, 16, 19, 0, tzinfo=UTC))
            state.restore_state(
                snapshot_id=snapshot["snapshot_id"],
                now=datetime(2026, 8, 16, 19, 1, tzinfo=UTC),
            )
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "10",
                    "max_total_amount": "100",
                    "valid_hours": "0.01",
                },
                now=datetime(2026, 8, 16, 19, 2, tzinfo=UTC),
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=datetime(2026, 8, 16, 19, 2, tzinfo=UTC),
            )

            with self.assertRaisesRegex(ValueError, "FRESH_ACTIVE_AUTHORIZATION_REQUIRED"):
                state.enable_auto_trading_after_restore(
                    confirm_live=True,
                    now=datetime(2026, 8, 16, 19, 3, tzinfo=UTC),
                )
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM authorizations WHERE authorization_id = ?",
                        (authorization["authorization_id"],),
                    ).fetchone()[0],
                    "EXPIRED",
                )
            self.assertTrue(state._kill_switch_path().exists())

    def test_restore_atomic_replace_failure_keeps_active_database_unchanged(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            db_path = config_dir / "auto-trade" / "authorization-state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            state.register_strategy(
                profile_id="profile-1", strategy_name="replace-restore", distribution="official"
            )
            snapshot = state.snapshot_state(now=datetime(2026, 8, 16, 18, 50, tzinfo=UTC))
            state.register_strategy(
                profile_id="profile-1", strategy_name="must-survive", distribution="official"
            )
            real_replace = os.replace
            failed = False

            def fail_active_replace(source, destination, *args, **kwargs):
                nonlocal failed
                if Path(destination) == db_path and not failed:
                    failed = True
                    raise OSError("injected active database replace failure")
                return real_replace(source, destination, *args, **kwargs)

            with patch.object(state_module.os, "replace", side_effect=fail_active_replace):
                with self.assertRaises(state_module.StateConflictError):
                    state.restore_state(
                        snapshot_id=snapshot["snapshot_id"],
                        now=datetime(2026, 8, 16, 18, 51, tzinfo=UTC),
                    )

            self.assertTrue(failed)
            self.assertEqual(
                [item["strategy_name"] for item in state.list_strategies(profile_id="profile-1")],
                ["replace-restore", "must-survive"],
            )
            self.assertTrue((config_dir / "auto-trade" / "automatic-trading.disabled").exists())

    def test_concurrent_snapshot_creation_is_serialized_and_leaves_no_temporary_files(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            config_dir = Path(tempdir) / "config"
            state = state_module.AutoTradeState(
                config_dir / "auto-trade" / "authorization-state.sqlite3"
            )
            state.initialize()
            barrier = threading.Barrier(6)

            def create_snapshot(index):
                barrier.wait()
                return state.snapshot_state(
                    retention_count=3,
                    now=datetime(2026, 8, 16, 19, index, tzinfo=UTC),
                )

            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(create_snapshot, range(6)))

            self.assertEqual(len({item["snapshot_id"] for item in results}), 6)
            snapshots_dir = config_dir / "snapshots"
            index = json.loads((snapshots_dir / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(len(index["snapshots"]), 3)
            self.assertEqual(
                [item["created_at"] for item in index["snapshots"]],
                sorted(item["created_at"] for item in index["snapshots"]),
            )
            self.assertEqual(
                [path.name for path in snapshots_dir.iterdir() if path.name.endswith((".tmp", ".deleting"))],
                [],
            )


class AutoTradeStrategyTests(unittest.TestCase):
    def test_strategy_id_is_reused_on_restart_and_copy_gets_a_new_id(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            state = state_module.AutoTradeState(Path(tempdir) / "state" / "state.sqlite3")
            state.initialize()
            self.assertTrue(hasattr(state, "register_strategy"), "strategy registry is not implemented")

            created = state.register_strategy(
                profile_id="profile-1",
                strategy_name="grid-btc",
                distribution="official",
                trading_mode="live",
            )
            restarted = state.register_strategy(
                profile_id="profile-1",
                strategy_name="grid-btc-renamed",
                distribution="official",
                trading_mode="live",
                strategy_id=created["strategy_id"],
            )
            copied = state.register_strategy(
                profile_id="profile-1",
                strategy_name="grid-btc-renamed",
                distribution="official",
                trading_mode="live",
            )

            self.assertEqual(restarted["strategy_id"], created["strategy_id"])
            self.assertEqual(restarted["strategy_name"], "grid-btc-renamed")
            self.assertNotEqual(copied["strategy_id"], created["strategy_id"])
            self.assertEqual(created["status"], "ACTIVE")
            self.assertEqual(copied["status"], "ACTIVE")


class AutoTradeAuthorizationRequestTests(unittest.TestCase):
    def test_ensure_reuses_pending_request_for_same_owner_and_normalized_scope(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            state = state_module.AutoTradeState(Path(tempdir) / "state" / "state.sqlite3")
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1",
                strategy_name="grid-btc",
                distribution="official",
                trading_mode="live",
            )
            self.assertTrue(hasattr(state, "ensure_authorization"), "authorization ensure is not implemented")

            now = datetime(2026, 8, 16, 6, 30, tzinfo=UTC)
            first = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["FUTURES", "SPOT", "FUTURES"],
                    "symbols": ["btcusdt", "BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "10.00",
                    "max_total_amount": "100.000",
                },
                now=now,
            )
            repeated = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT", "FUTURES"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "10",
                    "max_total_amount": "100",
                    "valid_hours": "24",
                },
                now=now,
            )

            self.assertEqual(first["status"], "AUTHORIZATION_REQUIRED")
            self.assertEqual(repeated["request_id"], first["request_id"])
            self.assertEqual(
                first["scope"],
                {
                    "trade_types": ["SPOT", "FUTURES"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "10",
                    "max_total_amount": "100",
                    "valid_hours": "24",
                },
            )
            self.assertEqual(first["consumed_amount_u"], "0")
            self.assertEqual(first["reserved_amount_u"], "0")
            self.assertEqual(first["remaining_amount_u"], "100")
            self.assertEqual(first["next_action"], "WAIT_FOR_AUTHORIZATION")


class AutoTradeAuthorizationGrantTests(unittest.TestCase):
    def test_grant_strongly_binds_request_and_replaces_only_the_same_strategy_owner(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state" / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            strategy_a = state.register_strategy(
                profile_id="profile-1", strategy_name="strategy-a", distribution="official"
            )
            strategy_b = state.register_strategy(
                profile_id="profile-1", strategy_name="strategy-b", distribution="official"
            )
            now = datetime(2026, 8, 16, 7, 0, tzinfo=UTC)
            base_scope = {
                "trade_types": ["SPOT"],
                "symbols": ["BTCUSDT"],
                "all_symbols": False,
                "max_single_amount": "10",
                "max_total_amount": "100",
            }
            request_a1 = state.ensure_authorization(
                strategy_id=strategy_a["strategy_id"], scope=base_scope, now=now
            )
            request_b = state.ensure_authorization(
                strategy_id=strategy_b["strategy_id"], scope=base_scope, now=now
            )
            self.assertTrue(hasattr(state, "grant_authorization"), "authorization grant is not implemented")

            with self.assertRaisesRegex(ValueError, "STRATEGY_AUTHORIZATION_MISMATCH"):
                state.grant_authorization(
                    strategy_id=strategy_b["strategy_id"],
                    request_id=request_a1["request_id"],
                    scope_signature=request_a1["scope_signature"],
                    confirm_live=True,
                    now=now,
                )

            authorization_a1 = state.grant_authorization(
                strategy_id=strategy_a["strategy_id"],
                request_id=request_a1["request_id"],
                scope_signature=request_a1["scope_signature"],
                confirm_live=True,
                now=now,
            )
            authorization_b = state.grant_authorization(
                strategy_id=strategy_b["strategy_id"],
                request_id=request_b["request_id"],
                scope_signature=request_b["scope_signature"],
                confirm_live=True,
                now=now,
            )
            request_a2 = state.ensure_authorization(
                strategy_id=strategy_a["strategy_id"],
                scope={**base_scope, "max_total_amount": "200"},
                now=now,
            )
            authorization_a2 = state.grant_authorization(
                strategy_id=strategy_a["strategy_id"],
                request_id=request_a2["request_id"],
                scope_signature=request_a2["scope_signature"],
                confirm_live=True,
                now=now,
            )

            self.assertEqual(authorization_a1["status"], "ACTIVE")
            self.assertEqual(authorization_a2["status"], "ACTIVE")
            self.assertEqual(authorization_b["status"], "ACTIVE")
            with closing(sqlite3.connect(db_path)) as connection:
                statuses = dict(
                    connection.execute(
                        "SELECT authorization_id, status FROM authorizations"
                    ).fetchall()
                )
            self.assertEqual(statuses[authorization_a1["authorization_id"]], "REPLACED")
            self.assertEqual(statuses[authorization_a2["authorization_id"]], "ACTIVE")
            self.assertEqual(statuses[authorization_b["authorization_id"]], "ACTIVE")


class AutoTradeAuthorizationLifecycleTests(unittest.TestCase):
    def test_read_paths_expire_due_records_once(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state" / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="lazy-expiry", distribution="official"
            )
            started_at = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
            pending = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "10",
                    "max_total_amount": "100",
                    "valid_hours": "0.01",
                },
                now=started_at,
            )
            pending_after = started_at.replace(minute=16)
            self.assertEqual(
                state.get_authorization_request(
                    strategy_id=strategy["strategy_id"],
                    request_id=pending["request_id"],
                    now=pending_after,
                )["request_status"],
                "EXPIRED",
            )
            state.get_authorization_request(
                strategy_id=strategy["strategy_id"],
                request_id=pending["request_id"],
                now=pending_after,
            )

            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["ETHUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "10",
                    "max_total_amount": "100",
                    "valid_hours": "0.01",
                },
                now=pending_after,
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=pending_after,
            )
            expired_at = pending_after.replace(second=37)
            listed = state.list_authorizations(
                strategy_id=strategy["strategy_id"], now=expired_at
            )
            self.assertEqual(
                next(
                    item for item in listed
                    if item["authorization_id"] == authorization["authorization_id"]
                )["status"],
                "EXPIRED",
            )
            state.list_authorizations(strategy_id=strategy["strategy_id"], now=expired_at)
            with closing(sqlite3.connect(db_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM authorization_events "
                        "WHERE request_id = ? AND event_type = 'AUTHORIZATION_REQUEST_EXPIRED'",
                        (pending["request_id"],),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM authorization_events "
                        "WHERE authorization_id = ? AND event_type = 'AUTHORIZATION_EXPIRED'",
                        (authorization["authorization_id"],),
                    ).fetchone()[0],
                    1,
                )

    def test_expire_revoke_and_retire_preserve_history_and_block_future_authorization(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state" / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="lifecycle", distribution="official"
            )
            scope = {
                "trade_types": ["FUTURES"],
                "symbols": ["BTCUSDT"],
                "all_symbols": False,
                "max_single_amount": "10",
                "max_total_amount": "100",
                "valid_hours": "0.01",
            }
            started_at = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
            request_1 = state.ensure_authorization(
                strategy_id=strategy["strategy_id"], scope=scope, now=started_at
            )
            authorization_1 = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request_1["request_id"],
                scope_signature=request_1["scope_signature"],
                confirm_live=True,
                now=started_at,
            )

            after_expiry = started_at.replace(second=37)
            request_2 = state.ensure_authorization(
                strategy_id=strategy["strategy_id"], scope=scope, now=after_expiry
            )
            authorization_2 = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request_2["request_id"],
                scope_signature=request_2["scope_signature"],
                confirm_live=True,
                now=after_expiry,
            )
            self.assertTrue(hasattr(state, "revoke_authorization"), "authorization revoke is not implemented")
            revoked = state.revoke_authorization(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization_2["authorization_id"],
                now=after_expiry,
            )
            self.assertEqual(revoked["status"], "REVOKED")

            request_3 = state.ensure_authorization(
                strategy_id=strategy["strategy_id"], scope=scope, now=after_expiry
            )
            authorization_3 = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request_3["request_id"],
                scope_signature=request_3["scope_signature"],
                confirm_live=True,
                now=after_expiry,
            )
            self.assertTrue(hasattr(state, "retire_strategy"), "strategy retirement is not implemented")
            retired = state.retire_strategy(strategy_id=strategy["strategy_id"], now=after_expiry)
            self.assertEqual(retired["status"], "RETIRED")
            with self.assertRaisesRegex(ValueError, "STRATEGY_RETIRED"):
                state.ensure_authorization(
                    strategy_id=strategy["strategy_id"], scope=scope, now=after_expiry
                )

            with closing(sqlite3.connect(db_path)) as connection:
                history = dict(
                    connection.execute(
                        "SELECT authorization_id, status FROM authorizations"
                    ).fetchall()
                )
            self.assertEqual(history[authorization_1["authorization_id"]], "EXPIRED")
            self.assertEqual(history[authorization_2["authorization_id"]], "REVOKED")
            self.assertEqual(history[authorization_3["authorization_id"]], "REVOKED")
            self.assertEqual(len(history), 3)

    def test_expired_submission_commits_state_and_one_immediate_lifecycle_event(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state" / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="expire-submit", distribution="official"
            )
            started_at = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "10",
                    "max_total_amount": "100",
                    "valid_hours": "0.01",
                },
                now=started_at,
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=started_at,
            )

            with self.assertRaisesRegex(ValueError, "AUTHORIZATION_NOT_ACTIVE"):
                state.prepare_submission_group(
                    strategy_id=strategy["strategy_id"],
                    authorization_id=authorization["authorization_id"],
                    idempotency_key="expired-submit",
                    request_fingerprint="e" * 64,
                    legs=[
                        {
                            "leg_id": "leg-0",
                            "leg_index": 0,
                            "leg_type": "PRIMARY",
                            "module": "SPOT",
                            "symbol": "BTCUSDT",
                            "estimated_amount_u": "1",
                            "valuation_source": "fixture",
                            "side": "BUY",
                            "order_type": "LIMIT",
                            "quantity": "1",
                            "price": "1",
                        }
                    ],
                    now=started_at.replace(second=37),
                )

            with closing(sqlite3.connect(db_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM authorizations WHERE authorization_id = ?",
                        (authorization["authorization_id"],),
                    ).fetchone()[0],
                    "EXPIRED",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM authorization_events "
                        "WHERE authorization_id = ? AND event_type = 'AUTHORIZATION_EXPIRED'",
                        (authorization["authorization_id"],),
                    ).fetchone()[0],
                    1,
                )
            claims = state.claim_notifications(now=started_at.replace(second=38))
            lifecycle = [claim for claim in claims if claim.get("event_type") == "AUTHORIZATION_EXPIRED"]
            self.assertEqual(len(lifecycle), 1)


class AutoTradeUsageTests(unittest.TestCase):
    def test_usage_transitions_keep_decimal_accepted_and_reserved_balances(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            state = state_module.AutoTradeState(Path(tempdir) / "state" / "state.sqlite3")
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="quota", distribution="official"
            )
            scope = {
                "trade_types": ["SPOT", "FUTURES"],
                "symbols": ["BTCUSDT"],
                "all_symbols": False,
                "max_single_amount": "40",
                "max_total_amount": "100",
            }
            now = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"], scope=scope, now=now
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=now,
            )
            self.assertTrue(hasattr(state, "reserve_usage"), "usage reservation is not implemented")

            reserved_1 = state.reserve_usage(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="order-1",
                estimated_amount_u="30.00",
                module="SPOT",
                symbol="BTCUSDT",
                valuation_source="official-depth",
                now=now,
            )
            repeated_1 = state.reserve_usage(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="order-1",
                estimated_amount_u="30",
                module="SPOT",
                symbol="BTCUSDT",
                valuation_source="official-depth",
                now=now,
            )
            self.assertEqual(repeated_1["usage_id"], reserved_1["usage_id"])
            self.assertEqual(reserved_1["reserved_amount_u"], "30")
            self.assertEqual(reserved_1["remaining_amount_u"], "70")

            self.assertTrue(hasattr(state, "settle_usage"), "usage settlement is not implemented")
            accepted_1 = state.settle_usage(
                usage_id=reserved_1["usage_id"], outcome="ACCEPTED", now=now
            )
            self.assertEqual(accepted_1["accepted_amount_u"], "30")
            self.assertEqual(accepted_1["reserved_amount_u"], "0")
            self.assertEqual(accepted_1["remaining_amount_u"], "70")

            reserved_2 = state.reserve_usage(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="order-2",
                estimated_amount_u="40",
                module="FUTURES",
                symbol="BTCUSDT",
                valuation_source="official-depth",
                now=now,
            )
            released_2 = state.settle_usage(
                usage_id=reserved_2["usage_id"], outcome="RELEASED", now=now
            )
            self.assertEqual(released_2["accepted_amount_u"], "30")
            self.assertEqual(released_2["reserved_amount_u"], "0")
            self.assertEqual(released_2["remaining_amount_u"], "70")

            reserved_3 = state.reserve_usage(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="order-3",
                estimated_amount_u="40",
                module="SPOT",
                symbol="BTCUSDT",
                valuation_source="official-depth",
                now=now,
            )
            review_3 = state.settle_usage(
                usage_id=reserved_3["usage_id"], outcome="REVIEW_REQUIRED", now=now
            )
            self.assertEqual(review_3["accepted_amount_u"], "30")
            self.assertEqual(review_3["reserved_amount_u"], "40")
            self.assertEqual(review_3["remaining_amount_u"], "30")

            with self.assertRaisesRegex(ValueError, "TOTAL_LIMIT_EXCEEDED"):
                state.reserve_usage(
                    strategy_id=strategy["strategy_id"],
                    authorization_id=authorization["authorization_id"],
                    idempotency_key="order-4",
                    estimated_amount_u="31",
                    module="SPOT",
                    symbol="BTCUSDT",
                    valuation_source="official-depth",
                    now=now,
                )

    def test_concurrent_reservations_cannot_overspend_the_last_total_quota(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state" / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="concurrent", distribution="official"
            )
            now = datetime(2026, 8, 16, 9, 30, tzinfo=UTC)
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "60",
                    "max_total_amount": "100",
                },
                now=now,
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=now,
            )
            barrier = threading.Barrier(2)

            def reserve(key: str):
                worker_state = state_module.AutoTradeState(db_path)
                barrier.wait(timeout=2)
                try:
                    return worker_state.reserve_usage(
                        strategy_id=strategy["strategy_id"],
                        authorization_id=authorization["authorization_id"],
                        idempotency_key=key,
                        estimated_amount_u="60",
                        module="SPOT",
                        symbol="BTCUSDT",
                        valuation_source="official-depth",
                        now=now,
                    )
                except Exception as exc:  # The exact fail-closed error is asserted below.
                    return exc

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(reserve, ("worker-a", "worker-b")))

            successes = [result for result in results if isinstance(result, dict)]
            failures = [result for result in results if isinstance(result, Exception)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], ValueError)
            self.assertIn("TOTAL_LIMIT_EXCEEDED", str(failures[0]))
            self.assertEqual(successes[0]["reserved_amount_u"], "60")
            self.assertEqual(successes[0]["remaining_amount_u"], "40")

    def test_decimal_accumulation_never_rounds_past_total_limit(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            state = state_module.AutoTradeState(Path(tempdir) / "state" / "state.sqlite3")
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="decimal-precision", distribution="official"
            )
            now = datetime(2026, 8, 16, 9, 45, tzinfo=UTC)
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "100000000000000000000",
                    "max_total_amount": "1000000000000000000000",
                },
                now=now,
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=now,
            )
            for index in range(10):
                state.reserve_usage(
                    strategy_id=strategy["strategy_id"],
                    authorization_id=authorization["authorization_id"],
                    idempotency_key=f"precision-{index}",
                    estimated_amount_u="99999999999999999999.99999994",
                    module="SPOT",
                    symbol="BTCUSDT",
                    valuation_source="fixture",
                    now=now,
                )
            with self.assertRaisesRegex(ValueError, "TOTAL_LIMIT_EXCEEDED"):
                state.reserve_usage(
                    strategy_id=strategy["strategy_id"],
                    authorization_id=authorization["authorization_id"],
                    idempotency_key="precision-overflow",
                    estimated_amount_u="0.00000070",
                    module="SPOT",
                    symbol="BTCUSDT",
                    valuation_source="fixture",
                    now=now,
                )
            self.assertEqual(state.health_check(full=True)["status"], "HEALTHY")


class AutoTradeInvariantRegressionTests(unittest.TestCase):
    def test_health_check_rejects_cross_strategy_authorization_request_mismatch(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state" / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            now = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
            strategies = [
                state.register_strategy(
                    profile_id="profile-1", strategy_name=f"invariant-{index}", distribution="official"
                )
                for index in range(2)
            ]
            requests = [
                state.ensure_authorization(
                    strategy_id=strategy["strategy_id"],
                    scope={
                        "trade_types": ["SPOT"],
                        "symbols": ["BTCUSDT"],
                        "all_symbols": False,
                        "max_single_amount": "10",
                        "max_total_amount": "100",
                    },
                    now=now,
                )
                for strategy in strategies
            ]
            authorization = state.grant_authorization(
                strategy_id=strategies[0]["strategy_id"],
                request_id=requests[0]["request_id"],
                scope_signature=requests[0]["scope_signature"],
                confirm_live=True,
                now=now,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "UPDATE authorizations SET request_id = ? WHERE authorization_id = ?",
                    (requests[1]["request_id"], authorization["authorization_id"]),
                )
                connection.commit()

            with self.assertRaises(state_module.StateConflictError):
                state.health_check(full=True)

    def test_health_check_rejects_contradictory_event_reference_chain(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "state" / "state.sqlite3"
            state = state_module.AutoTradeState(db_path)
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="event-chain", distribution="official"
            )
            now = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)

            def grant(symbol, total):
                request = state.ensure_authorization(
                    strategy_id=strategy["strategy_id"],
                    scope={
                        "trade_types": ["SPOT"],
                        "symbols": [symbol],
                        "all_symbols": False,
                        "max_single_amount": "10",
                        "max_total_amount": total,
                    },
                    now=now,
                )
                authorization = state.grant_authorization(
                    strategy_id=strategy["strategy_id"],
                    request_id=request["request_id"],
                    scope_signature=request["scope_signature"],
                    confirm_live=True,
                    now=now,
                )
                group = state.prepare_submission_group(
                    strategy_id=strategy["strategy_id"],
                    authorization_id=authorization["authorization_id"],
                    idempotency_key=f"chain-{symbol}",
                    request_fingerprint=("a" if symbol == "BTCUSDT" else "b") * 64,
                    legs=[
                        {
                            "leg_id": "leg-0",
                            "leg_index": 0,
                            "leg_type": "PRIMARY",
                            "module": "SPOT",
                            "symbol": symbol,
                            "estimated_amount_u": "1",
                            "valuation_source": "fixture",
                            "side": "BUY",
                            "order_type": "LIMIT",
                            "quantity": "1",
                            "price": "1",
                        }
                    ],
                    now=now,
                )
                return request, authorization, group["legs"][0]

            first = grant("BTCUSDT", "100")
            second = grant("ETHUSDT", "101")
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    """
                    UPDATE authorization_events
                    SET request_id = ?, authorization_id = ?, usage_id = ?, auto_trade_order_id = ?
                    WHERE event_type = 'ORDER_RECORDED' AND usage_id = ?
                    """,
                    (
                        second[0]["request_id"],
                        second[1]["authorization_id"],
                        first[2]["usage_id"],
                        second[2]["auto_trade_order_id"],
                        first[2]["usage_id"],
                    ),
                )
                connection.commit()

            with self.assertRaises(state_module.StateConflictError):
                state.health_check(full=True)


class AutoTradeOrderFactTests(unittest.TestCase):
    def test_reconciliation_updates_order_facts_without_changing_accepted_quota(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            state = state_module.AutoTradeState(Path(tempdir) / "state" / "state.sqlite3")
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="reconcile", distribution="official"
            )
            now = datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "50",
                    "max_total_amount": "100",
                },
                now=now,
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=now,
            )
            usage = state.reserve_usage(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="order-accepted",
                estimated_amount_u="25",
                module="SPOT",
                symbol="BTCUSDT",
                valuation_source="official-depth",
                now=now,
            )
            self.assertTrue(hasattr(state, "record_order"), "order ownership recording is not implemented")
            order = state.record_order(
                usage_id=usage["usage_id"],
                weex_order_id="weex-order-1",
                side="BUY",
                order_type="MARKET",
                quantity="0.001",
                price=None,
                now=now,
            )
            accepted = state.settle_usage(
                usage_id=usage["usage_id"], outcome="ACCEPTED", now=now
            )
            self.assertNotIn(authorization["authorization_id"], order["client_order_id"])
            self.assertTrue(hasattr(state, "reconcile_order"), "order reconciliation is not implemented")

            reconciled = state.reconcile_order(
                auto_trade_order_id=order["auto_trade_order_id"],
                reconciliation_status="COMPLETE",
                exchange_status="FILLED",
                executed_quantity="0.001",
                executed_quote_amount="24.50",
                fee_amount="0.0245",
                fee_asset="USDT",
                reconciliation_source="official-order-query",
                now=now,
            )

            self.assertEqual(reconciled["reconciliation_status"], "COMPLETE")
            self.assertEqual(reconciled["executed_quote_amount"], "24.5")
            self.assertEqual(reconciled["fee_amount"], "0.0245")
            self.assertEqual(reconciled["usage_status"], "ACCEPTED")
            self.assertEqual(reconciled["estimated_amount_u"], "25")
            self.assertEqual(reconciled["accepted_amount_u"], accepted["accepted_amount_u"])
            self.assertEqual(reconciled["reserved_amount_u"], accepted["reserved_amount_u"])
            self.assertEqual(reconciled["remaining_amount_u"], accepted["remaining_amount_u"])


class AutoTradeEventTests(unittest.TestCase):
    def test_business_events_are_append_only_versioned_and_traceable(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            state = state_module.AutoTradeState(Path(tempdir) / "state" / "state.sqlite3")
            state.initialize()
            now = datetime(2026, 8, 16, 10, 30, tzinfo=UTC)
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="events", distribution="official"
            )
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "50",
                    "max_total_amount": "100",
                },
                now=now,
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=now,
            )
            usage = state.reserve_usage(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="event-order",
                estimated_amount_u="25",
                module="SPOT",
                symbol="BTCUSDT",
                valuation_source="official-depth",
                now=now,
            )
            order = state.record_order(
                usage_id=usage["usage_id"],
                weex_order_id="weex-event-order",
                side="BUY",
                order_type="MARKET",
                quantity="0.001",
                price=None,
                now=now,
            )
            state.settle_usage(usage_id=usage["usage_id"], outcome="ACCEPTED", now=now)
            state.reconcile_order(
                auto_trade_order_id=order["auto_trade_order_id"],
                reconciliation_status="UNAVAILABLE",
                exchange_status=None,
                executed_quantity=None,
                executed_quote_amount=None,
                fee_amount=None,
                fee_asset=None,
                reconciliation_source="official-query-unavailable",
                now=now,
            )
            state.revoke_authorization(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                now=now,
            )
            self.assertTrue(hasattr(state, "list_events"), "event facade is not implemented")

            events = state.list_events(strategy_id=strategy["strategy_id"])

            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "STRATEGY_REGISTERED",
                    "AUTHORIZATION_REQUESTED",
                    "AUTHORIZATION_GRANTED",
                    "USAGE_RESERVED",
                    "ORDER_RECORDED",
                    "USAGE_ACCEPTED",
                    "ORDER_RECONCILIATION_UNAVAILABLE",
                    "AUTHORIZATION_REVOKED",
                ],
            )
            self.assertEqual(len({event["event_id"] for event in events}), len(events))
            for event in events:
                self.assertEqual(event["event_schema_version"], state_module.EVENT_SCHEMA_VERSION)
                self.assertEqual(
                    event["payload"]["event_schema_version"], state_module.EVENT_SCHEMA_VERSION
                )
                self.assertEqual(event["strategy_id"], strategy["strategy_id"])

    def test_notification_claims_aggregate_success_and_do_not_retry_failures(self) -> None:
        state_module = load_state_module()

        with tempfile.TemporaryDirectory() as tempdir:
            state = state_module.AutoTradeState(Path(tempdir) / "state" / "state.sqlite3")
            state.initialize()
            strategy = state.register_strategy(
                profile_id="profile-1", strategy_name="notifications", distribution="official"
            )
            window_start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
            request = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "20",
                    "max_total_amount": "100",
                },
                now=window_start,
            )
            authorization = state.grant_authorization(
                strategy_id=strategy["strategy_id"],
                request_id=request["request_id"],
                scope_signature=request["scope_signature"],
                confirm_live=True,
                now=window_start,
            )
            for index in range(2):
                usage = state.reserve_usage(
                    strategy_id=strategy["strategy_id"],
                    authorization_id=authorization["authorization_id"],
                    idempotency_key=f"notification-{index}",
                    estimated_amount_u="10",
                    module="SPOT",
                    symbol="BTCUSDT",
                    valuation_source="official-depth",
                    now=window_start.replace(second=index + 1),
                )
                state.settle_usage(
                    usage_id=usage["usage_id"],
                    outcome="ACCEPTED",
                    now=window_start.replace(second=index + 1),
                )
            self.assertTrue(hasattr(state, "claim_notifications"), "notification claims are not implemented")

            claims = state.claim_notifications(now=window_start.replace(minute=1, second=1))
            summaries = [claim for claim in claims if claim["kind"] == "ACCEPTED_SUMMARY"]
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["order_count"], 2)
            self.assertEqual(summaries[0]["estimated_amount_u"], "20")
            self.assertEqual(summaries[0]["remaining_amount_u"], "80")
            self.assertNotIn(authorization["authorization_id"], json.dumps(summaries[0]))
            self.assertEqual(
                state.claim_notifications(now=window_start.replace(minute=1, second=1)),
                [],
            )

            review_usage = state.reserve_usage(
                strategy_id=strategy["strategy_id"],
                authorization_id=authorization["authorization_id"],
                idempotency_key="notification-review",
                estimated_amount_u="10",
                module="SPOT",
                symbol="BTCUSDT",
                valuation_source="official-depth",
                now=window_start.replace(minute=1, second=2),
            )
            state.settle_usage(
                usage_id=review_usage["usage_id"],
                outcome="REVIEW_REQUIRED",
                now=window_start.replace(minute=1, second=2),
            )
            exception_claims = state.claim_notifications(
                now=window_start.replace(minute=1, second=3)
            )
            immediate = [claim for claim in exception_claims if claim["kind"] == "EXCEPTION"]
            self.assertEqual(len(immediate), 1)
            self.assertTrue(hasattr(state, "complete_notification"), "notification result is not implemented")
            state.complete_notification(
                notification_key=immediate[0]["notification_key"],
                outcome="FAILED",
                now=window_start.replace(minute=1, second=3),
            )
            self.assertEqual(
                state.claim_notifications(now=window_start.replace(minute=1, second=4)),
                [],
            )
            authorization_after = state.ensure_authorization(
                strategy_id=strategy["strategy_id"],
                scope={
                    "trade_types": ["SPOT"],
                    "symbols": ["BTCUSDT"],
                    "all_symbols": False,
                    "max_single_amount": "20",
                    "max_total_amount": "100",
                },
                now=window_start.replace(minute=1, second=4),
            )
            self.assertEqual(authorization_after["consumed_amount_u"], "20")
            self.assertEqual(authorization_after["reserved_amount_u"], "10")


class AutoTradeAmountTests(unittest.TestCase):
    def test_spot_limit_uses_decimal_notional_and_fee_upper_bound(self) -> None:
        amount_module = load_amount_module()
        self.assertTrue(hasattr(amount_module, "estimate_order_amount"), "amount estimator is not implemented")
        now_ms = 1_700_000_000_000
        result = amount_module.estimate_order_amount(
            market="SPOT",
            order={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "LIMIT",
                "quantity": "2",
                "price": "100",
            },
            facts={
                "timestamp_ms": now_ms - 100,
                "symbol": {
                    "quoteAsset": "USDT",
                    "makerFeeRate": "0.001",
                    "takerFeeRate": "0.002",
                },
            },
            now_ms=now_ms,
        )
        self.assertEqual(result["estimated_amount_u"], "200.4")
        self.assertTrue(result["estimated"])
        self.assertEqual(result["quote_asset"], "USDT")
        self.assertEqual(result["valuation_source"], "SPOT_LIMIT_NOTIONAL_PLUS_FEE_UPPER_BOUND")

    def test_spot_limit_preserves_precision_before_conservative_u_rounding(self) -> None:
        amount_module = load_amount_module()
        now_ms = 1_700_000_000_000

        result = amount_module.estimate_order_amount(
            market="SPOT",
            order={
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "LIMIT",
                "quantity": "1.00000000000000000000000000001",
                "price": "1",
            },
            facts={
                "timestamp_ms": now_ms,
                "symbol": {
                    "quoteAsset": "USDT",
                    "makerFeeRate": "0",
                    "takerFeeRate": "0",
                },
            },
            now_ms=now_ms,
        )

        self.assertEqual(result["notional_quote"], "1.00000000000000000000000000001")
        self.assertEqual(result["estimated_amount_u"], "1.00000001")

    def test_spot_market_consumes_adverse_depth_and_rejects_insufficient_depth(self) -> None:
        amount_module = load_amount_module()
        now_ms = 1_700_000_000_000
        facts = {
            "timestamp_ms": now_ms - 100,
            "depth": {
                "timestamp_ms": now_ms - 100,
                "limit": 15,
                "asks": [["100", "1"], ["101", "2"]],
                "bids": [["99", "1"], ["98", "2"]],
            },
            "symbol": {
                "quoteAsset": "USDT",
                "makerFeeRate": "0.001",
                "takerFeeRate": "0.002",
            },
        }
        result = amount_module.estimate_order_amount(
            market="SPOT",
            order={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "2"},
            facts=facts,
            now_ms=now_ms,
        )
        self.assertEqual(result["notional_quote"], "201")
        self.assertEqual(result["estimated_amount_u"], "201.402")
        with self.assertRaises(amount_module.ValuationUnavailable):
            amount_module.estimate_order_amount(
                market="SPOT",
                order={"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "4"},
                facts=facts,
                now_ms=now_ms,
            )
