#!/usr/bin/env python3
"""Profile-bound embedded MCP gateway for WEEX Trader internals.

The module has no import-time dependency on ``weex-mcp``. The fixed package is
loaded and contract-checked only when a Gateway or request description is used.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import math
import re
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


SKILL_ROOT = Path(__file__).resolve().parent.parent
MAPPING_PATH = SKILL_ROOT / "references" / "mcp-capability-mapping.json"
BOUNDARY_CONTRACT_PATH = SKILL_ROOT / "references" / "mcp-boundary-contract-v1.json"
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
_HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PROBE_ACTIONS = frozenset(
    {
        "install_verified_mcp_candidate",
        "repair_mcp_mapping",
        "repair_mcp_runtime",
        "repair_mcp_contract",
        "repair_mcp_gateway",
    }
)
_RUNTIME_ACTIONS = frozenset({"configure_trade_adapter", "configure_execution_grant_consumer"})
_CALLER_FORBIDDEN_ARGUMENTS = frozenset(
    {
        "credential",
        "permissions",
        "confirmation",
        "preview_id",
        "approval_token",
        "delegated_authorization",
        "expected_environment",
        "acknowledge",
        "endpoint",
        "path",
        "method",
    }
)
_UPSTREAM_PERMISSIONS = frozenset({"spotTrading", "futuresTrading", "accountDetails"})
_ACCOUNT_BINDING_DOMAIN = b"weex-trader-local-mcp-account-binding-v1\0"
_CREDENTIAL_BINDING_DOMAIN = b"weex-trader-local-mcp-credential-binding-v1\0"
_CREDENTIAL_FINGERPRINT_DOMAIN = b"weex-trader-local-mcp-credential-v2\0"


class McpGatewayError(RuntimeError):
    """Stable, non-sensitive failure raised at the Trader/MCP boundary."""

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
    VerifiedPrincipal: Any
    AccountBinding: Any
    CredentialBinding: Any
    CorrelationContext: Any
    AuthenticatedInvocationContext: Any
    ResolvedWeexCredential: Any
    SecretProviderError: Any
    ExecutionGrant: Any


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
        context_module = importlib.import_module("weex_mcp.invocation_context")
        secret_module = importlib.import_module("weex_mcp.secret_provider")
        grant_module = importlib.import_module("weex_mcp.execution_authorization")
        return _McpRuntime(
            package_version=installed_version,
            build_local_user_service=local_module.build_local_user_service,
            describe_request=local_module.describe_request,
            Settings=settings_module.Settings,
            VerifiedPrincipal=context_module.VerifiedPrincipal,
            AccountBinding=context_module.AccountBinding,
            CredentialBinding=context_module.CredentialBinding,
            CorrelationContext=context_module.CorrelationContext,
            AuthenticatedInvocationContext=context_module.AuthenticatedInvocationContext,
            ResolvedWeexCredential=secret_module.ResolvedWeexCredential,
            SecretProviderError=secret_module.SecretProviderError,
            ExecutionGrant=grant_module.ExecutionGrant,
        )
    except Exception as exc:
        raise McpGatewayError(
            "mcp_runtime_incompatible",
            "The installed WEEX MCP package does not expose the required Local runtime API.",
        ) from exc


def _read_json(path: Path, *, code: str, message: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise McpGatewayError(code, message) from exc
    if not isinstance(value, dict):
        raise McpGatewayError(code, message)
    return value


def _load_default_mapping() -> dict[str, Any]:
    return _read_json(
        MAPPING_PATH,
        code="mcp_mapping_invalid",
        message="The Trader MCP capability mapping is unavailable or invalid.",
    )


def _load_default_boundary_contract() -> dict[str, Any]:
    return _read_json(
        BOUNDARY_CONTRACT_PATH,
        code="mcp_mapping_invalid",
        message="The Trader MCP boundary contract is unavailable or invalid.",
    )


def _profile_store_function(name: str) -> Any:
    try:
        module = importlib.import_module("weex_profile_store")
        return getattr(module, name)
    except (AttributeError, ImportError, ModuleNotFoundError) as exc:
        raise McpGatewayError(
            "profile_runtime_unavailable",
            "The Trader profile and Vault runtime is unavailable.",
        ) from exc


def _default_profile_loader(profile_id: str) -> Any:
    return _profile_store_function("get_profile_by_id")(profile_id)


def _default_credential_loader(profile_id: str) -> Any:
    return _profile_store_function("load_profile_credentials")(profile_id)


def _default_binding_key_loader() -> str:
    return _profile_store_function("get_or_create_local_mcp_binding_key")()


def _default_binding_key_status_loader() -> dict[str, Any]:
    return _profile_store_function("local_mcp_binding_key_status")()


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


def _key_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _profile_id_from_value(value: Any) -> str | None:
    raw = (
        value.get("profile_id") or value.get("id") if isinstance(value, Mapping) else getattr(value, "profile_id", None)
    )
    return raw if isinstance(raw, str) and raw else None


def _safe_package_version(value: Any) -> str | None:
    return value if isinstance(value, str) and _PACKAGE_VERSION_PATTERN.fullmatch(value) else None


def _safe_contract_version(value: Any) -> str | None:
    return value if isinstance(value, str) and _CONTRACT_VERSION_PATTERN.fullmatch(value) else None


def _safe_digest(value: Any) -> str | None:
    return value if isinstance(value, str) and _DIGEST_PATTERN.fullmatch(value) else None


def _runtime_symbols_are_valid(runtime: Any) -> bool:
    required = (
        "package_version",
        "build_local_user_service",
        "describe_request",
        "Settings",
        "VerifiedPrincipal",
        "AccountBinding",
        "CredentialBinding",
        "CorrelationContext",
        "AuthenticatedInvocationContext",
        "ResolvedWeexCredential",
        "SecretProviderError",
        "ExecutionGrant",
    )
    return all(getattr(runtime, name, None) is not None for name in required)


def _select_verified_candidate(
    mapping: Any,
    *,
    trade_enabled: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(mapping, dict) or mapping.get("mapping_version") != EXPECTED_MAPPING_VERSION:
        raise McpGatewayError("mcp_mapping_invalid", "The Trader MCP capability mapping version is not supported.")
    candidate = mapping.get("verified_mcp_candidate")
    if not isinstance(candidate, dict):
        raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP candidate is missing.")
    versions = candidate.get("versions")
    required_versions = {
        "package",
        "tool_contract",
        "manifest",
        "output",
        "prepare",
        "request_description",
        "transport_meta",
        "grant",
    }
    if (
        _safe_package_version(candidate.get("package_version")) is None
        or candidate.get("package_version") != (versions or {}).get("package")
        or candidate.get("tool_contract_version") != (versions or {}).get("tool_contract")
        or candidate.get("manifest_version") != 2
        or not isinstance(versions, dict)
        or set(versions) != required_versions
        or versions.get("manifest") != "2"
        or not isinstance(candidate.get("source_commit"), str)
        or _COMMIT_PATTERN.fullmatch(candidate["source_commit"]) is None
        or candidate.get("publication_status") != "unpublished"
    ):
        raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP candidate contract is invalid.")
    distributions = candidate.get("distribution_sha256")
    if (
        not isinstance(distributions, dict)
        or set(distributions) != {"wheel", "sdist"}
        or any(
            not isinstance(value, str) or _HEX_64_PATTERN.fullmatch(value) is None for value in distributions.values()
        )
    ):
        raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP artifacts are invalid.")
    for field in (
        "boundary_contract_digest",
        "direct_tools_digest",
        "legacy_mapping_digest",
        "inventory_digest",
        "matrix_digest",
    ):
        if _safe_digest(candidate.get(field)) is None:
            raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP candidate digest is invalid.")
    source_catalogs = candidate.get("source_catalog_digests")
    if (
        not isinstance(source_catalogs, dict)
        or set(source_catalogs) != {"contract", "spot", "partner"}
        or any(_safe_digest(value) is None for value in source_catalogs.values())
    ):
        raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP source catalog digests are invalid.")
    profiles = candidate.get("compatibility_profiles")
    if not isinstance(profiles, list):
        raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP profile matrix is invalid.")
    seen: set[bool] = set()
    selected: list[dict[str, Any]] = []
    for item in profiles:
        if (
            not isinstance(item, dict)
            or item.get("runtime_profile") != LOCAL_RUNTIME_PROFILE
            or not isinstance(item.get("trade_enabled"), bool)
        ):
            raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP profile matrix is invalid.")
        enabled = item["trade_enabled"]
        if enabled in seen:
            raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP profile matrix is invalid.")
        seen.add(enabled)
        if (
            isinstance(item.get("tool_count"), bool)
            or not isinstance(item.get("tool_count"), int)
            or item["tool_count"] <= 0
        ):
            raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP profile contract is invalid.")
        for field in (
            "tool_set_digest",
            "schema_digest",
            "manifest_digest",
            "boundary_static_manifest_digest",
        ):
            if _safe_digest(item.get(field)) is None:
                raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP profile digest is invalid.")
        if enabled is trade_enabled:
            selected.append(dict(item))
    if seen != {False, True} or len(selected) != 1:
        raise McpGatewayError("mcp_mapping_invalid", "The verified WEEX MCP profile matrix is incomplete.")
    return dict(candidate), selected[0]


def _select_boundary_contract(
    value: Any,
    *,
    candidate: Mapping[str, Any],
) -> tuple[frozenset[str], dict[str, str]]:
    if not isinstance(value, dict):
        raise McpGatewayError("mcp_mapping_invalid", "The Trader MCP boundary contract does not match the candidate.")
    contract_body = dict(value)
    stored_digest = contract_body.pop("boundary_contract_digest", None)
    if stored_digest != candidate["boundary_contract_digest"] or stored_digest != _canonical_digest(contract_body):
        raise McpGatewayError("mcp_mapping_invalid", "The Trader MCP boundary contract does not match the candidate.")
    contract_versions = value.get("candidate_versions")
    if not isinstance(contract_versions, dict) or any(
        contract_versions.get(field) != item for field, item in candidate["versions"].items()
    ):
        raise McpGatewayError("mcp_mapping_invalid", "The Trader MCP boundary versions do not match the candidate.")
    permissions = value.get("permissions")
    if not isinstance(permissions, dict):
        raise McpGatewayError("mcp_mapping_invalid", "The Trader MCP permission contract is missing.")
    compatibility = value.get("compatibility")
    if (
        value.get("direct_tools_digest") != candidate["direct_tools_digest"]
        or not isinstance(compatibility, dict)
        or compatibility.get("legacy_mapping_digest") != candidate["legacy_mapping_digest"]
        or compatibility.get("inventory_digest") != candidate["inventory_digest"]
        or compatibility.get("matrix_digest") != candidate["matrix_digest"]
        or compatibility.get("source_catalog_digests") != candidate["source_catalog_digests"]
    ):
        raise McpGatewayError(
            "mcp_mapping_invalid",
            "The Trader MCP compatibility evidence does not match the candidate.",
        )
    capabilities: set[str] = set()
    upstream: set[str] = set()
    for group in ("account", "market", "partner", "trade"):
        entries = permissions.get(group)
        if not isinstance(entries, dict):
            raise McpGatewayError("mcp_mapping_invalid", "The Trader MCP permission contract is invalid.")
        for entry in entries.values():
            if not isinstance(entry, dict):
                raise McpGatewayError("mcp_mapping_invalid", "The Trader MCP permission contract is invalid.")
            capability = entry.get("host_policy_capability")
            if not isinstance(capability, str) or not capability:
                raise McpGatewayError("mcp_mapping_invalid", "The Trader MCP permission capability is invalid.")
            capabilities.add(capability)
            truth = entry.get("openapi_truth")
            if isinstance(truth, str) and truth in _UPSTREAM_PERMISSIONS:
                upstream.add(truth)
            required = entry.get("required_upstream_evidence", [])
            if not isinstance(required, list) or any(item not in _UPSTREAM_PERMISSIONS for item in required):
                raise McpGatewayError("mcp_mapping_invalid", "The Trader MCP upstream permission evidence is invalid.")
            upstream.update(required)
    return frozenset(capabilities), {name: "unverified" for name in sorted(upstream)}


def _validate_active_manifest(
    manifest: dict[str, Any],
    *,
    candidate: Mapping[str, Any],
    expected_profile: Mapping[str, Any],
    trade_enabled: bool,
    names: list[str],
    schemas: list[dict[str, Any]],
) -> None:
    payload = dict(manifest)
    stored = payload.pop("manifest_digest", None)
    expected = {
        "manifest_version": 2,
        "package_version": candidate["package_version"],
        "tool_contract_version": candidate["tool_contract_version"],
        "runtime_profile": LOCAL_RUNTIME_PROFILE,
        "trade_enabled": trade_enabled,
        "tools": names,
        "credential_modes": [],
        "delegated_authorization": False,
        "modules": ["account", "market", "partner", *(["trade"] if trade_enabled else [])],
        "tool_set_digest": _canonical_digest(names),
        "schema_digest": _canonical_digest(schemas),
    }
    if stored != _canonical_digest(payload) or any(manifest.get(field) != value for field, value in expected.items()):
        raise McpGatewayError(
            "mcp_contract_mismatch", "The WEEX MCP active compatibility contract does not match Trader."
        )
    if len(names) != expected_profile["tool_count"] or any(
        manifest.get(field) != expected_profile[field]
        for field in ("tool_set_digest", "schema_digest", "manifest_digest")
    ):
        raise McpGatewayError(
            "mcp_contract_mismatch", "The WEEX MCP active compatibility digest does not match Trader."
        )


def _validate_static_manifest(
    manifest: dict[str, Any],
    *,
    candidate: Mapping[str, Any],
    expected_profile: Mapping[str, Any],
    names: list[str],
    projections: list[dict[str, Any]],
) -> None:
    payload = dict(manifest)
    stored = payload.pop("manifest_digest", None)
    computed = {
        "input_schema_digest": _canonical_digest(
            [{"name": item["name"], "schema": item["inputSchema"]} for item in projections]
        ),
        "output_schema_digest": _canonical_digest(
            [{"name": item["name"], "schema": item["outputSchema"]} for item in projections]
        ),
        "annotations_digest": _canonical_digest(
            [{"name": item["name"], "annotations": item["annotations"]} for item in projections]
        ),
        "tool_meta_digest": _canonical_digest([{"name": item["name"], "_meta": item["_meta"]} for item in projections]),
    }
    expected = {
        "status": "CANDIDATE_NEEDS_RELEASE_OWNER",
        "versions": candidate["versions"],
        "package_version": candidate["package_version"],
        "composition_kind": LOCAL_RUNTIME_PROFILE,
        "tools": names,
        **computed,
        "boundary_contract_digest": candidate["boundary_contract_digest"],
        "direct_tools_digest": candidate["direct_tools_digest"],
        "legacy_mapping_digest": candidate["legacy_mapping_digest"],
        "inventory_digest": candidate["inventory_digest"],
        "matrix_digest": candidate["matrix_digest"],
        "source_catalog_digests": candidate["source_catalog_digests"],
        "credential_binding_version": 1,
        "grant_consumer_version": 1,
    }
    if stored != _canonical_digest(payload) or any(manifest.get(field) != value for field, value in expected.items()):
        raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP static boundary contract does not match Trader.")
    if stored != expected_profile["boundary_static_manifest_digest"]:
        raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP static manifest digest does not match Trader.")


def _validate_runtime_availability(value: Any, *, names: list[str], trade_enabled: bool) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "effective_tools",
        "private_read_ready",
        "mutation_ready",
        "action_required",
    }:
        raise McpGatewayError("mcp_protocol_error", "The WEEX MCP runtime availability is invalid.")
    actions = value.get("action_required")
    if (
        value.get("effective_tools") != names
        or not isinstance(value.get("private_read_ready"), bool)
        or not isinstance(value.get("mutation_ready"), bool)
        or not isinstance(actions, list)
        or not all(isinstance(item, str) and item in _RUNTIME_ACTIONS for item in actions)
        or len(actions) != len(set(actions))
        or (value["mutation_ready"] and (not trade_enabled or actions or not value["private_read_ready"]))
        or (not trade_enabled and (value["mutation_ready"] or actions))
    ):
        raise McpGatewayError("mcp_protocol_error", "The WEEX MCP runtime availability is invalid.")
    return {
        "effective_tools": list(names),
        "private_read_ready": value["private_read_ready"],
        "mutation_ready": value["mutation_ready"],
        "action_required": list(actions),
    }


def _validate_transport_meta(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not {"version", "request_id", "attempt_count"}.issubset(value)
        or not set(value).issubset(
            {"version", "request_id", "attempt_count", "http_status", "upstream_request_id", "rate_limit"}
        )
    ):
        raise McpGatewayError("mcp_protocol_error", "WEEX MCP returned invalid transport metadata.")
    if (
        value.get("version") != 1
        or not isinstance(value.get("request_id"), str)
        or not value["request_id"]
        or isinstance(value.get("attempt_count"), bool)
        or not isinstance(value.get("attempt_count"), int)
        or value["attempt_count"] < 1
    ):
        raise McpGatewayError("mcp_protocol_error", "WEEX MCP returned invalid transport metadata.")
    status = value.get("http_status")
    if status is not None and (isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599):
        raise McpGatewayError("mcp_protocol_error", "WEEX MCP returned invalid transport metadata.")
    upstream_id = value.get("upstream_request_id")
    if upstream_id is not None and (not isinstance(upstream_id, str) or not upstream_id):
        raise McpGatewayError("mcp_protocol_error", "WEEX MCP returned invalid transport metadata.")
    rate_limit = value.get("rate_limit")
    if rate_limit is not None:
        if not isinstance(rate_limit, dict) or not set(rate_limit).issubset({"used", "remaining"}):
            raise McpGatewayError("mcp_protocol_error", "WEEX MCP returned invalid transport metadata.")
        for bucket in rate_limit.values():
            if not isinstance(bucket, dict) or any(
                not isinstance(name, str)
                or not name
                or isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(float(amount))
                or amount < 0
                for name, amount in bucket.items()
            ):
                raise McpGatewayError("mcp_protocol_error", "WEEX MCP returned invalid transport metadata.")
    return _copy_json_object(value)


def _copy_json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise McpGatewayError(
            "mcp_protocol_error",
            "WEEX MCP returned a non-JSON result.",
        ) from exc


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
        raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP request description is invalid.")
    method, url = value.get("method"), value.get("url")
    headers, query, body = value.get("headers"), value.get("query"), value.get("body")
    metadata_value = value.get("_meta")
    weex_metadata = metadata_value.get("com.weex/mcp") if isinstance(metadata_value, dict) else None
    manifest = weex_metadata.get("compatibility") if isinstance(weex_metadata, dict) else None
    if (
        method not in {"GET", "POST", "DELETE"}
        or not isinstance(url, str)
        or not url.startswith("https://")
        or not isinstance(headers, dict)
        or not all(isinstance(key, str) and isinstance(item, str) for key, item in headers.items())
        or not isinstance(query, dict)
        or not isinstance(body, dict)
        or not isinstance(manifest, dict)
    ):
        raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP request description is invalid.")
    parsed_url = urlsplit(url)
    market = str(arguments.get("market") or "").strip().lower()
    expected_origin = (
        effective_spot_base_url.rstrip("/")
        if market == "spot"
        else effective_futures_base_url.rstrip("/")
        if market in {"contract", "futures"}
        else None
    )
    described_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
    private_request = name.startswith(("account_", "partner_", "trade_"))
    auth_headers = {
        "access-key": "***",
        "access-passphrase": "***",
        "access-timestamp": "<generated-at-send>",
        "access-sign": "***",
    }
    normalized: dict[str, str] = {}
    duplicate = False
    for key, item in headers.items():
        folded = key.casefold()
        duplicate = duplicate or folded in normalized
        normalized[folded] = item
    expected_headers = {"accept", "content-type", "locale", "user-agent", *(auth_headers if private_request else ())}
    if (
        expected_origin is None
        or described_origin != expected_origin
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.fragment
        or duplicate
        or set(normalized) != expected_headers
        or (private_request and any(normalized.get(key) != item for key, item in auth_headers.items()))
    ):
        raise McpGatewayError(
            "mcp_contract_mismatch", "The WEEX MCP request description violates the Local route contract."
        )
    manifest_payload = dict(manifest)
    stored = manifest_payload.pop("manifest_digest", None)
    expected_values = {
        "manifest_version": 2,
        "package_version": candidate["package_version"],
        "tool_contract_version": candidate["tool_contract_version"],
        "runtime_profile": LOCAL_RUNTIME_PROFILE,
        "trade_enabled": trade_enabled,
        "credential_modes": [],
        "delegated_authorization": False,
        "modules": ["account", "market", "partner", *(["trade"] if trade_enabled else [])],
    }
    if (
        stored != _canonical_digest(manifest_payload)
        or any(manifest.get(field) != item for field, item in expected_values.items())
        or any(
            manifest.get(field) != expected_manifest[field]
            for field in ("tool_set_digest", "schema_digest", "manifest_digest")
        )
    ):
        raise McpGatewayError(
            "mcp_contract_mismatch", "The WEEX MCP request-description contract does not match Trader."
        )
    return {"method": method, "url": url, "headers": dict(headers), "query": dict(query), "body": dict(body)}


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
    if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
        raise ValueError("name and arguments are required.")
    if (
        not isinstance(trade_enabled, bool)
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("trade_enabled and timeout_seconds are invalid.")
    try:
        mapping = (mapping_loader or _load_default_mapping)()
        candidate, expected = _select_verified_candidate(
            mapping,
            trade_enabled=trade_enabled,
        )
    except McpGatewayError:
        raise
    except Exception as exc:
        raise McpGatewayError(
            "mcp_mapping_invalid",
            "The Trader MCP capability mapping is unavailable or invalid.",
        ) from exc
    try:
        runtime = (runtime_loader or _load_default_runtime)(str(candidate["package_version"]))
    except McpGatewayError:
        raise
    except Exception as exc:
        raise McpGatewayError(
            "mcp_runtime_incompatible",
            "The installed WEEX MCP request-description API is unavailable.",
        ) from exc
    if not _runtime_symbols_are_valid(runtime) or runtime.package_version != candidate["package_version"]:
        raise McpGatewayError(
            "mcp_runtime_incompatible", "The installed WEEX MCP request-description API is unavailable."
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
        description = runtime.describe_request(settings=settings, name=name, arguments=dict(arguments))
    except Exception as exc:
        code, message = getattr(exc, "code", None), getattr(exc, "message", None)
        if isinstance(code, str) and code and isinstance(message, str) and message:
            raise McpGatewayError(code, message) from exc
        raise McpGatewayError(
            "mcp_request_description_failed", "The verified WEEX MCP request-description API failed."
        ) from exc
    request = _validate_request_description(
        description,
        name=name,
        arguments=arguments,
        candidate=candidate,
        expected_manifest=expected,
        trade_enabled=trade_enabled,
        effective_spot_base_url=effective_spot_base_url,
        effective_futures_base_url=effective_futures_base_url,
    )
    return {
        "structured_content": {"ok": True, "result": {"request": request}},
        "is_error": False,
        "transport_meta": None,
    }


class VaultSecretProvider:
    """Resolve a profile-bound credential only from trusted invocation context."""

    def __init__(
        self,
        *,
        profile_id: str,
        account_binding_id: str,
        binding_id: str,
        fingerprint_key: bytes,
        credential_loader: Callable[[str], Any],
        runtime: Any,
    ) -> None:
        if (
            not all(isinstance(value, str) and value for value in (profile_id, account_binding_id, binding_id))
            or not isinstance(fingerprint_key, bytes)
            or len(fingerprint_key) < 32
        ):
            raise ValueError("VaultSecretProvider requires complete binding values.")
        self._profile_id = profile_id
        self._account_binding_id = account_binding_id
        self._binding_id = binding_id
        self._fingerprint_key = fingerprint_key
        self._credential_loader = credential_loader
        self._runtime = runtime

    def _error(self, code: str, message: str) -> Exception:
        return self._runtime.SecretProviderError(code, message)

    def _load_values(self) -> tuple[str, str, str]:
        try:
            credentials = self._credential_loader(self._profile_id)
            values = (
                getattr(credentials, "api_key"),
                getattr(credentials, "api_secret"),
                getattr(credentials, "api_passphrase"),
            )
        except Exception as exc:
            raise self._error(
                "secret_provider_unconfigured", "Secret provider is not configured for private WEEX API calls."
            ) from exc
        if not all(isinstance(value, str) and value for value in values):
            raise self._error("credential_invalid", "Credential provider rejected the credential binding.")
        return values

    def _fingerprint(self, values: tuple[str, str, str]) -> str:
        canonical = json.dumps(
            {
                "profile_id": self._profile_id,
                "api_key": values[0],
                "api_secret": values[1],
                "api_passphrase": values[2],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            "fp_"
            + hmac.new(
                self._fingerprint_key, _CREDENTIAL_FINGERPRINT_DOMAIN + canonical.encode("utf-8"), hashlib.sha256
            ).hexdigest()
        )

    def _current_credential_fingerprint(self) -> str:
        return self._fingerprint(self._load_values())

    def resolve(self, *, invocation_context: Any, required_capabilities: frozenset[str]) -> Any:
        account = getattr(invocation_context, "account_binding", None)
        binding = getattr(invocation_context, "credential_binding", None)
        if (
            account is None
            or binding is None
            or getattr(account, "profile_id", None) != self._profile_id
            or getattr(account, "account_binding_id", None) != self._account_binding_id
            or getattr(binding, "binding_id", None) != self._binding_id
        ):
            raise self._error("credential_invalid", "Credential provider rejected the credential binding.")
        host_capabilities = frozenset(required_capabilities) - _UPSTREAM_PERMISSIONS
        if not host_capabilities.issubset(getattr(binding, "host_policy_capabilities", frozenset())):
            raise self._error("permission_denied", "Credential binding does not allow the required capability.")
        evidence = getattr(binding, "upstream_permission_evidence", {})
        if any(
            evidence.get(permission) in {None, "denied"}
            for permission in frozenset(required_capabilities) & _UPSTREAM_PERMISSIONS
        ):
            raise self._error(
                "permission_denied", "Credential binding does not allow the required upstream permission."
            )
        values = self._load_values()
        fingerprint = self._fingerprint(values)
        bound_fingerprint = getattr(binding, "credential_fingerprint", None)
        if not isinstance(bound_fingerprint, str) or not hmac.compare_digest(
            fingerprint,
            bound_fingerprint,
        ):
            raise self._error("credential_invalid", "Credential rotation invalidated the active binding.")
        verified_permissions = frozenset(
            permission
            for permission in frozenset(required_capabilities) & _UPSTREAM_PERMISSIONS
            if evidence.get(permission) == "verified"
        )
        return self._runtime.ResolvedWeexCredential(
            key_id=self._profile_id,
            api_key=values[0],
            api_secret=values[1],
            api_passphrase=values[2],
            permissions=verified_permissions,
            fingerprint=fingerprint,
            valid_until=None,
        )


class _PublicOnlySecretProvider:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def resolve(self, **_kwargs: Any) -> Any:
        raise self._runtime.SecretProviderError(
            "secret_provider_unconfigured",
            "Secret provider is not configured for private WEEX API calls.",
        )


class LocalMcpGateway:
    """Immutable Local User composition bound to one profile and route pair."""

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
        boundary_contract_loader: Callable[[], dict[str, Any]] | None = None,
        profile_loader: Callable[[str], Any] | None = None,
        credential_loader: Callable[[str], Any] | None = None,
        binding_key_loader: Callable[[], str] | None = None,
        binding_key_status_loader: Callable[[], dict[str, Any]] | None = None,
        grant_consumer: Any = None,
    ) -> None:
        if profile_id is not None and (not isinstance(profile_id, str) or not profile_id):
            raise ValueError("profile_id must be a non-empty string or None.")
        if not all(
            isinstance(value, str) and value
            for value in (effective_spot_base_url, effective_futures_base_url, request_locale)
        ):
            raise ValueError("Gateway routes and locale must be non-empty strings.")
        if (
            not isinstance(trade_enabled, bool)
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Gateway trade mode and timeout are invalid.")
        if trade_enabled and profile_id is None:
            raise McpGatewayError(
                "profile_required",
                "A saved profile is required for private MCP tools.",
            )
        try:
            self._mapping = (mapping_loader or _load_default_mapping)()
            candidate, expected = _select_verified_candidate(
                self._mapping,
                trade_enabled=trade_enabled,
            )
            boundary = (boundary_contract_loader or _load_default_boundary_contract)()
            host_capabilities, upstream_evidence = _select_boundary_contract(
                boundary,
                candidate=candidate,
            )
        except McpGatewayError:
            raise
        except Exception as exc:
            raise McpGatewayError(
                "mcp_mapping_invalid",
                "The Trader MCP capability mapping is unavailable or invalid.",
            ) from exc
        try:
            runtime = (runtime_loader or _load_default_runtime)(str(candidate["package_version"]))
        except McpGatewayError:
            raise
        except Exception as exc:
            raise McpGatewayError(
                "mcp_runtime_incompatible",
                "The installed WEEX MCP package could not be loaded.",
            ) from exc
        if not _runtime_symbols_are_valid(runtime):
            raise McpGatewayError(
                "mcp_runtime_incompatible",
                "The installed WEEX MCP package does not expose the required Local runtime API.",
            )
        if runtime.package_version != candidate["package_version"]:
            raise McpGatewayError(
                "mcp_version_mismatch", "The installed WEEX MCP package version does not match the verified contract."
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
        self._host_capabilities = host_capabilities
        self._upstream_evidence = upstream_evidence
        self._binding_key_status_loader = binding_key_status_loader or _default_binding_key_status_loader
        self._binding_key_fingerprint: str | None = None
        self._secret_provider: Any = _PublicOnlySecretProvider(runtime)
        self._account_binding_id: str | None = None
        self._credential_binding_id: str | None = None
        if profile_id is not None:
            try:
                binding_key = (binding_key_loader or _default_binding_key_loader)()
            except Exception as exc:
                raise McpGatewayError(
                    "binding_key_required", "A valid unlocked local MCP binding key is required."
                ) from exc
            if not isinstance(binding_key, str) or len(binding_key.encode("utf-8")) < 32:
                raise McpGatewayError("binding_key_required", "A valid unlocked local MCP binding key is required.")
            self._binding_key_fingerprint = _key_fingerprint(binding_key)
            self._require_current_binding_key()
            binding_bytes = binding_key.encode("utf-8")
            self._account_binding_id = "acct_" + _safe_hmac(binding_bytes, _ACCOUNT_BINDING_DOMAIN, profile_id).hex()
            self._credential_binding_id = (
                "cred_" + _safe_hmac(binding_bytes, _CREDENTIAL_BINDING_DOMAIN, profile_id).hex()
            )
            fingerprint_key = _safe_hmac(
                binding_bytes, _CREDENTIAL_FINGERPRINT_DOMAIN, candidate["boundary_contract_digest"]
            )
            self._secret_provider = VaultSecretProvider(
                profile_id=profile_id,
                account_binding_id=self._account_binding_id,
                binding_id=self._credential_binding_id,
                fingerprint_key=fingerprint_key,
                credential_loader=active_credential_loader,
                runtime=runtime,
            )
        settings = runtime.Settings(
            trade_enabled=trade_enabled,
            spot_base_url=effective_spot_base_url,
            futures_base_url=effective_futures_base_url,
            market_timeout_seconds=self.timeout_seconds,
            market_max_attempts=1,
            market_retry_base_delay_seconds=0.0,
            account_timeout_seconds=self.timeout_seconds,
            partner_timeout_seconds=self.timeout_seconds,
            request_locale=request_locale,
        )
        service_arguments: dict[str, Any] = {"settings": settings, "secret_provider": self._secret_provider}
        if grant_consumer is not None:
            service_arguments["grant_consumer"] = grant_consumer
        try:
            self._service = runtime.build_local_user_service(**service_arguments)
        except Exception as exc:
            raise McpGatewayError(
                "mcp_runtime_incompatible", "The WEEX MCP Local runtime could not be constructed."
            ) from exc
        self._request_counter = 0
        self._session_id = (
            "gateway_"
            + hashlib.sha256(
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
        )
        self._latest_availability: dict[str, Any] = {}
        self._latest_manifests: dict[str, dict[str, Any]] = {}
        self._tools = self._fresh_preflight()

    def _next_id(self, label: str) -> str:
        self._request_counter += 1
        return f"gateway-{label}-{self._request_counter}"

    def _jsonrpc_result(self, response: Any, *, operation: str, expected_id: str) -> dict[str, Any]:
        if (
            not isinstance(response, dict)
            or response.get("jsonrpc") != "2.0"
            or response.get("id") != expected_id
            or "error" in response
            or not isinstance(response.get("result"), dict)
        ):
            raise McpGatewayError("mcp_protocol_error", f"WEEX MCP {operation} failed.")
        return response["result"]

    def _fresh_preflight(self) -> dict[str, dict[str, Any]]:
        discover_id, list_id = self._next_id("discover"), self._next_id("list")
        try:
            discover = self._jsonrpc_result(
                self._service.handle_jsonrpc(
                    {"jsonrpc": "2.0", "id": discover_id, "method": "server/discover", "params": {}}
                ),
                operation="discovery",
                expected_id=discover_id,
            )
            listed = self._jsonrpc_result(
                self._service.handle_jsonrpc({"jsonrpc": "2.0", "id": list_id, "method": "tools/list", "params": {}}),
                operation="tool listing",
                expected_id=list_id,
            )
        except McpGatewayError:
            raise
        except Exception as exc:
            raise McpGatewayError("mcp_protocol_error", "WEEX MCP compatibility preflight failed.") from exc
        metadata_value = discover.get("_meta")
        weex_metadata = metadata_value.get("com.weex/mcp") if isinstance(metadata_value, dict) else None
        active = weex_metadata.get("compatibility") if isinstance(weex_metadata, dict) else None
        static = weex_metadata.get("boundaryStaticManifest") if isinstance(weex_metadata, dict) else None
        dynamic = weex_metadata.get("runtimeAvailability") if isinstance(weex_metadata, dict) else None
        tools = listed.get("tools")
        if not all(isinstance(value, expected) for value, expected in ((active, dict), (static, dict), (tools, list))):
            raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP compatibility contract is missing.")
        tools_by_name: dict[str, dict[str, Any]] = {}
        schemas: list[dict[str, Any]] = []
        projections: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP tool catalog is invalid.")
            name = tool.get("name")
            input_schema, output_schema = tool.get("inputSchema"), tool.get("outputSchema")
            annotations, tool_meta = tool.get("annotations"), tool.get("_meta")
            if (
                not isinstance(name, str)
                or not name
                or name in tools_by_name
                or not all(isinstance(value, dict) for value in (input_schema, output_schema, annotations, tool_meta))
            ):
                raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP tool catalog is invalid.")
            tools_by_name[name] = tool
            schemas.append({"name": name, "inputSchema": input_schema, "outputSchema": output_schema})
            projections.append(
                {
                    "name": name,
                    "inputSchema": input_schema,
                    "outputSchema": output_schema,
                    "annotations": annotations,
                    "_meta": tool_meta,
                }
            )
        names = sorted(tools_by_name)
        schemas.sort(key=lambda item: item["name"])
        projections.sort(key=lambda item: item["name"])
        _validate_active_manifest(
            active,
            candidate=self._expected_candidate,
            expected_profile=self._expected_manifest,
            trade_enabled=self.trade_enabled,
            names=names,
            schemas=schemas,
        )
        _validate_static_manifest(
            static,
            candidate=self._expected_candidate,
            expected_profile=self._expected_manifest,
            names=names,
            projections=projections,
        )
        availability = _validate_runtime_availability(dynamic, names=names, trade_enabled=self.trade_enabled)
        self._latest_availability = availability
        self._latest_manifests = {"active": dict(active), "static": dict(static)}
        return tools_by_name

    def _require_current_binding_key(self) -> None:
        if self.profile_id is None:
            return
        try:
            status = self._binding_key_status_loader()
        except Exception as exc:
            raise McpGatewayError(
                "binding_key_required", "A valid unlocked local MCP binding key is required."
            ) from exc
        fingerprint = status.get("fp_sha256") if isinstance(status, dict) else None
        if (
            not isinstance(status, dict)
            or status.get("present") is not True
            or status.get("valid") is not True
            or isinstance(status.get("length"), bool)
            or not isinstance(status.get("length"), int)
            or status["length"] < 32
            or not isinstance(fingerprint, str)
            or len(fingerprint) != 16
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise McpGatewayError("binding_key_required", "A valid unlocked local MCP binding key is required.")
        if not hmac.compare_digest(fingerprint, self._binding_key_fingerprint or ""):
            raise McpGatewayError("binding_key_changed", "The local MCP binding key changed; rebuild the Gateway.")

    def _context(self, *, execution_grant: Any, request_id: str) -> Any:
        if self.profile_id is None or not isinstance(self._secret_provider, VaultSecretProvider):
            raise McpGatewayError("profile_required", "A saved profile is required for private MCP tools.")
        self._require_current_binding_key()
        fingerprint = self._secret_provider._current_credential_fingerprint()
        route_fingerprint = _canonical_digest(
            {"spot": self.effective_spot_base_url, "futures": self.effective_futures_base_url}
        )
        return self._runtime.AuthenticatedInvocationContext(
            principal=self._runtime.VerifiedPrincipal(
                principal_id=self.profile_id,
                tenant_id="weex-trader-local",
                authn_method="local_profile_vault",
                authenticated_at=int(time.time() * 1000),
                trust_level="local_process",
            ),
            account_binding=self._runtime.AccountBinding(
                account_binding_id=self._account_binding_id,
                profile_id=self.profile_id,
                allowed_markets=frozenset({"spot", "futures"}),
                route_fingerprint=route_fingerprint,
            ),
            credential_binding=self._runtime.CredentialBinding(
                binding_id=self._credential_binding_id,
                credential_fingerprint=fingerprint,
                host_policy_capabilities=self._host_capabilities,
                upstream_permission_evidence=dict(self._upstream_evidence),
                valid_until=None,
            ),
            execution_grant=execution_grant,
            correlation=self._runtime.CorrelationContext(request_id=request_id, session_id=self._session_id),
        )

    @staticmethod
    def _tool_module(tool: Mapping[str, Any]) -> str:
        metadata_value = tool.get("_meta")
        weex_metadata = metadata_value.get("com.weex/mcp") if isinstance(metadata_value, dict) else None
        module = weex_metadata.get("module") if isinstance(weex_metadata, dict) else None
        if module not in {"market", "account", "partner", "trade"}:
            raise McpGatewayError("mcp_contract_mismatch", "The WEEX MCP tool module is invalid.")
        return module

    def runtime_status(self) -> dict[str, Any]:
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
                action_required = "repair_mcp_contract" if exc.code == "mcp_contract_mismatch" else "repair_mcp_runtime"
            except Exception:
                action_required = "repair_mcp_runtime"
            else:
                manifest_ok = True
        active = self._latest_manifests.get("active", {})
        static = self._latest_manifests.get("static", {})
        availability = self._latest_availability
        return {
            "installed": True,
            "version": version,
            "expected_version": expected_version,
            "package_ok": package_ok,
            "manifest_ok": manifest_ok,
            "runtime_profile": LOCAL_RUNTIME_PROFILE,
            "trade_enabled": self.trade_enabled,
            "manifest_version": self._expected_candidate["manifest_version"],
            "tool_contract_version": self._expected_candidate["tool_contract_version"],
            "manifest_digest": active.get("manifest_digest") or self._expected_manifest["manifest_digest"],
            "boundary_manifest_digest": static.get("manifest_digest")
            or self._expected_manifest["boundary_static_manifest_digest"],
            "boundary_contract_digest": self._expected_candidate["boundary_contract_digest"],
            "private_read_ready": availability.get("private_read_ready") is True,
            "mutation_ready": availability.get("mutation_ready") is True,
            "runtime_action_required": list(availability.get("action_required", [])),
            "action_required": action_required,
        }

    def prepare_mutation(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
            raise McpGatewayError("invalid_arguments", "A typed mutation tool and arguments are required.")
        unsafe = _CALLER_FORBIDDEN_ARGUMENTS.intersection(arguments)
        if unsafe:
            raise McpGatewayError(
                "invalid_arguments", "Caller-controlled credential or authorization fields are forbidden."
            )
        tools = self._fresh_preflight()
        tool = tools.get(name)
        if tool is None or self._tool_module(tool) != "trade":
            raise McpGatewayError("unsupported_tool", "The typed mutation tool is not in the verified Local catalog.")
        request_id = self._next_id("prepare")
        context = self._context(execution_grant=None, request_id=request_id)
        try:
            prepared = self._service.prepare_mutation(
                tool_name=name,
                arguments=dict(arguments),
                invocation_context=context,
            )
            payload = prepared.to_dict()
        except McpGatewayError:
            raise
        except Exception as exc:
            raise McpGatewayError("mcp_protocol_error", "WEEX MCP mutation preparation failed.") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("tool_name") != name
            or _safe_digest(payload.get("prepared_artifact_digest")) is None
            or _safe_digest(payload.get("execution_binding_digest")) is None
        ):
            raise McpGatewayError("mcp_protocol_error", "WEEX MCP returned an invalid prepared mutation.")
        return _copy_json_object(payload)

    def call_typed_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        execution_grant: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
            raise McpGatewayError("invalid_arguments", "A typed MCP tool and arguments are required.")
        if _CALLER_FORBIDDEN_ARGUMENTS.intersection(arguments):
            raise McpGatewayError(
                "invalid_arguments", "Caller-controlled credential or authorization fields are forbidden."
            )
        tools = self._fresh_preflight()
        tool = tools.get(name)
        if tool is None:
            raise McpGatewayError("unsupported_tool", "The typed MCP tool is not in the verified Local catalog.")
        module = self._tool_module(tool)
        if module == "trade":
            if not isinstance(execution_grant, self._runtime.ExecutionGrant):
                raise McpGatewayError(
                    "execution_grant_required", "A verified execution grant is required for Direct mutation."
                )
        elif execution_grant is not None:
            raise McpGatewayError("invalid_arguments", "Execution grants are valid only for Direct mutation tools.")
        call_id = self._next_id("call")
        context = self._context(execution_grant=execution_grant, request_id=call_id) if module != "market" else None
        try:
            response = self._service.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": call_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": dict(arguments)},
                },
                context=context,
            )
        except Exception as exc:
            raise McpGatewayError("mcp_protocol_error", "WEEX MCP tool call failed.") from exc
        result = self._jsonrpc_result(response, operation="tool call", expected_id=call_id)
        payload, is_error = result.get("structuredContent"), result.get("isError")
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("ok"), bool)
            or not isinstance(is_error, bool)
            or is_error is payload["ok"]
        ):
            raise McpGatewayError("mcp_protocol_error", "WEEX MCP returned an invalid tool envelope.")
        metadata_value = result.get("_meta")
        if metadata_value is None:
            transport_meta = None
        else:
            if not isinstance(metadata_value, dict) or set(metadata_value) != {"com.weex/transport"}:
                raise McpGatewayError("mcp_protocol_error", "WEEX MCP returned invalid transport metadata.")
            transport_meta = _validate_transport_meta(metadata_value["com.weex/transport"])
        return {
            "structured_content": _copy_json_object(payload),
            "is_error": is_error,
            "transport_meta": transport_meta,
        }


def _probe_status(
    *,
    installed: bool | None = None,
    version: str | None,
    expected_version: str | None,
    package_ok: bool,
    manifest_ok: bool,
    trade_enabled: bool,
    manifest_version: int | None,
    tool_contract_version: str | None,
    manifest_digest: str | None,
    boundary_manifest_digest: str | None,
    boundary_contract_digest: str | None,
    private_read_ready: bool,
    mutation_ready: bool,
    runtime_action_required: list[str],
    action_required: str | None,
) -> dict[str, Any]:
    safe_version = _safe_package_version(version)
    safe_expected = _safe_package_version(expected_version)
    safe_tool = _safe_contract_version(tool_contract_version)
    safe_manifest = _safe_digest(manifest_digest)
    safe_boundary_manifest = _safe_digest(boundary_manifest_digest)
    safe_boundary = _safe_digest(boundary_contract_digest)
    safe_action = action_required if action_required in _PROBE_ACTIONS else "repair_mcp_runtime"
    actions_ok = (
        isinstance(runtime_action_required, list)
        and all(isinstance(item, str) and item in _RUNTIME_ACTIONS for item in runtime_action_required)
        and len(runtime_action_required) == len(set(runtime_action_required))
    )
    safe_package_ok = bool(package_ok and safe_version and safe_version == safe_expected)
    safe_manifest_ok = bool(
        manifest_ok
        and safe_package_ok
        and manifest_version == 2
        and safe_tool == "2"
        and safe_manifest
        and safe_boundary_manifest
        and safe_boundary
        and actions_ok
        and action_required is None
    )
    if safe_manifest_ok:
        safe_action = None
    elif action_required is None:
        safe_action = "repair_mcp_runtime"
    return {
        "installed": installed if isinstance(installed, bool) else safe_version is not None,
        "version": safe_version,
        "expected_version": safe_expected,
        "package_ok": safe_package_ok,
        "manifest_ok": safe_manifest_ok,
        "runtime_profile": LOCAL_RUNTIME_PROFILE,
        "trade_enabled": trade_enabled if isinstance(trade_enabled, bool) else False,
        "manifest_version": manifest_version if manifest_version == 2 else None,
        "tool_contract_version": safe_tool,
        "manifest_digest": safe_manifest,
        "boundary_manifest_digest": safe_boundary_manifest,
        "boundary_contract_digest": safe_boundary,
        "private_read_ready": private_read_ready is True,
        "mutation_ready": mutation_ready is True,
        "runtime_action_required": list(runtime_action_required) if actions_ok else [],
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
    boundary_contract_loader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mapping_loader = mapping_loader or _load_default_mapping
    expected_version: str | None = None
    candidate: dict[str, Any] | None = None
    expected: dict[str, Any] | None = None
    try:
        mapping = mapping_loader()
        candidate, expected = _select_verified_candidate(mapping, trade_enabled=trade_enabled)
        expected_version = candidate["package_version"]
        boundary = (boundary_contract_loader or _load_default_boundary_contract)()
        _select_boundary_contract(boundary, candidate=candidate)
    except Exception:
        return _probe_status(
            version=_installed_package_version() if runtime_loader is None else None,
            expected_version=None,
            package_ok=False,
            manifest_ok=False,
            trade_enabled=trade_enabled,
            manifest_version=None,
            tool_contract_version=None,
            manifest_digest=None,
            boundary_manifest_digest=None,
            boundary_contract_digest=None,
            private_read_ready=False,
            mutation_ready=False,
            runtime_action_required=[],
            action_required="repair_mcp_mapping",
        )
    try:
        runtime = (runtime_loader or _load_default_runtime)(expected_version)
    except McpGatewayError as exc:
        action = (
            "install_verified_mcp_candidate"
            if exc.code in {"mcp_package_missing", "mcp_version_mismatch"}
            else "repair_mcp_runtime"
        )
        return _probe_status(
            version=_installed_package_version() if runtime_loader is None else None,
            expected_version=expected_version,
            package_ok=False,
            manifest_ok=False,
            trade_enabled=trade_enabled,
            manifest_version=candidate["manifest_version"],
            tool_contract_version=candidate["tool_contract_version"],
            manifest_digest=expected["manifest_digest"],
            boundary_manifest_digest=expected["boundary_static_manifest_digest"],
            boundary_contract_digest=candidate["boundary_contract_digest"],
            private_read_ready=False,
            mutation_ready=False,
            runtime_action_required=[],
            action_required=action,
        )
    except Exception:
        return _probe_status(
            version=None,
            expected_version=expected_version,
            package_ok=False,
            manifest_ok=False,
            trade_enabled=trade_enabled,
            manifest_version=candidate["manifest_version"],
            tool_contract_version=candidate["tool_contract_version"],
            manifest_digest=expected["manifest_digest"],
            boundary_manifest_digest=expected["boundary_static_manifest_digest"],
            boundary_contract_digest=candidate["boundary_contract_digest"],
            private_read_ready=False,
            mutation_ready=False,
            runtime_action_required=[],
            action_required="repair_mcp_runtime",
        )
    probe_profile = profile_id or ("mcp-runtime-probe" if trade_enabled else None)
    probe_key = "mcp-runtime-probe-binding-key-00000000000000000000000000000000"
    probe_status = {"present": True, "valid": True, "length": len(probe_key), "fp_sha256": _key_fingerprint(probe_key)}
    try:
        gateway = LocalMcpGateway(
            profile_id=probe_profile,
            effective_spot_base_url=DEFAULT_SPOT_BASE_URL,
            effective_futures_base_url=DEFAULT_FUTURES_BASE_URL,
            trade_enabled=trade_enabled,
            timeout_seconds=15.0,
            runtime_loader=lambda _expected: runtime,
            mapping_loader=lambda: mapping,
            boundary_contract_loader=lambda: boundary,
            profile_loader=lambda value: {"profile_id": value},
            credential_loader=lambda _value: (_ for _ in ()).throw(
                RuntimeError("Runtime probe does not resolve credentials.")
            ),
            binding_key_loader=lambda: probe_key,
            binding_key_status_loader=lambda: dict(probe_status),
        )
        return gateway.runtime_status()
    except McpGatewayError as exc:
        if exc.code == "mcp_contract_mismatch":
            action = "repair_mcp_contract"
        elif exc.code in {"mcp_package_missing", "mcp_version_mismatch"}:
            action = "install_verified_mcp_candidate"
        else:
            action = "repair_mcp_runtime"
    except Exception:
        action = "repair_mcp_runtime"
    return _probe_status(
        version=getattr(runtime, "package_version", None),
        expected_version=expected_version,
        package_ok=getattr(runtime, "package_version", None) == expected_version,
        manifest_ok=False,
        trade_enabled=trade_enabled,
        manifest_version=candidate["manifest_version"],
        tool_contract_version=candidate["tool_contract_version"],
        manifest_digest=expected["manifest_digest"],
        boundary_manifest_digest=expected["boundary_static_manifest_digest"],
        boundary_contract_digest=candidate["boundary_contract_digest"],
        private_read_ready=False,
        mutation_ready=False,
        runtime_action_required=[],
        action_required=action,
    )


__all__ = [
    "LocalMcpGateway",
    "McpGatewayError",
    "VaultSecretProvider",
    "describe_local_mcp_request",
    "probe_local_mcp_runtime",
]
