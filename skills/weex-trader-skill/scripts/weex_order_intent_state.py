#!/usr/bin/env python3
"""Persist and validate pending order confirmations for WEEX trade guard flows."""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
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
INTENT_AUDIT_FILENAME = "manual-mutation-intent-audit.jsonl"
_INTENT_PROCESS_LOCK = threading.RLock()
_DISPATCH_LOCK_FILENAME = ".order-intent-dispatch.lock"
_DISPATCH_PROCESS_LOCK = threading.Lock()
NOT_SENT_TERMINAL_OUTCOME_DIGEST = (
    "sha256:8f587cdbad0e314e82c20fefe6edf4379243e56eb154cb8ca47b198db2198027"
)
_REVIEW_REQUIRED_OUTCOME_DIGEST = (
    "sha256:4bde4a17c691eb4462fbc26b6d4e2fa0af1d897055b356df4a287b2c822d7f2c"
)
_INTENT_CONTRACT_VERSIONS = {
    "package": "0.3.3",
    "tool_contract": "2",
    "prepare": "1",
    "intent": "2",
    "grant": "2",
    "boundary_contract_digest": (
        "sha256:23c0fa218f2dcb3024b3067f8456d17e3f968ca63a7d9409e7b5a36ed9b0fc53"
    ),
}
_PREVIOUS_INTENT_CONTRACT_VERSIONS = (
    {
        "package": "0.3.1", "tool_contract": "2", "prepare": "1", "intent": "2", "grant": "2",
        "boundary_contract_digest": "sha256:a6762cafe7b9338b658252e6048582c2be8fca0bc1b5c730223ff807868be9b9",
    },
    {
        "package": "0.3.2", "tool_contract": "2", "prepare": "1", "intent": "2", "grant": "2",
        "boundary_contract_digest": "sha256:a08c5997ab1ef4698ad1bd37ff89378fedf340600dde668d0f9fc5fc256b7b10",
    },
)
_DIRECT_TOOLS = frozenset(
    {
        "trade_place_order",
        "trade_cancel_order",
        "trade_update_leverage",
        "trade_set_margin_mode",
        "trade_place_batch_orders",
        "trade_cancel_batch_orders",
        "trade_cancel_open_orders",
        "trade_place_conditional_order",
        "trade_cancel_conditional_order",
        "trade_place_tp_sl",
        "trade_modify_tp_sl",
        "trade_close_positions",
        "trade_adjust_position_margin",
        "trade_set_auto_append_margin",
    }
)
_BLOCKING_STATES = frozenset(
    {"UNCLAIMED", "CONFIRMED", "CLAIMED", "REVIEW_REQUIRED"}
)
_TERMINAL_STATES = frozenset(
    {
        "NOT_SENT_TERMINAL",
        "DEFINITIVE_REJECTION",
        "ACCEPTED",
        "LEGACY_INVALIDATED",
    }
)
_RESOLVED_RECONCILIATION = frozenset(
    {"RESOLVED_ACCEPTED", "RESOLVED_REJECTED", "RESOLVED_NOT_SENT"}
)
_FULL_INTENT_KEYS = frozenset(
    {
        "intent_version",
        "intent_id",
        "state",
        "tool_name",
        "action",
        "market",
        "trading_mode",
        "direct_arguments",
        "prepared_mutation",
        "prepared_artifact_digest",
        "execution_binding_digest",
        "risk_facts",
        "profile_binding",
        "contract_versions",
        "created_at",
        "expires_at",
        "confirmed_at",
        "grant",
        "request_identity",
        "reconciliation",
        "legacy",
    }
)
_PREPARED_MUTATION_KEYS = frozenset(
    {
        "contract_version",
        "tool_name",
        "action",
        "market",
        "trading_mode",
        "canonical_arguments",
        "canonical_arguments_digest",
        "risk_facts_digest",
        "route_fingerprint",
        "credential_fingerprint",
        "precondition",
        "risk_facts",
        "reconciliation_template",
        "expires_at",
        "execution_binding_digest",
        "prepared_artifact_digest",
    }
)
_SPARSE_INTENT_KEYS = frozenset(
    {"intent_version", "intent_id", "state", "reconciliation", "legacy"}
)
_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "api_passphrase",
        "secret_key",
        "passphrase",
        "password",
        "access_token",
        "refresh_token",
    }
)


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


def audit_path() -> Path:
    return intent_path().parent / INTENT_AUDIT_FILENAME


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


@contextmanager
def manual_intent_dispatch_lock():
    """Exclude explicit recovery while a Direct mutation is in flight."""
    if not _DISPATCH_PROCESS_LOCK.acquire(blocking=False):
        raise IntentStateError("intent_dispatch_in_flight", "A manual mutation is still in progress.")
    descriptor: int | None = None
    try:
        try:
            path = intent_path().parent / _DISPATCH_LOCK_FILENAME
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or (
                os.name != "nt" and (metadata.st_mode & 0o077 or metadata.st_uid != os.getuid())
            ):
                raise IntentStateError("intent_dispatch_lock_unavailable", "Manual mutation dispatch lock is not owner-only.")
            if fcntl is None and msvcrt is None:
                raise IntentStateError(
                    "intent_dispatch_lock_unavailable", "Manual mutation dispatch locking is unavailable on this platform."
                )
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise IntentStateError(
                        "intent_dispatch_in_flight", "A manual mutation is still in progress."
                    ) from exc
                raise
        except OSError as exc:
            raise IntentStateError(
                "intent_dispatch_lock_unavailable", "Manual mutation dispatch locking is unavailable."
            ) from exc
        yield
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            _DISPATCH_PROCESS_LOCK.release()


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


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _contains_forbidden_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_SECRET_KEYS:
                return True
            if _contains_forbidden_secret(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_secret(item) for item in value)
    return False


def _nonnegative_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _strict_load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    _require_owner_only_file(
        path,
        code="intent_state_permissions",
        message="Manual mutation intent state is not owner-only.",
    )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation intent state is corrupt.",
        ) from exc
    except OSError as exc:
        raise IntentStateError(
            "intent_state_unavailable",
            "Manual mutation intent state could not be loaded safely.",
        ) from exc
    if not isinstance(raw, dict):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation intent state must be an object.",
        )
    return raw


def _require_owner_only_file(
    path: Path,
    *,
    code: str,
    message: str,
) -> None:
    if os.name == "nt":
        return
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise IntentStateError(code, message) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
        or metadata.st_uid != os.getuid()
    ):
        raise IntentStateError(code, message)


def _request_identity_from_template(template: Mapping[str, Any]) -> dict[str, Any] | None:
    identity = template.get("identity")
    if isinstance(identity, Mapping):
        return deepcopy(dict(identity))
    return None


def _validate_reconciliation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "status",
        "template",
        "result",
        "observed_at",
        "evidence_digest",
    }:
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation reconciliation state is invalid.",
        )
    status = value.get("status")
    if status not in {
        "NOT_REQUIRED",
        "PENDING",
        "RESOLVED_ACCEPTED",
        "RESOLVED_REJECTED",
        "RESOLVED_NOT_SENT",
        "UNRESOLVED",
        "MANUAL_REVIEW_REQUIRED",
    }:
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation reconciliation status is invalid.",
        )
    if value.get("template") is not None and not isinstance(
        value.get("template"), dict
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation reconciliation template is invalid.",
        )
    evidence_digest = value.get("evidence_digest")
    observed_at = value.get("observed_at")
    result = value.get("result")
    if evidence_digest is not None and not _is_sha256(evidence_digest):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation reconciliation evidence is invalid.",
        )
    if observed_at is not None and not _nonnegative_integer(observed_at):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation reconciliation observation time is invalid.",
        )
    resolved_results = {
        "RESOLVED_ACCEPTED": "ACCEPTED",
        "RESOLVED_REJECTED": "DEFINITIVE_REJECTION",
        "RESOLVED_NOT_SENT": "NOT_SENT",
    }
    if status in {"NOT_REQUIRED", "PENDING"} and any(
        item is not None for item in (result, observed_at, evidence_digest)
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Inactive reconciliation contains result evidence.",
        )
    if status in resolved_results and (
        result != resolved_results[status]
        or not _nonnegative_integer(observed_at)
        or not _is_sha256(evidence_digest)
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Resolved reconciliation evidence is invalid.",
        )
    if status in {"UNRESOLVED", "MANUAL_REVIEW_REQUIRED"} and (
        result is not None
        or not _nonnegative_integer(observed_at)
        or not _is_sha256(evidence_digest)
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Unresolved reconciliation evidence is invalid.",
        )
    return value


def _validate_legacy_envelope(value: Any) -> dict[str, Any]:
    required = {
        "source_version",
        "source_record_digest",
        "source_state",
        "source_started",
        "migration",
        "migrated_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation legacy migration state is invalid.",
        )
    if value["source_version"] is not None and not _nonnegative_integer(
        value["source_version"]
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation legacy version is invalid.",
        )
    if not _is_sha256(value["source_record_digest"]):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation legacy digest is invalid.",
        )
    if value["source_state"] is not None and (
        not isinstance(value["source_state"], str) or not value["source_state"]
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation legacy state is invalid.",
        )
    if not isinstance(value["source_started"], bool) or value["migration"] not in {
        "LEGACY_INVALIDATED",
        "NOT_SENT_TERMINAL",
        "DEFINITIVE_REJECTION",
        "ACCEPTED",
        "REVIEW_REQUIRED",
    }:
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation legacy classification is invalid.",
        )
    if not _nonnegative_integer(value["migrated_at"]):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation legacy migration time is invalid.",
        )
    return value


def _validate_manual_intent(
    value: Any,
    *,
    allow_previous_candidate: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("intent_version") != 2:
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation intent version is invalid.",
        )
    if (
        not isinstance(value.get("intent_id"), str)
        or not value["intent_id"]
        or _contains_forbidden_secret(value)
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation intent identity or content is invalid.",
        )
    state = value.get("state")
    reconciliation = _validate_reconciliation(value.get("reconciliation"))
    legacy = value.get("legacy")
    if legacy is not None:
        if set(value) != _SPARSE_INTENT_KEYS:
            raise IntentStateError(
                "intent_state_corrupt",
                "Legacy migration must use the sparse intent envelope.",
            )
        _validate_legacy_envelope(legacy)
        if state not in _TERMINAL_STATES | {"REVIEW_REQUIRED"}:
            raise IntentStateError(
                "intent_state_corrupt",
                "Legacy migration state is invalid.",
            )
        if legacy["migration"] != state:
            raise IntentStateError(
                "intent_state_corrupt",
                "Legacy migration state does not match its envelope.",
            )
        if reconciliation.get("template") is not None:
            raise IntentStateError(
                "intent_state_corrupt",
                "Legacy migration must not fabricate reconciliation templates.",
            )
        if (
            state in _TERMINAL_STATES
            and reconciliation["status"] != "NOT_REQUIRED"
        ) or (
            state == "REVIEW_REQUIRED"
            and reconciliation["status"]
            not in _RESOLVED_RECONCILIATION
            | {"MANUAL_REVIEW_REQUIRED"}
        ):
            raise IntentStateError(
                "intent_state_corrupt",
                "Legacy migration reconciliation state is invalid.",
            )
        return value

    if set(value) != _FULL_INTENT_KEYS:
        raise IntentStateError(
            "intent_state_corrupt",
            "Complete manual mutation intent fields are invalid.",
        )
    if state not in {
        "UNCLAIMED",
        "CONFIRMED",
        "CLAIMED",
        "NOT_SENT_TERMINAL",
        "DEFINITIVE_REJECTION",
        "ACCEPTED",
        "REVIEW_REQUIRED",
    }:
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation intent state is invalid.",
        )
    prepared = value.get("prepared_mutation")
    direct_arguments = value.get("direct_arguments")
    risk_facts = value.get("risk_facts")
    profile = value.get("profile_binding")
    versions = value.get("contract_versions")
    if (
        not isinstance(prepared, dict)
        or not isinstance(direct_arguments, dict)
        or not isinstance(risk_facts, dict)
        or not isinstance(profile, dict)
        or not isinstance(versions, dict)
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation intent bindings are invalid.",
        )
    if set(prepared) != _PREPARED_MUTATION_KEYS:
        raise IntentStateError(
            "intent_state_corrupt",
            "Stored PreparedMutation fields are invalid.",
        )
    precondition = prepared.get("precondition")
    if (
        not isinstance(precondition, dict)
        or set(precondition)
        != {"policy", "digest", "observed_at", "expires_at", "facts"}
        or not _is_sha256(precondition.get("digest"))
        or not _nonnegative_integer(precondition.get("observed_at"))
        or not _nonnegative_integer(precondition.get("expires_at"))
        or not isinstance(precondition.get("facts"), dict)
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Stored PreparedMutation precondition is invalid.",
        )
    if (
        value.get("tool_name") not in _DIRECT_TOOLS
        or value.get("tool_name") != prepared.get("tool_name")
        or value.get("action") != prepared.get("action")
        or value.get("market") != prepared.get("market")
        or value.get("trading_mode") != prepared.get("trading_mode")
        or direct_arguments != prepared.get("canonical_arguments")
        or risk_facts != prepared.get("risk_facts")
        or value.get("market")
        != prepared.get("canonical_arguments", {}).get("market")
        or value.get("trading_mode")
        != prepared.get("canonical_arguments", {}).get("trading_mode")
        or value.get("prepared_artifact_digest")
        != prepared.get("prepared_artifact_digest")
        or value.get("execution_binding_digest")
        != prepared.get("execution_binding_digest")
        or value.get("expires_at") != prepared.get("expires_at")
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation intent does not match PreparedMutation.",
        )
    for field in (
        "canonical_arguments_digest",
        "risk_facts_digest",
        "route_fingerprint",
        "prepared_artifact_digest",
        "execution_binding_digest",
    ):
        if not _is_sha256(prepared.get(field)):
            raise IntentStateError(
                "intent_state_corrupt",
                "Stored PreparedMutation digest is invalid.",
            )
    if not all(
        _nonnegative_integer(value.get(field))
        for field in ("created_at", "expires_at")
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation intent time is invalid.",
        )
    if value["expires_at"] <= value["created_at"]:
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation intent expires before it can be used.",
        )
    if (
        not isinstance(prepared.get("credential_fingerprint"), str)
        or not prepared["credential_fingerprint"]
        or not isinstance(prepared.get("canonical_arguments"), dict)
        or not isinstance(prepared.get("risk_facts"), dict)
        or not isinstance(prepared.get("reconciliation_template"), dict)
        or not _nonnegative_integer(prepared.get("expires_at"))
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Stored PreparedMutation content is invalid.",
        )
    expected_profile_keys = {
        "profile_id",
        "profile_name",
        "principal_id",
        "account_binding_id",
        "spot_base_url",
        "futures_base_url",
        "route_fingerprint",
        "credential_fingerprint",
    }
    if (
        set(profile) != expected_profile_keys
        or any(not isinstance(profile[key], str) or not profile[key] for key in expected_profile_keys)
        or not profile["spot_base_url"].startswith("https://")
        or not profile["futures_base_url"].startswith("https://")
        or not _is_sha256(profile["route_fingerprint"])
        or profile["route_fingerprint"] != prepared["route_fingerprint"]
        or profile["credential_fingerprint"] != prepared["credential_fingerprint"]
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation profile binding is invalid.",
        )
    if versions != _INTENT_CONTRACT_VERSIONS and not (
        allow_previous_candidate and versions in _PREVIOUS_INTENT_CONTRACT_VERSIONS
    ):
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation contract versions are invalid.",
        )
    if value.get("request_identity") != _request_identity_from_template(
        prepared["reconciliation_template"]
    ) or reconciliation.get("template") != prepared["reconciliation_template"]:
        raise IntentStateError(
            "intent_state_corrupt",
            "Manual mutation reconciliation was not derived from MCP.",
        )
    grant = value.get("grant")
    confirmed_at = value.get("confirmed_at")
    expected_grant_state = {
        "CONFIRMED": "ISSUED",
        "CLAIMED": "CLAIMED",
        "NOT_SENT_TERMINAL": "NOT_SENT_TERMINAL",
        "DEFINITIVE_REJECTION": "DEFINITIVE_REJECTION",
        "ACCEPTED": "ACCEPTED",
        "REVIEW_REQUIRED": "REVIEW_REQUIRED",
    }.get(state)
    if state == "UNCLAIMED":
        if grant is not None or confirmed_at is not None or reconciliation["status"] != "NOT_REQUIRED":
            raise IntentStateError(
                "intent_state_corrupt",
                "UNCLAIMED intent contains confirmation state.",
            )
    else:
        required_grant = {
            "grant_id",
            "state",
            "principal_id",
            "account_binding_id",
            "credential_fingerprint",
            "tool_name",
            "prepared_artifact_digest",
            "execution_binding_digest",
            "issued_at",
            "expires_at",
        }
        optional_grant = {"claim_id", "terminal_state", "outcome_digest"}
        if (
            not isinstance(grant, dict)
            or not required_grant.issubset(grant)
            or not set(grant).issubset(required_grant | optional_grant)
            or grant.get("state") != expected_grant_state
            or not _nonnegative_integer(confirmed_at)
        ):
            raise IntentStateError(
                "intent_state_corrupt",
                "Manual mutation grant state is invalid.",
            )
        if (
            not isinstance(grant.get("grant_id"), str)
            or not grant["grant_id"]
            or not _nonnegative_integer(grant.get("issued_at"))
            or not _nonnegative_integer(grant.get("expires_at"))
            or grant["issued_at"] >= grant["expires_at"]
            or not _is_sha256(grant.get("prepared_artifact_digest"))
            or not _is_sha256(grant.get("execution_binding_digest"))
        ):
            raise IntentStateError(
                "intent_state_corrupt",
                "Manual mutation grant fields are invalid.",
            )
        if state == "CLAIMED" and (
            not isinstance(grant.get("claim_id"), str)
            or not grant["claim_id"]
        ):
            raise IntentStateError(
                "intent_state_corrupt",
                "Claimed manual mutation grant lacks claim identity.",
            )
        if state in {
            "NOT_SENT_TERMINAL",
            "DEFINITIVE_REJECTION",
            "ACCEPTED",
            "REVIEW_REQUIRED",
        } and (
            grant.get("terminal_state") != state
            or not _is_sha256(grant.get("outcome_digest"))
        ):
            raise IntentStateError(
                "intent_state_corrupt",
                "Terminal manual mutation grant fields are invalid.",
            )
        if (
            state == "NOT_SENT_TERMINAL"
            and grant.get("outcome_digest")
            != NOT_SENT_TERMINAL_OUTCOME_DIGEST
        ):
            raise IntentStateError(
                "intent_state_corrupt",
                "Not-sent terminal outcome digest is invalid.",
            )
        if (
            grant.get("principal_id") != profile["principal_id"]
            or grant.get("account_binding_id") != profile["account_binding_id"]
            or grant.get("credential_fingerprint")
            != profile["credential_fingerprint"]
            or grant.get("tool_name") != value["tool_name"]
            or grant.get("prepared_artifact_digest")
            != value["prepared_artifact_digest"]
            or grant.get("execution_binding_digest")
            != value["execution_binding_digest"]
            or grant.get("expires_at") != value["expires_at"]
        ):
            raise IntentStateError(
                "intent_state_corrupt",
                "Manual mutation grant does not match intent bindings.",
            )
    if state == "REVIEW_REQUIRED":
        if reconciliation["status"] not in {
            "PENDING",
            "RESOLVED_ACCEPTED",
            "RESOLVED_REJECTED",
            "RESOLVED_NOT_SENT",
            "UNRESOLVED",
            "MANUAL_REVIEW_REQUIRED",
        }:
            raise IntentStateError(
                "intent_state_corrupt",
                "Review-required reconciliation state is invalid.",
            )
    elif state != "UNCLAIMED" and reconciliation["status"] != "NOT_REQUIRED":
        raise IntentStateError(
            "intent_state_corrupt",
            "Terminal or claimed intent has invalid reconciliation state.",
        )
    return value


def build_unclaimed_intent(
    *,
    prepared_mutation: Mapping[str, Any],
    profile_binding: Mapping[str, Any],
    contract_versions: Mapping[str, Any],
    intent_id: str | None = None,
    created_at: int | None = None,
) -> dict[str, Any]:
    prepared = deepcopy(dict(prepared_mutation))
    profile = deepcopy(dict(profile_binding))
    versions = deepcopy(dict(contract_versions))
    active_intent_id = intent_id or uuid.uuid4().hex
    active_created_at = (
        int(time.time() * 1000) if created_at is None else created_at
    )
    template = prepared.get("reconciliation_template")
    if not isinstance(template, Mapping):
        raise IntentStateError(
            "intent_invalid",
            "PreparedMutation reconciliation template is invalid.",
        )
    payload = {
        "intent_version": 2,
        "intent_id": active_intent_id,
        "state": "UNCLAIMED",
        "tool_name": prepared.get("tool_name"),
        "action": prepared.get("action"),
        "market": prepared.get("market"),
        "trading_mode": prepared.get("trading_mode"),
        "direct_arguments": deepcopy(prepared.get("canonical_arguments")),
        "prepared_mutation": prepared,
        "prepared_artifact_digest": prepared.get("prepared_artifact_digest"),
        "execution_binding_digest": prepared.get("execution_binding_digest"),
        "risk_facts": deepcopy(prepared.get("risk_facts")),
        "profile_binding": profile,
        "contract_versions": versions,
        "created_at": active_created_at,
        "expires_at": prepared.get("expires_at"),
        "confirmed_at": None,
        "grant": None,
        "request_identity": _request_identity_from_template(template),
        "reconciliation": {
            "status": "NOT_REQUIRED",
            "template": deepcopy(dict(template)),
            "result": None,
            "observed_at": None,
            "evidence_digest": None,
        },
        "legacy": None,
    }
    try:
        return deepcopy(_validate_manual_intent(payload))
    except IntentStateError as exc:
        raise IntentStateError(
            "intent_invalid",
            "Complete manual mutation intent is invalid.",
        ) from exc


def _legacy_source_state(raw: Mapping[str, Any]) -> str | None:
    for key in ("state", "trade_status", "status"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    if raw.get("mcp_trade_confirmation_started_at") is not None:
        return "CONFIRMATION_STARTED"
    return None


def _sparse_migration(
    raw: Mapping[str, Any],
    *,
    migration: str,
    migrated_at: int,
    source_started: bool,
) -> dict[str, Any]:
    source = deepcopy(dict(raw))
    source_digest = _canonical_digest(source)
    raw_intent_id = source.get("intent_id")
    intent_id = (
        raw_intent_id
        if isinstance(raw_intent_id, str) and raw_intent_id
        else "legacy_" + source_digest[7:39]
    )
    status = (
        "MANUAL_REVIEW_REQUIRED"
        if migration == "REVIEW_REQUIRED"
        else "NOT_REQUIRED"
    )
    payload = {
        "intent_version": 2,
        "intent_id": intent_id,
        "state": migration,
        "reconciliation": {
            "status": status,
            "template": None,
            "result": None,
            "observed_at": migrated_at if migration == "REVIEW_REQUIRED" else None,
            "evidence_digest": (
                source_digest if migration == "REVIEW_REQUIRED" else None
            ),
        },
        "legacy": {
            "source_version": (
                source.get("intent_version")
                if _nonnegative_integer(source.get("intent_version"))
                else None
            ),
            "source_record_digest": source_digest,
            "source_state": _legacy_source_state(source),
            "source_started": source_started,
            "migration": migration,
            "migrated_at": migrated_at,
        },
    }
    return deepcopy(_validate_manual_intent(payload))


def _migrate_legacy_locked(
    raw: Mapping[str, Any],
    *,
    now_ms: int,
) -> dict[str, Any]:
    marker_started = raw.get("mcp_trade_confirmation_started_at") is not None
    source_state = _legacy_source_state(raw)
    state_text = str(source_state or "").strip().upper()
    if state_text == "ACCEPTED":
        migration = "ACCEPTED"
        started = True
    elif state_text in {"REJECTED", "DEFINITIVE_REJECTION"}:
        migration = "DEFINITIVE_REJECTION"
        started = True
    elif state_text in {"NOT_SENT", "NOT_SENT_TERMINAL"}:
        migration = "NOT_SENT_TERMINAL"
        started = True
    elif marker_started or state_text not in {
        "",
        "PENDING",
        "PREVIEWED",
        "UNCLAIMED",
    }:
        migration = "REVIEW_REQUIRED"
        started = True
    else:
        migration = "LEGACY_INVALIDATED"
        started = False
    migrated = _sparse_migration(
        raw,
        migration=migration,
        migrated_at=now_ms,
        source_started=started,
    )
    if migration in _TERMINAL_STATES:
        _append_terminal_audit_locked(migrated, archived_at=now_ms)
        try:
            _atomic_write_json(intent_path(), migrated)
        except OSError:
            pass
    else:
        _atomic_write_json(intent_path(), migrated)
    return migrated


def _validate_audit_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "audit_version",
        "archived_at",
        "intent",
        "record_digest",
    }:
        raise IntentStateError(
            "intent_audit_corrupt",
            "Manual mutation terminal audit record is invalid.",
        )
    body = dict(value)
    stored_digest = body.pop("record_digest")
    intent = _validate_manual_intent(
        value.get("intent"), allow_previous_candidate=True,
    )
    reconciliation_status = intent["reconciliation"]["status"]
    if (
        value.get("audit_version") != 1
        or not _nonnegative_integer(value.get("archived_at"))
        or stored_digest != _canonical_digest(body)
        or (
            intent["state"] not in _TERMINAL_STATES
            and not (
                intent["state"] == "REVIEW_REQUIRED"
                and reconciliation_status in _RESOLVED_RECONCILIATION
            )
        )
    ):
        raise IntentStateError(
            "intent_audit_corrupt",
            "Manual mutation terminal audit record is invalid.",
        )
    return value


def _load_terminal_audit_records_locked() -> list[dict[str, Any]]:
    path = audit_path()
    if not path.exists():
        return []
    _require_owner_only_file(
        path,
        code="intent_audit_permissions",
        message="Manual mutation terminal audit journal is not owner-only.",
    )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise IntentStateError(
            "intent_audit_unavailable",
            "Manual mutation terminal audit journal is unavailable.",
        ) from exc
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line:
            raise IntentStateError(
                "intent_audit_corrupt",
                "Manual mutation terminal audit journal contains an empty record.",
            )
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntentStateError(
                "intent_audit_corrupt",
                "Manual mutation terminal audit journal is corrupt.",
            ) from exc
        records.append(deepcopy(_validate_audit_record(value)))
    return records


def load_terminal_audit_records() -> list[dict[str, Any]]:
    with _intent_lock():
        return _load_terminal_audit_records_locked()


def _append_terminal_audit_locked(
    intent: Mapping[str, Any],
    *,
    archived_at: int,
) -> dict[str, Any]:
    validated = deepcopy(
        _validate_manual_intent(dict(intent), allow_previous_candidate=True)
    )
    if _contains_forbidden_secret(validated):
        raise IntentStateError(
            "intent_audit_unavailable",
            "Manual mutation terminal audit contains forbidden secret fields.",
        )
    body = {
        "audit_version": 1,
        "archived_at": archived_at,
        "intent": validated,
    }
    record = {**body, "record_digest": _canonical_digest(body)}
    existing = _load_terminal_audit_records_locked()
    if any(item["record_digest"] == record["record_digest"] for item in existing):
        return record
    path = audit_path()
    encoded = (
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor: int | None = None
    initial_size = 0
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        initial_size = os.fstat(descriptor).st_size
        os.lseek(descriptor, 0, os.SEEK_END)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("manual intent audit append made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
        return record
    except OSError as exc:
        if descriptor is not None:
            try:
                os.ftruncate(descriptor, initial_size)
                os.fsync(descriptor)
            except OSError as rollback_exc:
                raise IntentStateError(
                    "intent_state_unavailable",
                    "Manual mutation audit append could not be rolled back.",
                ) from rollback_exc
        raise IntentStateError(
            "intent_audit_unavailable",
            "Manual mutation terminal audit could not be persisted.",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _latest_audit_intent_locked(intent_id: str) -> dict[str, Any] | None:
    result = None
    for record in _load_terminal_audit_records_locked():
        candidate = record["intent"]
        if candidate.get("intent_id") == intent_id:
            result = candidate
    return deepcopy(result) if result is not None else None


def _effective_manual_intent_locked(
    *,
    now_ms: int,
) -> dict[str, Any] | None:
    raw = _strict_load_json(intent_path())
    if raw is None:
        return None
    if raw.get("intent_version") != 2:
        source_digest = _canonical_digest(raw)
        for record in reversed(_load_terminal_audit_records_locked()):
            legacy = record["intent"].get("legacy")
            if (
                isinstance(legacy, dict)
                and legacy.get("source_record_digest") == source_digest
            ):
                return deepcopy(record["intent"])
        current = _migrate_legacy_locked(raw, now_ms=now_ms)
    else:
        current = deepcopy(_validate_manual_intent(raw, allow_previous_candidate=True))
    overlay = _latest_audit_intent_locked(str(current["intent_id"]))
    if overlay is not None:
        return overlay
    versions = current.get("contract_versions")
    if versions != _INTENT_CONTRACT_VERSIONS and versions in _PREVIOUS_INTENT_CONTRACT_VERSIONS:
        if current["state"] == "UNCLAIMED":
            return _invalidate_unclaimed_locked(current, now_ms=now_ms)
        if current["state"] == "CONFIRMED":
            # The previous grant was never claimed, so its write cannot have started.
            terminal = _terminalized_intent(
                current,
                terminal_state="NOT_SENT_TERMINAL",
                outcome_digest=NOT_SENT_TERMINAL_OUTCOME_DIGEST,
            )
            _append_terminal_audit_locked(terminal, archived_at=now_ms)
            try:
                _atomic_write_json(intent_path(), terminal)
            except OSError:
                pass
            return terminal
    return current


def load_manual_intent(
    *,
    now_ms: int | None = None,
    expected_intent_id: str | None = None,
    expected_prepared_artifact_digest: str | None = None,
) -> dict[str, Any] | None:
    active_now = int(time.time() * 1000) if now_ms is None else now_ms
    if not _nonnegative_integer(active_now):
        raise IntentStateError("invalid_arguments", "now_ms is invalid.")
    if expected_intent_id is not None or expected_prepared_artifact_digest is not None:
        if (
            not isinstance(expected_intent_id, str)
            or not expected_intent_id
            or not _is_sha256(expected_prepared_artifact_digest)
        ):
            raise IntentStateError("invalid_arguments", "Manual mutation lookup binding is invalid.")
    with _intent_lock():
        current = _effective_manual_intent_locked(now_ms=active_now)
        if expected_intent_id is None:
            return current
        if current is None or current["intent_id"] != expected_intent_id:
            current = _latest_audit_intent_locked(expected_intent_id)
        if (
            current is not None
            and current.get("prepared_artifact_digest") == expected_prepared_artifact_digest
        ):
            return current
        return None


def _audit_contains_intent_locked(intent: Mapping[str, Any]) -> bool:
    expected = _canonical_digest(dict(intent))
    return any(
        _canonical_digest(record["intent"]) == expected
        for record in _load_terminal_audit_records_locked()
    )


def _invalidate_unclaimed_locked(
    intent: Mapping[str, Any],
    *,
    now_ms: int,
) -> dict[str, Any]:
    migrated = _sparse_migration(
        intent,
        migration="LEGACY_INVALIDATED",
        migrated_at=now_ms,
        source_started=False,
    )
    _append_terminal_audit_locked(migrated, archived_at=now_ms)
    try:
        _atomic_write_json(intent_path(), migrated)
    except OSError:
        pass
    return migrated


def save_unclaimed_intent(
    payload: Mapping[str, Any],
    *,
    now_ms: int | None = None,
) -> None:
    active_now = int(time.time() * 1000) if now_ms is None else now_ms
    try:
        candidate = deepcopy(_validate_manual_intent(dict(payload)))
    except (TypeError, ValueError, IntentStateError) as exc:
        raise IntentStateError(
            "intent_invalid",
            "Manual mutation intent is invalid.",
        ) from exc
    if candidate["state"] != "UNCLAIMED" or not _nonnegative_integer(active_now):
        raise IntentStateError(
            "intent_invalid",
            "Only a valid UNCLAIMED manual mutation intent can be created.",
        )
    try:
        with _intent_lock():
            current = _effective_manual_intent_locked(now_ms=active_now)
            if any(
                record["intent"].get("intent_id") == candidate["intent_id"]
                for record in _load_terminal_audit_records_locked()
            ):
                raise IntentStateError(
                    "intent_conflict",
                    "Manual mutation intent ID was already archived.",
                )
            if (
                current is not None
                and current["state"] == "UNCLAIMED"
                and current["expires_at"] <= active_now
            ):
                current = _invalidate_unclaimed_locked(
                    current,
                    now_ms=active_now,
                )
            if current is not None:
                replaceable = current["state"] in _TERMINAL_STATES or (
                    current["state"] == "REVIEW_REQUIRED"
                    and current["reconciliation"]["status"]
                    in _RESOLVED_RECONCILIATION
                )
                if not replaceable or not _audit_contains_intent_locked(current):
                    raise IntentStateError(
                        "intent_conflict",
                        "Another manual mutation intent still owns the global slot.",
                    )
            _atomic_write_json(intent_path(), candidate)
    except IntentStateError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise IntentStateError(
            "intent_state_unavailable",
            "Manual mutation intent could not be saved safely.",
        ) from exc


def ensure_manual_intent_slot_available(*, now_ms: int | None = None) -> None:
    active_now = int(time.time() * 1000) if now_ms is None else now_ms
    if not _nonnegative_integer(active_now):
        raise IntentStateError("invalid_arguments", "now_ms is invalid.")
    with _intent_lock():
        current = _effective_manual_intent_locked(now_ms=active_now)
        if (
            current is not None
            and current["state"] == "UNCLAIMED"
            and current["expires_at"] <= active_now
        ):
            current = _invalidate_unclaimed_locked(
                current,
                now_ms=active_now,
            )
        if current is None:
            return
        replaceable = current["state"] in _TERMINAL_STATES or (
            current["state"] == "REVIEW_REQUIRED"
            and current["reconciliation"]["status"] in _RESOLVED_RECONCILIATION
        )
        if not replaceable or not _audit_contains_intent_locked(current):
            raise IntentStateError(
                "intent_conflict",
                "Another manual mutation intent still owns the global slot.",
            )


def _execution_grant_dict(value: Any) -> dict[str, Any]:
    required = (
        "grant_id",
        "state",
        "principal_id",
        "account_binding_id",
        "credential_fingerprint",
        "tool_name",
        "prepared_artifact_digest",
        "execution_binding_digest",
        "issued_at",
        "expires_at",
    )
    optional = ("claim_id", "terminal_state", "outcome_digest")
    if isinstance(value, Mapping):
        source = dict(value)
        result = {field: source.get(field) for field in required}
        result.update(
            {
                field: source[field]
                for field in optional
                if source.get(field) is not None
            }
        )
    else:
        result = {field: getattr(value, field, None) for field in required}
        result.update(
            {
                field: getattr(value, field)
                for field in optional
                if getattr(value, field, None) is not None
            }
        )
    if any(result.get(field) is None for field in required):
        raise IntentStateError(
            "grant_invalid",
            "Execution grant is incomplete.",
        )
    return result


def _grant_matches_intent(
    grant: Mapping[str, Any],
    intent: Mapping[str, Any],
) -> bool:
    profile = intent["profile_binding"]
    return (
        grant.get("state") == "ISSUED"
        and grant.get("principal_id") == profile["principal_id"]
        and grant.get("account_binding_id") == profile["account_binding_id"]
        and grant.get("credential_fingerprint")
        == profile["credential_fingerprint"]
        and grant.get("tool_name") == intent["tool_name"]
        and grant.get("prepared_artifact_digest")
        == intent["prepared_artifact_digest"]
        and grant.get("execution_binding_digest")
        == intent["execution_binding_digest"]
        and grant.get("expires_at") == intent["expires_at"]
        and isinstance(grant.get("grant_id"), str)
        and bool(grant["grant_id"])
        and _nonnegative_integer(grant.get("issued_at"))
        and grant["issued_at"] < grant["expires_at"]
    )


def confirm_and_issue_intent(
    *,
    expected_intent_id: str,
    expected_prepared_artifact_digest: str,
    execution_grant: Any,
    confirmed_at: int,
) -> dict[str, Any]:
    grant = _execution_grant_dict(execution_grant)
    if (
        not isinstance(expected_intent_id, str)
        or not expected_intent_id
        or not _is_sha256(expected_prepared_artifact_digest)
        or not _nonnegative_integer(confirmed_at)
    ):
        raise IntentStateError(
            "invalid_arguments",
            "Manual mutation confirmation binding is invalid.",
        )
    try:
        with _intent_lock():
            current = _effective_manual_intent_locked(now_ms=confirmed_at)
            if current is None:
                raise IntentStateError(
                    "intent_missing",
                    "No manual mutation intent was found.",
                )
            if (
                current["intent_id"] != expected_intent_id
                or current.get("prepared_artifact_digest")
                != expected_prepared_artifact_digest
            ):
                raise IntentStateError(
                    "intent_changed",
                    "Manual mutation intent changed before confirmation.",
                )
            if current["state"] == "CONFIRMED":
                if current["grant"] == grant:
                    return deepcopy(current)
                raise IntentStateError(
                    "confirmation_started",
                    "Manual mutation confirmation already issued another grant.",
                )
            if current["state"] != "UNCLAIMED":
                raise IntentStateError(
                    "confirmation_started",
                    "Manual mutation confirmation already started.",
                )
            if current["expires_at"] <= confirmed_at:
                raise IntentStateError(
                    "intent_expired",
                    "Manual mutation intent expired before confirmation.",
                )
            if not _grant_matches_intent(grant, current):
                raise IntentStateError(
                    "grant_invalid",
                    "Execution grant does not match the manual mutation intent.",
                )
            confirmed = deepcopy(current)
            confirmed["state"] = "CONFIRMED"
            confirmed["confirmed_at"] = confirmed_at
            confirmed["grant"] = grant
            _validate_manual_intent(confirmed)
            _atomic_write_json(intent_path(), confirmed)
            return confirmed
    except IntentStateError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise IntentStateError(
            "intent_state_unavailable",
            "Manual mutation confirmation could not be persisted atomically.",
        ) from exc


def reconciliation_template_for_intent(intent_id: str) -> dict[str, Any]:
    if not isinstance(intent_id, str) or not intent_id:
        raise IntentStateError(
            "invalid_arguments",
            "A manual mutation intent ID is required.",
        )
    with _intent_lock():
        current = _effective_manual_intent_locked(
            now_ms=int(time.time() * 1000)
        )
        if current is None or current["intent_id"] != intent_id:
            raise IntentStateError(
                "intent_missing",
                "Manual mutation intent was not found.",
            )
        template = current["reconciliation"].get("template")
        if not isinstance(template, dict):
            raise IntentStateError(
                "reconciliation_unavailable",
                "Manual mutation reconciliation requires manual review.",
            )
        return deepcopy(template)


def _default_runtime_models() -> Any:
    try:
        authorization = importlib.import_module(
            "weex_mcp.execution_authorization"
        )
        execution = importlib.import_module("weex_mcp.trade_execution")
        return SimpleNamespace(
            Precondition=execution.Precondition,
            PreparedMutation=execution.PreparedMutation,
            ExecutionGrant=authorization.ExecutionGrant,
            IssuedGrant=authorization.IssuedGrant,
            GrantClaim=authorization.GrantClaim,
            GrantTerminalReceipt=authorization.GrantTerminalReceipt,
            GrantRecovery=authorization.GrantRecovery,
            NOT_SENT_TERMINAL_OUTCOME_DIGEST=(
                authorization.NOT_SENT_TERMINAL_OUTCOME_DIGEST
            ),
            verify_prepared_mutation=execution.verify_prepared_mutation,
        )
    except (AttributeError, ImportError, ModuleNotFoundError) as exc:
        raise IntentStateError(
            "mcp_runtime_unavailable",
            "The fixed WEEX MCP grant runtime is unavailable.",
        ) from exc


def _rehydrate_prepared(
    intent: Mapping[str, Any],
    runtime: Any,
) -> Any:
    payload = deepcopy(intent["prepared_mutation"])
    precondition_payload = payload.pop("precondition")
    try:
        precondition = runtime.Precondition(**precondition_payload)
        prepared = runtime.PreparedMutation(
            **payload,
            precondition=precondition,
        )
        runtime.verify_prepared_mutation(prepared)
        return prepared
    except Exception as exc:
        raise IntentStateError(
            "intent_state_corrupt",
            "Stored PreparedMutation failed MCP validation.",
        ) from exc


def _rehydrate_grant(intent: Mapping[str, Any], runtime: Any) -> Any:
    try:
        return runtime.ExecutionGrant(**deepcopy(intent["grant"]))
    except Exception as exc:
        raise IntentStateError(
            "intent_state_corrupt",
            "Stored execution grant failed MCP validation.",
        ) from exc


def _grant_binding_matches(
    intent: Mapping[str, Any],
    *,
    grant_id: str,
    principal_id: str,
    account_binding_id: str,
    tool_name: str,
) -> bool:
    grant = intent.get("grant")
    return (
        isinstance(grant, dict)
        and grant.get("grant_id") == grant_id
        and grant.get("principal_id") == principal_id
        and grant.get("account_binding_id") == account_binding_id
        and grant.get("tool_name") == tool_name
    )


def _terminalized_intent(
    current: Mapping[str, Any],
    *,
    terminal_state: str,
    outcome_digest: str,
) -> dict[str, Any]:
    result = deepcopy(dict(current))
    result["state"] = terminal_state
    result["grant"]["state"] = terminal_state
    result["grant"]["terminal_state"] = terminal_state
    result["grant"]["outcome_digest"] = outcome_digest
    result["reconciliation"]["status"] = (
        "PENDING" if terminal_state == "REVIEW_REQUIRED" else "NOT_REQUIRED"
    )
    result["reconciliation"]["result"] = None
    result["reconciliation"]["observed_at"] = None
    result["reconciliation"]["evidence_digest"] = None
    return deepcopy(_validate_manual_intent(result, allow_previous_candidate=True))


class FileManualIntentStore:
    def __init__(
        self,
        *,
        runtime_loader: Callable[[], Any] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._runtime_loader = runtime_loader or _default_runtime_models
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._runtime: Any | None = None

    def _models(self) -> Any:
        if self._runtime is None:
            self._runtime = self._runtime_loader()
        return self._runtime

    def load(
        self,
        *,
        grant_id: str,
        principal_id: str,
        account_binding_id: str,
        tool_name: str,
    ) -> Any:
        with _intent_lock():
            current = _effective_manual_intent_locked(now_ms=self._clock())
            if (
                current is None
                or current["state"] != "CONFIRMED"
                or not _grant_binding_matches(
                    current,
                    grant_id=grant_id,
                    principal_id=principal_id,
                    account_binding_id=account_binding_id,
                    tool_name=tool_name,
                )
            ):
                raise IntentStateError(
                    "grant_unavailable",
                    "Issued manual execution grant is unavailable.",
                )
            runtime = self._models()
            prepared = _rehydrate_prepared(current, runtime)
            grant = _rehydrate_grant(current, runtime)
            try:
                return runtime.IssuedGrant(
                    grant=grant,
                    prepared_mutation=prepared,
                    confirmed_at=current["confirmed_at"],
                )
            except Exception as exc:
                raise IntentStateError(
                    "intent_state_corrupt",
                    "Stored issued grant failed MCP validation.",
                ) from exc

    def claim(
        self,
        *,
        grant_id: str,
        principal_id: str,
        account_binding_id: str,
        tool_name: str,
        prepared_artifact_digest: str,
        execution_binding_digest: str,
        now: int,
    ) -> Any:
        if not _nonnegative_integer(now):
            raise IntentStateError("invalid_arguments", "Grant claim time is invalid.")
        with _intent_lock():
            current = _effective_manual_intent_locked(now_ms=now)
            grant = current.get("grant") if isinstance(current, dict) else None
            if (
                current is None
                or current["state"] != "CONFIRMED"
                or not _grant_binding_matches(
                    current,
                    grant_id=grant_id,
                    principal_id=principal_id,
                    account_binding_id=account_binding_id,
                    tool_name=tool_name,
                )
                or grant.get("prepared_artifact_digest")
                != prepared_artifact_digest
                or grant.get("execution_binding_digest")
                != execution_binding_digest
            ):
                raise IntentStateError(
                    "grant_unavailable",
                    "Manual execution grant cannot be claimed.",
                )
            runtime = self._models()
            prepared = _rehydrate_prepared(current, runtime)
            grant_model = _rehydrate_grant(current, runtime)
            try:
                issued = runtime.IssuedGrant(
                    grant=grant_model,
                    prepared_mutation=prepared,
                    confirmed_at=current["confirmed_at"],
                )
            except Exception as exc:
                raise IntentStateError(
                    "intent_state_corrupt",
                    "Stored issued grant failed MCP validation during claim.",
                ) from exc
            if (
                issued.prepared_mutation.prepared_artifact_digest
                != prepared_artifact_digest
                or issued.prepared_mutation.execution_binding_digest
                != execution_binding_digest
            ):
                raise IntentStateError(
                    "grant_unavailable",
                    "Manual execution grant digests do not match the stored artifact.",
                )
            if grant["expires_at"] <= now or current["expires_at"] <= now:
                if (
                    runtime.NOT_SENT_TERMINAL_OUTCOME_DIGEST
                    != NOT_SENT_TERMINAL_OUTCOME_DIGEST
                ):
                    raise IntentStateError(
                        "mcp_runtime_incompatible",
                        "The MCP not-sent terminal digest is incompatible.",
                    )
                receipt = runtime.GrantTerminalReceipt(
                    grant_id=grant_id,
                    state="NOT_SENT_TERMINAL",
                    terminal_state="NOT_SENT_TERMINAL",
                    resolved_at=now,
                    reason="grant_expired",
                    outcome_digest=runtime.NOT_SENT_TERMINAL_OUTCOME_DIGEST,
                )
                terminal = _terminalized_intent(
                    current,
                    terminal_state="NOT_SENT_TERMINAL",
                    outcome_digest=NOT_SENT_TERMINAL_OUTCOME_DIGEST,
                )
                _append_terminal_audit_locked(terminal, archived_at=now)
                try:
                    _atomic_write_json(intent_path(), terminal)
                except OSError:
                    pass
                return receipt
            claim_id = "claim_" + uuid.uuid4().hex
            claim_result = runtime.GrantClaim(
                grant_id=grant_id,
                claim_id=claim_id,
                state="CLAIMED",
                claimed_at=now,
            )
            claimed = deepcopy(current)
            claimed["state"] = "CLAIMED"
            claimed["grant"]["state"] = "CLAIMED"
            claimed["grant"]["claim_id"] = claim_id
            _validate_manual_intent(claimed)
            _atomic_write_json(intent_path(), claimed)
            return claim_result

    def finalize(
        self,
        *,
        grant_id: str,
        principal_id: str,
        account_binding_id: str,
        tool_name: str,
        claim_id: str,
        terminal_state: str,
        outcome_digest: str,
    ) -> None:
        if terminal_state not in {
            "NOT_SENT_TERMINAL",
            "DEFINITIVE_REJECTION",
            "ACCEPTED",
            "REVIEW_REQUIRED",
        } or not _is_sha256(outcome_digest):
            raise IntentStateError(
                "invalid_arguments",
                "Grant terminal state or outcome digest is invalid.",
            )
        if (
            terminal_state == "NOT_SENT_TERMINAL"
            and outcome_digest != NOT_SENT_TERMINAL_OUTCOME_DIGEST
        ):
            raise IntentStateError(
                "invalid_arguments",
                "Not-sent terminal outcome digest is invalid.",
            )
        archived_at = self._clock()
        with _intent_lock():
            current = _effective_manual_intent_locked(now_ms=archived_at)
            if (
                current is None
                or current.get("contract_versions") != _INTENT_CONTRACT_VERSIONS
                or not _grant_binding_matches(
                    current,
                    grant_id=grant_id,
                    principal_id=principal_id,
                    account_binding_id=account_binding_id,
                    tool_name=tool_name,
                )
            ):
                raise IntentStateError(
                    "grant_unavailable",
                    "Claimed manual execution grant is unavailable.",
                )
            grant = current["grant"]
            if grant.get("claim_id") != claim_id:
                raise IntentStateError(
                    "grant_unavailable",
                    "Manual execution grant claim does not match.",
                )
            if current["state"] in {
                "NOT_SENT_TERMINAL",
                "DEFINITIVE_REJECTION",
                "ACCEPTED",
                "REVIEW_REQUIRED",
            }:
                if (
                    current["state"] == terminal_state
                    and grant.get("outcome_digest") == outcome_digest
                ):
                    return None
                raise IntentStateError(
                    "grant_terminal_conflict",
                    "Manual execution grant already has a conflicting terminal result.",
                )
            if (
                current["state"] != "CLAIMED"
            ):
                raise IntentStateError(
                    "grant_unavailable",
                    "Manual execution grant claim does not match.",
                )
            terminal = _terminalized_intent(
                current,
                terminal_state=terminal_state,
                outcome_digest=outcome_digest,
            )
            if terminal_state == "REVIEW_REQUIRED":
                _atomic_write_json(intent_path(), terminal)
            else:
                _append_terminal_audit_locked(
                    terminal,
                    archived_at=archived_at,
                )
                try:
                    _atomic_write_json(intent_path(), terminal)
                except OSError:
                    pass
            return None

    def recover(
        self,
        *,
        grant_id: str,
        principal_id: str,
        account_binding_id: str,
        tool_name: str,
        now: int,
    ) -> Any:
        if not _nonnegative_integer(now):
            raise IntentStateError(
                "invalid_arguments",
                "Grant recovery time is invalid.",
            )
        with _intent_lock():
            current = _effective_manual_intent_locked(now_ms=now)
            if (
                current is None
                or current["state"] != "CLAIMED"
                or not _grant_binding_matches(
                    current,
                    grant_id=grant_id,
                    principal_id=principal_id,
                    account_binding_id=account_binding_id,
                    tool_name=tool_name,
                )
            ):
                raise IntentStateError(
                    "grant_unavailable",
                    "Claimed manual execution grant is unavailable for recovery.",
                )
            review = _terminalized_intent(
                current,
                terminal_state="REVIEW_REQUIRED",
                outcome_digest=_REVIEW_REQUIRED_OUTCOME_DIGEST,
            )
            recovery = self._models().GrantRecovery(
                grant_id=grant_id,
                state="REVIEW_REQUIRED",
                recovered_at=now,
                reason="unfinalized_claim",
            )
            _atomic_write_json(intent_path(), review)
            return recovery

    def record_reconciliation_result(
        self,
        intent_id: str,
        *,
        status: str,
        evidence_digest: str,
        observed_at: int,
    ) -> dict[str, Any]:
        return _record_reconciliation_result(
            intent_id,
            status=status,
            evidence_digest=evidence_digest,
            observed_at=observed_at,
        )


def execution_grant_for_dispatch(
    intent_id: str,
    *,
    runtime_loader: Callable[[], Any] | None = None,
    now_ms: int | None = None,
) -> Any:
    active_now = int(time.time() * 1000) if now_ms is None else now_ms
    with _intent_lock():
        current = _effective_manual_intent_locked(now_ms=active_now)
        if (
            current is None
            or current["intent_id"] != intent_id
            or current["state"] != "CONFIRMED"
        ):
            raise IntentStateError(
                "grant_unavailable",
                "Issued manual execution grant is unavailable for dispatch.",
            )
        runtime = (runtime_loader or _default_runtime_models)()
        return _rehydrate_grant(current, runtime)


def claimed_execution_grant_for_recovery(
    intent_id: str,
    *,
    runtime_loader: Callable[[], Any] | None = None,
    now_ms: int | None = None,
) -> Any:
    active_now = int(time.time() * 1000) if now_ms is None else now_ms
    with _intent_lock():
        current = _effective_manual_intent_locked(now_ms=active_now)
        if (
            current is None
            or current["intent_id"] != intent_id
            or current["state"] != "CLAIMED"
        ):
            raise IntentStateError(
                "grant_unavailable",
                "Claimed manual execution grant is unavailable for recovery.",
            )
        runtime = (runtime_loader or _default_runtime_models)()
        return _rehydrate_grant(current, runtime)


def _record_reconciliation_result(
    intent_id: str,
    *,
    status: str,
    evidence_digest: str,
    observed_at: int,
) -> dict[str, Any]:
    if (
        not isinstance(intent_id, str)
        or not intent_id
        or status not in ("RESOLVED_ACCEPTED", "UNRESOLVED", "MANUAL_REVIEW_REQUIRED")
        or not _is_sha256(evidence_digest)
        or not _nonnegative_integer(observed_at)
    ):
        raise IntentStateError(
            "invalid_arguments",
            "Manual mutation reconciliation result is invalid.",
        )
    with _intent_lock():
        current = _effective_manual_intent_locked(now_ms=observed_at)
        if (
            current is None
            or current["intent_id"] != intent_id
            or current["state"] != "REVIEW_REQUIRED"
        ):
            raise IntentStateError(
                "reconciliation_conflict",
                "Manual mutation intent is not awaiting reconciliation.",
            )
        current_status = current["reconciliation"]["status"]
        if current_status in _RESOLVED_RECONCILIATION:
            if (
                current_status == status
                and current["reconciliation"]["evidence_digest"]
                == evidence_digest
                and current["reconciliation"]["observed_at"] == observed_at
            ):
                return deepcopy(current)
            raise IntentStateError(
                "reconciliation_conflict",
                "Manual mutation reconciliation is already resolved.",
            )
        updated = deepcopy(current)
        updated["reconciliation"].update(
            {
                "status": status,
                "result": "ACCEPTED" if status == "RESOLVED_ACCEPTED" else None,
                "observed_at": observed_at,
                "evidence_digest": evidence_digest,
            }
        )
        _validate_manual_intent(updated, allow_previous_candidate=True)
        if status in _RESOLVED_RECONCILIATION:
            _append_terminal_audit_locked(updated, archived_at=observed_at)
            try:
                _atomic_write_json(intent_path(), updated)
            except OSError:
                pass
        else:
            _atomic_write_json(intent_path(), updated)
        return updated
