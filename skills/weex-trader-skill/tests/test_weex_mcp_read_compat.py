#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MAPPING_PATH = ROOT / "references" / "mcp-capability-mapping.json"
MODULE_PATH = SCRIPTS / "weex_mcp_read_compat.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_compat_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("weex_mcp_read_compat", MODULE_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_mapping() -> dict[str, object]:
    return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))


def mapping_entry(mapping: dict[str, object], legacy_key: str) -> dict[str, object]:
    matches = [
        entry
        for entry in mapping["capabilities"]
        if entry["legacy_key"] == legacy_key
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one mapping for {legacy_key}, got {len(matches)}")
    return matches[0]


def endpoint_from_mapping(entry: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        key=entry["legacy_key"],
        method=entry["method"],
        path=entry["path"],
        permission=entry["auth"],
        request_transport=entry["request_transport"],
    )


def gateway_wrapper(
    payload: object,
    *,
    transport_meta: dict[str, object] | None = None,
) -> dict[str, object]:
    ok = payload.get("ok") if isinstance(payload, dict) else None
    return {
        "structured_content": copy.deepcopy(payload),
        "is_error": isinstance(ok, bool) and not ok,
        "transport_meta": copy.deepcopy(transport_meta),
    }


def request_wrapper(description: object) -> dict[str, object]:
    return gateway_wrapper(
        {"ok": True, "result": {"request": copy.deepcopy(description)}}
    )


class FakeGateway:
    def __init__(
        self,
        payload: dict[str, object] | None = None,
        *,
        transport_meta: dict[str, object] | None = None,
        **kwargs: object,
    ):
        self.init_kwargs = kwargs
        self.payload = payload or {
            "ok": True,
            "data": {
                "raw": [],
                "transport": {"http_status": 200},
            },
        }
        self.transport_meta = transport_meta
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_typed_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, copy.deepcopy(arguments)))
        return gateway_wrapper(self.payload, transport_meta=self.transport_meta)


class McpReadCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compat = load_compat_module()
        self.assertIsNotNone(
            self.compat,
            "weex_mcp_read_compat.py is missing; expected T5 RED.",
        )
        self.mapping = load_mapping()

    def settings(self, *, profile_id: str | None = "profile-a"):
        return self.compat.LocalMcpReadSettings(
            profile_id=profile_id,
            spot_base_url="https://api-spot.weex.com",
            futures_base_url="https://api-contract.weex.com",
            timeout_seconds=15.0,
            locale="zh-CN",
        )

    def facade(self, gateway: FakeGateway, *, profile_id: str | None = "profile-a"):
        return self.compat.McpReadFacade(
            settings=self.settings(profile_id=profile_id),
            gateway_factory=lambda **kwargs: gateway,
            mapping_loader=lambda: copy.deepcopy(self.mapping),
        )

    def test_mapping_read_inventory_and_spot_aliases_are_frozen(self) -> None:
        read_entries = [
            entry
            for entry in self.mapping["capabilities"]
            if entry["target"]["variant"] == "read"
        ]
        counts = {
            product: sum(entry["product"] == product for entry in read_entries)
            for product in ("contract", "spot")
        }
        self.assertEqual(counts, {"contract": 31, "spot": 20})

        aliases = {
            "spot.account.get_bill_records": {
                "after": "start_time",
                "before": "end_time",
            },
            "spot.account.get_fund_bill_records": {
                "pageIndex": "page",
                "pageSize": "limit",
            },
            "spot.tax.get_spot_account_record": {"coin": "asset"},
        }
        for legacy_key, expected in aliases.items():
            with self.subTest(legacy_key=legacy_key):
                entry = mapping_entry(self.mapping, legacy_key)
                actual = {
                    field: entry["field_decisions"][field]["target"]
                    for field in expected
                }
                self.assertEqual(actual, expected)

        scalar_policy = self.mapping["shared_policies"]["request_fields"]
        self.assertEqual(
            scalar_policy["scalar_encoding"],
            "finite_json_number_to_canonical_string_v1",
        )

    def test_all_51_read_operations_match_catalog_and_frozen_golden(self) -> None:
        import weex_contract_api as contract
        import weex_spot_api as spot

        read_entries = sorted(
            (
                entry
                for entry in self.mapping["capabilities"]
                if entry["product"] in {"contract", "spot"}
                and entry["target"]["variant"] == "read"
            ),
            key=lambda entry: (entry["product"], entry["legacy_key"]),
        )
        matrix = [
            {
                "legacy_key": entry["legacy_key"],
                "product": entry["product"],
                "method": entry["method"],
                "path": entry["path"],
                "request_transport": entry["request_transport"],
                "catalog_request_fields": entry["catalog_request_fields"],
                "field_targets": {
                    key: value["target"]
                    for key, value in entry["field_decisions"].items()
                },
                "tool": entry["target"]["tool"],
                "fixed_arguments": entry["target"]["fixed_arguments"],
                "legacy_container": entry["response"]["legacy_container"],
            }
            for entry in read_entries
        ]
        digest = hashlib.sha256(
            json.dumps(
                matrix,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            digest,
            "0b91cae6f4d8adfcaa23e94dbe9c048fa01485002bbc7230849100cac19d1761",
        )
        self.assertEqual(
            self.mapping["compatibility_gates"]["t5_read"],
            {
                "status": "golden_verified",
                "contract_operations": 31,
                "spot_operations": 20,
                "operation_matrix_sha256": digest,
                "test": "tests/test_weex_mcp_read_compat.py",
                "direct_rest_fallback": False,
            },
        )

        mapped = {
            "contract": {
                entry["legacy_key"]
                for entry in read_entries
                if entry["product"] == "contract"
            },
            "spot": {
                entry["legacy_key"]
                for entry in read_entries
                if entry["product"] == "spot"
            },
        }
        self.assertEqual(
            mapped["contract"],
            {
                key
                for key, endpoint in contract.ENDPOINTS.items()
                if not endpoint.mutating
            },
        )
        self.assertEqual(
            mapped["spot"],
            {
                key
                for key, endpoint in spot.ENDPOINTS.items()
                if not spot.is_mutating(endpoint)
            },
        )

        facade = self.facade(FakeGateway())
        self.assertEqual(
            set(facade._operations),
            {
                (entry["product"], entry["legacy_key"])
                for entry in read_entries
            },
        )

    def test_direct_settings_construction_revalidates_routes_and_runtime_values(self) -> None:
        with self.assertRaises(self.compat.McpReadCompatibilityError):
            self.compat.LocalMcpReadSettings(
                profile_id="profile-a",
                spot_base_url="https://attacker.example.com",
                futures_base_url="https://api-contract.weex.com",
                timeout_seconds=15.0,
                locale="en-US",
            )
        for timeout in (True, 0, math.inf):
            with self.subTest(timeout=timeout):
                with self.assertRaises(self.compat.McpReadCompatibilityError):
                    self.compat.LocalMcpReadSettings(
                        profile_id=None,
                        spot_base_url="https://api-spot.weex.com",
                        futures_base_url="https://api-contract.weex.com",
                        timeout_seconds=timeout,
                        locale="en-US",
                    )

    def test_describe_maps_typed_arguments_without_building_gateway(self) -> None:
        entry = mapping_entry(self.mapping, "spot.market.get_ticker_info")
        calls = []
        facade = self.compat.McpReadFacade(
            settings=self.settings(profile_id=None),
            gateway_factory=lambda **_kwargs: self.fail(
                "request description must not construct a gateway"
            ),
            request_describer=lambda **kwargs: calls.append(kwargs)
            or request_wrapper({
                "method": "GET",
                "url": "https://api-spot.weex.com/api/v3/market/ticker/price?symbol=BTCUSDT",
                "headers": {},
                "query": {"symbol": "BTCUSDT"},
                "body": {},
            }),
            mapping_loader=lambda: copy.deepcopy(self.mapping),
        )

        description = facade.describe(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={"symbol": "BTCUSDT"},
            body={},
        )

        self.assertEqual(description["method"], "GET")
        self.assertEqual(calls[0]["name"], "market_get_symbol_price")
        self.assertEqual(
            calls[0]["arguments"],
            {"market": "spot", "symbol": "BTCUSDT"},
        )
        self.assertFalse(calls[0]["trade_enabled"])

    def test_describe_rejects_method_path_and_origin_drift(self) -> None:
        entry = mapping_entry(self.mapping, "spot.market.get_ticker_info")
        endpoint = endpoint_from_mapping(entry)
        valid = {
            "method": "GET",
            "url": "https://api-spot.weex.com/api/v3/market/ticker/price?symbol=BTCUSDT",
            "headers": {},
            "query": {"symbol": "BTCUSDT"},
            "body": {},
        }
        mutations = (
            {**valid, "method": "POST"},
            {**valid, "url": "https://api-spot.weex.com/api/v3/time"},
            {**valid, "url": "https://evil.example/api/v3/market/ticker/price"},
        )
        for description in mutations:
            with self.subTest(description=description):
                facade = self.compat.McpReadFacade(
                    settings=self.settings(profile_id=None),
                    request_describer=lambda **_kwargs: request_wrapper(description),
                    mapping_loader=lambda: copy.deepcopy(self.mapping),
                )
                with self.assertRaises(self.compat.McpReadCompatibilityError) as exc_info:
                    facade.describe(
                        product="spot",
                        endpoint=endpoint,
                        query={"symbol": "BTCUSDT"},
                        body={},
                    )
                self.assertEqual(exc_info.exception.code, "request_description_mismatch")

    def test_resolver_keeps_origin_timeout_locale_precedence_and_profile_id(self) -> None:
        profile = SimpleNamespace(
            profile_id="stable-id",
            contract_base_url="https://profile-contract.weex.tech",
            spot_base_url="https://profile-spot.weex.tech",
        )
        resolved = self.compat.resolve_local_mcp_read_settings(
            profile=profile,
            explicit_futures_base_url="https://explicit-contract.weex.tech/",
            explicit_spot_base_url=None,
            explicit_timeout=7.5,
            environ={
                "WEEX_CONTRACT_API_BASE": "https://env-contract.weex.tech",
                "WEEX_SPOT_API_BASE": "https://env-spot.weex.tech",
                "WEEX_API_TIMEOUT": "99",
                "WEEX_LOCALE": "zh-CN",
            },
        )

        self.assertEqual(resolved.profile_id, "stable-id")
        self.assertEqual(
            resolved.futures_base_url,
            "https://explicit-contract.weex.tech",
        )
        self.assertEqual(resolved.spot_base_url, "https://profile-spot.weex.tech")
        self.assertEqual(resolved.timeout_seconds, 7.5)
        self.assertEqual(resolved.locale, "zh-CN")

        public = self.compat.resolve_local_mcp_read_settings(
            profile=None,
            environ={"WEEX_API_BASE": "https://generic.weex.tech"},
        )
        self.assertIsNone(public.profile_id)
        self.assertEqual(public.futures_base_url, "https://generic.weex.tech")
        self.assertEqual(public.spot_base_url, "https://generic.weex.tech")
        self.assertEqual(public.timeout_seconds, 15.0)
        self.assertEqual(public.locale, "en-US")

        contract_only = self.compat.resolve_local_mcp_read_settings(
            profile=None,
            active_product="contract",
            environ={
                "WEEX_CONTRACT_API_BASE": "https://contract.weex.tech",
                "WEEX_SPOT_API_BASE": "https://attacker.example.com",
            },
        )
        self.assertEqual(
            contract_only.futures_base_url,
            "https://contract.weex.tech",
        )
        self.assertEqual(contract_only.spot_base_url, "https://api-spot.weex.com")

    def test_read_maps_aliases_and_canonical_scalars_before_one_typed_call(self) -> None:
        entry = mapping_entry(self.mapping, "spot.account.get_bill_records")
        gateway = FakeGateway(
            payload={
                "ok": True,
                "data": {
                    "raw": [{"billId": "b-1"}],
                    "http_status": 200,
                },
            },
            transport_meta={"version": 1, "request_id": "read", "attempt_count": 1, "http_status": 206},
        )
        response = self.facade(gateway).execute(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={
                "coinId": 7,
                "bizType": "TRADE",
                "after": 1000,
                "before": 2000,
                "limit": 50,
            },
        )

        self.assertEqual(
            gateway.calls,
            [
                (
                    "account_get_bills",
                    {
                        "market": "spot",
                        "trading_mode": "live",
                        "bill_type": "account",
                        "coin_id": "7",
                        "biz_type": "TRADE",
                        "start_time": "1000",
                        "end_time": "2000",
                        "limit": "50",
                    },
                )
            ],
        )
        self.assertEqual(
            response,
            {"ok": True, "status": 206, "data": [{"billId": "b-1"}]},
        )

    def test_invalid_gateway_wrapper_fails_closed(self) -> None:
        entry = mapping_entry(self.mapping, "account.get_account_balance")
        gateway = FakeGateway()
        gateway.call_typed_tool = lambda *_args, **_kwargs: {
            "structured_content": {"ok": True, "data": {"raw": []}},
            "transport_meta": None,
        }

        response = self.facade(gateway).execute(
            product="contract",
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={},
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "compat_response_invalid")

    def test_multi_symbol_is_identity_and_does_not_fan_out(self) -> None:
        entry = mapping_entry(self.mapping, "spot.market.get_book_ticker")
        gateway = FakeGateway(
            payload={
                "ok": True,
                "data": {
                    "raw": [{"symbol": "BTCUSDT"}, {"symbol": "ETHUSDT"}],
                    "transport": {"http_status": 200},
                },
            }
        )
        response = self.facade(gateway, profile_id=None).execute(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={"symbols": ["BTCUSDT", "ETHUSDT"]},
            body={},
        )

        self.assertEqual(
            gateway.calls,
            [
                (
                    "market_get_book_ticker",
                    {
                        "market": "spot",
                        "symbols": ["BTCUSDT", "ETHUSDT"],
                    },
                )
            ],
        )
        self.assertEqual(response["status"], 200)

    def test_success_requires_raw_and_never_invents_http_status(self) -> None:
        entry = mapping_entry(self.mapping, "account.get_account_balance")
        gateway = FakeGateway(payload={"ok": True, "data": {"raw": []}})
        response = self.facade(gateway).execute(
            product="contract",
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={},
        )
        self.assertEqual(response, {"ok": True, "status": None, "data": []})

        gateway.payload = {"ok": True, "data": {"balances": []}}
        invalid = self.facade(gateway).execute(
            product="contract",
            endpoint=endpoint_from_mapping(entry),
            query={},
            body={},
        )
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["status"], None)
        self.assertEqual(invalid["error"]["code"], "compat_response_invalid")

    def test_error_restores_safe_upstream_payload_or_keeps_typed_error(self) -> None:
        entry = mapping_entry(self.mapping, "spot.order.history_orders")
        gateway = FakeGateway(
            payload={
                "ok": False,
                "error": {
                    "code": "upstream_error",
                    "message": "WEEX account API returned an error.",
                    "details": {
                        "http_status": 429,
                        "upstream_code": "-1059",
                        "upstream_payload": {"code": "-1059", "msg": "too many requests"},
                    },
                },
            }
        )
        response = self.facade(gateway).execute(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={"symbol": "BTCUSDT"},
            body={},
        )
        self.assertEqual(response["status"], 429)
        self.assertEqual(
            response["error"],
            {"code": "-1059", "msg": "too many requests"},
        )

        gateway.payload = {
            "ok": False,
            "error": {"code": "invalid_arguments", "message": "limit is invalid"},
        }
        typed = self.facade(gateway).execute(
            product="spot",
            endpoint=endpoint_from_mapping(entry),
            query={"symbol": "BTCUSDT"},
            body={},
        )
        self.assertEqual(typed["status"], None)
        self.assertEqual(typed["error"]["code"], "invalid_arguments")

    def test_trade_unknown_fields_and_mapping_drift_fail_before_gateway_call(self) -> None:
        gateway = FakeGateway()
        facade = self.facade(gateway)
        write_entry = mapping_entry(self.mapping, "spot.order.place_order")
        with self.assertRaises(self.compat.McpReadCompatibilityError):
            facade.execute(
                product="spot",
                endpoint=endpoint_from_mapping(write_entry),
                query={},
                body={"symbol": "BTCUSDT"},
            )

        read_entry = mapping_entry(self.mapping, "market.get_server_time")
        with self.assertRaises(self.compat.McpReadCompatibilityError):
            facade.execute(
                product="contract",
                endpoint=endpoint_from_mapping(read_entry),
                query={"undocumented": "value"},
                body={},
            )

        drifted = endpoint_from_mapping(read_entry)
        drifted.path = "/capi/v3/market/other"
        with self.assertRaises(self.compat.McpReadCompatibilityError):
            facade.execute(
                product="contract",
                endpoint=drifted,
                query={},
                body={},
            )
        self.assertEqual(gateway.calls, [])

    def test_non_finite_numbers_are_rejected_before_gateway_call(self) -> None:
        entry = mapping_entry(self.mapping, "market.get_depth_data")
        gateway = FakeGateway()
        for value in (True, math.nan, math.inf):
            with self.subTest(value=value):
                with self.assertRaises(self.compat.McpReadCompatibilityError):
                    self.facade(gateway, profile_id=None).execute(
                        product="contract",
                        endpoint=endpoint_from_mapping(entry),
                        query={"symbol": "BTCUSDT", "limit": value},
                        body={},
                    )
        self.assertEqual(gateway.calls, [])


if __name__ == "__main__":
    unittest.main()
