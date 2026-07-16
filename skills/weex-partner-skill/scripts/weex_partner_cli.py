#!/usr/bin/env python3
"""Deterministic orchestration for the eight WEEX Partner query operations."""

from __future__ import annotations

import argparse
import calendar
import copy
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


CONTRACT_VERSION = "partner-v3-2026-06-22"
RESULT_MODES = {"summary_with_first_20", "complete_list", "aggregate_all"}
PRODUCT_TYPES = {"SPOT", "FUTURES"}


class PartnerQueryError(ValueError):
    """A local query error that must be resolved before any REST call."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class OperationPolicy:
    endpoint: str
    time_format: str = "none"
    default_days: Optional[int] = None
    max_days: Optional[int] = None
    history_days: Optional[int] = None
    max_months: Optional[int] = None
    history_months: Optional[int] = None
    time_required: bool = False
    scope_rule: str = "none"
    product_type: str = "none"
    page_field: Optional[str] = None
    page_size_field: Optional[str] = None


OPERATION_POLICIES: Dict[str, OperationPolicy] = {
    "list-referral-uids": OperationPolicy(
        endpoint="partner.get-affiliate-uids",
        time_format="milliseconds",
        default_days=90,
        max_days=90,
        history_days=365,
        scope_rule="optional_single_or_confirmed_all",
        page_field="page",
        page_size_field="pageSize",
    ),
    "get-direct-trade-asset": OperationPolicy(
        endpoint="partner.get-channel-user-trade-and-asset",
        time_format="milliseconds",
        default_days=90,
        max_days=90,
        history_days=365,
        scope_rule="optional_single_or_confirmed_all",
        page_field="page",
        page_size_field="pageSize",
    ),
    "get-commission": OperationPolicy(
        endpoint="partner.get-affiliate-commission",
        time_format="milliseconds",
        default_days=7,
        max_months=3,
        scope_rule="optional_single_or_confirmed_all",
        product_type="default_spot",
        page_field="page",
        page_size_field="pageSize",
    ),
    "get-internal-withdrawals": OperationPolicy(
        endpoint="partner.get-internal-withdrawal-status",
        time_format="milliseconds",
        time_required=True,
        max_months=1,
        history_months=1,
        page_field="page",
        page_size_field="pageSize",
    ),
    "get-sub-agent-stats": OperationPolicy(
        endpoint="partner.query-sub-channel-transactions",
        time_format="milliseconds",
        time_required=True,
        scope_rule="optional_single_or_confirmed_all",
        product_type="required",
        page_field="pageNum",
        page_size_field="pageSize",
    ),
    "verify-referrals": OperationPolicy(
        endpoint="partner.verify-referrals",
        scope_rule="one_or_more",
    ),
    "get-referral-assets": OperationPolicy(
        endpoint="partner.get-referral-assets",
        time_format="date",
        time_required=True,
        scope_rule="exactly_one",
    ),
    "get-referral-deal-data": OperationPolicy(
        endpoint="partner.get-referral-deal-data",
        time_format="date",
        time_required=True,
        scope_rule="one_or_more_or_confirmed_all",
    ),
}


KNOWN_FIELDS: Dict[str, set[str]] = {
    "list-referral-uids": {
        "uid", "registerTime", "kycResult", "inviteCode", "firstDeposit",
        "firstTrade", "lastDeposit", "lastTrade",
    },
    "get-direct-trade-asset": {
        "uid", "depositAmount", "withdrawalAmount", "spotTradingAmount",
        "futuresTradingAmount", "commission",
    },
    "get-commission": {
        "uid", "date", "coin", "fee", "commission", "rate", "productType",
        "symbol", "sourceType", "takerAmount", "makerAmount",
    },
    "get-internal-withdrawals": {
        "fromUserId", "toUserId", "withdrawId", "coin", "status", "amount",
        "createTime", "updateTime",
    },
    "get-sub-agent-stats": {
        "subAffiliateUid", "productType", "date", "tradingVolume",
        "netTradingFee", "paidCommission",
    },
    "verify-referrals": {"uid", "isRefferal", "is_referral"},
    "get-referral-assets": {
        "availableBalance", "fundingTotalUsdt", "spotProTotalUsdt",
        "unimarginTotalUsdt", "depositTotalAmount", "depositList",
    },
    "get-referral-deal-data": {
        "userId", "spotDealAmountUsdt", "futuresProDealAmountUsdt",
        "spotProDealAmountUsdtTemp",
    },
}


def choose_official_minimum_days(ranges: Iterable[int]) -> int:
    normalized = [int(value) for value in ranges if int(value) > 0]
    if not normalized:
        raise PartnerQueryError(
            "time_range_required",
            "The official contract does not expose a minimum query range.",
        )
    return min(normalized)


def _as_utc_datetime(value: str) -> datetime:
    raw = str(value).strip()
    if not raw:
        raise PartnerQueryError("invalid_time_range", "Time values cannot be empty.")
    if len(raw) == 10:
        parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PartnerQueryError("invalid_time_range", f"Invalid ISO-8601 time: {raw}") from exc
        if parsed.tzinfo is None:
            raise PartnerQueryError("invalid_time_range", "Time values must include a UTC offset.")
        parsed = parsed.astimezone(timezone.utc)
    return parsed


def _format_utc(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _subtract_calendar_months(value: datetime, months: int) -> datetime:
    absolute_month = value.year * 12 + (value.month - 1) - months
    year, month_index = divmod(absolute_month, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError as exc:
        raise PartnerQueryError("invalid_time_range", "Date values must use YYYY-MM-DD.") from exc


def _normalize_scope(request: Mapping[str, Any], policy: OperationPolicy) -> Dict[str, Any]:
    raw_scope = request.get("scope") or {}
    if not isinstance(raw_scope, Mapping):
        raise PartnerQueryError("invalid_scope", "scope must be an object.")
    mode = str(raw_scope.get("mode") or "none")
    uids = [str(value).strip() for value in raw_scope.get("uids", []) if str(value).strip()]
    if len(set(uids)) != len(uids):
        raise PartnerQueryError("invalid_scope", "UID values must not be duplicated.")
    all_confirmed = bool(raw_scope.get("all_confirmed", False))

    if mode == "all" and not all_confirmed:
        raise PartnerQueryError(
            "scope_confirmation_required",
            "An all-referrals query requires explicit all_confirmed=true.",
        )
    if mode == "all" and policy.scope_rule not in {
        "optional_single_or_confirmed_all",
        "one_or_more_or_confirmed_all",
    }:
        raise PartnerQueryError("invalid_scope", "This operation does not support an all-referrals scope.")
    if policy.scope_rule == "optional_single_or_confirmed_all":
        if mode == "all":
            return {"mode": "all", "uids": [], "all_confirmed": True}
        if mode != "uids" or not uids:
            raise PartnerQueryError(
                "scope_confirmation_required",
                "Provide a UID or explicitly confirm the all-referrals scope.",
            )
        if len(uids) != 1:
            raise PartnerQueryError("invalid_scope", "This operation accepts one UID per query.")
    elif policy.scope_rule == "exactly_one" and (mode != "uids" or len(uids) != 1):
        raise PartnerQueryError("invalid_scope", "This operation requires exactly one UID.")
    elif policy.scope_rule == "one_or_more" and (mode != "uids" or not uids):
        raise PartnerQueryError("invalid_scope", "This operation requires at least one UID.")
    elif policy.scope_rule == "one_or_more_or_confirmed_all":
        if mode != "all" and (mode != "uids" or not uids):
            raise PartnerQueryError(
                "scope_confirmation_required",
                "Provide UID values or explicitly confirm the all-referrals scope.",
            )
    elif policy.scope_rule == "none":
        return {"mode": "none", "uids": [], "all_confirmed": False}
    return {"mode": mode, "uids": uids, "all_confirmed": all_confirmed}


def _normalize_filters(request: Mapping[str, Any], policy: OperationPolicy) -> Dict[str, Any]:
    raw_filters = request.get("filters") or {}
    if not isinstance(raw_filters, Mapping):
        raise PartnerQueryError("invalid_filters", "filters must be an object.")
    filters = {str(key): value for key, value in raw_filters.items() if value is not None}
    product_type = filters.get("product_type")
    if policy.product_type == "default_spot" and not product_type:
        product_type = "SPOT"
    if policy.product_type == "required" and not product_type:
        raise PartnerQueryError("product_type_required", "product_type must be SPOT or FUTURES.")
    if product_type:
        normalized_product = str(product_type).upper()
        if normalized_product not in PRODUCT_TYPES:
            raise PartnerQueryError("invalid_product_type", "product_type must be SPOT or FUTURES.")
        filters["product_type"] = normalized_product
    return filters


def _normalize_time_range(
    request: Mapping[str, Any],
    policy: OperationPolicy,
    now: datetime,
) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    continuation = {"can_continue": False, "stop_reason": "no_time_continuation"}
    raw_range = request.get("time_range")
    if policy.time_format == "none":
        if raw_range:
            raise PartnerQueryError("invalid_time_range", "This operation does not accept a time range.")
        return None, continuation
    if raw_range is None:
        if policy.default_days is None:
            raise PartnerQueryError(
                "time_range_required",
                "The official contract does not expose a safe minimum default; provide start and end.",
            )
        minimum_days = choose_official_minimum_days([policy.default_days])
        end = now.astimezone(timezone.utc).replace(microsecond=0)
        start = end - timedelta(days=minimum_days)
        time_range = {
            "source": "official_minimum_default",
            "minimum_days": minimum_days,
            "requested_start": None,
            "requested_end": None,
            "actual_start": _format_utc(start),
            "actual_end": _format_utc(end),
        }
        if policy.history_days and policy.history_days > minimum_days and policy.max_days:
            continuation = {
                "can_continue": True,
                "stop_reason": "earlier_official_range_available",
                "next_end": _format_utc(start),
                "segment_days": minimum_days,
            }
        return time_range, continuation
    if not isinstance(raw_range, Mapping) or not raw_range.get("start") or not raw_range.get("end"):
        raise PartnerQueryError("time_range_required", "Both time_range.start and time_range.end are required.")

    if policy.time_format == "date":
        start_date = _parse_date(str(raw_range["start"]))
        end_date = _parse_date(str(raw_range["end"]))
        if start_date > end_date:
            raise PartnerQueryError("invalid_time_range", "Start date must not be after end date.")
        return {
            "source": "user",
            "requested_start": start_date.isoformat(),
            "requested_end": end_date.isoformat(),
            "actual_start": start_date.isoformat(),
            "actual_end": end_date.isoformat(),
        }, continuation

    requested_start = _as_utc_datetime(str(raw_range["start"]))
    requested_end = _as_utc_datetime(str(raw_range["end"]))
    if requested_start >= requested_end:
        raise PartnerQueryError("invalid_time_range", "Start time must be before end time.")
    actual_start = requested_start
    actual_end = requested_end
    duration = requested_end - requested_start
    if policy.history_days:
        earliest = now.astimezone(timezone.utc) - timedelta(days=policy.history_days)
        if requested_start < earliest:
            raise PartnerQueryError(
                "time_range_out_of_history",
                f"This endpoint only exposes the most recent {policy.history_days} days.",
            )
    if policy.history_months:
        earliest_month = _subtract_calendar_months(now.astimezone(timezone.utc), policy.history_months)
        if requested_start < earliest_month:
            raise PartnerQueryError(
                "time_range_out_of_history",
                f"This endpoint only exposes the most recent {policy.history_months} calendar month(s).",
            )
    if policy.max_days and duration > timedelta(days=policy.max_days):
        if policy.history_days and policy.history_days > policy.max_days:
            actual_start = requested_end - timedelta(days=policy.max_days)
            continuation = {
                "can_continue": True,
                "stop_reason": "requested_range_not_fully_covered",
                "next_end": _format_utc(actual_start),
                "remaining_start": _format_utc(requested_start),
                "segment_days": policy.max_days,
            }
        else:
            raise PartnerQueryError(
                "time_range_too_large",
                f"This endpoint accepts at most {policy.max_days} days per request.",
            )
    if policy.max_months and requested_start < _subtract_calendar_months(requested_end, policy.max_months):
        raise PartnerQueryError(
            "time_range_too_large",
            f"This endpoint accepts at most {policy.max_months} calendar month(s) per request.",
        )
    return {
        "source": "user",
        "requested_start": _format_utc(requested_start),
        "requested_end": _format_utc(requested_end),
        "actual_start": _format_utc(actual_start),
        "actual_end": _format_utc(actual_end),
    }, continuation


def plan_query(request: Mapping[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    operation = str(request.get("operation") or "")
    policy = OPERATION_POLICIES.get(operation)
    if policy is None:
        raise PartnerQueryError("unsupported_operation", f"Unsupported Partner operation: {operation!r}")
    profile = str(request.get("profile") or "").strip()
    if not profile:
        raise PartnerQueryError("profile_required", "A saved profile name or ID is required.")
    result_mode = str(request.get("result_mode") or "summary_with_first_20")
    if result_mode not in RESULT_MODES:
        raise PartnerQueryError("invalid_result_mode", f"Unsupported result mode: {result_mode!r}")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    scope = _normalize_scope(request, policy)
    filters = _normalize_filters(request, policy)
    time_range, time_continuation = _normalize_time_range(request, policy, current_time)
    return {
        "operation": operation,
        "endpoint": policy.endpoint,
        "profile": profile,
        "language": str(request.get("language") or "en"),
        "query_scope": scope,
        "time_range": time_range,
        "filters": filters,
        "result_mode": result_mode,
        "contract_version": CONTRACT_VERSION,
        "continuation": time_continuation,
    }


def _milliseconds(value: str) -> int:
    return int(_as_utc_datetime(value).timestamp() * 1000)


def _base_executor_request(plan: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "endpoint": plan["endpoint"],
        "profile": plan["profile"],
        "language": plan.get("language", "en"),
        "query": {},
        "body": {},
    }


def _apply_time_and_filters(request: Dict[str, Any], plan: Mapping[str, Any]) -> None:
    operation = str(plan["operation"])
    policy = OPERATION_POLICIES[operation]
    target = request["body"] if request["endpoint"] == "partner.query-sub-channel-transactions" else request["query"]
    time_range = plan.get("time_range")
    if time_range:
        if policy.time_format == "milliseconds":
            target["startTime"] = _milliseconds(str(time_range["actual_start"]))
            target["endTime"] = _milliseconds(str(time_range["actual_end"]))
        else:
            target["startTime"] = time_range["actual_start"]
            target["endTime"] = time_range["actual_end"]
    field_map = {
        "coin": "coin",
        "product_type": "productType",
        "withdraw_id": "withdrawID",
        "from_account_type": "fromAccountType",
        "to_account_type": "toAccountType",
    }
    for source, destination in field_map.items():
        if source in plan.get("filters", {}):
            target[destination] = plan["filters"][source]
    if policy.page_field:
        target[policy.page_field] = 1
    if policy.page_size_field:
        target[policy.page_size_field] = 100


def build_executor_requests(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
    operation = str(plan["operation"])
    scope = plan.get("query_scope") or {}
    uids = list(scope.get("uids") or [])
    requests: List[Dict[str, Any]] = []
    if operation == "verify-referrals":
        for index in range(0, len(uids), 100):
            item = _base_executor_request(plan)
            item["query"]["userIds"] = ",".join(uids[index:index + 100])
            requests.append(item)
        return requests

    item = _base_executor_request(plan)
    _apply_time_and_filters(item, plan)
    target = item["body"] if item["endpoint"] == "partner.query-sub-channel-transactions" else item["query"]
    if operation in {"list-referral-uids", "get-direct-trade-asset", "get-commission"} and uids:
        target["uid"] = uids[0]
    elif operation == "get-sub-agent-stats" and uids:
        target["subUid"] = uids[0]
    elif operation == "get-referral-assets":
        target["userId"] = uids[0]
    elif operation == "get-referral-deal-data" and uids:
        target["userIds"] = uids
    requests.append(item)
    return requests


def aggregate_decimal_fields(
    records: Iterable[Mapping[str, Any]],
    *,
    group_field: str,
    amount_field: str,
) -> Dict[str, Decimal]:
    result: Dict[str, Decimal] = {}
    for record in records:
        group = str(record.get(group_field, ""))
        if not group or amount_field not in record:
            continue
        try:
            amount = Decimal(str(record[amount_field]))
        except (InvalidOperation, ValueError) as exc:
            raise PartnerQueryError(
                "invalid_decimal",
                f"Invalid decimal value in {amount_field}.",
            ) from exc
        result[group] = result.get(group, Decimal("0")) + amount
    return result


def project_known_fields(payload: Mapping[str, Any], *, known_fields: set[str]) -> Dict[str, Any]:
    record = {key: value for key, value in payload.items() if key in known_fields}
    if "isRefferal" in record:
        record["is_referral"] = bool(record.pop("isRefferal"))
    unknown_fields = sorted(str(key) for key in payload if key not in known_fields)
    return {"record": record, "unknown_fields": unknown_fields}


def build_result_envelope(
    *,
    operation: str,
    result_mode: str,
    records: Sequence[Mapping[str, Any]],
    pages_fetched: int,
    pages_total: Optional[int],
    records_total: Optional[int],
    next_page: Optional[int],
    profile: Optional[Mapping[str, Any]] = None,
    query_scope: Optional[Mapping[str, Any]] = None,
    time_range: Optional[Mapping[str, Any]] = None,
    summary: Optional[Mapping[str, Any]] = None,
    continuation: Optional[Mapping[str, Any]] = None,
    source_complete: Optional[bool] = None,
) -> Dict[str, Any]:
    displayed = list(records) if result_mode == "complete_list" else list(records[:20])
    total = records_total if records_total is not None else len(records)
    has_more = total > len(displayed) or bool(next_page)
    complete = (
        bool(source_complete)
        if source_complete is not None
        else not has_more and (pages_total is None or pages_fetched >= pages_total)
    )
    remaining = max(total - len(displayed), 0)
    effective_continuation = dict(continuation or {})
    if has_more:
        effective_continuation.update(
            {
                "can_continue": True,
                "stop_reason": "next_page_or_records_available",
            }
        )
    else:
        effective_continuation.setdefault("can_continue", False)
        effective_continuation.setdefault("stop_reason", "requested_range_covered")
    return {
        "ok": True,
        "complete": complete,
        "partial": False,
        "operation": operation,
        "profile": dict(profile or {}),
        "api_domain": "partner",
        "environment": "partner_production",
        "capability_mode": "read_only_query",
        "query_scope": dict(query_scope or {}),
        "time_range": dict(time_range or {}) if time_range else None,
        "pagination": {
            "pages_fetched": pages_fetched,
            "pages_total": pages_total,
            "records_fetched": len(records),
            "records_total": total,
            "displayed_count": len(displayed),
            "has_more": has_more,
            "remaining_count": remaining,
            "next_page": next_page,
        },
        "summary": dict(summary or {}),
        "records": displayed,
        "continuation": effective_continuation,
        "warnings": [],
        "error": None,
    }


def build_partial_error_envelope(
    *,
    operation: str,
    records: Sequence[Mapping[str, Any]],
    pages_fetched: int,
    next_page: Optional[int],
    records_total: Optional[int],
    error: Mapping[str, Any],
    offset_pagination: bool,
) -> Dict[str, Any]:
    continuation = {
        "can_continue": False,
        "restart_required": bool(offset_pagination),
        "restart_from_page": 1 if offset_pagination else None,
        "stop_reason": (
            "unstable_offset_pagination_interrupted"
            if offset_pagination
            else "query_interrupted"
        ),
    }
    return {
        "ok": False,
        "complete": False,
        "partial": bool(records),
        "operation": operation,
        "pagination": {
            "pages_fetched": pages_fetched,
            "next_page": next_page,
            "records_fetched": len(records),
            "records_total": records_total,
            "has_more": True,
            "remaining_count": (
                max(records_total - len(records), 0) if records_total is not None else None
            ),
        },
        "summary": None,
        "records": list(records[:20]),
        "continuation": continuation,
        "error": dict(error),
    }


CONTINUATION_BINDING_FIELDS = (
    "resolved_profile_id",
    "query_scope",
    "operation",
    "contract_version",
    "filters",
    "result_mode",
)


def validate_continuation(continuation: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    mismatches = [
        field
        for field in CONTINUATION_BINDING_FIELDS
        if continuation.get(field) != request.get(field)
    ]
    if mismatches:
        raise PartnerQueryError(
            "continuation_mismatch",
            "Continuation does not match the current query: " + ", ".join(mismatches),
        )


def _trader_script() -> Path:
    return Path(__file__).resolve().parents[2] / "weex-trader-skill" / "scripts" / "weex_partner_api.py"


def invoke_trader(request_payload: Mapping[str, Any]) -> Dict[str, Any]:
    script = _trader_script()
    if not script.exists():
        raise PartnerQueryError(
            "trader_dependency_missing",
            "Install weex-trader-skill before weex-partner-skill.",
        )
    completed = subprocess.run(
        [sys.executable, str(script), "execute"],
        input=json.dumps(request_payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PartnerQueryError(
            "trader_protocol_error",
            "The trader Partner executor did not return valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise PartnerQueryError("trader_protocol_error", "Trader response must be a JSON object.")
    return payload


def _unwrap_records(response: Mapping[str, Any]) -> tuple[List[Mapping[str, Any]], Dict[str, Any]]:
    payload: Any = response.get("data")
    if isinstance(payload, Mapping) and "data" in payload and (
        "code" in payload or "msg" in payload
    ):
        payload = payload["data"]
    metadata: Dict[str, Any] = {}
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)], metadata
    if isinstance(payload, Mapping):
        metadata = dict(payload)
        for key in (
            "records",
            "channelUserInfoItemList",
            "channelCommissionInfoItems",
            "items",
            "list",
            "rows",
            "data",
        ):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, Mapping)], metadata
        return [payload], metadata
    return [], metadata


def execute_query(
    request: Mapping[str, Any],
    *,
    executor=invoke_trader,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    plan = plan_query(request, now=now)
    executor_requests = build_executor_requests(plan)
    collected: List[Mapping[str, Any]] = []
    unknown_fields: set[str] = set()
    pages_fetched = 0
    records_total = 0
    pages_total = 0
    next_page: Optional[int] = None
    resolved_profile: Dict[str, Any] = {}
    policy = OPERATION_POLICIES[str(plan["operation"])]
    fetch_all_pages = plan["result_mode"] in {"complete_list", "aggregate_all"}
    for initial_request in executor_requests:
        executor_request = copy.deepcopy(initial_request)
        first_page_for_request = True
        expected_remote_total: Optional[int] = None
        expected_remote_pages: Optional[int] = None
        while True:
            response = executor(executor_request)
            if not response.get("ok"):
                return build_partial_error_envelope(
                    operation=str(plan["operation"]),
                    records=collected,
                    pages_fetched=pages_fetched,
                    next_page=(
                        int(
                            (executor_request["body"] if executor_request["body"] else executor_request["query"])
                            .get(policy.page_field, pages_fetched + 1)
                        )
                        if policy.page_field
                        else None
                    ),
                    records_total=records_total or None,
                    error=response.get("error") or {"category": "upstream"},
                    offset_pagination=bool(policy.page_field),
                )
            resolved_profile = dict(response.get("profile") or resolved_profile)
            records, metadata = _unwrap_records(response)
            current_page = int(
                metadata.get("current", metadata.get("page", metadata.get("pageNum", 1))) or 1
            )
            remote_pages = int(
                metadata.get(
                    "pages",
                    metadata.get("totalPages", metadata.get("pageCount", 1)),
                )
                or 1
            )
            remote_total = int(metadata.get("total", len(records)) or len(records))
            if not first_page_for_request and (
                remote_total != expected_remote_total or remote_pages != expected_remote_pages
            ):
                return build_partial_error_envelope(
                    operation=str(plan["operation"]),
                    records=collected,
                    pages_fetched=pages_fetched,
                    next_page=current_page,
                    records_total=records_total or None,
                    error={
                        "category": "completeness",
                        "code": "pagination_metadata_changed",
                        "message": "Pagination total or page count changed during the query.",
                    },
                    offset_pagination=True,
                )
            pages_fetched += 1
            if first_page_for_request:
                pages_total += remote_pages
                records_total += remote_total
                expected_remote_total = remote_total
                expected_remote_pages = remote_pages
                first_page_for_request = False
            for record in records:
                projected = project_known_fields(
                    record,
                    known_fields=KNOWN_FIELDS[str(plan["operation"])],
                )
                collected.append(projected["record"])
                unknown_fields.update(projected["unknown_fields"])

            if current_page >= remote_pages:
                break
            next_page = current_page + 1
            if not fetch_all_pages or not policy.page_field:
                break
            target = executor_request["body"] if executor_request["body"] else executor_request["query"]
            target[policy.page_field] = next_page
            next_page = None

    summary: Dict[str, Any] = {}
    if plan["operation"] == "get-commission":
        amounts = aggregate_decimal_fields(
            collected,
            group_field="coin",
            amount_field="commission",
        )
        summary["commission_by_coin"] = {key: str(value) for key, value in amounts.items()}
    result = build_result_envelope(
        operation=str(plan["operation"]),
        result_mode=str(plan["result_mode"]),
        records=collected,
        pages_fetched=pages_fetched,
        pages_total=pages_total,
        records_total=records_total,
        next_page=next_page,
        profile=resolved_profile,
        query_scope=plan["query_scope"],
        time_range=plan["time_range"],
        summary=summary,
        continuation=plan["continuation"],
        source_complete=pages_fetched >= pages_total,
    )
    if unknown_fields:
        result["warnings"].append(
            {"code": "unknown_response_fields", "field_names": sorted(unknown_fields)}
        )
    return result


def _load_request(path: str) -> Dict[str, Any]:
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PartnerQueryError("invalid_json", f"Invalid request JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PartnerQueryError("invalid_json", "Request JSON must be an object.")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WEEX Partner deterministic query orchestrator")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in OPERATION_POLICIES:
        command = subparsers.add_parser(operation)
        command.add_argument(
            "--request-file",
            default="-",
            help="Structured request JSON file; default '-' reads stdin",
        )
        command.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        request_payload = _load_request(args.request_file)
        declared_operation = request_payload.get("operation")
        if declared_operation not in {None, args.operation}:
            raise PartnerQueryError(
                "operation_mismatch",
                "The subcommand and request operation do not match.",
            )
        request_payload["operation"] = args.operation
        result = execute_query(request_payload)
    except PartnerQueryError as exc:
        result = {
            "ok": False,
            "complete": False,
            "partial": False,
            "error": {"category": "local_validation", "code": exc.code, "message": str(exc)},
        }
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
