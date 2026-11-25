#!/usr/bin/env python3
"""Legacy Contract/Spot read facade backed by verified Local MCP tools."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from weex_mcp_gateway import (
    DEFAULT_FUTURES_BASE_URL,
    DEFAULT_SPOT_BASE_URL,
    LocalMcpGateway,
    McpGatewayError,
    describe_local_mcp_request,
)
from weex_url_policy import BaseUrlPolicyError, validate_weex_base_url


MAPPING_PATH = Path(__file__).resolve().parent.parent / "references" / "mcp-capability-mapping.json"
EXPECTED_MAPPING_VERSION = "trader-mcp-capability-v2"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_LOCALE = "en-US"
_PRODUCTS = frozenset({"contract", "spot"})
_LEGACY_CONTAINERS = frozenset({"array", "object", "conditional_object_or_array"})


class McpReadCompatibilityError(RuntimeError):
    """Stable failure raised before a typed read tool can be called."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LocalMcpReadSettings:
    profile_id: str | None
    spot_base_url: str
    futures_base_url: str
    timeout_seconds: float
    locale: str

    def __post_init__(self) -> None:
        if self.profile_id is not None and (
            not isinstance(self.profile_id, str) or not self.profile_id
        ):
            raise McpReadCompatibilityError(
                "profile_invalid",
                "The selected saved profile does not expose a stable profile ID.",
            )
        try:
            spot_base_url = validate_weex_base_url(
                self.spot_base_url,
                label="spot base URL",
            )
            futures_base_url = validate_weex_base_url(
                self.futures_base_url,
                label="contract base URL",
            )
        except BaseUrlPolicyError as exc:
            raise McpReadCompatibilityError(
                "origin_invalid",
                "The Local MCP read origin does not satisfy the Trader URL policy.",
            ) from exc
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise McpReadCompatibilityError(
                "timeout_invalid",
                "The Local MCP read timeout must be a positive finite number.",
            )
        if not isinstance(self.locale, str) or not self.locale:
            raise McpReadCompatibilityError(
                "locale_invalid",
                "The Local MCP read locale must be a non-empty string.",
            )
        object.__setattr__(self, "spot_base_url", spot_base_url)
        object.__setattr__(self, "futures_base_url", futures_base_url)
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


def resolve_local_mcp_read_settings(
    *,
    profile: Any | None,
    explicit_spot_base_url: str | None = None,
    explicit_futures_base_url: str | None = None,
    explicit_timeout: float | None = None,
    active_product: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> LocalMcpReadSettings:
    """Resolve the legacy CLI/profile/environment/default precedence once."""

    source = os.environ if environ is None else environ
    if active_product not in {None, "contract", "spot"}:
        raise McpReadCompatibilityError(
            "unsupported_product",
            "The requested legacy read product is not supported.",
        )
    profile_id: str | None = None
    profile_spot = ""
    profile_futures = ""
    if profile is not None:
        profile_id = getattr(profile, "profile_id", None)
        if not isinstance(profile_id, str) or not profile_id:
            raise McpReadCompatibilityError(
                "profile_invalid",
                "The selected saved profile does not expose a stable profile ID.",
            )
        profile_spot = str(getattr(profile, "spot_base_url", "") or "")
        profile_futures = str(getattr(profile, "contract_base_url", "") or "")

    spot_value = DEFAULT_SPOT_BASE_URL if active_product == "contract" else (
        (explicit_spot_base_url or "")
        or profile_spot
        or source.get("WEEX_SPOT_API_BASE", "")
        or source.get("WEEX_API_BASE", "")
        or DEFAULT_SPOT_BASE_URL
    )
    futures_value = DEFAULT_FUTURES_BASE_URL if active_product == "spot" else (
        (explicit_futures_base_url or "")
        or profile_futures
        or source.get("WEEX_CONTRACT_API_BASE", "")
        or source.get("WEEX_API_BASE", "")
        or DEFAULT_FUTURES_BASE_URL
    )
    try:
        spot_base_url = validate_weex_base_url(spot_value, label="spot base URL")
        futures_base_url = validate_weex_base_url(futures_value, label="contract base URL")
    except BaseUrlPolicyError as exc:
        raise SystemExit(str(exc)) from exc

    timeout_value: Any = (
        explicit_timeout
        if explicit_timeout is not None
        else source.get("WEEX_API_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    )
    try:
        timeout_seconds = float(timeout_value)
    except (TypeError, ValueError) as exc:
        raise SystemExit("WEEX API timeout must be a positive finite number.") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise SystemExit("WEEX API timeout must be a positive finite number.")

    locale = source.get("WEEX_LOCALE") or DEFAULT_LOCALE
    if not isinstance(locale, str) or not locale:
        locale = DEFAULT_LOCALE
    return LocalMcpReadSettings(
        profile_id=profile_id,
        spot_base_url=spot_base_url,
        futures_base_url=futures_base_url,
        timeout_seconds=timeout_seconds,
        locale=locale,
    )


def _load_default_mapping() -> dict[str, Any]:
    try:
        value = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpReadCompatibilityError(
            "mapping_invalid",
            "The Trader MCP capability mapping is unavailable or invalid.",
        ) from exc
    if not isinstance(value, dict):
        raise McpReadCompatibilityError(
            "mapping_invalid",
            "The Trader MCP capability mapping is unavailable or invalid.",
        )
    return value


class McpReadFacade:
    """Map allowlisted legacy read endpoints to typed Local MCP tools."""

    def __init__(
        self,
        *,
        settings: LocalMcpReadSettings,
        gateway_factory: Callable[..., Any] = LocalMcpGateway,
        request_describer: Callable[..., dict[str, Any]] = describe_local_mcp_request,
        mapping_loader: Callable[[], dict[str, Any]] = _load_default_mapping,
    ) -> None:
        self.settings = settings
        self._gateway_factory = gateway_factory
        self._request_describer = request_describer
        self._gateway: Any | None = None
        try:
            mapping = mapping_loader()
        except McpReadCompatibilityError:
            raise
        except Exception as exc:
            raise McpReadCompatibilityError(
                "mapping_invalid",
                "The Trader MCP capability mapping is unavailable or invalid.",
            ) from exc
        self._operations = _build_read_operations(mapping)

    def _get_gateway(self) -> Any:
        if self._gateway is None:
            self._gateway = self._gateway_factory(
                profile_id=self.settings.profile_id,
                effective_spot_base_url=self.settings.spot_base_url,
                effective_futures_base_url=self.settings.futures_base_url,
                trade_enabled=False,
                timeout_seconds=self.settings.timeout_seconds,
                request_locale=self.settings.locale,
            )
        return self._gateway

    def execute(
        self,
        *,
        product: str,
        endpoint: Any,
        query: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        if product not in _PRODUCTS:
            raise McpReadCompatibilityError(
                "unsupported_product",
                "The requested legacy read product is not supported.",
            )
        if not isinstance(query, Mapping) or not isinstance(body, Mapping):
            raise McpReadCompatibilityError(
                "invalid_arguments",
                "Legacy read query and body values must be objects.",
            )
        legacy_key = getattr(endpoint, "key", None)
        operation = self._operations.get((product, legacy_key))
        if operation is None:
            raise McpReadCompatibilityError(
                "unsupported_read_endpoint",
                "The requested endpoint is not an allowlisted MCP read operation.",
            )
        _validate_endpoint_contract(endpoint, operation)
        arguments = _map_arguments(operation, query=dict(query), body=dict(body))
        try:
            payload = self._get_gateway().call_typed_tool(
                str(operation["target"]["tool"]),
                arguments,
            )
        except McpGatewayError as exc:
            return {
                "ok": False,
                "status": None,
                "error": {"code": exc.code, "message": exc.message},
            }
        return _serialize_legacy_response(operation, payload)

    def describe(
        self,
        *,
        product: str,
        endpoint: Any,
        query: Mapping[str, Any],
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        if product not in _PRODUCTS:
            raise McpReadCompatibilityError(
                "unsupported_product",
                "The requested legacy read product is not supported.",
            )
        if not isinstance(query, Mapping) or not isinstance(body, Mapping):
            raise McpReadCompatibilityError(
                "invalid_arguments",
                "Legacy read query and body values must be objects.",
            )
        legacy_key = getattr(endpoint, "key", None)
        operation = self._operations.get((product, legacy_key))
        if operation is None:
            raise McpReadCompatibilityError(
                "unsupported_read_endpoint",
                "The requested endpoint is not an allowlisted MCP read operation.",
            )
        _validate_endpoint_contract(endpoint, operation)
        arguments = _map_arguments(operation, query=dict(query), body=dict(body))
        try:
            description = self._request_describer(
                name=str(operation["target"]["tool"]),
                arguments=arguments,
                effective_spot_base_url=self.settings.spot_base_url,
                effective_futures_base_url=self.settings.futures_base_url,
                trade_enabled=False,
                timeout_seconds=self.settings.timeout_seconds,
                request_locale=self.settings.locale,
            )
        except McpGatewayError as exc:
            raise McpReadCompatibilityError(exc.code, exc.message) from exc
        if _request_description_contract_error(
            operation,
            description,
            spot_base_url=self.settings.spot_base_url,
            futures_base_url=self.settings.futures_base_url,
        ):
            raise McpReadCompatibilityError(
                "request_description_mismatch",
                "The MCP request description does not match the verified legacy mapping.",
            )
        return description


def _request_description_contract_error(
    operation: Mapping[str, Any],
    description: Any,
    *,
    spot_base_url: str,
    futures_base_url: str,
) -> str | None:
    if not isinstance(description, dict):
        return "shape"
    url = description.get("url")
    if (
        description.get("method") != operation.get("method")
        or not isinstance(url, str)
        or not isinstance(description.get("headers"), dict)
        or not isinstance(description.get("query"), dict)
        or not isinstance(description.get("body"), dict)
    ):
        return "shape"
    parsed = urlsplit(url)
    expected_origin = (
        spot_base_url.rstrip("/")
        if operation.get("product") == "spot"
        else futures_base_url.rstrip("/")
    )
    if (
        f"{parsed.scheme}://{parsed.netloc}" != expected_origin
        or parsed.path != operation.get("path")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return "route"
    return None


def _build_read_operations(mapping: Any) -> dict[tuple[str, str], dict[str, Any]]:
    if (
        not isinstance(mapping, dict)
        or mapping.get("mapping_version") != EXPECTED_MAPPING_VERSION
        or not isinstance(mapping.get("capabilities"), list)
    ):
        raise McpReadCompatibilityError(
            "mapping_invalid",
            "The Trader MCP capability mapping version is not supported.",
        )
    request_policy = mapping.get("shared_policies", {}).get("request_fields", {})
    if request_policy.get("scalar_encoding") != "finite_json_number_to_canonical_string_v1":
        raise McpReadCompatibilityError(
            "mapping_invalid",
            "The Trader MCP scalar encoding policy is not supported.",
        )

    operations: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in mapping["capabilities"]:
        if not isinstance(raw, dict):
            raise McpReadCompatibilityError("mapping_invalid", "The Trader MCP mapping contains an invalid entry.")
        target = raw.get("target")
        if not isinstance(target, dict) or target.get("variant") != "read":
            continue
        product = raw.get("product")
        if product not in _PRODUCTS:
            continue
        legacy_key = raw.get("legacy_key")
        key = (product, legacy_key)
        if (
            not isinstance(legacy_key, str)
            or not legacy_key
            or key in operations
        ):
            raise McpReadCompatibilityError("mapping_invalid", "The Trader MCP read mapping is not unique.")
        fields = raw.get("catalog_request_fields")
        decisions = raw.get("field_decisions")
        response = raw.get("response")
        if (
            raw.get("request_transport") not in {"query", "body"}
            or not isinstance(fields, list)
            or not all(isinstance(field, str) and field for field in fields)
            or not isinstance(decisions, dict)
            or set(decisions) != set(fields)
            or not isinstance(target.get("tool"), str)
            or not isinstance(target.get("fixed_arguments"), dict)
            or not isinstance(response, dict)
            or response.get("legacy_container") not in _LEGACY_CONTAINERS
        ):
            raise McpReadCompatibilityError("mapping_invalid", "The Trader MCP read mapping is incomplete.")
        for decision in decisions.values():
            if (
                not isinstance(decision, dict)
                or decision.get("decision") != "map"
                or not isinstance(decision.get("target"), str)
                or not decision["target"]
            ):
                raise McpReadCompatibilityError("mapping_invalid", "The Trader MCP read field mapping is invalid.")
        operations[key] = raw
    return operations


def _validate_endpoint_contract(endpoint: Any, operation: dict[str, Any]) -> None:
    expected = {
        "key": operation.get("legacy_key"),
        "method": operation.get("method"),
        "path": operation.get("path"),
        "request_transport": operation.get("request_transport"),
    }
    actual = {
        "key": getattr(endpoint, "key", None),
        "method": getattr(endpoint, "method", None),
        "path": getattr(endpoint, "path", None),
        "request_transport": getattr(endpoint, "request_transport", None),
    }
    if actual != expected:
        raise McpReadCompatibilityError(
            "mapping_contract_mismatch",
            "The legacy endpoint catalog does not match the verified MCP mapping.",
        )


def _map_arguments(
    operation: dict[str, Any],
    *,
    query: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    transport = operation["request_transport"]
    if transport == "query" and body:
        raise McpReadCompatibilityError(
            "request_transport_mismatch",
            "The legacy read endpoint accepts query fields only.",
        )
    if transport == "body" and query:
        raise McpReadCompatibilityError(
            "request_transport_mismatch",
            "The legacy read endpoint accepts body fields only.",
        )
    supplied = query if transport == "query" else body
    allowed = set(operation["catalog_request_fields"])
    unknown = sorted(set(supplied) - allowed)
    if unknown:
        raise McpReadCompatibilityError(
            "unsupported_fields",
            "The legacy read request contains fields not present in the verified mapping.",
        )
    arguments = dict(operation["target"]["fixed_arguments"])
    decisions = operation["field_decisions"]
    for source, value in supplied.items():
        target = decisions[source]["target"]
        if target in arguments:
            raise McpReadCompatibilityError(
                "mapping_invalid",
                "The Trader MCP field mapping conflicts with fixed arguments.",
            )
        arguments[target] = _encode_legacy_value(value)
    return arguments


def _encode_legacy_value(value: Any) -> Any:
    if isinstance(value, bool):
        raise McpReadCompatibilityError(
            "invalid_arguments",
            "Boolean values are not valid legacy read scalars.",
        )
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise McpReadCompatibilityError(
                "invalid_arguments",
                "Non-finite values are not valid legacy read scalars.",
            )
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    if isinstance(value, list):
        return [_encode_legacy_collection_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode_legacy_collection_value(item) for key, item in value.items()}
    return value


def _encode_legacy_collection_value(value: Any) -> Any:
    if isinstance(value, bool) or (isinstance(value, float) and not math.isfinite(value)):
        raise McpReadCompatibilityError(
            "invalid_arguments",
            "The legacy read request contains an invalid structured value.",
        )
    return value


def _serialize_legacy_response(
    operation: dict[str, Any],
    payload: Any,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return _compat_error("The WEEX MCP read result envelope is invalid.")
    if payload["ok"]:
        data = payload.get("data")
        if not isinstance(data, dict) or "raw" not in data:
            return _compat_error("The WEEX MCP read result did not preserve the legacy payload.")
        raw = data["raw"]
        if not _matches_container(raw, operation["response"]["legacy_container"]):
            return _compat_error("The WEEX MCP read result does not match the legacy container.")
        return {
            "ok": True,
            "status": _http_status(data) or _http_status(data.get("transport")),
            "data": raw,
        }

    error = payload.get("error")
    if not isinstance(error, dict):
        return _compat_error("The WEEX MCP read error envelope is invalid.")
    details = error.get("details")
    status = _http_status(details)
    if isinstance(details, dict) and "upstream_payload" in details:
        legacy_error = details["upstream_payload"]
    else:
        legacy_error = error
    return {"ok": False, "status": status, "error": legacy_error}


def _compat_error(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": None,
        "error": {"code": "compat_response_invalid", "message": message},
    }


def _matches_container(value: Any, container: str) -> bool:
    if container == "array":
        return isinstance(value, list)
    if container == "object":
        return isinstance(value, dict)
    return isinstance(value, (dict, list))


def _http_status(value: Any) -> int | None:
    if not isinstance(value, Mapping):
        return None
    status = value.get("http_status")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return None
    return status


__all__ = [
    "LocalMcpReadSettings",
    "McpReadCompatibilityError",
    "McpReadFacade",
    "resolve_local_mcp_read_settings",
]
