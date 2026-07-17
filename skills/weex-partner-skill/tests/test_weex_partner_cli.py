#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "weex_partner_cli.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_partner_cli_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("weex_partner_cli", MODULE_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WeexPartnerCliPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.partner = load_partner_cli_module()

    def require_partner(self):
        self.assertIsNotNone(
            self.partner,
            "Partner orchestration CLI is not implemented yet; expected T1 RED.",
        )
        return self.partner

    def test_official_minimum_selector_uses_smallest_documented_range(self) -> None:
        partner = self.require_partner()
        self.assertEqual(partner.choose_official_minimum_days([30, 90, 365]), 30)

    def test_missing_time_uses_endpoint_official_minimum_and_reports_utc(self) -> None:
        partner = self.require_partner()
        now = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)
        request = {
            "operation": "list-referral-uids",
            "profile": "main",
            "scope": {"mode": "all", "all_confirmed": True},
            "time_range": None,
            "filters": {},
            "result_mode": "summary_with_first_20",
        }
        plan = partner.plan_query(request, now=now)

        self.assertEqual(plan["time_range"]["source"], "official_minimum_default")
        self.assertEqual(plan["time_range"]["minimum_days"], 90)
        self.assertEqual(plan["time_range"]["actual_end"], "2026-07-15T00:00:00Z")
        self.assertEqual(plan["time_range"]["actual_start"], "2026-04-16T00:00:00Z")
        self.assertTrue(plan["continuation"]["can_continue"])

    def test_explicit_time_range_takes_priority_over_endpoint_default(self) -> None:
        partner = self.require_partner()
        request = {
            "operation": "get-commission",
            "profile": "main",
            "scope": {"mode": "uids", "uids": ["10001"], "all_confirmed": False},
            "time_range": {
                "start": "2026-07-01T00:00:00Z",
                "end": "2026-07-10T00:00:00Z",
            },
            "filters": {},
            "result_mode": "summary_with_first_20",
        }
        plan = partner.plan_query(request, now=datetime(2026, 7, 15, tzinfo=timezone.utc))

        self.assertEqual(plan["time_range"]["source"], "user")
        self.assertEqual(plan["time_range"]["actual_start"], request["time_range"]["start"])
        self.assertEqual(plan["time_range"]["actual_end"], request["time_range"]["end"])

    def test_missing_time_is_rejected_when_only_maximum_or_unknown_range_is_public(self) -> None:
        partner = self.require_partner()
        request = {
            "operation": "get-sub-agent-stats",
            "profile": "main",
            "scope": {"mode": "uids", "uids": ["20001"], "all_confirmed": False},
            "time_range": None,
            "filters": {"product_type": "FUTURES"},
            "result_mode": "summary_with_first_20",
        }

        with self.assertRaises(partner.PartnerQueryError) as exc_info:
            partner.plan_query(request, now=datetime(2026, 7, 15, tzinfo=timezone.utc))
        self.assertEqual(exc_info.exception.code, "time_range_required")

    def test_internal_withdrawal_history_limit_is_not_used_as_a_default_time(self) -> None:
        partner = self.require_partner()
        request = {
            "operation": "get-internal-withdrawals",
            "profile": "main",
            "scope": {"mode": "none", "all_confirmed": False},
            "time_range": None,
            "filters": {},
            "result_mode": "summary_with_first_20",
        }

        with self.assertRaises(partner.PartnerQueryError) as exc_info:
            partner.plan_query(request, now=datetime(2026, 7, 15, tzinfo=timezone.utc))
        self.assertEqual(exc_info.exception.code, "time_range_required")

    def test_implicit_all_scope_is_rejected_before_any_executor_call(self) -> None:
        partner = self.require_partner()
        request = {
            "operation": "get-referral-deal-data",
            "profile": "main",
            "scope": {"mode": "all", "all_confirmed": False},
            "time_range": {
                "start": "2026-07-01",
                "end": "2026-07-15",
            },
            "filters": {},
            "result_mode": "summary_with_first_20",
        }

        with self.assertRaises(partner.PartnerQueryError) as exc_info:
            partner.plan_query(request, now=datetime(2026, 7, 15, tzinfo=timezone.utc))
        self.assertEqual(exc_info.exception.code, "scope_confirmation_required")

    def test_default_result_displays_first_twenty_and_explains_more_records(self) -> None:
        partner = self.require_partner()
        records = [{"uid": str(index)} for index in range(1, 46)]
        result = partner.build_result_envelope(
            operation="list-referral-uids",
            result_mode="summary_with_first_20",
            records=records,
            pages_fetched=1,
            pages_total=3,
            records_total=145,
            next_page=2,
        )

        self.assertEqual(len(result["records"]), 20)
        self.assertFalse(result["complete"])
        self.assertTrue(result["pagination"]["has_more"])
        self.assertEqual(result["pagination"]["remaining_count"], 125)
        self.assertEqual(result["pagination"]["next_page"], 2)
        self.assertTrue(result["continuation"]["can_continue"])
        self.assertIn("next", result["continuation"]["stop_reason"])

    def test_decimal_aggregation_never_uses_binary_float(self) -> None:
        partner = self.require_partner()
        summary = partner.aggregate_decimal_fields(
            [
                {"coin": "USDT", "commission": "0.1"},
                {"coin": "USDT", "commission": "0.2"},
                {"coin": "BTC", "commission": "0.00000001"},
            ],
            group_field="coin",
            amount_field="commission",
        )

        self.assertEqual(summary["USDT"], Decimal("0.3"))
        self.assertEqual(summary["BTC"], Decimal("0.00000001"))

    def test_rate_limit_after_partial_pages_blocks_complete_summary_and_requires_restart(self) -> None:
        partner = self.require_partner()
        result = partner.build_partial_error_envelope(
            operation="get-direct-trade-asset",
            records=[{"uid": "10001"}, {"uid": "10002"}],
            pages_fetched=2,
            next_page=3,
            records_total=530,
            error={"category": "rate_limit", "http_status": 429},
            offset_pagination=True,
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["partial"])
        self.assertIsNone(result["summary"])
        self.assertTrue(result["continuation"]["restart_required"])
        self.assertEqual(result["continuation"]["restart_from_page"], 1)
        self.assertFalse(result["continuation"]["can_continue"])

    def test_continuation_is_bound_to_profile_scope_operation_filters_and_contract(self) -> None:
        partner = self.require_partner()
        continuation = {
            "resolved_profile_id": "profile-a",
            "query_scope": {"mode": "uids", "uids": ["10001"]},
            "operation": "get-commission",
            "contract_version": "partner-v3-2026-06-22",
            "filters": {"product_type": "SPOT"},
            "result_mode": "summary_with_first_20",
        }
        request = {
            "resolved_profile_id": "profile-b",
            "query_scope": {"mode": "uids", "uids": ["10001"]},
            "operation": "get-commission",
            "contract_version": "partner-v3-2026-06-22",
            "filters": {"product_type": "SPOT"},
            "result_mode": "summary_with_first_20",
        }

        with self.assertRaises(partner.PartnerQueryError) as exc_info:
            partner.validate_continuation(continuation, request)
        self.assertEqual(exc_info.exception.code, "continuation_mismatch")

    def test_unknown_response_fields_keep_names_but_hide_values(self) -> None:
        partner = self.require_partner()
        normalized = partner.project_known_fields(
            {"userId": "10001", "totalUsdt": "12.3", "contractTotalUsdt": "999.9"},
            known_fields={"userId", "totalUsdt"},
        )

        self.assertEqual(normalized["record"]["userId"], "10001")
        self.assertNotIn("contractTotalUsdt", normalized["record"])
        self.assertEqual(normalized["unknown_fields"], ["contractTotalUsdt"])
        self.assertNotIn("999.9", repr(normalized))

    def test_all_eight_operations_map_to_the_exact_trader_allowlist_keys(self) -> None:
        partner = self.require_partner()
        expected = {
            "list-referral-uids": "partner.get-affiliate-uids",
            "get-direct-trade-asset": "partner.get-channel-user-trade-and-asset",
            "get-commission": "partner.get-affiliate-commission",
            "get-internal-withdrawals": "partner.get-internal-withdrawal-status",
            "get-sub-agent-stats": "partner.query-sub-channel-transactions",
            "verify-referrals": "partner.verify-referrals",
            "get-referral-assets": "partner.get-referral-assets",
            "get-referral-deal-data": "partner.get-referral-deal-data",
        }
        self.assertEqual(
            {name: policy.endpoint for name, policy in partner.OPERATION_POLICIES.items()},
            expected,
        )

    def test_commission_missing_time_uses_official_seven_day_minimum(self) -> None:
        partner = self.require_partner()
        now = datetime(2026, 7, 15, 0, 0, 0, tzinfo=timezone.utc)
        plan = partner.plan_query(
            {
                "operation": "get-commission",
                "profile": "main",
                "scope": {"mode": "uids", "uids": ["10001"], "all_confirmed": False},
                "time_range": None,
                "filters": {},
                "result_mode": "summary_with_first_20",
            },
            now=now,
        )

        self.assertEqual(plan["time_range"]["minimum_days"], 7)
        self.assertEqual(plan["time_range"]["actual_start"], "2026-07-08T00:00:00Z")
        self.assertEqual(plan["filters"]["product_type"], "SPOT")
        self.assertFalse(plan["continuation"]["can_continue"])

    def test_verify_referrals_splits_more_than_one_hundred_uids_without_overlap(self) -> None:
        partner = self.require_partner()
        uids = [str(index) for index in range(1, 206)]
        plan = partner.plan_query(
            {
                "operation": "verify-referrals",
                "profile": "main",
                "scope": {"mode": "uids", "uids": uids, "all_confirmed": False},
                "time_range": None,
                "filters": {},
                "result_mode": "complete_list",
            },
            now=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )
        requests = partner.build_executor_requests(plan)

        self.assertEqual(len(requests), 3)
        batches = [item["query"]["userIds"].split(",") for item in requests]
        self.assertEqual([len(batch) for batch in batches], [100, 100, 5])
        self.assertEqual([uid for batch in batches for uid in batch], uids)

    def test_deal_data_repeats_user_ids_in_executor_query(self) -> None:
        partner = self.require_partner()
        plan = partner.plan_query(
            {
                "operation": "get-referral-deal-data",
                "profile": "main",
                "scope": {"mode": "uids", "uids": ["10001", "10002"], "all_confirmed": False},
                "time_range": {"start": "2026-07-01", "end": "2026-07-15"},
                "filters": {},
                "result_mode": "summary_with_first_20",
            },
            now=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )
        request = partner.build_executor_requests(plan)[0]

        self.assertEqual(request["query"]["userIds"], ["10001", "10002"])
        self.assertEqual(request["query"]["startTime"], "2026-07-01")
        self.assertEqual(request["query"]["endTime"], "2026-07-15")

    def test_default_mode_fetches_one_page_and_discloses_the_next_page(self) -> None:
        partner = self.require_partner()
        calls = []

        def executor(request):
            calls.append(request)
            return {
                "ok": True,
                "profile": {"resolved_profile_id": "profile-a", "name": "main"},
                "data": {
                    "code": "00000",
                    "data": {
                        "records": [{"uid": "10001"}, {"uid": "10002"}],
                        "current": 1,
                        "pages": 3,
                        "total": 5,
                    },
                },
            }

        result = partner.execute_query(
            {
                "operation": "list-referral-uids",
                "profile": "main",
                "scope": {"mode": "all", "all_confirmed": True},
                "time_range": None,
                "filters": {},
                "result_mode": "summary_with_first_20",
            },
            executor=executor,
            now=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )

        self.assertEqual(len(calls), 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["pagination"]["pages_total"], 3)
        self.assertEqual(result["pagination"]["records_total"], 5)
        self.assertEqual(result["pagination"]["next_page"], 2)
        self.assertTrue(result["pagination"]["has_more"])

    def test_complete_list_fetches_all_pages_serially(self) -> None:
        partner = self.require_partner()
        requested_pages = []

        def executor(request):
            page = request["query"]["page"]
            requested_pages.append(page)
            page_records = {
                1: [{"uid": "10001"}, {"uid": "10002"}],
                2: [{"uid": "10003"}, {"uid": "10004"}],
                3: [{"uid": "10005"}],
            }[page]
            return {
                "ok": True,
                "profile": {"resolved_profile_id": "profile-a", "name": "main"},
                "data": {
                    "code": "00000",
                    "data": {
                        "records": page_records,
                        "current": page,
                        "pages": 3,
                        "total": 5,
                    },
                },
            }

        result = partner.execute_query(
            {
                "operation": "list-referral-uids",
                "profile": "main",
                "scope": {"mode": "all", "all_confirmed": True},
                "time_range": None,
                "filters": {},
                "result_mode": "complete_list",
            },
            executor=executor,
            now=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )

        self.assertEqual(requested_pages, [1, 2, 3])
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["records"]), 5)
        self.assertFalse(result["pagination"]["has_more"])

    def test_complete_list_rate_limit_on_later_page_requires_page_one_restart(self) -> None:
        partner = self.require_partner()

        def executor(request):
            page = request["query"]["page"]
            if page == 2:
                return {
                    "ok": False,
                    "error": {"category": "rate_limit", "http_status": 429},
                }
            return {
                "ok": True,
                "profile": {"resolved_profile_id": "profile-a", "name": "main"},
                "data": {
                    "code": "00000",
                    "data": {
                        "records": [{"uid": "10001"}, {"uid": "10002"}],
                        "current": 1,
                        "pages": 3,
                        "total": 5,
                    },
                },
            }

        result = partner.execute_query(
            {
                "operation": "list-referral-uids",
                "profile": "main",
                "scope": {"mode": "all", "all_confirmed": True},
                "time_range": None,
                "filters": {},
                "result_mode": "complete_list",
            },
            executor=executor,
            now=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )

        self.assertFalse(result["complete"])
        self.assertTrue(result["partial"])
        self.assertIsNone(result["summary"])
        self.assertTrue(result["continuation"]["restart_required"])
        self.assertEqual(result["continuation"]["restart_from_page"], 1)

    def test_requested_start_older_than_official_history_is_rejected(self) -> None:
        partner = self.require_partner()
        with self.assertRaises(partner.PartnerQueryError) as exc_info:
            partner.plan_query(
                {
                    "operation": "list-referral-uids",
                    "profile": "main",
                    "scope": {"mode": "all", "all_confirmed": True},
                    "time_range": {
                        "start": "2025-01-01T00:00:00Z",
                        "end": "2026-07-15T00:00:00Z",
                    },
                    "filters": {},
                    "result_mode": "summary_with_first_20",
                },
                now=datetime(2026, 7, 15, tzinfo=timezone.utc),
            )
        self.assertEqual(exc_info.exception.code, "time_range_out_of_history")

    def test_total_change_between_pages_blocks_complete_result(self) -> None:
        partner = self.require_partner()

        def executor(request):
            page = request["query"]["page"]
            total = 5 if page == 1 else 6
            return {
                "ok": True,
                "profile": {"resolved_profile_id": "profile-a", "name": "main"},
                "data": {
                    "code": "00000",
                    "data": {
                        "records": [{"uid": str(page)}],
                        "current": page,
                        "pages": 2,
                        "total": total,
                    },
                },
            }

        result = partner.execute_query(
            {
                "operation": "list-referral-uids",
                "profile": "main",
                "scope": {"mode": "all", "all_confirmed": True},
                "time_range": None,
                "filters": {},
                "result_mode": "complete_list",
            },
            executor=executor,
            now=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )

        self.assertFalse(result["complete"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["error"]["category"], "completeness")
        self.assertTrue(result["continuation"]["restart_required"])

    def test_official_response_list_field_names_are_unwrapped(self) -> None:
        partner = self.require_partner()
        fixtures = {
            "channelUserInfoItemList": [{"uid": "10001"}],
            "channelCommissionInfoItems": [{"uid": "10001", "commission": "1"}],
            "items": [{"withdrawId": "w-1"}],
        }
        for field, expected in fixtures.items():
            with self.subTest(field=field):
                records, metadata = partner._unwrap_records(
                    {"data": {field: expected, "pages": 1, "total": 1}}
                )
                self.assertEqual(records, expected)
                self.assertEqual(metadata["total"], 1)

    def test_commission_three_month_limit_uses_calendar_months(self) -> None:
        partner = self.require_partner()
        base = {
            "operation": "get-commission",
            "profile": "main",
            "scope": {"mode": "uids", "uids": ["10001"], "all_confirmed": False},
            "filters": {},
            "result_mode": "summary_with_first_20",
        }
        allowed = dict(base, time_range={
            "start": "2026-04-30T00:00:00Z",
            "end": "2026-07-30T00:00:00Z",
        })
        partner.plan_query(allowed, now=datetime(2026, 7, 30, tzinfo=timezone.utc))

        rejected = dict(base, time_range={
            "start": "2026-04-29T23:59:59Z",
            "end": "2026-07-30T00:00:00Z",
        })
        with self.assertRaises(partner.PartnerQueryError) as exc_info:
            partner.plan_query(rejected, now=datetime(2026, 7, 30, tzinfo=timezone.utc))
        self.assertEqual(exc_info.exception.code, "time_range_too_large")


if __name__ == "__main__":
    unittest.main()
