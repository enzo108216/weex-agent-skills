#!/usr/bin/env python3
"""Profile-bound embedded MCP gateway for WEEX Trader internals.

This module intentionally has no import-time dependency on ``weex-mcp``.  The
runtime package is loaded and contract-checked only when a gateway is built.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import math
import re
import secrets
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


MAPPING_PATH = Path(__file__).resolve().parent.parent / "references" / "mcp-capability-mapping.json"
EXPECTED_MAPPING_VERSION = "trader-mcp-capability-v2"
LOCAL_RUNTIME_PROFILE = "local_user"
DEFAULT_SPOT_BASE_URL = "https://api-spot.weex.com"
DEFAULT_FUTURES_BASE_URL = "https://api-contract.weex.com"
_PACKAGE_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]{0,8}))?"
    r"(?:\.post(?:0|[1-9][0-9]{0,8}))?"
    r"(?:\.dev(?:0|[1-9][0-9]{0,8}))?$"
)
_CONTRACT_VERSION_PATTERN = re.compile(r"^(?:0|[1-9][0-9]{0,8})$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROBE_ACTIONS = frozenset(
    {
        "install_verified_mcp_candidate",
        "repair_mcp_mapping",
        "repair_mcp_runtime",
        "repair_mcp_contract",
    }
)
_SUPPORTED_PERMISSIONS = frozenset({"USER_DATA", "TRADE"})
_CALLER_FORBIDDEN_ARGUMENTS = frozenset({"credential", "endpoint", "path", "method"})
_CREDENTIAL_FINGERPRINT_DOMAIN = b"weex-trader-vault-credential-v1\0"
_APPROVAL_SECRET_DOMAIN = b"weex-trader-local-mcp-approval-v1\0"
_FINGERPRINT_KEY_DOMAIN = b"weex-trader-local-mcp-fingerprint-v1\0"
_DELEGATED_AUTHORIZATION_CODES = frozenset(
    {
        "execution_authorization_invalid",
        "execution_authorization_denied",
        "execution_authorization_expired",
    }
)


class McpGatewayError(RuntimeError):
    """Stable, non-sensitive failure raised before returning a business result."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _McpRuntime:
    package_version: str
    build_local_user_service: Any
    describe_request: Any
    Settings: Any
    TenantContext: Any
    ResolvedWeexCredential: Any
    SecretProviderError: Any
    ExecutionAuthorizationError: Any
    VerifiedExecutionAuthorization: Any


def _load_default_runtime(expected_version: str) -> _McpRuntime:
    try:
        installed_version = metadata.version("weex-mcp")
    except metadata.PackageNotFoundError as exc:
        raise McpGatewayError(
            "mcp_package_missing",
            "The required WEEX MCP package is not installed for this interpreter.",
        ) from exc
    if installed_version != expected_version:
        raise McpGatewayError(
            "mcp_version_mismatch",
            "The installed WEEX MCP package version does not match the verified contract.",
        )
    try:
        local_module = importlib.import_module("weex_mcp.local")
        settings_module = importlib.import_module("weex_mcp.settings")
        tenant_module = importlib.import_module("weex_mcp.tenant_context")
        secret_module = importlib.import_module("weex_mcp.secret_provider")
        authorization_module = importlib.import_module(
            "weex_mcp.execution_authorization"
        )
        runtime = _McpRuntime(
            package_version=installed_version,
            build_local_user_service=local_module.build_local_user_service,
            describe_request=local_module.describe_request,
            Settings=settings_module.Settings,
            TenantContext=tenant_module.TenantContext,
            ResolvedWeexCredential=secret_module.ResolvedWeexCredential,
            SecretProviderError=secret_module.SecretProviderError,
            ExecutionAuthorizationError=authorization_module.ExecutionAuthorizationError,
            VerifiedExecutionAuthorization=authorization_module.VerifiedExecutionAuthorization,
        )
    except Exception as exc:
        raise McpGatewayError(
            "mcp_runtime_incompatible",
            "The installed WEEX MCP package does not expose the required public Local runtime API.",
        ) from exc
    return runtime


def _load_default_mapping() -> dict[str, Any]:
    try:
        value = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpGatewayError(
            "mcp_mapping_invalid",
            "The Trader MCP capability mapping is unavailable or invalid.",
        ) from exc
    if not isinstance(value, dict):
        raise McpGatewayError(
            "mcp_mapping_invalid",
            "The Trader MCP capability mapping is unavailable or invalid.",
        )
    return value


def _profile_store_function(name: str) -> Any:
    try:
        module = importlib.import_module("weex_profile_store")
        value = getattr(module, name)
    except (AttributeError, ImportError, ModuleNotFoundError) as exc:
        raise McpGatewayError(
            "profile_runtime_unavailable",
            "The Trader profile and Vault runtime is unavailable.",
        ) from exc
    return value


def _default_profile_loader(profile_id: str) -> Any:
    return _profile_store_function("get_profile_by_id")(profile_id)


def _default_credential_loader(profile_id: str) -> Any:
    return _profile_store_function("load_profile_credentials")(profile_id)


def _default_approval_key_loader() -> str:
    return _profile_store_function("get_or_create_local_mcp_approval_key")()


def _default_approval_key_status_loader() -> dict[str, Any]:
    return _profile_store_function("local_mcp_approval_key_status")()


def _canonical_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise McpGatewayError(
            "mcp_contract_mismatch",
            "The WEEX MCP compatibility contract is not canonical JSON.",
        ) from exc
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _safe_hmac(key: bytes, domain: bytes, value: str) -> bytes:
    return hmac.new(key, domain + value.encode("utf-8"), hashlib.sha256).digest()


def _new_bound_ref() -> str:
    return "weex_ref_" + secrets.token_urlsafe(32)


def _approval_key_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _constant_time_text_equal(left: Any, right: str) -> bool:
    return isinstance(left, str) and hmac.compare_digest(
        left.encode("utf-8"),
        right.encode("utf-8"),
    )


def _profile_id_from_value(value: Any) -> str | None:
    if isinstance(value, Mapping):
        raw = value.get("profile_id") or value.get("id")
    else:
        raw = getattr(value, "profile_id", None)
    return raw if isinstance(raw, str) and raw else None


def _safe_package_version(value: Any) -> str | None:
    if not isinstance(value, str) or _PACKAGE_VERSION_PATTERN.fullmatch(value) is None:
        return None
    return value


def _safe_contract_version(value: Any) -> str | None:
    if not isinstance(value, str) or _CONTRACT_VERSION_PATTERN.fullmatch(value) is None:
        return None
    return value


def _safe_digest(value: Any) -> str | None:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        return None
    return value


def _runtime_symbols_are_valid(runtime: Any) -> bool:
    required = (
        "package_version",
        "build_local_user_service",
        "describe_request",
        "Settings",
        "TenantContext",
        "ResolvedWeexCredential",
        "SecretProviderError",
        "ExecutionAuthorizationError",
        "VerifiedExecutionAuthorization",
    )
    return all(getattr(runtime, name, None) is not None for name in required)


def _select_verified_candidate(
    mapping: Any,
    *,
    trade_enabled: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(mapping, dict) or mapping.get("mapping_version") != EXPECTED_MAPPING_VERSION:
        raise McpGatewayError(
            "mcp_mapping_invalid",
            "The Trader MCP capability mapping version is not supported.",
        )
    candidate = mapping.get("verified_mcp_candidate")
    if not isinstance(candidate, dict):
        raise McpGatewayError(
            "mcp_mapping_invalid",
            "The verified WEEX MCP candidate is missing from the capability mapping.",
        )
    package_version = candidate.get("package_version")
    tool_contract_version = candidate.get("tool_contract_version")
    profiles = candidate.get("compatibility_profiles")
    if (
        _safe_package_version(package_version) is None
        or _safe_contract_version(tool_contract_version) is None
        or not isinstance(profiles, list)
    ):
        raise McpGatewayError(
            "mcp_mapping_invalid",
            "The verified WEEX MCP candidate contract is invalid.",
        )

    seen: set[tuple[str, bool]] = set()
    selected: list[dict[str, Any]] = []
    for item in profiles:
        if not isinstance(item, dict):
            raise McpGatewayError("mcp_mapping_invalid", "The WEEX MCP profile contract is invalid.")
        profile = item.get("runtime_profile")
        enabled = item.get("trade_enabled")
        if not isinstance(profile, str) or profile not in {"local_user", "broker_server"} or not isinstance(enabled, bool):
            raise McpGatewayError("mcp_mapping_invalid", "The WEEX MCP profile contract is invalid.")
        key = (profile, enabled)
        if key in seen:
            raise McpGatewayError("mcp_mapping_invalid", "The WEEX MCP profile contract is invalid.")
        seen.add(key)
        if (
            isinstance(item.get("tool_count"), bool)
            or not isinstance(item.get("tool_count"), int)
            or item["tool_count"] <= 0
            or not all(
                isinstance(item.get(field), str)
                and _DIGEST_PATTERN.fullmatch(str(item[field])) is not None
                for field in ("tool_set_digest", "schema_digest", "manifest_digest")
            )
        ):
            raise McpGatewayError("mcp_mapping_invalid", "The WEEX MCP profile contract is invalid.")
        if profile == LOCAL_RUNTIME_PROFILE and enabled is trade_enabled:
            selected.append(item)
    expected_pairs = {
        ("local_user", False),
        ("local_user", True),
        ("broker_server", False),
        ("broker_server", True),
    }
    if seen != expected_pairs or len(selected) != 1:
        raise McpGatewayError(
            "mcp_mapping_invalid",
            "The verified WEEX MCP profile matrix is incomplete.",
        )
    return candidate, dict(selected[0])


def describe_local_mcp_request(
    *,
    name: str,
    arguments: Mapping[str, Any],
    effective_spot_base_url: str,
    effective_futures_base_url: str,
    trade_enabled: bool,
    timeout_seconds: float,
    request_locale: str = "en-US",
    runtime_loader: Callable[[str], Any] | None = None,
    mapping_loader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Describe a typed Local MCP request without building a service or reading secrets."""

    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string.")
    if not isinstance(arguments, Mapping):
        raise ValueError("arguments must be an object.")
    if not isinstance(trade_enabled, bool):
        raise ValueError("trade_enabled must be a boolean.")
    if not isinstance(effective_spot_base_url, str) or not effective_spot_base_url:
        raise ValueError("effective_spot_base_url must be a non-empty string.")
    if not isinstance(effective_futures_base_url, str) or not effective_futures_base_url:
        raise ValueError("effective_futures_base_url must be a non-empty string.")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number.")
    if not isinstance(request_locale, str) or not request_locale:
        raise ValueError("request_locale must be a non-empty string.")

    try:
        mapping = (mapping_loader or _load_default_mapping)()
        candidate, expected_manifest = _select_verified_candidate(
            mapping,
            trade_enabled=trade_enabled,
        )
        expected_version = str(candidate["package_version"])
        runtime = (runtime_loader or _load_default_runtime)(expected_version)
    except McpGatewayError:
        raise
    except Exception as exc:
        raise McpGatewayError(
            "mcp_runtime_incompatible",
            "The WEEX MCP request-description runtime is unavailable.",
        ) from exc
    if not _runtime_symbols_are_valid(runtime):
        raise McpGatewayError(
            "mcp_runtime_incompatible",
            "The installed WEEX MCP package does not expose the required public Local runtime API.",
        )
    if runtime.package_version != expected_version:
        raise McpGatewayError(
            "mcp_version_mismatch",
            "The installed WEEX MCP package version does not match the verified contract.",
        )

    try:
        settings = runtime.Settings(
            trade_enabled=trade_enabled,
            spot_base_url=effective_spot_base_url,
            futures_base_url=effective_futures_base_url,
            market_timeout_seconds=float(timeout_seconds),
            market_max_attempts=1,
            market_retry_base_delay_seconds=0.0,
            account_timeout_seconds=float(timeout_seconds),
            partner_timeout_seconds=float(timeout_seconds),
            request_locale=request_locale,
        )
        description = runtime.describe_request(
            settings=settings,
            name=name,
            arguments=dict(arguments),
        )
    except McpGatewayError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None)
        if isinstance(code, str) and code and isinstance(message, str) and message:
            raise McpGatewayError(code, message) from exc
        raise McpGatewayError(
            "mcp_request_description_failed",
            "The verified WEEX MCP request-description API failed.",
        ) from exc
    return _validate_request_description(
        description,
        name=name,
        arguments=arguments,
        candidate=candidate,
        expected_manifest=expected_manifest,
        trade_enabled=trade_enabled,
        effective_spot_base_url=effective_spot_base_url,
        effective_futures_base_url=effective_futures_base_url,
    )


def _validate_request_description(
    value: Any,
    *,
    name: str,
    arguments: Mapping[str, Any],
    candidate: Mapping[str, Any],
    expected_manifest: Mapping[str, Any],
    trade_enabled: bool,
    effective_spot_base_url: str,
    effective_futures_base_url: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise McpGatewayError(
            "mcp_contract_mismatch",
            "The WEEX MCP request description is invalid.",
        )
    method = value.get("method")
    url = value.get("url")
    headers = value.get("headers")
    query = value.get("query")
    body = value.get("body")
    metadata_value = value.get("_meta")
    weex_metadata = (
        metadata_value.get("com.weex/mcp")
        if isinstance(metadata_value, dict)
        else None
    )
    manifest = (
        weex_metadata.get("compatibility")
        if isinstance(weex_metadata, dict)
        else None
    )
    if (
        method not in {"GET", "POST", "DELETE"}
        or not isinstance(url, str)
        or not url.startswith("https://")
        or not isinstance(headers, dict)
        or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in headers.items()
        )
        or not isinstance(query, dict)
        or not isinstance(body, dict)
        or not isinstance(manifest, dict)
    ):
        raise McpGatewayError(
            "mcp_contract_mismatch",
            "The WEEX MCP request description is invalid.",
        )
    parsed_url = urlsplit(url)
    described_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    market = str(arguments.get("market") or "").strip().lower()
    expected_origin = (
        effective_spot_base_url.rstrip("/")
        if market == "spot"
        else effective_futures_base_url.rstrip("/")
        if market in {"contract", "futures"}
        else None
    )
    auth_header_placeholders = {
        "access-key": "***",
        "access-passphrase": "***",
        "access-timestamp": "<generated-at-send>",
        "access-sign": "***",
    }
    private_request = name.startswith("account_") or name.startswith(
        "trade_preview_"
    )
    normalized_headers: dict[str, str] = {}
    duplicate_header = False
    for key, item in headers.items():
        normalized_key = key.casefold()
        if normalized_key in normalized_headers:
            duplicate_header = True
        normalized_headers[normalized_key] = item
    expected_header_names = {
        "accept",
        "content-type",
        "locale",
        "user-agent",
        *(auth_header_placeholders if private_request else ()),
    }
    if (
        expected_origin is None
        or described_origin != expected_origin
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.fragment
        or duplicate_header
        or set(normalized_headers) != expected_header_names
        or (
            private_request
            and any(
                normalized_headers.get(key) != item
                for key, item in auth_header_placeholders.items()
            )
        )
    ):
        raise McpGatewayError(
            "mcp_contract_mismatch",
            "The WEEX MCP request description violates the Local route or redaction contract.",
        )
    manifest_payload = dict(manifest)
    stored_manifest_digest = manifest_payload.pop("manifest_digest", None)
    expected_modules = [
        "account",
        "market",
        "partner",
        *(["trade"] if trade_enabled else []),
    ]
    expected_values = {
        "package_version": candidate["package_version"],
        "tool_contract_version": candidate["tool_contract_version"],
        "runtime_profile": LOCAL_RUNTIME_PROFILE,
        "trade_enabled": trade_enabled,
        "credential_modes": ["credential_ref"],
        "delegated_authorization": trade_enabled,
        "modules": expected_modules,
    }
    if (
        not isinstance(stored_manifest_digest, str)
        or _DIGEST_PATTERN.fullmatch(stored_manifest_digest) is None
        or _canonical_digest(manifest_payload) != stored_manifest_digest
        or any(
            manifest.get(field) != item for field, item in expected_values.items()
        )
        or not isinstance(manifest.get("tools"), list)
        or len(manifest["tools"]) != expected_manifest["tool_count"]
        or any(
            manifest.get(field) != expected_manifest[field]
            for field in ("tool_set_digest", "schema_digest", "manifest_digest")
        )
    ):
        raise McpGatewayError(
            "mcp_contract_mismatch",
            "The WEEX MCP request-description contract does not match Trader.",
        )
    return {
        "method": method,
        "url": url,
        "headers": dict(headers),
        "query": dict(query),
        "body": dict(body),
    }


class VaultSecretProvider:
    """Resolve one profile-bound credential reference from the existing Vault."""

    def __init__(
        self,
        *,
        profile_id: str,
        bound_ref: str,
        expected_user_id: str,
        fingerprint_key: bytes,
        credential_loader: Callable[[str], Any],
        runtime: Any,
    ) -> None:
        if not all(isinstance(value, str) and value for value in (profile_id, bound_ref, expected_user_id)):
            raise ValueError("VaultSecretProvider requires non-empty binding values.")
        if not isinstance(fingerprint_key, bytes) or len(fingerprint_key) < 32:
            raise ValueError("VaultSecretProvider fingerprint key must contain at least 32 bytes.")
        self._profile_id = profile_id
        self._bound_ref = bound_ref
        self._expected_user_id = expected_user_id
        self._fingerprint_key = fingerprint_key
        self._credential_loader = credential_loader
        self._runtime = runtime

    def _error(self, code: str, message: str) -> Exception:
        return self._runtime.SecretProviderError(code, message)

    def resolve(
        self,
        *,
        credential_argument: dict[str, Any],
        context: Any,
        required_permission: str,
    ) -> Any:
        if not isinstance(credential_argument, dict) or credential_argument.get("type") != "credential_ref":
            raise self._error("credential_invalid", "Credential provider rejected the credential context.")
        key_id = credential_argument.get("key_id")
        ref = credential_argument.get("ref")
        if not _constant_time_text_equal(key_id, self._profile_id):
            raise self._error("credential_invalid", "Credential provider rejected the credential context.")
        if not _constant_time_text_equal(ref, self._bound_ref):
            raise self._error("credential_ref_not_found", "Credential reference was not found.")
        user_id = getattr(context, "user_id", None)
        if not _constant_time_text_equal(user_id, self._expected_user_id):
            raise self._error("permission_denied", "Credential does not allow the required permission.")
        if required_permission not in _SUPPORTED_PERMISSIONS:
            raise self._error("permission_denied", "Credential does not allow the required permission.")

        try:
            credentials = self._credential_loader(self._profile_id)
            api_key = getattr(credentials, "api_key")
            api_secret = getattr(credentials, "api_secret")
            api_passphrase = getattr(credentials, "api_passphrase")
        except Exception as exc:
            raise self._error(
                "secret_provider_unconfigured",
                "Secret provider is not configured for private WEEX API calls.",
            ) from exc
        if not all(isinstance(value, str) and value for value in (api_key, api_secret, api_passphrase)):
            raise self._error("credential_invalid", "Credential provider rejected the credential context.")
        canonical = json.dumps(
            {
                "profile_id": self._profile_id,
                "key_id": key_id,
                "api_key": api_key,
                "api_secret": api_secret,
                "api_passphrase": api_passphrase,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = "fp_" + hmac.new(
            self._fingerprint_key,
            _CREDENTIAL_FINGERPRINT_DOMAIN + canonical,
            hashlib.sha256,
        ).hexdigest()[:32]
        return self._runtime.ResolvedWeexCredential(
            key_id=self._profile_id,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            permissions=frozenset(_SUPPORTED_PERMISSIONS),
            fingerprint=fingerprint,
        )


class _PublicOnlySecretProvider:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def resolve(self, **_: Any) -> Any:
        raise self._runtime.SecretProviderError(
            "secret_provider_unconfigured",
            "Secret provider is not configured for private WEEX API calls.",
        )


class _CallbackExecutionAuthorizer:
    """Bridge a neutral Trader callback to the installed MCP authorization SPI."""

    def __init__(self, *, callback: Callable[[dict[str, Any]], dict[str, Any]], runtime: Any) -> None:
        self._callback = callback
        self._runtime = runtime

    def _error(self, code: str) -> Exception:
        messages = {
            "execution_authorization_invalid": "Delegated execution authorization is invalid.",
            "execution_authorization_denied": "Delegated execution authorization was denied.",
            "execution_authorization_expired": "Delegated execution authorization has expired.",
        }
        return self._runtime.ExecutionAuthorizationError(code, messages[code])

    def authorize(
        self,
        *,
        context: Any,
        credential_fingerprint: str,
        execution: Any,
        delegated_ref: dict[str, Any],
    ) -> Any:
        try:
            payload = _delegated_authorization_payload(
                context=context,
                credential_fingerprint=credential_fingerprint,
                execution=execution,
                delegated_ref=delegated_ref,
            )
        except (TypeError, ValueError):
            raise self._error("execution_authorization_invalid") from None
        try:
            result = self._callback(payload)
        except Exception as exc:
            raw_code = getattr(exc, "code", None)
            code = (
                raw_code
                if isinstance(raw_code, str)
                and raw_code in _DELEGATED_AUTHORIZATION_CODES
                else "execution_authorization_denied"
            )
            raise self._error(code) from None
        if not isinstance(result, dict):
            raise self._error("execution_authorization_invalid")
        try:
            return self._runtime.VerifiedExecutionAuthorization(
                authorization_id=result["authorization_id"],
                scope_fingerprint=result["scope_fingerprint"],
                expires_at=result["expires_at"],
                audit_metadata=result["audit_metadata"],
            )
        except (KeyError, TypeError, ValueError):
            raise self._error("execution_authorization_invalid") from None


def _delegated_authorization_payload(
    *,
    context: Any,
    credential_fingerprint: Any,
    execution: Any,
    delegated_ref: Any,
) -> dict[str, Any]:
    context_payload = {
        "tenant_id": getattr(context, "tenant_id", None),
        "user_id": getattr(context, "user_id", None),
        "session_id": getattr(context, "session_id", None),
        "request_id": getattr(context, "request_id", None),
    }
    if not all(isinstance(value, str) and value for value in context_payload.values()):
        raise ValueError("authorization context is invalid")
    if not isinstance(credential_fingerprint, str) or not credential_fingerprint:
        raise ValueError("credential fingerprint is invalid")
    if (
        not isinstance(delegated_ref, dict)
        or set(delegated_ref) != {"ref"}
        or not isinstance(delegated_ref.get("ref"), str)
        or not delegated_ref["ref"]
    ):
        raise ValueError("delegated ref is invalid")
    execution_payload = {
        "action": getattr(execution, "action", None),
        "market": getattr(execution, "market", None),
        "trading_mode": getattr(execution, "trading_mode", None),
        "summary": getattr(execution, "summary", None),
        "risk": getattr(execution, "risk", None),
        "risk_signature": getattr(execution, "risk_signature", None),
    }
    if (
        not all(
            isinstance(execution_payload[field], str) and execution_payload[field]
            for field in ("action", "market", "trading_mode", "risk_signature")
        )
        or not isinstance(execution_payload["summary"], dict)
        or not isinstance(execution_payload["risk"], dict)
    ):
        raise ValueError("execution authorization payload is invalid")
    return {
        "context": context_payload,
        "credential_fingerprint": credential_fingerprint,
        "execution": execution_payload,
        "delegated_ref": dict(delegated_ref),
    }


class LocalMcpGateway:
    """One immutable Local User composition for caller-resolved effective routes.

    The caller owns the existing explicit-override/profile/environment/default
    precedence.  This Gateway binds the already-resolved Spot and Futures
    origins together with one profile and one Trade mode for its full lifetime.
    """

    def __init__(
        self,
        *,
        profile_id: str | None,
        effective_spot_base_url: str,
        effective_futures_base_url: str,
        trade_enabled: bool,
        timeout_seconds: float,
        request_locale: str = "en-US",
        runtime_loader: Callable[[str], Any] | None = None,
        mapping_loader: Callable[[], dict[str, Any]] | None = None,
        profile_loader: Callable[[str], Any] | None = None,
        credential_loader: Callable[[str], Any] | None = None,
        approval_key_loader: Callable[[], str] | None = None,
        approval_key_status_loader: Callable[[], dict[str, Any]] | None = None,
        delegated_authorizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        if profile_id is not None and (not isinstance(profile_id, str) or not profile_id):
            raise ValueError("profile_id must be a non-empty string or None.")
        if not isinstance(effective_spot_base_url, str) or not effective_spot_base_url:
            raise ValueError("effective_spot_base_url must be a non-empty string.")
        if not isinstance(effective_futures_base_url, str) or not effective_futures_base_url:
            raise ValueError("effective_futures_base_url must be a non-empty string.")
        if not isinstance(trade_enabled, bool):
            raise ValueError("trade_enabled must be a boolean.")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number.")
        if not isinstance(request_locale, str) or not request_locale:
            raise ValueError("request_locale must be a non-empty string.")
        if trade_enabled and profile_id is None:
            raise McpGatewayError("profile_required", "A saved profile is required for private MCP tools.")
        if delegated_authorizer is not None and not callable(delegated_authorizer):
            raise ValueError("delegated_authorizer must be callable or None.")
        if delegated_authorizer is not None and not trade_enabled:
            raise ValueError("delegated_authorizer requires trade_enabled=True.")

        try:
            mapping = (mapping_loader or _load_default_mapping)()
        except McpGatewayError:
            raise
        except Exception as exc:
            raise McpGatewayError(
                "mcp_mapping_invalid",
                "The Trader MCP capability mapping is unavailable or invalid.",
            ) from exc
        candidate, expected = _select_verified_candidate(mapping, trade_enabled=trade_enabled)
        expected_version = str(candidate["package_version"])
        runtime = (runtime_loader or _load_default_runtime)(expected_version)
        if not _runtime_symbols_are_valid(runtime):
            raise McpGatewayError(
                "mcp_runtime_incompatible",
                "The installed WEEX MCP package does not expose the required public Local runtime API.",
            )
        if runtime.package_version != expected_version:
            raise McpGatewayError(
                "mcp_version_mismatch",
                "The installed WEEX MCP package version does not match the verified contract.",
            )

        active_profile_loader = profile_loader or _default_profile_loader
        active_credential_loader = credential_loader or _default_credential_loader
        if profile_id is not None:
            try:
                profile = active_profile_loader(profile_id)
            except Exception as exc:
                raise McpGatewayError("profile_required", "A saved profile is required for private MCP tools.") from exc
            if _profile_id_from_value(profile) != profile_id:
                raise McpGatewayError("profile_required", "A saved profile is required for private MCP tools.")

        self.profile_id = profile_id
        self.trade_enabled = trade_enabled
        self.effective_spot_base_url = effective_spot_base_url
        self.effective_futures_base_url = effective_futures_base_url
        self.timeout_seconds = float(timeout_seconds)
        self._runtime = runtime
        self._expected_candidate = candidate
        self._expected_manifest = expected
        self._approval_key_status_loader = approval_key_status_loader or _default_approval_key_status_loader
        self._approval_key_fingerprint: str | None = None
        approval_secret = ""

        if trade_enabled:
            active_approval_loader = approval_key_loader or _default_approval_key_loader
            try:
                approval_key = active_approval_loader()
            except Exception as exc:
                raise McpGatewayError(
                    "approval_key_required",
                    "A valid unlocked local MCP approval key is required.",
                ) from exc
            if not isinstance(approval_key, str) or len(approval_key.encode("utf-8")) < 32:
                raise McpGatewayError(
                    "approval_key_required",
                    "A valid unlocked local MCP approval key is required.",
                )
            self._approval_key_fingerprint = _approval_key_fingerprint(approval_key)
            self._require_current_approval_key(initial=True)
            manifest_digest = str(expected["manifest_digest"])
            approval_key_bytes = approval_key.encode("utf-8")
            approval_secret = _safe_hmac(
                approval_key_bytes,
                _APPROVAL_SECRET_DOMAIN,
                manifest_digest,
            ).hex()
            fingerprint_key = _safe_hmac(
                approval_key_bytes,
                _FINGERPRINT_KEY_DOMAIN,
                manifest_digest,
            )
        else:
            fingerprint_key = secrets.token_bytes(32)

        if profile_id is None:
            secret_provider: Any = _PublicOnlySecretProvider(runtime)
            self._bound_ref = None
        else:
            self._bound_ref = _new_bound_ref()
            secret_provider = VaultSecretProvider(
                profile_id=profile_id,
                bound_ref=self._bound_ref,
                expected_user_id=profile_id,
                fingerprint_key=fingerprint_key,
                credential_loader=active_credential_loader,
                runtime=runtime,
            )

        try:
            settings = runtime.Settings(
                trade_enabled=trade_enabled,
                approval_token_secret=approval_secret,
                spot_base_url=effective_spot_base_url,
                futures_base_url=effective_futures_base_url,
                market_timeout_seconds=self.timeout_seconds,
                market_max_attempts=1,
                market_retry_base_delay_seconds=0.0,
                account_timeout_seconds=self.timeout_seconds,
                partner_timeout_seconds=self.timeout_seconds,
                request_locale=request_locale,
            )
            service_arguments: dict[str, Any] = {
                "settings": settings,
                "secret_provider": secret_provider,
            }
            if delegated_authorizer is not None:
                service_arguments["execution_authorizer"] = _CallbackExecutionAuthorizer(
                    callback=delegated_authorizer,
                    runtime=runtime,
                )
            self._service = runtime.build_local_user_service(
                **service_arguments,
            )
        except McpGatewayError:
            raise
        except Exception as exc:
            raise McpGatewayError(
                "mcp_runtime_incompatible",
                "The WEEX MCP Local runtime could not be constructed.",
            ) from exc
        self._request_counter = 0
        self._session_id = "gateway_" + hashlib.sha256(
            json.dumps(
                {
                    "profile_id": profile_id,
                    "spot": effective_spot_base_url,
                    "futures": effective_futures_base_url,
                    "trade": trade_enabled,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        self._tools = self._fresh_preflight()

    def _next_id(self, label: str) -> str:
        self._request_counter += 1
        return f"gateway-{label}-{self._request_counter}"

    def _jsonrpc_result(
        self,
        response: Any,
        *,
        operation: str,
        expected_id: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(response, dict)
            or response.get("jsonrpc") != "2.0"
            or response.get("id") != expected_id
            or "error" in response
        ):
            raise McpGatewayError("mcp_protocol_error", f"WEEX MCP {operation} failed.")
        result = response.get("result")
        if not isinstance(result, dict):
            raise McpGatewayError("mcp_protocol_error", f"WEEX MCP {operation} returned an invalid result.")
        return result

    def _fresh_preflight(self) -> dict[str, dict[str, Any]]:
        discover_id = self._next_id("discover")
        list_id = self._next_id("list")
        try:
            discover_response = self._service.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": discover_id,
                    "method": "server/discover",
                    "params": {},
                }
            )
            listed_response = self._service.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": list_id,
                    "method": "tools/list",
                    "params": {},
                }
            )
        except Exception as exc:
            raise McpGatewayError(
                "mcp_protocol_error",
                "WEEX MCP compatibility preflight failed.",
            ) from exc
        discover = self._jsonrpc_result(
            discover_response,
            operation="discovery",
            expected_id=discover_id,
        )
        listed = self._jsonrpc_result(
            listed_response,
            operation="tool listing",
            expected_id=list_id,
        )
        metadata_value = discover.get("_meta")
        weex_metadata = metadata_value.get("com.weex/mcp") if isinstance(metadata_value, dict) else None
        manifest = weex_metadata.get("compatibility") if isinstance(weex_metadata, dict) else None
        tools = listed.get("tools")
        if not isinstance(manifest, dict) or not isinstance(tools, list):
            raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP compatibility contract is missing.")
        return self._validate_contract(manifest=manifest, tools=tools)

    def _validate_contract(
        self,
        *,
        manifest: dict[str, Any],
        tools: list[Any],
    ) -> dict[str, dict[str, Any]]:
        tools_by_name: dict[str, dict[str, Any]] = {}
        schemas: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP tool catalog is invalid.")
            name = tool.get("name")
            input_schema = tool.get("inputSchema")
            output_schema = tool.get("outputSchema")
            if (
                not isinstance(name, str)
                or not name
                or name in tools_by_name
                or not isinstance(input_schema, dict)
                or not isinstance(output_schema, dict)
            ):
                raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP tool catalog is invalid.")
            tools_by_name[name] = tool
            schemas.append(
                {
                    "name": name,
                    "inputSchema": input_schema,
                    "outputSchema": output_schema,
                }
            )
        names = sorted(tools_by_name)
        schemas.sort(key=lambda item: item["name"])
        for field in ("tool_set_digest", "schema_digest", "manifest_digest"):
            if not isinstance(manifest.get(field), str) or _DIGEST_PATTERN.fullmatch(manifest[field]) is None:
                raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP compatibility digest is invalid.")
        manifest_payload = dict(manifest)
        stored_manifest_digest = manifest_payload.pop("manifest_digest")
        expected_modules = ["account", "market", "partner", *( ["trade"] if self.trade_enabled else [])]
        expected_values = {
            "package_version": self._expected_candidate["package_version"],
            "tool_contract_version": self._expected_candidate["tool_contract_version"],
            "runtime_profile": LOCAL_RUNTIME_PROFILE,
            "trade_enabled": self.trade_enabled,
            "tools": names,
            "credential_modes": ["credential_ref"],
            "delegated_authorization": self.trade_enabled,
            "modules": expected_modules,
            "tool_set_digest": _canonical_digest(names),
            "schema_digest": _canonical_digest(schemas),
        }
        if _canonical_digest(manifest_payload) != stored_manifest_digest:
            raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP compatibility manifest was modified.")
        if any(manifest.get(field) != value for field, value in expected_values.items()):
            raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP compatibility contract does not match Trader.")
        if len(names) != self._expected_manifest["tool_count"]:
            raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP tool count does not match Trader.")
        for field in ("tool_set_digest", "schema_digest", "manifest_digest"):
            if manifest.get(field) != self._expected_manifest[field]:
                raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP compatibility digest does not match Trader.")
        return tools_by_name

    def _require_current_approval_key(self, *, initial: bool = False) -> None:
        if not self.trade_enabled:
            return
        try:
            status = self._approval_key_status_loader()
        except Exception as exc:
            raise McpGatewayError(
                "approval_key_required",
                "A valid unlocked local MCP approval key is required.",
            ) from exc
        if (
            not isinstance(status, dict)
            or status.get("present") is not True
            or status.get("valid") is not True
            or isinstance(status.get("length"), bool)
            or not isinstance(status.get("length"), int)
            or status["length"] < 32
            or not isinstance(status.get("fp_sha256"), str)
        ):
            raise McpGatewayError(
                "approval_key_required",
                "A valid unlocked local MCP approval key is required.",
            )
        if self._approval_key_fingerprint != status["fp_sha256"]:
            code = "approval_key_changed" if not initial else "approval_key_changed"
            raise McpGatewayError(code, "The local MCP approval key changed; rebuild the Gateway.")

    def _context(self) -> Any:
        return self._runtime.TenantContext(
            tenant_id="weex-trader-local",
            user_id=self.profile_id or "public",
            session_id=self._session_id,
        )

    def runtime_status(self) -> dict[str, Any]:
        """Return a fresh, non-secret projection of this Gateway's contract state."""

        expected_version = str(self._expected_candidate["package_version"])
        version = self._runtime.package_version
        package_ok = version == expected_version
        manifest_ok = False
        action_required: str | None = None
        if not package_ok:
            action_required = "install_verified_mcp_candidate"
        else:
            try:
                self._fresh_preflight()
            except McpGatewayError as exc:
                action_required = (
                    "repair_mcp_contract"
                    if exc.code == "mcp_contract_mismatch"
                    else "repair_mcp_runtime"
                )
            except Exception:
                action_required = "repair_mcp_runtime"
            else:
                manifest_ok = True

        return {
            "installed": True,
            "version": version,
            "expected_version": expected_version,
            "package_ok": package_ok,
            "manifest_ok": manifest_ok,
            "runtime_profile": LOCAL_RUNTIME_PROFILE,
            "trade_enabled": self.trade_enabled,
            "tool_contract_version": self._expected_candidate["tool_contract_version"],
            "manifest_digest": self._expected_manifest["manifest_digest"],
            "action_required": action_required,
        }

    def call_typed_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(name, str) or not name:
            raise McpGatewayError("typed_tool_required", "A typed MCP tool name is required.")
        if not isinstance(arguments, Mapping):
            raise McpGatewayError("invalid_arguments", "Typed MCP tool arguments must be an object.")
        unsafe = sorted(_CALLER_FORBIDDEN_ARGUMENTS.intersection(arguments))
        if unsafe:
            raise McpGatewayError("invalid_arguments", "Caller-controlled routing or credential fields are forbidden.")
        tools = self._fresh_preflight()
        tool = tools.get(name)
        if tool is None:
            raise McpGatewayError("unsupported_tool", "The typed MCP tool is not in the verified Local catalog.")
        properties = tool["inputSchema"].get("properties")
        private_tool = isinstance(properties, dict) and "credential" in properties
        call_arguments = dict(arguments)
        if private_tool:
            if self.profile_id is None or self._bound_ref is None:
                raise McpGatewayError("profile_required", "A saved profile is required for private MCP tools.")
            call_arguments["credential"] = {
                "type": "credential_ref",
                "key_id": self.profile_id,
                "ref": self._bound_ref,
            }
        if name.startswith("trade_"):
            self._require_current_approval_key()
        call_id = self._next_id("call")
        try:
            response = self._service.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": call_arguments},
                },
                context=self._context(),
            )
        except Exception as exc:
            raise McpGatewayError("mcp_protocol_error", "WEEX MCP tool call failed.") from exc
        result = self._jsonrpc_result(
            response,
            operation="tool call",
            expected_id=call_id,
        )
        payload = result.get("structuredContent")
        is_error = result.get("isError")
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("ok"), bool)
            or not isinstance(is_error, bool)
            or is_error is payload["ok"]
        ):
            raise McpGatewayError("mcp_protocol_error", "WEEX MCP returned an invalid tool envelope.")
        return dict(payload)


def _probe_status(
    *,
    installed: bool | None = None,
    version: str | None,
    expected_version: str | None,
    package_ok: bool,
    manifest_ok: bool,
    trade_enabled: bool,
    tool_contract_version: str | None,
    manifest_digest: str | None,
    action_required: str | None,
) -> dict[str, Any]:
    safe_version = _safe_package_version(version)
    safe_expected_version = _safe_package_version(expected_version)
    safe_contract_version = _safe_contract_version(tool_contract_version)
    safe_manifest_digest = _safe_digest(manifest_digest)
    safe_action = action_required if action_required in _PROBE_ACTIONS else "repair_mcp_runtime"
    safe_trade_enabled = trade_enabled if isinstance(trade_enabled, bool) else False
    safe_package_ok = bool(
        package_ok
        and safe_version is not None
        and safe_version == safe_expected_version
    )
    safe_manifest_ok = bool(
        manifest_ok
        and safe_package_ok
        and safe_contract_version is not None
        and safe_manifest_digest is not None
        and action_required is None
        and isinstance(trade_enabled, bool)
    )
    if safe_manifest_ok:
        safe_action = None
    elif action_required is None:
        safe_action = "repair_mcp_runtime"
    return {
        "installed": installed if isinstance(installed, bool) else safe_version is not None,
        "version": safe_version,
        "expected_version": safe_expected_version,
        "package_ok": safe_package_ok,
        "manifest_ok": safe_manifest_ok,
        "runtime_profile": LOCAL_RUNTIME_PROFILE,
        "trade_enabled": safe_trade_enabled,
        "tool_contract_version": safe_contract_version,
        "manifest_digest": safe_manifest_digest,
        "action_required": safe_action,
    }


def _installed_package_version() -> str | None:
    try:
        return _safe_package_version(metadata.version("weex-mcp"))
    except Exception:
        return None


def probe_local_mcp_runtime(
    *,
    profile_id: str | None = None,
    trade_enabled: bool = False,
    runtime_loader: Callable[[str], Any] | None = None,
    mapping_loader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Probe the verified Local MCP composition without reading secrets or calling WEEX."""

    active_mapping_loader = mapping_loader or _load_default_mapping
    expected_version: str | None = None
    tool_contract_version: str | None = None
    manifest_digest: str | None = None
    try:
        mapping = active_mapping_loader()
        candidate, expected_manifest = _select_verified_candidate(
            mapping,
            trade_enabled=trade_enabled,
        )
        expected_version = str(candidate["package_version"])
        tool_contract_version = str(candidate["tool_contract_version"])
        manifest_digest = str(expected_manifest["manifest_digest"])
    except Exception:
        return _probe_status(
            version=_installed_package_version() if runtime_loader is None else None,
            expected_version=None,
            package_ok=False,
            manifest_ok=False,
            trade_enabled=trade_enabled,
            tool_contract_version=None,
            manifest_digest=None,
            action_required="repair_mcp_mapping",
        )

    active_runtime_loader = runtime_loader or _load_default_runtime
    try:
        runtime = active_runtime_loader(expected_version)
    except McpGatewayError as exc:
        version = _installed_package_version() if runtime_loader is None else None
        action = (
            "install_verified_mcp_candidate"
            if exc.code in {"mcp_package_missing", "mcp_version_mismatch"}
            else "repair_mcp_runtime"
        )
        return _probe_status(
            version=version,
            expected_version=expected_version,
            package_ok=False,
            manifest_ok=False,
            trade_enabled=trade_enabled,
            tool_contract_version=tool_contract_version,
            manifest_digest=manifest_digest,
            action_required=action,
        )
    except Exception:
        return _probe_status(
            version=None,
            expected_version=expected_version,
            package_ok=False,
            manifest_ok=False,
            trade_enabled=trade_enabled,
            tool_contract_version=tool_contract_version,
            manifest_digest=manifest_digest,
            action_required="repair_mcp_runtime",
        )

    version = getattr(runtime, "package_version", None)
    version = version if isinstance(version, str) and version else None
    if version != expected_version:
        return _probe_status(
            installed=True,
            version=version,
            expected_version=expected_version,
            package_ok=False,
            manifest_ok=False,
            trade_enabled=trade_enabled,
            tool_contract_version=tool_contract_version,
            manifest_digest=manifest_digest,
            action_required="install_verified_mcp_candidate",
        )
    if not _runtime_symbols_are_valid(runtime):
        return _probe_status(
            installed=True,
            version=version,
            expected_version=expected_version,
            package_ok=True,
            manifest_ok=False,
            trade_enabled=trade_enabled,
            tool_contract_version=tool_contract_version,
            manifest_digest=manifest_digest,
            action_required="repair_mcp_runtime",
        )

    probe_profile_id = profile_id
    if trade_enabled and probe_profile_id is None:
        probe_profile_id = "mcp-runtime-probe"
    probe_approval_key = "mcp-runtime-probe-approval-key-00000000000000000000000000000000"
    probe_approval_status = {
        "present": True,
        "valid": True,
        "length": len(probe_approval_key),
        "fp_sha256": _approval_key_fingerprint(probe_approval_key),
    }
    try:
        gateway = LocalMcpGateway(
            profile_id=probe_profile_id,
            effective_spot_base_url=DEFAULT_SPOT_BASE_URL,
            effective_futures_base_url=DEFAULT_FUTURES_BASE_URL,
            trade_enabled=trade_enabled,
            timeout_seconds=15.0,
            runtime_loader=lambda _expected: runtime,
            mapping_loader=lambda: mapping,
            profile_loader=lambda value: {"profile_id": value},
            credential_loader=lambda _value: (_ for _ in ()).throw(
                RuntimeError("Runtime probe does not resolve credentials.")
            ),
            approval_key_loader=lambda: probe_approval_key,
            approval_key_status_loader=lambda: dict(probe_approval_status),
        )
    except McpGatewayError as exc:
        action = (
            "repair_mcp_contract"
            if exc.code == "mcp_contract_mismatch"
            else "repair_mcp_runtime"
        )
        return _probe_status(
            installed=True,
            version=version,
            expected_version=expected_version,
            package_ok=True,
            manifest_ok=False,
            trade_enabled=trade_enabled,
            tool_contract_version=tool_contract_version,
            manifest_digest=manifest_digest,
            action_required=action,
        )
    except Exception:
        return _probe_status(
            installed=True,
            version=version,
            expected_version=expected_version,
            package_ok=True,
            manifest_ok=False,
            trade_enabled=trade_enabled,
            tool_contract_version=tool_contract_version,
            manifest_digest=manifest_digest,
            action_required="repair_mcp_runtime",
        )
    return gateway.runtime_status()


__all__ = [
    "LocalMcpGateway",
    "McpGatewayError",
    "VaultSecretProvider",
    "describe_local_mcp_request",
    "probe_local_mcp_runtime",
]
