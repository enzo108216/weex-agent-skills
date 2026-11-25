#!/usr/bin/env python3
"""Persist and validate pending order confirmations for WEEX trade guard flows."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from weex_agent_state import config_dir

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX path
    msvcrt = None


INTENT_FILENAME = "order-intent.json"
INTENT_LOCK_FILENAME = ".order-intent.lock"
_INTENT_PROCESS_LOCK = threading.RLock()


class IntentStateError(RuntimeError):
    """Raised when pending order intent state cannot be changed safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def intent_path() -> Path:
    path = config_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path / INTENT_FILENAME


def intent_lock_path() -> Path:
    return intent_path().parent / INTENT_LOCK_FILENAME


@contextmanager
def _intent_lock():
    with _INTENT_PROCESS_LOCK:
        lock_path = intent_lock_path()
        descriptor: int | None = None
        lock_backend: str | None = None
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock_path, flags, 0o600)
            os.chmod(lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                lock_backend = "fcntl"
            elif msvcrt is not None:
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                lock_backend = "msvcrt"
            else:
                raise IntentStateError(
                    "intent_lock_unavailable",
                    "Pending order intent locking is unavailable on this platform.",
                )
        except IntentStateError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise IntentStateError(
                "intent_lock_unavailable",
                "Pending order intent locking is unavailable.",
            ) from exc
        try:
            yield
        finally:
            try:
                if descriptor is not None and lock_backend == "fcntl":
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                elif descriptor is not None and lock_backend == "msvcrt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            finally:
                if descriptor is not None:
                    os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor: int | None = None
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("pending order intent write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        _unlink_if_exists(temp_path)


def _load_intent_path(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None
    except (OSError, UnicodeError) as exc:
        raise IntentStateError(
            "intent_state_unavailable",
            "Pending order intent could not be loaded safely.",
        ) from exc
    return raw if isinstance(raw, dict) else None


def build_risk_signature(
    *,
    profile_name: str,
    market: str,
    trading_mode: str,
    order_preview: dict[str, Any],
    analysis_output: dict[str, Any],
) -> str:
    serialized = json.dumps(
        {
            "profile_name": profile_name,
            "market": market,
            "trading_mode": trading_mode,
            "order_preview": order_preview,
            "alerts": analysis_output.get("alerts", []),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_intent(
    *,
    profile_name: str,
    market: str,
    trading_mode: str = "live",
    environment: dict[str, Any] | None = None,
    order_preview: dict[str, Any],
    raw_order: dict[str, Any],
    analysis_output: dict[str, Any],
    now_ms: int | None = None,
    ttl_seconds: int = 300,
    intent_type: str = "order",
    tp_sl_order: dict[str, Any] | None = None,
    mcp_trade_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    expires_at = current_ms + (ttl_seconds * 1000)
    payload = {
        "intent_id": uuid.uuid4().hex,
        "intent_type": intent_type,
        "profile_name": profile_name,
        "market": market,
        "trading_mode": trading_mode,
        "created_at": current_ms,
        "expires_at": expires_at,
        "ttl_seconds": ttl_seconds,
        "order_preview": order_preview,
        "raw_order": raw_order,
        "analysis_output": analysis_output,
        "risk_signature": build_risk_signature(
            profile_name=profile_name,
            market=market,
            trading_mode=trading_mode,
            order_preview=order_preview,
            analysis_output=analysis_output,
        ),
    }
    if environment is not None:
        payload["environment"] = environment
    if tp_sl_order is not None:
        payload["tp_sl_order"] = tp_sl_order
    if mcp_trade_preview is not None:
        if not isinstance(mcp_trade_preview, dict):
            raise ValueError("mcp_trade_preview must be an object")
        mcp_expires_at = mcp_trade_preview.get("expires_at")
        if (
            isinstance(mcp_expires_at, bool)
            or not isinstance(mcp_expires_at, int)
            or mcp_expires_at <= 0
        ):
            raise ValueError("mcp_trade_preview expires_at must be a positive integer")
        serialized_preview = json.dumps(
            mcp_trade_preview,
            ensure_ascii=False,
            sort_keys=True,
        ).lower()
        if any(
            forbidden in serialized_preview
            for forbidden in ('"credential"', '"api_secret"', '"api_passphrase"')
        ):
            raise ValueError("mcp_trade_preview must not contain credentials")
        payload["mcp_trade_preview"] = mcp_trade_preview
        payload["expires_at"] = min(payload["expires_at"], mcp_expires_at * 1000)
    return payload


def save_intent(payload: dict[str, Any]) -> None:
    path = intent_path()
    try:
        with _intent_lock():
            current = _load_intent_path(path)
            if isinstance(current, dict):
                started_at = current.get("mcp_trade_confirmation_started_at")
                if started_at is not None:
                    same_claimed_intent = (
                        payload.get("intent_id") == current.get("intent_id")
                        and payload.get("risk_signature") == current.get("risk_signature")
                        and payload.get("intent_type", "order")
                        == current.get("intent_type", "order")
                        and payload.get("mcp_trade_confirmation_started_at") == started_at
                    )
                    if not same_claimed_intent:
                        raise IntentStateError(
                            "confirmation_started",
                            "Pending order confirmation already started and cannot be replaced.",
                        )
            _atomic_write_json(path, payload)
    except IntentStateError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise IntentStateError(
            "intent_state_unavailable",
            "Pending order intent could not be saved safely.",
        ) from exc


def load_intent() -> dict[str, Any] | None:
    return _load_intent_path(intent_path())


def claim_intent_for_confirmation(
    *,
    expected_intent_id: str,
    expected_risk_signature: str,
    expected_intent_type: str,
    started_at_ms: int | None = None,
) -> dict[str, Any]:
    path = intent_path()
    try:
        with _intent_lock():
            current = _load_intent_path(path)
            if current is None:
                raise IntentStateError("intent_missing", "No pending order intent was found.")
            if (
                current.get("intent_id") != expected_intent_id
                or current.get("risk_signature") != expected_risk_signature
                or current.get("intent_type", "order") != expected_intent_type
            ):
                raise IntentStateError(
                    "intent_changed",
                    "Pending order intent changed before confirmation.",
                )
            if current.get("mcp_trade_confirmation_started_at") is not None:
                raise IntentStateError(
                    "confirmation_started",
                    "Pending order confirmation already started.",
                )
            claim_time_ms = (
                started_at_ms if started_at_ms is not None else int(time.time() * 1000)
            )
            if int(current.get("expires_at") or 0) <= claim_time_ms:
                raise IntentStateError(
                    "intent_expired",
                    "Pending order intent expired before confirmation could be claimed.",
                )
            claimed = dict(current)
            claimed["mcp_trade_confirmation_started_at"] = claim_time_ms
            _atomic_write_json(path, claimed)
            return claimed
    except IntentStateError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise IntentStateError(
            "intent_state_unavailable",
            "Pending order intent could not be claimed safely.",
        ) from exc


def clear_intent(
    *,
    expected_intent_id: str | None = None,
    expected_risk_signature: str | None = None,
    expected_intent_type: str | None = None,
    expected_confirmation_started: bool | None = None,
) -> bool:
    path = intent_path()
    try:
        with _intent_lock():
            current = _load_intent_path(path)
            if current is None:
                return False
            if expected_intent_id is not None and current.get("intent_id") != expected_intent_id:
                return False
            if (
                expected_risk_signature is not None
                and current.get("risk_signature") != expected_risk_signature
            ):
                return False
            if (
                expected_intent_type is not None
                and current.get("intent_type", "order") != expected_intent_type
            ):
                return False
            confirmation_started = current.get("mcp_trade_confirmation_started_at") is not None
            if (
                expected_confirmation_started is not None
                and confirmation_started is not expected_confirmation_started
            ):
                return False
            _unlink_if_exists(path)
            _fsync_directory(path.parent)
            return True
    except IntentStateError:
        raise
    except OSError as exc:
        raise IntentStateError(
            "intent_state_unavailable",
            "Pending order intent could not be cleared safely.",
        ) from exc


def intent_is_expired(payload: dict[str, Any], *, now_ms: int | None = None) -> bool:
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    expires_at = int(payload.get("expires_at") or 0)
    return expires_at <= current_ms
