#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from weex_auto_trade_state import AutoTradeState  # noqa: E402


NOW = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64


def prepared_state(tempdir: str):
    state = AutoTradeState(Path(tempdir) / "state" / "auto.sqlite3")
    state.initialize()
    strategy = state.register_strategy(
        profile_id="profile-id",
        strategy_name="delegated-state",
    )
    request = state.ensure_authorization(
        strategy_id=strategy["strategy_id"],
        scope={
            "trade_types": ["SPOT"],
            "symbols": ["BTCUSDT"],
            "all_symbols": False,
            "max_single_amount": "100",
            "max_total_amount": "1000",
            "valid_hours": "24",
        },
        now=NOW,
    )
    authorization = state.grant_authorization(
        strategy_id=strategy["strategy_id"],
        request_id=request["request_id"],
        scope_signature=request["scope_signature"],
        confirm_live=True,
        now=NOW,
    )
    group = state.prepare_submission_group(
        strategy_id=strategy["strategy_id"],
        authorization_id=authorization["authorization_id"],
        idempotency_key="delegated-idempotency",
        request_fingerprint=FINGERPRINT,
        legs=[
            {
                "leg_id": "leg-0",
                "leg_index": 0,
                "leg_type": "PRIMARY",
                "module": "SPOT",
                "symbol": "BTCUSDT",
                "estimated_amount_u": "10",
                "valuation_source": "unit-test",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": "1",
                "price": None,
                "advisory_alerts": [],
                "risk_rule_version": "v1",
                "risk_input_timestamp": None,
            }
        ],
        now=NOW,
    )
    return state, strategy, authorization, group


class DelegatedSubmissionStateTests(unittest.TestCase):
    def test_snapshot_is_pure_select_and_returns_safe_reserved_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state, strategy, authorization, group = prepared_state(tempdir)
            leg = group["legs"][0]
            statements: list[str] = []
            real_connect = state._connect

            def tracked_connect():
                connection = real_connect()
                connection.set_trace_callback(statements.append)
                return connection

            with mock.patch.object(state, "_connect", side_effect=tracked_connect):
                snapshot = state.read_delegated_submission_group(
                    authorization_id=authorization["authorization_id"],
                    submission_group_id=group["submission_group_id"],
                    request_fingerprint=FINGERPRINT,
                    usage_ids=[leg["usage_id"]],
                    client_order_ids=[leg["client_order_id"]],
                    now=NOW + timedelta(seconds=1),
                )

        self.assertEqual(snapshot["authorization_id"], authorization["authorization_id"])
        self.assertEqual(snapshot["strategy_id"], strategy["strategy_id"])
        self.assertEqual(snapshot["profile_id"], "profile-id")
        self.assertEqual(snapshot["scope_signature"], authorization["scope_signature"])
        self.assertEqual(snapshot["submission_group_id"], group["submission_group_id"])
        self.assertEqual(snapshot["usage_count"], 1)
        self.assertEqual(snapshot["usage_ids"], [leg["usage_id"]])
        self.assertEqual(snapshot["client_order_ids"], [leg["client_order_id"]])
        self.assertGreater(snapshot["expires_at"], int(NOW.timestamp()))
        forbidden = ("BEGIN", "UPDATE", "INSERT", "DELETE", "COMMIT", "ROLLBACK")
        self.assertFalse(
            any(statement.lstrip().upper().startswith(forbidden) for statement in statements),
            statements,
        )

    def test_snapshot_rejects_binding_status_expiry_and_kill_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state, _strategy, authorization, group = prepared_state(tempdir)
            leg = group["legs"][0]
            common = {
                "authorization_id": authorization["authorization_id"],
                "submission_group_id": group["submission_group_id"],
                "request_fingerprint": FINGERPRINT,
                "usage_ids": [leg["usage_id"]],
                "client_order_ids": [leg["client_order_id"]],
                "now": NOW + timedelta(seconds=1),
            }
            for field, value in (
                ("request_fingerprint", "b" * 64),
                ("usage_ids", ["use_wrong"]),
                ("client_order_ids", ["client-wrong"]),
                ("submission_group_id", "grp_wrong"),
            ):
                with self.subTest(field=field):
                    with self.assertRaises((ValueError, RuntimeError)):
                        state.read_delegated_submission_group(**(common | {field: value}))

            with self.assertRaises((ValueError, RuntimeError)):
                state.read_delegated_submission_group(
                    **(common | {"now": NOW + timedelta(hours=25)})
                )

            state.disable_automatic_trading(
                reason="SUBMISSION_STATE_UNCERTAIN",
                now=NOW,
            )
            with self.assertRaisesRegex(ValueError, "AUTO_TRADE_DISABLED"):
                state.read_delegated_submission_group(**common)

    def test_snapshot_rejects_usage_that_is_no_longer_reserved(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state, _strategy, authorization, group = prepared_state(tempdir)
            leg = group["legs"][0]
            state.settle_usage(
                usage_id=leg["usage_id"],
                outcome="REVIEW_REQUIRED",
                now=NOW + timedelta(seconds=1),
            )
            with self.assertRaises((ValueError, RuntimeError)):
                state.read_delegated_submission_group(
                    authorization_id=authorization["authorization_id"],
                    submission_group_id=group["submission_group_id"],
                    request_fingerprint=FINGERPRINT,
                    usage_ids=[leg["usage_id"]],
                    client_order_ids=[leg["client_order_id"]],
                    now=NOW + timedelta(seconds=2),
                )


if __name__ == "__main__":
    unittest.main()
