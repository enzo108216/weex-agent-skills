#!/usr/bin/env python3
"""Legacy Contract/Spot Trade compatibility facade over Local MCP tools."""

from __future__ import annotations

import json
import math
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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


_PRODUCTS = frozenset({"contract", "spot"})
_OUTCOME_CLASSES = frozenset(
    {"not_sent", "definitive_rejection", "accepted", "unknown", "partial"}
)
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
    )


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
        client_id_factory: Callable[[], str] = _default_client_id,
        delegated_authorizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self._gateway_factory = gateway_factory
        self._request_describer = request_describer
        self._gateway: Any | None = None
        self._client_id_factory = client_id_factory
        self._delegated_authorizer = delegated_authorizer
        try:
            mapping = mapping_loader()
        except Exception as exc:
            raise McpTradeCompatibilityError(
                "mapping_invalid",
                "The Trader MCP capability mapping is unavailable or invalid.",
            ) from exc
        self._operations = _build_trade_operations(mapping)

    def _get_gateway(self) -> Any:
        if self._gateway is None:
            self._gateway = self._gateway_factory(
                profile_id=self.settings.profile_id,
                effective_spot_base_url=self.settings.spot_base_url,
                effective_futures_base_url=self.settings.futures_base_url,
                trade_enabled=True,
                timeout_seconds=self.settings.timeout_seconds,
                request_locale=self.settings.locale,
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
        preview_tool = str(operation["target"]["tool"])
        try:
            wrapped = self._get_gateway().call_typed_tool(preview_tool, arguments)
        except McpGatewayError as exc:
            return _gateway_error(exc)
        unwrapped = _unwrap_gateway_result(wrapped)
        if unwrapped is None:
            return _compat_error("The WEEX MCP Trade preview wrapper is invalid.")
        payload, transport_meta = unwrapped
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return _project_prewrite_error(
                payload,
                transport_meta=transport_meta,
            )
        preview_id = payload.get("preview_id")
        expires_at = payload.get("expires_at")
        if (
            not isinstance(preview_id, str)
            or not preview_id
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= 0
            or not isinstance(payload.get("summary"), dict)
            or not isinstance(payload.get("risk"), dict)
        ):
            return _compat_error("The WEEX MCP Trade preview result is invalid.")
        record = {
            "version": 1,
            "product": product,
            "legacy_key": operation["legacy_key"],
            "preview_tool": preview_tool,
            "confirm_tool": operation["target"]["confirm_tool"],
            "arguments": arguments,
            "preview_id": preview_id,
            "expires_at": expires_at,
            "profile_id": self.settings.profile_id,
            "spot_base_url": self.settings.spot_base_url,
            "futures_base_url": self.settings.futures_base_url,
        }
        return {
            "ok": True,
            "status": _http_status(transport_meta),
            "preview": record,
            "summary": dict(payload["summary"]),
            "risk": dict(payload["risk"]),
            "expires_at": expires_at,
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
                name=str(operation["target"]["tool"]),
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
        return self._confirm(preview, delegated_ref=None)

    def confirm_delegated(
        self,
        preview: Mapping[str, Any],
        *,
        delegated_ref: str,
    ) -> dict[str, Any]:
        if not isinstance(delegated_ref, str) or not delegated_ref:
            raise McpTradeCompatibilityError(
                "execution_authorization_invalid",
                "Delegated authorization ref must be a non-empty string.",
            )
        if self._delegated_authorizer is None:
            raise McpTradeCompatibilityError(
                "execution_authorizer_unconfigured",
                "Delegated execution authorizer is not configured.",
            )
        return self._confirm(preview, delegated_ref=delegated_ref)

    def _confirm(
        self,
        preview: Mapping[str, Any],
        *,
        delegated_ref: str | None,
    ) -> dict[str, Any]:
        operation, arguments = self._validate_preview_record(preview)
        confirm_arguments = {
            **arguments,
            "preview_id": preview["preview_id"],
        }
        if delegated_ref is None:
            confirm_arguments["confirmation"] = {"accepted": True}
        else:
            confirm_arguments["delegated_authorization"] = {"ref": delegated_ref}
        try:
            wrapped = self._get_gateway().call_typed_tool(
                str(operation["target"]["confirm_tool"]),
                confirm_arguments,
            )
        except McpGatewayError as exc:
            return _confirm_uncertain(
                operation,
                code=exc.code,
                message=exc.message,
            )
        unwrapped = _unwrap_gateway_result(wrapped)
        if unwrapped is None:
            return _confirm_uncertain(
                operation,
                code="compat_response_invalid",
                message="The WEEX MCP Trade confirmation wrapper is invalid.",
            )
        payload, transport_meta = unwrapped
        return _project_confirm_result(
            operation,
            payload,
            transport_meta=transport_meta,
        )

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
        return self.confirm(preview["preview"])

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

    def _validate_preview_record(
        self,
        preview: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(preview, Mapping) or preview.get("version") != 1:
            raise McpTradeCompatibilityError(
                "preview_invalid",
                "The saved MCP Trade preview is invalid.",
            )
        bindings = {
            "profile_id": self.settings.profile_id,
            "spot_base_url": self.settings.spot_base_url,
            "futures_base_url": self.settings.futures_base_url,
        }
        if any(preview.get(key) != value for key, value in bindings.items()):
            raise McpTradeCompatibilityError(
                "preview_binding_mismatch",
                "The saved MCP Trade preview does not match the current profile or origins.",
            )
        key = (preview.get("product"), preview.get("legacy_key"))
        operation = self._operations.get(key)
        if (
            operation is None
            or preview.get("preview_tool") != operation["target"]["tool"]
            or preview.get("confirm_tool") != operation["target"]["confirm_tool"]
            or not isinstance(preview.get("preview_id"), str)
            or not preview["preview_id"]
            or not isinstance(preview.get("arguments"), dict)
        ):
            raise McpTradeCompatibilityError(
                "preview_invalid",
                "The saved MCP Trade preview is invalid.",
            )
        return operation, dict(preview["arguments"])


def _build_trade_operations(mapping: Any) -> dict[tuple[str, str], dict[str, Any]]:
    if (
        not isinstance(mapping, dict)
        or mapping.get("mapping_version") != EXPECTED_MAPPING_VERSION
        or not isinstance(mapping.get("capabilities"), list)
    ):
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The Trader MCP capability mapping version is not supported.",
        )
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
        operations[key] = item
    if sum(product == "contract" for product, _ in operations) != 16 or sum(
        product == "spot" for product, _ in operations
    ) != 5:
        raise McpTradeCompatibilityError(
            "mapping_invalid",
            "The Trader MCP Trade mapping does not cover the 21 legacy writes.",
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

    preview_tool = operation["target"]["tool"]
    if preview_tool == "trade_preview_order" and not arguments.get("client_order_id"):
        arguments["client_order_id"] = _new_client_id(client_id_factory)
    if (
        preview_tool == "trade_preview_cancel_scope"
        and arguments.get("market") == "futures"
    ):
        if arguments.get("symbol") not in (None, ""):
            arguments["scope"] = "symbol"
            arguments.pop("acknowledge_all_symbols", None)
        else:
            arguments["scope"] = "all"
            arguments["acknowledge_all_symbols"] = True
    if preview_tool == "trade_preview_close_positions":
        has_position = arguments.get("position_id") not in (None, "")
        has_symbol = arguments.get("symbol") not in (None, "")
        if has_position:
            arguments["scope"] = "position"
        elif has_symbol:
            arguments["scope"] = "symbol"
        else:
            arguments["scope"] = "all"
            arguments["acknowledge_all_positions"] = True
    if preview_tool == "trade_preview_tp_sl" and arguments.get("operation") == "place":
        quantity = arguments.get("quantity")
        if quantity in (None, "", "0"):
            arguments["quantity"] = "0"
            arguments["acknowledge_full_position"] = True
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
        aliases = {"CROSS": "cross", "CROSSED": "cross", "ISOLATED": "isolated"}
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


def _project_prewrite_error(
    payload: Any,
    *,
    transport_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return {
            "ok": False,
            "status": _http_status(transport_meta),
            "error": dict(payload["error"]),
        }
    return _compat_error("The WEEX MCP Trade preview error envelope is invalid.")


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
    raw: Any = None
    result = payload.get("result")
    if isinstance(result, dict) and "raw" in result:
        raw = result["raw"]
    if payload["ok"] and outcome_class == "accepted":
        if not _matches_container(raw, operation["response"]["legacy_container"]):
            return _confirm_uncertain(
                operation,
                code="compat_response_invalid",
                message="The WEEX MCP Trade result did not preserve the legacy payload.",
                status=status,
            )
        return {
            "ok": True,
            "status": status,
            "data": raw,
            "outcome": outcome,
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
            else ("REJECTED" if outcome_class == "definitive_rejection" else "NOT_SENT")
        ),
        **({"data": raw} if raw is not None else {}),
    }


def _matches_container(value: Any, container: str) -> bool:
    if container == "array":
        return isinstance(value, list)
    if container == "object":
        return isinstance(value, dict)
    return isinstance(value, (dict, list))


def _gateway_error(exc: McpGatewayError) -> dict[str, Any]:
    return {
        "ok": False,
        "status": None,
        "error": {"code": exc.code, "message": exc.message},
        "review_required": False,
        "trade_status": "NOT_SENT",
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
            "action": _outcome_action(operation),
        },
        "review_required": True,
        "trade_status": "REVIEW_REQUIRED",
    }


def _outcome_action(operation: dict[str, Any]) -> str:
    confirm_tool = str(operation["target"]["confirm_tool"])
    if confirm_tool == "trade_confirm_leverage":
        return "update_leverage"
    if confirm_tool == "trade_confirm_margin_mode":
        return "change_margin_mode"
    return confirm_tool.removeprefix("trade_confirm_")


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
