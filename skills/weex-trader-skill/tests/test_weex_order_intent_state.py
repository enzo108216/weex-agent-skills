#!/usr/bin/env python3
from __future__ import annotations

import dataclasses
import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import weex_order_intent_state as intent_state


@dataclasses.dataclass(frozen=True)
class FakePrecondition:
    policy: str
    digest: str
    observed_at: int
    expires_at: int
    facts: dict[str, object]


@dataclasses.dataclass(frozen=True)
class FakePreparedMutation:
    contract_version: str
    tool_name: str
    action: str
    market: str
    trading_mode: str
    canonical_arguments: dict[str, object]
    canonical_arguments_digest: str
    risk_facts_digest: str
    route_fingerprint: str
    credential_fingerprint: str
    precondition: FakePrecondition
    risk_facts: dict[str, object]
    reconciliation_template: dict[str, object]
    expires_at: int
    execution_binding_digest: str
    prepared_artifact_digest: str


@dataclasses.dataclass(frozen=True)
class FakeExecutionGrant:
    grant_id: str
    state: str
    principal_id: str
    account_binding_id: str
    credential_fingerprint: str
    tool_name: str
    prepared_artifact_digest: str
    execution_binding_digest: str
    issued_at: int
    expires_at: int
    claim_id: str | None = None
    terminal_state: str | None = None
    outcome_digest: str | None = None


@dataclasses.dataclass(frozen=True)
class FakeIssuedGrant:
    grant: FakeExecutionGrant
    prepared_mutation: FakePreparedMutation
    confirmed_at: int


@dataclasses.dataclass(frozen=True)
class FakeGrantClaim:
    grant_id: str
    claim_id: str
    state: str
    claimed_at: int


@dataclasses.dataclass(frozen=True)
class FakeGrantTerminalReceipt:
    grant_id: str
    state: str
    terminal_state: str
    resolved_at: int
    reason: str
    outcome_digest: str


@dataclasses.dataclass(frozen=True)
class FakeGrantRecovery:
    grant_id: str
    state: str
    recovered_at: int
    reason: str


def fake_runtime_models():
    return SimpleNamespace(
        Precondition=FakePrecondition,
        PreparedMutation=FakePreparedMutation,
        ExecutionGrant=FakeExecutionGrant,
        IssuedGrant=FakeIssuedGrant,
        GrantClaim=FakeGrantClaim,
        GrantTerminalReceipt=FakeGrantTerminalReceipt,
        GrantRecovery=FakeGrantRecovery,
        NOT_SENT_TERMINAL_OUTCOME_DIGEST=intent_state.NOT_SENT_TERMINAL_OUTCOME_DIGEST,
        verify_prepared_mutation=lambda prepared: None,
    )


def prepared_mutation(*, expires_at: int = 10_000) -> dict[str, object]:
    return {
        "contract_version": "1",
        "tool_name": "trade_place_order",
        "action": "place_order",
        "market": "spot",
        "trading_mode": "live",
        "canonical_arguments": {
            "market": "spot",
            "trading_mode": "live",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": "1",
            "client_order_id": "client-1",
        },
        "canonical_arguments_digest": "sha256:" + "a" * 64,
        "risk_facts_digest": "sha256:" + "b" * 64,
        "route_fingerprint": "sha256:" + "c" * 64,
        "credential_fingerprint": "credential-fingerprint",
        "precondition": {
            "policy": "STATIC",
            "digest": "sha256:" + "d" * 64,
            "observed_at": 100,
            "expires_at": expires_at,
            "facts": {},
        },
        "risk_facts": {"level": "normal"},
        "reconciliation_template": {
            "strategy": "query_exact_identity",
            "read_tool": "account_get_orders",
            "arguments": {
                "market": "spot",
                "trading_mode": "live",
                "client_order_id": "client-1",
            },
            "expected": {"client_order_id": "client-1"},
            "identity": {"client_order_id": "client-1"},
            "completeness": "deterministic",
        },
        "expires_at": expires_at,
        "execution_binding_digest": "sha256:" + "e" * 64,
        "prepared_artifact_digest": "sha256:" + "f" * 64,
    }


def profile_binding() -> dict[str, object]:
    return {
        "profile_id": "profile-id",
        "profile_name": "main",
        "principal_id": "principal-id",
        "account_binding_id": "account-id",
        "spot_base_url": "https://api-spot.weex.com",
        "futures_base_url": "https://api-contract.weex.com",
        "route_fingerprint": "sha256:" + "c" * 64,
        "credential_fingerprint": "credential-fingerprint",
    }


def contract_versions() -> dict[str, str]:
    return {
        "package": "0.3.4",
        "tool_contract": "2",
        "prepare": "1",
        "intent": "2",
        "grant": "2",
        "boundary_contract_digest": (
            "sha256:15a8e64916a3227b32c431afae9b6b9de55ca350cf2291c6722f33694b828e38"
        ),
    }


def previous_contract_versions() -> dict[str, str]:
    return {
        **contract_versions(),
        "package": "0.3.1",
        "boundary_contract_digest": (
            "sha256:a6762cafe7b9338b658252e6048582c2be8fca0bc1b5c730223ff807868be9b9"
        ),
    }


def execution_grant(*, expires_at: int = 10_000) -> FakeExecutionGrant:
    return FakeExecutionGrant(
        grant_id="grant-1",
        state="ISSUED",
        principal_id="principal-id",
        account_binding_id="account-id",
        credential_fingerprint="credential-fingerprint",
        tool_name="trade_place_order",
        prepared_artifact_digest="sha256:" + "f" * 64,
        execution_binding_digest="sha256:" + "e" * 64,
        issued_at=200,
        expires_at=expires_at,
    )


class ManualIntentStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.environment = mock.patch.dict(
            os.environ,
            {"WEEX_TRADER_SKILL_HOME": self.tempdir.name},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def build_intent(
        self,
        *,
        intent_id: str = "intent-1",
        expires_at: int = 10_000,
    ) -> dict[str, object]:
        return intent_state.build_unclaimed_intent(
            prepared_mutation=prepared_mutation(expires_at=expires_at),
            profile_binding=profile_binding(),
            contract_versions=contract_versions(),
            intent_id=intent_id,
            created_at=100,
        )

    def store(self):
        return intent_state.FileManualIntentStore(
            runtime_loader=fake_runtime_models,
            clock=lambda: 500,
        )

    def write_raw_state(self, value: object, *, encoded: bool = True) -> None:
        text = json.dumps(value) if encoded else str(value)
        intent_state.intent_path().write_text(text, encoding="utf-8")
        os.chmod(intent_state.intent_path(), 0o600)

    def previous_intent(self, state: str, *, intent_id: str = "intent-1") -> dict:
        intent = self.build_intent(intent_id=intent_id)
        intent["contract_versions"] = previous_contract_versions()
        intent["state"] = state
        if state != "UNCLAIMED":
            intent["confirmed_at"] = 200
            intent["grant"] = {
                field: value
                for field, value in dataclasses.asdict(execution_grant()).items()
                if value is not None
            }
        if state in {"CLAIMED", "REVIEW_REQUIRED", "ACCEPTED"}:
            intent["grant"].update(state=state, claim_id="claim-1")
        if state in {"REVIEW_REQUIRED", "ACCEPTED"}:
            intent["grant"].update(
                terminal_state=state, outcome_digest="sha256:" + "9" * 64,
            )
        if state == "REVIEW_REQUIRED":
            intent["reconciliation"]["status"] = "PENDING"
        return intent

    def test_new_intents_reject_previous_candidate_contract_versions(self) -> None:
        with self.assertRaises(intent_state.IntentStateError) as old_contract:
            intent_state.build_unclaimed_intent(
                prepared_mutation=prepared_mutation(),
                profile_binding=profile_binding(),
                contract_versions=previous_contract_versions(),
                intent_id="intent-1",
                created_at=100,
            )
        self.assertEqual(old_contract.exception.code, "intent_invalid")
        with self.assertRaises(intent_state.IntentStateError) as old_save:
            intent_state.save_unclaimed_intent(
                self.previous_intent("UNCLAIMED"), now_ms=100,
            )
        self.assertEqual(old_save.exception.code, "intent_invalid")
        self.assertIsNone(intent_state.load_manual_intent(now_ms=100))

    def test_previous_unclaimed_intent_is_invalidated_before_expiry(self) -> None:
        previous = self.previous_intent("UNCLAIMED")
        self.write_raw_state(previous)

        migrated = intent_state.load_manual_intent(now_ms=300)

        self.assertEqual(migrated["state"], "LEGACY_INVALIDATED")
        self.assertEqual(migrated["legacy"]["source_state"], "UNCLAIMED")
        self.assertFalse(migrated["legacy"]["source_started"])
        self.assertEqual(intent_state.load_terminal_audit_records()[0]["intent"], migrated)
        intent_state.save_unclaimed_intent(
            self.build_intent(intent_id="replacement"), now_ms=400,
        )
        self.assertEqual(intent_state.load_manual_intent(now_ms=400)["intent_id"], "replacement")

    def test_previous_confirmed_intent_is_audited_as_known_not_sent(self) -> None:
        self.write_raw_state(self.previous_intent("CONFIRMED"))
        grant_identity = {
            "grant_id": "grant-1",
            "principal_id": "principal-id",
            "account_binding_id": "account-id",
            "tool_name": "trade_place_order",
        }
        with self.assertRaises(intent_state.IntentStateError) as old_dispatch:
            intent_state.execution_grant_for_dispatch(
                "intent-1", runtime_loader=fake_runtime_models, now_ms=300,
            )
        self.assertEqual(old_dispatch.exception.code, "grant_unavailable")
        with self.assertRaises(intent_state.IntentStateError):
            self.store().load(**grant_identity)
        with self.assertRaises(intent_state.IntentStateError):
            self.store().claim(
                **grant_identity,
                prepared_artifact_digest="sha256:" + "f" * 64,
                execution_binding_digest="sha256:" + "e" * 64,
                now=300,
            )

        terminal = intent_state.load_manual_intent(now_ms=300)
        self.assertEqual(terminal["state"], "NOT_SENT_TERMINAL")
        self.assertEqual(terminal["grant"]["grant_id"], grant_identity["grant_id"])
        self.assertEqual(
            terminal["grant"]["outcome_digest"],
            intent_state.NOT_SENT_TERMINAL_OUTCOME_DIGEST,
        )
        self.assertEqual(
            [record["intent"] for record in intent_state.load_terminal_audit_records()],
            [terminal],
        )
        intent_state.ensure_manual_intent_slot_available(now_ms=400)

    def test_previous_confirmed_intent_keeps_original_state_if_audit_fails(self) -> None:
        self.write_raw_state(self.previous_intent("CONFIRMED"))
        original_bytes = intent_state.intent_path().read_bytes()

        with mock.patch.object(
            intent_state,
            "_append_terminal_audit_locked",
            side_effect=intent_state.IntentStateError(
                "intent_audit_unavailable", "audit unavailable",
            ),
        ):
            with self.assertRaises(intent_state.IntentStateError) as failed:
                intent_state.ensure_manual_intent_slot_available(now_ms=300)
        self.assertEqual(failed.exception.code, "intent_audit_unavailable")
        self.assertEqual(intent_state.intent_path().read_bytes(), original_bytes)

    def test_previous_claimed_intent_can_recover_but_cannot_finalize_as_a_write(self) -> None:
        previous = self.previous_intent("CLAIMED")
        self.write_raw_state(previous)
        grant_identity = {
            "grant_id": "grant-1",
            "principal_id": "principal-id",
            "account_binding_id": "account-id",
            "tool_name": "trade_place_order",
        }

        with self.assertRaises(intent_state.IntentStateError):
            self.store().finalize(
                **grant_identity, claim_id="claim-1", terminal_state="ACCEPTED",
                outcome_digest="sha256:" + "9" * 64,
            )
        recovered_grant = intent_state.claimed_execution_grant_for_recovery(
            "intent-1", runtime_loader=fake_runtime_models, now_ms=300,
        )
        self.assertEqual(recovered_grant.claim_id, "claim-1")
        self.store().recover(**grant_identity, now=400)
        review = intent_state.load_manual_intent(now_ms=500)
        self.assertEqual(review["state"], "REVIEW_REQUIRED")
        self.assertEqual(review["prepared_mutation"], previous["prepared_mutation"])
        with self.assertRaises(intent_state.IntentStateError):
            intent_state.ensure_manual_intent_slot_available(now_ms=500)

    def test_previous_review_intent_keeps_template_and_blocks_replacement_and_dispatch(self) -> None:
        previous = self.previous_intent("REVIEW_REQUIRED")
        self.write_raw_state(previous)
        self.assertEqual(intent_state.load_manual_intent(now_ms=300), previous)
        self.assertEqual(
            intent_state.reconciliation_template_for_intent("intent-1"),
            previous["prepared_mutation"]["reconciliation_template"],
        )
        with self.assertRaises(intent_state.IntentStateError):
            intent_state.execution_grant_for_dispatch(
                "intent-1", runtime_loader=fake_runtime_models, now_ms=300,
            )
        with self.assertRaises(intent_state.IntentStateError) as conflict:
            intent_state.save_unclaimed_intent(
                self.build_intent(intent_id="replacement"), now_ms=300,
            )
        self.assertEqual(conflict.exception.code, "intent_conflict")

    def test_previous_terminal_audit_is_read_without_rewriting_record_bytes(self) -> None:
        previous = self.previous_intent("ACCEPTED", intent_id="previous-terminal")
        body = {"audit_version": 1, "archived_at": 250, "intent": previous}
        record = {**body, "record_digest": intent_state._canonical_digest(body)}
        audit_path = intent_state.audit_path()
        audit_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        os.chmod(audit_path, 0o600)
        original_bytes = audit_path.read_bytes()

        self.assertEqual(intent_state.load_terminal_audit_records(), [record])
        self.assertEqual(audit_path.read_bytes(), original_bytes)

    def test_032_intents_use_historical_rules_only_after_the_pin_advances(self) -> None:
        historical = {
            "package": "0.3.2", "tool_contract": "2", "prepare": "1", "intent": "2", "grant": "2",
            "boundary_contract_digest": "sha256:a08c5997ab1ef4698ad1bd37ff89378fedf340600dde668d0f9fc5fc256b7b10",
        }
        old_intents = {
            state: {
                **self.previous_intent(state, intent_id=f"intent-032-{state.lower()}"),
                "contract_versions": historical,
            }
            for state in ("UNCLAIMED", "CONFIRMED", "CLAIMED", "REVIEW_REQUIRED")
        }
        grant_identity = {
            "grant_id": "grant-1", "principal_id": "principal-id",
            "account_binding_id": "account-id", "tool_name": "trade_place_order",
        }
        self.write_raw_state(old_intents["UNCLAIMED"])
        with mock.patch.object(intent_state, "_INTENT_CONTRACT_VERSIONS", historical):
            self.assertEqual(intent_state.load_manual_intent(now_ms=300), old_intents["UNCLAIMED"])

        next_pin = {**historical, "package": "0.3.4", "boundary_contract_digest": "sha256:" + "8" * 64}
        with mock.patch.object(intent_state, "_INTENT_CONTRACT_VERSIONS", next_pin):
            for original, expected in (
                ("UNCLAIMED", "LEGACY_INVALIDATED"), ("CONFIRMED", "NOT_SENT_TERMINAL"),
                ("CLAIMED", "CLAIMED"), ("REVIEW_REQUIRED", "REVIEW_REQUIRED"),
            ):
                with self.subTest(original=original):
                    previous = old_intents[original]
                    self.write_raw_state(previous)
                    restored = intent_state.load_manual_intent(now_ms=400)
                    self.assertEqual(restored["state"], expected)
                    with self.assertRaises(intent_state.IntentStateError):
                        intent_state.execution_grant_for_dispatch(
                            previous["intent_id"], runtime_loader=fake_runtime_models, now_ms=400,
                        )
                    if original == "CLAIMED":
                        recovered = intent_state.claimed_execution_grant_for_recovery(
                            previous["intent_id"], runtime_loader=fake_runtime_models, now_ms=400,
                        )
                        self.assertEqual(recovered.claim_id, "claim-1")
                        self.store().recover(**grant_identity, now=500)
                        restored = intent_state.load_manual_intent(now_ms=500)
                        self.assertEqual(restored["state"], "REVIEW_REQUIRED")
                    if original in {"CLAIMED", "REVIEW_REQUIRED"}:
                        self.assertEqual(restored["prepared_mutation"], previous["prepared_mutation"])
                        with self.assertRaises(intent_state.IntentStateError):
                            intent_state.ensure_manual_intent_slot_available(now_ms=500)
            self.assertEqual(
                [record["intent"]["state"] for record in intent_state.load_terminal_audit_records()],
                ["LEGACY_INVALIDATED", "NOT_SENT_TERMINAL"],
            )
            self.write_raw_state({
                **old_intents["REVIEW_REQUIRED"],
                "contract_versions": {**historical, "boundary_contract_digest": "sha256:" + "9" * 64},
            })
            with self.assertRaises(intent_state.IntentStateError):
                intent_state.load_manual_intent(now_ms=600)

    def test_build_unclaimed_intent_uses_prepared_values_without_scaling_expiry(self) -> None:
        prepared = prepared_mutation(expires_at=12_345)

        intent = intent_state.build_unclaimed_intent(
            prepared_mutation=prepared,
            profile_binding=profile_binding(),
            contract_versions=contract_versions(),
            intent_id="intent-1",
            created_at=100,
        )

        self.assertEqual(intent["intent_version"], 2)
        self.assertEqual(intent["state"], "UNCLAIMED")
        self.assertEqual(intent["expires_at"], 12_345)
        self.assertEqual(intent["prepared_mutation"], prepared)
        self.assertEqual(intent["direct_arguments"], prepared["canonical_arguments"])
        self.assertEqual(intent["request_identity"], {"client_order_id": "client-1"})
        self.assertIsNone(intent["confirmed_at"])
        self.assertIsNone(intent["grant"])
        self.assertIsNone(intent["legacy"])
        self.assertNotIn("risk_signature", intent)
        self.assertNotIn("api_secret", json.dumps(intent, sort_keys=True))

    def test_strict_load_migrates_sparse_legacy_and_rejects_corruption(self) -> None:
        path = intent_state.intent_path()
        self.write_raw_state("{", encoded=False)
        with self.assertRaises(intent_state.IntentStateError) as corrupt:
            intent_state.load_manual_intent(now_ms=200)
        self.assertEqual(corrupt.exception.code, "intent_state_corrupt")

        self.write_raw_state(
            {
                "intent_id": "legacy-open",
                "risk_signature": "legacy-risk",
                "expires_at": 10_000,
            }
        )
        migrated = intent_state.load_manual_intent(now_ms=200)
        self.assertEqual(
            set(migrated),
            {"intent_version", "intent_id", "state", "reconciliation", "legacy"},
        )
        self.assertEqual(migrated["state"], "LEGACY_INVALIDATED")
        self.assertFalse(migrated["legacy"]["source_started"])
        self.assertTrue(intent_state.audit_path().is_file())

        self.write_raw_state(
            {
                "intent_id": "legacy-started",
                "risk_signature": "legacy-risk",
                "mcp_trade_confirmation_started_at": 150,
            }
        )
        started = intent_state.load_manual_intent(now_ms=200)
        self.assertEqual(started["state"], "REVIEW_REQUIRED")
        self.assertEqual(
            started["reconciliation"]["status"],
            "MANUAL_REVIEW_REQUIRED",
        )
        self.assertTrue(started["legacy"]["source_started"])
        self.assertNotIn("prepared_mutation", started)
        self.assertNotIn("grant", started)

        legacy_states = (
            ("trade_status", "UNKNOWN", "REVIEW_REQUIRED"),
            ("status", "PARTIAL", "REVIEW_REQUIRED"),
            ("trade_status", "ACCEPTED", "ACCEPTED"),
            ("status", "REJECTED", "DEFINITIVE_REJECTION"),
            ("trade_status", "NOT_SENT", "NOT_SENT_TERMINAL"),
            ("status", "unrecognized", "REVIEW_REQUIRED"),
        )
        for index, (field, source_state, expected_state) in enumerate(
            legacy_states
        ):
            with self.subTest(field=field, source_state=source_state):
                self.write_raw_state(
                    {
                        "intent_id": f"legacy-state-{index}",
                        "risk_signature": "legacy-risk",
                        field: source_state,
                    }
                )
                migrated_state = intent_state.load_manual_intent(now_ms=300 + index)
                self.assertEqual(migrated_state["state"], expected_state)
                self.assertEqual(
                    migrated_state["legacy"]["source_state"],
                    source_state,
                )
                self.assertEqual(
                    migrated_state["legacy"]["source_started"],
                    expected_state != "LEGACY_INVALIDATED",
                )

    def test_single_slot_blocks_active_and_archives_expired_unclaimed_before_replace(self) -> None:
        first = self.build_intent(intent_id="intent-first", expires_at=500)
        second = self.build_intent(intent_id="intent-second", expires_at=1_000)
        intent_state.save_unclaimed_intent(first, now_ms=100)

        with self.assertRaises(intent_state.IntentStateError):
            intent_state.ensure_manual_intent_slot_available(now_ms=200)
        with self.assertRaises(intent_state.IntentStateError) as active:
            intent_state.save_unclaimed_intent(second, now_ms=200)
        self.assertEqual(active.exception.code, "intent_conflict")

        intent_state.save_unclaimed_intent(second, now_ms=600)
        current = intent_state.load_manual_intent(now_ms=600)
        self.assertEqual(current["intent_id"], "intent-second")
        records = intent_state.load_terminal_audit_records()
        self.assertEqual(records[-1]["intent"]["intent_id"], "intent-first")
        self.assertEqual(records[-1]["intent"]["state"], "LEGACY_INVALIDATED")

    @unittest.skipIf(os.name == "nt", "POSIX owner-only mode and file-lock check")
    def test_dispatch_lock_excludes_another_process_and_releases_on_exit(self) -> None:
        worker = "\n".join((
            "import os, sys",
            "sys.path.insert(0, sys.argv[1])",
            "from weex_order_intent_state import IntentStateError, manual_intent_dispatch_lock",
            "try:",
            "    with manual_intent_dispatch_lock():",
            "        print('acquired', flush=True)",
            "        os._exit(0)",
            "except IntentStateError as exc:",
            "    print(exc.code)",
        ))
        command = [sys.executable, "-B", "-c", worker, str(SCRIPTS)]
        with intent_state.manual_intent_dispatch_lock():
            lock_files = list(Path(self.tempdir.name).glob(".*dispatch.lock"))
            self.assertEqual(len(lock_files), 1)
            self.assertEqual(lock_files[0].stat().st_mode & 0o777, 0o600)
            blocked = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
            self.assertEqual(blocked.stdout.strip(), "intent_dispatch_in_flight")

        crashed = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(crashed.stdout.strip(), "acquired")
        with self.assertRaisesRegex(RuntimeError, "dispatch failed"):
            with intent_state.manual_intent_dispatch_lock():
                raise RuntimeError("dispatch failed")
        with intent_state.manual_intent_dispatch_lock():
            self.assertTrue(lock_files[0].is_file())

    def test_confirm_and_issue_is_atomic_and_mcp_claims_once(self) -> None:
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        confirmed = intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        self.assertEqual(confirmed["state"], "CONFIRMED")
        self.assertEqual(confirmed["grant"]["state"], "ISSUED")
        issued = self.store().load(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
        )
        self.assertIsInstance(issued, FakeIssuedGrant)

        results: list[object] = []
        lock = threading.Lock()

        def claim() -> None:
            try:
                result = self.store().claim(
                    grant_id="grant-1",
                    principal_id="principal-id",
                    account_binding_id="account-id",
                    tool_name="trade_place_order",
                    prepared_artifact_digest="sha256:" + "f" * 64,
                    execution_binding_digest="sha256:" + "e" * 64,
                    now=300,
                )
            except Exception as exc:
                result = exc
            with lock:
                results.append(result)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(
            sum(isinstance(item, FakeGrantClaim) for item in results),
            1,
        )
        self.assertEqual(
            intent_state.load_manual_intent(now_ms=300)["state"],
            "CLAIMED",
        )

    @unittest.skipIf(os.name == "nt", "POSIX subprocess environment syntax")
    def test_two_processes_can_claim_the_same_grant_only_once(self) -> None:
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        helper_root = Path(self.tempdir.name) / "helpers"
        helper_root.mkdir()
        (helper_root / "fake_claim_models.py").write_text(
            "\n".join(
                (
                    "from dataclasses import dataclass",
                    "from types import SimpleNamespace",
                    "@dataclass(frozen=True)",
                    "class Precondition:",
                    "    policy: str",
                    "    digest: str",
                    "    observed_at: int",
                    "    expires_at: int",
                    "    facts: dict",
                    "@dataclass(frozen=True)",
                    "class PreparedMutation:",
                    "    contract_version: str",
                    "    tool_name: str",
                    "    action: str",
                    "    market: str",
                    "    trading_mode: str",
                    "    canonical_arguments: dict",
                    "    canonical_arguments_digest: str",
                    "    risk_facts_digest: str",
                    "    route_fingerprint: str",
                    "    credential_fingerprint: str",
                    "    precondition: Precondition",
                    "    risk_facts: dict",
                    "    reconciliation_template: dict",
                    "    expires_at: int",
                    "    execution_binding_digest: str",
                    "    prepared_artifact_digest: str",
                    "@dataclass(frozen=True)",
                    "class ExecutionGrant:",
                    "    grant_id: str",
                    "    state: str",
                    "    principal_id: str",
                    "    account_binding_id: str",
                    "    credential_fingerprint: str",
                    "    tool_name: str",
                    "    prepared_artifact_digest: str",
                    "    execution_binding_digest: str",
                    "    issued_at: int",
                    "    expires_at: int",
                    "    claim_id: str | None = None",
                    "    terminal_state: str | None = None",
                    "    outcome_digest: str | None = None",
                    "@dataclass(frozen=True)",
                    "class IssuedGrant:",
                    "    grant: ExecutionGrant",
                    "    prepared_mutation: PreparedMutation",
                    "    confirmed_at: int",
                    "@dataclass(frozen=True)",
                    "class GrantClaim:",
                    "    grant_id: str",
                    "    claim_id: str",
                    "    state: str",
                    "    claimed_at: int",
                    "def models():",
                    "    return SimpleNamespace(",
                    "        Precondition=Precondition,",
                    "        PreparedMutation=PreparedMutation,",
                    "        ExecutionGrant=ExecutionGrant,",
                    "        IssuedGrant=IssuedGrant,",
                    "        GrantClaim=GrantClaim,",
                    "        verify_prepared_mutation=lambda prepared: None,",
                    "    )",
                    "",
                )
            ),
            encoding="utf-8",
        )
        worker = "\n".join(
            (
                "from fake_claim_models import models",
                "from weex_order_intent_state import FileManualIntentStore",
                "store = FileManualIntentStore(runtime_loader=models)",
                "try:",
                "    result = store.claim(",
                "        grant_id='grant-1',",
                "        principal_id='principal-id',",
                "        account_binding_id='account-id',",
                "        tool_name='trade_place_order',",
                "        prepared_artifact_digest='sha256:' + 'f' * 64,",
                "        execution_binding_digest='sha256:' + 'e' * 64,",
                "        now=300,",
                "    )",
                "except Exception:",
                "    print('ERROR')",
                "else:",
                "    print(result.state)",
            )
        )
        child_environment = dict(os.environ)
        child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        child_environment["PYTHONPATH"] = os.pathsep.join(
            (str(helper_root), str(SCRIPTS))
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", worker],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_environment,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=10) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0, 0])
        self.assertEqual(
            sorted(stdout.strip() for stdout, _stderr in results),
            ["CLAIMED", "ERROR"],
        )

    def test_confirm_write_failure_leaves_unclaimed_intent_without_half_grant(self) -> None:
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        original = intent_state.intent_path().read_bytes()

        with mock.patch.object(
            intent_state,
            "_atomic_write_json",
            side_effect=OSError("write failed"),
        ):
            with self.assertRaises(intent_state.IntentStateError):
                intent_state.confirm_and_issue_intent(
                    expected_intent_id="intent-1",
                    expected_prepared_artifact_digest="sha256:" + "f" * 64,
                    execution_grant=execution_grant(),
                    confirmed_at=200,
                )

        self.assertEqual(intent_state.intent_path().read_bytes(), original)
        self.assertEqual(
            intent_state.load_manual_intent(now_ms=200)["state"],
            "UNCLAIMED",
        )

    def test_grant_return_model_failure_never_commits_durable_state(self) -> None:
        runtime = fake_runtime_models()
        runtime.GrantClaim = lambda **_kwargs: (_ for _ in ()).throw(
            ValueError("claim model rejected")
        )
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        before = intent_state.intent_path().read_bytes()
        with self.assertRaises(ValueError):
            intent_state.FileManualIntentStore(
                runtime_loader=lambda: runtime,
            ).claim(
                grant_id="grant-1",
                principal_id="principal-id",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                prepared_artifact_digest="sha256:" + "f" * 64,
                execution_binding_digest="sha256:" + "e" * 64,
                now=300,
            )
        self.assertEqual(intent_state.intent_path().read_bytes(), before)
        self.assertFalse(intent_state.audit_path().exists())

        intent_state._unlink_if_exists(intent_state.intent_path())
        expiring = self.build_intent(intent_id="intent-expiring", expires_at=250)
        intent_state.save_unclaimed_intent(expiring, now_ms=100)
        expiring_grant = dataclasses.replace(
            execution_grant(expires_at=250),
            grant_id="grant-expiring",
        )
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-expiring",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=expiring_grant,
            confirmed_at=200,
        )
        runtime = fake_runtime_models()
        runtime.GrantTerminalReceipt = lambda **_kwargs: (
            _ for _ in ()
        ).throw(ValueError("receipt model rejected"))
        before = intent_state.intent_path().read_bytes()
        with self.assertRaises(ValueError):
            intent_state.FileManualIntentStore(
                runtime_loader=lambda: runtime,
            ).claim(
                grant_id="grant-expiring",
                principal_id="principal-id",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                prepared_artifact_digest="sha256:" + "f" * 64,
                execution_binding_digest="sha256:" + "e" * 64,
                now=300,
            )
        self.assertEqual(intent_state.intent_path().read_bytes(), before)
        self.assertFalse(intent_state.audit_path().exists())

        intent_state._unlink_if_exists(intent_state.intent_path())
        intent_state.save_unclaimed_intent(
            self.build_intent(intent_id="intent-recovery"),
            now_ms=100,
        )
        recovery_grant = dataclasses.replace(
            execution_grant(),
            grant_id="grant-recovery",
        )
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-recovery",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=recovery_grant,
            confirmed_at=200,
        )
        self.store().claim(
            grant_id="grant-recovery",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            prepared_artifact_digest="sha256:" + "f" * 64,
            execution_binding_digest="sha256:" + "e" * 64,
            now=300,
        )
        runtime = fake_runtime_models()
        runtime.GrantRecovery = lambda **_kwargs: (_ for _ in ()).throw(
            ValueError("recovery model rejected")
        )
        before = intent_state.intent_path().read_bytes()
        with self.assertRaises(ValueError):
            intent_state.FileManualIntentStore(
                runtime_loader=lambda: runtime,
            ).recover(
                grant_id="grant-recovery",
                principal_id="principal-id",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                now=400,
            )
        self.assertEqual(intent_state.intent_path().read_bytes(), before)

    def test_expired_claim_requires_durable_audit_before_terminal_receipt(self) -> None:
        intent_state.save_unclaimed_intent(
            self.build_intent(expires_at=250),
            now_ms=100,
        )
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(expires_at=250),
            confirmed_at=200,
        )
        store = self.store()

        with mock.patch.object(
            intent_state,
            "_append_terminal_audit_locked",
            side_effect=intent_state.IntentStateError(
                "intent_audit_unavailable",
                "audit unavailable",
            ),
        ):
            with self.assertRaises(intent_state.IntentStateError):
                store.claim(
                    grant_id="grant-1",
                    principal_id="principal-id",
                    account_binding_id="account-id",
                    tool_name="trade_place_order",
                    prepared_artifact_digest="sha256:" + "f" * 64,
                    execution_binding_digest="sha256:" + "e" * 64,
                    now=300,
                )
        self.assertEqual(
            intent_state.load_manual_intent(now_ms=300)["state"],
            "CONFIRMED",
        )

        receipt = store.claim(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            prepared_artifact_digest="sha256:" + "f" * 64,
            execution_binding_digest="sha256:" + "e" * 64,
            now=300,
        )
        self.assertIsInstance(receipt, FakeGrantTerminalReceipt)
        self.assertEqual(receipt.reason, "grant_expired")
        self.assertEqual(
            receipt.outcome_digest,
            intent_state.NOT_SENT_TERMINAL_OUTCOME_DIGEST,
        )
        self.assertEqual(
            intent_state.load_manual_intent(now_ms=300)["state"],
            "NOT_SENT_TERMINAL",
        )
        self.assertEqual(intent_state.audit_path().stat().st_mode & 0o777, 0o600)
        audit_text = intent_state.audit_path().read_text(encoding="utf-8")
        self.assertNotIn("api_secret", audit_text)
        record = intent_state.load_terminal_audit_records()[-1]
        body = dict(record)
        stored_digest = body.pop("record_digest")
        self.assertEqual(stored_digest, intent_state._canonical_digest(body))

    def test_load_and_binding_mismatch_leave_confirmed_file_byte_equivalent(self) -> None:
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        before = intent_state.intent_path().read_bytes()
        self.store().load(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
        )
        self.assertEqual(intent_state.intent_path().read_bytes(), before)
        with self.assertRaises(intent_state.IntentStateError):
            self.store().claim(
                grant_id="grant-1",
                principal_id="wrong-principal",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                prepared_artifact_digest="sha256:" + "f" * 64,
                execution_binding_digest="sha256:" + "e" * 64,
                now=300,
            )
        self.assertEqual(intent_state.intent_path().read_bytes(), before)

    def test_claim_revalidates_stored_prepared_mutation_inside_transaction(self) -> None:
        runtime = fake_runtime_models()

        def verify(prepared):
            if prepared.risk_facts != {"level": "normal"}:
                raise ValueError("prepared mutation digest mismatch")

        runtime.verify_prepared_mutation = verify
        store = intent_state.FileManualIntentStore(
            runtime_loader=lambda: runtime,
            clock=lambda: 250,
        )
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        store.load(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
        )
        tampered = json.loads(intent_state.intent_path().read_text(encoding="utf-8"))
        tampered["prepared_mutation"]["risk_facts"] = {"tampered": True}
        tampered["risk_facts"] = {"tampered": True}
        intent_state._atomic_write_json(intent_state.intent_path(), tampered)
        tampered_bytes = intent_state.intent_path().read_bytes()

        with self.assertRaises(intent_state.IntentStateError):
            store.claim(
                grant_id="grant-1",
                principal_id="principal-id",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                prepared_artifact_digest="sha256:" + "f" * 64,
                execution_binding_digest="sha256:" + "e" * 64,
                now=300,
            )
        self.assertEqual(
            intent_state.load_manual_intent(now_ms=300)["state"],
            "CONFIRMED",
        )
        self.assertEqual(intent_state.intent_path().read_bytes(), tampered_bytes)

    def test_terminal_audit_overlay_is_authoritative_when_current_refresh_fails(self) -> None:
        store = self.store()
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        claim = store.claim(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            prepared_artifact_digest="sha256:" + "f" * 64,
            execution_binding_digest="sha256:" + "e" * 64,
            now=300,
        )
        real_write = intent_state._atomic_write_json
        with mock.patch.object(
            intent_state,
            "_atomic_write_json",
            side_effect=OSError("current refresh failed"),
        ):
            self.assertIsNone(
                store.finalize(
                    grant_id="grant-1",
                    principal_id="principal-id",
                    account_binding_id="account-id",
                    tool_name="trade_place_order",
                    claim_id=claim.claim_id,
                    terminal_state="ACCEPTED",
                    outcome_digest="sha256:" + "9" * 64,
                )
            )
        self.assertEqual(
            intent_state.load_manual_intent(now_ms=400)["state"],
            "ACCEPTED",
        )
        with mock.patch.object(intent_state, "_atomic_write_json", real_write):
            intent_state.save_unclaimed_intent(
                self.build_intent(intent_id="intent-2"),
                now_ms=400,
            )
        self.assertEqual(
            intent_state.load_manual_intent(now_ms=400)["intent_id"],
            "intent-2",
        )
        archived = intent_state.load_manual_intent(
            now_ms=400,
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
        )
        self.assertEqual((archived["intent_id"], archived["state"]), ("intent-1", "ACCEPTED"))
        self.assertIsNone(intent_state.load_manual_intent(
            now_ms=400,
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "b" * 64,
        ))

    def test_finalize_is_idempotent_and_recover_keeps_review_slot_blocked(self) -> None:
        store = self.store()
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        claim = store.claim(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            prepared_artifact_digest="sha256:" + "f" * 64,
            execution_binding_digest="sha256:" + "e" * 64,
            now=300,
        )
        outcome_digest = "sha256:" + "9" * 64
        self.assertIsNone(
            store.finalize(
                grant_id="grant-1",
                principal_id="principal-id",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                claim_id=claim.claim_id,
                terminal_state="ACCEPTED",
                outcome_digest=outcome_digest,
            )
        )
        with self.assertRaises(intent_state.IntentStateError):
            store.finalize(
                grant_id="grant-1",
                principal_id="principal-id",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                claim_id="wrong-claim",
                terminal_state="ACCEPTED",
                outcome_digest=outcome_digest,
            )
        self.assertIsNone(
            store.finalize(
                grant_id="grant-1",
                principal_id="principal-id",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                claim_id=claim.claim_id,
                terminal_state="ACCEPTED",
                outcome_digest=outcome_digest,
            )
        )
        with self.assertRaises(intent_state.IntentStateError):
            store.finalize(
                grant_id="grant-1",
                principal_id="principal-id",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                claim_id=claim.claim_id,
                terminal_state="DEFINITIVE_REJECTION",
                outcome_digest="sha256:" + "8" * 64,
            )

        intent_state.save_unclaimed_intent(
            self.build_intent(intent_id="intent-2"),
            now_ms=400,
        )
        second_grant = dataclasses.replace(execution_grant(), grant_id="grant-2")
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-2",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=second_grant,
            confirmed_at=450,
        )
        store.claim(
            grant_id="grant-2",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            prepared_artifact_digest="sha256:" + "f" * 64,
            execution_binding_digest="sha256:" + "e" * 64,
            now=460,
        )
        recovery = store.recover(
            grant_id="grant-2",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            now=500,
        )
        self.assertIsInstance(recovery, FakeGrantRecovery)
        self.assertEqual(
            intent_state.load_manual_intent(now_ms=500)["state"],
            "REVIEW_REQUIRED",
        )
        with self.assertRaises(intent_state.IntentStateError) as blocked:
            intent_state.save_unclaimed_intent(
                self.build_intent(intent_id="intent-3"),
                now_ms=600,
            )
        self.assertEqual(blocked.exception.code, "intent_conflict")

    def test_finalize_not_sent_requires_the_fixed_outcome_digest(self) -> None:
        store = self.store()
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        claim = store.claim(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            prepared_artifact_digest="sha256:" + "f" * 64,
            execution_binding_digest="sha256:" + "e" * 64,
            now=300,
        )
        before = intent_state.intent_path().read_bytes()
        with self.assertRaises(intent_state.IntentStateError):
            store.finalize(
                grant_id="grant-1",
                principal_id="principal-id",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                claim_id=claim.claim_id,
                terminal_state="NOT_SENT_TERMINAL",
                outcome_digest="sha256:" + "9" * 64,
            )
        self.assertEqual(intent_state.intent_path().read_bytes(), before)
        self.assertFalse(intent_state.audit_path().exists())
        self.assertIsNone(
            store.finalize(
                grant_id="grant-1",
                principal_id="principal-id",
                account_binding_id="account-id",
                tool_name="trade_place_order",
                claim_id=claim.claim_id,
                terminal_state="NOT_SENT_TERMINAL",
                outcome_digest=intent_state.NOT_SENT_TERMINAL_OUTCOME_DIGEST,
            )
        )
        tampered = json.loads(intent_state.intent_path().read_text(encoding="utf-8"))
        tampered["grant"]["outcome_digest"] = "sha256:" + "9" * 64
        self.write_raw_state(tampered)
        with self.assertRaises(intent_state.IntentStateError) as corrupt:
            intent_state.load_manual_intent(now_ms=400)
        self.assertEqual(corrupt.exception.code, "intent_state_corrupt")

    def test_claimed_grant_can_be_rehydrated_after_restart_for_host_recovery(self) -> None:
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        self.store().claim(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            prepared_artifact_digest="sha256:" + "f" * 64,
            execution_binding_digest="sha256:" + "e" * 64,
            now=300,
        )

        recovered_grant = intent_state.claimed_execution_grant_for_recovery(
            "intent-1",
            runtime_loader=fake_runtime_models,
            now_ms=400,
        )

        self.assertIsInstance(recovered_grant, FakeExecutionGrant)
        self.assertEqual(recovered_grant.state, "CLAIMED")
        self.assertIsNotNone(recovered_grant.claim_id)

    def test_invalid_resolved_legacy_record_cannot_release_slot(self) -> None:
        invalid = {
            "intent_version": 2,
            "intent_id": "legacy-invalid",
            "state": "REVIEW_REQUIRED",
            "reconciliation": {
                "status": "RESOLVED_ACCEPTED",
                "template": None,
                "result": None,
                "observed_at": None,
                "evidence_digest": None,
            },
            "legacy": {
                "source_version": None,
                "source_record_digest": "sha256:" + "1" * 64,
                "source_state": "UNKNOWN",
                "source_started": True,
                "migration": "REVIEW_REQUIRED",
                "migrated_at": 100,
            },
        }
        self.write_raw_state(invalid)
        with self.assertRaises(intent_state.IntentStateError) as caught:
            intent_state.load_manual_intent(now_ms=200)
        self.assertEqual(caught.exception.code, "intent_state_corrupt")

    @unittest.skipIf(os.name == "nt", "POSIX owner-only mode check")
    def test_state_and_audit_reads_fail_closed_when_mode_is_not_owner_only(self) -> None:
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        os.chmod(intent_state.intent_path(), 0o644)
        with self.assertRaises(intent_state.IntentStateError) as state_error:
            intent_state.load_manual_intent(now_ms=200)
        self.assertEqual(state_error.exception.code, "intent_state_permissions")

        os.chmod(intent_state.intent_path(), 0o600)
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        store = self.store()
        claim = store.claim(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            prepared_artifact_digest="sha256:" + "f" * 64,
            execution_binding_digest="sha256:" + "e" * 64,
            now=300,
        )
        store.finalize(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            claim_id=claim.claim_id,
            terminal_state="ACCEPTED",
            outcome_digest="sha256:" + "9" * 64,
        )
        os.chmod(intent_state.audit_path(), 0o644)
        with self.assertRaises(intent_state.IntentStateError) as audit_error:
            intent_state.load_terminal_audit_records()
        self.assertEqual(audit_error.exception.code, "intent_audit_permissions")

    def test_resolved_review_is_audited_before_the_slot_becomes_replaceable(self) -> None:
        store = self.store()
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        intent_state.confirm_and_issue_intent(
            expected_intent_id="intent-1",
            expected_prepared_artifact_digest="sha256:" + "f" * 64,
            execution_grant=execution_grant(),
            confirmed_at=200,
        )
        store.claim(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            prepared_artifact_digest="sha256:" + "f" * 64,
            execution_binding_digest="sha256:" + "e" * 64,
            now=300,
        )
        store.recover(
            grant_id="grant-1",
            principal_id="principal-id",
            account_binding_id="account-id",
            tool_name="trade_place_order",
            now=400,
        )
        evidence_digest = "sha256:" + "1" * 64
        with self.assertRaises(intent_state.IntentStateError):
            store.record_reconciliation_result(
                "intent-1", status="RESOLVED_NOT_SENT", evidence_digest=evidence_digest,
                observed_at=500,
            )
        self.assertEqual(intent_state.load_terminal_audit_records(), [])
        resolved = store.record_reconciliation_result(
            "intent-1", status="RESOLVED_ACCEPTED", evidence_digest=evidence_digest,
            observed_at=500,
        )
        self.assertEqual(resolved["reconciliation"]["result"], "ACCEPTED")
        self.assertEqual(resolved["reconciliation"]["evidence_digest"], evidence_digest)
        self.assertEqual(len(intent_state.load_terminal_audit_records()), 1)
        self.assertEqual(
            store.record_reconciliation_result(
                "intent-1", status="RESOLVED_ACCEPTED", evidence_digest=evidence_digest,
                observed_at=500,
            ),
            resolved,
        )
        self.assertEqual(len(intent_state.load_terminal_audit_records()), 1)
        with self.assertRaises(intent_state.IntentStateError):
            store.record_reconciliation_result(
                "intent-1", status="RESOLVED_ACCEPTED", evidence_digest="sha256:" + "2" * 64,
                observed_at=600,
            )
        self.assertEqual(
            intent_state.load_manual_intent(now_ms=600)["reconciliation"]["evidence_digest"],
            evidence_digest,
        )
        self.assertEqual(len(intent_state.load_terminal_audit_records()), 1)
        intent_state.save_unclaimed_intent(
            self.build_intent(intent_id="intent-2"),
            now_ms=600,
        )
        self.assertEqual(
            intent_state.load_manual_intent(now_ms=600)["intent_id"],
            "intent-2",
        )

    def test_legacy_released_status_stays_review_required_without_trusted_terminal_evidence(self) -> None:
        self.write_raw_state(
            {
                "intent_id": "legacy-released",
                "status": "RELEASED",
                "mcp_trade_confirmation_started_at": 100,
            }
        )
        first = intent_state.load_manual_intent(now_ms=200)
        self.assertEqual(first["state"], "REVIEW_REQUIRED")
        self.assertEqual(len(intent_state.load_terminal_audit_records()), 0)

        second = intent_state.load_manual_intent(now_ms=300)
        self.assertEqual(second, first)
        self.assertEqual(len(intent_state.load_terminal_audit_records()), 0)

    def test_reconciliation_template_api_accepts_only_intent_id(self) -> None:
        intent_state.save_unclaimed_intent(self.build_intent(), now_ms=100)
        signature = inspect.signature(
            intent_state.reconciliation_template_for_intent
        )
        self.assertEqual(tuple(signature.parameters), ("intent_id",))
        template = intent_state.reconciliation_template_for_intent("intent-1")
        self.assertEqual(template["identity"], {"client_order_id": "client-1"})

    def test_legacy_intent_write_claim_and_clear_apis_are_removed(self) -> None:
        for name in (
            "build_risk_signature",
            "build_intent",
            "save_intent",
            "load_intent",
            "claim_intent_for_confirmation",
            "clear_intent",
            "intent_is_expired",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(intent_state, name))


if __name__ == "__main__":
    unittest.main()
