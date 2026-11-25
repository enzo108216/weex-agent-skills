#!/usr/bin/env python3
"""One-shot delegated authorization adapter for automated MCP Trade confirms."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


_OPERATION_ACTIONS = {
    "spot.order.place_order": "order",
    "spot.order.bulk_order": "batch_order",
    "transaction.place_order": "order",
    "transaction.place_orders_batch": "batch_order",
    "transaction.place_pending_order": "conditional_order",
    "transaction.place_tp_sl_order": "tp_sl",
}


class AutoTradeDelegationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AutoTradeDelegation:
    """Bind one reserved submission group to one in-process delegated confirm."""

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
        now_epoch: Callable[[], int] | None = None,
        ref_factory: Callable[[], str] | None = None,
    ) -> None:
        action = _OPERATION_ACTIONS.get(operation_key)
        if action is None:
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
        self.action = action
        self._now_epoch = now_epoch or (lambda: int(time.time()))
        self._ref = (ref_factory or (lambda: "auto_" + secrets.token_urlsafe(32)))()
        if not isinstance(self._ref, str) or not self._ref:
            raise ValueError("delegated authorization ref must be a non-empty string")
        self._profile_id: str | None = None
        self._preview_expires_at: int | None = None
        self._execution_digest: str | None = None
        self._bound = False
        self._used = False

    @property
    def delegated_ref(self) -> str:
        return self._ref

    def bind_preview(
        self,
        *,
        profile_id: str,
        preview: Mapping[str, Any],
        summary: Mapping[str, Any],
        risk: Mapping[str, Any],
    ) -> None:
        if self._bound or self._used:
            raise AutoTradeDelegationError(
                "execution_authorization_invalid",
                "Delegated execution preview is already bound.",
            )
        if not isinstance(profile_id, str) or not profile_id:
            raise AutoTradeDelegationError(
                "execution_authorization_invalid",
                "Delegated execution profile is invalid.",
            )
        if preview.get("legacy_key") != self.operation_key:
            raise AutoTradeDelegationError(
                "execution_authorization_invalid",
                "Delegated execution operation does not match the preview.",
            )
        preview_profile = preview.get("profile_id")
        expires_at = preview.get("expires_at")
        arguments = preview.get("arguments")
        if (
            preview_profile != profile_id
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= self._now_epoch()
            or not isinstance(arguments, Mapping)
            or not isinstance(summary, Mapping)
            or not isinstance(risk, Mapping)
        ):
            raise AutoTradeDelegationError(
                "execution_authorization_invalid",
                "Delegated execution preview binding is invalid.",
            )
        preview_client_ids = _client_order_ids(arguments)
        if preview_client_ids != self.client_order_ids:
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution client-order identities do not match the reservation.",
            )
        if not hmac.compare_digest(
            _orders_digest(
                _orders_from_request_body(
                    self.operation_key,
                    summary.get("request_body"),
                )
            ),
            self._expected_orders_digest,
        ):
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution request does not match the reservation.",
            )
        self._profile_id = profile_id
        self._preview_expires_at = expires_at
        self._execution_digest = _execution_digest(
            action=self.action,
            market=str(summary.get("market") or ""),
            trading_mode=str(summary.get("trading_mode") or ""),
            summary=summary,
            risk=risk,
        )
        self._bound = True

    def authorize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._used:
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution authorization was already used.",
            )
        if not self._bound or self._profile_id is None or self._execution_digest is None:
            raise AutoTradeDelegationError(
                "execution_authorization_invalid",
                "Delegated execution preview is not bound.",
            )
        delegated_ref = payload.get("delegated_ref")
        context = payload.get("context")
        execution = payload.get("execution")
        fingerprint = payload.get("credential_fingerprint")
        if (
            not isinstance(delegated_ref, Mapping)
            or set(delegated_ref) != {"ref"}
            or not hmac.compare_digest(str(delegated_ref.get("ref") or ""), self._ref)
            or not isinstance(context, Mapping)
            or context.get("user_id") != self._profile_id
            or not isinstance(execution, Mapping)
            or not isinstance(fingerprint, str)
            or not fingerprint
        ):
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution authorization binding was rejected.",
            )
        digest = _execution_digest(
            action=str(execution.get("action") or ""),
            market=str(execution.get("market") or ""),
            trading_mode=str(execution.get("trading_mode") or ""),
            summary=execution.get("summary"),
            risk=execution.get("risk"),
        )
        if (
            execution.get("action") != self.action
            or not hmac.compare_digest(digest, self._execution_digest)
        ):
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution action or preview did not match.",
            )
        current_epoch = self._now_epoch()
        snapshot = self.state.read_delegated_submission_group(
            authorization_id=self.authorization_id,
            submission_group_id=self.submission_group_id,
            request_fingerprint=self.request_fingerprint,
            usage_ids=self.usage_ids,
            client_order_ids=self.client_order_ids,
            now=datetime.fromtimestamp(current_epoch, tz=UTC),
        )
        if not isinstance(snapshot, Mapping):
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution state snapshot is invalid.",
            )
        if (
            snapshot.get("strategy_id") != self.strategy_id
            or snapshot.get("profile_id") != self._profile_id
        ):
            raise AutoTradeDelegationError(
                "execution_authorization_denied",
                "Delegated execution state binding did not match.",
            )
        state_expires = snapshot.get("expires_at")
        if isinstance(state_expires, bool) or not isinstance(state_expires, int):
            raise AutoTradeDelegationError(
                "execution_authorization_invalid",
                "Delegated execution state expiry is invalid.",
            )
        expires_at = min(state_expires, int(self._preview_expires_at or 0))
        if expires_at <= current_epoch:
            raise AutoTradeDelegationError(
                "execution_authorization_expired",
                "Delegated execution authorization expired.",
            )
        scope_signature = snapshot.get("scope_signature")
        if not isinstance(scope_signature, str) or not scope_signature:
            raise AutoTradeDelegationError(
                "execution_authorization_invalid",
                "Delegated execution scope is invalid.",
            )
        scope_fingerprint = hashlib.sha256(
            f"{scope_signature}:{self._execution_digest}:{fingerprint}".encode("utf-8")
        ).hexdigest()
        self._used = True
        return {
            "authorization_id": self.authorization_id,
            "scope_fingerprint": scope_fingerprint,
            "expires_at": expires_at,
            "audit_metadata": {
                "submission_group_id": self.submission_group_id,
                "usage_count": len(self.usage_ids),
            },
        }


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


def _execution_digest(
    *,
    action: str,
    market: str,
    trading_mode: str,
    summary: Any,
    risk: Any,
) -> str:
    if not isinstance(summary, Mapping) or not isinstance(risk, Mapping):
        raise AutoTradeDelegationError(
            "execution_authorization_invalid",
            "Delegated execution summary or risk is invalid.",
        )
    encoded = json.dumps(
        {
            "action": action,
            "market": market,
            "trading_mode": trading_mode,
            "summary": summary,
            "risk": risk,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
            "Delegated execution summary is missing its request body.",
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
