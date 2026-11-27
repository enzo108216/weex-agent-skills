#!/usr/bin/env python3
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


if __package__:
    from . import test_weex_order_intent_state as intent_fixtures
else:
    import test_weex_order_intent_state as intent_fixtures


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def matrix_cases() -> list[dict[str, object]]:
    pins = {
        "old": ("0.3.2", "a" * 40, "1" * 64, "sha256:" + "b" * 64),
        "candidate": ("0.3.3", "c" * 40, "2" * 64, "sha256:" + "d" * 64),
    }
    cases = []
    for skill in pins:
        for wheel in pins:
            expected_version, commit, _, mapping_digest = pins[skill]
            version, _, wheel_digest, _ = pins[wheel]
            match = skill == wheel
            for trade_enabled in (False, True):
                profile_index = (0 if skill == "old" else 2) + int(trade_enabled)
                cases.append({
                    "skill": skill,
                    "wheel": wheel,
                    "trade_enabled": trade_enabled,
                    "skill_commit": commit,
                    "wheel_sha256": wheel_digest,
                    "package_version": version,
                    "expected_package_version": expected_version,
                    "manifest_digest": "sha256:" + "1234"[profile_index] * 64 if match else None,
                    "boundary_static_manifest_digest": "sha256:" + "5678"[profile_index] * 64 if match else None,
                    "mapping_digest": mapping_digest,
                    "inventory_digest": "sha256:" + "5" * 64,
                    "matrix_digest": "sha256:" + "6" * 64,
                    "process_exit": 0,
                    "result": "ready" if match else "contract_mismatch",
                    "package_ok": match,
                    "manifest_ok": match,
                    "private_read_ready": False,
                    "mutation_ready": False,
                    "action_required": None if match else "install_verified_mcp_candidate",
                })
    return cases


class MatrixDriverTests(unittest.TestCase):
    def validate(self, cases: list[dict[str, object]]) -> bool:
        self.assertTrue((SCRIPTS / "verify_mcp_contract_matrix.py").is_file(), "matrix driver is missing")
        driver = importlib.import_module("verify_mcp_contract_matrix")
        self.assertTrue(callable(getattr(driver, "_validate_matrix_cases", None)), "matrix evidence validator is missing")
        return driver._validate_matrix_cases(cases)

    def test_matrix_requires_each_pair_and_profile_exactly_once(self) -> None:
        cases = matrix_cases()
        self.assertTrue(self.validate(cases))
        self.assertFalse(self.validate(cases[:-1]))
        self.assertFalse(self.validate([*cases[:-1], cases[0]]))

    def test_crossed_readiness_and_unverified_failures_cannot_pass(self) -> None:
        for override in (
            {"result": "ready", "package_ok": True, "manifest_ok": True, "action_required": None},
            {"result": "failed", "action_required": "repair_mcp_runtime"},
            {"process_exit": 9},
            {"private_read_ready": True},
            {"mutation_ready": True},
            {"manifest_digest": "sha256:" + "7" * 64},
            {"package_version": "0.3.2"},
        ):
            with self.subTest(override=override):
                cases = matrix_cases()
                crossed = next(case for case in cases if case["skill"] == "old" and case["wheel"] == "candidate")
                crossed.update(override)
                self.assertFalse(self.validate(cases))

    def test_matching_release_requires_verified_manifest_and_process_success(self) -> None:
        for override in ({"manifest_digest": None}, {"manifest_ok": False}, {"process_exit": 1}):
            with self.subTest(override=override):
                cases = matrix_cases()
                cases[0].update(override)
                self.assertFalse(self.validate(cases))

    def test_revision_and_wheel_evidence_cannot_drift_between_profiles(self) -> None:
        for override in (
            {"skill_commit": "e" * 40},
            {"wheel_sha256": "0" * 64},
            {"mapping_digest": "sha256:" + "e" * 64},
            {"inventory_digest": "sha256:" + "e" * 64},
            {"matrix_digest": "sha256:" + "e" * 64},
        ):
            with self.subTest(override=override):
                cases = matrix_cases()
                cases[0].update(override)
                self.assertFalse(self.validate(cases))


class HistoricalIntentMatrixTests(unittest.TestCase):
    old_versions = {
        "package": "0.3.2", "tool_contract": "2", "prepare": "1", "intent": "2", "grant": "2",
        "boundary_contract_digest": "sha256:a08c5997ab1ef4698ad1bd37ff89378fedf340600dde668d0f9fc5fc256b7b10",
    }

    def _seed_intent(self, *, state, versions, fixture_id):
        intent_state = intent_fixtures.intent_state
        grant = intent_fixtures.execution_grant()
        binding = {
            field: getattr(grant, field)
            for field in ("grant_id", "principal_id", "account_binding_id", "tool_name")
        }
        store = intent_state.FileManualIntentStore(
            runtime_loader=intent_fixtures.fake_runtime_models, clock=lambda: 400,
        )
        with mock.patch.object(intent_state, "_INTENT_CONTRACT_VERSIONS", versions):
            intent = intent_state.build_unclaimed_intent(
                prepared_mutation=intent_fixtures.prepared_mutation(),
                profile_binding=intent_fixtures.profile_binding(),
                contract_versions=versions, intent_id=fixture_id, created_at=100,
            )
            intent_state.save_unclaimed_intent(intent, now_ms=100)
            if state != "UNCLAIMED":
                intent_state.confirm_and_issue_intent(
                    expected_intent_id=fixture_id,
                    expected_prepared_artifact_digest=grant.prepared_artifact_digest,
                    execution_grant=grant, confirmed_at=200,
                )
            if state not in {"UNCLAIMED", "CONFIRMED"}:
                claim = store.claim(
                    **binding, prepared_artifact_digest=grant.prepared_artifact_digest,
                    execution_binding_digest=grant.execution_binding_digest, now=300,
                )
                if state == "REVIEW_REQUIRED":
                    store.recover(**binding, now=350)
                elif state != "CLAIMED":
                    store.finalize(
                        **binding, claim_id=claim.claim_id, terminal_state=state,
                        outcome_digest=(
                            intent_state.NOT_SENT_TERMINAL_OUTCOME_DIGEST
                            if state == "NOT_SENT_TERMINAL" else "sha256:" + "9" * 64
                        ),
                    )
            original = intent_state.load_manual_intent(now_ms=450)
            self.assertEqual(original["state"], state)
        return original, store, binding

    def test_fixed_old_pin_intents_never_replay_and_keep_outstanding_evidence(self) -> None:
        intent_state = intent_fixtures.intent_state
        for source, expected in (
            ("UNCLAIMED", "LEGACY_INVALIDATED"),
            ("CONFIRMED", "NOT_SENT_TERMINAL"),
            ("CLAIMED", "CLAIMED"),
            ("REVIEW_REQUIRED", "REVIEW_REQUIRED"),
            ("ACCEPTED", "ACCEPTED"),
            ("DEFINITIVE_REJECTION", "DEFINITIVE_REJECTION"),
            ("NOT_SENT_TERMINAL", "NOT_SENT_TERMINAL"),
        ):
            with (
                self.subTest(source=source), tempfile.TemporaryDirectory() as directory,
                mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": directory}),
            ):
                fixture_id = f"old-{source.lower()}"
                original, store, binding = self._seed_intent(
                    state=source, versions=self.old_versions, fixture_id=fixture_id,
                )
                audit_path = intent_state.audit_path()
                past_audit = audit_path.read_bytes() if audit_path.is_file() else None
                restored = intent_state.load_manual_intent(now_ms=500)
                self.assertEqual(restored["state"], expected)
                with self.assertRaises(intent_state.IntentStateError):
                    intent_state.execution_grant_for_dispatch(
                        fixture_id, runtime_loader=intent_fixtures.fake_runtime_models, now_ms=500,
                    )
                with self.assertRaises(intent_state.IntentStateError):
                    store.load(**binding)

                if source == "CLAIMED":
                    claim = intent_state.claimed_execution_grant_for_recovery(
                        fixture_id, runtime_loader=intent_fixtures.fake_runtime_models, now_ms=550,
                    )
                    self.assertEqual(claim.state, "CLAIMED")
                    store.recover(**binding, now=600)
                    restored = intent_state.load_manual_intent(now_ms=620)
                if source in {"CLAIMED", "REVIEW_REQUIRED"}:
                    self.assertEqual(restored["state"], "REVIEW_REQUIRED")
                    self.assertEqual(restored["prepared_mutation"], original["prepared_mutation"])
                    with self.assertRaises(intent_state.IntentStateError):
                        intent_state.ensure_manual_intent_slot_available(now_ms=650)
                else:
                    intent_state.ensure_manual_intent_slot_available(now_ms=650)
                    records = intent_state.load_terminal_audit_records()
                    self.assertEqual([record["intent"]["state"] for record in records], [expected])
                    if past_audit is not None:
                        self.assertEqual(audit_path.read_bytes(), past_audit)

    def test_version_rollback_refuses_new_claimed_intent_without_discarding_it(self) -> None:
        intent_state = intent_fixtures.intent_state
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": directory}),
        ):
            current, _, binding = self._seed_intent(
                state="CLAIMED", versions=intent_fixtures.contract_versions(), fixture_id="candidate-claim",
            )
            persisted = intent_state.intent_path().read_bytes()
            with mock.patch.object(intent_state, "_INTENT_CONTRACT_VERSIONS", self.old_versions):
                with self.assertRaises(intent_state.IntentStateError):
                    intent_state.load_manual_intent(now_ms=500)
            self.assertEqual(intent_state.intent_path().read_bytes(), persisted)
            self.assertEqual(intent_state.load_manual_intent(now_ms=500), current)
            with self.assertRaises(intent_state.IntentStateError):
                intent_state.FileManualIntentStore(
                    runtime_loader=intent_fixtures.fake_runtime_models, clock=lambda: 500,
                ).load(**binding)


if __name__ == "__main__":
    unittest.main()
