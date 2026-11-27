#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from weex_auto_trade_state import AutoTradeState, build_verified_usage_evidence  # noqa: E402


NOW = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
FINGERPRINT = "a" * 64


def prepared_state(tempdir: str, *, batch: bool = False):
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
                "leg_id": f"leg-{index}",
                "leg_index": index,
                "leg_type": "BATCH_CHILD" if batch else "PRIMARY",
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
            for index in range(2 if batch else 1)
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


class DurableAutoGrantTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from weex_mcp import execution_authorization, trade_execution
        except ModuleNotFoundError:
            self.skipTest("fixed MCP 0.3.3 runtime is required")
        self.authorization_model = execution_authorization
        self.execution_model = trade_execution

    def _issue(self, state, strategy, authorization, group, *, issued_at=None, duration=300_000):
        issued_at = issued_at if issued_at is not None else int(NOW.timestamp() * 1_000) + 100
        expires_at = issued_at + duration
        client_order_ids = [leg["client_order_id"] for leg in group["legs"]]
        orders = [
            {"symbol": "BTCUSDT", "side": "BUY", "order_type": "MARKET", "quantity": "1", "client_order_id": client_id}
            for client_id in client_order_ids
        ]
        batch = len(orders) > 1
        action = "place_batch_orders" if batch else "place_order"
        prepared = self.execution_model.build_prepared_mutation(
            tool_name="trade_" + action, action=action, market="spot", trading_mode="live",
            canonical_arguments={"market": "spot", "trading_mode": "live", **({"orders": orders} if batch else orders[0])},
            route_fingerprint="sha256:" + "b" * 64, credential_fingerprint="credential-fingerprint",
            precondition=self.execution_model.Precondition("STATIC", "sha256:" + "c" * 64, issued_at, expires_at, {}),
            risk_facts={}, reconciliation_template={}, binding_valid_until=None, resolved_valid_until=None,
            prepare_expires_at=expires_at,
        )
        grant = self.authorization_model.ExecutionGrant(
            grant_id="grant-1", state="ISSUED", principal_id="profile-id", account_binding_id="account-id",
            credential_fingerprint=prepared.credential_fingerprint, tool_name=prepared.tool_name,
            prepared_artifact_digest=prepared.prepared_artifact_digest,
            execution_binding_digest=prepared.execution_binding_digest, issued_at=issued_at, expires_at=expires_at,
        )
        self.assertTrue(hasattr(state, "issue_execution_grant"), "no durable grant issue API")
        issued = state.issue_execution_grant(
            submission_group_id=group["submission_group_id"], authorization_id=authorization["authorization_id"],
            strategy_id=strategy["strategy_id"], profile_id="profile-id", request_fingerprint=FINGERPRINT,
            client_order_ids=client_order_ids, grant=grant, prepared=prepared.to_dict(),
        )
        return issued, prepared

    @staticmethod
    def _binding(grant):
        return {field: getattr(grant, field) for field in ("grant_id", "principal_id", "account_binding_id", "tool_name")}

    def _claim(self, state, grant, *, now=None):
        return state.claim(
            **self._binding(grant), prepared_artifact_digest=grant.prepared_artifact_digest,
            execution_binding_digest=grant.execution_binding_digest,
            now=now if now is not None else grant.issued_at + 1,
        )

    def test_concurrent_claim_is_durable_once_and_wrong_binding_cannot_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state, strategy, authorization, group = prepared_state(tempdir)
            grant, prepared = self._issue(state, strategy, authorization, group)
            restored = AutoTradeState(state.db_path)
            issued = restored.load(**self._binding(grant))
            self.assertEqual(issued.grant, grant)
            self.assertEqual(issued.prepared_mutation.to_dict(), prepared.to_dict())
            with self.assertRaises((ValueError, RuntimeError)):
                restored.claim(
                    **(self._binding(grant) | {"principal_id": "wrong-profile"}),
                    prepared_artifact_digest=grant.prepared_artifact_digest,
                    execution_binding_digest=grant.execution_binding_digest, now=grant.issued_at + 1,
                )

            def attempt(store):
                try:
                    return self._claim(store, grant)
                except (ValueError, RuntimeError):
                    return None

            with ThreadPoolExecutor(max_workers=2) as pool:
                claims = list(pool.map(attempt, (state, restored)))
            self.assertEqual(sum(isinstance(claim, self.authorization_model.GrantClaim) for claim in claims), 1)
            with self.assertRaises((ValueError, RuntimeError)):
                restored.load(**self._binding(grant))

    def test_expired_or_revoked_grant_returns_terminal_receipt_without_releasing_quota(self) -> None:
        for reason in ("grant_expired", "revoked"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tempdir:
                state, strategy, authorization, group = prepared_state(tempdir)
                authorization_expiry = int((NOW + timedelta(hours=24)).timestamp() * 1_000)
                issue_time = authorization_expiry - 100 if reason == "grant_expired" else int(NOW.timestamp() * 1_000)
                grant, prepared = self._issue(state, strategy, authorization, group, issued_at=issue_time)
                if reason == "revoked":
                    state.revoke_authorization(
                        strategy_id=strategy["strategy_id"], authorization_id=authorization["authorization_id"],
                        now=NOW + timedelta(milliseconds=1),
                    )
                    claim_time = issue_time + 2
                else:
                    self.assertEqual(grant.expires_at, authorization_expiry)
                    self.assertGreater(prepared.expires_at, grant.expires_at)
                    claim_time = grant.expires_at
                receipt = self._claim(state, grant, now=claim_time)
                self.assertIsInstance(receipt, self.authorization_model.GrantTerminalReceipt)
                self.assertEqual(receipt.reason, reason)
                self.assertEqual(receipt.outcome_digest, self.authorization_model.NOT_SENT_TERMINAL_OUTCOME_DIGEST)
                self.assertEqual(state.get_usage(usage_id=group["legs"][0]["usage_id"])["reserved_amount_u"], "10")
                with self.assertRaises((ValueError, RuntimeError)):
                    self._claim(state, grant, now=claim_time)

    def test_interrupted_native_claim_and_unprojected_partial_hold_reserved_quota(self) -> None:
        for terminal in (False, True):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as tempdir:
                state, strategy, authorization, group = prepared_state(tempdir, batch=True)
                grant, _prepared = self._issue(state, strategy, authorization, group)
                claim = self._claim(state, grant)
                outcome = {"class": "partial", "grant_disposition": "review_required", "write_attempted": True}
                binding = {"submission_group_id": group["submission_group_id"], "grant": grant}
                if terminal:
                    state.finalize(
                        **self._binding(grant), claim_id=claim.claim_id, terminal_state="REVIEW_REQUIRED",
                        outcome_digest=self.execution_model.canonical_object_digest(outcome),
                    )
                    self.assertTrue(state.execution_grant_outcome_matches(**binding, outcome=outcome))
                    self.assertFalse(state.execution_grant_outcome_matches(
                        **binding, outcome={**outcome, "class": "unknown"},
                    ))
                    self.assertFalse(state.execution_grant_outcome_matches(
                        **{**binding, "submission_group_id": "wrong-group"}, outcome=outcome,
                    ))
                    state.settle_usage(usage_id=group["legs"][0]["usage_id"], outcome="ACCEPTED", now=NOW)

                restored = AutoTradeState(state.db_path)
                restored.recover_interrupted_grants()
                restored.recover_interrupted_grants()
                first, second = [restored.get_usage(usage_id=item["usage_id"]) for item in group["legs"]]
                self.assertEqual(second["status"], "REVIEW_REQUIRED")
                self.assertEqual(second["reserved_amount_u"], "10" if terminal else "20")
                self.assertEqual(first["status"], "ACCEPTED" if terminal else "REVIEW_REQUIRED")
                with self.assertRaises((ValueError, RuntimeError)):
                    self._claim(restored, grant)

    def test_native_group_cannot_release_usage_from_absence(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state, strategy, authorization, group = prepared_state(tempdir)
            grant, _prepared = self._issue(state, strategy, authorization, group)
            self._claim(state, grant)
            state.recover_interrupted_grants()
            leg = group["legs"][0]
            evidence = build_verified_usage_evidence({
                "outcome": "RELEASED", "evidence_source": "WEEX_SPOT_ORDER_NOT_FOUND", "weex_order_id": None,
                "usage_id": leg["usage_id"], "strategy_id": strategy["strategy_id"],
                "authorization_id": authorization["authorization_id"], "submission_group_id": group["submission_group_id"],
                "leg_id": leg["leg_id"], "client_order_id": leg["client_order_id"], "module": "SPOT", "symbol": "BTCUSDT",
                "side": "BUY", "order_type": "MARKET", "quantity": "1", "price": None,
            })
            with self.assertRaises((ValueError, RuntimeError)):
                state.resolve_uncertain_usage(verified_evidence=evidence, confirm_live=True, now=NOW)
            self.assertEqual(state.get_usage(usage_id=leg["usage_id"])["reserved_amount_u"], "10")


if __name__ == "__main__":
    unittest.main()
