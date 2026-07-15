#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib import error


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
    "partner.get-internal-withdrawal-status": (
        "GET",
        "/api/v3/rebate/affiliate/getInternalWithdrawalStatus",
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

EXPECTED_DOC_SUFFIXES = {
    "partner.get-affiliate-uids": "GetAffiliateUIDs",
    "partner.get-channel-user-trade-and-asset": "GetChannelUserTradeAndAsset",
    "partner.get-affiliate-commission": "GetAffiliateCommission",
    "partner.get-internal-withdrawal-status": "GetInternalWithdrawalStatus",
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

    def test_registry_contains_exactly_eight_read_operations(self) -> None:
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
        self.assertEqual(len(definitions), 8)
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

    def test_signature_uses_the_same_doseq_query_string_as_the_url(self) -> None:
        partner = self.require_partner()
        query = {
            "userIds": ["10001", "10002"],
            "startTime": "2026-07-01",
        }
        query_string = partner.encode_query(query)
        self.assertEqual(
            query_string,
            "userIds=10001&userIds=10002&startTime=2026-07-01",
        )

        message = (
            "1784073600000GET/api/v3/agency/getDealData?"
            "userIds=10001&userIds=10002&startTime=2026-07-01"
        )
        expected = base64.b64encode(
            hmac.new(b"test-secret", message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        actual = partner.sign_request(
            secret="test-secret",
            timestamp_ms="1784073600000",
            method="GET",
            path="/api/v3/agency/getDealData",
            query_string=query_string,
            body_string="",
        )
        self.assertEqual(actual, expected)

    def test_prepare_request_preserves_read_only_post_body(self) -> None:
        partner = self.require_partner()
        endpoint = partner.load_endpoint_map()["partner.query-sub-channel-transactions"]
        prepared = partner.prepare_signed_request(
            endpoint=endpoint,
            api_key="test-key",
            api_secret="test-secret",
            api_passphrase="test-passphrase",
            timestamp_ms="1784073600000",
            query={},
            body={
                "productType": "FUTURES",
                "pageNum": 1,
                "pageSize": 100,
            },
        )

        self.assertEqual(prepared["method"], "POST")
        self.assertEqual(
            prepared["url"],
            "https://api-spot.weex.com/api/v3/rebate/affiliate/querySubChannelTransactions",
        )
        self.assertEqual(
            json.loads(prepared["data"].decode("utf-8"))["productType"],
            "FUTURES",
        )
        self.assertIn("ACCESS-SIGN", prepared["headers"])
        self.assertNotIn("confirm", prepared)

    def test_unknown_or_write_endpoint_is_rejected_before_credentials_are_loaded(self) -> None:
        partner = self.require_partner()
        credential_loader_called = False

        def credential_loader(_profile: str):
            nonlocal credential_loader_called
            credential_loader_called = True
            raise AssertionError("credentials must not be loaded")

        with self.assertRaises(partner.PartnerPolicyError):
            partner.execute_partner_request(
                {
                    "endpoint": "partner.internal-withdrawal",
                    "profile": "main",
                    "query": {},
                    "body": {"coin": "USDT"},
                },
                credential_loader=credential_loader,
                preflight=lambda **_kwargs: None,
                opener=lambda *_args, **_kwargs: None,
            )
        self.assertFalse(credential_loader_called)

    def test_http_429_is_classified_without_retry_and_keeps_rate_limit_headers(self) -> None:
        partner = self.require_partner()
        result = partner.normalize_http_result(
            status=429,
            headers={
                "X-USED-WEIGHT-1M": "1200",
                "X-REMAINING-WEIGHT-1M": "0",
            },
            payload={"code": "-1003", "msg": "Too many requests"},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "rate_limit")
        self.assertEqual(result["rate_limit"]["used"]["1M"], 1200)
        self.assertEqual(result["rate_limit"]["remaining"]["1M"], 0)
        self.assertFalse(result["retry"]["automatic"])

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

        def credential_loader(profile_name):
            events.append(f"credentials:{profile_name}")
            return SimpleNamespace(
                api_key="test-key",
                api_secret="test-secret",
                api_passphrase="test-passphrase",
            )

        def opener(prepared, _timeout):
            events.append(f"open:{prepared['url']}")
            return FakeResponse()

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001,10002"},
                "body": {},
            },
            credential_loader=credential_loader,
            profile_resolver=profile_resolver,
            preflight=preflight,
            opener=opener,
            environ={},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(events[:3], ["preflight", "profile:main", "credentials:main"])
        self.assertTrue(events[3].startswith("open:https://api-spot.weex.com/"))
        self.assertEqual(result["profile"]["resolved_profile_id"], "stable-id")

    def test_http_200_business_error_is_not_treated_as_success(self) -> None:
        partner = self.require_partner()
        result = partner.normalize_http_result(
            status=200,
            headers={},
            payload={"code": "-1050", "msg": "Partner permission denied"},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "permission")
        self.assertEqual(result["error"]["code"], "-1050")

    def test_non_json_success_response_is_a_schema_error(self) -> None:
        partner = self.require_partner()
        result = partner.normalize_http_result(
            status=200,
            headers={},
            payload={
                "code": None,
                "message": "Partner API returned non-JSON data",
                "raw_type": "non_json",
            },
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "schema")

    def test_redirect_error_is_returned_without_retrying_an_authenticated_request(self) -> None:
        partner = self.require_partner()
        attempts = 0

        def opener(prepared, _timeout):
            nonlocal attempts
            attempts += 1
            raise error.HTTPError(
                prepared["url"],
                302,
                "Found",
                {},
                None,
            )

        result = partner.execute_partner_request(
            {
                "endpoint": "partner.verify-referrals",
                "profile": "main",
                "query": {"userIds": "10001"},
                "body": {},
            },
            credential_loader=lambda _name: SimpleNamespace(
                api_key="test-key",
                api_secret="test-secret",
                api_passphrase="test-passphrase",
            ),
            profile_resolver=lambda _ref: SimpleNamespace(
                profile_id="stable-id",
                name="main",
                spot_base_url="",
            ),
            preflight=lambda **_kwargs: None,
            opener=opener,
            environ={},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 302)
        self.assertEqual(attempts, 1)
        self.assertFalse(result["retry"]["automatic"])


if __name__ == "__main__":
    unittest.main()
