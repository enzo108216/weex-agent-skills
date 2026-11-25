#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "weex_mcp_gateway.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_gateway_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("weex_mcp_gateway", MODULE_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def credential_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "type": {"const": "credential_ref"},
            "key_id": {"type": "string"},
            "ref": {"type": "string"},
        },
    }


def tool_catalog(*, trade_enabled: bool) -> list[dict[str, object]]:
    output_schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    tools: list[dict[str, object]] = [
        {
            "name": "market_get_symbol_price",
            "inputSchema": {
                "type": "object",
                "properties": {"market": {"type": "string"}, "symbol": {"type": "string"}},
            },
            "outputSchema": output_schema,
        },
        {
            "name": "account_get_balance",
            "inputSchema": {
                "type": "object",
                "properties": {"credential": credential_schema()},
            },
            "outputSchema": output_schema,
        },
        {
            "name": "partner_verify_referrals",
            "inputSchema": {
                "type": "object",
                "properties": {"credential": credential_schema()},
            },
            "outputSchema": output_schema,
        },
    ]
    if trade_enabled:
        tools.append(
            {
                "name": "trade_preview_order",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "credential": credential_schema(),
                        "delegated_authorization": {
                            "type": "object",
                            "properties": {"ref": {"type": "string"}},
                        },
                    },
                },
                "outputSchema": output_schema,
            }
        )
    return sorted(tools, key=lambda item: str(item["name"]))


def build_manifest(tools: list[dict[str, object]], *, trade_enabled: bool) -> dict[str, object]:
    names = sorted(str(tool["name"]) for tool in tools)
    schemas = sorted(
        (
            {
                "name": tool["name"],
                "inputSchema": tool["inputSchema"],
                "outputSchema": tool["outputSchema"],
            }
            for tool in tools
        ),
        key=lambda item: str(item["name"]),
    )
    manifest: dict[str, object] = {
        "manifest_version": 1,
        "package_version": "0.2.0",
        "tool_contract_version": "1",
        "runtime_profile": "local_user",
        "trade_enabled": trade_enabled,
        "tools": names,
        "credential_modes": ["credential_ref"],
        "delegated_authorization": trade_enabled,
        "modules": ["account", "market", "partner", *( ["trade"] if trade_enabled else [])],
        "tool_set_digest": canonical_digest(names),
        "schema_digest": canonical_digest(schemas),
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    return manifest


def mapping_for(manifests: dict[bool, dict[str, object]]) -> dict[str, object]:
    profiles: list[dict[str, object]] = []
    for runtime_profile in ("broker_server", "local_user"):
        for trade_enabled in (False, True):
            if runtime_profile == "local_user":
                manifest = manifests[trade_enabled]
                profiles.append(
                    {
                        "runtime_profile": runtime_profile,
                        "trade_enabled": trade_enabled,
                        "tool_count": len(manifest["tools"]),
                        "tool_set_digest": manifest["tool_set_digest"],
                        "schema_digest": manifest["schema_digest"],
                        "manifest_digest": manifest["manifest_digest"],
                    }
                )
            else:
                profiles.append(
                    {
                        "runtime_profile": runtime_profile,
                        "trade_enabled": trade_enabled,
                        "tool_count": 26 if not trade_enabled else 52,
                        "tool_set_digest": f"sha256:{'1' if not trade_enabled else '2'}" + "0" * 63,
                        "schema_digest": f"sha256:{'3' if not trade_enabled else '4'}" + "0" * 63,
                        "manifest_digest": f"sha256:{'5' if not trade_enabled else '6'}" + "0" * 63,
                    }
                )
    return {
        "schema_version": 1,
        "mapping_version": "trader-mcp-capability-v2",
        "verified_mcp_candidate": {
            "package_version": "0.2.0",
            "tool_contract_version": "1",
            "compatibility_profiles": profiles,
        },
    }


class FakeSecretProviderError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class FakeExecutionAuthorizationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FakeVerifiedExecutionAuthorization:
    authorization_id: str
    scope_fingerprint: str
    expires_at: int
    audit_metadata: dict[str, object]


@dataclass(frozen=True)
class FakeResolvedCredential:
    key_id: str
    api_key: str
    api_secret: str
    api_passphrase: str
    permissions: frozenset[str]
    fingerprint: str


@dataclass(frozen=True)
class FakeTenantContext:
    tenant_id: str
    user_id: str
    session_id: str
    request_id: str = "request"


class FakeSettings:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeService:
    def __init__(self, *, tools: list[dict[str, object]], manifest: dict[str, object], secret_provider):
        self.tools = copy.deepcopy(tools)
        self.manifest = copy.deepcopy(manifest)
        self.secret_provider = secret_provider
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.tool_handler = None

    def handle_jsonrpc(self, request, *, context=None):
        method = request.get("method")
        self.calls.append((str(method), copy.deepcopy(request)))
        if method == "server/discover":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "_meta": {
                        "com.weex/mcp": {"compatibility": copy.deepcopy(self.manifest)}
                    }
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"tools": copy.deepcopy(self.tools)},
            }
        if method == "tools/call":
            if self.tool_handler is not None:
                return self.tool_handler(request, context)
            payload = {"ok": True, "data": {"name": request["params"]["name"]}}
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "structuredContent": payload,
                    "content": [{"type": "text", "text": "ignored"}],
                    "isError": False,
                },
            }
        raise AssertionError(method)


def fake_runtime(service: FakeService, *, package_version: str = "0.2.0"):
    def build_local_user_service(**kwargs):
        service.secret_provider = kwargs["secret_provider"]
        service.settings = kwargs["settings"]
        service.execution_authorizer = kwargs.get("execution_authorizer")
        return service

    def describe_request(*, settings, name, arguments):
        trade_enabled = bool(getattr(settings, "trade_enabled", False))
        private = name.startswith("account_") or name.startswith("trade_preview_")
        return {
            "method": "POST" if name.startswith("trade_preview_") else "GET",
            "url": "https://api-spot.weex.com/description",
            "headers": {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "locale": "en-US",
                "User-Agent": "weex-mcp/0.1 trade",
                **(
                    {
                    "ACCESS-KEY": "***",
                    "ACCESS-PASSPHRASE": "***",
                    "ACCESS-TIMESTAMP": "<generated-at-send>",
                    "ACCESS-SIGN": "***",
                    }
                    if private
                    else {}
                ),
            },
            "query": {},
            "body": {},
            "_meta": {
                "com.weex/mcp": {
                    "compatibility": build_manifest(
                        tool_catalog(trade_enabled=trade_enabled),
                        trade_enabled=trade_enabled,
                    )
                }
            },
        }

    return SimpleNamespace(
        package_version=package_version,
        build_local_user_service=build_local_user_service,
        describe_request=describe_request,
        Settings=FakeSettings,
        TenantContext=FakeTenantContext,
        ResolvedWeexCredential=FakeResolvedCredential,
        SecretProviderError=FakeSecretProviderError,
        ExecutionAuthorizationError=FakeExecutionAuthorizationError,
        VerifiedExecutionAuthorization=FakeVerifiedExecutionAuthorization,
    )


class GatewayFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = load_gateway_module()
        self.assertIsNotNone(
            self.gateway,
            "weex_mcp_gateway.py is missing; expected T4B RED.",
        )
        self.credentials = SimpleNamespace(
            api_key="AK_ONE",
            api_secret="SK_ONE",
            api_passphrase="PP_ONE",
        )
        self.credential_loads: list[str] = []
        self.profile_loads: list[str] = []
        self.approval_key = "approval-key-" + "a" * 48
        self.approval_status = {
            "present": True,
            "valid": True,
            "length": len(self.approval_key),
            "fp_sha256": hashlib.sha256(self.approval_key.encode()).hexdigest()[:16],
        }

    def credential_loader(self, profile_id: str):
        self.credential_loads.append(profile_id)
        return self.credentials

    def profile_loader(self, profile_id: str):
        self.profile_loads.append(profile_id)
        return SimpleNamespace(profile_id=profile_id, name="renamed-profile")

    def make_gateway(
        self,
        *,
        trade_enabled: bool,
        profile_id: str | None = "profile-a",
        effective_spot_base_url: str = "https://api-spot.weex.com",
        effective_futures_base_url: str = "https://api-contract.weex.com",
        delegated_authorizer=None,
    ):
        tools = tool_catalog(trade_enabled=trade_enabled)
        manifest = build_manifest(tools, trade_enabled=trade_enabled)
        manifests = {
            False: build_manifest(tool_catalog(trade_enabled=False), trade_enabled=False),
            True: build_manifest(tool_catalog(trade_enabled=True), trade_enabled=True),
        }
        service = FakeService(tools=tools, manifest=manifest, secret_provider=None)
        gateway = self.gateway.LocalMcpGateway(
            profile_id=profile_id,
            effective_spot_base_url=effective_spot_base_url,
            effective_futures_base_url=effective_futures_base_url,
            trade_enabled=trade_enabled,
            timeout_seconds=15.0,
            runtime_loader=lambda _expected_version: fake_runtime(service),
            mapping_loader=lambda: mapping_for(manifests),
            profile_loader=self.profile_loader,
            credential_loader=self.credential_loader,
            approval_key_loader=lambda: self.approval_key,
            approval_key_status_loader=lambda: dict(self.approval_status),
            delegated_authorizer=delegated_authorizer,
        )
        return gateway, service


class VaultSecretProviderTests(GatewayFixture):
    def test_provider_accepts_legacy_non_ascii_profile_id(self) -> None:
        runtime = fake_runtime(FakeService(tools=[], manifest={}, secret_provider=None))
        loaded: list[str] = []
        credentials = SimpleNamespace(
            api_key="AK_ONE",
            api_secret="SK_ONE",
            api_passphrase="PP_ONE",
        )
        provider = self.gateway.VaultSecretProvider(
            profile_id="中文账户",
            bound_ref="ref-bound",
            expected_user_id="中文账户",
            fingerprint_key=b"f" * 32,
            credential_loader=lambda profile_id: loaded.append(profile_id) or credentials,
            runtime=runtime,
        )

        resolved = provider.resolve(
            credential_argument={
                "type": "credential_ref",
                "key_id": "中文账户",
                "ref": "ref-bound",
            },
            context=FakeTenantContext("local", "中文账户", "session"),
            required_permission="USER_DATA",
        )

        self.assertEqual(loaded, ["中文账户"])
        self.assertEqual(resolved.key_id, "中文账户")
        self.assertNotIn("中文账户", resolved.fingerprint)

    def test_provider_rejects_binding_context_and_permission_before_loading(self) -> None:
        runtime = fake_runtime(
            FakeService(
                tools=[],
                manifest={},
                secret_provider=None,
            )
        )
        provider = self.gateway.VaultSecretProvider(
            profile_id="profile-a",
            bound_ref="ref-bound",
            expected_user_id="profile-a",
            fingerprint_key=b"f" * 32,
            credential_loader=self.credential_loader,
            runtime=runtime,
        )
        context = FakeTenantContext("local", "profile-a", "session")
        valid = {"type": "credential_ref", "key_id": "profile-a", "ref": "ref-bound"}
        invalid = (
            ({**valid, "type": "plaintext"}, context, "USER_DATA"),
            ({**valid, "key_id": "profile-b"}, context, "USER_DATA"),
            ({**valid, "ref": "other-ref"}, context, "USER_DATA"),
            (valid, FakeTenantContext("local", "profile-b", "session"), "USER_DATA"),
            (valid, context, "ADMIN"),
        )
        for arguments, active_context, permission in invalid:
            with self.subTest(arguments=arguments, permission=permission):
                with self.assertRaises(FakeSecretProviderError):
                    provider.resolve(
                        credential_argument=arguments,
                        context=active_context,
                        required_permission=permission,
                    )
        self.assertEqual(self.credential_loads, [])

    def test_provider_loads_each_time_and_fingerprint_binds_full_secret_tuple(self) -> None:
        runtime = fake_runtime(FakeService(tools=[], manifest={}, secret_provider=None))
        provider = self.gateway.VaultSecretProvider(
            profile_id="profile-a",
            bound_ref="ref-bound",
            expected_user_id="profile-a",
            fingerprint_key=b"f" * 32,
            credential_loader=self.credential_loader,
            runtime=runtime,
        )
        arguments = {"type": "credential_ref", "key_id": "profile-a", "ref": "ref-bound"}
        context = FakeTenantContext("local", "profile-a", "session")

        first = provider.resolve(
            credential_argument=arguments,
            context=context,
            required_permission="USER_DATA",
        )
        self.credentials = SimpleNamespace(
            api_key="AK_ONE",
            api_secret="SK_TWO",
            api_passphrase="PP_ONE",
        )
        second = provider.resolve(
            credential_argument=arguments,
            context=context,
            required_permission="TRADE",
        )

        self.assertEqual(self.credential_loads, ["profile-a", "profile-a"])
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.permissions, frozenset({"USER_DATA", "TRADE"}))
        canonical = json.dumps(
            {
                "profile_id": "profile-a",
                "key_id": "profile-a",
                "api_key": "AK_ONE",
                "api_secret": "SK_ONE",
                "api_passphrase": "PP_ONE",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        expected = "fp_" + hmac.new(
            b"f" * 32,
            b"weex-trader-vault-credential-v1\0" + canonical,
            hashlib.sha256,
        ).hexdigest()[:32]
        self.assertEqual(first.fingerprint, expected)
        self.assertNotIn("profile-a", first.fingerprint)
        self.assertNotIn("SK_ONE", first.fingerprint)
        self.assertEqual(first.api_secret, "SK_ONE")

    def test_provider_maps_loader_failures_to_safe_existing_codes(self) -> None:
        runtime = fake_runtime(FakeService(tools=[], manifest={}, secret_provider=None))

        def broken_loader(_profile_id: str):
            raise RuntimeError("profile-a ref-secret AK_LEAK")

        provider = self.gateway.VaultSecretProvider(
            profile_id="profile-a",
            bound_ref="ref-bound",
            expected_user_id="profile-a",
            fingerprint_key=b"f" * 32,
            credential_loader=broken_loader,
            runtime=runtime,
        )
        with self.assertRaises(FakeSecretProviderError) as ctx:
            provider.resolve(
                credential_argument={
                    "type": "credential_ref",
                    "key_id": "profile-a",
                    "ref": "ref-bound",
                },
                context=FakeTenantContext("local", "profile-a", "session"),
                required_permission="USER_DATA",
            )
        self.assertIn(ctx.exception.code, {
            "secret_provider_unconfigured",
            "credential_invalid",
            "credential_ref_not_found",
            "permission_denied",
        })
        self.assertNotIn("profile-a", str(ctx.exception))
        self.assertNotIn("AK_LEAK", str(ctx.exception))


class LocalMcpGatewayTests(GatewayFixture):
    def test_request_description_uses_pure_runtime_without_service_or_private_state(self) -> None:
        manifests = {
            False: build_manifest(
                tool_catalog(trade_enabled=False),
                trade_enabled=False,
            ),
            True: build_manifest(
                tool_catalog(trade_enabled=True),
                trade_enabled=True,
            ),
        }
        runtime = fake_runtime(
            FakeService(
                tools=tool_catalog(trade_enabled=True),
                manifest=manifests[True],
                secret_provider=None,
            )
        )
        runtime.build_local_user_service = mock.Mock(
            side_effect=AssertionError("request description must not build a service")
        )

        with (
            mock.patch.object(
                self.gateway,
                "_default_profile_loader",
                side_effect=AssertionError("profile loader must not run"),
            ),
            mock.patch.object(
                self.gateway,
                "_default_credential_loader",
                side_effect=AssertionError("credential loader must not run"),
            ),
            mock.patch.object(
                self.gateway,
                "_default_approval_key_loader",
                side_effect=AssertionError("approval loader must not run"),
            ),
        ):
            description = self.gateway.describe_local_mcp_request(
                name="trade_preview_order",
                arguments={
                    "market": "spot",
                    "trading_mode": "live",
                    "symbol": "BTCUSDT",
                },
                effective_spot_base_url="https://api-spot.weex.com",
                effective_futures_base_url="https://api-contract.weex.com",
                trade_enabled=True,
                timeout_seconds=15.0,
                request_locale="zh-CN",
                runtime_loader=lambda _expected: runtime,
                mapping_loader=lambda: mapping_for(manifests),
            )

        self.assertEqual(description["method"], "POST")
        runtime.build_local_user_service.assert_not_called()
        self.assertEqual(self.profile_loads, [])
        self.assertEqual(self.credential_loads, [])

    def test_request_description_rejects_origin_and_auth_placeholder_drift(self) -> None:
        manifests = {
            False: build_manifest(
                tool_catalog(trade_enabled=False),
                trade_enabled=False,
            ),
            True: build_manifest(
                tool_catalog(trade_enabled=True),
                trade_enabled=True,
            ),
        }
        for mutation in ("origin", "signature", "duplicate_signature", "extra_header"):
            with self.subTest(mutation=mutation):
                runtime = fake_runtime(
                    FakeService(
                        tools=tool_catalog(trade_enabled=True),
                        manifest=manifests[True],
                        secret_provider=None,
                    )
                )
                original = runtime.describe_request

                def describe_request(**kwargs):
                    value = original(**kwargs)
                    if mutation == "origin":
                        value["url"] = "https://evil.example/description"
                    elif mutation == "signature":
                        value["headers"]["ACCESS-SIGN"] = "real-signature"
                    elif mutation == "duplicate_signature":
                        value["headers"]["access-sign"] = "real-signature"
                    else:
                        value["headers"]["X-Debug"] = "private-value"
                    return value

                runtime.describe_request = describe_request
                with self.assertRaises(self.gateway.McpGatewayError) as exc_info:
                    self.gateway.describe_local_mcp_request(
                        name="trade_preview_order",
                        arguments={"market": "spot"},
                        effective_spot_base_url="https://api-spot.weex.com",
                        effective_futures_base_url="https://api-contract.weex.com",
                        trade_enabled=True,
                        timeout_seconds=15.0,
                        runtime_loader=lambda _expected: runtime,
                        mapping_loader=lambda: mapping_for(manifests),
                    )
                self.assertEqual(exc_info.exception.code, "mcp_contract_mismatch")

    def test_gateway_ref_is_instance_unique_but_approval_and_credential_fingerprint_are_stable(self) -> None:
        first, first_service = self.make_gateway(trade_enabled=True)
        second, second_service = self.make_gateway(trade_enabled=True)
        captured: dict[str, dict[str, object]] = {}

        def handler(label):
            def capture(request, context):
                captured[label] = copy.deepcopy(request["params"]["arguments"]["credential"])
                payload = {"ok": True, "data": {}}
                return {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"structuredContent": payload, "isError": False},
                }

            return capture

        first_service.tool_handler = handler("first")
        second_service.tool_handler = handler("second")
        first.call_typed_tool(
            "account_get_balance",
            {"market": "spot", "trading_mode": "live"},
        )
        second.call_typed_tool(
            "account_get_balance",
            {"market": "spot", "trading_mode": "live"},
        )

        self.assertNotEqual(captured["first"]["ref"], captured["second"]["ref"])
        self.assertEqual(
            first_service.settings.approval_token_secret,
            second_service.settings.approval_token_secret,
        )
        first_resolved = first_service.secret_provider.resolve(
            credential_argument=captured["first"],
            context=first._context(),
            required_permission="USER_DATA",
        )
        second_resolved = second_service.secret_provider.resolve(
            credential_argument=captured["second"],
            context=second._context(),
            required_permission="USER_DATA",
        )
        self.assertEqual(first_resolved.fingerprint, second_resolved.fingerprint)

        loads_before = len(self.credential_loads)
        with self.assertRaises(FakeSecretProviderError):
            second_service.secret_provider.resolve(
                credential_argument=captured["first"],
                context=second._context(),
                required_permission="USER_DATA",
            )
        self.assertEqual(len(self.credential_loads), loads_before)

    def test_module_import_does_not_require_installed_mcp(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree_prefix = source.split("def _load_default_runtime", 1)[0]
        self.assertNotIn("from weex_mcp", tree_prefix)
        self.assertNotIn("import weex_mcp", tree_prefix)

    def test_runtime_status_is_nonsecret_and_rechecks_the_manifest(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False, profile_id=None)

        ready = gateway.runtime_status()

        self.assertEqual(
            set(ready),
            {
                "installed",
                "version",
                "expected_version",
                "package_ok",
                "manifest_ok",
                "runtime_profile",
                "trade_enabled",
                "tool_contract_version",
                "manifest_digest",
                "action_required",
            },
        )
        self.assertEqual(ready["version"], "0.2.0")
        self.assertEqual(ready["expected_version"], "0.2.0")
        self.assertTrue(ready["installed"])
        self.assertTrue(ready["package_ok"])
        self.assertTrue(ready["manifest_ok"])
        self.assertEqual(ready["runtime_profile"], "local_user")
        self.assertFalse(ready["trade_enabled"])
        self.assertEqual(ready["tool_contract_version"], "1")
        self.assertIsNone(ready["action_required"])
        serialized = json.dumps(ready, sort_keys=True)
        for forbidden in (
            "profile-a",
            "api-spot.weex.com",
            "api-contract.weex.com",
            "partner-ref",
            "approval-key",
            "/Users/",
        ):
            self.assertNotIn(forbidden, serialized)

        service.manifest["manifest_digest"] = f"sha256:{'f' * 64}"
        stale = gateway.runtime_status()
        self.assertTrue(stale["package_ok"])
        self.assertFalse(stale["manifest_ok"])
        self.assertEqual(stale["action_required"], "repair_mcp_contract")

    def test_probe_local_mcp_runtime_reports_safe_success_and_failure_states(self) -> None:
        manifests = {
            False: build_manifest(tool_catalog(trade_enabled=False), trade_enabled=False),
            True: build_manifest(tool_catalog(trade_enabled=True), trade_enabled=True),
        }
        mapping = mapping_for(manifests)
        tools = tool_catalog(trade_enabled=False)
        service = FakeService(tools=tools, manifest=manifests[False], secret_provider=None)

        ready = self.gateway.probe_local_mcp_runtime(
            runtime_loader=lambda _expected: fake_runtime(service),
            mapping_loader=lambda: copy.deepcopy(mapping),
        )
        self.assertTrue(ready["installed"])
        self.assertTrue(ready["package_ok"])
        self.assertTrue(ready["manifest_ok"])
        self.assertIsNone(ready["action_required"])

        failures = (
            (
                lambda _expected: (_ for _ in ()).throw(
                    self.gateway.McpGatewayError("mcp_package_missing", "AK_MISSING /private/path")
                ),
                "install_verified_mcp_candidate",
                False,
            ),
            (
                lambda _expected: fake_runtime(service, package_version="9.9.9"),
                "install_verified_mcp_candidate",
                True,
            ),
            (
                lambda _expected: (_ for _ in ()).throw(RuntimeError("AK_RUNTIME /private/path")),
                "repair_mcp_runtime",
                False,
            ),
        )
        for runtime_loader, action_required, installed in failures:
            with self.subTest(action_required=action_required):
                status = self.gateway.probe_local_mcp_runtime(
                    runtime_loader=runtime_loader,
                    mapping_loader=lambda: copy.deepcopy(mapping),
                )
                self.assertEqual(status["installed"], installed)
                self.assertFalse(status["package_ok"])
                self.assertFalse(status["manifest_ok"])
                self.assertEqual(status["action_required"], action_required)
                serialized = json.dumps(status, sort_keys=True)
                self.assertNotIn("AK_", serialized)
                self.assertNotIn("/private/path", serialized)

        tampered_service = FakeService(
            tools=tools,
            manifest=manifests[False],
            secret_provider=None,
        )
        tampered_service.manifest["manifest_digest"] = f"sha256:{'e' * 64}"
        contract = self.gateway.probe_local_mcp_runtime(
            runtime_loader=lambda _expected: fake_runtime(tampered_service),
            mapping_loader=lambda: copy.deepcopy(mapping),
        )
        self.assertTrue(contract["installed"])
        self.assertTrue(contract["package_ok"])
        self.assertFalse(contract["manifest_ok"])
        self.assertEqual(contract["action_required"], "repair_mcp_contract")

    def test_probe_local_mcp_runtime_sanitizes_malformed_mapping_and_runtime_versions(self) -> None:
        manifests = {
            False: build_manifest(tool_catalog(trade_enabled=False), trade_enabled=False),
            True: build_manifest(tool_catalog(trade_enabled=True), trade_enabled=True),
        }
        mapping = mapping_for(manifests)
        service = FakeService(
            tools=tool_catalog(trade_enabled=False),
            manifest=manifests[False],
            secret_provider=None,
        )
        malformed_mapping_values = (
            ("package_version", "AK_MAPPING /private/candidate.whl"),
            ("tool_contract_version", "profile-a"),
        )
        for field, unsafe_value in malformed_mapping_values:
            with self.subTest(field=field):
                bad_mapping = copy.deepcopy(mapping)
                bad_mapping["verified_mcp_candidate"][field] = unsafe_value
                status = self.gateway.probe_local_mcp_runtime(
                    runtime_loader=lambda _expected: fake_runtime(service),
                    mapping_loader=lambda: bad_mapping,
                )
                self.assertEqual(status["action_required"], "repair_mcp_mapping")
                serialized = json.dumps(status, sort_keys=True)
                self.assertNotIn(unsafe_value, serialized)
                self.assertNotIn("/private/", serialized)

        bad_digest_mapping = copy.deepcopy(mapping)
        bad_digest_mapping["verified_mcp_candidate"]["compatibility_profiles"][2][
            "manifest_digest"
        ] = "AK_DIGEST /private/manifest.json"
        digest_status = self.gateway.probe_local_mcp_runtime(
            runtime_loader=lambda _expected: fake_runtime(service),
            mapping_loader=lambda: bad_digest_mapping,
        )
        self.assertEqual(digest_status["action_required"], "repair_mcp_mapping")
        self.assertNotIn("AK_DIGEST", json.dumps(digest_status, sort_keys=True))

        runtime_status = self.gateway.probe_local_mcp_runtime(
            runtime_loader=lambda _expected: fake_runtime(
                service,
                package_version="AK_RUNTIME /private/runtime.py",
            ),
            mapping_loader=lambda: copy.deepcopy(mapping),
        )
        self.assertEqual(
            runtime_status["action_required"],
            "install_verified_mcp_candidate",
        )
        self.assertIsNone(runtime_status["version"])
        serialized_runtime = json.dumps(runtime_status, sort_keys=True)
        self.assertNotIn("AK_RUNTIME", serialized_runtime)
        self.assertNotIn("/private/", serialized_runtime)

    def test_gateway_selects_each_local_manifest_and_derives_trade_secrets_only_for_trade(self) -> None:
        approval_loads = []
        tools = tool_catalog(trade_enabled=False)
        readonly_manifest = build_manifest(tools, trade_enabled=False)
        manifests = {
            False: readonly_manifest,
            True: build_manifest(tool_catalog(trade_enabled=True), trade_enabled=True),
        }
        readonly_fake = FakeService(tools=tools, manifest=readonly_manifest, secret_provider=None)
        readonly = self.gateway.LocalMcpGateway(
            profile_id="profile-a",
            effective_spot_base_url="https://api-spot.weex.com",
            effective_futures_base_url="https://api-contract.weex.com",
            trade_enabled=False,
            timeout_seconds=15.0,
            runtime_loader=lambda _expected: fake_runtime(readonly_fake),
            mapping_loader=lambda: mapping_for(manifests),
            profile_loader=self.profile_loader,
            credential_loader=self.credential_loader,
            approval_key_loader=lambda: approval_loads.append("load") or self.approval_key,
            approval_key_status_loader=lambda: approval_loads.append("status") or dict(self.approval_status),
        )
        readonly_service = readonly_fake
        trading, trading_service = self.make_gateway(trade_enabled=True)

        self.assertFalse(readonly.trade_enabled)
        self.assertEqual(readonly_service.settings.approval_token_secret, "")
        self.assertEqual(approval_loads, [])
        self.assertTrue(trading.trade_enabled)
        self.assertNotEqual(trading_service.settings.approval_token_secret, self.approval_key)
        self.assertGreaterEqual(len(trading_service.settings.approval_token_secret), 32)
        self.assertEqual(self.profile_loads, ["profile-a", "profile-a"])

        expected_secret = hmac.new(
            self.approval_key.encode(),
            b"weex-trader-local-mcp-approval-v1\0"
            + str(manifests[True]["manifest_digest"]).encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(trading_service.settings.approval_token_secret, expected_secret)

    def test_gateway_passes_caller_resolved_effective_origins_exactly_to_settings(self) -> None:
        gateway, service = self.make_gateway(
            trade_enabled=False,
            effective_spot_base_url="https://effective-spot.weex.tech",
            effective_futures_base_url="https://effective-futures.weex.tech",
        )

        self.assertEqual(
            gateway.effective_spot_base_url,
            "https://effective-spot.weex.tech",
        )
        self.assertEqual(
            gateway.effective_futures_base_url,
            "https://effective-futures.weex.tech",
        )
        self.assertEqual(
            service.settings.spot_base_url,
            "https://effective-spot.weex.tech",
        )
        self.assertEqual(
            service.settings.futures_base_url,
            "https://effective-futures.weex.tech",
        )

    def test_read_gateway_preserves_locale_and_disables_market_retries(self) -> None:
        tools = tool_catalog(trade_enabled=False)
        manifest = build_manifest(tools, trade_enabled=False)
        manifests = {
            False: manifest,
            True: build_manifest(tool_catalog(trade_enabled=True), trade_enabled=True),
        }
        service = FakeService(tools=tools, manifest=manifest, secret_provider=None)

        self.gateway.LocalMcpGateway(
            profile_id="profile-a",
            effective_spot_base_url="https://api-spot.weex.com",
            effective_futures_base_url="https://api-contract.weex.com",
            trade_enabled=False,
            timeout_seconds=7.5,
            request_locale="zh-CN",
            runtime_loader=lambda _expected: fake_runtime(service),
            mapping_loader=lambda: mapping_for(manifests),
            profile_loader=self.profile_loader,
            credential_loader=self.credential_loader,
        )

        self.assertEqual(service.settings.request_locale, "zh-CN")
        self.assertEqual(service.settings.market_timeout_seconds, 7.5)
        self.assertEqual(service.settings.account_timeout_seconds, 7.5)
        self.assertEqual(service.settings.partner_timeout_seconds, 7.5)
        self.assertEqual(service.settings.market_max_attempts, 1)
        self.assertEqual(service.settings.market_retry_base_delay_seconds, 0.0)

    def test_public_gateway_needs_no_profile_or_vault_but_private_tool_requires_profile(self) -> None:
        public_gateway, service = self.make_gateway(trade_enabled=False, profile_id=None)
        public = public_gateway.call_typed_tool(
            "market_get_symbol_price",
            {"market": "spot", "symbol": "BTCUSDT"},
        )
        self.assertTrue(public["ok"])
        self.assertEqual(self.profile_loads, [])
        self.assertEqual(self.credential_loads, [])

        with self.assertRaises(self.gateway.McpGatewayError) as ctx:
            public_gateway.call_typed_tool(
                "account_get_balance",
                {"market": "spot", "trading_mode": "live"},
            )
        self.assertEqual(ctx.exception.code, "profile_required")
        self.assertFalse(any(call[0] == "tools/call" for call in service.calls[5:]))

    def test_private_call_injects_bound_credential_and_rejects_caller_override(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False)
        captured: dict[str, object] = {}

        def handler(request, context):
            captured["arguments"] = copy.deepcopy(request["params"]["arguments"])
            captured["context"] = context
            payload = {"ok": True, "data": {"balances": []}}
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"structuredContent": payload, "isError": False},
            }

        service.tool_handler = handler
        result = gateway.call_typed_tool(
            "account_get_balance",
            {"market": "spot", "trading_mode": "live"},
        )
        self.assertTrue(result["ok"])
        credential = captured["arguments"]["credential"]
        self.assertEqual(credential["type"], "credential_ref")
        self.assertEqual(credential["key_id"], "profile-a")
        self.assertNotEqual(credential["ref"], "profile-a")
        self.assertEqual(captured["context"].user_id, "profile-a")

        for forbidden in ("credential", "endpoint", "path", "method"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(self.gateway.McpGatewayError):
                    gateway.call_typed_tool(
                        "account_get_balance",
                        {"market": "spot", forbidden: "override"},
                    )

    def test_every_call_rechecks_discovery_and_tools_before_business_call(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False)
        initial_discover = sum(method == "server/discover" for method, _ in service.calls)
        initial_list = sum(method == "tools/list" for method, _ in service.calls)

        gateway.call_typed_tool(
            "market_get_symbol_price",
            {"market": "spot", "symbol": "BTCUSDT"},
        )
        gateway.call_typed_tool(
            "market_get_symbol_price",
            {"market": "spot", "symbol": "ETHUSDT"},
        )

        self.assertEqual(
            sum(method == "server/discover" for method, _ in service.calls),
            initial_discover + 2,
        )
        self.assertEqual(
            sum(method == "tools/list" for method, _ in service.calls),
            initial_list + 2,
        )

    def test_trade_call_rejects_rotated_missing_or_locked_approval_key_before_tool(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=True)
        base_tool_calls = sum(method == "tools/call" for method, _ in service.calls)
        original = dict(self.approval_status)
        invalid_statuses = (
            ({**original, "fp_sha256": "0" * 16}, "approval_key_changed"),
            ({"present": False, "valid": False, "length": 0, "fp_sha256": None}, "approval_key_required"),
        )
        for status, expected_code in invalid_statuses:
            with self.subTest(expected_code=expected_code):
                self.approval_status = status
                with self.assertRaises(self.gateway.McpGatewayError) as ctx:
                    gateway.call_typed_tool(
                        "trade_preview_order",
                        {"market": "spot", "trading_mode": "live"},
                    )
                self.assertEqual(ctx.exception.code, expected_code)
        self.assertEqual(
            sum(method == "tools/call" for method, _ in service.calls),
            base_tool_calls,
        )

        self.approval_status = dict(original)
        gateway._approval_key_status_loader = lambda: (_ for _ in ()).throw(
            RuntimeError("vault locked profile-a")
        )
        with self.assertRaises(self.gateway.McpGatewayError) as locked:
            gateway.call_typed_tool(
                "trade_preview_order",
                {"market": "spot", "trading_mode": "live"},
            )
        self.assertEqual(locked.exception.code, "approval_key_required")
        self.assertNotIn("profile-a", str(locked.exception))

    def test_trade_gateway_bridges_neutral_delegated_authorizer_callback(self) -> None:
        captured: dict[str, object] = {}

        def callback(payload: dict[str, object]) -> dict[str, object]:
            captured.update(copy.deepcopy(payload))
            return {
                "authorization_id": "auth-1",
                "scope_fingerprint": "scope-fingerprint",
                "expires_at": 4102444800,
                "audit_metadata": {"usage_count": 2},
            }

        gateway, service = self.make_gateway(
            trade_enabled=True,
            delegated_authorizer=callback,
        )
        execution = SimpleNamespace(
            action="batch_order",
            market="spot",
            trading_mode="live",
            summary={"orders": 2},
            risk={"level": "bounded"},
            risk_signature="risk-signature",
        )

        result = service.execution_authorizer.authorize(
            context=gateway._context(),
            credential_fingerprint="fp_credential",
            execution=execution,
            delegated_ref={"ref": "opaque-ref"},
        )

        self.assertIsInstance(result, FakeVerifiedExecutionAuthorization)
        self.assertEqual(result.authorization_id, "auth-1")
        self.assertEqual(result.scope_fingerprint, "scope-fingerprint")
        self.assertEqual(result.audit_metadata, {"usage_count": 2})
        self.assertEqual(
            captured,
            {
                "context": {
                    "tenant_id": "weex-trader-local",
                    "user_id": "profile-a",
                    "session_id": gateway._context().session_id,
                    "request_id": "request",
                },
                "credential_fingerprint": "fp_credential",
                "execution": {
                    "action": "batch_order",
                    "market": "spot",
                    "trading_mode": "live",
                    "summary": {"orders": 2},
                    "risk": {"level": "bounded"},
                    "risk_signature": "risk-signature",
                },
                "delegated_ref": {"ref": "opaque-ref"},
            },
        )

    def test_delegated_authorizer_failures_use_stable_mcp_error_codes(self) -> None:
        class CallbackError(RuntimeError):
            def __init__(self, code: str):
                super().__init__("unsafe callback detail AK_LEAK")
                self.code = code
                self.message = "unsafe callback detail AK_LEAK"

        cases = (
            ("execution_authorization_invalid", "execution_authorization_invalid"),
            ("execution_authorization_expired", "execution_authorization_expired"),
            ("unsupported_code", "execution_authorization_denied"),
        )
        for callback_code, expected_code in cases:
            with self.subTest(callback_code=callback_code):
                gateway, service = self.make_gateway(
                    trade_enabled=True,
                    delegated_authorizer=lambda _payload, code=callback_code: (_ for _ in ()).throw(
                        CallbackError(code)
                    ),
                )
                with self.assertRaises(FakeExecutionAuthorizationError) as raised:
                    service.execution_authorizer.authorize(
                        context=gateway._context(),
                        credential_fingerprint="fp",
                        execution=SimpleNamespace(
                            action="order",
                            market="spot",
                            trading_mode="live",
                            summary={},
                            risk={},
                            risk_signature="sig",
                        ),
                        delegated_ref={"ref": "opaque"},
                    )
                self.assertEqual(raised.exception.code, expected_code)
                self.assertNotIn("AK_LEAK", str(raised.exception))

        gateway, service = self.make_gateway(
            trade_enabled=True,
            delegated_authorizer=lambda _payload: {"authorization_id": "missing-fields"},
        )
        with self.assertRaises(FakeExecutionAuthorizationError) as invalid:
            service.execution_authorizer.authorize(
                context=gateway._context(),
                credential_fingerprint="fp",
                execution=SimpleNamespace(
                    action="order",
                    market="spot",
                    trading_mode="live",
                    summary={},
                    risk={},
                    risk_signature="sig",
                ),
                delegated_ref={"ref": "opaque"},
            )
        self.assertEqual(invalid.exception.code, "execution_authorization_invalid")

    def test_manual_gateway_keeps_default_fail_closed_authorizer_wiring(self) -> None:
        _gateway, service = self.make_gateway(trade_enabled=True)
        self.assertIsNone(service.execution_authorizer)

    def test_credential_rotation_is_loaded_fresh_and_mcp_error_is_preserved(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=True)
        fingerprints: list[str] = []

        def handler(request, context):
            resolved = service.secret_provider.resolve(
                credential_argument=request["params"]["arguments"]["credential"],
                context=context,
                required_permission="TRADE",
            )
            fingerprints.append(resolved.fingerprint)
            payload = (
                {"ok": True, "data": {"preview_id": "first"}}
                if len(fingerprints) == 1
                else {"ok": False, "error": {"code": "approval_mismatch", "message": "safe"}}
            )
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"structuredContent": payload, "isError": not payload["ok"]},
            }

        service.tool_handler = handler
        first = gateway.call_typed_tool(
            "trade_preview_order",
            {"market": "spot", "trading_mode": "live"},
        )
        self.assertTrue(first["ok"])
        self.credentials = SimpleNamespace(
            api_key="AK_ONE",
            api_secret="SK_ROTATED",
            api_passphrase="PP_ONE",
        )
        second = gateway.call_typed_tool(
            "trade_preview_order",
            {"market": "spot", "trading_mode": "live"},
        )
        self.assertEqual(second["error"]["code"], "approval_mismatch")
        self.assertEqual(len(fingerprints), 2)
        self.assertNotEqual(fingerprints[0], fingerprints[1])

    def test_strict_envelope_preserves_structured_fields_and_rejects_protocol_errors(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False)
        payload = {
            "ok": False,
            "error": {"code": "upstream_error", "message": "safe"},
            "audit": {"request_id": "request"},
            "outcome": {"class": "unknown", "write_attempted": True, "action": "x"},
        }
        service.tool_handler = lambda request, context: {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "structuredContent": copy.deepcopy(payload),
                "content": [{"type": "text", "text": "not parsed"}],
                "isError": True,
            },
        }
        self.assertEqual(
            gateway.call_typed_tool(
                "market_get_symbol_price",
                {"market": "spot", "symbol": "BTCUSDT"},
            ),
            payload,
        )

        invalid_responses = (
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603}},
            {"jsonrpc": "2.0", "id": 1, "result": []},
            {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": []}},
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"structuredContent": {"ok": True}, "isError": True},
            },
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                service.tool_handler = lambda request, context, response=response: response
                with self.assertRaises(self.gateway.McpGatewayError):
                    gateway.call_typed_tool(
                        "market_get_symbol_price",
                        {"market": "spot", "symbol": "BTCUSDT"},
                    )

    def test_jsonrpc_version_and_id_are_strict_for_discover_list_and_call(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False)
        original_handle = service.handle_jsonrpc
        mutations = (
            ("server/discover", "missing_version"),
            ("server/discover", "wrong_version"),
            ("tools/list", "missing_id"),
            ("tools/list", "wrong_id"),
            ("tools/call", "missing_version"),
            ("tools/call", "wrong_version"),
            ("tools/call", "missing_id"),
            ("tools/call", "wrong_id"),
        )

        for target_method, mutation in mutations:
            with self.subTest(target_method=target_method, mutation=mutation):
                def mutate(request, *, context=None):
                    response = original_handle(request, context=context)
                    if request.get("method") != target_method:
                        return response
                    response = copy.deepcopy(response)
                    if mutation == "missing_version":
                        response.pop("jsonrpc", None)
                    elif mutation == "wrong_version":
                        response["jsonrpc"] = "1.0"
                    elif mutation == "missing_id":
                        response.pop("id", None)
                    else:
                        response["id"] = "wrong-id"
                    return response

                service.handle_jsonrpc = mutate
                with self.assertRaises(self.gateway.McpGatewayError) as ctx:
                    gateway.call_typed_tool(
                        "market_get_symbol_price",
                        {"market": "spot", "symbol": "BTCUSDT"},
                    )
                self.assertEqual(ctx.exception.code, "mcp_protocol_error")
                service.handle_jsonrpc = original_handle

    def test_package_mapping_manifest_and_tool_tampering_fail_before_business_call(self) -> None:
        mutations = (
            "package_version",
            "mapping_version",
            "missing_local_profile",
            "manifest_digest",
            "tool_schema",
            "duplicate_tool",
            "profile_label",
            "trade_flag",
            "unhashable_profile",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                tools = tool_catalog(trade_enabled=False)
                manifest = build_manifest(tools, trade_enabled=False)
                manifests = {
                    False: build_manifest(tool_catalog(trade_enabled=False), trade_enabled=False),
                    True: build_manifest(tool_catalog(trade_enabled=True), trade_enabled=True),
                }
                mapping = mapping_for(manifests)
                runtime = fake_runtime(
                    FakeService(tools=tools, manifest=manifest, secret_provider=None)
                )
                service = runtime.build_local_user_service(
                    settings=FakeSettings(),
                    secret_provider=None,
                )
                if mutation == "package_version":
                    runtime.package_version = "9.9.9"
                elif mutation == "mapping_version":
                    mapping["mapping_version"] = "trader-mcp-capability-v1"
                elif mutation == "missing_local_profile":
                    mapping["verified_mcp_candidate"]["compatibility_profiles"] = [
                        item
                        for item in mapping["verified_mcp_candidate"]["compatibility_profiles"]
                        if item["runtime_profile"] != "local_user"
                    ]
                elif mutation == "manifest_digest":
                    service.manifest["manifest_digest"] = f"sha256:{'f' * 64}"
                elif mutation == "tool_schema":
                    service.tools[0]["inputSchema"]["properties"]["tampered"] = {"type": "string"}
                elif mutation == "duplicate_tool":
                    service.tools.append(copy.deepcopy(service.tools[0]))
                elif mutation == "profile_label":
                    service.manifest["runtime_profile"] = "broker_server"
                    payload = dict(service.manifest)
                    payload.pop("manifest_digest")
                    service.manifest["manifest_digest"] = canonical_digest(payload)
                elif mutation == "trade_flag":
                    service.manifest["trade_enabled"] = True
                    payload = dict(service.manifest)
                    payload.pop("manifest_digest")
                    service.manifest["manifest_digest"] = canonical_digest(payload)
                elif mutation == "unhashable_profile":
                    mapping["verified_mcp_candidate"]["compatibility_profiles"][0][
                        "runtime_profile"
                    ] = {"bad": []}

                with self.assertRaises(self.gateway.McpGatewayError):
                    self.gateway.LocalMcpGateway(
                        profile_id="profile-a",
                        effective_spot_base_url="https://api-spot.weex.com",
                        effective_futures_base_url="https://api-contract.weex.com",
                        trade_enabled=False,
                        timeout_seconds=15.0,
                        runtime_loader=lambda _expected: runtime,
                        mapping_loader=lambda mapping=mapping: mapping,
                        profile_loader=self.profile_loader,
                        credential_loader=self.credential_loader,
                        approval_key_loader=lambda: self.approval_key,
                        approval_key_status_loader=lambda: dict(self.approval_status),
                    )
                self.assertFalse(any(method == "tools/call" for method, _ in service.calls))

    def test_default_runtime_loader_reports_missing_package_version_and_public_api(self) -> None:
        with mock.patch.object(
            self.gateway.metadata,
            "version",
            side_effect=self.gateway.metadata.PackageNotFoundError,
        ):
            with self.assertRaises(self.gateway.McpGatewayError) as missing:
                self.gateway._load_default_runtime("0.2.0")
        self.assertEqual(missing.exception.code, "mcp_package_missing")

        with mock.patch.object(self.gateway.metadata, "version", return_value="0.1.0"):
            with self.assertRaises(self.gateway.McpGatewayError) as mismatch:
                self.gateway._load_default_runtime("0.2.0")
        self.assertEqual(mismatch.exception.code, "mcp_version_mismatch")

        modules = {
            "weex_mcp.local": SimpleNamespace(),
            "weex_mcp.settings": SimpleNamespace(Settings=FakeSettings),
            "weex_mcp.tenant_context": SimpleNamespace(TenantContext=FakeTenantContext),
            "weex_mcp.secret_provider": SimpleNamespace(
                ResolvedWeexCredential=FakeResolvedCredential,
                SecretProviderError=FakeSecretProviderError,
            ),
        }
        with mock.patch.object(self.gateway.metadata, "version", return_value="0.2.0"):
            with mock.patch.object(
                self.gateway.importlib,
                "import_module",
                side_effect=lambda name: modules[name],
            ):
                with self.assertRaises(self.gateway.McpGatewayError) as incompatible:
                    self.gateway._load_default_runtime("0.2.0")
        self.assertEqual(incompatible.exception.code, "mcp_runtime_incompatible")

        with mock.patch.object(self.gateway.metadata, "version", return_value="0.2.0"):
            with mock.patch.object(
                self.gateway.importlib,
                "import_module",
                side_effect=RuntimeError("AK_LEAK /private/checkout"),
            ):
                with self.assertRaises(self.gateway.McpGatewayError) as broken:
                    self.gateway._load_default_runtime("0.2.0")
        self.assertEqual(broken.exception.code, "mcp_runtime_incompatible")
        self.assertNotIn("AK_LEAK", str(broken.exception))

    def test_mapping_and_service_exceptions_are_projected_as_stable_gateway_errors(self) -> None:
        with self.assertRaises(self.gateway.McpGatewayError) as mapping_error:
            self.gateway.LocalMcpGateway(
                profile_id=None,
                effective_spot_base_url="https://api-spot.weex.com",
                effective_futures_base_url="https://api-contract.weex.com",
                trade_enabled=False,
                timeout_seconds=15.0,
                runtime_loader=lambda _expected: None,
                mapping_loader=lambda: (_ for _ in ()).throw(RuntimeError("AK_MAPPING")),
            )
        self.assertEqual(mapping_error.exception.code, "mcp_mapping_invalid")
        self.assertNotIn("AK_MAPPING", str(mapping_error.exception))

        gateway, service = self.make_gateway(trade_enabled=False)
        original_handle = service.handle_jsonrpc

        def broken_handle(request, *, context=None):
            if request.get("method") == "server/discover":
                raise RuntimeError("AK_SERVICE private-origin")
            return original_handle(request, context=context)

        service.handle_jsonrpc = broken_handle
        with self.assertRaises(self.gateway.McpGatewayError) as service_error:
            gateway.call_typed_tool(
                "market_get_symbol_price",
                {"market": "spot", "symbol": "BTCUSDT"},
            )
        self.assertEqual(service_error.exception.code, "mcp_protocol_error")
        self.assertNotIn("AK_SERVICE", str(service_error.exception))

    def test_source_has_no_fallback_private_mcp_helper_or_editable_path(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "StaticMappingSecretProvider",
            "_credential_version_fingerprint",
            "/Users/enzo/IdeaProjects/weex-mcp",
            "pip install",
            "urlopen(",
            "requests.",
            "direct_rest",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
