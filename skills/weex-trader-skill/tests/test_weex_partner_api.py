#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "weex_partner_api.py"
DEFINITIONS_PATH = ROOT / "references" / "partner-api-definitions.json"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

EXPECTED_ENDPOINTS = {
    "partner.get-affiliate-uids": (
        "GET",
        "/api/v3/rebate/affiliate/getAffiliateUIDs",
    ),
    "partner.get-channel-user-trade-and-asset": (
        "GET",
        "/api/v3/rebate/affiliate/getChannelUserTradeAndAsset",
    ),
    "partner.get-affiliate-commission": (
        "GET",
        "/api/v3/rebate/affiliate/getAffiliateCommission",
    ),
    "partner.query-sub-channel-transactions": (
        "POST",
        "/api/v3/rebate/affiliate/querySubChannelTransactions",
    ),
    "partner.verify-referrals": (
        "GET",
        "/api/v3/agency/verifyReferrals",
    ),
    "partner.get-referral-assets": (
        "GET",
        "/api/v3/agency/getAssert",
    ),
    "partner.get-referral-deal-data": (
        "GET",
        "/api/v3/agency/getDealData",
    ),
}


def mcp_partner_success(*, settings, endpoint, data=None, status=200):
    return {
        "ok": True,
        "status": status,
        "rate_limit": {"used": {}, "remaining": {}},
        "retry": {"automatic": False},
        "data": {"code": "00000", "data": []} if data is None else data,
        "endpoint": endpoint.key,
        "method": endpoint.method,
        "path": endpoint.path,
        "weight": endpoint.weight,
        "operation_class": endpoint.operation_class,
        "profile": {
            "resolved_profile_id": settings.profile_id,
            "name": settings.profile_name,
        },
        "api_domain": "partner",
        "environment": settings.environment,
        "capability_mode": "read_only_query",
    }


def mcp_partner_error(*, settings, endpoint, category, code, status=None, details=None):
    return {
        "ok": False,
        "status": status,
        "rate_limit": {"used": {}, "remaining": {}},
        "retry": {"automatic": False},
        "error": {
            "category": category,
            "http_status": status,
            "code": code,
            "message": "Partner request failed.",
            "details": {} if details is None else details,
            "recovery_action": "Inspect the Partner failure and retry only when safe.",
        },
        "endpoint": endpoint.key,
        "method": endpoint.method,
        "path": endpoint.path,
        "weight": endpoint.weight,
        "operation_class": endpoint.operation_class,
        "profile": {
            "resolved_profile_id": settings.profile_id,
            "name": settings.profile_name,
        },
        "api_domain": "partner",
        "environment": settings.environment,
        "capability_mode": "read_only_query",
    }

EXPECTED_DOC_SUFFIXES = {
    "partner.get-affiliate-uids": "GetAffiliateUIDs",
    "partner.get-channel-user-trade-and-asset": "GetChannelUserTradeAndAsset",
    "partner.get-affiliate-commission": "GetAffiliateCommission",
    "partner.query-sub-channel-transactions": "QuerySubChannelTransactions",
    "partner.verify-referrals": "VerifyReferrals",
    "partner.get-referral-assets": "GetAffiliateAssets",
    "partner.get-referral-deal-data": "GetAffiliateDealData",
}


def load_partner_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("weex_partner_api", MODULE_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WeexPartnerApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.partner = load_partner_module()

    def require_partner(self):
        self.assertIsNotNone(
            self.partner,
            "Partner REST executor is not implemented yet; expected T1 RED.",
        )
        return self.partner

    def test_registry_contains_exactly_seven_read_operations(self) -> None:
        partner = self.require_partner()
        endpoints = partner.load_endpoint_map()

        self.assertEqual(set(endpoints), set(EXPECTED_ENDPOINTS))
        for key, (method, path) in EXPECTED_ENDPOINTS.items():
            with self.subTest(endpoint=key):
                endpoint = endpoints[key]
                self.assertEqual(endpoint.method, method)
                self.assertEqual(endpoint.path, path)
                self.assertEqual(endpoint.operation_class, "read")
                self.assertTrue(endpoint.requires_auth)

    def test_registry_keeps_read_only_post_and_excludes_internal_withdrawal_write(self) -> None:
        partner = self.require_partner()
        endpoints = partner.load_endpoint_map()

        read_post = endpoints["partner.query-sub-channel-transactions"]
        self.assertEqual(read_post.method, "POST")
        self.assertEqual(read_post.operation_class, "read")
        all_paths = {endpoint.path for endpoint in endpoints.values()}
        self.assertNotIn("/api/v3/rebate/affiliate/getInternalWithdrawalStatus", all_paths)
        self.assertNotIn("/api/v3/rebate/affiliate/internalWithdrawal", all_paths)

    def test_registry_links_each_endpoint_to_its_exact_official_document(self) -> None:
        partner = self.require_partner()
        endpoints = partner.load_endpoint_map()
        prefix = "https://www.weex.com/api-doc/zh-CN/partner/rebate-endpoints/"

        for key, suffix in EXPECTED_DOC_SUFFIXES.items():
            with self.subTest(endpoint=key):
                self.assertEqual(endpoints[key].doc_url, prefix + suffix)

    def test_definition_file_is_the_allowlist_source_of_truth(self) -> None:
        self.assertTrue(
            DEFINITIONS_PATH.exists(),
            "Partner endpoint definitions are not implemented yet; expected T1 RED.",
        )
        payload = json.loads(DEFINITIONS_PATH.read_text(encoding="utf-8"))
        definitions = payload["definitions"]
        self.assertEqual(len(definitions), 7)
        self.assertTrue(all(item["operation_class"] == "read" for item in definitions))

    def test_partner_host_policy_accepts_only_the_exact_official_origin(self) -> None:
        partner = self.require_partner()

        self.assertEqual(
            partner.validate_partner_base_url("https://api-spot.weex.com"),
            "https://api-spot.weex.com",
        )
        rejected = (
            "http://api-spot.weex.com",
            "https://api-contract.weex.com",
            "https://foo.api-spot.weex.com",
            "https://api-spot.weex.com:443",
            "https://api-spot.weex.com/api",
            "https://user@api-spot.weex.com",
            "https://api-spot.weex.com?debug=1",
            "https://api-spot.weex.com#fragment",
        )
        for raw_url in rejected:
            with self.subTest(raw_url=raw_url):
                with self.assertRaises(partner.PartnerPolicyError):
                    partner.validate_partner_base_url(raw_url)

    def test_partner_origin_resolver_allows_production_and_test_subdomains(self) -> None:
        partner = self.require_partner()
        synthetic_test_origin = "https://unit-test.weex.tech"

        self.assertEqual(
            partner.resolve_partner_origin("", {}),
            ("https://api-spot.weex.com", "partner_production"),
        )
        self.assertEqual(
            partner.resolve_partner_origin("https://api-spot.weex.com/", {}),
            ("https://api-spot.weex.com", "partner_production"),
        )
        self.assertEqual(
            partner.resolve_partner_origin(f"{synthetic_test_origin}/", {}),
            (synthetic_test_origin, "partner_test"),
        )
        label_63 = "a" * 63
        self.assertEqual(
            partner.resolve_partner_origin(f"https://{label_63}.weex.tech", {}),
            (f"https://{label_63}.weex.tech", "partner_test"),
        )

    def test_partner_safe_preflight_exposes_environment_without_origin_or_key_hint(self) -> None:
        partner = self.require_partner()
        self.assertTrue(
            hasattr(partner, "build_partner_preflight_envelope"),
            "Partner-safe preflight projection is not implemented yet; expected T15 RED.",
        )
        synthetic_test_origin = "https://partner-safe-fixture.weex.tech"
        records = {
            "init": {
                "profiles": {
                    "count": 2,
                    "default_profile_id": None,
                    "default_profile_name": None,
                    "summary": [
                        {
                            "id": "profile-a",
                            "name": "partner-main",
                            "description": "must-stay-hidden",
                            "contract_base_url": "https://contract-hidden.weex.tech",
                            "spot_base_url": synthetic_test_origin,
                            "api_key_hint": "***secret-hint",
                        },
                        {
                            "id": "profile-b",
                            "name": "unrelated-profile",
                            "spot_base_url": "https://api-spot.weex.com",
                            "api_key_hint": "***other-hint",
                        },
                    ],
                },
            },
            "runtime": {
                "host": {"requirements_ready": True, "missing_modules": []},
                "env_validation": {"ok": True, "issues": []},
                "vault": {
                    "configured": True,
                    "state": "unlocked",
                    "action_required": None,
                },
            },
        }

        result = partner.build_partner_preflight_envelope(
            records,
            profile_ref="partner-main",
            environ={},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["environment"], "partner_test")
        self.assertEqual(
            result["profile"],
            {"resolved_profile_id": "profile-a", "name": "partner-main"},
        )
        serialized = json.dumps(result, ensure_ascii=False)
        for hidden_value in (
            synthetic_test_origin,
            "contract-hidden.weex.tech",
            "secret-hint",
            "unrelated-profile",
            "must-stay-hidden",
        ):
            self.assertNotIn(hidden_value, serialized)

    def test_partner_executor_cli_exposes_safe_preflight_subcommand(self) -> None:
        partner = self.require_partner()
        help_text = partner.build_parser().format_help()

        self.assertIn("preflight", help_text)

    def test_partner_preflight_cli_disables_default_profile_credential_probe(self) -> None:
        partner = self.require_partner()
        records = {
            "init": {
                "profiles": {
                    "summary": [{
                        "id": "profile-a",
                        "name": "partner-main",
                        "spot_base_url": "https://api-spot.weex.com",
                    }],
                },
            },
            "runtime": {
                "host": {"requirements_ready": True, "missing_modules": []},
                "env_validation": {"ok": True, "issues": []},
                "vault": {"configured": True, "state": "unlocked", "action_required": None},
            },
        }
        argv = [
            "weex_partner_api.py",
            "preflight",
            "--profile",
            "partner-main",
            "--language",
            "zh",
        ]

        with mock.patch.object(sys, "argv", argv):
            with mock.patch.object(partner, "refresh_agent_records", return_value=records) as refresh_mock:
                with mock.patch.object(partner, "_output"):
                    exit_code = partner.main()

        self.assertEqual(exit_code, 0)
        refresh_mock.assert_called_once_with(
            preferred_language="zh",
            command="partner.preflight",
            probe_default_profile_usable=False,
        )

    def test_partner_safe_preflight_fails_before_profile_projection_when_runtime_is_not_ready(self) -> None:
        partner = self.require_partner()
        self.assertTrue(hasattr(partner, "build_partner_preflight_envelope"))
        synthetic_test_origin = "https://partner-safe-fixture.weex.tech"
        records = {
            "init": {
                "profiles": {
                    "summary": [{
                        "id": "profile-a",
                        "name": "partner-main",
                        "spot_base_url": synthetic_test_origin,
                        "api_key_hint": "***secret-hint",
                    }],
                },
            },
            "runtime": {
                "host": {"requirements_ready": False, "missing_modules": ["cryptography"]},
                "env_validation": {"ok": True, "issues": []},
                "vault": {"configured": True, "state": "unlocked", "action_required": None},
            },
        }

        result = partner.build_partner_preflight_envelope(
            records,
            profile_ref="partner-main",
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertIsNone(result["environment"])
        self.assertEqual(result["error"]["category"], "runtime_preflight")
        self.assertEqual(result["profile"], {"requested": "partner-main"})
        self.assertNotIn(synthetic_test_origin, json.dumps(result))

    def test_partner_safe_preflight_reports_missing_profile_without_listing_other_profiles(self) -> None:
        partner = self.require_partner()
        self.assertTrue(hasattr(partner, "build_partner_preflight_envelope"))
        records = {
            "init": {
                "profiles": {
                    "summary": [{
                        "id": "profile-b",
                        "name": "unrelated-profile",
                        "spot_base_url": "https://api-spot.weex.com",
                    }],
                },
            },
            "runtime": {
                "host": {"requirements_ready": True, "missing_modules": []},
                "env_validation": {"ok": True, "issues": []},
                "vault": {"configured": True, "state": "unlocked", "action_required": None},
            },
        }

        result = partner.build_partner_preflight_envelope(
            records,
            profile_ref="missing-profile",
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "profile_vault")
        self.assertEqual(result["profile"], {"requested": "missing-profile"})
        self.assertNotIn("unrelated-profile", json.dumps(result))

    def test_partner_safe_preflight_invalid_origin_and_locked_vault_do_not_leak_metadata(self) -> None:
        partner = self.require_partner()
        base_runtime = {
            "host": {"requirements_ready": True, "missing_modules": []},
            "env_validation": {"ok": True, "issues": []},
            "vault": {"configured": True, "state": "unlocked", "action_required": None},
        }
        hidden_hint = "***hidden-hint"
        invalid_origin = "https://partner-safe-fixture.weex.tech/hidden-path"
        invalid_records = {
            "init": {
                "profiles": {
                    "summary": [{
                        "id": "profile-a",
                        "name": "partner-main",
                        "description": "hidden-description",
                        "spot_base_url": invalid_origin,
                        "api_key_hint": hidden_hint,
                    }],
                },
            },
            "runtime": base_runtime,
        }

        invalid_result = partner.build_partner_preflight_envelope(
            invalid_records,
            profile_ref="partner-main",
            environ={},
        )

        self.assertFalse(invalid_result["ok"])
        self.assertFalse(invalid_result["request_sent"])
        self.assertEqual(invalid_result["error"]["code"], "invalid_partner_origin")
        invalid_serialized = json.dumps(invalid_result, ensure_ascii=False)
        self.assertNotIn(invalid_origin, invalid_serialized)
        self.assertNotIn(hidden_hint, invalid_serialized)
        self.assertNotIn("hidden-description", invalid_serialized)

        locked_records = {
            "init": {
                "profiles": {
                    "summary": [{
                        "id": "profile-a",
                        "name": "partner-main",
                        "spot_base_url": "https://api-spot.weex.com",
                        "api_key_hint": hidden_hint,
                    }],
                },
            },
            "runtime": dict(
                base_runtime,
                vault={"configured": True, "state": "locked", "action_required": "unlock"},
            ),
        }

        locked_result = partner.build_partner_preflight_envelope(
            locked_records,
            profile_ref="partner-main",
            environ={},
        )

        self.assertFalse(locked_result["ok"])
        self.assertFalse(locked_result["request_sent"])
        self.assertEqual(locked_result["environment"], "partner_production")
        self.assertEqual(locked_result["error"]["code"], "vault_unavailable")
        self.assertNotIn(hidden_hint, json.dumps(locked_result, ensure_ascii=False))

    def test_partner_origin_resolver_rejects_malformed_or_untrusted_origins(self) -> None:
        partner = self.require_partner()
        invalid_origins = (
            "http://unit-test.weex.tech",
            "https://weex.tech",
            "https://unit-test.weex.tech.evil.example",
            "https://UNIT-TEST.weex.tech",
            "https://unit-test.weex.tech.",
            "https://double..label.weex.tech",
            "https://under_score.weex.tech",
            "https://-leading.weex.tech",
            "https://trailing-.weex.tech",
            f"https://{'a' * 64}.weex.tech",
            "https://user@unit-test.weex.tech",
            "https://unit-test.weex.tech:443",
            "https://unit-test.weex.tech:notaport",
            "https://unit-test.weex.tech/api",
            "https://unit-test.weex.tech?debug=1",
            "https://unit-test.weex.tech#fragment",
            "https://unit-test\\.weex.tech",
            "https://unit-test。weex.tech",
            "https://tést.weex.tech",
            "https://unit-test.weex.tech\n.evil.example",
        )

        for raw_url in invalid_origins:
            with self.subTest(raw_url=raw_url):
                with self.assertRaises(partner.PartnerPolicyError) as exc_info:
                    partner.resolve_partner_origin(raw_url, {})
                self.assertNotIn(raw_url, str(exc_info.exception))

        for env_name in partner.PARTNER_OVERRIDE_ENV_VARS:
            with self.subTest(env_name=env_name):
                with self.assertRaises(partner.PartnerPolicyError):
                    partner.resolve_partner_origin(
                        "https://unit-test.weex.tech",
                        {env_name: "https://override.example"},
                    )

    def test_invalid_test_origin_fails_before_mcp_execution(self) -> None:
        partner = self.require_partner()
        events = []
        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001"},
                "body": {},
            },
            profile_resolver=lambda _ref: SimpleNamespace(
                profile_id="stable-id",
                name="main",
                spot_base_url="https://unit-test.weex.tech.evil.example",
            ),
            preflight=lambda **_kwargs: None,
            mcp_executor=lambda **_kwargs: events.append("mcp"),
            timeout=1.0,
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "local_policy")
        self.assertIsNone(result["environment"])
        self.assertEqual(events, [])

    def test_test_environment_transport_error_does_not_expose_selected_origin(self) -> None:
        partner = self.require_partner()
        synthetic_test_origin = "https://unit-test.weex.tech"

        def mcp_executor(*, settings, endpoint, **_kwargs):
            return mcp_partner_error(
                settings=settings,
                endpoint=endpoint,
                category="transport",
                code="partner_transport_error",
                details={"reason": "URLError", "origin": synthetic_test_origin},
            )

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001"},
                "body": {},
            },
            profile_resolver=lambda _ref: SimpleNamespace(
                profile_id="stable-id",
                name="main",
                spot_base_url=synthetic_test_origin,
            ),
            preflight=lambda **_kwargs: None,
            mcp_executor=mcp_executor,
            timeout=1.0,
            environ={},
        )
        rendered = json.dumps(result, ensure_ascii=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["environment"], "partner_test")
        self.assertEqual(result["error"]["category"], "transport")
        self.assertNotIn("unit-test.weex.tech", rendered.lower())

    def test_test_environment_redacts_partially_percent_encoded_hostname(self) -> None:
        partner = self.require_partner()
        synthetic_test_origin = "https://unit-test.weex.tech"
        encoded_variants = (
            "unit-test%2Eweex%2Etech",
            "%75nit-test.weex.tech",
            "https%3A%2F%2Funit-test%2Eweex%2Etech",
            r"https:\/\/unit-test.weex.tech",
            r"https:%5Cu002f%5Cu002funit-test%5Cu002eweex%5Cu002etech",
        )
        result = partner.sanitize_partner_result(
            {
                "ok": False,
                "error": {
                    variant: f"failed for {variant}"
                    for variant in encoded_variants
                },
            },
            origin=synthetic_test_origin,
            environment="partner_test",
        )
        rendered = json.dumps(result, ensure_ascii=False).lower()

        for variant in encoded_variants:
            with self.subTest(variant=variant):
                self.assertNotIn(variant.lower(), rendered)

    def test_unknown_or_write_endpoint_is_rejected_before_profile_or_mcp(self) -> None:
        partner = self.require_partner()

        with self.assertRaises(partner.PartnerPolicyError):
            partner.execute_partner_request(
                {
                    "endpoint": "partner.internal-withdrawal",
                    "profile": "main",
                    "query": {},
                    "body": {"coin": "USDT"},
                },
                profile_resolver=lambda _ref: self.fail("profile must not be resolved"),
                preflight=lambda **_kwargs: None,
                mcp_executor=lambda **_kwargs: self.fail("MCP must not be called"),
            )

    def test_output_sanitizer_redacts_credentials_signatures_and_secret_values(self) -> None:
        partner = self.require_partner()
        payload = {
            "headers": {
                "ACCESS-KEY": "test-key",
                "ACCESS-PASSPHRASE": "test-passphrase",
                "ACCESS-SIGN": "test-signature",
            },
            "error": {
                "message": "request failed for test-key using test-secret/test-passphrase",
            },
        }
        sanitized = partner.sanitize_for_output(
            payload,
            secret_values=("test-key", "test-secret", "test-passphrase", "test-signature"),
        )
        rendered = json.dumps(sanitized, ensure_ascii=False)

        for secret in ("test-key", "test-secret", "test-passphrase", "test-signature"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(sanitized["headers"]["ACCESS-KEY"], "***")

    def test_execute_runs_preflight_before_profile_credentials_and_transport(self) -> None:
        partner = self.require_partner()
        events = []

        class FakeResponse:
            status = 200
            headers = {"X-REMAINING-WEIGHT-1M": "1190"}

            def read(self):
                return b'{"code":"00000","data":[]}'

        def preflight(**_kwargs):
            events.append("preflight")

        def profile_resolver(profile_ref):
            events.append(f"profile:{profile_ref}")
            return SimpleNamespace(profile_id="stable-id", name="main", spot_base_url="")

        def mcp_executor(*, settings, endpoint, query, body):
            events.append(f"mcp:{endpoint.key}")
            self.assertEqual(query, {"userIds": "10001,10002"})
            self.assertEqual(body, {})
            return mcp_partner_success(settings=settings, endpoint=endpoint)

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001,10002"},
                "body": {},
            },
            profile_resolver=profile_resolver,
            preflight=preflight,
            mcp_executor=mcp_executor,
            environ={},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            events,
            ["preflight", "profile:main", "mcp:partner.verify-referrals"],
        )
        self.assertEqual(result["profile"]["resolved_profile_id"], "stable-id")

    def test_execute_uses_30_second_partner_default_timeout(self) -> None:
        partner = self.require_partner()
        observed_timeouts = []

        def mcp_executor(*, settings, endpoint, **_kwargs):
            observed_timeouts.append(settings.timeout_seconds)
            return mcp_partner_success(settings=settings, endpoint=endpoint)

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001"},
                "body": {},
            },
            profile_resolver=lambda _ref: SimpleNamespace(
                profile_id="stable-id",
                name="main",
                spot_base_url="",
            ),
            preflight=lambda **_kwargs: None,
            mcp_executor=mcp_executor,
            environ={},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(observed_timeouts, [30.0])

    def test_execute_keeps_valid_api_timeout_override(self) -> None:
        partner = self.require_partner()
        observed_timeouts = []

        def mcp_executor(*, settings, endpoint, **_kwargs):
            observed_timeouts.append(settings.timeout_seconds)
            return mcp_partner_success(settings=settings, endpoint=endpoint)

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001"},
                "body": {},
            },
            profile_resolver=lambda _ref: SimpleNamespace(
                profile_id="stable-id",
                name="main",
                spot_base_url="",
            ),
            preflight=lambda **_kwargs: None,
            mcp_executor=mcp_executor,
            environ={"WEEX_API_TIMEOUT": "12.5"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(observed_timeouts, [12.5])

    def test_non_json_http_error_is_a_schema_error(self) -> None:
        partner = self.require_partner()

        def mcp_executor(*, settings, endpoint, **_kwargs):
            return mcp_partner_error(
                settings=settings,
                endpoint=endpoint,
                category="schema",
                code="partner_response_schema_error",
                status=500,
                details={"raw_type": "non_json"},
            )

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001"},
                "body": {},
            },
            profile_resolver=lambda _ref: SimpleNamespace(
                profile_id="stable-id",
                name="main",
                spot_base_url="",
            ),
            preflight=lambda **_kwargs: None,
            mcp_executor=mcp_executor,
            timeout=1.0,
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "schema")
        self.assertEqual(result["error"]["details"]["raw_type"], "non_json")

    def test_invalid_timeout_returns_local_configuration_envelope(self) -> None:
        partner = self.require_partner()
        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001"},
                "body": {},
            },
            preflight=lambda **_kwargs: self.fail("preflight must not run"),
            environ={"WEEX_API_TIMEOUT": "not-a-number"},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "local_configuration")
        self.assertEqual(result["error"]["code"], "invalid_api_timeout")
        self.assertIn("recovery_action", result["error"])
        self.assertIsNone(result["environment"])

    def test_preflight_failure_returns_runtime_envelope_before_profile_access(self) -> None:
        partner = self.require_partner()

        def preflight(**_kwargs):
            raise RuntimeError("managed runtime is unavailable")

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001"},
                "body": {},
            },
            preflight=preflight,
            profile_resolver=lambda _ref: self.fail("profile must not be resolved"),
            timeout=1.0,
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "runtime_preflight")
        self.assertEqual(result["error"]["code"], "private_runtime_not_ready")
        self.assertIn("recovery_action", result["error"])
        self.assertIsNone(result["environment"])

    def test_profile_failure_returns_profile_vault_envelope(self) -> None:
        partner = self.require_partner()

        def profile_resolver(_ref):
            raise RuntimeError("profile metadata unavailable")

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "missing",
                "query": {"userIds": "10001"},
                "body": {},
            },
            preflight=lambda **_kwargs: None,
            profile_resolver=profile_resolver,
            timeout=1.0,
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "profile_vault")
        self.assertEqual(result["error"]["code"], "profile_unavailable")
        self.assertIn("recovery_action", result["error"])

    def test_incomplete_vault_credentials_return_profile_vault_envelope(self) -> None:
        partner = self.require_partner()

        def mcp_executor(*, settings, endpoint, **_kwargs):
            return mcp_partner_error(
                settings=settings,
                endpoint=endpoint,
                category="profile_vault",
                code="vault_credentials_unavailable",
            )

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001"},
                "body": {},
            },
            preflight=lambda **_kwargs: None,
            profile_resolver=lambda _ref: SimpleNamespace(
                profile_id="stable-id",
                name="main",
                spot_base_url="",
            ),
            mcp_executor=mcp_executor,
            timeout=1.0,
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "profile_vault")
        self.assertEqual(result["error"]["code"], "vault_credentials_unavailable")

    def test_redirect_error_is_returned_without_retrying_an_authenticated_request(self) -> None:
        partner = self.require_partner()
        attempts = 0

        def mcp_executor(*, settings, endpoint, **_kwargs):
            nonlocal attempts
            attempts += 1
            return mcp_partner_error(
                settings=settings,
                endpoint=endpoint,
                category="redirect",
                code="partner_redirect_forbidden",
                status=302,
            )

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001"},
                "body": {},
            },
            profile_resolver=lambda _ref: SimpleNamespace(
                profile_id="stable-id",
                name="main",
                spot_base_url="",
            ),
            preflight=lambda **_kwargs: None,
            mcp_executor=mcp_executor,
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 302)
        self.assertEqual(attempts, 1)
        self.assertFalse(result["retry"]["automatic"])

    def test_expected_test_environment_mismatch_stops_before_mcp_execution(self) -> None:
        partner = self.require_partner()
        result = partner.execute_partner_request(
            {
                "endpoint": "partner.query-sub-channel-transactions",
                "profile": "main",
                "expected_environment": "partner_test",
                "query": {},
                "body": {
                    "startTime": None,
                    "endTime": None,
                    "productType": "FUTURES",
                    "pageNum": 1,
                    "pageSize": 100,
                },
            },
            profile_resolver=lambda _ref: SimpleNamespace(
                profile_id="stable-id",
                name="main",
                spot_base_url="",
            ),
            preflight=lambda **_kwargs: None,
            mcp_executor=lambda **_kwargs: self.fail("MCP must not be called"),
            timeout=1.0,
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "local_policy")
        self.assertEqual(result["error"]["code"], "expected_partner_environment_mismatch")
        self.assertEqual(result["environment"], "partner_production")

    def test_invalid_expected_environment_stops_before_profile_resolution(self) -> None:
        partner = self.require_partner()
        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "expected_environment": "unknown",
                "query": {"userIds": "10001"},
                "body": {},
            },
            profile_resolver=lambda _ref: self.fail("profile must not be resolved"),
            preflight=lambda **_kwargs: None,
            timeout=1.0,
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "local_policy")
        self.assertEqual(result["error"]["code"], "invalid_expected_environment")
        self.assertIsNone(result["environment"])

    def test_non_scalar_expected_environment_is_a_stable_local_policy_error(self) -> None:
        partner = self.require_partner()
        for value in ([], {}):
            with self.subTest(value=value):
                result = partner.execute_partner_request(
                    {
                        "endpoint": "partner.verify-referrals",
                        "profile": "main",
                        "expected_environment": value,
                        "query": {"userIds": "10001"},
                        "body": {},
                    },
                    profile_resolver=lambda _ref: self.fail("profile must not be resolved"),
                    preflight=lambda **_kwargs: None,
                    timeout=1.0,
                    environ={},
                )

                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["category"], "local_policy")
                self.assertEqual(result["error"]["code"], "invalid_expected_environment")

    def test_falsey_non_object_query_and_body_fail_before_preflight_or_profile(self) -> None:
        partner = self.require_partner()
        base = {
            "endpoint": "partner.verify-referrals",
            "profile": "main",
            "query": {"userIds": "10001"},
            "body": {},
        }
        for field in ("query", "body"):
            for value in ([], "", False, 0):
                with self.subTest(field=field, value=value):
                    result = partner.execute_partner_request(
                        dict(base, **{field: value}),
                        preflight=lambda **_kwargs: self.fail("preflight must not run"),
                        profile_resolver=lambda _ref: self.fail("profile must not be resolved"),
                        timeout=1.0,
                        environ={},
                    )
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"]["category"], "local_policy")
                    self.assertEqual(result["error"]["code"], "invalid_request_container")

if __name__ == "__main__":
    unittest.main()
