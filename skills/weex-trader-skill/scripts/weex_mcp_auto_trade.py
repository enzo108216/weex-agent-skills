#!/usr/bin/env python3
"""Reservation-bound automated mutations through the verified Local MCP runtime."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


from weex_mcp_gateway import LocalMcpGateway
from weex_mcp_read_compat import _unwrap_gateway_result
from weex_mcp_trade_compat import McpTradeFacade, _map_trade_arguments


_AUTO_OPERATIONS = frozenset({
    "spot.order.place_order",
    "spot.order.bulk_order",
    "transaction.place_order",
    "transaction.place_orders_batch",
    "transaction.place_pending_order",
    "transaction.place_tp_sl_order",
})


class AutoTradeDelegationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AutoTradeDelegation:
    """Prepare and submit one reserved group using a durable SQLite grant."""

    def __init__(
        self,
        *,
        state: Any,
        authorization_id: str,
        strategy_id: str,
        submission_group_id: str,
        request_fingerprint: str,
        usage_ids: list[str],
        client_order_ids: list[str],
        expected_orders: list[dict[str, Any]],
        operation_key: str,
        now_epoch: Callable[[], float] | None = None,
    ) -> None:
        if operation_key not in _AUTO_OPERATIONS:
            raise ValueError("operation_key is not supported for delegated execution")
        if not all(
            isinstance(value, str) and value
            for value in (
                authorization_id,
                strategy_id,
                submission_group_id,
                request_fingerprint,
            )
        ):
            raise ValueError("delegated authorization bindings must be non-empty strings")
        if not usage_ids or not client_order_ids or len(usage_ids) != len(client_order_ids):
            raise ValueError("delegated authorization identities are incomplete")
        if (
            not isinstance(expected_orders, list)
            or len(expected_orders) != len(usage_ids)
            or any(not isinstance(order, dict) for order in expected_orders)
        ):
            raise ValueError("delegated authorization orders are incomplete")
        if not hmac.compare_digest(
            _request_fingerprint(operation_key, expected_orders),
            request_fingerprint,
        ):
            raise ValueError(
                "delegated authorization orders do not match the reserved request fingerprint"
            )
        self.state = state
        self.authorization_id = authorization_id
        self.strategy_id = strategy_id
        self.submission_group_id = submission_group_id
        self.request_fingerprint = request_fingerprint
        self.usage_ids = list(usage_ids)
        self.client_order_ids = list(client_order_ids)
        self._expected_orders_digest = _orders_digest(expected_orders)
        self.operation_key = operation_key
        self._now_epoch = now_epoch or time.time

    def execute(
        self,
        *,
        settings: Any,
        endpoint: Any,
        query: dict[str, Any],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if getattr(endpoint, "key", None) != self.operation_key or query:
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution operation does not match the reservation.",
            )
        if not hmac.compare_digest(
            _orders_digest(_orders_from_request_body(self.operation_key, body)),
            self._expected_orders_digest,
        ):
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution request does not match the reservation.",
            )
        product = "spot" if self.operation_key.startswith("spot.") else "contract"
        operation = McpTradeFacade(settings=settings)._operation(product=product, endpoint=endpoint)
        arguments = _map_trade_arguments(
            operation, query=query, body=body, client_id_factory=_missing_client_id,
        )
        expected_market = "spot" if product == "spot" else "futures"
        if (
            arguments.get("market") != expected_market
            or arguments.get("trading_mode") != "live"
            or _client_order_ids(arguments) != self.client_order_ids
            or (
                operation["direct_tool"] == "trade_place_tp_sl"
                and arguments.get("quantity_mode") != "exact"
            )
        ):
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution market, mode, or client identities do not match the reservation.",
            )
        snapshot = self.state.read_delegated_submission_group(
            authorization_id=self.authorization_id,
            submission_group_id=self.submission_group_id,
            request_fingerprint=self.request_fingerprint,
            usage_ids=self.usage_ids,
            client_order_ids=self.client_order_ids,
            now=datetime.fromtimestamp(self._now_epoch(), tz=UTC),
        )
        if (
            snapshot.get("strategy_id") != self.strategy_id
            or snapshot.get("profile_id") != settings.profile_id
            or snapshot.get("usage_ids") != self.usage_ids
            or snapshot.get("client_order_ids") != self.client_order_ids
        ):
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution state binding did not match.",
            )
        gateway = LocalMcpGateway(
            profile_id=settings.profile_id,
            effective_spot_base_url=settings.spot_base_url,
            effective_futures_base_url=settings.futures_base_url,
            trade_enabled=True,
            timeout_seconds=settings.timeout_seconds,
            request_locale=settings.locale,
            grant_consumer=self.state,
        )
        tool = operation["direct_tool"]
        prepared = gateway.prepare_mutation(tool, deepcopy(arguments))
        canonical = prepared.get("canonical_arguments")
        if (
            prepared.get("tool_name") != tool
            or prepared.get("market") != expected_market
            or prepared.get("trading_mode") != "live"
            or not isinstance(canonical, dict)
            or canonical != arguments
        ):
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "PreparedMutation does not match the reserved order.",
            )
        grant = gateway.build_execution_grant(
            prepared,
            grant_id=f"auto_{self.submission_group_id}",
            issued_at=int(self._now_epoch() * 1000),
        )
        issued = self.state.issue_execution_grant(
            submission_group_id=self.submission_group_id,
            authorization_id=self.authorization_id,
            strategy_id=self.strategy_id,
            profile_id=settings.profile_id,
            request_fingerprint=self.request_fingerprint,
            client_order_ids=self.client_order_ids,
            grant=grant,
            prepared=prepared,
        )
        unwrapped = _unwrap_gateway_result(
            gateway.call_typed_tool(tool, canonical, execution_grant=issued),
        )
        if unwrapped is None:
            raise AutoTradeDelegationError(
                "execution_outcome_unverified", "MCP did not return a verified mutation result.",
            )
        payload, _transport_meta = unwrapped
        if not isinstance(payload, dict) or not self.state.execution_grant_outcome_matches(
            submission_group_id=self.submission_group_id,
            grant=issued,
            outcome=payload.get("outcome"),
        ):
            raise AutoTradeDelegationError(
                "execution_outcome_unverified", "MCP outcome does not match the durable execution grant.",
            )
        return payload


def _missing_client_id() -> str:
    raise AutoTradeDelegationError(
        "execution_authorization_invalid", "Reserved execution must already have a client ID.",
    )


def _client_order_ids(arguments: Mapping[str, Any]) -> list[str]:
    orders = arguments.get("orders")
    if isinstance(orders, list):
        values = [
            str(item.get("client_order_id") or "")
            for item in orders
            if isinstance(item, Mapping)
        ]
    else:
        value = arguments.get("client_order_id") or arguments.get("client_algo_id")
        values = [str(value or "")]
    if len(values) == 0 or any(not value for value in values):
        raise AutoTradeDelegationError(
            "execution_authorization_invalid",
            "Delegated execution preview is missing client-order identities.",
        )
    return values


def _request_fingerprint(
    operation_key: str,
    orders: list[dict[str, Any]],
) -> str:
    caller_id_fields = {"newClientOrderId", "clientAlgoId"}
    normalized_orders = [
        {key: value for key, value in order.items() if key not in caller_id_fields}
        for order in orders
    ]
    encoded = json.dumps(
        {"operation_key": operation_key, "orders": normalized_orders},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _orders_digest(orders: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        [_canonical_order(order) for order in orders],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_order(order: dict[str, Any]) -> dict[str, Any]:
    caller_ids = {"newClientOrderId", "clientAlgoId"}
    uppercase_fields = {
        "symbol",
        "side",
        "positionSide",
        "type",
        "timeInForce",
        "planType",
        "triggerPriceType",
        "TpWorkingType",
        "SlWorkingType",
    }
    numeric_fields = {
        "quantity",
        "price",
        "triggerPrice",
        "executePrice",
        "tpTriggerPrice",
        "slTriggerPrice",
        "presetTakeProfitPrice",
        "presetStopLossPrice",
    }
    result: dict[str, Any] = {}
    for key, value in order.items():
        if key in caller_ids or value in (None, ""):
            continue
        if key in uppercase_fields:
            result[key] = str(value).upper()
        elif key in numeric_fields:
            try:
                decimal = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise AutoTradeDelegationError(
                    "execution_authorization_invalid",
                    "Delegated execution order contains an invalid numeric value.",
                ) from exc
            if not decimal.is_finite():
                raise AutoTradeDelegationError(
                    "execution_authorization_invalid",
                    "Delegated execution order contains a non-finite value.",
                )
            text = format(decimal, "f")
            result[key] = text.rstrip("0").rstrip(".") if "." in text else text
        else:
            result[key] = value
    return result


def _orders_from_request_body(
    operation_key: str,
    request_body: Any,
) -> list[dict[str, Any]]:
    if not isinstance(request_body, Mapping):
        raise AutoTradeDelegationError(
            "execution_authorization_invalid",
            "Delegated execution request is missing its body.",
        )
    body = dict(request_body)
    if operation_key == "spot.order.bulk_order":
        symbol = body.get("symbol")
        rows = body.get("orderList")
        if not isinstance(symbol, str) or not symbol or not isinstance(rows, list) or not rows:
            raise AutoTradeDelegationError(
                "execution_authorization_invalid",
                "Delegated Spot batch summary is invalid.",
            )
        if any(not isinstance(row, Mapping) for row in rows):
            raise AutoTradeDelegationError(
                "execution_authorization_invalid",
                "Delegated Spot batch summary is invalid.",
            )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            row_value = dict(row)
            if row_value.get("symbol") not in (None, symbol):
                raise AutoTradeDelegationError(
                    "execution_authorization_denied",
                    "Delegated Spot batch symbol does not match its envelope.",
                )
            normalized.append({**row_value, "symbol": symbol})
        return normalized
    if operation_key == "transaction.place_orders_batch":
        rows = body.get("batchOrders")
        if not isinstance(rows, list) or not rows or any(
            not isinstance(row, Mapping) for row in rows
        ):
            raise AutoTradeDelegationError(
                "execution_authorization_invalid",
                "Delegated Futures batch summary is invalid.",
            )
        return [dict(row) for row in rows]
    return [body]


__all__ = [
    "AutoTradeDelegation",
    "AutoTradeDelegationError",
]
