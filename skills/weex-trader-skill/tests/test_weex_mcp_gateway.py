#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import sys
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "weex_mcp_gateway.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

DIRECT_DIGEST = "sha256:" + "2" * 64
LEGACY_DIGEST = "sha256:" + "3" * 64
INVENTORY_DIGEST = "sha256:" + "4" * 64
MATRIX_DIGEST = "sha256:" + "5" * 64
SOURCE_CATALOG_DIGESTS = {
    "contract": "sha256:" + "6" * 64,
    "spot": "sha256:" + "7" * 64,
    "partner": "sha256:" + "8" * 64,
}
VERSIONS = {
    "package": "0.3.4",
    "tool_contract": "2",
    "manifest": "2",
    "output": "1",
    "prepare": "1",
    "request_description": "2",
    "transport_meta": "1",
    "grant": "2",
}


def load_gateway_module():
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


def tool_catalog(*, trade_enabled: bool) -> list[dict[str, object]]:
    output_schema = {
        "type": "object",
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }
    tools: list[dict[str, object]] = []
    for name, module in (
        ("market_get_symbol_price", "market"),
        ("account_get_balance", "account"),
        ("partner_verify_referrals", "partner"),
    ):
        tools.append(
            {
                "name": name,
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "market": {"type": "string"},
                        "symbol": {"type": "string"},
                    },
                },
                "outputSchema": copy.deepcopy(output_schema),
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": True,
                },
                "_meta": {
                    "com.weex/mcp": {"module": module, "permission": "NONE" if module == "market" else "USER_DATA"}
                },
            }
        )
    if trade_enabled:
        tools.append(
            {
                "name": "trade_place_order",
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "market": {"type": "string"},
                        "symbol": {"type": "string"},
                        "client_order_id": {"type": "string"},
                    },
                },
                "outputSchema": copy.deepcopy(output_schema),
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
                "_meta": {"com.weex/mcp": {"module": "trade", "permission": "TRADE"}},
            }
        )
    return sorted(tools, key=lambda item: str(item["name"]))


def build_manifest(tools: list[dict[str, object]], *, trade_enabled: bool) -> dict[str, object]:
    names = sorted(str(tool["name"]) for tool in tools)
    schemas = sorted(
        [
            {
                "name": tool["name"],
                "inputSchema": tool["inputSchema"],
                "outputSchema": tool["outputSchema"],
            }
            for tool in tools
        ],
        key=lambda item: str(item["name"]),
    )
    manifest: dict[str, object] = {
        "manifest_version": 2,
        "package_version": "0.3.4",
        "tool_contract_version": "2",
        "runtime_profile": "local_user",
        "trade_enabled": trade_enabled,
        "tools": names,
        "credential_modes": [],
        "delegated_authorization": False,
        "modules": ["account", "market", "partner", *(["trade"] if trade_enabled else [])],
        "tool_set_digest": canonical_digest(names),
        "schema_digest": canonical_digest(schemas),
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    return manifest


def build_static_manifest(tools: list[dict[str, object]]) -> dict[str, object]:
    projections = sorted(tools, key=lambda item: str(item["name"]))
    manifest: dict[str, object] = {
        "status": "CANDIDATE_NEEDS_RELEASE_OWNER",
        "versions": dict(VERSIONS),
        "package_version": "0.3.4",
        "composition_kind": "local_user",
        "tools": [item["name"] for item in projections],
        "input_schema_digest": canonical_digest(
            [{"name": item["name"], "schema": item["inputSchema"]} for item in projections]
        ),
        "output_schema_digest": canonical_digest(
            [{"name": item["name"], "schema": item["outputSchema"]} for item in projections]
        ),
        "annotations_digest": canonical_digest(
            [{"name": item["name"], "annotations": item["annotations"]} for item in projections]
        ),
        "tool_meta_digest": canonical_digest([{"name": item["name"], "_meta": item["_meta"]} for item in projections]),
        "boundary_contract_digest": boundary_contract()["boundary_contract_digest"],
        "direct_tools_digest": DIRECT_DIGEST,
        "legacy_mapping_digest": LEGACY_DIGEST,
        "inventory_digest": INVENTORY_DIGEST,
        "matrix_digest": MATRIX_DIGEST,
        "source_catalog_digests": dict(SOURCE_CATALOG_DIGESTS),
        "credential_binding_version": 1,
        "grant_consumer_version": 2,
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    return manifest


def availability(
    tools: list[dict[str, object]], *, trade_enabled: bool, mutation_ready: bool = False
) -> dict[str, object]:
    actions = [] if not trade_enabled or mutation_ready else ["configure_execution_grant_consumer"]
    return {
        "effective_tools": [tool["name"] for tool in tools],
        "private_read_ready": True,
        "mutation_ready": bool(trade_enabled and mutation_ready),
        "action_required": actions,
    }


def mapping_for(
    manifests: dict[bool, dict[str, object]],
    statics: dict[bool, dict[str, object]],
) -> dict[str, object]:
    profiles = []
    for trade_enabled in (False, True):
        manifest = manifests[trade_enabled]
        profiles.append(
            {
                "runtime_profile": "local_user",
                "trade_enabled": trade_enabled,
                "tool_count": len(manifest["tools"]),
                "tool_set_digest": manifest["tool_set_digest"],
                "schema_digest": manifest["schema_digest"],
                "manifest_digest": manifest["manifest_digest"],
                "boundary_static_manifest_digest": statics[trade_enabled]["manifest_digest"],
            }
        )
    return {
        "schema_version": 1,
        "mapping_version": "trader-mcp-capability-v2",
        "verified_mcp_candidate": {
            "official_repository": "https://github.com/weex-labs/weex-mcp",
            "package_version": "0.3.4",
            "tool_contract_version": "2",
            "manifest_version": 2,
            "source_commit": "45c7bf5ae63db360cf6ec0e18d92806d3f416391",
            "publication_status": "unpublished",
            "distribution_sha256": {"wheel": "a" * 64, "sdist": "b" * 64},
            "versions": dict(VERSIONS),
            "boundary_contract_digest": boundary_contract()["boundary_contract_digest"],
            "direct_tools_digest": DIRECT_DIGEST,
            "legacy_mapping_digest": LEGACY_DIGEST,
            "inventory_digest": INVENTORY_DIGEST,
            "matrix_digest": MATRIX_DIGEST,
            "source_catalog_digests": dict(SOURCE_CATALOG_DIGESTS),
            "compatibility_profiles": profiles,
        },
    }


def boundary_contract() -> dict[str, object]:
    body: dict[str, object] = {
        "candidate_versions": {**VERSIONS, "intent": "2", "status": "CANDIDATE_NEEDS_RELEASE_OWNER"},
        "direct_tools_digest": DIRECT_DIGEST,
        "compatibility": {
            "legacy_mapping_digest": LEGACY_DIGEST,
            "inventory_digest": INVENTORY_DIGEST,
            "matrix_digest": MATRIX_DIGEST,
            "source_catalog_digests": dict(SOURCE_CATALOG_DIGESTS),
        },
        "permissions": {
            "account": {
                "account_get_balance/spot": {
                    "host_policy_capability": "account.spot.get_balance",
                    "openapi_truth": "accountDetails",
                }
            },
            "market": {},
            "partner": {
                "partner_verify_referrals": {
                    "host_policy_capability": "partner.read.verify_referrals",
                    "openapi_truth": "accountDetails",
                }
            },
            "trade": {
                "trade_place_order/spot": {
                    "host_policy_capability": "trade.spot.place_order",
                    "required_upstream_evidence": ["spotTrading"],
                }
            },
        },
    }
    return {"boundary_contract_digest": canonical_digest(body), **body}


class FakeSecretProviderError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FakePrincipal:
    principal_id: str
    tenant_id: str
    authn_method: str
    authenticated_at: int
    trust_level: str


@dataclass(frozen=True)
class FakeAccountBinding:
    account_binding_id: str
    profile_id: str
    allowed_markets: frozenset[str]
    route_fingerprint: str


@dataclass(frozen=True)
class FakeCredentialBinding:
    binding_id: str
    credential_fingerprint: str
    host_policy_capabilities: frozenset[str]
    upstream_permission_evidence: dict[str, str]
    valid_until: int | None


@dataclass(frozen=True)
class FakeCorrelation:
    request_id: str
    session_id: str = ""


@dataclass(frozen=True)
class FakeExecutionGrant:
    grant_id: str
    state: str = "ISSUED"
    principal_id: str = "profile-a"
    account_binding_id: str = "account-binding"
    credential_fingerprint: str = "credential-fingerprint"
    tool_name: str = "trade_place_order"
    prepared_artifact_digest: str = "sha256:" + "d" * 64
    execution_binding_digest: str = "sha256:" + "e" * 64
    issued_at: int = 100
    expires_at: int = 1_000
    claim_id: str | None = None
    terminal_state: str | None = None
    outcome_digest: str | None = None


@dataclass(frozen=True)
class FakeInvocationContext:
    principal: FakePrincipal
    account_binding: FakeAccountBinding | None
    credential_binding: FakeCredentialBinding | None
    execution_grant: FakeExecutionGrant | None
    correlation: FakeCorrelation


@dataclass(frozen=True)
class FakeResolvedCredential:
    key_id: str
    api_key: str
    api_secret: str
    api_passphrase: str
    permissions: frozenset[str]
    fingerprint: str
    valid_until: int | None


class FakeSettings:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakePrepared:
    def __init__(
        self,
        *,
        tool_name: str,
        route_fingerprint: str,
        credential_fingerprint: str,
    ):
        self.tool_name = tool_name
        self.route_fingerprint = route_fingerprint
        self.credential_fingerprint = credential_fingerprint

    def to_dict(self):
        return {
            "version": 1,
            "tool_name": self.tool_name,
            "prepared_artifact_digest": "sha256:" + "d" * 64,
            "execution_binding_digest": "sha256:" + "e" * 64,
            "route_fingerprint": self.route_fingerprint,
            "credential_fingerprint": self.credential_fingerprint,
            "expires_at": 1_000,
        }


class FakeService:
    def __init__(
        self,
        *,
        tools: list[dict[str, object]],
        manifest: dict[str, object],
        static_manifest: dict[str, object],
        runtime_availability: dict[str, object],
    ):
        self.tools = copy.deepcopy(tools)
        self.manifest = copy.deepcopy(manifest)
        self.static_manifest = copy.deepcopy(static_manifest)
        self.runtime_availability = copy.deepcopy(runtime_availability)
        self.calls: list[tuple[str, dict[str, object], object]] = []
        self.prepares: list[tuple[str, dict[str, object], object]] = []
        self.recoveries: list[tuple[str, object, int | None]] = []
        self.tool_handler = None

    def prepare_mutation(self, *, tool_name, arguments, invocation_context):
        self.prepares.append((tool_name, copy.deepcopy(arguments), invocation_context))
        return FakePrepared(
            tool_name=tool_name,
            route_fingerprint=invocation_context.account_binding.route_fingerprint,
            credential_fingerprint=invocation_context.credential_binding.credential_fingerprint,
        )

    def recover_mutation(
        self,
        *,
        tool_name,
        invocation_context,
        now_ms=None,
    ):
        self.recoveries.append((tool_name, invocation_context, now_ms))
        return SimpleNamespace(
            grant_id=invocation_context.execution_grant.grant_id,
            state="REVIEW_REQUIRED",
            recovered_at=now_ms,
            reason="unfinalized_claim",
        )

    def handle_jsonrpc(self, request, *, context=None):
        method = request.get("method")
        self.calls.append((str(method), copy.deepcopy(request), context))
        if method == "server/discover":
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "_meta": {
                        "com.weex/mcp": {
                            "compatibility": copy.deepcopy(self.manifest),
                            "boundaryStaticManifest": copy.deepcopy(self.static_manifest),
                            "runtimeAvailability": copy.deepcopy(self.runtime_availability),
                        }
                    }
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request["id"], "result": {"tools": copy.deepcopy(self.tools)}}
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
                    "_meta": {
                        "com.weex/transport": {
                            "version": 1,
                            "request_id": "upstream-request",
                            "attempt_count": 1,
                            "http_status": 200,
                        }
                    },
                },
            }
        raise AssertionError(method)


class GrantConsumerSpy:
    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        if name in {"load", "claim", "finalize", "recover"}:
            def fail(**_kwargs):
                self.calls.append(name)
                raise AssertionError(f"grant builder must not call {name}")

            return fail
        raise AttributeError(name)


def fake_runtime(service: FakeService, *, package_version: str = "0.3.4"):
    def build_local_user_service(**kwargs):
        service.settings = kwargs["settings"]
        service.secret_provider = kwargs["secret_provider"]
        service.grant_consumer = kwargs.get("grant_consumer")
        return service

    def describe_request(*, settings, name, arguments):
        trade_enabled = bool(getattr(settings, "trade_enabled", False))
        private = name.startswith(("account_", "partner_", "trade_"))
        return {
            "method": "POST" if name.startswith("trade_") else "GET",
            "url": "https://api-spot.weex.com/description",
            "headers": {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "locale": "en-US",
                "User-Agent": "weex-mcp/0.3",
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
        VerifiedPrincipal=FakePrincipal,
        AccountBinding=FakeAccountBinding,
        CredentialBinding=FakeCredentialBinding,
        CorrelationContext=FakeCorrelation,
        AuthenticatedInvocationContext=FakeInvocationContext,
        ResolvedWeexCredential=FakeResolvedCredential,
        SecretProviderError=FakeSecretProviderError,
        ExecutionGrant=FakeExecutionGrant,
    )


class GatewayFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway_module = load_gateway_module()
        self.assertIsNotNone(self.gateway_module)
        self.credentials = SimpleNamespace(
            api_key="AK_ONE",
            api_secret="SK_ONE",
            api_passphrase="PP_ONE",
        )
        self.credential_loads: list[str] = []
        self.binding_key = "binding-key-" + "b" * 48
        self.binding_status = {
            "present": True,
            "valid": True,
            "length": len(self.binding_key),
            "fp_sha256": hashlib.sha256(self.binding_key.encode()).hexdigest()[:16],
        }

    def credential_loader(self, profile_id: str):
        self.credential_loads.append(profile_id)
        return self.credentials

    @staticmethod
    def profile_loader(profile_id: str):
        return SimpleNamespace(profile_id=profile_id, name="main")

    def make_gateway(
        self,
        *,
        trade_enabled: bool,
        profile_id: str | None = "profile-a",
        mutation_ready: bool = False,
        grant_consumer=None,
        boundary_contract_loader=boundary_contract,
        effective_spot_base_url: str = "https://api-spot.weex.com",
        effective_futures_base_url: str = "https://api-contract.weex.com",
        timeout_seconds: float = 15.0,
        request_locale: str = "en-US",
    ):
        tools_by_mode = {flag: tool_catalog(trade_enabled=flag) for flag in (False, True)}
        manifests = {flag: build_manifest(tools_by_mode[flag], trade_enabled=flag) for flag in (False, True)}
        statics = {flag: build_static_manifest(tools_by_mode[flag]) for flag in (False, True)}
        active_tools = tools_by_mode[trade_enabled]
        service = FakeService(
            tools=active_tools,
            manifest=manifests[trade_enabled],
            static_manifest=statics[trade_enabled],
            runtime_availability=availability(
                active_tools,
                trade_enabled=trade_enabled,
                mutation_ready=mutation_ready,
            ),
        )
        gateway = self.gateway_module.LocalMcpGateway(
            profile_id=profile_id,
            effective_spot_base_url=effective_spot_base_url,
            effective_futures_base_url=effective_futures_base_url,
            trade_enabled=trade_enabled,
            timeout_seconds=timeout_seconds,
            request_locale=request_locale,
            runtime_loader=lambda _expected: fake_runtime(service),
            mapping_loader=lambda: mapping_for(manifests, statics),
            boundary_contract_loader=boundary_contract_loader,
            profile_loader=self.profile_loader,
            credential_loader=self.credential_loader,
            binding_key_loader=lambda: self.binding_key,
            binding_key_status_loader=lambda: dict(self.binding_status),
            grant_consumer=grant_consumer,
        )
        return gateway, service


class VaultSecretProviderTests(GatewayFixture):
    def make_provider(self, *, profile_id: str = "profile-a"):
        runtime = fake_runtime(
            FakeService(
                tools=[],
                manifest={},
                static_manifest={},
                runtime_availability={},
            )
        )
        return self.gateway_module.VaultSecretProvider(
            profile_id=profile_id,
            account_binding_id="account-binding",
            binding_id="credential-binding",
            fingerprint_key=b"f" * 32,
            credential_loader=self.credential_loader,
            runtime=runtime,
        ), runtime

    def context(self, runtime, *, profile_id: str = "profile-a", capabilities=None):
        provider, _ = self.make_provider(profile_id=profile_id)
        fingerprint = provider._current_credential_fingerprint()
        capabilities = frozenset(capabilities or {"account.spot.get_balance"})
        return runtime.AuthenticatedInvocationContext(
            principal=runtime.VerifiedPrincipal("principal", "tenant", "local", 1, "local_process"),
            account_binding=runtime.AccountBinding(
                "account-binding",
                profile_id,
                frozenset({"spot", "futures"}),
                "sha256:" + "a" * 64,
            ),
            credential_binding=runtime.CredentialBinding(
                "credential-binding",
                fingerprint,
                capabilities,
                {"accountDetails": "unverified"},
                None,
            ),
            execution_grant=None,
            correlation=runtime.CorrelationContext("request", "session"),
        )

    def test_provider_resolves_from_trusted_context_without_public_credential_argument(self) -> None:
        provider, runtime = self.make_provider(profile_id="配置一")
        context = self.context(runtime, profile_id="配置一")

        resolved = provider.resolve(
            invocation_context=context,
            required_capabilities=frozenset({"account.spot.get_balance", "accountDetails"}),
        )

        self.assertEqual(resolved.api_key, "AK_ONE")
        self.assertEqual(resolved.fingerprint, context.credential_binding.credential_fingerprint)
        self.assertEqual(resolved.valid_until, None)
        self.assertEqual(resolved.permissions, frozenset())

    def test_provider_rejects_profile_binding_capability_and_fingerprint_drift(self) -> None:
        provider, runtime = self.make_provider()
        valid = self.context(runtime)
        cases = (
            runtime.AuthenticatedInvocationContext(
                valid.principal,
                runtime.AccountBinding("account-binding", "other", frozenset({"spot"}), "sha256:" + "a" * 64),
                valid.credential_binding,
                None,
                valid.correlation,
            ),
            runtime.AuthenticatedInvocationContext(
                valid.principal,
                valid.account_binding,
                runtime.CredentialBinding(
                    "credential-binding",
                    valid.credential_binding.credential_fingerprint,
                    frozenset(),
                    {"accountDetails": "unverified"},
                    None,
                ),
                None,
                valid.correlation,
            ),
            runtime.AuthenticatedInvocationContext(
                valid.principal,
                valid.account_binding,
                runtime.CredentialBinding(
                    "credential-binding",
                    "stale",
                    valid.credential_binding.host_policy_capabilities,
                    {"accountDetails": "unverified"},
                    None,
                ),
                None,
                valid.correlation,
            ),
        )
        for context in cases:
            with self.subTest(context=context):
                before = len(self.credential_loads)
                with self.assertRaises(runtime.SecretProviderError):
                    provider.resolve(
                        invocation_context=context,
                        required_capabilities=frozenset({"account.spot.get_balance"}),
                    )
                self.assertLessEqual(len(self.credential_loads) - before, 1)

    def test_provider_reloads_credentials_and_rotation_changes_fingerprint(self) -> None:
        provider, _runtime = self.make_provider()
        first = provider._current_credential_fingerprint()
        self.credentials = SimpleNamespace(
            api_key="AK_TWO",
            api_secret="SK_TWO",
            api_passphrase="PP_TWO",
        )
        second = provider._current_credential_fingerprint()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(self.credential_loads), 2)

    def test_provider_loader_failure_is_safe(self) -> None:
        provider, runtime = self.make_provider()
        provider._credential_loader = lambda _profile: (_ for _ in ()).throw(RuntimeError("SK_LEAK /private/vault"))
        with self.assertRaises(runtime.SecretProviderError) as caught:
            provider._current_credential_fingerprint()
        self.assertEqual(caught.exception.code, "secret_provider_unconfigured")
        self.assertNotIn("SK_LEAK", str(caught.exception))


class LocalMcpGatewayTests(GatewayFixture):
    def test_module_import_has_no_runtime_dependency_or_legacy_authorization_surface(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "TenantContext",
            "approval_token_secret",
            "delegated_authorizer",
            "credential_argument",
            "trade_preview_",
            "trade_confirm_",
        ):
            self.assertNotIn(forbidden, source)

    def test_gateway_builds_v2_runtime_and_passes_resolved_routes(self) -> None:
        gateway, service = self.make_gateway(
            trade_enabled=False,
            effective_spot_base_url="https://stg-spotpro-openapi.weex.tech",
            effective_futures_base_url="https://stg-api-contract.weex.tech",
            timeout_seconds=7.5,
            request_locale="zh-CN",
        )
        self.assertEqual(gateway.runtime_status()["version"], "0.3.4")
        self.assertEqual(
            service.settings.spot_base_url,
            "https://stg-spotpro-openapi.weex.tech",
        )
        self.assertEqual(
            service.settings.futures_base_url,
            "https://stg-api-contract.weex.tech",
        )
        self.assertEqual(service.settings.request_locale, "zh-CN")
        self.assertEqual(service.settings.market_timeout_seconds, 7.5)
        self.assertEqual(service.settings.account_timeout_seconds, 7.5)
        self.assertEqual(service.settings.partner_timeout_seconds, 7.5)
        self.assertEqual(service.settings.market_max_attempts, 1)
        self.assertEqual(service.settings.market_retry_base_delay_seconds, 0.0)
        self.assertFalse(hasattr(service.settings, "approval_token_secret"))

    def test_public_call_needs_no_profile_and_private_call_requires_profile(self) -> None:
        gateway, _ = self.make_gateway(trade_enabled=False, profile_id=None)
        result = gateway.call_typed_tool(
            "market_get_symbol_price",
            {"market": "spot", "symbol": "BTCUSDT"},
        )
        self.assertTrue(result["structured_content"]["ok"])
        with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
            gateway.call_typed_tool("account_get_balance", {"market": "spot"})
        self.assertEqual(caught.exception.code, "profile_required")

        with self.assertRaises(self.gateway_module.McpGatewayError) as trade_gateway:
            self.make_gateway(trade_enabled=True, profile_id=None)
        self.assertEqual(trade_gateway.exception.code, "profile_required")

    def test_private_call_uses_context_and_never_injects_credential_argument(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False)

        def resolve_through_gateway(request, context):
            resolved = service.secret_provider.resolve(
                invocation_context=context,
                required_capabilities=frozenset({"account.spot.get_balance", "accountDetails"}),
            )
            self.assertEqual(resolved.api_key, "AK_ONE")
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "structuredContent": {"ok": True, "data": {}},
                    "isError": False,
                },
            }

        service.tool_handler = resolve_through_gateway
        result = gateway.call_typed_tool("account_get_balance", {"market": "spot"})
        method, request, context = service.calls[-1]
        self.assertEqual(method, "tools/call")
        self.assertEqual(request["params"]["arguments"], {"market": "spot"})
        self.assertIsInstance(context, FakeInvocationContext)
        self.assertIsNone(context.execution_grant)
        self.assertEqual(context.account_binding.profile_id, "profile-a")
        self.assertIsNone(result["transport_meta"])

    def test_binding_key_rotation_rejects_private_call_before_tool_dispatch(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False)
        self.binding_status["fp_sha256"] = "0" * 16
        before = len(service.calls)
        with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
            gateway.call_typed_tool("account_get_balance", {"market": "spot"})
        self.assertEqual(caught.exception.code, "binding_key_changed")
        self.assertEqual(len(service.calls), before + 2)  # fresh discover/list only

    def test_caller_credential_and_routing_fields_are_rejected_before_preflight(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False)
        before = len(service.calls)
        for arguments in (
            {"market": "spot", "credential": {"ref": "caller"}},
            {"market": "spot", "expected_environment": "staging"},
            {"market": "spot", "endpoint": "https://evil.example"},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
                    gateway.call_typed_tool("account_get_balance", arguments)
                self.assertEqual(caught.exception.code, "invalid_arguments")
                self.assertEqual(len(service.calls), before)

    def test_prepare_mutation_uses_context_without_execution_grant(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=True)
        prepared = gateway.prepare_mutation(
            "trade_place_order",
            {"market": "spot", "symbol": "BTCUSDT", "client_order_id": "client-1"},
        )
        self.assertEqual(prepared["tool_name"], "trade_place_order")
        _name, arguments, context = service.prepares[-1]
        self.assertEqual(arguments["client_order_id"], "client-1")
        self.assertIsNone(context.execution_grant)

    def test_build_execution_grant_uses_trusted_context_without_claim_or_persist(self) -> None:
        consumer = GrantConsumerSpy()
        gateway, service = self.make_gateway(
            trade_enabled=True,
            grant_consumer=consumer,
        )
        prepared = gateway.prepare_mutation(
            "trade_place_order",
            {"market": "spot", "symbol": "BTCUSDT", "client_order_id": "client-1"},
        )
        trusted_context = service.prepares[-1][2]
        before_calls = len(service.calls)
        before_prepares = len(service.prepares)

        grant = gateway.build_execution_grant(
            prepared,
            grant_id="grant-1",
            issued_at=100,
        )

        self.assertEqual(
            grant,
            FakeExecutionGrant(
                grant_id="grant-1",
                principal_id=trusted_context.principal.principal_id,
                account_binding_id=trusted_context.account_binding.account_binding_id,
                credential_fingerprint=trusted_context.credential_binding.credential_fingerprint,
            ),
        )
        self.assertEqual(len(service.calls), before_calls)
        self.assertEqual(len(service.prepares), before_prepares)
        self.assertEqual(consumer.calls, [])

    def test_build_execution_grant_rejects_untrusted_or_invalid_prepared_input(self) -> None:
        gateway, _service = self.make_gateway(trade_enabled=True)
        prepared = gateway.prepare_mutation(
            "trade_place_order",
            {"market": "spot", "symbol": "BTCUSDT", "client_order_id": "client-1"},
        )
        cases = (
            ({**prepared, "route_fingerprint": "sha256:" + "0" * 64}, "grant-1", 100, "invalid_arguments"),
            ({**prepared, "credential_fingerprint": "stale"}, "grant-1", 100, "invalid_arguments"),
            ({**prepared, "tool_name": "account_get_balance"}, "grant-1", 100, "unsupported_tool"),
            ({**prepared, "prepared_artifact_digest": "invalid"}, "grant-1", 100, "mcp_protocol_error"),
            ({**prepared, "expires_at": 100}, "grant-1", 100, "invalid_arguments"),
            (prepared, "", 100, "invalid_arguments"),
            (prepared, "grant-1", True, "invalid_arguments"),
        )
        for candidate, grant_id, issued_at, error_code in cases:
            with self.subTest(candidate=candidate, grant_id=grant_id, issued_at=issued_at):
                with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
                    gateway.build_execution_grant(
                        candidate,
                        grant_id=grant_id,
                        issued_at=issued_at,
                    )
                self.assertEqual(caught.exception.code, error_code)

    def test_build_execution_grant_rejects_post_prepare_binding_drift(self) -> None:
        consumer = GrantConsumerSpy()
        gateway, service = self.make_gateway(
            trade_enabled=True,
            grant_consumer=consumer,
        )
        prepared = gateway.prepare_mutation(
            "trade_place_order",
            {"market": "spot", "symbol": "BTCUSDT", "client_order_id": "client-1"},
        )
        before_calls = len(service.calls)
        before_prepares = len(service.prepares)
        self.credentials = SimpleNamespace(
            api_key="AK_ROTATED",
            api_secret="SK_ROTATED",
            api_passphrase="PP_ROTATED",
        )

        with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
            gateway.build_execution_grant(
                prepared,
                grant_id="grant-1",
                issued_at=100,
            )

        self.assertEqual(caught.exception.code, "invalid_arguments")
        self.assertEqual(len(service.calls), before_calls)
        self.assertEqual(len(service.prepares), before_prepares)
        self.assertEqual(consumer.calls, [])

    def test_recover_mutation_uses_host_private_service_with_claimed_context(self) -> None:
        gateway, service = self.make_gateway(
            trade_enabled=True,
            grant_consumer=object(),
        )
        prepared = gateway.prepare_mutation(
            "trade_place_order",
            {"market": "spot", "symbol": "BTCUSDT", "client_order_id": "client-1"},
        )
        issued = gateway.build_execution_grant(
            prepared,
            grant_id="grant-1",
            issued_at=100,
        )
        claimed = replace(
            issued,
            state="CLAIMED",
            claim_id="claim-1",
        )
        before_credential_loads = len(self.credential_loads)
        self.binding_status["fp_sha256"] = "0" * 16
        self.credentials = SimpleNamespace(
            api_key="AK_ROTATED",
            api_secret="SK_ROTATED",
            api_passphrase="PP_ROTATED",
        )

        recovery = gateway.recover_mutation(
            tool_name="trade_place_order",
            execution_grant=claimed,
            now_ms=300,
        )

        self.assertEqual(recovery.state, "REVIEW_REQUIRED")
        self.assertEqual(len(service.recoveries), 1)
        tool_name, context, now_ms = service.recoveries[0]
        self.assertEqual(tool_name, "trade_place_order")
        self.assertIs(context.execution_grant, claimed)
        self.assertEqual(now_ms, 300)
        self.assertEqual(len(self.credential_loads), before_credential_loads)
        self.assertEqual(
            context.credential_binding.credential_fingerprint,
            claimed.credential_fingerprint,
        )
        listed = service.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}}
        )
        self.assertNotIn(
            "recover_mutation",
            {tool["name"] for tool in listed["result"]["tools"]},
        )

    def test_reconcile_uses_private_service_and_returns_only_bound_digest(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=True, grant_consumer=object())
        prepared = gateway.prepare_mutation(
            "trade_place_order",
            {"market": "spot", "symbol": "BTCUSDT", "client_order_id": "client-1"},
        )
        digest = "sha256:" + "9" * 64
        payload = {
            "tool_name": "trade_place_order",
            "prepared_artifact_digest": prepared["prepared_artifact_digest"],
            "status": "RESOLVED_ACCEPTED",
            "result": "ACCEPTED",
            "observed_at": 300,
            "evidence": {"kind": "query"},
            "evidence_digest": digest,
        }
        private = mock.Mock(return_value=SimpleNamespace(to_dict=lambda: payload))
        service.reconcile_mutation = private
        active_boundary = gateway._expected_candidate["boundary_contract_digest"]
        with mock.patch.object(
            self.gateway_module, "_PREVIOUS_RECONCILIATION_BOUNDARY_DIGESTS", (active_boundary,), create=True,
        ):
            result = gateway.reconcile_mutation(prepared, now_ms=300, boundary_contract_digest=active_boundary)

        arguments = private.call_args.kwargs
        self.assertEqual(arguments["prepared_mutation"], prepared)
        self.assertIsNone(arguments["invocation_context"].execution_grant)
        self.assertEqual(arguments["now_ms"], 300)
        self.assertEqual(result, {
            "status": "RESOLVED_ACCEPTED",
            "observed_at": 300,
            "evidence_digest": digest,
        })
        self.assertFalse(any(method == "tools/call" for method, *_ in service.calls))

        payload["prepared_artifact_digest"] = "sha256:" + "7" * 64
        with self.assertRaises(self.gateway_module.McpGatewayError):
            gateway.reconcile_mutation(prepared, now_ms=300)

    def test_previous_candidate_reconciliation_uses_isolated_read_binding(self) -> None:
        for previous_contract in (
            "sha256:a6762cafe7b9338b658252e6048582c2be8fca0bc1b5c730223ff807868be9b9",
            "sha256:a08c5997ab1ef4698ad1bd37ff89378fedf340600dde668d0f9fc5fc256b7b10",
        ):
            with self.subTest(previous_contract=previous_contract):
                self._assert_previous_reconciliation_is_isolated(previous_contract)

    def _assert_previous_reconciliation_is_isolated(self, previous_contract: str) -> None:
        gateway, service = self.make_gateway(trade_enabled=True, grant_consumer=object())
        arguments = {"market": "spot", "symbol": "BTCUSDT", "client_order_id": "client-1"}
        active = gateway.prepare_mutation("trade_place_order", arguments)
        fingerprint_domain = b"weex-trader-local-mcp-credential-v2\0"
        previous_key = hmac.digest(
            self.binding_key.encode(), fingerprint_domain + previous_contract.encode(), "sha256",
        )
        credential_values = json.dumps(
            {"profile_id": "profile-a", **vars(self.credentials)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
        previous_fingerprint = "fp_" + hmac.digest(
            previous_key, fingerprint_domain + credential_values, "sha256",
        ).hex()
        self.assertNotEqual(previous_fingerprint, active["credential_fingerprint"])
        prepared = {**active, "credential_fingerprint": previous_fingerprint}
        payload = {
            "tool_name": prepared["tool_name"],
            "prepared_artifact_digest": prepared["prepared_artifact_digest"],
            "status": "RESOLVED_ACCEPTED",
            "result": "ACCEPTED",
            "observed_at": 300,
            "evidence": {"kind": "query"},
            "evidence_digest": "sha256:" + "9" * 64,
        }
        private_read = mock.Mock(return_value=SimpleNamespace(to_dict=lambda: payload))
        old_read_service = SimpleNamespace(reconcile_mutation=private_read)
        with mock.patch.object(
            gateway._runtime, "build_local_user_service", return_value=old_read_service,
        ) as build_read_service:
            self.assertEqual(
                gateway.reconcile_mutation(
                    prepared, now_ms=300, boundary_contract_digest=previous_contract,
                )["status"],
                "RESOLVED_ACCEPTED",
            )
            build_read_service.assert_called_once()
            service_options = build_read_service.call_args.kwargs
            self.assertFalse(service_options["settings"].trade_enabled)
            self.assertNotIn("grant_consumer", service_options)
            self.assertTrue(service.settings.trade_enabled)
            self.assertIsNot(service_options["secret_provider"], service.secret_provider)
            context = private_read.call_args.kwargs["invocation_context"]
            self.assertIsNone(context.execution_grant)
            self.assertEqual(
                context.credential_binding.host_policy_capabilities,
                frozenset({"account.spot.get_balance"}),
            )
            resolved = service_options["secret_provider"].resolve(
                invocation_context=context,
                required_capabilities=frozenset({"account.spot.get_balance", "accountDetails"}),
            )
            self.assertEqual(resolved.fingerprint, previous_fingerprint)
            with mock.patch.object(self.credentials, "api_secret", "SK_ROTATED"):
                with self.assertRaises(self.gateway_module.McpGatewayError):
                    gateway.reconcile_mutation(
                        prepared, now_ms=300, boundary_contract_digest=previous_contract,
                    )
            with self.assertRaises(self.gateway_module.McpGatewayError):
                gateway.reconcile_mutation(
                    {**prepared, "route_fingerprint": "sha256:" + "8" * 64},
                    now_ms=300, boundary_contract_digest=previous_contract,
                )
            with self.assertRaises(self.gateway_module.McpGatewayError):
                gateway.reconcile_mutation(
                    prepared, now_ms=300, boundary_contract_digest="sha256:" + "0" * 64,
                )
            private_read.assert_called_once()

        self.assertEqual(gateway.prepare_mutation("trade_place_order", arguments), active)

    def test_direct_call_requires_execution_grant_only_in_context(self) -> None:
        gateway, service = self.make_gateway(
            trade_enabled=True,
            mutation_ready=True,
            grant_consumer=object(),
        )
        arguments = {"market": "spot", "symbol": "BTCUSDT", "client_order_id": "client-1"}
        with self.assertRaises(self.gateway_module.McpGatewayError) as missing:
            gateway.call_typed_tool("trade_place_order", arguments)
        self.assertEqual(missing.exception.code, "execution_grant_required")

        grant = FakeExecutionGrant("grant-1")
        gateway.call_typed_tool(
            "trade_place_order",
            arguments,
            execution_grant=grant,
        )
        _method, request, context = service.calls[-1]
        self.assertNotIn("execution_grant", request["params"]["arguments"])
        self.assertIs(context.execution_grant, grant)

    def test_fixed_wrapper_preserves_top_level_transport_meta_and_null_absence(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False)
        wrapped = gateway.call_typed_tool(
            "market_get_symbol_price",
            {"market": "spot", "symbol": "BTCUSDT"},
        )
        self.assertEqual(set(wrapped), {"structured_content", "is_error", "transport_meta"})
        self.assertFalse(wrapped["is_error"])
        self.assertEqual(wrapped["transport_meta"]["version"], 1)

        def no_meta(request, _context):
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"structuredContent": {"ok": True}, "isError": False},
            }

        service.tool_handler = no_meta
        without_meta = gateway.call_typed_tool(
            "market_get_symbol_price",
            {"market": "spot", "symbol": "BTCUSDT"},
        )
        self.assertIsNone(without_meta["transport_meta"])

        def typed_error(request, _context):
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "structuredContent": {
                        "ok": False,
                        "error": {"code": "rejected", "message": "Rejected."},
                    },
                    "isError": True,
                    "_meta": {
                        "com.weex/transport": {
                            "version": 1,
                            "request_id": "typed-error",
                            "attempt_count": 1,
                        }
                    },
                },
            }

        service.tool_handler = typed_error
        rejected = gateway.call_typed_tool(
            "market_get_symbol_price",
            {"market": "spot", "symbol": "BTCUSDT"},
        )
        self.assertTrue(rejected["is_error"])
        self.assertFalse(rejected["structured_content"]["ok"])
        self.assertEqual(
            rejected["structured_content"]["error"]["code"],
            "rejected",
        )
        self.assertEqual(rejected["transport_meta"]["request_id"], "typed-error")

    def test_malformed_transport_meta_fails_closed_without_payload_pollution(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False)
        for invalid in (
            {"version": 1, "request_id": "r", "attempt_count": 0},
            {"version": 1, "request_id": "r", "attempt_count": 1, "secret": "SK_LEAK"},
            "invalid",
        ):

            def handler(request, _context, value=invalid):
                return {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "structuredContent": {"ok": True},
                        "isError": False,
                        "_meta": {"com.weex/transport": value},
                    },
                }

            service.tool_handler = handler
            with self.subTest(invalid=invalid):
                with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
                    gateway.call_typed_tool(
                        "market_get_symbol_price",
                        {"market": "spot", "symbol": "BTCUSDT"},
                    )
                self.assertEqual(caught.exception.code, "mcp_protocol_error")
                self.assertNotIn("SK_LEAK", str(caught.exception))

    def test_static_contract_is_pinned_while_dynamic_readiness_may_change(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=True)
        first = gateway.runtime_status()
        self.assertTrue(first["manifest_ok"])
        self.assertFalse(first["mutation_ready"])
        self.assertEqual(first["runtime_action_required"], ["configure_execution_grant_consumer"])

        service.runtime_availability = availability(
            service.tools,
            trade_enabled=True,
            mutation_ready=True,
        )
        second = gateway.runtime_status()
        self.assertTrue(second["manifest_ok"])
        self.assertTrue(second["mutation_ready"])
        self.assertEqual(second["boundary_manifest_digest"], first["boundary_manifest_digest"])

        service.runtime_availability["action_required"] = ["AK_LEAK"]
        malformed = gateway.runtime_status()
        self.assertFalse(malformed["manifest_ok"])
        self.assertEqual(malformed["action_required"], "repair_mcp_runtime")
        self.assertNotIn("AK_LEAK", json.dumps(malformed, sort_keys=True))

    def test_active_static_and_tool_tampering_fail_before_business_call(self) -> None:
        mutations = ("manifest", "static", "tool")
        for mutation in mutations:
            gateway, service = self.make_gateway(trade_enabled=False)
            if mutation == "manifest":
                service.manifest["package_version"] = "9.9.9"
            elif mutation == "static":
                service.static_manifest["boundary_contract_digest"] = "sha256:" + "0" * 64
            else:
                service.tools[0]["inputSchema"]["properties"]["extra"] = {"type": "string"}
            before = len(service.calls)
            with self.subTest(mutation=mutation):
                with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
                    gateway.call_typed_tool(
                        "market_get_symbol_price",
                        {"market": "spot", "symbol": "BTCUSDT"},
                    )
                self.assertEqual(caught.exception.code, "mcp_contract_mismatch")
                self.assertEqual(len(service.calls), before + 2)

    def test_boundary_permission_tampering_with_stale_digest_fails_closed(self) -> None:
        tampered = boundary_contract()
        tampered["permissions"]["account"]["account_get_balance/spot"]["host_policy_capability"] = (
            "account.spot.unreviewed_capability"
        )

        with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
            self.make_gateway(
                trade_enabled=False,
                boundary_contract_loader=lambda: tampered,
            )

        self.assertEqual(caught.exception.code, "mcp_mapping_invalid")

    def test_request_description_returns_fixed_wrapper_and_rejects_origin_drift(self) -> None:
        gateway, _service = self.make_gateway(trade_enabled=False)
        wrapped = self.gateway_module.describe_local_mcp_request(
            name="account_get_balance",
            arguments={"market": "spot"},
            effective_spot_base_url="https://api-spot.weex.com",
            effective_futures_base_url="https://api-contract.weex.com",
            trade_enabled=False,
            timeout_seconds=15.0,
            runtime_loader=lambda _expected: gateway._runtime,
            mapping_loader=lambda: gateway._mapping,
        )
        self.assertEqual(set(wrapped), {"structured_content", "is_error", "transport_meta"})
        self.assertFalse(wrapped["is_error"])
        self.assertIsNone(wrapped["transport_meta"])
        self.assertEqual(
            wrapped["structured_content"]["result"]["request"]["method"],
            "GET",
        )

        runtime = gateway._runtime
        original = runtime.describe_request

        def drifted(**kwargs):
            value = original(**kwargs)
            value["url"] = "https://evil.example/private"
            return value

        runtime.describe_request = drifted
        with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
            self.gateway_module.describe_local_mcp_request(
                name="account_get_balance",
                arguments={"market": "spot"},
                effective_spot_base_url="https://api-spot.weex.com",
                effective_futures_base_url="https://api-contract.weex.com",
                trade_enabled=False,
                timeout_seconds=15.0,
                runtime_loader=lambda _expected: runtime,
                mapping_loader=lambda: gateway._mapping,
            )
        self.assertEqual(caught.exception.code, "mcp_contract_mismatch")

    def test_protocol_errors_and_is_error_mismatch_fail_closed(self) -> None:
        gateway, service = self.make_gateway(trade_enabled=False)

        def bad(request, _context):
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"structuredContent": {"ok": True}, "isError": True},
            }

        service.tool_handler = bad
        with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
            gateway.call_typed_tool(
                "market_get_symbol_price",
                {"market": "spot", "symbol": "BTCUSDT"},
            )
        self.assertEqual(caught.exception.code, "mcp_protocol_error")

        for response in (
            {"jsonrpc": "1.0", "id": "wrong", "result": {}},
            {"jsonrpc": "2.0", "id": "wrong", "result": {}},
            {"jsonrpc": "2.0", "id": "placeholder", "error": {"code": -1}},
        ):

            def protocol_handler(request, _context, raw=response):
                value = copy.deepcopy(raw)
                if value.get("id") == "placeholder":
                    value["id"] = request["id"]
                return value

            service.tool_handler = protocol_handler
            with self.subTest(response=response):
                with self.assertRaises(self.gateway_module.McpGatewayError) as protocol_error:
                    gateway.call_typed_tool(
                        "market_get_symbol_price",
                        {"market": "spot", "symbol": "BTCUSDT"},
                    )
                self.assertEqual(protocol_error.exception.code, "mcp_protocol_error")

    def test_old_new_contract_mismatches_fail_at_preflight(self) -> None:
        tools = tool_catalog(trade_enabled=False)
        manifest = build_manifest(tools, trade_enabled=False)
        static = build_static_manifest(tools)
        service = FakeService(
            tools=tools,
            manifest=manifest,
            static_manifest=static,
            runtime_availability=availability(tools, trade_enabled=False),
        )
        manifests = {False: manifest, True: build_manifest(tool_catalog(trade_enabled=True), trade_enabled=True)}
        statics = {False: static, True: build_static_manifest(tool_catalog(trade_enabled=True))}
        old_mapping = mapping_for(manifests, statics)
        old_mapping["verified_mcp_candidate"]["package_version"] = "0.2.0"
        with self.assertRaises(self.gateway_module.McpGatewayError) as caught:
            self.gateway_module.LocalMcpGateway(
                profile_id=None,
                effective_spot_base_url="https://api-spot.weex.com",
                effective_futures_base_url="https://api-contract.weex.com",
                trade_enabled=False,
                timeout_seconds=15.0,
                runtime_loader=lambda _expected: fake_runtime(service),
                mapping_loader=lambda: old_mapping,
                boundary_contract_loader=boundary_contract,
            )
        self.assertIn(caught.exception.code, {"mcp_mapping_invalid", "mcp_version_mismatch"})

    def test_mapping_and_runtime_loader_failures_use_stable_safe_errors(self) -> None:
        with self.assertRaises(self.gateway_module.McpGatewayError) as mapping_error:
            self.gateway_module.LocalMcpGateway(
                profile_id=None,
                effective_spot_base_url="https://api-spot.weex.com",
                effective_futures_base_url="https://api-contract.weex.com",
                trade_enabled=False,
                timeout_seconds=15.0,
                mapping_loader=lambda: (_ for _ in ()).throw(RuntimeError("AK_LEAK /private/mapping")),
            )
        self.assertEqual(mapping_error.exception.code, "mcp_mapping_invalid")
        self.assertNotIn("AK_LEAK", str(mapping_error.exception))

        tools_by_mode = {flag: tool_catalog(trade_enabled=flag) for flag in (False, True)}
        manifests = {flag: build_manifest(tools_by_mode[flag], trade_enabled=flag) for flag in (False, True)}
        statics = {flag: build_static_manifest(tools_by_mode[flag]) for flag in (False, True)}
        with self.assertRaises(self.gateway_module.McpGatewayError) as runtime_error:
            self.gateway_module.LocalMcpGateway(
                profile_id=None,
                effective_spot_base_url="https://api-spot.weex.com",
                effective_futures_base_url="https://api-contract.weex.com",
                trade_enabled=False,
                timeout_seconds=15.0,
                mapping_loader=lambda: mapping_for(manifests, statics),
                boundary_contract_loader=boundary_contract,
                runtime_loader=lambda _expected: (_ for _ in ()).throw(RuntimeError("SK_LEAK /private/runtime")),
            )
        self.assertEqual(runtime_error.exception.code, "mcp_runtime_incompatible")
        self.assertNotIn("SK_LEAK", str(runtime_error.exception))

    def test_default_runtime_loader_requires_v2_public_api(self) -> None:
        module = self.gateway_module
        with mock.patch.object(
            module.metadata,
            "version",
            side_effect=module.metadata.PackageNotFoundError,
        ):
            with self.assertRaises(module.McpGatewayError) as missing:
                module._load_default_runtime("0.3.4")
        self.assertEqual(missing.exception.code, "mcp_package_missing")

        with mock.patch.object(module.metadata, "version", return_value="0.2.0"):
            with self.assertRaises(module.McpGatewayError) as mismatch:
                module._load_default_runtime("0.3.4")
        self.assertEqual(mismatch.exception.code, "mcp_version_mismatch")

        with (
            mock.patch.object(module.metadata, "version", return_value="0.3.4"),
            mock.patch.object(
                module.importlib,
                "import_module",
            ) as importer,
        ):
            modules = {
                "weex_mcp.local": SimpleNamespace(build_local_user_service=object(), describe_request=object()),
                "weex_mcp.settings": SimpleNamespace(Settings=object()),
                "weex_mcp.invocation_context": SimpleNamespace(
                    VerifiedPrincipal=object(),
                    AccountBinding=object(),
                    CredentialBinding=object(),
                    CorrelationContext=object(),
                    AuthenticatedInvocationContext=object(),
                ),
                "weex_mcp.secret_provider": SimpleNamespace(
                    ResolvedWeexCredential=object(),
                    SecretProviderError=object(),
                ),
                "weex_mcp.execution_authorization": SimpleNamespace(ExecutionGrant=object()),
            }
            importer.side_effect = lambda name: modules[name]
            runtime = module._load_default_runtime("0.3.4")
        self.assertIs(
            runtime.AuthenticatedInvocationContext,
            modules["weex_mcp.invocation_context"].AuthenticatedInvocationContext,
        )
        imported = {call.args[0] for call in importer.call_args_list}
        self.assertNotIn("weex_mcp.tenant_context", imported)

    def test_probe_projects_safe_v2_status(self) -> None:
        gateway, _service = self.make_gateway(trade_enabled=False)
        status = self.gateway_module.probe_local_mcp_runtime(
            runtime_loader=lambda _expected: gateway._runtime,
            mapping_loader=lambda: gateway._mapping,
            boundary_contract_loader=boundary_contract,
        )
        self.assertTrue(status["manifest_ok"])
        self.assertEqual(status["manifest_version"], 2)
        self.assertEqual(status["tool_contract_version"], "2")
        serialized = json.dumps(status, sort_keys=True)
        for forbidden in ("AK_ONE", "SK_ONE", "profile-a", "api-spot"):
            self.assertNotIn(forbidden, serialized)

    def test_probe_sanitizes_unexpected_runtime_loader_failures(self) -> None:
        gateway, _service = self.make_gateway(trade_enabled=False)
        status = self.gateway_module.probe_local_mcp_runtime(
            runtime_loader=lambda _expected: (_ for _ in ()).throw(RuntimeError("SK_LEAK /private/runtime")),
            mapping_loader=lambda: gateway._mapping,
            boundary_contract_loader=boundary_contract,
        )

        self.assertFalse(status["manifest_ok"])
        self.assertEqual(status["action_required"], "repair_mcp_runtime")
        serialized = json.dumps(status, sort_keys=True)
        self.assertNotIn("SK_LEAK", serialized)
        self.assertNotIn("/private/runtime", serialized)


if __name__ == "__main__":
    unittest.main()
