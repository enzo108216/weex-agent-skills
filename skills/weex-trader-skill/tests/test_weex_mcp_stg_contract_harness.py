#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

PROFILE = "offline-profile-fixture"
OWNER = "normal"
SPOT = "https://stg-spotpro-openapi.weex.tech"
FUTURES = "https://stg-api-contract.weex.tech"
FROZEN_ACTIONS = {
    "trade_place_order", "trade_cancel_order", "trade_update_leverage", "trade_set_margin_mode",
    "trade_place_batch_orders", "trade_cancel_batch_orders", "trade_cancel_open_orders",
    "trade_place_conditional_order", "trade_cancel_conditional_order", "trade_place_tp_sl",
    "trade_modify_tp_sl", "trade_close_positions", "trade_adjust_position_margin", "trade_set_auto_append_margin",
}
BASELINE_DIGEST = "sha256:" + "a" * 64
BUDGETS = {"seed": 1, "test": 1, "cleanup": 2}
RECEIPT_IDS = {"offline-seed-fixture": "41", "offline-target-fixture": "42"}


def valid_cases() -> list[dict]:
    cases = []
    for case_id, phase, identity in (
        ("offline-seed-fixture", "seed", "offline-seed-identity"),
        ("offline-target-fixture", "test", "offline-target-identity"),
    ):
        cases.append({
            "case_id": case_id, "owner": OWNER, "profile": PROFILE,
            "phase": phase, "tool_name": "trade_place_order", "market": "spot",
            "expected_identity": {"client_order_id": identity}, "expected_state": {"status": "OPEN"},
        })
    for original in cases[:]:
        cases.append({
            **original,
            "case_id": "cleanup-" + original["case_id"], "phase": "cleanup",
            "tool_name": "trade_cancel_order", "cleanup_of": original["case_id"],
            "expected_state": {"status": "CANCELED"},
        })
    return cases


class OfflineStgGateTests(unittest.TestCase):
    def harness(self):
        self.assertTrue((SCRIPTS / "weex_mcp_stg_contract_harness.py").is_file(), "offline STG harness is missing")
        return importlib.import_module("weex_mcp_stg_contract_harness")

    def exercise(
        self, *, cases=None, profile=PROFILE, owner=OWNER, spot=SPOT, futures=FUTURES,
        budgets=None, outcome_class="accepted", first_read_override=None,
        initial_baseline_override=None, final_baseline_override=None,
        fail_dispatch=False, fault_case=None, scoped_noop=None, cleanup_read_override=None,
    ):
        trace = []
        reads = 0
        baseline_checks = 0

        def dispatch(case):
            trace.append(("dispatch", case["case_id"]))
            fault_here = fault_case is None or fault_case == case["case_id"]
            if fail_dispatch and fault_here:
                raise RuntimeError("unlogged-fixture-detail")
            actual_outcome = outcome_class if fault_here else "accepted"
            write_attempted, disposition = {
                "accepted": (True, "accepted"), "unknown": (True, "review_required"),
                "partial": (True, "review_required"), "definitive_rejection": (True, "definitive_rejection"),
                "not_sent": (False, "not_sent_terminal"),
                "invocation_rejected": (False, "unchanged"),
            }[actual_outcome]
            body = {
                "ok": actual_outcome == "accepted", "action": case["tool_name"].removeprefix("trade_"),
                "outcome": {
                    "class": actual_outcome, "write_attempted": write_attempted, "grant_disposition": disposition,
                },
            }
            if actual_outcome == "accepted":
                body["result"] = {"status": "accepted", "request_identity": deepcopy(case["expected_identity"])}
                if "market" in case and case["tool_name"] in {"trade_place_order", "trade_cancel_order"}:
                    body["result"]["market"] = case["market"]
                if case["tool_name"] in {"trade_update_leverage", "trade_set_margin_mode"}:
                    body["result"].pop("request_identity")
                    body["result"].update(
                        symbol=case["expected_identity"]["symbol"], target=deepcopy(case["expected_state"]),
                    )
                if case["case_id"] in RECEIPT_IDS:
                    body["result"]["upstream_order_id"] = RECEIPT_IDS[case["case_id"]]
                if case["tool_name"] == "trade_place_tp_sl":
                    body["result"].update(
                        items=[{
                            "index": 0, "request_identity": deepcopy(case["expected_identity"]),
                            "status": "accepted", "outcome": {"class": "accepted", "write_attempted": True},
                            "upstream_order_id": RECEIPT_IDS[case["case_id"]],
                        }],
                        counts={"accepted": 1, "rejected": 0, "unknown": 0},
                    )
                if scoped_noop is not None:
                    body["result"].update(case["expected_identity"])
                    body["result"].update(status="accepted_noop", noop=True, affected_count=scoped_noop)
            else:
                body["error"] = {"code": "unlogged-fixture-detail", "retryable": False}
                if actual_outcome in {"partial", "unknown"}:
                    body["reconciliation"] = {"strategy": "manual_review", "completeness": "unprovable"}
            return {"structured_content": body, "is_error": not body["ok"], "transport_meta": None}

        def read_back(case, _result):
            nonlocal reads
            reads += 1
            trace.append(("read", case["case_id"]))
            evidence = {"complete": True, "identity": deepcopy(case["expected_identity"]), "matches": True}
            if "market" in case and "expected_state" in case:
                evidence.update(market=case["market"], state=deepcopy(case["expected_state"]))
            if case["case_id"] in RECEIPT_IDS:
                evidence["upstream_order_id"] = RECEIPT_IDS[case["case_id"]]
            if case["phase"] == "cleanup" and case["cleanup_of"] in RECEIPT_IDS:
                evidence["upstream_order_id"] = RECEIPT_IDS[case["cleanup_of"]]
                evidence.update(cleanup_read_override or {})
            return {**evidence, **(first_read_override or {})} if reads == 1 else evidence

        def baseline():
            nonlocal baseline_checks
            baseline_checks += 1
            trace.append(("baseline", baseline_checks))
            evidence = {"complete": True, "clean": True, "snapshot_digest": BASELINE_DIGEST}
            override = initial_baseline_override if baseline_checks == 1 else final_baseline_override
            return {**evidence, **(override or {})}

        def reconcile(case, _result):
            trace.append(("reconcile", case["case_id"]))
            return {"status": "RESOLVED_ACCEPTED", "evidence_digest": "sha256:" + "b" * 64}

        result = self.harness().run_offline_gate(
            profile=profile, owner=owner, spot_base_url=spot, futures_base_url=futures,
            cases=valid_cases() if cases is None else cases, budgets=dict(BUDGETS) if budgets is None else budgets,
            dispatch=dispatch, read_back=read_back, baseline=baseline, reconcile=reconcile,
        )
        return result, trace

    def assertNoDispatch(self, result, trace):
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(any(kind == "dispatch" for kind, _ in trace), trace)
        self.assertEqual(result["dispatch_counts"], {"seed": 0, "test": 0, "cleanup": 0})

    def test_client_alias_cleanup_must_read_back_the_prior_exchange_order_id(self) -> None:
        for wrong in (None, "999"):
            with self.subTest(exchange_order_id=wrong):
                result, trace = self.exercise(cleanup_read_override={"upstream_order_id": wrong})
                self.assertEqual(result["status"], "review_required")
                self.assertEqual(result["stop_reason"], "read_back_unproved")
                self.assertEqual(result["dispatch_counts"]["cleanup"], 1)
                self.assertNotIn(("dispatch", "cleanup-offline-target-fixture"), trace)

    def test_frozen_action_catalog_and_exact_stg_hosts(self) -> None:
        harness = self.harness()
        self.assertEqual(len(harness.DIRECT_ACTIONS), 14)
        self.assertEqual(set(harness.DIRECT_ACTIONS), FROZEN_ACTIONS)
        self.assertEqual((harness.STG_SPOT_ORIGIN, harness.STG_FUTURES_ORIGIN), (SPOT, FUTURES))

    def test_wrong_owner_profile_routes_and_budgets_cannot_dispatch(self) -> None:
        wrong_case_owner = valid_cases()
        wrong_case_owner[0]["owner"] = "master"
        wrong_role = [{**case, "owner": "master"} for case in valid_cases()]
        wrong_tool = valid_cases()
        wrong_tool[0]["tool_name"] = "trade_unfrozen"
        for options in (
            {"cases": wrong_case_owner}, {"owner": "master", "cases": wrong_role},
            {"profile": ""}, {"profile": "different-profile"},
            {"spot": "https://api-spot.weex.com"}, {"futures": "https://api-contract.weex.com"},
            {"spot": FUTURES, "futures": SPOT}, {"spot": SPOT + "/path"},
            {"budgets": {**BUDGETS, "cleanup": 1}}, {"budgets": {**BUDGETS, "seed": True}},
            {"budgets": {**BUDGETS, "test": -1}}, {"cases": wrong_tool},
            {"initial_baseline_override": {"complete": False}}, {"initial_baseline_override": {"clean": False}},
        ):
            with self.subTest(options=options):
                self.assertNoDispatch(*self.exercise(**options))

    def test_own_cleanup_needs_valid_earlier_link_and_exact_identity_and_no_replays(self) -> None:
        for change in ("duplicate_case", "unknown_parent", "mismatched_identity", "cleanup_parent"):
            with self.subTest(change=change):
                cases = valid_cases()
                if change == "duplicate_case":
                    cases[1]["case_id"] = cases[0]["case_id"]
                elif change == "unknown_parent":
                    cases[2]["cleanup_of"] = "unplanned-order"
                elif change == "mismatched_identity":
                    cases[2]["expected_identity"] = cases[1]["expected_identity"]
                else:
                    cases[3]["cleanup_of"] = cases[2]["case_id"]
                self.assertNoDispatch(*self.exercise(cases=cases))

    def test_same_numeric_id_does_not_authorize_cleanup_across_order_classes(self) -> None:
        seed, _, cleanup, _ = valid_cases()
        normal = {
            **seed, "tool_name": "trade_cancel_order", "market": "futures",
            "expected_identity": {"order_id": "41"}, "expected_state": {"status": "CANCELED"},
        }
        for tool in ("trade_cancel_conditional_order", "trade_modify_tp_sl"):
            with self.subTest(tool=tool):
                wrong_cleanup = {
                    **cleanup, "tool_name": tool, "market": "futures", "expected_identity": {"order_id": "41"},
                    "expected_state": {"trigger_price": "1"} if tool == "trade_modify_tp_sl" else {"status": "CANCELED"},
                }
                self.assertNoDispatch(*self.exercise(
                    cases=[normal, wrong_cleanup], budgets={"seed": 1, "test": 0, "cleanup": 1},
                ))

    def test_conditional_cleanup_uses_its_own_prior_placement_id_seen_in_read_back(self) -> None:
        seed, _, cleanup, _ = valid_cases()
        budget = {"seed": 1, "test": 0, "cleanup": 1}
        for placement in ("trade_place_conditional_order", "trade_place_tp_sl"):
            with self.subTest(placement=placement):
                original = {
                    **seed, "tool_name": placement, "market": "futures",
                    "expected_identity": {"client_algo_id": "offline-plan-identity"},
                }
                exact_cleanup = {
                    **cleanup, "tool_name": "trade_cancel_conditional_order", "market": "futures",
                    "expected_identity": {"order_id": "41"},
                }
                result, trace = self.exercise(cases=[original, exact_cleanup], budgets=budget)
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["baseline_proven"])
                self.assertEqual(trace, [
                    ("baseline", 1), ("dispatch", original["case_id"]), ("read", original["case_id"]),
                    ("dispatch", exact_cleanup["case_id"]), ("read", exact_cleanup["case_id"]), ("baseline", 2),
                ])
                for cleanup_id, read_override in (("99", None), ("41", {"upstream_order_id": "99"})):
                    with self.subTest(cleanup_id=cleanup_id, read_override=read_override):
                        different_cleanup = {**exact_cleanup, "expected_identity": {"order_id": cleanup_id}}
                        result, trace = self.exercise(
                            cases=[original, different_cleanup], budgets=budget, first_read_override=read_override,
                        )
                        self.assertFalse(result["ok"])
                        self.assertEqual(result["status"], "review_required")
                        self.assertFalse(result["baseline_proven"])
                        self.assertEqual(result["dispatch_counts"], {"seed": 1, "test": 0, "cleanup": 0})
                        self.assertEqual([case_id for kind, case_id in trace if kind == "dispatch"], [original["case_id"]])

    def test_observed_cancellation_requires_the_frozen_market_and_terminal_state(self) -> None:
        case = {
            "case_id": "offline-cancellation-proof", "owner": OWNER, "profile": PROFILE,
            "phase": "test", "tool_name": "trade_cancel_order", "market": "spot",
            "expected_identity": {"order_id": "41"}, "expected_state": {"status": "CANCELED"},
        }
        budget = {"seed": 0, "test": 1, "cleanup": 0}
        for override, ready in (
            ({}, True), ({"state": {"status": "OPEN"}}, False), ({"market": "futures"}, False),
        ):
            with self.subTest(override=override):
                result, trace = self.exercise(cases=[case], budgets=budget, first_read_override=override)
                self.assertEqual(result["status"], "ready" if ready else "review_required")
                self.assertEqual(result["baseline_proven"], ready)
                self.assertEqual(result["dispatch_counts"], budget)
                self.assertIn(("read", case["case_id"]), trace)

    def test_observed_config_must_match_the_exact_target_even_when_identity_matches(self) -> None:
        case = {
            "case_id": "offline-config-proof", "owner": OWNER, "profile": PROFILE,
            "phase": "test", "tool_name": "trade_update_leverage", "market": "futures",
            "expected_identity": {"symbol": "BTCUSDT"}, "expected_state": {"cross_leverage": "4"},
        }
        budget = {"seed": 0, "test": 1, "cleanup": 0}
        for override, ready in (({}, True), ({"state": {"cross_leverage": "5"}}, False)):
            with self.subTest(override=override):
                result, trace = self.exercise(cases=[case], budgets=budget, first_read_override=override)
                self.assertEqual(result["status"], "ready" if ready else "review_required")
                self.assertEqual(result["baseline_proven"], ready)
                self.assertEqual(result["dispatch_counts"], budget)
                self.assertIn(("read", case["case_id"]), trace)

    def test_ready_requires_exact_read_back_own_cleanup_and_recovered_baseline(self) -> None:
        result, trace = self.exercise()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["manifest_complete"])
        self.assertTrue(result["baseline_proven"])
        self.assertEqual(result["dispatch_counts"], BUDGETS)
        expected = [("baseline", 1)]
        for case in valid_cases():
            expected.extend([("dispatch", case["case_id"]), ("read", case["case_id"])])
        expected.append(("baseline", 2))
        self.assertEqual(trace, expected)
        serialized = json.dumps(result)
        for private in (PROFILE, "offline-seed-identity", "offline-target-fixture"):
            self.assertNotIn(private, serialized)

    def test_unknown_partial_rejection_and_unverified_read_are_sticky(self) -> None:
        for options in (
            {"outcome_class": "unknown"}, {"outcome_class": "partial"},
            {"outcome_class": "definitive_rejection"}, {"outcome_class": "not_sent"},
            {"outcome_class": "invocation_rejected"}, {"fail_dispatch": True},
            {"first_read_override": {"complete": False}}, {"first_read_override": {"matches": False}},
            {"first_read_override": {"identity": {"client_order_id": "other-order"}}},
            {"first_read_override": {"upstream_order_id": "99"}},
        ):
            with self.subTest(options=options):
                result, trace = self.exercise(**options)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "review_required")
                self.assertFalse(result["baseline_proven"])
                self.assertEqual(result["dispatch_counts"], {"seed": 1, "test": 0, "cleanup": 0})
                self.assertEqual([case_id for kind, case_id in trace if kind == "dispatch"], ["offline-seed-fixture"])
                if options.get("outcome_class") in {"unknown", "partial"}:
                    self.assertIn(("reconcile", "offline-seed-fixture"), trace)
                self.assertNotIn("unlogged-fixture-detail", json.dumps(result))

    def test_uncertain_cleanup_never_retries_or_runs_a_later_cleanup(self) -> None:
        failed_case = "cleanup-offline-seed-fixture"
        result, trace = self.exercise(outcome_class="unknown", fault_case=failed_case)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "review_required")
        self.assertFalse(result["baseline_proven"])
        self.assertEqual(result["dispatch_counts"], {"seed": 1, "test": 1, "cleanup": 1})
        self.assertEqual([case_id for kind, case_id in trace if kind == "dispatch"], [
            "offline-seed-fixture", "offline-target-fixture", failed_case,
        ])
        self.assertIn(("reconcile", failed_case), trace)

    def test_cleanup_acknowledgment_without_a_complete_matching_baseline_is_not_clean(self) -> None:
        for override in ({"clean": False}, {"complete": False}, {"snapshot_digest": "sha256:" + "c" * 64}):
            with self.subTest(override=override):
                result, trace = self.exercise(final_baseline_override=override)
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "review_required")
                self.assertFalse(result["baseline_proven"])
                self.assertEqual(result["dispatch_counts"], BUDGETS)
                self.assertEqual(trace[-1], ("baseline", 2))

    def test_scoped_noop_requires_zero_affected_count_and_explicit_empty_read_proof(self) -> None:
        scope_case = {
            "case_id": "offline-scoped-fixture", "owner": OWNER, "profile": PROFILE,
            "phase": "test", "tool_name": "trade_cancel_open_orders", "market": "spot", "allow_scoped_noop": True,
            "expected_identity": {"scope": "symbol", "symbol": "BTCUSDT", "order_kind": "normal"},
            "expected_state": {"open_count": 0},
        }
        budget = {"seed": 0, "test": 1, "cleanup": 0}
        for count, empty, ready in ((0, True, True), (0, False, False), (1, True, False), (False, True, False)):
            with self.subTest(count=count, empty=empty):
                result, trace = self.exercise(
                    cases=[scope_case], budgets=budget, scoped_noop=count, first_read_override={"scope_empty": empty},
                )
                self.assertEqual(result["ok"], ready)
                self.assertEqual(result["baseline_proven"], ready)
                self.assertEqual(result["dispatch_counts"], budget)
                self.assertEqual(sum(kind == "dispatch" for kind, _ in trace), 1)
        all_case = {**scope_case, "expected_identity": {"scope": "all", "order_kind": "normal"}}
        self.assertNoDispatch(*self.exercise(cases=[all_case], budgets=budget, scoped_noop=0))


if __name__ == "__main__":
    unittest.main()
