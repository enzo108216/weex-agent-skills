#!/usr/bin/env python3
"""Strict read-only WEEX Partner compatibility executor backed by Local MCP."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence
from urllib import parse

from weex_agent_state import (
    ensure_private_runtime_ready,
    project_mcp_status,
    refresh_agent_records,
)
from weex_mcp_partner_compat import (
    LocalMcpPartnerSettings,
    McpPartnerCompatibilityError,
    McpPartnerFacade,
)
from weex_url_policy import REDACTED_WEEX_AUTH_HEADER_NAMES


PARTNER_BASE_URL = "https://api-spot.weex.com"
PARTNER_PRODUCTION_ENVIRONMENT = "partner_production"
PARTNER_TEST_ENVIRONMENT = "partner_test"
PARTNER_ENVIRONMENTS = frozenset(
    {PARTNER_PRODUCTION_ENVIRONMENT, PARTNER_TEST_ENVIRONMENT}
)
PARTNER_TEST_HOST_SUFFIX = ".weex.tech"
HOST_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
DEFAULT_TIMEOUT = 30.0
DEFAULT_LOCALE = "en-US"
PARTNER_OVERRIDE_ENV_VARS = (
    "WEEX_PARTNER_API_BASE",
    "WEEX_SPOT_API_BASE",
    "WEEX_API_BASE",
)
class PartnerPolicyError(ValueError):
    """Raised before credentials are loaded when a Partner request is unsafe."""


@dataclass(frozen=True)
class Endpoint:
    key: str
    category: str
    title: str
    method: str
    path: str
    requires_auth: bool
    operation_class: str
    weight: int
    query_fields: tuple[str, ...]
    body_fields: tuple[str, ...]
    doc_url: str


def _definition_path() -> Path:
    return Path(__file__).resolve().parent.parent / "references" / "partner-api-definitions.json"


def load_endpoint_map() -> Dict[str, Endpoint]:
    payload = json.loads(_definition_path().read_text(encoding="utf-8"))
    endpoints: Dict[str, Endpoint] = {}
    for item in payload.get("definitions", []):
        endpoint = Endpoint(
            key=str(item["key"]),
            category=str(item.get("category", "partner")),
            title=str(item.get("title", "")),
            method=str(item["method"]).upper(),
            path=str(item["path"]),
            requires_auth=bool(item.get("requires_auth", True)),
            operation_class=str(item.get("operation_class", "")),
            weight=int(item.get("weight", 0)),
            query_fields=tuple(str(value) for value in item.get("query_fields", [])),
            body_fields=tuple(str(value) for value in item.get("body_fields", [])),
            doc_url=str(item.get("doc_url", "")),
        )
        if endpoint.operation_class != "read":
            raise PartnerPolicyError(f"Partner endpoint {endpoint.key!r} is not read-only")
        endpoints[endpoint.key] = endpoint
    if len(endpoints) != 7:
        raise PartnerPolicyError("Partner endpoint allowlist must contain exactly seven entries")
    return endpoints


ENDPOINTS = load_endpoint_map()


def _invalid_partner_origin() -> PartnerPolicyError:
    return PartnerPolicyError(
        "Partner credentials may only be sent to the production origin or a saved "
        "HTTPS subdomain under the approved WEEX test-domain suffix."
    )


def _validate_host_labels(hostname: str) -> None:
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _invalid_partner_origin() from exc
    labels = hostname.split(".")
    if not labels or any(not HOST_LABEL_PATTERN.fullmatch(label) for label in labels):
        raise _invalid_partner_origin()


def classify_partner_origin(raw_url: str) -> tuple[str, str]:
    value = str(raw_url or "")
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise _invalid_partner_origin()
    try:
        parsed = parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise _invalid_partner_origin() from exc
    hostname = parsed.hostname or ""
    _validate_host_labels(hostname)
    if (
        parsed.scheme != "https"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise _invalid_partner_origin()
    canonical_origin = f"https://{hostname}"
    if value not in {canonical_origin, f"{canonical_origin}/"}:
        raise _invalid_partner_origin()
    if canonical_origin == PARTNER_BASE_URL:
        return canonical_origin, PARTNER_PRODUCTION_ENVIRONMENT
    if hostname != "weex.tech" and hostname.endswith(PARTNER_TEST_HOST_SUFFIX):
        return canonical_origin, PARTNER_TEST_ENVIRONMENT
    raise _invalid_partner_origin()


def validate_partner_base_url(raw_url: str) -> str:
    return classify_partner_origin(raw_url)[0]


def resolve_partner_origin(
    profile_spot_base_url: str = "",
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    configured_names = sorted(
        name
        for name in PARTNER_OVERRIDE_ENV_VARS
        if str(env.get(name, "")).strip()
    )
    if configured_names:
        raise PartnerPolicyError(
            "Partner requests do not accept API base URL environment overrides "
            f"({', '.join(configured_names)})."
        )
    profile_origin = str(profile_spot_base_url or "")
    if not profile_origin:
        return PARTNER_BASE_URL, PARTNER_PRODUCTION_ENVIRONMENT
    return classify_partner_origin(profile_origin)


def reject_partner_base_overrides(
    *,
    profile_spot_base_url: str = "",
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    resolve_partner_origin(profile_spot_base_url, environ)


def _partner_preflight_runtime_summary(records: Mapping[str, Any]) -> Dict[str, Any]:
    runtime = records.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    host = runtime.get("host")
    host = host if isinstance(host, Mapping) else {}
    env_validation = runtime.get("env_validation")
    env_validation = env_validation if isinstance(env_validation, Mapping) else {}
    missing_modules = host.get("missing_modules")
    missing_modules = missing_modules if isinstance(missing_modules, list) else []
    issues = env_validation.get("issues")
    issues = issues if isinstance(issues, list) else []
    mcp = project_mcp_status(runtime.get("mcp"))
    return {
        "requirements_ready": bool(host.get("requirements_ready")),
        "missing_modules": [str(value) for value in missing_modules],
        "env_validation": {
            "ok": bool(env_validation.get("ok")),
            "issue_count": len(issues),
        },
        "mcp": mcp,
    }


def _partner_preflight_vault_summary(records: Mapping[str, Any]) -> Dict[str, Any]:
    runtime = records.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    vault = runtime.get("vault")
    vault = vault if isinstance(vault, Mapping) else {}
    return {
        "configured": bool(vault.get("configured")),
        "state": str(vault.get("state") or "unknown"),
        "action_required": vault.get("action_required"),
    }


def _partner_preflight_failure(
    *,
    profile_ref: str,
    runtime: Mapping[str, Any],
    vault: Mapping[str, Any],
    category: str,
    code: str,
    message: str,
    recovery_action: str,
    environment: Optional[str] = None,
    profile: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "request_sent": False,
        "api_domain": "partner",
        "environment": environment,
        "capability_mode": "read_only_query",
        "runtime": dict(runtime),
        "vault": dict(vault),
        "profile": dict(profile or {"requested": profile_ref}),
        "error": {
            "category": category,
            "code": code,
            "message": message,
            "recovery_action": recovery_action,
        },
    }


def build_partner_preflight_envelope(
    records: Mapping[str, Any],
    *,
    profile_ref: str,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Project refreshed trader state into a Partner-safe tool response."""
    requested_profile = str(profile_ref or "").strip()
    runtime = _partner_preflight_runtime_summary(records)
    vault = _partner_preflight_vault_summary(records)
    if not requested_profile:
        return _partner_preflight_failure(
            profile_ref=requested_profile,
            runtime=runtime,
            vault=vault,
            category="local_policy",
            code="profile_required",
            message="Partner preflight requires a saved profile name or ID.",
            recovery_action="Choose one saved profile before running a Partner query.",
        )
    if (
        not runtime["requirements_ready"]
        or runtime["missing_modules"]
        or not runtime["env_validation"]["ok"]
        or runtime["mcp"]["installed"] is not True
        or runtime["mcp"]["package_ok"] is not True
        or runtime["mcp"]["manifest_ok"] is not True
        or runtime["mcp"]["runtime_profile"] != "local_user"
        or runtime["mcp"]["trade_enabled"] is not False
        or runtime["mcp"]["action_required"] is not None
    ):
        return _partner_preflight_failure(
            profile_ref=requested_profile,
            runtime=runtime,
            vault=vault,
            category="runtime_preflight",
            code="private_runtime_not_ready",
            message="Private runtime preflight failed before Partner profile resolution.",
            recovery_action="Repair the reported runtime dependency or environment validation issue, then rerun Partner preflight.",
        )

    init = records.get("init")
    init = init if isinstance(init, Mapping) else {}
    profiles = init.get("profiles")
    profiles = profiles if isinstance(profiles, Mapping) else {}
    summary = profiles.get("summary")
    summary = summary if isinstance(summary, list) else []
    matches = [
        item
        for item in summary
        if isinstance(item, Mapping)
        and requested_profile in {str(item.get("id") or ""), str(item.get("name") or "")}
    ]
    if len(matches) != 1:
        return _partner_preflight_failure(
            profile_ref=requested_profile,
            runtime=runtime,
            vault=vault,
            category="profile_vault",
            code="profile_unavailable",
            message="The requested saved profile could not be resolved uniquely.",
            recovery_action="Choose an existing saved profile by its exact name or stable ID.",
        )

    selected = matches[0]
    safe_profile = {
        "resolved_profile_id": str(selected.get("id") or ""),
        "name": str(selected.get("name") or ""),
    }
    try:
        _origin, environment = resolve_partner_origin(
            str(selected.get("spot_base_url") or ""),
            os.environ if environ is None else environ,
        )
    except PartnerPolicyError:
        return _partner_preflight_failure(
            profile_ref=requested_profile,
            runtime=runtime,
            vault=vault,
            profile=safe_profile,
            category="local_policy",
            code="invalid_partner_origin",
            message="The saved profile does not resolve to an allowed Partner environment.",
            recovery_action="Repair the saved profile Partner origin before querying.",
        )

    if not vault["configured"] or vault["state"] != "unlocked":
        return _partner_preflight_failure(
            profile_ref=requested_profile,
            runtime=runtime,
            vault=vault,
            profile=safe_profile,
            environment=environment,
            category="profile_vault",
            code="vault_unavailable",
            message="The Application Vault is not ready for a private Partner query.",
            recovery_action="Configure or unlock the existing Application Vault, then rerun Partner preflight.",
        )

    return {
        "ok": True,
        "request_sent": False,
        "api_domain": "partner",
        "environment": environment,
        "capability_mode": "read_only_query",
        "runtime": runtime,
        "vault": vault,
        "profile": safe_profile,
        "error": None,
    }


def _validate_endpoint_parameters(
    endpoint: Endpoint,
    query: Mapping[str, Any],
    body: Mapping[str, Any],
) -> None:
    if endpoint.operation_class != "read" or not endpoint.requires_auth:
        raise PartnerPolicyError(f"Partner endpoint {endpoint.key!r} is not an authenticated read")
    unknown_query = sorted(set(query) - set(endpoint.query_fields))
    unknown_body = sorted(set(body) - set(endpoint.body_fields))
    if unknown_query or unknown_body:
        raise PartnerPolicyError(
            f"Unexpected Partner parameters for {endpoint.key}: "
            f"query={unknown_query}, body={unknown_body}"
        )
    if endpoint.method == "GET" and body:
        raise PartnerPolicyError("GET Partner requests do not accept a body")
    if endpoint.method == "POST" and query:
        raise PartnerPolicyError("The read-only Partner POST does not accept query parameters")


def _replace_secret_values(value: str, secrets: Sequence[str]) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = re.sub(re.escape(secret), "***", result, flags=re.IGNORECASE)
    return result


def sanitize_for_output(value: Any, secret_values: Iterable[str] = ()) -> Any:
    secrets = tuple(str(secret) for secret in secret_values if str(secret))
    if isinstance(value, Mapping):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = _replace_secret_values(str(key), secrets)
            if key_text.upper() in REDACTED_WEEX_AUTH_HEADER_NAMES:
                sanitized[key_text] = "***"
            else:
                sanitized[key_text] = sanitize_for_output(item, secrets)
        return sanitized
    if isinstance(value, list):
        return [sanitize_for_output(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_output(item, secrets) for item in value)
    if isinstance(value, str):
        return _replace_secret_values(value, secrets)
    return value


def _partner_origin_secret_values(origin: str) -> tuple[str, ...]:
    hostname = parse.urlsplit(origin).hostname or ""
    values = {
        origin,
        hostname,
        origin.replace("/", "\\/"),
        parse.quote(origin, safe=""),
        parse.quote(hostname, safe=""),
    }
    return tuple(sorted((value for value in values if value), key=len, reverse=True))


def _normalize_partner_leak_text(value: str) -> str:
    normalized = value
    for _ in range(4):
        previous = normalized
        normalized = normalized.replace("\\/", "/")
        normalized = re.sub(r"\\u002f", "/", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\\u002e", ".", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\\u003a", ":", normalized, flags=re.IGNORECASE)
        normalized = parse.unquote(normalized)
        if normalized == previous:
            break
    return normalized.casefold()


def _contains_partner_origin(value: str, origin: str) -> bool:
    hostname = parse.urlsplit(origin).hostname or ""
    return bool(hostname) and _normalize_partner_leak_text(hostname) in _normalize_partner_leak_text(value)


def _redact_partner_origin_content(value: Any, origin: str) -> Any:
    if isinstance(value, Mapping):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            safe_key = "***" if _contains_partner_origin(key_text, origin) else key_text
            redacted[safe_key] = _redact_partner_origin_content(item, origin)
        return redacted
    if isinstance(value, list):
        return [_redact_partner_origin_content(item, origin) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_partner_origin_content(item, origin) for item in value)
    if isinstance(value, str) and _contains_partner_origin(value, origin):
        return "***"
    return value


def _partner_origin_remains(value: Any, origin: str) -> bool:
    if isinstance(value, Mapping):
        return any(
            _contains_partner_origin(str(key), origin)
            or _partner_origin_remains(item, origin)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_partner_origin_remains(item, origin) for item in value)
    return isinstance(value, str) and _contains_partner_origin(value, origin)


def sanitize_partner_result(
    value: Mapping[str, Any],
    *,
    origin: str,
    environment: str,
    secret_values: Iterable[str] = (),
) -> Dict[str, Any]:
    redacted_values = tuple(secret_values)
    if environment == PARTNER_TEST_ENVIRONMENT:
        redacted_values += _partner_origin_secret_values(origin)
    sanitized = sanitize_for_output(value, redacted_values)
    if environment == PARTNER_TEST_ENVIRONMENT:
        sanitized = _redact_partner_origin_content(sanitized, origin)
        if _partner_origin_remains(sanitized, origin):
            return {
                "ok": False,
                "status": None,
                "rate_limit": {"used": {}, "remaining": {}},
                "retry": {"automatic": False},
                "error": {
                    "category": "schema",
                    "http_status": None,
                    "code": "partner_output_redaction_failed",
                    "message": "Partner output redaction validation failed.",
                    "recovery_action": "Stop using this response and review the Partner output policy.",
                },
                "api_domain": "partner",
                "environment": PARTNER_TEST_ENVIRONMENT,
                "capability_mode": "read_only_query",
            }
    return dict(sanitized)


def _local_failure(
    *,
    endpoint: Endpoint,
    profile_ref: str,
    category: str,
    code: str,
    message: str,
    recovery_action: str,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "status": None,
        "rate_limit": {"used": {}, "remaining": {}},
        "retry": {"automatic": False},
        "error": {
            "category": category,
            "http_status": None,
            "code": code,
            "message": message,
            "recovery_action": recovery_action,
        },
        "endpoint": endpoint.key,
        "method": endpoint.method,
        "path": endpoint.path,
        "weight": endpoint.weight,
        "operation_class": endpoint.operation_class,
        "profile": {"requested": profile_ref},
        "api_domain": "partner",
        "environment": environment,
        "capability_mode": "read_only_query",
    }


def _load_profile_resolver():
    from weex_profile_store import resolve_profile

    return resolve_profile


def _default_preflight(**kwargs: Any) -> None:
    ensure_private_runtime_ready(
        command=str(kwargs.get("command", "partner.execute")),
        auto_setup=True,
        language=kwargs.get("language"),
    )


def execute_partner_request(
    payload: Mapping[str, Any],
    *,
    profile_resolver: Optional[Callable[[str], Any]] = None,
    preflight: Optional[Callable[..., None]] = None,
    mcp_executor: Optional[Callable[..., Dict[str, Any]]] = None,
    environ: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    endpoint_key = str(payload.get("endpoint", ""))
    endpoint = ENDPOINTS.get(endpoint_key)
    if endpoint is None or endpoint.operation_class != "read":
        raise PartnerPolicyError(f"Unsupported Partner read endpoint: {endpoint_key!r}")

    profile_ref = str(payload.get("profile", "")).strip()
    if not profile_ref:
        raise PartnerPolicyError("Partner requests require a saved profile name or ID")
    expected_environment = payload.get("expected_environment")
    if expected_environment is not None and (
        not isinstance(expected_environment, str)
        or expected_environment not in PARTNER_ENVIRONMENTS
    ):
        return _local_failure(
            endpoint=endpoint,
            profile_ref=profile_ref,
            category="local_policy",
            code="invalid_expected_environment",
            message="Expected Partner environment is invalid.",
            recovery_action="Use partner_production or partner_test after resolving the saved profile environment.",
        )
    raw_query = payload.get("query")
    raw_body = payload.get("body")
    query = {} if raw_query is None else raw_query
    body = {} if raw_body is None else raw_body
    if not isinstance(query, Mapping) or not isinstance(body, Mapping):
        return _local_failure(
            endpoint=endpoint,
            profile_ref=profile_ref,
            category="local_policy",
            code="invalid_request_container",
            message="Partner query and body must be JSON objects.",
            recovery_action="Submit query and body as JSON objects before resolving the saved profile.",
        )
    _validate_endpoint_parameters(endpoint, query, body)

    env = os.environ if environ is None else environ
    effective_timeout = timeout
    if effective_timeout is None:
        try:
            effective_timeout = float(env.get("WEEX_API_TIMEOUT", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            return _local_failure(
                endpoint=endpoint,
                profile_ref=profile_ref,
                category="local_configuration",
                code="invalid_api_timeout",
                message="WEEX_API_TIMEOUT must be a positive finite number.",
                recovery_action="Unset WEEX_API_TIMEOUT or set it to a positive number of seconds.",
            )
    if not math.isfinite(effective_timeout) or effective_timeout <= 0:
        return _local_failure(
            endpoint=endpoint,
            profile_ref=profile_ref,
            category="local_configuration",
            code="invalid_api_timeout",
            message="WEEX_API_TIMEOUT must be a positive finite number.",
            recovery_action="Unset WEEX_API_TIMEOUT or set it to a positive number of seconds.",
        )
    try:
        (preflight or _default_preflight)(
            command="partner.execute",
            language=payload.get("language"),
        )
    except Exception:
        return _local_failure(
            endpoint=endpoint,
            profile_ref=profile_ref,
            category="runtime_preflight",
            code="private_runtime_not_ready",
            message="Private runtime preflight failed before the Partner request.",
            recovery_action="Run the trader preflight/doctor guidance, repair the reported runtime issue, and retry.",
        )

    try:
        if profile_resolver is None:
            profile_resolver = _load_profile_resolver()
        profile = profile_resolver(profile_ref)
    except Exception:
        return _local_failure(
            endpoint=endpoint,
            profile_ref=profile_ref,
            category="profile_vault",
            code="profile_unavailable",
            message="The saved profile could not be resolved.",
            recovery_action="Open the trader profile manager and verify that the saved profile exists and is readable.",
        )
    if profile is None:
        return _local_failure(
            endpoint=endpoint,
            profile_ref=profile_ref,
            category="profile_vault",
            code="profile_unavailable",
            message="The saved profile could not be resolved.",
            recovery_action="Open the trader profile manager and create or repair the requested saved profile.",
        )
    try:
        partner_origin, partner_environment = resolve_partner_origin(
            str(getattr(profile, "spot_base_url", "") or ""),
            env,
        )
    except PartnerPolicyError:
        return _local_failure(
            endpoint=endpoint,
            profile_ref=profile_ref,
            category="local_policy",
            code="invalid_partner_origin",
            message=(
                "The saved Partner origin is not an allowed production or test origin."
            ),
            recovery_action="Review the saved spot base URL in the trader profile manager.",
        )
    if expected_environment is not None and expected_environment != partner_environment:
        return _local_failure(
            endpoint=endpoint,
            profile_ref=profile_ref,
            category="local_policy",
            code="expected_partner_environment_mismatch",
            message="Saved profile environment does not match the expected Partner environment.",
            recovery_action="Refresh trader preflight/profile metadata and submit a request for the resolved environment.",
            environment=partner_environment,
        )
    try:
        settings = LocalMcpPartnerSettings(
            profile_id=str(getattr(profile, "profile_id", "")),
            profile_name=str(getattr(profile, "name", profile_ref)),
            origin=partner_origin,
            environment=partner_environment,
            timeout_seconds=effective_timeout,
            locale=str(payload.get("locale") or DEFAULT_LOCALE),
        )
        if mcp_executor is None:
            result = McpPartnerFacade(settings=settings).execute(
                endpoint=endpoint,
                query=query,
                body=body,
            )
        else:
            result = mcp_executor(
                settings=settings,
                endpoint=endpoint,
                query=dict(query),
                body=dict(body),
            )
    except McpPartnerCompatibilityError as exc:
        return _local_failure(
            endpoint=endpoint,
            profile_ref=profile_ref,
            category="runtime_preflight",
            code=exc.code,
            message=exc.message,
            recovery_action="Repair the verified Local MCP Partner runtime and retry.",
            environment=partner_environment,
        )
    except Exception:
        return _local_failure(
            endpoint=endpoint,
            profile_ref=profile_ref,
            category="runtime_preflight",
            code="partner_mcp_executor_failed",
            message="The verified Local MCP Partner executor failed.",
            recovery_action="Repair the verified Local MCP Partner runtime and retry.",
            environment=partner_environment,
        )
    return sanitize_partner_result(
        result,
        origin=partner_origin,
        environment=partner_environment,
        secret_values=(),
    )


def _read_stdin_object() -> Dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise PartnerPolicyError(f"Invalid Partner request JSON on stdin: {exc}") from exc
    if not isinstance(payload, dict):
        raise PartnerPolicyError("Partner request JSON must be an object")
    return payload


def _output(payload: Mapping[str, Any], pretty: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Strict read-only WEEX Partner MCP compatibility executor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list-endpoints")
    list_parser.add_argument("--pretty", action="store_true")
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--profile", required=True)
    preflight_parser.add_argument("--language", choices=("zh", "en"), default="en")
    preflight_parser.add_argument("--pretty", action="store_true")
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "list-endpoints":
        rows = [
            {
                "key": endpoint.key,
                "method": endpoint.method,
                "path": endpoint.path,
                "operation_class": endpoint.operation_class,
                "weight": endpoint.weight,
            }
            for endpoint in ENDPOINTS.values()
        ]
        _output({"count": len(rows), "endpoints": rows}, args.pretty)
        return 0
    if args.command == "preflight":
        try:
            records = refresh_agent_records(
                preferred_language=args.language,
                command="partner.preflight",
                probe_default_profile_usable=False,
            )
            result = build_partner_preflight_envelope(
                records,
                profile_ref=args.profile,
            )
        except Exception:
            result = _partner_preflight_failure(
                profile_ref=args.profile,
                runtime={
                    "requirements_ready": False,
                    "missing_modules": [],
                    "env_validation": {"ok": False, "issue_count": 0},
                },
                vault={
                    "configured": False,
                    "state": "unknown",
                    "action_required": None,
                },
                category="runtime_preflight",
                code="partner_preflight_failed",
                message="Partner preflight failed before a safe profile summary could be produced.",
                recovery_action="Run the local trader doctor guidance, repair the runtime, and retry Partner preflight.",
            )
        _output(result, args.pretty)
        return 0 if result.get("ok") else 1
    try:
        result = execute_partner_request(_read_stdin_object())
    except PartnerPolicyError as exc:
        _output(
            {
                "ok": False,
                "error": {"category": "local_policy", "message": str(exc)},
                "api_domain": "partner",
                "environment": None,
                "capability_mode": "read_only_query",
            },
            args.pretty,
        )
        return 2
    except Exception:
        _output(
            {
                "ok": False,
                "error": {
                    "category": "internal_error",
                    "code": "partner_executor_failed",
                    "message": "The Partner executor failed before producing a safe response.",
                    "recovery_action": "Run the trader doctor/preflight command and retry after the reported issue is fixed.",
                },
                "api_domain": "partner",
                "environment": None,
                "capability_mode": "read_only_query",
            },
            args.pretty,
        )
        return 2
    _output(result, args.pretty)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
