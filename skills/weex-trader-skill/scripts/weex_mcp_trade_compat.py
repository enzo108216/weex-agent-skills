#!/usr/bin/env python3
"""Legacy Contract/Spot Trade compatibility facade over Local MCP tools."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weex_mcp_gateway import (
    LocalMcpGateway,
    McpGatewayError,
    describe_local_mcp_request,
)
from weex_mcp_read_compat import (
    EXPECTED_MAPPING_VERSION,
    LocalMcpReadSettings,
    _http_status,
    _load_default_mapping,
    _request_description_contract_error,
    _unwrap_gateway_result,
    _unwrap_request_description,
    resolve_local_mcp_read_settings,
)
from weex_order_intent_state import (
    FileManualIntentStore,
    IntentStateError,
    build_unclaimed_intent,
    claimed_execution_grant_for_recovery,
    confirm_and_issue_intent,
    execution_grant_for_dispatch,
    load_manual_intent,
    manual_intent_dispatch_lock,
    save_unclaimed_intent,
)


_PRODUCTS = frozenset({"contract", "spot"})
_OUTCOME_CLASSES = frozenset(
    {
        "invocation_rejected",
        "not_sent",
        "definitive_rejection",
        "accepted",
        "unknown",
        "partial",
    }
)
_BOUNDARY_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "mcp-boundary-contract-v2.json"
)


def dynamic_scope_notice(
    risk: Mapping[str, Any], *, action: str | None, language: str
) -> str | None:
    if risk.get("dynamic_scope") is not True:
        return None
    count = risk.get("observed_count")
    positions = action in {"close_positions", "trade_close_positions"}
    if language.lower().startswith("zh"):
        subject = "仓位" if positions else "订单"
        return f"预览时观察到 {count} 个{subject}。确认作用于执行时匹配的范围，当前数量不是快照。"
    subject = "positions" if positions else "orders"
    return f"Observed {count} {subject} during preparation. Confirmation applies to the matching set at execution time; the observed count is not a snapshot."


class McpTradeCompatibilityError(RuntimeError):
    """Stable failure raised before an MCP Trade mutation can be attempted."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LocalMcpTradeSettings:
    profile_id: str
    spot_base_url: str
    futures_base_url: str
    timeout_seconds: float
    locale: str
    profile_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise McpTradeCompatibilityError(
                "profile_required",
                "A saved profile is required for MCP Trade tools.",
            )
        try:
            validated = LocalMcpReadSettings(
                profile_id=self.profile_id,
                spot_base_url=self.spot_base_url,
                futures_base_url=self.futures_base_url,
                timeout_seconds=self.timeout_seconds,
                locale=self.locale,
            )
        except Exception as exc:
            raise McpTradeCompatibilityError(
                "trade_settings_invalid",
                "The Local MCP Trade settings are invalid.",
            ) from exc
        object.__setattr__(self, "spot_base_url", validated.spot_base_url)
        object.__setattr__(self, "futures_base_url", validated.futures_base_url)
        object.__setattr__(self, "timeout_seconds", validated.timeout_seconds)
        if self.profile_name is None:
            object.__setattr__(self, "profile_name", self.profile_id)
        elif not isinstance(self.profile_name, str) or not self.profile_name:
            raise McpTradeCompatibilityError(
                "profile_required",
                "A saved profile name is required for MCP Trade tools.",
            )


def resolve_local_mcp_trade_settings(
    *,
    profile: Any,
    active_product: str,
    explicit_spot_base_url: str | None = None,
    explicit_futures_base_url: str | None = None,
    explicit_timeout: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> LocalMcpTradeSettings:
    resolved = resolve_local_mcp_read_settings(
        profile=profile,
        explicit_spot_base_url=explicit_spot_base_url,
        explicit_futures_base_url=explicit_futures_base_url,
        explicit_timeout=explicit_timeout,
        active_product=active_product,
        environ=environ,
    )
    if resolved.profile_id is None:
        raise McpTradeCompatibilityError(
            "profile_required",
            "A saved profile is required for MCP Trade tools.",
        )
    return LocalMcpTradeSettings(
        profile_id=resolved.profile_id,
        spot_base_url=resolved.spot_base_url,
        futures_base_url=resolved.futures_base_url,
        timeout_seconds=resolved.timeout_seconds,
        locale=resolved.locale,
        profile_name=(
            str(getattr(profile, "name", "") or resolved.profile_id)
        ),
    )


def _load_default_boundary_contract() -> dict[str, Any]:
    try:
        value = json.loads(_BOUNDARY_CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The Trader MCP boundary contract is unavailable or invalid.",
        ) from exc
    if not isinstance(value, dict):
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The Trader MCP boundary contract is unavailable or invalid.",
        )
    return value


def _default_client_id() -> str:
    return f"codex-{int(time.time() * 1000)}-{secrets.token_hex(3)}"


class McpTradeFacade:
    """Prepare and confirm one mapping-bound manual Trade operation."""

    def __init__(
        self,
        *,
        settings: LocalMcpTradeSettings,
        gateway_factory: Callable[..., Any] = LocalMcpGateway,
        request_describer: Callable[..., dict[str, Any]] = describe_local_mcp_request,
        mapping_loader: Callable[[], dict[str, Any]] = _load_default_mapping,
        boundary_contract_loader: Callable[
            [], dict[str, Any]
        ] = _load_default_boundary_contract,
        client_id_factory: Callable[[], str] = _default_client_id,
        intent_id_factory: Callable[[], str] | None = None,
        intent_store_factory: Callable[[], Any] = FileManualIntentStore,
        intent_saver: Callable[..., None] = save_unclaimed_intent,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.settings = settings
        self._gateway_factory = gateway_factory
        self._request_describer = request_describer
        self._gateway: Any | None = None
        self._client_id_factory = client_id_factory
        self._intent_id_factory = intent_id_factory or (
            lambda: "intent_" + secrets.token_hex(16)
        )
        self._intent_store = intent_store_factory()
        self._intent_saver = intent_saver
        self._clock = clock or (lambda: int(time.time() * 1000))
        try:
            mapping = mapping_loader()
            boundary = boundary_contract_loader()
        except Exception as exc:
            raise McpTradeCompatibilityError(
                "mapping_invalid",
                "The Trader MCP capability mapping is unavailable or invalid.",
            ) from exc
        self._operations = _build_trade_operations(mapping, boundary)
        self._contract_versions = _candidate_contract_versions(mapping)

    def _get_gateway(self) -> Any:
        if self._gateway is None:
            self._gateway = self._gateway_factory(
                profile_id=self.settings.profile_id,
                effective_spot_base_url=self.settings.spot_base_url,
                effective_futures_base_url=self.settings.futures_base_url,
                trade_enabled=True,
                timeout_seconds=self.settings.timeout_seconds,
                request_locale=self.settings.locale,
                grant_consumer=self._intent_store,
            )
        return self._gateway

    def preview(
        self,
        *,
        product: str,
        endpoint: Any,
        query: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        operation = self._operation(product=product, endpoint=endpoint)
        arguments = _map_trade_arguments(
            operation,
            query=dict(query),
            body=dict(body),
            client_id_factory=self._client_id_factory,
        )
        direct_tool = str(operation["direct_tool"])
        intent_id = self._intent_id_factory()
        if not isinstance(intent_id, str) or not intent_id:
            raise McpTradeCompatibilityError(
                "intent_invalid",
                "The manual mutation intent ID is invalid.",
            )
        try:
            gateway = self._get_gateway()
            prepared = gateway.prepare_mutation(direct_tool, arguments)
            binding_probe = gateway.build_execution_grant(
                prepared,
                grant_id="probe_" + hashlib.sha256(
                    intent_id.encode("utf-8")
                ).hexdigest()[:32],
                issued_at=0,
            )
        except McpGatewayError as exc:
            return _gateway_error(exc)
        if (
            not isinstance(prepared, dict)
            or prepared.get("tool_name") != direct_tool
            or not isinstance(prepared.get("risk_facts"), dict)
            or not isinstance(prepared.get("canonical_arguments"), dict)
        ):
            return _compat_error(
                "The WEEX MCP PreparedMutation is invalid."
            )
        active_now = self._clock()
        profile_binding = {
            "profile_id": self.settings.profile_id,
            "profile_name": str(self.settings.profile_name),
            "principal_id": binding_probe.principal_id,
            "account_binding_id": binding_probe.account_binding_id,
            "spot_base_url": self.settings.spot_base_url,
            "futures_base_url": self.settings.futures_base_url,
            "route_fingerprint": prepared["route_fingerprint"],
            "credential_fingerprint": binding_probe.credential_fingerprint,
        }
        try:
            intent = build_unclaimed_intent(
                prepared_mutation=prepared,
                profile_binding=profile_binding,
                contract_versions=self._contract_versions,
                intent_id=intent_id,
                created_at=active_now,
            )
            self._intent_saver(intent, now_ms=active_now)
        except IntentStateError as exc:
            return _intent_error(exc)
        record = {
            "version": 2,
            "intent_id": intent_id,
            "product": product,
            "legacy_key": operation["legacy_key"],
            "direct_tool": direct_tool,
            "prepared_mutation": deepcopy(prepared),
            "prepared_artifact_digest": prepared[
                "prepared_artifact_digest"
            ],
            "execution_binding_digest": prepared[
                "execution_binding_digest"
            ],
            "expires_at": prepared["expires_at"],
            "profile_id": self.settings.profile_id,
            "spot_base_url": self.settings.spot_base_url,
            "futures_base_url": self.settings.futures_base_url,
        }
        return {
            "ok": True,
            "status": None,
            "preview": record,
            "summary": {
                "tool_name": direct_tool,
                "action": prepared.get("action"),
                "market": prepared.get("market"),
                "trading_mode": prepared.get("trading_mode"),
            },
            "risk": deepcopy(prepared["risk_facts"]),
            "expires_at": prepared["expires_at"],
            "intent_id": intent_id,
            "risk_signature": prepared["prepared_artifact_digest"],
        }

    def describe(
        self,
        *,
        product: str,
        endpoint: Any,
        query: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        operation = self._operation(product=product, endpoint=endpoint)
        arguments = _map_trade_arguments(
            operation,
            query=dict(query),
            body=dict(body),
            client_id_factory=self._client_id_factory,
        )
        try:
            wrapped = self._request_describer(
                name=str(operation["direct_tool"]),
                arguments=arguments,
                effective_spot_base_url=self.settings.spot_base_url,
                effective_futures_base_url=self.settings.futures_base_url,
                trade_enabled=True,
                timeout_seconds=self.settings.timeout_seconds,
                request_locale=self.settings.locale,
            )
        except McpGatewayError as exc:
            raise McpTradeCompatibilityError(exc.code, exc.message) from exc
        description = _unwrap_request_description(wrapped)
        if _request_description_contract_error(
            operation,
            description,
            spot_base_url=self.settings.spot_base_url,
            futures_base_url=self.settings.futures_base_url,
        ):
            raise McpTradeCompatibilityError(
                "request_description_mismatch",
                "The MCP request description does not match the verified legacy mapping.",
            )
        return description

    def confirm(self, preview: Mapping[str, Any]) -> dict[str, Any]:
        return _two_phase_confirmation_required()

    def confirm_intent(
        self,
        *,
        intent_id: str,
        prepared_artifact_digest: str,
    ) -> dict[str, Any]:
        active_now = self._clock()
        try:
            current = load_manual_intent(now_ms=active_now)
        except IntentStateError as exc:
            return _intent_error(exc)
        if (
            not isinstance(current, dict)
            or current.get("intent_id") != intent_id
            or current.get("prepared_artifact_digest")
            != prepared_artifact_digest
            or current.get("profile_binding", {}).get("profile_id")
            != self.settings.profile_id
            or current.get("profile_binding", {}).get("spot_base_url")
            != self.settings.spot_base_url
            or current.get("profile_binding", {}).get("futures_base_url")
            != self.settings.futures_base_url
        ):
            raise McpTradeCompatibilityError(
                "preview_binding_mismatch",
                "The manual mutation intent does not match this profile or digest.",
            )
        operation = self._operation_for_intent(current)
        return self._dispatch_current_intent(
            operation,
            current,
            active_now=active_now,
        )

    def reconcile_intent(self, intent_id: str) -> dict[str, Any]:
        active_now = self._clock()
        try:
            current = load_manual_intent(now_ms=active_now)
        except IntentStateError as exc:
            return _intent_error(exc)
        if (
            not isinstance(intent_id, str)
            or not intent_id
            or not isinstance(current, dict)
            or current.get("intent_id") != intent_id
            or current.get("profile_binding", {}).get("profile_id")
            != self.settings.profile_id
            or current.get("profile_binding", {}).get("spot_base_url")
            != self.settings.spot_base_url
            or current.get("profile_binding", {}).get("futures_base_url")
            != self.settings.futures_base_url
        ):
            return _intent_error(
                IntentStateError(
                    "intent_missing",
                    "Manual mutation intent was not found for this profile.",
                )
            )
        operation = self._operation_for_intent(current)
        if current["state"] in {"CLAIMED", "REVIEW_REQUIRED"}:
            try:
                with manual_intent_dispatch_lock():
                    locked_intent = _safe_load_intent(current, active_now)
                    if locked_intent.get("intent_id") != intent_id:
                        raise IntentStateError("intent_missing", "The stored intent is unavailable.")
                    return self._reconcile_current_intent(operation, locked_intent, active_now=active_now)
            except IntentStateError as exc:
                return _project_persisted_intent_state(
                    operation, _safe_load_intent(current, active_now), code=exc.code, message=str(exc),
                )
        return self._reconcile_current_intent(operation, current, active_now=active_now)

    def _reconcile_current_intent(
        self,
        operation: dict[str, Any],
        current: dict[str, Any],
        *,
        active_now: int,
    ) -> dict[str, Any]:
        intent_id = current["intent_id"]
        if current["state"] == "CLAIMED":
            try:
                gateway = self._get_gateway()
                claimed = claimed_execution_grant_for_recovery(
                    intent_id,
                    now_ms=active_now,
                )
                gateway.recover_mutation(
                    tool_name=current["tool_name"],
                    execution_grant=claimed,
                    now_ms=active_now,
                )
            except (IntentStateError, McpGatewayError) as exc:
                code = getattr(exc, "code", "recovery_failed")
                return _project_persisted_intent_state(
                    operation,
                    _safe_load_intent(current, active_now),
                    code=code,
                    message=str(exc),
                )
            recovered = _safe_load_intent(current, active_now)
            if recovered.get("intent_id") != intent_id:
                return _project_persisted_intent_state(
                    operation,
                    recovered,
                    code="intent_missing",
                    message="The recovered intent is unavailable.",
                )
            current = recovered
        reconciliation = current.get("reconciliation")
        reconciliation_status = (
            reconciliation.get("status")
            if isinstance(reconciliation, dict)
            else None
        )
        if reconciliation_status in {
            "RESOLVED_ACCEPTED",
            "RESOLVED_REJECTED",
            "RESOLVED_NOT_SENT",
        }:
            return _project_persisted_intent_state(
                operation,
                current,
                code="reconciliation_already_resolved",
                message="The durable reconciliation result is already terminal.",
            )
        if current["state"] != "REVIEW_REQUIRED":
            return _project_persisted_intent_state(
                operation,
                current,
                code="reconciliation_not_required",
                message="The manual mutation intent does not require reconciliation.",
            )
        prepared = current.get("prepared_mutation")
        if not isinstance(prepared, dict):
            return _project_persisted_intent_state(
                operation,
                current,
                code="manual_review_required",
                message="This historical intent has no complete PreparedMutation for reconciliation.",
            )
        saved_boundary = current["contract_versions"]["boundary_contract_digest"]
        try:
            reconciliation_options = (
                {"boundary_contract_digest": saved_boundary}
                if saved_boundary != self._contract_versions["boundary_contract_digest"]
                else {}
            )
            result = self._get_gateway().reconcile_mutation(
                prepared, now_ms=active_now, **reconciliation_options
            )
            updated = self._intent_store.record_reconciliation_result(
                intent_id,
                status=result["status"],
                evidence_digest=result["evidence_digest"],
                observed_at=result["observed_at"],
            )
        except (McpGatewayError, IntentStateError) as exc:
            return _project_persisted_intent_state(
                operation,
                _safe_load_intent(current, active_now),
                code=exc.code,
                message=str(exc),
            )
        code, message = {
            "RESOLVED_ACCEPTED": (
                "reconciliation_resolved",
                "WEEX MCP verified an accepted outcome.",
            ),
            "UNRESOLVED": (
                "reconciliation_unresolved",
                "WEEX MCP did not prove a terminal outcome.",
            ),
            "MANUAL_REVIEW_REQUIRED": (
                "manual_review_required",
                "WEEX MCP requires manual review for this outcome.",
            ),
        }[result["status"]]
        return _project_persisted_intent_state(operation, updated, code=code, message=message)

    def _dispatch_current_intent(
        self,
        operation: dict[str, Any],
        current: dict[str, Any],
        *,
        active_now: int,
    ) -> dict[str, Any]:
        try:
            gateway = self._get_gateway()
            if current["state"] == "UNCLAIMED":
                grant = gateway.build_execution_grant(
                    current["prepared_mutation"],
                    grant_id=_grant_id_for_intent(current["intent_id"]),
                    issued_at=active_now,
                )
                confirm_and_issue_intent(
                    expected_intent_id=current["intent_id"],
                    expected_prepared_artifact_digest=current[
                        "prepared_artifact_digest"
                    ],
                    execution_grant=grant,
                    confirmed_at=active_now,
                )
                execution_grant = execution_grant_for_dispatch(
                    current["intent_id"]
                )
            elif current["state"] == "CONFIRMED":
                execution_grant = execution_grant_for_dispatch(
                    current["intent_id"]
                )
            else:
                return _with_intent_reference(
                    _project_persisted_intent_state(
                        operation,
                        current,
                        code="intent_not_dispatchable",
                        message="The manual mutation intent is not dispatchable.",
                    ),
                    current,
                )
            with manual_intent_dispatch_lock():
                wrapped = gateway.call_typed_tool(
                    str(operation["direct_tool"]),
                    dict(current["direct_arguments"]),
                    execution_grant=execution_grant,
                )
        except IntentStateError as exc:
            if exc.code in {"intent_dispatch_in_flight", "grant_unavailable", "confirmation_started"}:
                persisted = _safe_load_intent(current, active_now)
                if exc.code == "intent_dispatch_in_flight" or persisted["state"] in {
                    "CLAIMED", "REVIEW_REQUIRED", "ACCEPTED", "DEFINITIVE_REJECTION", "NOT_SENT_TERMINAL",
                }:
                    return _with_intent_reference(
                        _project_persisted_intent_state(operation, persisted, code=exc.code, message=str(exc)),
                        persisted,
                    )
            return _intent_error(exc)
        except McpGatewayError as exc:
            persisted = _safe_load_intent(current, active_now)
            return _with_intent_reference(
                _project_persisted_intent_state(
                    operation,
                    code=exc.code,
                    message=exc.message,
                    current=persisted,
                ),
                persisted,
            )
        unwrapped = _unwrap_gateway_result(wrapped)
        if unwrapped is None:
            persisted = _safe_load_intent(current, active_now)
            return _with_intent_reference(
                _project_persisted_intent_state(
                    operation,
                    current=persisted,
                    code="compat_response_invalid",
                    message="The WEEX MCP Direct mutation wrapper is invalid.",
                ),
                persisted,
            )
        payload, transport_meta = unwrapped
        projected = _project_confirm_result(
            operation,
            payload,
            transport_meta=transport_meta,
        )
        persisted = _safe_load_intent(current, active_now)
        return _with_intent_reference(
            _project_against_durable_state(
                operation,
                projected,
                persisted,
            ),
            persisted,
        )

    def _operation_for_intent(
        self,
        current: Mapping[str, Any],
    ) -> dict[str, Any]:
        market = current.get("market")
        tool_name = current.get("tool_name")
        product = "spot" if market == "spot" else "contract"
        candidates = sorted(
            (
                operation
                for (operation_product, _legacy_key), operation in self._operations.items()
                if operation_product == product
                and operation.get("direct_tool") == tool_name
            ),
            key=lambda item: str(item["legacy_key"]),
        )
        if not candidates:
            raise McpTradeCompatibilityError(
                "mapping_contract_mismatch",
                "The manual mutation intent has no verified legacy mapping.",
            )
        if (
            tool_name == "trade_place_order"
            and product == "contract"
            and current.get("trading_mode") == "demo"
        ):
            demo = [
                item
                for item in candidates
                if item["legacy_key"] == "sim.transaction.place_order"
            ]
            if demo:
                return demo[0]
        return candidates[0]

    def preview_and_confirm(
        self,
        *,
        product: str,
        endpoint: Any,
        query: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        preview = self.preview(
            product=product,
            endpoint=endpoint,
            query=query,
            body=body,
        )
        if preview.get("ok") is not True:
            return preview
        return {
            **_two_phase_confirmation_required(),
            "confirmation_required": True,
            **{field: preview[field] for field in ("intent_id", "risk_signature", "expires_at", "summary", "risk")},
        }

    def _operation(self, *, product: str, endpoint: Any) -> dict[str, Any]:
        legacy_key = getattr(endpoint, "key", None)
        operation = self._operations.get((product, legacy_key))
        if product not in _PRODUCTS or operation is None:
            raise McpTradeCompatibilityError(
                "unsupported_trade_endpoint",
                "The requested endpoint is not an allowlisted MCP Trade operation.",
            )
        expected = {
            "key": operation["legacy_key"],
            "method": operation["method"],
            "path": operation["path"],
            "request_transport": operation["request_transport"],
        }
        actual = {
            "key": legacy_key,
            "method": getattr(endpoint, "method", None),
            "path": getattr(endpoint, "path", None),
            "request_transport": getattr(endpoint, "request_transport", None),
        }
        if expected != actual:
            raise McpTradeCompatibilityError(
                "mapping_contract_mismatch",
                "The legacy Trade endpoint catalog does not match the verified MCP mapping.",
            )
        return operation


def _candidate_contract_versions(mapping: Mapping[str, Any]) -> dict[str, str]:
    candidate = mapping.get("verified_mcp_candidate")
    versions = candidate.get("versions") if isinstance(candidate, dict) else None
    result = {
        "package": (versions or {}).get("package"),
        "tool_contract": (versions or {}).get("tool_contract"),
        "prepare": (versions or {}).get("prepare"),
        "intent": "2",
        "grant": (versions or {}).get("grant"),
        "boundary_contract_digest": (
            candidate.get("boundary_contract_digest")
            if isinstance(candidate, dict)
            else None
        ),
    }
    if result != {
        "package": "0.3.4",
        "tool_contract": "2",
        "prepare": "1",
        "intent": "2",
        "grant": "2",
        "boundary_contract_digest": (
            "sha256:15a8e64916a3227b32c431afae9b6b9de55ca350cf2291c6722f33694b828e38"
        ),
    }:
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The verified MCP manual intent versions are invalid.",
        )
    return result


def _build_trade_operations(
    mapping: Any,
    boundary: Any,
) -> dict[tuple[str, str], dict[str, Any]]:
    if (
        not isinstance(mapping, dict)
        or mapping.get("mapping_version") != EXPECTED_MAPPING_VERSION
        or not isinstance(mapping.get("capabilities"), list)
    ):
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The Trader MCP capability mapping version is not supported.",
        )
    candidate = mapping.get("verified_mcp_candidate")
    if (
        not isinstance(boundary, dict)
        or boundary.get("boundary_contract_digest")
        != (
            candidate.get("boundary_contract_digest")
            if isinstance(candidate, dict)
            else None
        )
        or not isinstance(boundary.get("legacy_write_mapping"), dict)
        or not isinstance(boundary.get("direct_tools"), list)
    ):
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The Trader MCP boundary contract does not match the capability mapping.",
        )
    direct_contracts = {
        item.get("name"): item
        for item in boundary["direct_tools"]
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    legacy_direct = boundary["legacy_write_mapping"]
    operations: dict[tuple[str, str], dict[str, Any]] = {}
    for item in mapping["capabilities"]:
        if not isinstance(item, dict) or item.get("product") not in _PRODUCTS:
            continue
        target = item.get("target")
        if not isinstance(target, dict) or target.get("variant") != "preview_confirm":
            continue
        key = (item.get("product"), item.get("legacy_key"))
        if (
            not isinstance(key[1], str)
            or key in operations
            or not isinstance(target.get("tool"), str)
            or not isinstance(target.get("confirm_tool"), str)
            or not isinstance(target.get("fixed_arguments"), dict)
            or item.get("request_transport") not in {"query", "body"}
            or not isinstance(item.get("catalog_request_fields"), list)
            or not isinstance(item.get("field_decisions"), dict)
        ):
            raise McpTradeCompatibilityError(
                "mapping_invalid",
                "The Trader MCP Trade mapping is incomplete or duplicated.",
            )
        legacy_key = str(item["legacy_key"])
        direct_tool = legacy_direct.get(legacy_key)
        direct_contract = direct_contracts.get(direct_tool)
        if (
            not isinstance(direct_tool, str)
            or not isinstance(direct_contract, dict)
            or not isinstance(direct_contract.get("input_schema"), dict)
        ):
            raise McpTradeCompatibilityError(
                "mapping_invalid",
                "The 21-to-14 Direct mutation mapping is incomplete.",
            )
        operation = deepcopy(item)
        operation["direct_tool"] = direct_tool
        operation["direct_input_schema"] = deepcopy(
            direct_contract["input_schema"]
        )
        operations[key] = operation
    if sum(product == "contract" for product, _ in operations) != 16 or sum(
        product == "spot" for product, _ in operations
    ) != 5:
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The Trader MCP Trade mapping does not cover the 21 legacy writes.",
        )
    if set(legacy_direct) != {
        operation["legacy_key"] for operation in operations.values()
    } or len(set(legacy_direct.values())) != 14:
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The 21-to-14 Direct mutation mapping is invalid.",
        )
    return operations


def _map_trade_arguments(
    operation: dict[str, Any],
    *,
    query: dict[str, Any],
    body: dict[str, Any],
    client_id_factory: Callable[[], str],
) -> dict[str, Any]:
    transport = operation["request_transport"]
    if transport == "query" and body:
        raise McpTradeCompatibilityError(
            "request_transport_mismatch",
            "The legacy Trade endpoint accepts query fields only.",
        )
    if transport == "body" and query:
        raise McpTradeCompatibilityError(
            "request_transport_mismatch",
            "The legacy Trade endpoint accepts body fields only.",
        )
    supplied = query if transport == "query" else body
    allowed_roots = {
        str(field).split("[]", 1)[0].split(".", 1)[0]
        for field in operation["catalog_request_fields"]
    }
    if set(supplied) - allowed_roots:
        raise McpTradeCompatibilityError(
            "unsupported_fields",
            "The legacy Trade request contains fields outside the verified mapping.",
        )
    arguments = dict(operation["target"]["fixed_arguments"])
    decisions = operation["field_decisions"]
    for source, value in supplied.items():
        decision = decisions.get(source)
        if not isinstance(decision, dict):
            raise McpTradeCompatibilityError(
                "mapping_invalid",
                "The Trader MCP Trade field mapping is incomplete.",
            )
        transform = decision.get("value_transform")
        target = decision.get("target")
        if transform == "spot_batch_orders_v1":
            arguments["orders"] = _map_batch_orders(
                value,
                market="spot",
                envelope_symbol=supplied.get("symbol"),
                field_mapping=_batch_field_mapping(operation, source),
                client_id_factory=client_id_factory,
            )
            continue
        if transform == "contract_batch_orders_v1":
            arguments["orders"] = _map_batch_orders(
                value,
                market="futures",
                envelope_symbol=None,
                field_mapping=_batch_field_mapping(operation, source),
                client_id_factory=client_id_factory,
            )
            continue
        if decision.get("decision") == "fanout" and target == "orders[].symbol":
            continue
        if not isinstance(target, str) or "[]" in target:
            continue
        arguments[target] = _transform_value(value, transform)

    direct_tool = operation["direct_tool"]
    if direct_tool == "trade_place_order" and not arguments.get("client_order_id"):
        arguments["client_order_id"] = _new_client_id(client_id_factory)
    if (
        direct_tool == "trade_cancel_open_orders"
        and arguments.get("market") == "futures"
    ):
        if arguments.get("symbol") not in (None, ""):
            arguments["scope"] = "symbol"
        else:
            arguments["scope"] = "all"
    if direct_tool == "trade_close_positions":
        has_position = arguments.get("position_id") not in (None, "")
        has_symbol = arguments.get("symbol") not in (None, "")
        if has_position:
            arguments["scope"] = "position"
        elif has_symbol:
            arguments["scope"] = "symbol"
        else:
            arguments["scope"] = "all"
    if direct_tool == "trade_place_tp_sl":
        arguments.pop("operation", None)
        quantity = arguments.get("quantity")
        if quantity in (None, "", "0"):
            arguments.pop("quantity", None)
            arguments["quantity_mode"] = "full_position"
        else:
            arguments["quantity_mode"] = "exact"
    if direct_tool == "trade_modify_tp_sl":
        for field in (
            "operation",
            "symbol",
            "client_algo_id",
            "plan_type",
            "quantity",
            "position_side",
            "quantity_mode",
        ):
            arguments.pop(field, None)
    for field in (
        "acknowledge_all_symbols",
        "acknowledge_all_positions",
        "acknowledge_full_position",
    ):
        arguments.pop(field, None)
    direct_schema = operation["direct_input_schema"]
    properties = direct_schema.get("properties")
    if not isinstance(properties, dict) or set(arguments) - set(properties):
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The legacy Trade mapping produced unsupported Direct arguments.",
        )
    return arguments


def _map_batch_orders(
    value: Any,
    *,
    market: str,
    envelope_symbol: Any,
    field_mapping: dict[str, str],
    client_id_factory: Callable[[], str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise McpTradeCompatibilityError(
            "invalid_arguments",
            "Batch orders must be a non-empty array of objects.",
        )
    result: list[dict[str, Any]] = []
    for raw in value:
        unknown = set(raw) - set(field_mapping)
        if unknown:
            raise McpTradeCompatibilityError(
                "unsupported_fields",
                "A batch order contains fields outside the verified mapping.",
            )
        item: dict[str, Any] = {}
        for source, target in field_mapping.items():
            if source in raw and raw[source] not in (None, ""):
                item[target] = _encode_scalar(raw[source])
        if market == "spot":
            if envelope_symbol in (None, ""):
                raise McpTradeCompatibilityError(
                    "invalid_arguments",
                    "Spot batch orders require the envelope symbol.",
                )
            item["symbol"] = str(envelope_symbol)
            item.pop("position_side", None)
        if not item.get("client_order_id"):
            item["client_order_id"] = _new_client_id(client_id_factory)
        result.append(item)
    return result


def _batch_field_mapping(
    operation: dict[str, Any],
    root: str,
) -> dict[str, str]:
    decisions: dict[str, Any] = dict(operation["field_decisions"])
    nested_groups = operation.get("nested_field_decisions")
    if isinstance(nested_groups, dict) and isinstance(nested_groups.get(root), dict):
        decisions.update(nested_groups[root])
    prefix = f"{root}[]."
    mapping: dict[str, str] = {}
    for source, decision in decisions.items():
        if not isinstance(source, str) or not source.startswith(prefix):
            continue
        target = decision.get("target") if isinstance(decision, dict) else None
        if not isinstance(target, str) or not target.startswith("orders[]."):
            raise McpTradeCompatibilityError(
                "mapping_invalid",
                "The Trader MCP batch field mapping is invalid.",
            )
        mapping[source[len(prefix):]] = target[len("orders[]."):]
    if not mapping:
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The Trader MCP batch field mapping is missing.",
        )
    return mapping


def _transform_value(value: Any, transform: Any) -> Any:
    if transform == "position_margin_adjustment_v1":
        normalized = str(value).strip().lower()
        aliases = {"1": "increase", "increase": "increase", "2": "decrease", "decrease": "decrease"}
        if normalized not in aliases:
            raise McpTradeCompatibilityError(
                "invalid_arguments",
                "Position margin type must be 1 or 2.",
            )
        return aliases[normalized]
    if transform == "margin_mode_v1":
        normalized = str(value).strip().upper()
        aliases = {
            "CROSS": "CROSS",
            "CROSSED": "CROSS",
            "ISOLATED": "ISOLATED",
        }
        if normalized not in aliases:
            raise McpTradeCompatibilityError(
                "invalid_arguments",
                "Margin mode is not supported.",
            )
        return aliases[normalized]
    if transform == "strict_boolean_v1":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
        raise McpTradeCompatibilityError(
            "invalid_arguments",
            "autoAppendMargin must be a boolean.",
        )
    if transform == "canonical_string_list_v1":
        if not isinstance(value, list) or not value:
            raise McpTradeCompatibilityError(
                "invalid_arguments",
                "Identifier lists must be non-empty arrays.",
            )
        return [_canonical_string(item) for item in value]
    return _encode_scalar(value)


def _encode_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise McpTradeCompatibilityError(
                "invalid_arguments",
                "Non-finite Trade values are not supported.",
            )
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    if isinstance(value, list):
        return [_encode_scalar(item) for item in value]
    return value


def _canonical_string(value: Any) -> str:
    encoded = _encode_scalar(value)
    if not isinstance(encoded, str) or not encoded:
        raise McpTradeCompatibilityError(
            "invalid_arguments",
            "Trade identifiers must be non-empty canonical strings.",
        )
    return encoded


def _new_client_id(factory: Callable[[], str]) -> str:
    value = factory()
    if not isinstance(value, str) or not value or len(value) > 36:
        raise McpTradeCompatibilityError(
            "client_order_id_invalid",
            "The generated client order ID is invalid.",
        )
    return value


def _project_confirm_result(
    operation: dict[str, Any],
    payload: Any,
    *,
    transport_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    status = _http_status(transport_meta)
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return _confirm_uncertain(
            operation,
            code="compat_response_invalid",
            message="The WEEX MCP Trade confirmation envelope is invalid.",
            status=status,
        )
    outcome = payload.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("class") not in _OUTCOME_CLASSES:
        return _confirm_uncertain(
            operation,
            code="compat_response_invalid",
            message="The WEEX MCP Trade confirmation outcome is invalid.",
            status=status,
        )
    outcome_class = str(outcome["class"])
    result = payload.get("result")
    legacy_data = _project_legacy_mutation(operation, payload, outcome_class)
    if payload["ok"] and outcome_class == "accepted":
        if not isinstance(result, dict):
            return _confirm_uncertain(
                operation,
                code="compat_response_invalid",
                message="The WEEX MCP Direct result is invalid.",
                status=status,
            )
        return {
            "ok": True,
            "status": status,
            "data": deepcopy(result),
            **({"legacy_data": legacy_data} if legacy_data is not None else {}),
            "outcome": outcome,
            **(
                {"warning": deepcopy(payload["warning"])}
                if isinstance(payload.get("warning"), dict)
                else {}
            ),
            "review_required": False,
            "trade_status": "ACCEPTED",
        }
    error = payload.get("error")
    if not isinstance(error, dict):
        error = {
            "code": "trade_not_accepted",
            "message": "The WEEX MCP Trade operation was not accepted.",
        }
    review_required = outcome_class in {"unknown", "partial"}
    return {
        "ok": False,
        "status": status,
        "error": error,
        "outcome": outcome,
        "review_required": review_required,
        "trade_status": (
            "REVIEW_REQUIRED"
            if review_required
            else (
                "REJECTED"
                if outcome_class == "definitive_rejection"
                else "NOT_SENT"
            )
        ),
        **({"data": deepcopy(result)} if isinstance(result, dict) else {}),
        **({"legacy_data": legacy_data} if legacy_data is not None else {}),
    }

def _project_legacy_mutation(
    operation: dict[str, Any],
    payload: dict[str, Any],
    outcome_class: str,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    result = payload.get("result")
    if (
        payload.get("action") != _outcome_action(operation)
        or outcome_class not in {"accepted", "definitive_rejection", "partial"}
        or payload["ok"] != (outcome_class == "accepted")
        or not isinstance(result, dict)
    ):
        return None
    key = operation["legacy_key"]
    container = operation["response"]["legacy_container"]
    spot_batch = key in {"spot.order.bulk_order", "spot.order.bulk_cancel"}
    if container != "array" and not spot_batch:
        return _legacy_mutation_record(key, result, outcome_class, item=False)

    items = result.get("items")
    if (
        items is None
        and outcome_class == "accepted"
        and result.get("status") == "accepted_noop"
        and result.get("noop") is True
        and type(result.get("affected_count")) is int
        and result["affected_count"] == 0
    ):
        items = []
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return None
    rows = []
    for item in items:
        outcome = item.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("write_attempted") is not True:
            return None
        item_class = outcome.get("class")
        status = item.get("status")
        if item_class not in ("accepted", "definitive_rejection", "unknown") or (
            status != item_class and not (item_class == "accepted" and status == "accepted_noop")
        ):
            return None
        rows.append(_legacy_mutation_record(key, item, item_class, item=True))
    return {"orderList": rows} if spot_batch else rows


def _legacy_mutation_record(
    key: str,
    record: dict[str, Any],
    outcome_class: str,
    *,
    item: bool,
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    identity = record.get("request_identity")
    identity = identity if isinstance(identity, dict) else {}
    if key in {
        "spot.order.place_order", "spot.order.bulk_order", "transaction.place_order",
        "transaction.place_orders_batch", "sim.transaction.place_order",
    }:
        _copy_legacy_text(projected, "clientOrderId", identity.get("client_order_id"))
    elif key == "transaction.place_pending_order":
        _copy_legacy_text(projected, "clientOrderId", identity.get("client_algo_id"))
    elif key in {"transaction.cancel_order", "transaction.cancel_orders_batch", "transaction.cancel_pending_order"}:
        _copy_legacy_text(projected, "origClientOrderId", identity.get("client_order_id"))

    if key.startswith(("spot.order.", "transaction.", "sim.transaction.")) and key != "transaction.modify_tp_sl_order":
        if key == "transaction.close_positions":
            _copy_legacy_id(projected, "positionId", identity.get("position_id"), as_long=True)
            _copy_legacy_id(projected, "successOrderId", record.get("upstream_order_id"), as_long=True)
        else:
            _copy_legacy_id(
                projected, "orderId", record.get("upstream_order_id"),
                as_long=key.startswith("spot.") or key in {
                    "transaction.cancel_all_orders", "transaction.cancel_all_pending_orders", "transaction.place_tp_sl_order",
                },
            )
    if key.startswith(("transaction.", "sim.transaction.")) and outcome_class in {"accepted", "definitive_rejection"}:
        projected["success"] = outcome_class == "accepted"
    if key in {"spot.order.place_order", "account.update_leverage_trade"}:
        _copy_legacy_text(projected, "symbol", record.get("symbol"))
    elif key == "spot.order.bulk_order":
        _copy_legacy_text(projected, "symbol", identity.get("symbol"))
    elif key in {"spot.order.cancel_order", "spot.order.bulk_cancel", "spot.order.cancel_symbol_orders"}:
        _copy_legacy_text(projected, "status", record.get("final_order_status"))
    if key in {"spot.order.place_order", "spot.order.bulk_order"}:
        compatibility = record.get("compatibility")
        transact_time = compatibility.get("transact_time") if isinstance(compatibility, dict) else None
        if type(transact_time) is int and 0 <= transact_time <= 9_223_372_036_854_775_807:
            projected["transactTime"] = transact_time
    if key == "account.update_leverage_trade":
        target = record.get("target")
        if isinstance(target, dict):
            if target.get("margin_mode") == "CROSS":
                projected["marginType"] = "CROSSED"
            elif target.get("margin_mode") == "ISOLATED":
                projected["marginType"] = "ISOLATED"
            for legacy, typed in (
                ("crossLeverage", "cross_leverage"),
                ("isolatedLongLeverage", "isolated_long_leverage"),
                ("isolatedShortLeverage", "isolated_short_leverage"),
            ):
                _copy_legacy_text(projected, legacy, target.get(typed))
    error = record.get("error")
    if item and isinstance(error, dict):
        if key == "spot.order.bulk_order" or (key.startswith("transaction.") and key != "transaction.close_positions"):
            _copy_legacy_text(projected, "errorCode", error.get("code"))
        if key in {"spot.order.bulk_order", "spot.order.bulk_cancel"}:
            _copy_legacy_text(projected, "errorMsg", error.get("message"))
        elif key.startswith("transaction."):
            _copy_legacy_text(projected, "errorMessage", error.get("message"))
    return projected


def _copy_legacy_text(output: dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, str) and value:
        output[key] = value


def _copy_legacy_id(output: dict[str, Any], key: str, value: Any, *, as_long: bool) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        return
    if not as_long:
        output[key] = value
    elif len(value) < 19 or (len(value) == 19 and value <= "9223372036854775807"):
        output[key] = int(value)


def _gateway_error(exc: McpGatewayError) -> dict[str, Any]:
    return {
        "ok": False,
        "status": None,
        "error": {"code": exc.code, "message": exc.message},
        "outcome": {
            "class": "invocation_rejected",
            "write_attempted": False,
            "grant_disposition": "unchanged",
        },
        "review_required": False,
        "trade_status": "NOT_SENT",
    }


def _intent_error(exc: IntentStateError) -> dict[str, Any]:
    return {
        "ok": False,
        "status": None,
        "error": {"code": exc.code, "message": str(exc)},
        "outcome": {
            "class": "invocation_rejected",
            "write_attempted": False,
            "grant_disposition": "unchanged",
        },
        "review_required": False,
        "trade_status": "NOT_SENT",
    }


def _two_phase_confirmation_required() -> dict[str, Any]:
    return _intent_error(
        IntentStateError(
            "two_phase_confirmation_required",
            "Review the prepared risk, then confirm the saved intent ID and risk signature.",
        )
    )


def _grant_id_for_intent(intent_id: str) -> str:
    return "grant_" + hashlib.sha256(intent_id.encode("utf-8")).hexdigest()[:32]


def _safe_load_intent(intent: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    try:
        current = load_manual_intent(
            now_ms=now_ms,
            expected_intent_id=intent["intent_id"],
            expected_prepared_artifact_digest=intent["prepared_artifact_digest"],
        )
    except IntentStateError:
        return {"state": "REVIEW_REQUIRED"}
    return current if isinstance(current, dict) else {"state": "REVIEW_REQUIRED"}


def _project_against_durable_state(
    operation: dict[str, Any],
    projected: dict[str, Any],
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    state = current.get("state") if isinstance(current, dict) else None
    projected_status = projected.get("trade_status")
    outcome = projected.get("outcome")
    outcome_class = outcome.get("class") if isinstance(outcome, dict) else None
    disposition = (
        outcome.get("grant_disposition") if isinstance(outcome, dict) else None
    )
    matches = (
        (
            state == "ACCEPTED"
            and projected.get("ok") is True
            and projected_status == "ACCEPTED"
            and outcome_class == "accepted"
            and disposition == "accepted"
        )
        or (
            state == "DEFINITIVE_REJECTION"
            and projected.get("ok") is False
            and projected_status == "REJECTED"
            and outcome_class == "definitive_rejection"
            and disposition == "definitive_rejection"
        )
        or (
            state == "NOT_SENT_TERMINAL"
            and projected.get("ok") is False
            and projected_status == "NOT_SENT"
            and outcome_class == "not_sent"
            and disposition == "not_sent_terminal"
        )
        or (
            state in {"CLAIMED", "REVIEW_REQUIRED"}
            and projected.get("ok") is False
            and projected_status == "REVIEW_REQUIRED"
            and outcome_class in {"unknown", "partial"}
            and disposition == "review_required"
        )
        or (
            state in {"UNCLAIMED", "CONFIRMED"}
            and projected.get("ok") is False
            and outcome_class == "invocation_rejected"
            and disposition == "unchanged"
        )
    )
    if matches:
        return projected
    return _project_persisted_intent_state(
        operation,
        current,
        code="durable_outcome_projection_mismatch",
        message="The MCP envelope did not match the durable grant outcome.",
    )


def _with_intent_reference(
    payload: dict[str, Any],
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    outcome = payload.get("outcome")
    outcome_class = outcome.get("class") if isinstance(outcome, dict) else None
    if (
        not isinstance(current, Mapping)
        or (
            payload.get("review_required") is not True
            and outcome_class != "invocation_rejected"
        )
    ):
        return payload
    intent_id = current.get("intent_id")
    digest = current.get("prepared_artifact_digest")
    expires_at = current.get("expires_at")
    if (
        isinstance(intent_id, str)
        and intent_id
        and isinstance(digest, str)
        and isinstance(expires_at, int)
        and not isinstance(expires_at, bool)
    ):
        return {
            **payload,
            "intent_id": intent_id,
            "risk_signature": digest,
            "expires_at": expires_at,
        }
    return payload


def _project_persisted_intent_state(
    operation: dict[str, Any],
    current: dict[str, Any] | None,
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    state = current.get("state") if isinstance(current, dict) else None
    reconciliation_status = (
        current.get("reconciliation", {}).get("status")
        if isinstance(current, dict)
        and isinstance(current.get("reconciliation"), dict)
        else None
    )
    if state == "REVIEW_REQUIRED":
        state = {
            "RESOLVED_ACCEPTED": "ACCEPTED",
            "RESOLVED_REJECTED": "DEFINITIVE_REJECTION",
            "RESOLVED_NOT_SENT": "NOT_SENT_TERMINAL",
        }.get(reconciliation_status, state)
    action = _outcome_action(operation)
    if state == "ACCEPTED":
        return {
            "ok": True,
            "status": None,
            "data": {},
            "outcome": {
                "class": "accepted",
                "write_attempted": True,
                "grant_disposition": "accepted",
                "action": action,
            },
            "warning": {
                "code": "internal_projection_error",
                "message": (
                    "Accepted, but the full result projection was unavailable."
                ),
            },
            "review_required": False,
            "trade_status": "ACCEPTED",
        }
    if state == "DEFINITIVE_REJECTION":
        outcome_class = "definitive_rejection"
        trade_status = "REJECTED"
        attempted = True
        disposition = "definitive_rejection"
        review_required = False
    elif state == "NOT_SENT_TERMINAL":
        outcome_class = "not_sent"
        trade_status = "NOT_SENT"
        attempted = False
        disposition = "not_sent_terminal"
        review_required = False
    elif state in {"CLAIMED", "REVIEW_REQUIRED"}:
        outcome_class = "unknown"
        trade_status = "REVIEW_REQUIRED"
        attempted = True
        disposition = "review_required"
        review_required = True
    else:
        outcome_class = "invocation_rejected"
        trade_status = "NOT_SENT"
        attempted = False
        disposition = "unchanged"
        review_required = False
    return {
        "ok": False,
        "status": None,
        "error": {"code": code, "message": message},
        "outcome": {
            "class": outcome_class,
            "write_attempted": attempted,
            "grant_disposition": disposition,
            "action": action,
        },
        "review_required": review_required,
        "trade_status": trade_status,
    }


def _confirm_uncertain(
    operation: dict[str, Any],
    *,
    code: str,
    message: str,
    status: int | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "status": status,
        "error": {"code": code, "message": message},
        "outcome": {
            "class": "unknown",
            "write_attempted": True,
            "grant_disposition": "review_required",
            "action": _outcome_action(operation),
        },
        "review_required": True,
        "trade_status": "REVIEW_REQUIRED",
    }


def _outcome_action(operation: dict[str, Any]) -> str:
    return str(operation["direct_tool"]).removeprefix("trade_")


def _compat_error(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": None,
        "error": {"code": "compat_response_invalid", "message": message},
        "review_required": False,
        "trade_status": "NOT_SENT",
    }


__all__ = [
    "LocalMcpTradeSettings",
    "McpTradeCompatibilityError",
    "McpTradeFacade",
    "resolve_local_mcp_trade_settings",
]
