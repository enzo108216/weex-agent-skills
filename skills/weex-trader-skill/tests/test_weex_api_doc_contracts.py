#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "scripts" / "generate_weex_api_definitions.py"
MCP_CAPABILITY_MAPPING = ROOT / "references" / "mcp-capability-mapping.json"
PARTNER_API_DEFINITIONS = ROOT / "references" / "partner-api-definitions.json"


def load_generator():
    spec = importlib.util.spec_from_file_location("generate_weex_api_definitions_contract_test", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load API definition generator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_definitions(product: str) -> tuple[dict, dict[str, dict]]:
    path = ROOT / "references" / f"{product}-api-definitions.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload, {item["key"]: item for item in payload["definitions"]}


def params_by_name(definition: dict, section: str) -> dict[str, dict]:
    return {row["name"]: row for row in definition[section]}


def expected_mcp_mapping_sources() -> dict[str, tuple[str, dict]]:
    contract, _ = load_definitions("contract")
    spot, _ = load_definitions("spot")
    partner = json.loads(PARTNER_API_DEFINITIONS.read_text(encoding="utf-8"))
    sources: dict[str, tuple[str, dict]] = {}
    for definition in contract["definitions"]:
        sources[definition["key"]] = ("contract", definition)
    for definition in spot["definitions"]:
        if definition.get("category") != "rebate":
            sources[definition["key"]] = ("spot", definition)
    for definition in partner["definitions"]:
        sources[definition["key"]] = ("partner", definition)
    return sources


def expected_auth(definition: dict) -> str:
    if definition.get("permission"):
        return str(definition["permission"])
    return "USER_DATA" if definition.get("requires_auth") else "NONE"


def expected_request_transport(product: str, definition: dict) -> str:
    if product == "partner":
        return "body" if definition.get("body_fields") else "query"
    return str(definition["request_transport"])


def expected_response_container(product: str, definition: dict) -> str:
    return "partner_envelope" if product == "partner" else str(definition["response_container"])


class GeneratorRegressionTests(unittest.TestCase):
    def test_fetch_text_forces_utf8_before_reading_response_text(self) -> None:
        generator = load_generator()

        class FakeResponse:
            encoding = None

            def raise_for_status(self) -> None:
                return None

            @property
            def text(self) -> str:
                return "new / hot / >="

        response = FakeResponse()
        with mock.patch.object(generator.requests, "get", return_value=response):
            self.assertEqual(generator.fetch_text("https://example.invalid"), "new / hot / >=")

        self.assertEqual(response.encoding, "utf-8")

    def test_parameter_type_header_does_not_overwrite_parameter_name(self) -> None:
        generator = load_generator()
        soup = BeautifulSoup(
            """
            <div><table>
              <tr><th>Parameter</th><th>Parameter Type</th><th>Required</th><th>Description</th></tr>
              <tr><td>uid</td><td>Long</td><td>No</td><td>Invited user UID</td></tr>
            </table></div>
            """,
            "html.parser",
        )

        rows = generator.extract_table_rows(soup.div)

        self.assertEqual(rows, [{"name": "uid", "type": "Long", "required": "No", "description": "Invited user UID"}])

    def test_narrative_only_response_is_preserved_as_root_response(self) -> None:
        generator = load_generator()
        soup = BeautifulSoup(
            """
            <div>
              <p><strong>Response</strong></p>
              <p>Returns the transfer ID as a string when the request succeeds.</p>
              <p><strong>Response example</strong></p>
            </div>
            """,
            "html.parser",
        )

        request_params, response_params, constraints = generator._extract_sections(soup.div)

        self.assertEqual(request_params, [])
        self.assertEqual(constraints, [])
        self.assertEqual(
            response_params,
            [
                {
                    "name": "$",
                    "type": "String",
                    "description": "Returns the transfer ID as a string when the request succeeds.",
                }
            ],
        )

    def test_parse_doc_extracts_delete_body_transport_and_array_container(self) -> None:
        generator = load_generator()
        html = """
        <article>
          <div class="theme-doc-markdown markdown">
            <h1>Cancel Orders Batch (TRADE)</h1>
            <p>DELETE /capi/v3/batchOrders</p>
            <p>Request parameters</p>
            <table>
              <tr><th>Parameter</th><th>Type</th><th>Required</th><th>Description</th></tr>
              <tr><td>orderIdList</td><td>Array&lt;Long&gt;</td><td>No</td><td>Order IDs</td></tr>
            </table>
            <p>Request example</p>
            <pre>curl -X DELETE "https://api-contract.weex.com/capi/v3/batchOrders" -H "Content-Type: application/json" -d '{"orderIdList":[1]}'</pre>
            <p>Response parameters</p>
            <p>Returns an array of cancelled orders.</p>
            <p>Response example</p>
            <pre>[{"orderId":1}]</pre>
          </div>
        </article>
        """

        with mock.patch.object(generator, "fetch_text", return_value=html):
            parsed = generator.parse_doc(
                "https://www.weex.com/api-doc/contract/Transaction_API/CancelOrdersBatch"
            )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.request_transport, "body")
        self.assertEqual(parsed.query_fields, [])
        self.assertEqual(parsed.body_fields, ["orderIdList"])
        self.assertEqual(parsed.response_container, "array")

    def test_parse_doc_marks_documented_object_or_array_response_as_conditional(self) -> None:
        generator = load_generator()
        html = """
        <article>
          <div class="theme-doc-markdown markdown">
            <h1>Get Book Ticker</h1>
            <p>GET /api/v3/ticker/bookTicker</p>
            <p>Request parameters</p>
            <table>
              <tr><th>Parameter</th><th>Type</th><th>Required</th><th>Description</th></tr>
              <tr><td>symbol</td><td>String</td><td>No</td><td>Trading pair</td></tr>
            </table>
            <p>Request example</p>
            <pre>curl "https://api-spot.weex.com/api/v3/ticker/bookTicker?symbol=BTCUSDT"</pre>
            <p>Response parameters</p>
            <p>If symbol is supplied the response is a single object; otherwise, it is an array.</p>
            <p>Response example</p>
            <pre>[{"symbol":"BTCUSDT"}]</pre>
          </div>
        </article>
        """

        with mock.patch.object(generator, "fetch_text", return_value=html):
            parsed = generator.parse_doc(
                "https://www.weex.com/api-doc/spot/MarketDataAPI/GetBookTicker"
            )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.request_transport, "query")
        self.assertEqual(parsed.query_fields, ["symbol"])
        self.assertEqual(parsed.body_fields, [])
        self.assertEqual(parsed.response_container, "conditional_object_or_array")

    def test_url_collection_includes_tax_current_partner_rebate_and_demo_pages(self) -> None:
        generator = load_generator()
        tax = "https://www.weex.com/api-doc/spot/tax/GetSpotAccountRecord"
        rebate = "https://www.weex.com/api-doc/partner/rebate-endpoints/GetAffiliateCommission"
        removed = "https://www.weex.com/api-doc/partner/rebate-endpoints/GetInternalWithdrawalStatus"
        demo = "https://www.weex.com/api-doc/contract/demo/PlaceOrder"
        urls = [tax, rebate, removed, demo]

        spot_urls = generator.iter_doc_urls("spot", urls)
        contract_urls = generator.iter_doc_urls("contract", urls)

        self.assertIn(tax, spot_urls)
        self.assertIn(rebate, spot_urls)
        self.assertNotIn(removed, spot_urls)
        self.assertIn(demo, contract_urls)


class CheckedInDefinitionRegressionTests(unittest.TestCase):
    def test_mcp_capability_mapping_covers_exact_trader_inventory_and_catalog_contract(self) -> None:
        self.assertTrue(MCP_CAPABILITY_MAPPING.exists(), "missing MCP capability mapping")
        payload = json.loads(MCP_CAPABILITY_MAPPING.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["inventory_counts"], {"contract": 47, "spot": 25, "partner": 7, "total": 79})

        expected = expected_mcp_mapping_sources()
        capabilities = payload["capabilities"]
        keys = [item["legacy_key"] for item in capabilities]
        self.assertEqual(len(keys), 79)
        self.assertEqual(len(set(keys)), 79)
        self.assertEqual(set(keys), set(expected))
        self.assertNotIn("spot.rebate.internal_withdrawal", keys)
        self.assertFalse(any(key.startswith("spot.rebate.") for key in keys))

        for item in capabilities:
            key = item["legacy_key"]
            product, definition = expected[key]
            with self.subTest(key=key):
                self.assertEqual(item["product"], product)
                self.assertEqual(item["method"], definition["method"])
                self.assertEqual(item["path"], definition["path"])
                self.assertEqual(item["auth"], expected_auth(definition))
                self.assertEqual(item["request_transport"], expected_request_transport(product, definition))
                catalog_fields = [*definition.get("query_fields", []), *definition.get("body_fields", [])]
                self.assertEqual(item["catalog_request_fields"], catalog_fields)
                decisions = item["field_decisions"]
                self.assertEqual(set(decisions), set(catalog_fields))
                for field, decision in decisions.items():
                    self.assertIn(decision["decision"], {"map", "default", "forbid", "fanout"}, field)
                    if decision["decision"] in {"map", "fanout"}:
                        self.assertTrue(decision.get("target"), field)

                target = item["target"]
                self.assertTrue(target["tool"])
                self.assertIn(target["variant"], {"read", "preview_confirm"})
                self.assertIsInstance(target["fixed_arguments"], dict)
                self.assertTrue(target["hosts"])
                self.assertIn(target["permission"], {"NONE", "USER_DATA", "TRADE"})
                if target["variant"] == "preview_confirm":
                    self.assertTrue(target.get("confirm_tool"))
                    self.assertEqual(target["permission"], "TRADE")
                else:
                    self.assertNotIn("confirm_tool", target)

                response = item["response"]
                self.assertEqual(response["legacy_container"], expected_response_container(product, definition))
                self.assertEqual(response["raw_policy"], "preserve_for_compat_serializer")
                self.assertEqual(response["http_status_policy"], "compat_serializer_only")
                self.assertEqual(item["dry_run"], {"owner": "trader_facade", "network": False})
                self.assertEqual(item["error_policy"], {"name": "typed_fail_closed_v1", "fail_closed": True})
                self.assertIn(item["evidence"]["status"], {"field_audit_required", "tool_extension_required"})
                self.assertIn(item["evidence"]["capability"], {"existing_path", "missing_endpoint"})
                self.assertEqual(item["evidence"]["source"], payload["source_catalogs"][product])

    def test_mcp_capability_mapping_assigns_eleven_missing_capabilities_to_ten_tools(self) -> None:
        payload = json.loads(MCP_CAPABILITY_MAPPING.read_text(encoding="utf-8"))
        missing = [item for item in payload["capabilities"] if item["evidence"]["capability"] == "missing_endpoint"]
        expected_keys = {
            "account.get_single_position",
            "market.get_server_time",
            "spot.config.get_server_time",
            "spot.config.ping",
            "partner.get-affiliate-uids",
            "partner.get-channel-user-trade-and-asset",
            "partner.get-affiliate-commission",
            "partner.query-sub-channel-transactions",
            "partner.verify-referrals",
            "partner.get-referral-assets",
            "partner.get-referral-deal-data",
        }
        self.assertEqual({item["legacy_key"] for item in missing}, expected_keys)
        self.assertTrue(all(item["evidence"]["status"] == "tool_extension_required" for item in missing))
        tools = {item["target"]["tool"] for item in missing}
        self.assertEqual(len(missing), 11)
        self.assertEqual(len(tools), 10)
        server_times = {
            item["target"]["tool"]
            for item in missing
            if item["legacy_key"] in {"market.get_server_time", "spot.config.get_server_time"}
        }
        self.assertEqual(server_times, {"market_get_server_time"})

    def test_mcp_capability_mapping_freezes_exact_tool_extension_set(self) -> None:
        payload = json.loads(MCP_CAPABILITY_MAPPING.read_text(encoding="utf-8"))
        extension_keys = {
            item["legacy_key"]
            for item in payload["capabilities"]
            if item["evidence"]["status"] == "tool_extension_required"
        }
        self.assertEqual(
            extension_keys,
            {
                "account.get_single_position",
                "account.update_leverage_trade",
                "market.get_server_time",
                "partner.get-affiliate-commission",
                "partner.get-affiliate-uids",
                "partner.get-channel-user-trade-and-asset",
                "partner.get-referral-assets",
                "partner.get-referral-deal-data",
                "partner.query-sub-channel-transactions",
                "partner.verify-referrals",
                "spot.config.get_product_info",
                "spot.config.get_server_time",
                "spot.config.ping",
                "spot.market.get_ticker_info",
            },
        )

    def test_mcp_capability_mapping_records_verified_unpublished_candidate(self) -> None:
        payload = json.loads(MCP_CAPABILITY_MAPPING.read_text(encoding="utf-8"))
        self.assertEqual(payload["mapping_version"], "trader-mcp-capability-v2")

        candidate = payload["verified_mcp_candidate"]
        source_commit = "913b30e0343ddcdb83d74379d98b3a072b354e3e"
        self.assertEqual(
            {
                key: candidate[key]
                for key in (
                    "official_repository",
                    "package_version",
                    "tool_contract_version",
                    "source_commit",
                    "publication_status",
                )
            },
            {
                "official_repository": "https://github.com/weex-labs/weex-mcp",
                "package_version": "0.2.0",
                "tool_contract_version": "1",
                "source_commit": source_commit,
                "publication_status": "unpublished",
            },
        )
        self.assertRegex(candidate["source_commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(
            candidate["distribution_sha256"],
            {
                "wheel": "757c9ec8278139e70a28ffbef738f21f30eab0ebaa6ea42c037657b178dd782d",
                "sdist": "32f1da83eaf1f3421e1f341704514b150a8621c760233e8b8701b3b3aa62d8bf",
            },
        )
        for digest in candidate["distribution_sha256"].values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotIn("/Users/", json.dumps(candidate, ensure_ascii=False))

        expected_profiles = {
            ("broker_server", False): {
                "tool_count": 26,
                "tool_set_digest": "sha256:1abdc9a2ad78310de4c450f32310c9f985004733ba094c7370a28afc024d767f",
                "schema_digest": "sha256:45146531a09ffa99fa5e57b696341d90541274660e5689fe9aaecced06cd3ed5",
                "manifest_digest": "sha256:91c5e6049046ebe8ac0000f278d922af2dfaa1e337e19e435c943883ae9497af",
            },
            ("broker_server", True): {
                "tool_count": 52,
                "tool_set_digest": "sha256:e4b7b026283486499cc4ced31c1abdf29a703f910936b0be26158e492e42d9cf",
                "schema_digest": "sha256:81a4ba853969fbdaf3767df9f8938f531977a8d68b90593f9a78f2646b3da3da",
                "manifest_digest": "sha256:06ef9b0c41e7acd62cb1db42810341491a9c4b2838070ccbe393d5ca0e4e2115",
            },
            ("local_user", False): {
                "tool_count": 31,
                "tool_set_digest": "sha256:5243852221e5444f54531937befec646116795794cf6875ba3166cddd5b424e6",
                "schema_digest": "sha256:10fa8579693a221251874fd65145a3d49f50d76ceb31516ce5f2a16cd7a6d50d",
                "manifest_digest": "sha256:a30ddae053088dff1d462f4a2d2c3914790548b8d7dba4ac6f75f1e81d9c1c74",
            },
            ("local_user", True): {
                "tool_count": 57,
                "tool_set_digest": "sha256:6479ec2cb74a21a45266dd44455b4a44610dc5d9c8bc3d98e1df6d8e37ce8728",
                "schema_digest": "sha256:458e41b3fc601bc484b568e38329bd714ba6b28870cbffe86fa55bb36617e917",
                "manifest_digest": "sha256:36682513ae0951c866e851433f7cb20167416fe4a0ea9d582f5379ffab04dbd7",
            },
        }
        profiles = candidate["compatibility_profiles"]
        self.assertEqual(len(profiles), 4)
        actual_profiles = {
            (item["runtime_profile"], item["trade_enabled"]): {
                "tool_count": item["tool_count"],
                "tool_set_digest": item["tool_set_digest"],
                "schema_digest": item["schema_digest"],
                "manifest_digest": item["manifest_digest"],
            }
            for item in profiles
        }
        self.assertEqual(actual_profiles, expected_profiles)
        for field in ("tool_set_digest", "schema_digest", "manifest_digest"):
            values = {item[field] for item in profiles}
            self.assertEqual(len(values), 4)
            for value in values:
                self.assertRegex(value, r"^sha256:[0-9a-f]{64}$")

        extension = [
            item
            for item in payload["capabilities"]
            if item["evidence"]["status"] == "tool_extension_required"
        ]
        self.assertEqual(len(extension), 14)
        for item in extension:
            with self.subTest(key=item["legacy_key"]):
                evidence = item["evidence"]
                self.assertEqual(evidence["implementation_status"], "implemented_in_candidate")
                self.assertEqual(evidence["source_commit"], source_commit)
                self.assertEqual(evidence["package_version"], candidate["package_version"])
                self.assertEqual(evidence["tool_contract_version"], candidate["tool_contract_version"])

        pending = [
            item
            for item in payload["capabilities"]
            if item["evidence"]["status"] == "field_audit_required"
        ]
        self.assertEqual(len(pending), 65)
        for item in pending:
            with self.subTest(key=item["legacy_key"]):
                self.assertNotIn("implementation_status", item["evidence"])

    def test_partner_mcp_mapping_is_local_read_only_and_never_maps_internal_withdrawal(self) -> None:
        payload = json.loads(MCP_CAPABILITY_MAPPING.read_text(encoding="utf-8"))
        partner = [item for item in payload["capabilities"] if item["product"] == "partner"]
        self.assertEqual(len(partner), 7)
        for item in partner:
            with self.subTest(key=item["legacy_key"]):
                self.assertEqual(item["target"]["hosts"], ["local_user"])
                self.assertEqual(item["target"]["permission"], "USER_DATA")
                self.assertEqual(item["target"]["variant"], "read")
                self.assertEqual(item["auth"], "USER_DATA")
                self.assertNotIn("withdraw", item["legacy_key"].lower())
                self.assertNotIn("withdraw", item["path"].lower())

    def test_all_definitions_publish_machine_readable_transport_and_response_container(self) -> None:
        expected_delete_body = {
            "transaction.cancel_orders_batch",
            "spot.order.bulk_cancel",
        }
        conditional = {
            "spot.market.get_all_ticker_info",
            "spot.market.get_book_ticker",
            "spot.market.get_ticker_info",
        }
        for product in ("contract", "spot"):
            payload, _ = load_definitions(product)
            for definition in payload["definitions"]:
                with self.subTest(product=product, key=definition["key"]):
                    self.assertIn(definition["request_transport"], {"query", "body"})
                    request_names = [item["name"] for item in definition["request_params"]]
                    self.assertEqual(
                        definition["query_fields"] + definition["body_fields"],
                        request_names,
                    )
                    if definition["key"] in expected_delete_body:
                        self.assertEqual(definition["request_transport"], "body")
                    self.assertIn(
                        definition["response_container"],
                        {"object", "array", "conditional_object_or_array", "string"},
                    )
                    if definition["key"] in conditional:
                        self.assertEqual(
                            definition["response_container"],
                            "conditional_object_or_array",
                        )
    def test_spot_definitions_match_current_official_contract(self) -> None:
        payload, by_key = load_definitions("spot")
        self.assertEqual(len(payload["definitions"]), 33)

        account = by_key["spot.account.get_account_balance"]
        self.assertEqual(account["path"], "/api/v3/account")

        klines = by_key["spot.market.get_k_line_data"]
        interval = params_by_name(klines, "request_params")["interval"]["description"]
        self.assertNotIn("1M", interval)
        self.assertEqual(set(params_by_name(klines, "response_params")), {str(index) for index in range(11)})

        exchange = by_key["spot.config.get_product_info"]
        status = params_by_name(exchange, "request_params")["symbolStatus"]["description"]
        for expected in ("TRADING", "HALT", "BREAK"):
            self.assertIn(expected, status)
        exchange_response = params_by_name(exchange, "response_params")
        for expected in (
            "rateLimits",
            "rateLimits[].interval",
            "rateLimits[].intervalNum",
            "rateLimits[].limit",
            "rateLimits[].rateLimitType",
            "symbols[].status",
        ):
            self.assertIn(expected, exchange_response)

        tax = by_key["spot.tax.get_spot_account_record"]
        self.assertEqual((tax["method"], tax["path"]), ("POST", "/api/v3/tax/income"))
        self.assertEqual(set(params_by_name(tax, "request_params")), {"coin", "bizType", "month", "limit", "page"})
        self.assertEqual(
            set(params_by_name(tax, "response_params")),
            {"billId", "coinId", "coinName", "bizType", "fillSize", "fillValue", "deltaAmount", "afterAmount", "fees", "cTime"},
        )

        rebate = by_key["spot.rebate.get_affiliate_commission"]
        self.assertIn("/partner/rebate-endpoints/", rebate["doc_url"])
        self.assertEqual(
            set(params_by_name(rebate, "request_params")),
            {"uid", "startTime", "endTime", "coin", "productType", "page", "pageSize"},
        )

        ping_response = params_by_name(by_key["spot.config.ping"], "response_params")["$"]
        self.assertEqual(ping_response["type"], "Object")
        self.assertIn("empty JSON object", ping_response["description"])

        withdrawal_response = params_by_name(
            by_key["spot.rebate.internal_withdrawal"],
            "response_params",
        )["$"]
        self.assertEqual(withdrawal_response["type"], "String")
        self.assertIn("transfer ID", withdrawal_response["description"])

        api_symbols = by_key["spot.config.get_api_trading_symbols"]
        self.assertEqual(api_symbols["response_container"], "array")
        self.assertEqual(
            params_by_name(api_symbols, "response_params"),
            {
                "$": {
                    "name": "$",
                    "type": "Array<String>",
                    "description": "Raw response is an array of spot symbols available for API trading.",
                }
            },
        )

    def test_contract_definitions_match_current_official_contract(self) -> None:
        payload, by_key = load_definitions("contract")
        self.assertEqual(len(payload["definitions"]), 47)

        bills = by_key["account.get_contract_bills"]
        self.assertTrue({"nextKeyId", "nextKeyTime"}.issubset(params_by_name(bills, "request_params")))
        self.assertTrue({"nextKey", "nextKey.nextKeyId", "nextKey.nextKeyTime"}.issubset(params_by_name(bills, "response_params")))

        place_order = by_key["transaction.place_order"]
        self.assertIn("POST_ONLY", params_by_name(place_order, "request_params")["timeInForce"]["description"])
        batch = by_key["transaction.place_orders_batch"]
        self.assertIn("5", params_by_name(batch, "request_params")["batchOrders"]["description"])

        tp_sl = by_key["transaction.place_tp_sl_order"]
        quantity = params_by_name(tp_sl, "request_params")["quantity"]
        self.assertEqual(quantity["required"], "No")
        self.assertIn("full position", quantity["description"].lower())

        close_positions = by_key["transaction.close_positions"]
        self.assertIn("positionId", params_by_name(close_positions, "request_params"))
        self.assertTrue(any("positionId" in constraint and "priority" in constraint.lower() for constraint in close_positions["constraints"]))

        exchange = params_by_name(by_key["market.get_contract_info"], "response_params")
        for expected in (
            "rateLimits",
            "rateLimits[].interval",
            "symbols[].displaySymbol",
            "symbols[].baseAssetPrecision",
            "symbols[].quotePrecision",
            "symbols[].delivery",
            "symbols[].forwardContractFlag",
        ):
            self.assertIn(expected, exchange)

        self.assertEqual(
            set(params_by_name(by_key["market.get_klines"], "response_params")),
            {f"index[{index}]" for index in range(11)},
        )

        api_symbols = by_key["market.get_api_trading_symbols"]
        self.assertEqual(api_symbols["response_container"], "array")
        self.assertEqual(
            params_by_name(api_symbols, "response_params"),
            {
                "$": {
                    "name": "$",
                    "type": "Array<String>",
                    "description": "Raw response is an array of futures symbols available for API trading.",
                }
            },
        )

    def test_cross_page_response_references_are_expanded(self) -> None:
        _, by_key = load_definitions("contract")
        expected_sources = {
            "account.get_single_position": "account.get_all_positions",
            "market.get_history_klines": "market.get_klines",
            "market.get_index_price_klines": "market.get_klines",
            "market.get_mark_price_klines": "market.get_klines",
            "transaction.cancel_orders_batch": "transaction.cancel_order",
            "transaction.cancel_pending_order": "transaction.cancel_order",
            "transaction.get_current_order_status": "transaction.get_single_order_info",
            "transaction.get_order_history": "transaction.get_single_order_info",
            "transaction.place_orders_batch": "transaction.place_order",
            "transaction.place_pending_order": "transaction.place_order",
            "sim.transaction.get_order_history": "transaction.get_single_order_info",
        }
        for key, source_key in expected_sources.items():
            with self.subTest(key=key):
                self.assertEqual(by_key[key]["response_params"], by_key[source_key]["response_params"])
                self.assertNotIn("$", params_by_name(by_key[key], "response_params"))

        _, spot_by_key = load_definitions("spot")
        self.assertEqual(
            spot_by_key["spot.order.history_orders"]["response_params"],
            spot_by_key["spot.order.order_details"]["response_params"],
        )
        self.assertNotIn(
            "$",
            params_by_name(spot_by_key["spot.order.history_orders"], "response_params"),
        )

    def test_narrative_constraints_and_current_rate_limit_model_are_preserved(self) -> None:
        _, by_key = load_definitions("contract")
        self.assertTrue(any("At least one" in item for item in by_key["account.update_leverage_trade"].get("constraints", [])))
        self.assertTrue(any("7 days" in item for item in by_key["market.get_funding_rate_history"].get("constraints", [])))
        trade_constraints = " ".join(by_key["transaction.get_trade_details"].get("constraints", []))
        self.assertIn("7 days", trade_constraints)
        self.assertIn("365 days", trade_constraints)
        execute_price = params_by_name(by_key["transaction.modify_tp_sl_order"], "request_params")["executePrice"]["description"]
        self.assertIn("Copy-trading API keys", execute_price)

        expected_order_limits = {
            "X-ORDER-COUNT-10S": 1,
            "X-ORDER-COUNT-1M": 1,
            "X-USED-WEIGHT-1M": 0,
        }
        actual_order_limits = {
            item["header"]: item["limit"]
            for item in by_key["transaction.place_order"]["rate_limits"]
        }
        self.assertEqual(actual_order_limits, expected_order_limits)
        self.assertEqual(by_key["transaction.cancel_order"]["weight_ip"], 1)
        self.assertEqual(by_key["transaction.cancel_pending_order"]["weight_ip"], 1)

        spot_payload, spot_by_key = load_definitions("spot")
        self.assertEqual(spot_by_key["spot.order.cancel_symbol_orders"]["weight_ip"], 5)
        for definition in [*spot_payload["definitions"], *load_definitions("contract")[0]["definitions"]]:
            self.assertNotIn("weight_uid", definition)

    def test_demo_definitions_are_generated_from_current_pages_with_chinese_contract_override(self) -> None:
        _, by_key = load_definitions("contract")
        demo = by_key["sim.transaction.place_order"]
        self.assertTrue(demo["doc_url"].endswith("/contract/demo/PlaceOrder"))
        self.assertIn("POST_ONLY", params_by_name(demo, "request_params")["timeInForce"]["description"])
        self.assertEqual(
            {item["header"]: item["limit"] for item in demo["rate_limits"]},
            {"X-ORDER-COUNT-10S": 1, "X-ORDER-COUNT-1M": 1, "X-USED-WEIGHT-1M": 0},
        )

    def test_generated_definitions_do_not_contain_mojibake(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT / "references" / "spot-api-definitions.json",
                ROOT / "references" / "spot-api-definitions.md",
                ROOT / "references" / "contract-api-definitions.json",
                ROOT / "references" / "contract-api-definitions.md",
            )
        )
        for marker in ("â", "â", "â"):
            self.assertNotIn(marker, combined)


if __name__ == "__main__":
    unittest.main()
