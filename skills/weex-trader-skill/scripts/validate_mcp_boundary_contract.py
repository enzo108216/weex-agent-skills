#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_REL = Path("references/mcp-boundary-contract-v1.json")
INVENTORY_REL = Path("references/mcp-compat-field-inventory-v1.json")
MATRIX_REL = Path("references/mcp-compat-field-source-matrix-v1.json")
MAPPING_REL = Path("references/mcp-capability-mapping.json")
DEFINITION_RELS = {
    "contract": Path("references/contract-api-definitions.json"),
    "spot": Path("references/spot-api-definitions.json"),
    "partner": Path("references/partner-api-definitions.json"),
}
LOCAL_EVIDENCE_RELS = {
    "gateway": Path("scripts/weex_mcp_gateway.py"),
    "partner_facade": Path("scripts/weex_mcp_partner_compat.py"),
    "partner_golden": Path("tests/test_weex_mcp_partner_compat.py"),
    "read_facade": Path("scripts/weex_mcp_read_compat.py"),
    "read_golden": Path("tests/test_weex_mcp_read_compat.py"),
    "request_description_golden": Path("tests/test_weex_mcp_request_description_integration.py"),
    "trade_facade": Path("scripts/weex_mcp_trade_compat.py"),
    "trade_golden": Path("tests/test_weex_mcp_trade_compat.py"),
}
EXPECTED_COUNTS = {"read": 51, "write": 21, "partner": 7, "total": 79}
EXPECTED_DIRECT_TOOLS = {
    "trade_place_order",
    "trade_cancel_order",
    "trade_update_leverage",
    "trade_set_margin_mode",
    "trade_place_batch_orders",
    "trade_cancel_batch_orders",
    "trade_cancel_open_orders",
    "trade_place_conditional_order",
    "trade_cancel_conditional_order",
    "trade_place_tp_sl",
    "trade_modify_tp_sl",
    "trade_close_positions",
    "trade_adjust_position_margin",
    "trade_set_auto_append_margin",
}
EXPECTED_SCHEMA_COMPONENTS = {
    "authenticated_invocation_context",
    "credential_binding",
    "prepared_mutation",
    "execution_grant",
    "outcome",
    "transport_meta",
    "reconciliation",
    "request_identity",
    "batch_item",
}
EXPECTED_GRANT_CONSUMER_PARAMETERS = {
    "load": ["grant_id", "principal_id", "account_binding_id", "tool_name"],
    "claim": [
        "grant_id",
        "principal_id",
        "account_binding_id",
        "tool_name",
        "prepared_artifact_digest",
        "execution_binding_digest",
        "now",
    ],
    "finalize": [
        "grant_id",
        "principal_id",
        "account_binding_id",
        "tool_name",
        "claim_id",
        "terminal_state",
        "outcome_digest",
    ],
    "recover": ["grant_id", "principal_id", "account_binding_id", "tool_name", "now"],
}
EXPECTED_PERMISSION_COUNTS = {"market": 13, "account": 15, "partner": 7, "trade": 19}
EXPECTED_PERMISSION_KEYS = {
    "market": {
        "market_get_symbol_price",
        "market_get_server_time",
        "market_ping",
        "market_get_depth",
        "market_get_klines",
        "market_get_exchange_info",
        "market_get_ticker_24h",
        "market_get_book_ticker",
        "market_get_recent_trades",
        "market_get_funding_rate",
        "market_get_open_interest",
        "market_get_advanced_klines",
        "market_get_catalog",
    },
    "account": {
        "account_get_balance/spot",
        "account_get_balance/futures",
        "account_get_position/futures",
        "account_get_positions/futures",
        "account_get_orders/spot",
        "account_get_orders/futures",
        "account_get_trades/spot",
        "account_get_trades/futures",
        "account_get_bills/spot",
        "account_get_bills/futures",
        "account_get_config/futures",
        "account_get_commission_rate/futures",
        "account_get_symbol_config/futures",
        "account_get_conditional_orders/futures",
        "account_get_transfer_records/spot",
    },
    "partner": {
        "partner_get_affiliate_commission",
        "partner_get_affiliate_uids",
        "partner_get_channel_user_trade_and_asset",
        "partner_get_referral_assets",
        "partner_get_referral_deal_data",
        "partner_query_sub_channel_transactions",
        "partner_verify_referrals",
    },
    "trade": {
        "trade_place_order/spot",
        "trade_place_order/futures",
        "trade_cancel_order/spot",
        "trade_cancel_order/futures",
        "trade_update_leverage/futures",
        "trade_set_margin_mode/futures",
        "trade_place_batch_orders/spot",
        "trade_place_batch_orders/futures",
        "trade_cancel_batch_orders/spot",
        "trade_cancel_batch_orders/futures",
        "trade_cancel_open_orders/spot",
        "trade_cancel_open_orders/futures",
        "trade_place_conditional_order/futures",
        "trade_cancel_conditional_order/futures",
        "trade_place_tp_sl/futures",
        "trade_modify_tp_sl/futures",
        "trade_close_positions/futures",
        "trade_adjust_position_margin/futures",
        "trade_set_auto_append_margin/futures",
    },
}
EXPECTED_TRADE_PREPARE_TRUTH = {
    "trade_place_order/spot": "public_market_rules",
    "trade_place_order/futures": "public_market_rules",
    "trade_cancel_order/spot": "accountDetails_exact_order",
    "trade_cancel_order/futures": "accountDetails_exact_order",
    "trade_update_leverage/futures": "accountDetails_symbol_config",
    "trade_set_margin_mode/futures": "accountDetails_current_mode",
    "trade_place_batch_orders/spot": "public_market_rules",
    "trade_place_batch_orders/futures": "public_market_rules",
    "trade_cancel_batch_orders/spot": "accountDetails_exact_orders",
    "trade_cancel_batch_orders/futures": "accountDetails_exact_orders",
    "trade_cancel_open_orders/spot": "accountDetails_observed_count",
    "trade_cancel_open_orders/futures": "accountDetails_observed_count",
    "trade_place_conditional_order/futures": "public_market_rules",
    "trade_cancel_conditional_order/futures": "accountDetails_exact_algo_order",
    "trade_place_tp_sl/futures": "accountDetails_position_snapshot",
    "trade_modify_tp_sl/futures": "accountDetails_algo_order_snapshot",
    "trade_close_positions/futures": "accountDetails_position_snapshot_or_count",
    "trade_adjust_position_margin/futures": "accountDetails_position_margin_snapshot",
    "trade_set_auto_append_margin/futures": "accountDetails_position_current_bool",
}
EXPECTED_PRECONDITION_FIELDS = {
    "trade_place_order": {"symbol", "trading_mode", "rules_version"},
    "trade_cancel_order": {"request_identity", "current_status", "observed_at"},
    "trade_update_leverage": {"symbol", "current_margin_mode", "current_leverage", "observed_at"},
    "trade_set_margin_mode": {"symbol", "current_margin_mode", "observed_at"},
    "trade_place_batch_orders": {"trading_mode", "rules_version"},
    "trade_cancel_batch_orders": {"items", "observed_at"},
    "trade_cancel_open_orders": {"observed_count", "observed_at", "selector_digest"},
    "trade_place_conditional_order": {"symbol", "rules_version"},
    "trade_cancel_conditional_order": {"request_identity", "current_status", "observed_at"},
    "trade_place_tp_sl": {"position_snapshot", "rules_version"},
    "trade_modify_tp_sl": {"request_identity", "current_status", "observed_at"},
    "trade_close_positions": {"observed_at", "selector_digest", "position_snapshot", "observed_count"},
    "trade_adjust_position_margin": {"position_snapshot", "current_margin", "observed_at"},
    "trade_set_auto_append_margin": {"position_snapshot", "current_enabled", "observed_at"},
}
EXPECTED_RISK_FIELDS = {
    "trade_place_order": {"market", "trading_mode", "symbol", "side", "order_type", "quantity", "position_side", "price"},
    "trade_cancel_order": {"request_identity"},
    "trade_update_leverage": {"symbol", "target"},
    "trade_set_margin_mode": {"symbol", "target"},
    "trade_place_batch_orders": {"market", "trading_mode", "items"},
    "trade_cancel_batch_orders": {"market", "items"},
    "trade_cancel_open_orders": {"scope", "order_kind", "observed_count", "observed_at", "dynamic_scope", "symbol"},
    "trade_place_conditional_order": {"symbol", "side", "position_side", "quantity", "trigger_price", "price"},
    "trade_cancel_conditional_order": {"request_identity"},
    "trade_place_tp_sl": {"symbol", "position_side", "trigger_price", "plan_type", "quantity_mode", "quantity"},
    "trade_modify_tp_sl": {"request_identity", "trigger_price"},
    "trade_close_positions": {"scope", "observed_at", "symbol", "request_identity", "observed_count", "dynamic_scope"},
    "trade_adjust_position_margin": {"request_identity", "adjustment", "amount"},
    "trade_set_auto_append_margin": {"request_identity", "target"},
}
REQUIRED_SCENARIOS = {"success", "business_rejection", "transport_error", "schema_error"}
ALLOWED_SOURCES = {"typed_result", "transport_meta", "static_mapping", "removed_breaking"}


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def classify(item: dict[str, Any]) -> str:
    if item.get("product") == "partner":
        return "partner"
    if item.get("auth") == "TRADE":
        return "write"
    return "read"


def mapping_identity(mapping: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str]:
    capabilities = mapping.get("capabilities")
    if not isinstance(capabilities, list):
        raise ValueError("mapping capabilities must be an array")
    by_key: dict[str, dict[str, Any]] = {}
    identity_rows: list[dict[str, Any]] = []
    counts = {"read": 0, "write": 0, "partner": 0}
    for item in capabilities:
        if not isinstance(item, dict):
            raise ValueError("mapping capability must be an object")
        key = item.get("legacy_key")
        if not isinstance(key, str) or not key or key in by_key:
            raise ValueError("mapping capability keys must be unique")
        counts[classify(item)] += 1
        response = item.get("response")
        if not isinstance(response, dict):
            raise ValueError(f"{key} lacks response policy")
        identity_rows.append(
            {
                "legacy_key": key,
                "product": item.get("product"),
                "auth": item.get("auth"),
                "method": item.get("method"),
                "path": item.get("path"),
                "request_transport": item.get("request_transport"),
                "legacy_container": response.get("legacy_container"),
            }
        )
        by_key[key] = item
    if {**counts, "total": sum(counts.values())} != EXPECTED_COUNTS:
        raise ValueError(f"mapping counts are unexpected: {counts}")
    identity = {
        "mapping_version": mapping.get("mapping_version"),
        "capabilities": sorted(identity_rows, key=lambda row: row["legacy_key"]),
    }
    return by_key, canonical_digest(identity)


def catalog_digests(skill_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for product, relative in DEFINITION_RELS.items():
        document = load_json(skill_root / relative)
        definitions = document.get("definitions")
        if not isinstance(definitions, list):
            raise ValueError(f"{relative} definitions must be an array")
        projection = [
            {
                "key": item.get("key"),
                "method": item.get("method"),
                "path": item.get("path"),
                "response_container": item.get("response_container"),
                "response_params": item.get("response_params"),
            }
            for item in definitions
        ]
        result[product] = canonical_digest(
            sorted(projection, key=lambda row: str(row["key"]))
        )
    return result


def inventory_digest_body(inventory: dict[str, Any]) -> dict[str, Any]:
    normalized_entries = []
    for entry in sorted(inventory["entries"], key=lambda item: item["legacy_key"]):
        normalized_entries.append(
            {
                "legacy_key": entry["legacy_key"],
                "capability_class": entry["capability_class"],
                "fields": sorted(
                    entry["fields"],
                    key=lambda field: (field["scenario"], field["legacy_path"]),
                ),
            }
        )
    return {
        "schema_version": inventory["schema_version"],
        "generation_contract": inventory["generation_contract"],
        "field_path_contract": inventory["field_path_contract"],
        "source_mapping": inventory["source_mapping"],
        "source_mapping_version": inventory["source_mapping_version"],
        "legacy_mapping_digest": inventory["legacy_mapping_digest"],
        "source_catalog_digests": inventory["source_catalog_digests"],
        "source_evidence_digests": inventory["source_evidence_digests"],
        "expected_counts": inventory["expected_counts"],
        "review_status": inventory["review_status"],
        "entries": normalized_entries,
    }


def matrix_digest_body(matrix: dict[str, Any]) -> dict[str, Any]:
    normalized_entries = []
    for entry in sorted(matrix["entries"], key=lambda item: item["legacy_key"]):
        normalized_entries.append(
            {
                "legacy_key": entry["legacy_key"],
                "status": entry["status"],
                "fields": sorted(
                    entry["fields"],
                    key=lambda field: (field["scenario"], field["legacy_path"]),
                ),
            }
        )
    return {
        "schema_version": matrix["schema_version"],
        "field_path_contract": matrix["field_path_contract"],
        "source_mapping": matrix["source_mapping"],
        "source_mapping_version": matrix["source_mapping_version"],
        "legacy_mapping_digest": matrix["legacy_mapping_digest"],
        "expected_inventory": matrix["expected_inventory"],
        "inventory_digest": matrix["inventory_digest"],
        "direct_contract_digest": matrix["direct_contract_digest"],
        "allowed_sources": sorted(matrix["allowed_sources"]),
        "expected_counts": matrix["expected_counts"],
        "review_status": matrix["review_status"],
        "entries": normalized_entries,
    }


def property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in schema.values():
            names.update(property_names(value))
    elif isinstance(schema, list):
        for value in schema:
            names.update(property_names(value))
    return names


def property_schema_occurrences(schema: Any, property_name: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict) and isinstance(properties.get(property_name), dict):
            matches.append(properties[property_name])
        for value in schema.values():
            matches.extend(property_schema_occurrences(value, property_name))
    elif isinstance(schema, list):
        for value in schema:
            matches.extend(property_schema_occurrences(value, property_name))
    return matches


def validate_implementation_contract(contract: dict[str, Any]) -> None:
    if contract.get("schema_dialect") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("boundary contract schema dialect mismatch")
    schema_resolution = contract.get("schema_resolution")
    if not isinstance(schema_resolution, dict) or schema_resolution.get("root") != "document":
        raise ValueError("boundary contract schema resolution root mismatch")
    if schema_resolution.get("json_pointer_base") != "#":
        raise ValueError("boundary contract schema resolution pointer mismatch")

    components = contract.get("schema_components")
    if not isinstance(components, dict) or set(components) != EXPECTED_SCHEMA_COMPONENTS:
        raise ValueError("boundary contract schema components mismatch")
    if len(components["outcome"].get("oneOf", [])) != 6:
        raise ValueError("outcome schema must define six exact branches")
    if len(components["reconciliation"].get("oneOf", [])) != 3:
        raise ValueError("reconciliation schema must define query, manual, and per-item branches")
    if set(components["reconciliation"].get("$defs", {})) != {
        "query",
        "manual",
        "per_item",
    }:
        raise ValueError("reconciliation schema definitions mismatch")
    query_reconciliation = components["reconciliation"]["$defs"]["query"]
    for field in ("arguments", "expected"):
        schema = query_reconciliation.get("properties", {}).get(field)
        if not isinstance(schema, dict) or schema.get("additionalProperties") is not False:
            raise ValueError(f"reconciliation {field} schema must be closed")
    if len(components["batch_item"].get("oneOf", [])) != 3:
        raise ValueError("batch item schema must define accepted, rejected, and unknown branches")
    if set(components["execution_grant"].get("$defs", {})) != {
        "issued_grant",
        "grant_claim",
        "grant_recovery",
    }:
        raise ValueError("execution grant return models mismatch")
    if "resolved_weex_credential" not in components["credential_binding"].get("$defs", {}):
        raise ValueError("resolved credential model is missing")
    prepared_branches = (
        components["prepared_mutation"].get("allOf", [{}])[0].get("oneOf", [])
    )
    if len(prepared_branches) != 14:
        raise ValueError("prepared mutation tool bindings mismatch")
    prepared_by_tool = {
        branch.get("properties", {}).get("tool_name", {}).get("const"): branch
        for branch in prepared_branches
    }
    if set(prepared_by_tool) != EXPECTED_DIRECT_TOOLS:
        raise ValueError("prepared mutation tool discriminator mismatch")
    for name in (
        "trade_place_order",
        "trade_place_conditional_order",
        "trade_place_tp_sl",
    ):
        if not prepared_by_tool[name].get("allOf"):
            raise ValueError(f"{name} prepared mutation variant binding is missing")
    if len(prepared_by_tool["trade_close_positions"].get("oneOf", [])) != 3:
        raise ValueError("trade_close_positions prepared mutation variants mismatch")
    for component_name, schema in components.items():
        if not isinstance(schema, dict):
            raise ValueError(f"schema component is invalid: {component_name}")

    grant_api = contract.get("grant_consumer_api")
    if not isinstance(grant_api, dict) or set(grant_api) != set(EXPECTED_GRANT_CONSUMER_PARAMETERS):
        raise ValueError("grant consumer API coverage mismatch")
    for operation, parameters in EXPECTED_GRANT_CONSUMER_PARAMETERS.items():
        definition = grant_api[operation]
        if not isinstance(definition, dict):
            raise ValueError(f"grant consumer API operation is invalid: {operation}")
        if definition.get("parameters") != parameters:
            raise ValueError(f"grant consumer API parameters mismatch: {operation}")
        parameter_schemas = definition.get("parameter_schemas")
        if not isinstance(parameter_schemas, dict) or set(parameter_schemas) != set(parameters):
            raise ValueError(f"grant consumer API parameter schemas mismatch: {operation}")
        if not isinstance(definition.get("return_schema"), dict):
            raise ValueError(f"grant consumer API return schema mismatch: {operation}")
        if definition.get("allowed_caller") != "mcp_dispatch_only":
            raise ValueError(f"grant consumer API caller mismatch: {operation}")
    expected_return_refs = {
        "load": "#/schema_components/execution_grant/$defs/issued_grant",
        "claim": "#/schema_components/execution_grant/$defs/grant_claim",
        "recover": "#/schema_components/execution_grant/$defs/grant_recovery",
    }
    for operation, expected_ref in expected_return_refs.items():
        if grant_api[operation]["return_schema"].get("$ref") != expected_ref:
            raise ValueError(f"grant consumer API return reference mismatch: {operation}")
    if grant_api["finalize"]["return_schema"] != {"type": "null"}:
        raise ValueError("grant consumer API finalize return schema mismatch")

    resolver_api = contract.get("credential_resolver_api")
    if not isinstance(resolver_api, dict):
        raise ValueError("credential resolver API is missing")
    if resolver_api.get("parameters") != [
        "invocation_context",
        "required_capabilities",
    ]:
        raise ValueError("credential resolver API parameters mismatch")
    if set(resolver_api.get("parameter_schemas", {})) != set(resolver_api["parameters"]):
        raise ValueError("credential resolver API parameter schemas mismatch")
    if resolver_api.get("return_schema_ref") != (
        "#/schema_components/credential_binding/$defs/resolved_weex_credential"
    ):
        raise ValueError("credential resolver API return model mismatch")

    permissions = contract.get("permissions")
    if not isinstance(permissions, dict) or set(permissions) != set(EXPECTED_PERMISSION_COUNTS):
        raise ValueError("permission matrix domains mismatch")
    actual_permission_counts = {
        domain: len(entries) if isinstance(entries, dict) else -1
        for domain, entries in permissions.items()
    }
    if actual_permission_counts != EXPECTED_PERMISSION_COUNTS:
        raise ValueError(f"permission matrix counts mismatch: {actual_permission_counts}")
    for domain, expected_keys in EXPECTED_PERMISSION_KEYS.items():
        if set(permissions[domain]) != expected_keys:
            raise ValueError(f"permission matrix keys mismatch: {domain}")
    for tool_name, entry in permissions["market"].items():
        if entry.get("openapi_truth") != "public":
            raise ValueError("permission matrix market truth mismatch")
        expected_host = f"market.public.{tool_name.removeprefix('market_')}"
        if entry.get("host_policy_capability") != expected_host:
            raise ValueError("permission matrix market host policy mismatch")
    for key, entry in permissions["account"].items():
        tool_name, market = key.split("/", 1)
        expected_host = f"account.{market}.{tool_name.removeprefix('account_')}"
        if entry != {
            "host_policy_capability": expected_host,
            "openapi_truth": "accountDetails",
        }:
            raise ValueError("permission matrix account truth mismatch")
    for tool_name, entry in permissions["partner"].items():
        expected_host = f"partner.read.{tool_name.removeprefix('partner_')}"
        if entry != {
            "host_policy_capability": expected_host,
            "openapi_truth": "accountDetails",
        }:
            raise ValueError("permission matrix partner truth mismatch")
    for key, entry in permissions["trade"].items():
        tool_name, market = key.split("/", 1)
        mutation_truth = "spotTrading" if market == "spot" else "futuresTrading"
        expected_evidence = [mutation_truth]
        if EXPECTED_TRADE_PREPARE_TRUTH[key].startswith("accountDetails"):
            expected_evidence.append("accountDetails")
        expected_entry = {
            "host_policy_capability": (
                f"trade.{market}.{tool_name.removeprefix('trade_')}"
            ),
            "mutation_endpoint_truth": mutation_truth,
            "prepare_read_truth": EXPECTED_TRADE_PREPARE_TRUTH[key],
            "required_upstream_evidence": expected_evidence,
        }
        if entry != expected_entry:
            raise ValueError(f"permission matrix trade truth mismatch: {key}")
        evidence = entry.get("required_upstream_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("permission matrix trade evidence is incomplete")

    direct_tools = contract.get("direct_tools")
    if not isinstance(direct_tools, list):
        raise ValueError("direct_tools must be an array")
    forbidden = set(contract.get("forbidden_public_arguments", []))
    for tool in direct_tools:
        name = tool.get("name")
        market_modes = tool.get("market_modes")
        permission_refs = tool.get("permission_refs")
        if not isinstance(market_modes, dict) or not isinstance(permission_refs, dict):
            raise ValueError(f"{name} permission references are invalid")
        if set(permission_refs) != set(market_modes):
            raise ValueError(f"{name} permission matrix market coverage mismatch")
        for market, permission_key in permission_refs.items():
            expected_key = f"{name}/{market}"
            if permission_key != expected_key or permission_key not in permissions["trade"]:
                raise ValueError(f"{name} permission matrix reference mismatch")

        input_schema = tool.get("input_schema")
        output_schema = tool.get("output_schema")
        for schema_name, schema in (
            ("input schema", input_schema),
            ("output schema", output_schema),
        ):
            if not isinstance(schema, dict):
                raise ValueError(f"{name} {schema_name} is missing")
            if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise ValueError(f"{name} {schema_name} root must be a closed object")
            if not isinstance(schema.get("properties"), dict) or not schema["properties"]:
                raise ValueError(f"{name} {schema_name} properties are missing")
        if not {"market", "trading_mode"}.issubset(input_schema["properties"]):
            raise ValueError(f"{name} input schema lacks market or trading_mode")
        if forbidden & property_names(input_schema):
            raise ValueError(f"{name} input schema exposes forbidden public arguments")
        if "outcome" not in output_schema["properties"] or len(output_schema.get("oneOf", [])) != 6:
            raise ValueError(f"{name} output schema lacks six outcome branches")
        for branch in output_schema["oneOf"]:
            if "error" in branch.get("required", []):
                retryable = (
                    branch.get("properties", {})
                    .get("error", {})
                    .get("properties", {})
                    .get("retryable", {})
                    .get("const")
                )
                if retryable is not False:
                    raise ValueError(f"{name} output error retryability mismatch")
        declared_required = set(tool.get("required", []))
        schema_required = set(input_schema.get("required", []))
        if not declared_required.issubset(schema_required):
            raise ValueError(f"{name} input schema required fields mismatch")
        prepare_contract = tool.get("prepare_contract")
        if not isinstance(prepare_contract, dict) or set(prepare_contract) != {
            "precondition_policies",
            "precondition_facts_schema",
            "risk_facts_schema",
            "reconciliation_template_schema",
        }:
            raise ValueError(f"{name} prepare contract is incomplete")
        for schema_name in ("precondition_facts_schema", "risk_facts_schema"):
            schema = prepare_contract[schema_name]
            if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise ValueError(f"{name} {schema_name} must be a closed object")
        if set(prepare_contract["precondition_facts_schema"]["properties"]) != (
            EXPECTED_PRECONDITION_FIELDS[name]
        ):
            raise ValueError(f"{name} precondition fact allowlist mismatch")
        if set(prepare_contract["risk_facts_schema"]["properties"]) != (
            EXPECTED_RISK_FIELDS[name]
        ):
            raise ValueError(f"{name} risk fact allowlist mismatch")
        if name in {
            "trade_update_leverage",
            "trade_set_margin_mode",
            "trade_set_auto_append_margin",
        }:
            target = prepare_contract["risk_facts_schema"]["properties"]["target"]
            if not target.get("required") and not target.get("anyOf"):
                raise ValueError(f"{name} risk target may not be empty")
        reconciliation = prepare_contract["reconciliation_template_schema"]
        if not isinstance(reconciliation, dict):
            raise ValueError(f"{name} reconciliation template is missing")
        query_arguments = property_schema_occurrences(reconciliation, "arguments")
        selectors = property_schema_occurrences(reconciliation, "selector")
        if not query_arguments and not selectors:
            raise ValueError(f"{name} reconciliation action binding is missing")
        for field in ("arguments", "expected", "identity", "selector"):
            occurrences = property_schema_occurrences(reconciliation, field)
            if occurrences and not any(
                schema.get("required") or schema.get("oneOf")
                for schema in occurrences
            ):
                raise ValueError(f"{name} reconciliation {field} requirements are missing")

    tools_by_name = {tool["name"]: tool for tool in direct_tools}
    for tool_name in (
        "trade_place_batch_orders",
        "trade_cancel_batch_orders",
        "trade_place_tp_sl",
    ):
        item_schema = (
            tools_by_name[tool_name]["output_schema"]["properties"]["result"]
            ["properties"]["items"]["items"]
        )
        if not isinstance(item_schema.get("allOf"), list):
            raise ValueError(f"{tool_name} batch item identity schema is missing")


def validate_documents(
    *,
    skill_root: Path,
    contract: dict[str, Any],
    inventory: dict[str, Any],
    matrix: dict[str, Any],
) -> dict[str, Any]:
    contract_body = dict(contract)
    stored_contract_digest = contract_body.pop("boundary_contract_digest", None)
    if stored_contract_digest != canonical_digest(contract_body):
        raise ValueError("boundary contract digest mismatch")
    if contract.get("status") != "CANDIDATE_NEEDS_IMPLEMENTATION":
        raise ValueError("boundary contract must remain a non-active candidate")
    approval = contract.get("approval")
    if not isinstance(approval, dict):
        raise ValueError("boundary contract approval is missing")
    if approval.get("status") != "USER_CONFIRMED_CANDIDATE":
        raise ValueError("boundary contract approval status is invalid")
    for field in ("approved_by", "approved_at", "authorization_source"):
        if not isinstance(approval.get(field), str) or not approval[field]:
            raise ValueError(f"boundary contract approval lacks {field}")
    if "T5 business implementation" not in approval.get("excluded", []):
        raise ValueError("boundary approval must exclude T5 implementation")
    validate_implementation_contract(contract)

    mapping = load_json(skill_root / MAPPING_REL)
    mapping_by_key, legacy_mapping_digest = mapping_identity(mapping)
    if inventory.get("legacy_mapping_digest") != legacy_mapping_digest:
        raise ValueError("inventory legacy mapping digest mismatch")
    if matrix.get("legacy_mapping_digest") != legacy_mapping_digest:
        raise ValueError("matrix legacy mapping digest mismatch")

    if inventory.get("review_status") != "REVIEWED":
        raise ValueError("inventory must be REVIEWED")
    if matrix.get("review_status") != "REVIEWED":
        raise ValueError("source matrix must be REVIEWED")
    if inventory.get("expected_counts") != EXPECTED_COUNTS:
        raise ValueError("inventory counts mismatch")
    if matrix.get("expected_counts") != EXPECTED_COUNTS:
        raise ValueError("matrix counts mismatch")
    if inventory.get("source_catalog_digests") != catalog_digests(skill_root):
        raise ValueError("source catalog digest mismatch")
    for name, relative in LOCAL_EVIDENCE_RELS.items():
        expected = inventory["source_evidence_digests"].get(name)
        if expected != file_digest(skill_root / relative):
            raise ValueError(f"source evidence digest mismatch: {name}")

    actual_inventory_digest = canonical_digest(inventory_digest_body(inventory))
    if inventory.get("inventory_digest") != actual_inventory_digest:
        raise ValueError("inventory digest mismatch")
    if matrix.get("inventory_digest") != actual_inventory_digest:
        raise ValueError("matrix inventory binding mismatch")
    actual_matrix_digest = canonical_digest(matrix_digest_body(matrix))
    if matrix.get("matrix_digest") != actual_matrix_digest:
        raise ValueError("matrix digest mismatch")

    inventory_by_key = {entry["legacy_key"]: entry for entry in inventory["entries"]}
    matrix_by_key = {entry["legacy_key"]: entry for entry in matrix["entries"]}
    if set(inventory_by_key) != set(mapping_by_key) or set(matrix_by_key) != set(mapping_by_key):
        raise ValueError("79-capability coverage mismatch")
    field_decisions = 0
    removed_breaking = 0
    replacements = 0
    output_paths: dict[str, list[str]] = {}
    for legacy_key in sorted(mapping_by_key):
        inventory_entry = inventory_by_key[legacy_key]
        matrix_entry = matrix_by_key[legacy_key]
        if inventory_entry.get("capability_class") != classify(mapping_by_key[legacy_key]):
            raise ValueError(f"{legacy_key} capability class mismatch")
        if matrix_entry.get("status") != "REVIEWED":
            raise ValueError(f"{legacy_key} matrix entry is not REVIEWED")
        inventory_fields = {
            (field["scenario"], field["legacy_path"]): field
            for field in inventory_entry["fields"]
        }
        matrix_fields = {
            (field["scenario"], field["legacy_path"]): field
            for field in matrix_entry["fields"]
        }
        if set(inventory_fields) != set(matrix_fields):
            raise ValueError(f"{legacy_key} exact field coverage mismatch")
        if not REQUIRED_SCENARIOS.issubset(
            {scenario for scenario, _ in inventory_fields}
        ):
            raise ValueError(f"{legacy_key} lacks required scenarios")
        paths: set[str] = set()
        for field in matrix_entry["fields"]:
            source = field.get("source")
            source_path = field.get("source_path")
            if source not in ALLOWED_SOURCES:
                raise ValueError(f"{legacy_key} has unsupported source")
            if not isinstance(source_path, str) or "/raw" in source_path:
                raise ValueError(f"{legacy_key} source path contains raw or is invalid")
            if source == "typed_result" and not source_path.startswith("/structured_content"):
                raise ValueError(f"{legacy_key} typed source is outside structured_content")
            if source == "transport_meta" and not source_path.startswith("/transport_meta"):
                raise ValueError(f"{legacy_key} transport source is outside transport_meta")
            if source == "static_mapping" and not field.get("derivation_rule"):
                raise ValueError(f"{legacy_key} static source lacks derivation rule")
            if source == "removed_breaking":
                removed_breaking += 1
            if field.get("compatibility") == "narrowed_typed_replacement":
                replacements += 1
                policy_id = field.get("replacement_change_id")
                if policy_id not in contract.get("replacement_policies", {}):
                    raise ValueError(f"{legacy_key} replacement policy is missing")
                for required in (
                    "rationale",
                    "change_ledger_ref",
                    "release_note_ref",
                    "compat_test_ref",
                ):
                    if not field.get(required):
                        raise ValueError(f"{legacy_key} replacement lacks {required}")
            field_decisions += 1
            paths.add(source_path)
        output_paths[legacy_key] = sorted(paths)

    if field_decisions != 2448:
        raise ValueError(f"expected 2448 field decisions, got {field_decisions}")
    if removed_breaking != 0:
        raise ValueError("reviewed candidate must not contain removed_breaking")
    if replacements != 151:
        raise ValueError(f"expected 151 narrowed replacements, got {replacements}")
    if contract.get("capability_output_paths") != output_paths:
        raise ValueError("matrix path does not match capability target output schema")
    if contract.get("capability_output_paths_digest") != canonical_digest(output_paths):
        raise ValueError("capability output path digest mismatch")

    direct_tools = contract.get("direct_tools")
    if not isinstance(direct_tools, list):
        raise ValueError("direct_tools must be an array")
    tool_names = {tool.get("name") for tool in direct_tools}
    if tool_names != EXPECTED_DIRECT_TOOLS:
        raise ValueError("14-tool contract mismatch")
    if contract.get("direct_tools_digest") != canonical_digest(direct_tools):
        raise ValueError("direct tool digest mismatch")
    forbidden = set(contract.get("forbidden_public_arguments", []))
    for tool in direct_tools:
        declared = set(tool.get("required", [])) | set(tool.get("optional", []))
        if declared & forbidden:
            raise ValueError(f"{tool['name']} exposes forbidden public arguments")
    legacy_writes = contract.get("legacy_write_mapping")
    if not isinstance(legacy_writes, dict) or len(legacy_writes) != 21:
        raise ValueError("legacy write mapping must contain 21 entries")
    if not set(legacy_writes.values()).issubset(EXPECTED_DIRECT_TOOLS):
        raise ValueError("legacy write mapping targets an unknown Direct tool")
    if contract.get("legacy_write_mapping_digest") != canonical_digest(legacy_writes):
        raise ValueError("legacy write mapping digest mismatch")
    auto_operations = contract.get("auto_operation_allowlist")
    if not isinstance(auto_operations, list) or len(auto_operations) != 6:
        raise ValueError("Auto allowlist must contain six legacy operations")
    if not set(auto_operations).issubset(legacy_writes):
        raise ValueError("Auto allowlist contains an unknown legacy operation")
    if len(contract.get("partner_response_schemas", {})) != 7:
        raise ValueError("Partner response schemas must cover seven operations")

    compatibility = contract.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ValueError("boundary compatibility binding is missing")
    expected_bindings = {
        "inventory_digest": actual_inventory_digest,
        "matrix_digest": actual_matrix_digest,
        "legacy_mapping_digest": legacy_mapping_digest,
        "source_catalog_digests": inventory["source_catalog_digests"],
        "source_evidence_digests": inventory["source_evidence_digests"],
        "direct_contract_digest": matrix["direct_contract_digest"],
        "field_decisions": 2448,
        "removed_breaking": 0,
    }
    for key, expected in expected_bindings.items():
        if compatibility.get(key) != expected:
            raise ValueError(f"boundary compatibility binding mismatch: {key}")
    return {
        "ok": True,
        "capabilities": len(mapping_by_key),
        "field_decisions": field_decisions,
        "direct_tools": len(direct_tools),
        "legacy_writes": len(legacy_writes),
        "auto_operations": len(auto_operations),
        "removed_breaking": removed_breaking,
        "narrowed_replacements": replacements,
        "boundary_contract_digest": stored_contract_digest,
        "inventory_digest": actual_inventory_digest,
        "matrix_digest": actual_matrix_digest,
    }


def validate_bundle(skill_root: Path = SKILL_ROOT) -> dict[str, Any]:
    return validate_documents(
        skill_root=skill_root,
        contract=load_json(skill_root / CONTRACT_REL),
        inventory=load_json(skill_root / INVENTORY_REL),
        matrix=load_json(skill_root / MATRIX_REL),
    )


def copy_validation_fixture(source_root: Path, target_root: Path) -> None:
    required = {
        CONTRACT_REL,
        INVENTORY_REL,
        MATRIX_REL,
        MAPPING_REL,
        *DEFINITION_RELS.values(),
        *LOCAL_EVIDENCE_RELS.values(),
    }
    for relative in required:
        destination = target_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)


def main() -> int:
    try:
        report = validate_bundle(SKILL_ROOT)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"MCP boundary contract validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
