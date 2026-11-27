#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MAPPING_PATH = ROOT / "references" / "mcp-capability-mapping.json"
MODULE_PATH = SCRIPTS / "weex_mcp_partner_compat.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_mapping() -> dict[str, object]:
    return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))


def mapping_entry(mapping: dict[str, object], key: str) -> dict[str, object]:
    matches = [item for item in mapping["capabilities"] if item["legacy_key"] == key]
    if len(matches) != 1:
        raise AssertionError(f"expected one mapping for {key}, got {len(matches)}")
    return matches[0]


def load_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("weex_mcp_partner_compat", MODULE_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def endpoint_from_mapping(entry: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        key=entry["legacy_key"],
        method=entry["method"],
        path=entry["path"],
        operation_class="read",
        request_transport=entry["request_transport"],
        weight=10,
    )


class FakeGateway:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        transport_meta: dict[str, object] | None = None,
        **kwargs: object,
    ):
        self.payload = payload
        self.transport_meta = transport_meta
        self.init_kwargs = kwargs
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_typed_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, copy.deepcopy(arguments)))
        ok = self.payload.get("ok")
        return {
            "structured_content": copy.deepcopy(self.payload),
            "is_error": isinstance(ok, bool) and not ok,
            "transport_meta": copy.deepcopy(self.transport_meta),
        }


class PartnerMappingTests(unittest.TestCase):
    def test_seven_partner_mappings_and_user_id_transforms_are_frozen(self) -> None:
        import weex_partner_api as partner_api

        mapping = load_mapping()
        partner = sorted(
            (item for item in mapping["capabilities"] if item["product"] == "partner"),
            key=lambda item: item["legacy_key"],
        )
        self.assertEqual(len(partner), 7)
        verify = mapping_entry(mapping, "partner.verify-referrals")
        deal = mapping_entry(mapping, "partner.get-referral-deal-data")
        self.assertEqual(
            verify["field_decisions"]["userIds"]["value_transform"],
            "comma_or_array_unique_string_list_v1",
        )
        self.assertEqual(
            deal["field_decisions"]["userIds"]["value_transform"],
            "comma_or_array_string_list_v1",
        )
        matrix = [
            {
                "legacy_key": item["legacy_key"],
                "method": item["method"],
                "path": item["path"],
                "weight": item["weight"],
                "request_transport": item["request_transport"],
                "catalog_request_fields": item["catalog_request_fields"],
                "field_decisions": item["field_decisions"],
                "tool": item["target"]["tool"],
                "legacy_container": item["response"]["legacy_container"],
            }
            for item in partner
        ]
        for item in partner:
            self.assertEqual(
                item["weight"],
                partner_api.ENDPOINTS[item["legacy_key"]].weight,
            )
        digest = hashlib.sha256(
            json.dumps(matrix, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(
            digest,
            "9d5097561b53a1c87614f047bd4e449eb967a88ff7da41ff0b9b10c0548fbdcc",
        )
        self.assertEqual(
            mapping["compatibility_gates"]["t7_partner_read"],
            {
                "status": "golden_verified",
                "operations": 7,
                "operation_matrix_sha256": digest,
                "test": "tests/test_weex_mcp_partner_compat.py",
                "direct_rest_fallback": False,
                "automatic_retry": False,
            },
        )


class McpPartnerCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.partner = load_module()
        self.assertIsNotNone(
            self.partner,
            "weex_mcp_partner_compat.py is missing; expected T7 RED.",
        )
        self.mapping = load_mapping()

    def settings(self, *, environment: str = "partner_production"):
        return self.partner.LocalMcpPartnerSettings(
            profile_id="profile-id",
            profile_name="main",
            origin=(
                "https://api-spot.weex.com"
                if environment == "partner_production"
                else "https://stg-partner.weex.tech"
            ),
            environment=environment,
            timeout_seconds=12.5,
            locale="zh-CN",
        )

    def facade(self, gateway: FakeGateway, *, environment: str = "partner_production"):
        return self.partner.McpPartnerFacade(
            settings=self.settings(environment=environment),
            gateway_factory=lambda **kwargs: gateway,
            mapping_loader=lambda: copy.deepcopy(self.mapping),
        )

    def test_verify_referrals_maps_comma_ids_and_restores_legacy_success_envelope(self) -> None:
        entry = mapping_entry(self.mapping, "partner.verify-referrals")
        gateway = FakeGateway(
            {
                "ok": True,
                "data": {
                    "endpoint": entry["legacy_key"],
                    "method": entry["method"],
                    "path": entry["path"],
                    "weight": 10,
                    "http_status": 200,
                    "rate_limit": {"used": {"1M": 10}, "remaining": {"1M": 90}},
                    "retry": {"automatic": False},
                    "environment": "partner_production",
                    "capability_mode": "read_only_query",
                    "data": {"rows": [{"uid": "10001"}], "page": 1, "pages": 1},
                },
            },
            transport_meta={
                "version": 1,
                "request_id": "partner-success",
                "attempt_count": 1,
                "http_status": 201,
                "rate_limit": {"used": {"1M": 11}, "remaining": {"1M": 89}},
            },
        )
        result = self.facade(gateway).execute(
            endpoint=endpoint_from_mapping(entry),
            query={"userIds": "10001,10002"},
            body={},
        )

        self.assertEqual(
            gateway.calls,
            [
                (
                    "partner_verify_referrals",
                    {
                        "user_ids": ["10001", "10002"],
                    },
                )
            ],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 201)
        self.assertEqual(result["profile"]["resolved_profile_id"], "profile-id")
        self.assertEqual(result["data"]["page"], 1)
        self.assertEqual(result["rate_limit"]["remaining"]["1M"], 89)

    def test_numeric_and_test_null_time_mapping_is_single_call(self) -> None:
        entry = mapping_entry(self.mapping, "partner.query-sub-channel-transactions")
        gateway = FakeGateway(
            {
                "ok": True,
                "data": {
                    "endpoint": entry["legacy_key"],
                    "method": entry["method"],
                    "path": entry["path"],
                    "weight": 10,
                    "http_status": 200,
                    "rate_limit": {"used": {}, "remaining": {}},
                    "retry": {"automatic": False},
                    "environment": "partner_test",
                    "capability_mode": "read_only_query",
                    "data": {"items": []},
                },
            }
        )
        result = self.facade(gateway, environment="partner_test").execute(
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={
                "subUid": 7,
                "startTime": None,
                "endTime": None,
                "productType": "SPOT",
                "pageNum": 1,
                "pageSize": 20,
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            gateway.calls[0][1],
            {
                "sub_uid": "7",
                "start_time": None,
                "end_time": None,
                "product_type": "SPOT",
                "page_num": "1",
                "page_size": "20",
            },
        )
        self.assertNotIn("expected_environment", gateway.calls[0][1])

    def test_typed_rate_limit_error_restores_partner_error_contract(self) -> None:
        entry = mapping_entry(self.mapping, "partner.get-affiliate-uids")
        gateway = FakeGateway(
            {
                "ok": False,
                "error": {
                    "code": "partner_rate_limit",
                    "message": "WEEX Partner rate limit was reached.",
                    "details": {
                        "category": "rate_limit",
                        "http_status": 429,
                        "upstream_code": "-1054",
                        "rate_limit": {
                            "used": {"1M": 1200},
                            "remaining": {"1M": 0},
                        },
                        "retry_automatic": False,
                    },
                },
            },
            transport_meta={
                "version": 1,
                "request_id": "partner-error",
                "attempt_count": 1,
                "http_status": 429,
                "rate_limit": {"used": {"1M": 101}, "remaining": {"1M": 0}},
            },
        )
        result = self.facade(gateway).execute(
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 429)
        self.assertEqual(result["error"]["category"], "rate_limit")
        self.assertEqual(result["error"]["code"], "-1054")
        self.assertEqual(result["rate_limit"]["used"]["1M"], 101)
        self.assertFalse(result["retry"]["automatic"])
        self.assertEqual(result["environment"], "partner_production")

    def test_settings_reject_non_partner_origin(self) -> None:
        with self.assertRaises(self.partner.McpPartnerCompatibilityError):
            self.partner.LocalMcpPartnerSettings(
                profile_id="profile-id",
                profile_name="main",
                origin="https://ordinary.weex.com",
                environment="partner_production",
                timeout_seconds=30,
                locale="en-US",
            )

    def test_invalid_user_id_encoding_is_validation_error_before_gateway(self) -> None:
        entry = mapping_entry(self.mapping, "partner.verify-referrals")
        gateway = FakeGateway({"ok": True})
        result = self.facade(gateway).execute(
            endpoint=endpoint_from_mapping(entry),
            query={"userIds": "10001,10001"},
            body={},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "validation")
        self.assertEqual(gateway.calls, [])

    def test_success_metadata_mismatch_is_schema_error(self) -> None:
        entry = mapping_entry(self.mapping, "partner.verify-referrals")
        gateway = FakeGateway(
            {
                "ok": True,
                "data": {
                    "endpoint": entry["legacy_key"],
                    "method": entry["method"],
                    "path": entry["path"],
                    "weight": 999,
                    "http_status": 200,
                    "rate_limit": {"used": {}, "remaining": {}},
                    "retry": {"automatic": False},
                    "environment": "partner_production",
                    "capability_mode": "read_only_query",
                    "data": {"rows": []},
                },
            }
        )
        result = self.facade(gateway).execute(
            endpoint=endpoint_from_mapping(entry),
            query={"userIds": "10001"},
            body={},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["category"], "schema")


if __name__ == "__main__":
    unittest.main()
