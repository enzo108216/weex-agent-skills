#!/usr/bin/env python3
"""Legacy Partner executor envelope over verified Local MCP Partner tools."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from weex_mcp_gateway import (
    DEFAULT_FUTURES_BASE_URL,
    LocalMcpGateway,
    McpGatewayError,
)
from weex_mcp_read_compat import EXPECTED_MAPPING_VERSION, _load_default_mapping


PARTNER_BASE_URL = "https://api-spot.weex.com"
PARTNER_PRODUCTION_ENVIRONMENT = "partner_production"
PARTNER_TEST_ENVIRONMENT = "partner_test"
_HOST_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_CREDENTIAL_ERROR_CODES = frozenset(
    {
        "secret_provider_unconfigured",
        "credential_invalid",
        "credential_ref_not_found",
        "permission_denied",
        "credential_resolution_failed",
    }
)
_RECOVERY_ACTIONS = {
    "authentication": "Verify the saved API key, passphrase, timestamp, and Vault credentials.",
    "permission": "Verify Partner API permission, IP allowlist, and account status with WEEX.",
    "rate_limit": "Stop requests and wait for WEEX rate-limit weight to recover; do not retry automatically.",
    "validation": "Correct the request parameters and submit a new query.",
    "transport": "Check network connectivity and submit a new query; no automatic retry was attempted.",
    "schema": "Stop using this response and verify the current WEEX Partner response contract.",
    "redirect": "Stop using this response and verify the configured Partner origin.",
    "upstream": "Wait for the WEEX service to recover and submit a new query manually.",
    "local_policy": "Repair the Partner request or saved profile policy before retrying.",
    "profile_vault": "Set up or unlock the Application Vault and verify the saved profile.",
    "runtime_preflight": "Repair the Local MCP runtime before retrying the Partner request.",
}


class McpPartnerCompatibilityError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LocalMcpPartnerSettings:
    profile_id: str
    profile_name: str
    origin: str
    environment: str
    timeout_seconds: float
    locale: str

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise McpPartnerCompatibilityError(
                "profile_required",
                "A stable saved profile ID is required for MCP Partner tools.",
            )
        if not isinstance(self.profile_name, str) or not self.profile_name:
            raise McpPartnerCompatibilityError(
                "profile_required",
                "A saved profile name is required for the Partner response envelope.",
            )
        origin, environment = _classify_partner_origin(self.origin)
        if environment != self.environment:
            raise McpPartnerCompatibilityError(
                "expected_partner_environment_mismatch",
                "Partner origin and environment do not match.",
            )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise McpPartnerCompatibilityError(
                "invalid_api_timeout",
                "Partner timeout must be a positive finite number.",
            )
        if not isinstance(self.locale, str) or not self.locale:
            raise McpPartnerCompatibilityError(
                "invalid_locale",
                "Partner locale must be a non-empty string.",
            )
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


class McpPartnerFacade:
    def __init__(
        self,
        *,
        settings: LocalMcpPartnerSettings,
        gateway_factory: Callable[..., Any] = LocalMcpGateway,
        mapping_loader: Callable[[], dict[str, Any]] = _load_default_mapping,
    ) -> None:
        self.settings = settings
        self._gateway_factory = gateway_factory
        self._gateway: Any | None = None
        try:
            mapping = mapping_loader()
        except Exception as exc:
            raise McpPartnerCompatibilityError(
                "mapping_invalid",
                "The Trader MCP Partner mapping is unavailable or invalid.",
            ) from exc
        self._operations = _build_partner_operations(mapping)

    def _get_gateway(self) -> Any:
        if self._gateway is None:
            self._gateway = self._gateway_factory(
                profile_id=self.settings.profile_id,
                effective_spot_base_url=self.settings.origin,
                effective_futures_base_url=DEFAULT_FUTURES_BASE_URL,
                trade_enabled=False,
                timeout_seconds=self.settings.timeout_seconds,
                request_locale=self.settings.locale,
            )
        return self._gateway

    def execute(
        self,
        *,
        endpoint: Any,
        query: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        operation = self._operation(endpoint)
        try:
            arguments = _map_arguments(
                operation,
                query=dict(query),
                body=dict(body),
            )
        except McpPartnerCompatibilityError as exc:
            return self._local_failure(
                operation,
                category="validation",
                code=exc.code,
                message=exc.message,
            )
        arguments["expected_environment"] = self.settings.environment
        try:
            payload = self._get_gateway().call_typed_tool(
                str(operation["target"]["tool"]),
                arguments,
            )
        except McpGatewayError as exc:
            return self._local_failure(
                operation,
                category="runtime_preflight",
                code=exc.code,
                message=exc.message,
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            return self._local_failure(
                operation,
                category="schema",
                code="partner_response_schema_error",
                message="WEEX Partner response schema validation failed.",
            )
        if payload["ok"]:
            return self._success(operation, payload)
        return self._error(operation, payload)

    def _operation(self, endpoint: Any) -> dict[str, Any]:
        key = getattr(endpoint, "key", None)
        operation = self._operations.get(key)
        if operation is None:
            raise McpPartnerCompatibilityError(
                "unsupported_partner_endpoint",
                "The requested Partner endpoint is not in the verified mapping.",
            )
        expected = {
            "key": operation["legacy_key"],
            "method": operation["method"],
            "path": operation["path"],
            "request_transport": operation["request_transport"],
        }
        actual = {
            "key": key,
            "method": getattr(endpoint, "method", None),
            "path": getattr(endpoint, "path", None),
            "request_transport": (
                "query"
                if getattr(endpoint, "method", None) == "GET"
                else "body"
            ),
        }
        if expected != actual:
            raise McpPartnerCompatibilityError(
                "mapping_contract_mismatch",
                "The Partner endpoint catalog does not match the verified MCP mapping.",
            )
        return operation

    def _base_envelope(self, operation: dict[str, Any]) -> dict[str, Any]:
        return {
            "endpoint": operation["legacy_key"],
            "method": operation["method"],
            "path": operation["path"],
            "weight": operation.get("weight"),
            "operation_class": "read",
            "profile": {
                "resolved_profile_id": self.settings.profile_id,
                "name": self.settings.profile_name,
            },
            "api_domain": "partner",
            "environment": self.settings.environment,
            "capability_mode": "read_only_query",
        }

    def _success(
        self,
        operation: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        data = payload.get("data")
        status = _http_status(data.get("http_status")) if isinstance(data, dict) else None
        rate_limit = data.get("rate_limit") if isinstance(data, dict) else None
        retry = data.get("retry") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("endpoint") != operation["legacy_key"]
            or data.get("method") != operation["method"]
            or data.get("path") != operation["path"]
            or data.get("weight") != operation["weight"]
            or data.get("environment") != self.settings.environment
            or data.get("capability_mode") != "read_only_query"
            or status is None
            or not 200 <= status < 300
            or not isinstance(rate_limit, dict)
            or not isinstance(rate_limit.get("used"), dict)
            or not isinstance(rate_limit.get("remaining"), dict)
            or retry != {"automatic": False}
            or "data" not in data
        ):
            return self._local_failure(
                operation,
                category="schema",
                code="partner_response_schema_error",
                message="WEEX Partner response schema validation failed.",
            )
        result = self._base_envelope(operation)
        result.update(
            {
                "ok": True,
                "status": status,
                "weight": data.get("weight"),
                "rate_limit": rate_limit,
                "retry": retry,
                "data": data["data"],
            }
        )
        return result

    def _error(
        self,
        operation: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        error = payload.get("error")
        if not isinstance(error, dict):
            return self._local_failure(
                operation,
                category="schema",
                code="partner_response_schema_error",
                message="WEEX Partner response schema validation failed.",
            )
        details = error.get("details")
        details = dict(details) if isinstance(details, dict) else {}
        code = error.get("code")
        if code in _CREDENTIAL_ERROR_CODES:
            category = "profile_vault"
        elif code in {"missing_fields", "invalid_arguments"}:
            category = "validation"
        elif code in {
            "expected_partner_environment_mismatch",
            "partner_test_time_binding_required",
        }:
            category = "local_policy"
        else:
            category = details.get("category")
            if category not in _RECOVERY_ACTIONS:
                category = "upstream"
        upstream_code = details.get("upstream_code")
        legacy_code = upstream_code if isinstance(upstream_code, str) else code
        status = _http_status(details.get("http_status"))
        rate_limit = details.get("rate_limit")
        if not isinstance(rate_limit, dict):
            rate_limit = {"used": {}, "remaining": {}}
        result = self._base_envelope(operation)
        result.update(
            {
                "ok": False,
                "status": status,
                "rate_limit": rate_limit,
                "retry": {"automatic": False},
                "error": {
                    "category": category,
                    "http_status": status,
                    "code": legacy_code,
                    "message": str(error.get("message") or "Partner request failed."),
                    "details": details,
                    "recovery_action": _RECOVERY_ACTIONS[category],
                },
            }
        )
        return result

    def _local_failure(
        self,
        operation: dict[str, Any],
        *,
        category: str,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        result = self._base_envelope(operation)
        result.update(
            {
                "ok": False,
                "status": None,
                "rate_limit": {"used": {}, "remaining": {}},
                "retry": {"automatic": False},
                "error": {
                    "category": category,
                    "http_status": None,
                    "code": code,
                    "message": message,
                    "recovery_action": _RECOVERY_ACTIONS[category],
                },
            }
        )
        return result


def _build_partner_operations(mapping: Any) -> dict[str, dict[str, Any]]:
    if (
        not isinstance(mapping, dict)
        or mapping.get("mapping_version") != EXPECTED_MAPPING_VERSION
        or not isinstance(mapping.get("capabilities"), list)
    ):
        raise McpPartnerCompatibilityError(
            "mapping_invalid",
            "The Trader MCP Partner mapping version is not supported.",
        )
    operations: dict[str, dict[str, Any]] = {}
    for item in mapping["capabilities"]:
        if not isinstance(item, dict) or item.get("product") != "partner":
            continue
        key = item.get("legacy_key")
        target = item.get("target")
        if (
            not isinstance(key, str)
            or key in operations
            or not isinstance(target, dict)
            or target.get("variant") != "read"
            or not isinstance(target.get("tool"), str)
            or item.get("request_transport") not in {"query", "body"}
            or not isinstance(item.get("field_decisions"), dict)
        ):
            raise McpPartnerCompatibilityError(
                "mapping_invalid",
                "The Trader MCP Partner mapping is incomplete or duplicated.",
            )
        if isinstance(item.get("weight"), bool) or not isinstance(item.get("weight"), int):
            raise McpPartnerCompatibilityError(
                "mapping_invalid",
                "The Trader MCP Partner mapping weight is invalid.",
            )
        copied = dict(item)
        operations[key] = copied
    if len(operations) != 7:
        raise McpPartnerCompatibilityError(
            "mapping_invalid",
            "The Trader MCP Partner mapping must contain seven operations.",
        )
    return operations


def _map_arguments(
    operation: dict[str, Any],
    *,
    query: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    transport = operation["request_transport"]
    if transport == "query" and body:
        raise McpPartnerCompatibilityError(
            "request_transport_mismatch",
            "The Partner endpoint accepts query fields only.",
        )
    if transport == "body" and query:
        raise McpPartnerCompatibilityError(
            "request_transport_mismatch",
            "The Partner endpoint accepts body fields only.",
        )
    supplied = query if transport == "query" else body
    allowed = set(operation["catalog_request_fields"])
    if set(supplied) - allowed:
        raise McpPartnerCompatibilityError(
            "unsupported_fields",
            "The Partner request contains fields outside the verified mapping.",
        )
    arguments: dict[str, Any] = {}
    decisions = operation["field_decisions"]
    for source, value in supplied.items():
        decision = decisions[source]
        target = decision["target"]
        transform = decision.get("value_transform")
        if transform == "comma_or_array_unique_string_list_v1":
            arguments[target] = _string_list(value, unique=True)
        elif transform == "comma_or_array_string_list_v1":
            arguments[target] = _string_list(value, unique=False)
        else:
            arguments[target] = _encode_value(value)
    return arguments


def _string_list(value: Any, *, unique: bool) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise McpPartnerCompatibilityError(
            "invalid_arguments",
            "Partner userIds must be a comma-separated string or an array.",
        )
    if not items or (unique and len(set(items)) != len(items)):
        raise McpPartnerCompatibilityError(
            "invalid_arguments",
            "Partner userIds must contain valid unique identifiers.",
        )
    return items


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise McpPartnerCompatibilityError(
            "invalid_arguments",
            "Boolean Partner query values are not supported.",
        )
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise McpPartnerCompatibilityError(
                "invalid_arguments",
                "Non-finite Partner query values are not supported.",
            )
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    if isinstance(value, list):
        return [str(item) for item in value]
    return value


def _classify_partner_origin(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise McpPartnerCompatibilityError(
            "partner_origin_invalid",
            "Partner origin is invalid.",
        ) from exc
    hostname = parsed.hostname or ""
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise McpPartnerCompatibilityError(
            "partner_origin_invalid",
            "Partner origin is invalid.",
        ) from exc
    canonical = f"https://{hostname}"
    labels = hostname.split(".")
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or parsed.scheme != "https"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value != canonical
        or not labels
        or any(not _HOST_LABEL_PATTERN.fullmatch(label) for label in labels)
    ):
        raise McpPartnerCompatibilityError(
            "partner_origin_invalid",
            "Partner origin is invalid.",
        )
    if canonical == PARTNER_BASE_URL:
        return canonical, PARTNER_PRODUCTION_ENVIRONMENT
    if hostname != "weex.tech" and hostname.endswith(".weex.tech"):
        return canonical, PARTNER_TEST_ENVIRONMENT
    raise McpPartnerCompatibilityError(
        "partner_origin_invalid",
        "Partner origin is invalid.",
    )


def _http_status(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        return None
    return value


__all__ = [
    "LocalMcpPartnerSettings",
    "McpPartnerCompatibilityError",
    "McpPartnerFacade",
]
