#!/usr/bin/env python3
"""Strict read-only WEEX Partner REST executor."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence
from urllib import error, parse, request

from weex_agent_state import ensure_private_runtime_ready
from weex_url_policy import open_weex_request


PARTNER_BASE_URL = "https://api-spot.weex.com"
DEFAULT_TIMEOUT = 15.0
DEFAULT_LOCALE = "en-US"
PARTNER_OVERRIDE_ENV_VARS = (
    "WEEX_PARTNER_API_BASE",
    "WEEX_SPOT_API_BASE",
    "WEEX_API_BASE",
)
AUTHENTICATION_CODES = {
    "-1040",
    "-1041",
    "-1042",
    "-1043",
    "-1044",
    "-1046",
    "-1047",
    "-1049",
}
PERMISSION_CODES = {
    "-1050",
    "-1051",
    "-1052",
    "-1053",
    "-1055",
    "-1056",
    "-1057",
    "-1058",
    "-1190",
}
VALIDATION_CODES = {
    "-1045",
    "-1128",
    "-1135",
    "-1140",
    "-1141",
    "-1142",
    "-1150",
    "-1160",
    "-1170",
    "-1171",
}
UPSTREAM_CODES = {"-1000", "-1054"}
REDACTED_HEADER_NAMES = {
    "ACCESS-KEY",
    "ACCESS-PASSPHRASE",
    "ACCESS-SIGN",
}


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
    if len(endpoints) != 8:
        raise PartnerPolicyError("Partner endpoint allowlist must contain exactly eight entries")
    return endpoints


ENDPOINTS = load_endpoint_map()


def validate_partner_base_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    parsed = parse.urlsplit(value)
    if (
        value != PARTNER_BASE_URL
        or parsed.scheme != "https"
        or parsed.hostname != "api-spot.weex.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PartnerPolicyError(
            f"Partner credentials may only be sent to the exact origin {PARTNER_BASE_URL}"
        )
    return PARTNER_BASE_URL


def reject_partner_base_overrides(
    *,
    profile_spot_base_url: str = "",
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    env = os.environ if environ is None else environ
    configured = {
        name: str(env.get(name, "")).strip()
        for name in PARTNER_OVERRIDE_ENV_VARS
        if str(env.get(name, "")).strip()
    }
    profile_override = str(profile_spot_base_url or "").strip()
    if profile_override and profile_override != PARTNER_BASE_URL:
        raise PartnerPolicyError(
            "Partner requests do not accept a saved spot base URL override; "
            f"the only allowed origin is {PARTNER_BASE_URL}"
        )
    if configured:
        names = ", ".join(sorted(configured))
        raise PartnerPolicyError(
            f"Partner requests do not accept API base URL environment overrides ({names})"
        )


def encode_query(query: Mapping[str, Any]) -> str:
    return parse.urlencode(query, doseq=True)


def compact_json(value: Optional[Mapping[str, Any]]) -> str:
    if not value:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sign_request(
    *,
    secret: str,
    timestamp_ms: str,
    method: str,
    path: str,
    query_string: str,
    body_string: str,
) -> str:
    message = f"{timestamp_ms}{method.upper()}{path}"
    if query_string:
        message += f"?{query_string}"
    message += body_string
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


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


def prepare_signed_request(
    *,
    endpoint: Endpoint,
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    timestamp_ms: str,
    query: Optional[Mapping[str, Any]] = None,
    body: Optional[Mapping[str, Any]] = None,
    locale: str = DEFAULT_LOCALE,
) -> Dict[str, Any]:
    validate_partner_base_url(PARTNER_BASE_URL)
    query_payload = dict(query or {})
    body_payload = dict(body or {})
    _validate_endpoint_parameters(endpoint, query_payload, body_payload)
    if not api_key or not api_secret or not api_passphrase:
        raise PartnerPolicyError("Saved profile credentials are incomplete")

    query_string = encode_query(query_payload)
    body_string = compact_json(body_payload)
    signature = sign_request(
        secret=api_secret,
        timestamp_ms=timestamp_ms,
        method=endpoint.method,
        path=endpoint.path,
        query_string=query_string,
        body_string=body_string,
    )
    url = f"{PARTNER_BASE_URL}{endpoint.path}"
    if query_string:
        url += f"?{query_string}"
    return {
        "method": endpoint.method,
        "url": url,
        "headers": {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "locale": locale,
            "User-Agent": "weex-trader-skill-partner/1.0",
            "ACCESS-KEY": api_key,
            "ACCESS-PASSPHRASE": api_passphrase,
            "ACCESS-TIMESTAMP": timestamp_ms,
            "ACCESS-SIGN": signature,
        },
        "data": body_string.encode("utf-8") if endpoint.method == "POST" else None,
        "query": query_payload,
        "body": body_payload,
    }


def _replace_secret_values(value: str, secrets: Sequence[str]) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    return result


def sanitize_for_output(value: Any, secret_values: Iterable[str] = ()) -> Any:
    secrets = tuple(str(secret) for secret in secret_values if str(secret))
    if isinstance(value, Mapping):
        sanitized: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.upper() in REDACTED_HEADER_NAMES:
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


def _header_value(headers: Mapping[str, Any], name: str) -> Optional[str]:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return None


def _rate_limit_metadata(headers: Mapping[str, Any]) -> Dict[str, Dict[str, int]]:
    result: Dict[str, Dict[str, int]] = {"used": {}, "remaining": {}}
    for key, value in headers.items():
        upper = str(key).upper()
        bucket: Optional[str] = None
        group: Optional[str] = None
        if upper.startswith("X-USED-WEIGHT-"):
            group = "used"
            bucket = upper.removeprefix("X-USED-WEIGHT-")
        elif upper.startswith("X-REMAINING-WEIGHT-"):
            group = "remaining"
            bucket = upper.removeprefix("X-REMAINING-WEIGHT-")
        if group and bucket:
            try:
                result[group][bucket] = int(str(value))
            except ValueError:
                continue
    return result


def _business_code(payload: Any) -> Optional[str]:
    if not isinstance(payload, Mapping):
        return None
    for name in ("code", "errorCode"):
        if name in payload and payload[name] is not None:
            return str(payload[name])
    return None


def classify_error(status: Optional[int], payload: Any) -> str:
    code = _business_code(payload)
    if isinstance(payload, Mapping) and payload.get("raw_type") == "non_json":
        return "schema"
    if status == 429:
        return "rate_limit"
    if status == 401 or code in AUTHENTICATION_CODES:
        return "authentication"
    if status == 403 or code in PERMISSION_CODES:
        return "permission"
    if status == 400 or code in VALIDATION_CODES:
        return "validation"
    if status is not None and status >= 500 or code in UPSTREAM_CODES:
        return "upstream"
    if status is None:
        return "transport"
    return "upstream"


def _payload_is_business_error(payload: Any) -> bool:
    if isinstance(payload, Mapping) and payload.get("raw_type") == "non_json":
        return True
    code = _business_code(payload)
    return code not in {None, "0", "00000", "200"}


def normalize_http_result(
    *,
    status: Optional[int],
    headers: Optional[Mapping[str, Any]],
    payload: Any,
) -> Dict[str, Any]:
    response_headers = dict(headers or {})
    successful_http = status is not None and 200 <= status < 300
    ok = successful_http and not _payload_is_business_error(payload)
    result: Dict[str, Any] = {
        "ok": ok,
        "status": status,
        "rate_limit": _rate_limit_metadata(response_headers),
        "retry": {"automatic": False},
    }
    if ok:
        result["data"] = payload
    else:
        result["error"] = {
            "category": classify_error(status, payload),
            "http_status": status,
            "code": _business_code(payload),
            "details": payload,
        }
    return result


def _load_profile_dependencies():
    from weex_profile_store import load_profile_credentials, resolve_profile

    return load_profile_credentials, resolve_profile


def _default_preflight(**kwargs: Any) -> None:
    ensure_private_runtime_ready(
        command=str(kwargs.get("command", "partner.execute")),
        auto_setup=True,
        language=kwargs.get("language"),
    )


def _default_open(prepared: Mapping[str, Any], timeout: float):
    req = request.Request(
        url=str(prepared["url"]),
        method=str(prepared["method"]),
        data=prepared.get("data"),
        headers=dict(prepared["headers"]),
    )
    return open_weex_request(req, timeout=timeout, headers=prepared["headers"])


def _read_response(response: Any) -> tuple[int, Dict[str, Any], Any]:
    raw = response.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return int(response.status), dict(response.headers), {
            "code": None,
            "message": "Partner API returned non-JSON data",
            "raw_type": "non_json",
        }
    return int(response.status), dict(response.headers), payload


def execute_partner_request(
    payload: Mapping[str, Any],
    *,
    credential_loader: Optional[Callable[[str], Any]] = None,
    profile_resolver: Optional[Callable[[str], Any]] = None,
    preflight: Optional[Callable[..., None]] = None,
    opener: Optional[Callable[[Mapping[str, Any], float], Any]] = None,
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
    query = payload.get("query") or {}
    body = payload.get("body") or {}
    if not isinstance(query, Mapping) or not isinstance(body, Mapping):
        raise PartnerPolicyError("Partner query and body must be JSON objects")
    _validate_endpoint_parameters(endpoint, query, body)

    env = os.environ if environ is None else environ
    effective_timeout = timeout
    if effective_timeout is None:
        effective_timeout = float(env.get("WEEX_API_TIMEOUT", DEFAULT_TIMEOUT))
    (preflight or _default_preflight)(command="partner.execute", language=payload.get("language"))

    if credential_loader is None or profile_resolver is None:
        default_loader, default_resolver = _load_profile_dependencies()
        credential_loader = credential_loader or default_loader
        profile_resolver = profile_resolver or default_resolver
    profile = profile_resolver(profile_ref)
    if profile is None:
        raise PartnerPolicyError(f"Saved profile was not found: {profile_ref!r}")
    reject_partner_base_overrides(
        profile_spot_base_url=str(getattr(profile, "spot_base_url", "") or ""),
        environ=env,
    )
    credentials = credential_loader(str(getattr(profile, "name", profile_ref)))
    secret_values = (
        str(credentials.api_key),
        str(credentials.api_secret),
        str(credentials.api_passphrase),
    )
    prepared = prepare_signed_request(
        endpoint=endpoint,
        api_key=secret_values[0],
        api_secret=secret_values[1],
        api_passphrase=secret_values[2],
        timestamp_ms=str(int(time.time() * 1000)),
        query=query,
        body=body,
        locale=str(payload.get("locale") or DEFAULT_LOCALE),
    )

    try:
        response_or_context = (opener or _default_open)(prepared, effective_timeout)
        if hasattr(response_or_context, "__enter__"):
            with response_or_context as response:
                status, response_headers, response_payload = _read_response(response)
        else:
            status, response_headers, response_payload = _read_response(response_or_context)
        result = normalize_http_result(
            status=status,
            headers=response_headers,
            payload=response_payload,
        )
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            response_payload = json.loads(raw)
        except json.JSONDecodeError:
            response_payload = {"message": "Partner API returned non-JSON error data"}
        result = normalize_http_result(
            status=exc.code,
            headers=dict(exc.headers or {}),
            payload=response_payload,
        )
    except (error.URLError, TimeoutError, OSError) as exc:
        result = normalize_http_result(
            status=None,
            headers={},
            payload={"message": str(exc)},
        )

    result.update(
        {
            "endpoint": endpoint.key,
            "method": endpoint.method,
            "path": endpoint.path,
            "operation_class": endpoint.operation_class,
            "profile": {
                "resolved_profile_id": str(getattr(profile, "profile_id", "")),
                "name": str(getattr(profile, "name", profile_ref)),
            },
            "api_domain": "partner",
            "environment": "partner_production",
            "capability_mode": "read_only_query",
        }
    )
    return sanitize_for_output(result, secret_values)


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
    parser = argparse.ArgumentParser(description="Strict read-only WEEX Partner REST executor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list-endpoints")
    list_parser.add_argument("--pretty", action="store_true")
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
    try:
        result = execute_partner_request(_read_stdin_object())
    except PartnerPolicyError as exc:
        _output(
            {
                "ok": False,
                "error": {"category": "local_policy", "message": str(exc)},
            },
            args.pretty,
        )
        return 2
    _output(result, args.pretty)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
