#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_V1_REL = Path("references/mcp-boundary-contract-v1.json")
CONTRACT_REL = Path("references/mcp-boundary-contract-v2.json")
MANUAL_INTENT_REL = Path("references/manual-mutation-intent-v2.json")
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
PARTNER_EVIDENCE_RELS = {
    "partner_field_catalog": Path("references/partner-field-catalog.json"),
    "partner_output_schema": Path("references/partner-output-schema.md"),
}
APPROVED_SOURCE_EVIDENCE_NAMES = frozenset(
    {*LOCAL_EVIDENCE_RELS, *PARTNER_EVIDENCE_RELS}
)
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
    "reconciliation_result",
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
HISTORICAL_V1_DIGEST = (
    "sha256:07d3e11874c1f8956680b87e725061f486c9a001236bbf105b12be8dce2322a1"
)
NOT_SENT_TERMINAL_OUTCOME = {
    "class": "not_sent",
    "grant_disposition": "not_sent_terminal",
    "write_attempted": False,
}
NOT_SENT_TERMINAL_OUTCOME_DIGEST = (
    "sha256:8f587cdbad0e314e82c20fefe6edf4379243e56eb154cb8ca47b198db2198027"
)
RECONCILIATION_EVIDENCE_DIGEST_DOMAIN = (
    "weex-mcp/reconciliation-evidence/v1\\0"
)
EXPECTED_RECONCILIATION_RESULT_STATUSES = [
    "RESOLVED_ACCEPTED",
    "UNRESOLVED",
    "MANUAL_REVIEW_REQUIRED",
]
EXPECTED_RECONCILIATION_EVIDENCE_ERRORS = [
    "read_failed",
    "invalid_response",
    "identity_not_found",
    "identity_ambiguous",
    "search_incomplete",
    "state_mismatch",
]
EXPECTED_RECONCILIATION_READ_TOOLS = [
    "account_get_orders",
    "account_get_conditional_orders",
    "account_get_symbol_config",
    "account_get_positions",
    "account_get_position",
]
EXPECTED_SAFE_OBSERVATION_FIELDS = {
    "market",
    "trading_mode",
    "symbol",
    "order_id",
    "client_order_id",
    "client_algo_id",
    "position_id",
    "scope",
    "order_kind",
    "status",
    "margin_mode",
    "position_side",
    "plan_type",
    "quantity_mode",
    "enabled",
    "amount",
    "trigger_price",
    "target",
}
EXPECTED_INTENT_STATES = [
    "UNCLAIMED",
    "CONFIRMED",
    "CLAIMED",
    "NOT_SENT_TERMINAL",
    "DEFINITIVE_REJECTION",
    "ACCEPTED",
    "REVIEW_REQUIRED",
    "LEGACY_INVALIDATED",
]
EXPECTED_RECONCILIATION_STATUSES = [
    "NOT_REQUIRED",
    "PENDING",
    "RESOLVED_ACCEPTED",
    "RESOLVED_REJECTED",
    "RESOLVED_NOT_SENT",
    "UNRESOLVED",
    "MANUAL_REVIEW_REQUIRED",
]
EXPECTED_INTENT_PROPERTIES = {
    "intent_version",
    "intent_id",
    "state",
    "tool_name",
    "action",
    "market",
    "trading_mode",
    "direct_arguments",
    "prepared_mutation",
    "prepared_artifact_digest",
    "execution_binding_digest",
    "risk_facts",
    "profile_binding",
    "contract_versions",
    "created_at",
    "expires_at",
    "confirmed_at",
    "grant",
    "request_identity",
    "reconciliation",
    "legacy",
}
EXPECTED_INTENT_REQUIRED = {
    "intent_version",
    "intent_id",
    "state",
    "reconciliation",
    "legacy",
}
EXPECTED_V2_INTENT_REQUIRED = EXPECTED_INTENT_PROPERTIES
EXPECTED_LEGACY_FORBIDDEN_FIELDS = EXPECTED_INTENT_PROPERTIES - EXPECTED_INTENT_REQUIRED


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


def _source_evidence_paths(skill_root: Path) -> dict[str, Path]:
    paths = {
        name: skill_root / relative
        for name, relative in LOCAL_EVIDENCE_RELS.items()
    }
    partner_root = skill_root.parent / "weex-partner-skill"
    paths.update(
        {
            name: partner_root / relative
            for name, relative in PARTNER_EVIDENCE_RELS.items()
        }
    )
    return paths


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _iter_schema_refs(value: Any):
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str):
            yield reference
        for child in value.values():
            yield from _iter_schema_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_schema_refs(child)


def _resolve_json_pointer(document: Any, fragment: str) -> Any:
    if fragment in ("", "#"):
        return document
    if not fragment.startswith("#/"):
        raise ValueError(f"unsupported JSON Schema reference fragment: {fragment}")
    current = document
    for raw_token in fragment[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise ValueError(f"unresolved JSON Schema reference: {fragment}")
            current = current[token]
        elif isinstance(current, list):
            try:
                index = int(token)
                current = current[index]
            except (ValueError, IndexError) as exc:
                raise ValueError(
                    f"unresolved JSON Schema reference: {fragment}"
                ) from exc
        else:
            raise ValueError(f"unresolved JSON Schema reference: {fragment}")
    return current


def validate_schema_references(
    document: dict[str, Any],
    *,
    document_name: str,
    resources: dict[str, dict[str, Any]],
) -> None:
    for reference in _iter_schema_refs(document):
        if reference.startswith("#"):
            target = document
            fragment = reference
        else:
            if "#" not in reference:
                raise ValueError(
                    f"external JSON Schema reference lacks fragment: {reference}"
                )
            resource_name, raw_fragment = reference.split("#", 1)
            if resource_name not in resources:
                raise ValueError(
                    f"unsupported external JSON Schema reference: {reference}"
                )
            target = resources[resource_name]
            fragment = f"#{raw_fragment}"
        _resolve_json_pointer(target, fragment)
    if document_name not in resources or resources[document_name] is not document:
        raise ValueError("JSON Schema resource registration mismatch")


def _conditional_map(
    conditions: Any,
    discriminator: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(conditions, list):
        raise ValueError("schema conditional rules must be an array")
    result: dict[str, dict[str, Any]] = {}
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        if_schema = condition.get("if")
        if not isinstance(if_schema, dict):
            continue
        discriminator_schema = (
            if_schema.get("properties", {}).get(discriminator, {})
            if isinstance(if_schema.get("properties"), dict)
            else {}
        )
        value = discriminator_schema.get("const")
        if isinstance(value, str):
            if value in result:
                raise ValueError(
                    f"duplicate schema conditional for {discriminator}={value}"
                )
            result[value] = condition
    return result


def _second_all_of_property_const(schema: Any, property_name: str) -> Any:
    if not isinstance(schema, dict):
        raise ValueError("schema allOf binding must be an object")
    all_of = schema.get("allOf")
    if not isinstance(all_of, list) or len(all_of) != 2:
        raise ValueError("schema allOf binding must contain two branches")
    second = all_of[1]
    if not isinstance(second, dict):
        raise ValueError("schema allOf binding branch must be an object")
    properties = second.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("schema allOf binding properties are missing")
    property_schema = properties.get(property_name)
    if not isinstance(property_schema, dict):
        raise ValueError(f"schema allOf binding lacks {property_name}")
    return property_schema.get("const")


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


def validate_historical_v1(contract_v1: dict[str, Any]) -> None:
    body = dict(contract_v1)
    stored_digest = body.pop("boundary_contract_digest", None)
    if stored_digest != HISTORICAL_V1_DIGEST:
        raise ValueError("historical boundary v1 digest changed")
    if stored_digest != canonical_digest(body):
        raise ValueError("historical boundary v1 canonical digest mismatch")
    if contract_v1.get("schema_version") != 1:
        raise ValueError("historical boundary v1 schema version changed")
    versions = contract_v1.get("candidate_versions")
    if not isinstance(versions, dict):
        raise ValueError("historical boundary v1 versions are missing")
    if versions.get("package") != "0.3.0" or versions.get("grant") != "1":
        raise ValueError("historical boundary v1 versions changed")


def validate_manual_intent_contract(
    manual_intent: dict[str, Any],
    boundary_contract: dict[str, Any],
) -> str:
    body = dict(manual_intent)
    stored_digest = body.pop("schema_digest", None)
    if stored_digest != canonical_digest(body):
        raise ValueError("manual intent schema digest mismatch")
    if manual_intent.get("$schema") != (
        "https://json-schema.org/draft/2020-12/schema"
    ):
        raise ValueError("manual intent schema dialect mismatch")
    if manual_intent.get("$id") != (
        "https://github.com/weex-labs/weex-agent-skills/blob/main/"
        "skills/weex-trader-skill/references/manual-mutation-intent-v2.json"
    ):
        raise ValueError("manual intent schema id mismatch")
    if manual_intent.get("intent_version") != 2:
        raise ValueError("manual intent schema version mismatch")
    if manual_intent.get("type") != "object" or (
        manual_intent.get("additionalProperties") is not False
    ):
        raise ValueError("manual intent root must be a closed object")
    if set(manual_intent.get("required", [])) != EXPECTED_INTENT_REQUIRED:
        raise ValueError("manual intent required field set mismatch")
    properties = manual_intent.get("properties")
    if not isinstance(properties, dict) or set(properties) != EXPECTED_INTENT_PROPERTIES:
        raise ValueError("manual intent property set mismatch")
    if properties["state"].get("enum") != EXPECTED_INTENT_STATES:
        raise ValueError("manual intent state machine mismatch")
    if set(properties["tool_name"].get("enum", [])) != EXPECTED_DIRECT_TOOLS:
        raise ValueError("manual intent Direct tool allowlist mismatch")
    expected_actions = {
        name.removeprefix("trade_") for name in EXPECTED_DIRECT_TOOLS
    }
    if set(properties["action"].get("enum", [])) != expected_actions:
        raise ValueError("manual intent action allowlist mismatch")
    contract_versions = properties.get("contract_versions")
    if contract_versions != {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "package": {"const": "0.3.2"},
            "tool_contract": {"const": "2"},
            "prepare": {"const": "1"},
            "intent": {"const": "2"},
            "grant": {"const": "2"},
            "boundary_contract_digest": {
                "type": "string",
                "pattern": "^sha256:[0-9a-f]{64}$",
            },
        },
        "required": [
            "package",
            "tool_contract",
            "prepare",
            "intent",
            "grant",
            "boundary_contract_digest",
        ],
    }:
        raise ValueError("manual intent contract versions mismatch")

    root_branches = manual_intent.get("oneOf")
    if not isinstance(root_branches, list) or len(root_branches) != 2:
        raise ValueError("manual intent v2/legacy root branches mismatch")
    v2_branch, legacy_branch = root_branches
    if set(v2_branch.get("required", [])) != EXPECTED_V2_INTENT_REQUIRED:
        raise ValueError("manual intent complete v2 branch mismatch")
    if v2_branch.get("properties", {}).get("legacy") != {"type": "null"}:
        raise ValueError("manual intent complete v2 legacy isolation mismatch")
    if (
        v2_branch.get("properties", {})
        .get("reconciliation", {})
        .get("properties", {})
        .get("template", {})
        .get("$ref")
        != "mcp-boundary-contract-v2.json#/schema_components/reconciliation"
    ):
        raise ValueError("manual intent complete v2 reconciliation binding mismatch")
    if set(legacy_branch.get("required", [])) != EXPECTED_INTENT_REQUIRED:
        raise ValueError("manual intent legacy envelope required fields mismatch")
    legacy_forbidden = {
        tuple(rule.get("required", []))
        for rule in legacy_branch.get("not", {}).get("anyOf", [])
        if isinstance(rule, dict)
    }
    if legacy_forbidden != {
        (field,) for field in EXPECTED_LEGACY_FORBIDDEN_FIELDS
    }:
        raise ValueError("manual intent legacy envelope permits fabricated v2 fields")
    legacy_schema = legacy_branch.get("properties", {}).get("legacy")
    if not isinstance(legacy_schema, dict) or set(legacy_schema.get("required", [])) != {
        "source_version",
        "source_record_digest",
        "source_state",
        "source_started",
        "migration",
        "migrated_at",
    }:
        raise ValueError("manual intent legacy source metadata mismatch")
    if (
        legacy_branch.get("properties", {})
        .get("reconciliation", {})
        .get("properties", {})
        .get("template")
        != {"type": "null"}
    ):
        raise ValueError("manual intent legacy reconciliation must not fabricate a template")

    resources = {
        "manual-mutation-intent-v2.json": manual_intent,
        "mcp-boundary-contract-v2.json": boundary_contract,
    }
    validate_schema_references(
        boundary_contract,
        document_name="mcp-boundary-contract-v2.json",
        resources=resources,
    )
    validate_schema_references(
        manual_intent,
        document_name="manual-mutation-intent-v2.json",
        resources=resources,
    )

    reconciliation = properties.get("reconciliation")
    if not isinstance(reconciliation, dict):
        raise ValueError("manual intent reconciliation schema is missing")
    reconciliation_properties = reconciliation.get("properties")
    if not isinstance(reconciliation_properties, dict):
        raise ValueError("manual intent reconciliation properties are missing")
    if reconciliation_properties["status"].get("enum") != (
        EXPECTED_RECONCILIATION_STATUSES
    ):
        raise ValueError("manual intent reconciliation states mismatch")
    reconcile_conditions = _conditional_map(
        reconciliation.get("allOf"),
        "status",
    )
    if set(reconcile_conditions) != set(EXPECTED_RECONCILIATION_STATUSES):
        raise ValueError("manual intent reconciliation FSM coverage mismatch")
    expected_results = {
        "NOT_REQUIRED": None,
        "PENDING": None,
        "RESOLVED_ACCEPTED": "ACCEPTED",
        "RESOLVED_REJECTED": "DEFINITIVE_REJECTION",
        "RESOLVED_NOT_SENT": "NOT_SENT",
        "UNRESOLVED": None,
        "MANUAL_REVIEW_REQUIRED": None,
    }
    evidence_required = {
        "RESOLVED_ACCEPTED",
        "RESOLVED_REJECTED",
        "RESOLVED_NOT_SENT",
        "UNRESOLVED",
        "MANUAL_REVIEW_REQUIRED",
    }
    for status, expected_result in expected_results.items():
        then_properties = reconcile_conditions[status].get("then", {}).get(
            "properties", {}
        )
        result_schema = then_properties.get("result", {})
        if expected_result is None:
            if result_schema != {"type": "null"}:
                raise ValueError(
                    f"manual intent reconciliation result mismatch: {status}"
                )
        elif result_schema.get("const") != expected_result:
            raise ValueError(
                f"manual intent reconciliation result mismatch: {status}"
            )
        observed_schema = then_properties.get("observed_at", {})
        evidence_schema = then_properties.get("evidence_digest", {})
        if status in evidence_required:
            if observed_schema.get("type") != "integer" or (
                evidence_schema.get("pattern") != "^sha256:[0-9a-f]{64}$"
            ):
                raise ValueError(
                    f"manual intent reconciliation evidence mismatch: {status}"
                )
        elif observed_schema != {"type": "null"} or evidence_schema != {
            "type": "null"
        }:
            raise ValueError(
                f"manual intent reconciliation pending evidence mismatch: {status}"
            )

    all_conditions = manual_intent.get("allOf")
    tool_conditions = _conditional_map(all_conditions, "tool_name")
    if set(tool_conditions) != EXPECTED_DIRECT_TOOLS:
        raise ValueError("manual intent per-tool binding coverage mismatch")
    tools = boundary_contract.get("direct_tools")
    if not isinstance(tools, list) or len(tools) != 14:
        raise ValueError("manual intent boundary Direct tools are missing")
    for index, tool in enumerate(tools):
        name = tool.get("name")
        if name not in EXPECTED_DIRECT_TOOLS:
            raise ValueError("manual intent boundary Direct tool mismatch")
        action = name.removeprefix("trade_")
        then_properties = tool_conditions[name].get("then", {}).get(
            "properties", {}
        )
        if then_properties.get("action") != {"const": action}:
            raise ValueError(f"manual intent action binding mismatch: {name}")
        if then_properties.get("direct_arguments", {}).get("$ref") != (
            f"mcp-boundary-contract-v2.json#/direct_tools/{index}/input_schema"
        ):
            raise ValueError(f"manual intent argument binding mismatch: {name}")
        if then_properties.get("risk_facts", {}).get("$ref") != (
            "mcp-boundary-contract-v2.json#/direct_tools/"
            f"{index}/prepare_contract/risk_facts_schema"
        ):
            raise ValueError(f"manual intent risk binding mismatch: {name}")
        prepared_binding = then_properties.get("prepared_mutation", {}).get(
            "allOf", []
        )
        if len(prepared_binding) != 2 or prepared_binding[0].get("$ref") != (
            "mcp-boundary-contract-v2.json#/schema_components/prepared_mutation"
        ):
            raise ValueError(f"manual intent PreparedMutation binding mismatch: {name}")
        prepared_properties = prepared_binding[1].get("properties", {})
        if prepared_properties.get("tool_name") != {"const": name} or (
            prepared_properties.get("action") != {"const": action}
        ):
            raise ValueError(f"manual intent discriminator binding mismatch: {name}")

    state_conditions = _conditional_map(all_conditions, "state")
    if set(state_conditions) != set(EXPECTED_INTENT_STATES):
        raise ValueError("manual intent state constraint coverage mismatch")
    unclaimed_properties = state_conditions["UNCLAIMED"]["then"]["properties"]
    if (
        unclaimed_properties.get("grant") != {"type": "null"}
        or unclaimed_properties.get("confirmed_at") != {"type": "null"}
        or unclaimed_properties.get("legacy") != {"type": "null"}
        or unclaimed_properties.get("reconciliation", {})
        .get("properties", {})
        .get("status", {})
        .get("enum")
        != ["NOT_REQUIRED"]
    ):
        raise ValueError("manual intent UNCLAIMED state mismatch")
    for state in ("CONFIRMED", "CLAIMED"):
        expected_grant_state = "ISSUED" if state == "CONFIRMED" else "CLAIMED"
        state_properties = state_conditions[state]["then"]["properties"]
        grant_schema = state_properties["grant"]
        if _second_all_of_property_const(grant_schema, "state") != (
            expected_grant_state
        ):
            raise ValueError(f"manual intent grant state mismatch: {state}")
        if (
            state_properties.get("confirmed_at", {}).get("type") != "integer"
            or state_properties.get("legacy") != {"type": "null"}
            or state_properties.get("reconciliation", {})
            .get("properties", {})
            .get("status", {})
            .get("enum")
            != ["NOT_REQUIRED"]
        ):
            raise ValueError(f"manual intent state metadata mismatch: {state}")
    for state in ("NOT_SENT_TERMINAL", "DEFINITIVE_REJECTION", "ACCEPTED"):
        terminal_then = state_conditions[state].get("then", {})
        terminal_branches = terminal_then.get("oneOf", [])
        if len(terminal_branches) != 2:
            raise ValueError(f"manual intent terminal branch mismatch: {state}")
        if (
            terminal_then.get("properties", {})
            .get("reconciliation", {})
            .get("properties", {})
            .get("status")
            != {"const": "NOT_REQUIRED"}
        ):
            raise ValueError(f"manual intent terminal reconciliation mismatch: {state}")
        grant_schema = terminal_branches[0].get("properties", {}).get("grant", {})
        legacy_schema = terminal_branches[1].get("properties", {}).get("legacy", {})
        if _second_all_of_property_const(grant_schema, "state") != state:
            raise ValueError(f"manual intent terminal grant mismatch: {state}")
        if _second_all_of_property_const(legacy_schema, "migration") != state:
            raise ValueError(f"manual intent terminal legacy mismatch: {state}")
        if (
            terminal_branches[0].get("properties", {}).get("confirmed_at", {}).get(
                "type"
            )
            != "integer"
            or terminal_branches[1].get("properties", {}).get("grant")
            != {"type": "null"}
        ):
            raise ValueError(f"manual intent terminal timestamp mismatch: {state}")
    review_branches = state_conditions["REVIEW_REQUIRED"].get("then", {}).get(
        "oneOf", []
    )
    if len(review_branches) != 2:
        raise ValueError("manual intent review branch mismatch")
    if _second_all_of_property_const(
        review_branches[0].get("properties", {}).get("grant"),
        "state",
    ) != "REVIEW_REQUIRED" or _second_all_of_property_const(
        review_branches[1].get("properties", {}).get("legacy"),
        "migration",
    ) != "REVIEW_REQUIRED":
        raise ValueError("manual intent review grant/legacy mismatch")
    review_statuses = (
        state_conditions["REVIEW_REQUIRED"]
        .get("then", {})
        .get("properties", {})
        .get("reconciliation", {})
        .get("properties", {})
        .get("status", {})
        .get("enum")
    )
    if review_statuses != EXPECTED_RECONCILIATION_STATUSES[1:]:
        raise ValueError("manual intent review reconciliation mismatch")
    invalidated_properties = state_conditions["LEGACY_INVALIDATED"]["then"][
        "properties"
    ]
    invalidated_legacy = invalidated_properties["legacy"]
    if _second_all_of_property_const(
        invalidated_legacy,
        "migration",
    ) != "LEGACY_INVALIDATED":
        raise ValueError("manual intent legacy invalidation mismatch")
    if (
        invalidated_properties.get("grant") != {"type": "null"}
        or invalidated_properties.get("confirmed_at") != {"type": "null"}
        or invalidated_properties.get("reconciliation", {})
        .get("properties", {})
        .get("status", {})
        .get("enum")
        != ["NOT_REQUIRED"]
    ):
        raise ValueError("manual intent LEGACY_INVALIDATED state mismatch")

    single_slot = manual_intent.get("x-single-slot")
    if not isinstance(single_slot, dict) or single_slot.get(
        "unclaimed_expiry_policy"
    ) != "expired_unclaimed_is_archivable_and_replaceable":
        raise ValueError("manual intent single-slot expiry policy mismatch")
    if single_slot.get("blocking_states") != [
        "UNCLAIMED",
        "CONFIRMED",
        "CLAIMED",
        "REVIEW_REQUIRED",
    ]:
        raise ValueError("manual intent single-slot blockers mismatch")
    if single_slot.get("review_required_replaceable_after") != [
        "RESOLVED_ACCEPTED",
        "RESOLVED_REJECTED",
        "RESOLVED_NOT_SENT",
    ]:
        raise ValueError("manual intent resolved review policy mismatch")
    if single_slot.get("replaceable_states") != [
        "NOT_SENT_TERMINAL",
        "DEFINITIVE_REJECTION",
        "ACCEPTED",
        "LEGACY_INVALIDATED",
    ]:
        raise ValueError("manual intent single-slot terminal policy mismatch")

    invariants = manual_intent.get("x-invariants")
    if invariants != [
        "PreparedMutation is persisted byte-for-byte and is never reconstructed by the Skill.",
        "For the complete v2 branch, tool_name/action equal PreparedMutation.tool_name/action; direct_arguments equal PreparedMutation.canonical_arguments; risk_facts and both digests/expires_at equal their same-named PreparedMutation fields; market/trading_mode equal PreparedMutation.canonical_arguments; request_identity is derived only from PreparedMutation.reconciliation_template.",
        "For the complete v2 branch, profile_binding principal_id/account_binding_id/credential_fingerprint equal the grant fields; profile_binding route_fingerprint/credential_fingerprint equal PreparedMutation.route_fingerprint/credential_fingerprint.",
        "contract_versions.boundary_contract_digest equals the boundary contract selected by the Gateway for this intent.",
        "CONFIRMED intent and ISSUED grant are written in one file-lock transaction.",
        "Skill code issues grants but never calls claim, finalize, or recover.",
        "reconcile accepts only intent_id and consumes the stored reconciliation template.",
        "Skill persists the evidence_digest returned by MCP ReconciliationResult byte-for-byte and never computes or replaces it.",
        "legacy previews are never converted into v2 grants.",
        "The legacy migration envelope stores only source metadata and reconciliation evidence; it never fabricates PreparedMutation, grant, binding, argument, digest, or request identity fields.",
        "PreparedMutation expires_at is Unix milliseconds and is never multiplied by 1000.",
    ]:
        raise ValueError("manual intent binding invariants are missing")
    audit = manual_intent.get("$defs", {}).get("terminal_audit_record")
    if not isinstance(audit, dict):
        raise ValueError("manual intent terminal audit schema is missing")
    digest_contract = audit.get("x-record-digest")
    if digest_contract != {
        "algorithm": "sha256",
        "canonicalization": (
            "UTF-8 JSON with ensure_ascii=false, sort_keys=true, "
            "separators=(',', ':'), and allow_nan=false"
        ),
        "excludes": ["record_digest"],
    }:
        raise ValueError("manual intent audit digest contract mismatch")
    archivable = audit.get("properties", {}).get("intent", {}).get("allOf", [])
    if len(archivable) != 2 or len(archivable[1].get("oneOf", [])) != 2:
        raise ValueError("manual intent terminal audit archivable states mismatch")
    archivable_branches = archivable[1]["oneOf"]
    if archivable_branches[0].get("properties", {}).get("state", {}).get(
        "enum"
    ) != [
        "NOT_SENT_TERMINAL",
        "DEFINITIVE_REJECTION",
        "ACCEPTED",
        "LEGACY_INVALIDATED",
    ] or (
        archivable_branches[1]
        .get("properties", {})
        .get("reconciliation", {})
        .get("properties", {})
        .get("status", {})
        .get("enum")
        != ["RESOLVED_ACCEPTED", "RESOLVED_REJECTED", "RESOLVED_NOT_SENT"]
    ):
        raise ValueError("manual intent terminal audit state binding mismatch")
    return stored_digest


def validate_reconciliation_result_contract(contract: dict[str, Any]) -> None:
    components = contract.get("schema_components")
    if not isinstance(components, dict):
        raise ValueError("ReconciliationResult schema components are missing")
    result = components.get("reconciliation_result")
    if (
        not isinstance(result, dict)
        or result.get("type") != "object"
        or result.get("additionalProperties") is not False
    ):
        raise ValueError("ReconciliationResult must be a closed object")
    properties = result.get("properties")
    expected_fields = [
        "tool_name",
        "prepared_artifact_digest",
        "status",
        "result",
        "observed_at",
        "evidence",
        "evidence_digest",
    ]
    if (
        not isinstance(properties, dict)
        or set(properties) != set(expected_fields)
        or result.get("required") != expected_fields
    ):
        raise ValueError("ReconciliationResult field contract mismatch")
    direct_tools = contract.get("direct_tools")
    if not isinstance(direct_tools, list) or properties["tool_name"] != {
        "type": "string",
        "enum": [tool.get("name") for tool in direct_tools],
    }:
        raise ValueError("ReconciliationResult tool binding mismatch")
    digest_schema = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
    if properties["prepared_artifact_digest"] != digest_schema or properties[
        "evidence_digest"
    ] != digest_schema:
        raise ValueError("ReconciliationResult digest field mismatch")
    if properties["status"] != {
        "type": "string",
        "enum": EXPECTED_RECONCILIATION_RESULT_STATUSES,
    }:
        raise ValueError("ReconciliationResult status contract mismatch")
    if properties["result"] != {
        "oneOf": [{"const": "ACCEPTED"}, {"type": "null"}]
    }:
        raise ValueError("ReconciliationResult result contract mismatch")
    if properties["observed_at"] != {"type": "integer", "minimum": 0}:
        raise ValueError("ReconciliationResult observed_at contract mismatch")

    conditions = _conditional_map(result.get("allOf"), "status")
    if set(conditions) != set(EXPECTED_RECONCILIATION_RESULT_STATUSES):
        raise ValueError("ReconciliationResult status coverage mismatch")
    for status in EXPECTED_RECONCILIATION_RESULT_STATUSES:
        expected_result = (
            {"const": "ACCEPTED"}
            if status == "RESOLVED_ACCEPTED"
            else {"type": "null"}
        )
        if (
            conditions[status]
            .get("then", {})
            .get("properties", {})
            .get("result")
            != expected_result
        ):
            raise ValueError(f"ReconciliationResult result mismatch: {status}")

    definitions = result.get("$defs")
    if not isinstance(definitions, dict) or set(definitions) != {
        "safe_observation",
        "query_evidence",
        "per_item_evidence",
        "manual_evidence",
    }:
        raise ValueError("ReconciliationResult evidence definitions mismatch")
    safe_observation = definitions["safe_observation"]
    query_template = components.get("reconciliation", {}).get("$defs", {}).get(
        "query", {}
    )
    expected_observation = query_template.get("properties", {}).get("expected")
    if (
        not isinstance(safe_observation, dict)
        or safe_observation.get("additionalProperties") is not False
        or set(safe_observation.get("properties", {}))
        != EXPECTED_SAFE_OBSERVATION_FIELDS
        or safe_observation != expected_observation
    ):
        raise ValueError("ReconciliationResult safe observation must be closed")

    query = definitions["query_evidence"]
    query_fields = [
        "kind",
        "strategy",
        "read_tool",
        "completeness",
        "identity",
        "matched",
        "search_complete",
        "observation",
        "error_code",
    ]
    if (
        query.get("type") != "object"
        or query.get("additionalProperties") is not False
        or set(query.get("properties", {})) != set(query_fields)
        or query.get("required") != query_fields
    ):
        raise ValueError("ReconciliationResult query evidence must be closed")
    query_properties = query["properties"]
    if (
        query_properties["kind"] != {"const": "query"}
        or query_properties["strategy"]
        != {
            "type": "string",
            "enum": ["query_exact_identity", "query_state"],
        }
        or query_properties["read_tool"]
        != {
            "type": "string",
            "enum": EXPECTED_RECONCILIATION_READ_TOOLS,
        }
        or query_properties["completeness"]
        != {
            "type": "string",
            "enum": ["deterministic", "state_based", "partial"],
        }
        or query_properties["identity"]
        != {"$ref": "#/schema_components/request_identity"}
        or query_properties["matched"] != {"type": "boolean"}
        or query_properties["search_complete"] != {"type": "boolean"}
        or query_properties["observation"]
        != {
            "oneOf": [
                {
                    "$ref": (
                        "#/schema_components/reconciliation_result/$defs/"
                        "safe_observation"
                    )
                },
                {"type": "null"},
            ]
        }
        or query_properties["error_code"]
        != {
            "oneOf": [
                {
                    "type": "string",
                    "enum": EXPECTED_RECONCILIATION_EVIDENCE_ERRORS,
                },
                {"type": "null"},
            ]
        }
    ):
        raise ValueError("ReconciliationResult query evidence contract mismatch")

    per_item = definitions["per_item_evidence"]
    if (
        per_item.get("type") != "object"
        or per_item.get("additionalProperties") is not False
        or set(per_item.get("properties", {}))
        != {"kind", "completeness", "items"}
        or per_item.get("required") != ["kind", "completeness", "items"]
        or per_item["properties"]["kind"] != {"const": "per_item"}
        or per_item["properties"]["completeness"] != {"const": "partial"}
    ):
        raise ValueError("ReconciliationResult per-item evidence must be closed")
    items = per_item["properties"]["items"]
    item = items.get("items") if isinstance(items, dict) else None
    if (
        not isinstance(item, dict)
        or items.get("type") != "array"
        or items.get("minItems") != 1
        or item.get("additionalProperties") is not False
        or set(item.get("properties", {})) != {"index", "identity", "query"}
        or item.get("required") != ["index", "identity", "query"]
        or item["properties"]["index"] != {"type": "integer", "minimum": 0}
        or item["properties"]["identity"]
        != {"$ref": "#/schema_components/request_identity"}
        or item["properties"]["query"]
        != {
            "$ref": (
                "#/schema_components/reconciliation_result/$defs/query_evidence"
            )
        }
    ):
        raise ValueError("ReconciliationResult per-item evidence contract mismatch")

    manual = definitions["manual_evidence"]
    manual_template = components.get("reconciliation", {}).get("$defs", {}).get(
        "manual", {}
    )
    if (
        manual.get("type") != "object"
        or manual.get("additionalProperties") is not False
        or set(manual.get("properties", {}))
        != {"kind", "reason", "selector", "identity", "completeness"}
        or manual.get("required")
        != ["kind", "reason", "selector", "identity", "completeness"]
        or manual["properties"]["kind"] != {"const": "manual"}
        or manual["properties"]["reason"]
        != manual_template.get("properties", {}).get("reason")
        or manual["properties"]["selector"]
        != manual_template.get("properties", {}).get("selector")
        or manual["properties"]["identity"]
        != {"$ref": "#/schema_components/request_identity"}
        or manual["properties"]["completeness"]
        != {"const": "unprovable"}
    ):
        raise ValueError("ReconciliationResult manual evidence must be closed")

    expected_evidence_refs = [
        {
            "$ref": (
                "#/schema_components/reconciliation_result/$defs/query_evidence"
            )
        },
        {
            "$ref": (
                "#/schema_components/reconciliation_result/$defs/"
                "per_item_evidence"
            )
        },
        {
            "$ref": (
                "#/schema_components/reconciliation_result/$defs/manual_evidence"
            )
        },
    ]
    if properties["evidence"] != {"oneOf": expected_evidence_refs}:
        raise ValueError("ReconciliationResult evidence union mismatch")
    if result.get("x-evidence-digest") != {
        "algorithm": "sha256",
        "domain": RECONCILIATION_EVIDENCE_DIGEST_DOMAIN,
        "canonicalization": (
            "UTF-8 JSON with ensure_ascii=false, sort_keys=true, "
            "separators=(',', ':'), and allow_nan=false"
        ),
        "includes": [
            "tool_name",
            "prepared_artifact_digest",
            "status",
            "result",
            "observed_at",
            "evidence",
        ],
    }:
        raise ValueError("ReconciliationResult evidence digest contract mismatch")


def validate_v2_revision(
    contract: dict[str, Any],
    contract_v1: dict[str, Any],
    manual_intent_digest: str,
) -> None:
    if contract.get("schema_version") != 2:
        raise ValueError("boundary v2 schema version mismatch")
    expected_versions = dict(contract_v1.get("candidate_versions", {}))
    expected_versions.update({"package": "0.3.2", "grant": "2"})
    if contract.get("candidate_versions") != expected_versions:
        raise ValueError("boundary v2 candidate versions mismatch")
    if contract.get("source_baselines") != {
        "skill_commit": "650303cfc7264bc83b7c71569745e6852c5c2e71",
        "mcp_commit": "e684b575bb7b7c44edd4f2a897137d020a79b4fb",
        "openapi_main": "cc9b95b9db6e61c7e10176b5b70fb05b05b0146c",
    }:
        raise ValueError("boundary v2 source baselines mismatch")
    historical_owners = contract_v1.get("owners")
    historical_mcp_owners = (
        historical_owners.get("mcp") if isinstance(historical_owners, dict) else None
    )
    if not isinstance(historical_mcp_owners, list):
        raise ValueError("historical boundary owner contract is missing")
    expected_owners = {
        **historical_owners,
        "mcp": [
            *historical_mcp_owners,
            "reconciliation_read_execution",
            "reconciliation_evidence_digest",
        ],
        "mcp_dispatch_only": [
            "grant_claim",
            "grant_finalize",
            "grant_recover",
        ],
    }
    if contract.get("owners") != expected_owners:
        raise ValueError("boundary v2 owner contract mismatch")
    if contract.get("supersedes") != {
        "schema_version": 1,
        "package": "0.3.0",
        "boundary_contract_digest": HISTORICAL_V1_DIGEST,
        "status": "SUPERSEDED",
    }:
        raise ValueError("boundary v2 supersedes binding mismatch")
    if contract.get("manual_intent_contract") != {
        "path": str(MANUAL_INTENT_REL),
        "schema_version": 2,
        "schema_digest": manual_intent_digest,
    }:
        raise ValueError("boundary v2 manual intent binding mismatch")
    if contract.get("direct_tools_digest") != contract_v1.get("direct_tools_digest"):
        raise ValueError("boundary v2 Direct tool contract unexpectedly changed")
    if contract.get("legacy_write_mapping_digest") != contract_v1.get(
        "legacy_write_mapping_digest"
    ):
        raise ValueError("boundary v2 legacy write mapping unexpectedly changed")

    host_api = contract.get("mcp_host_private_api")
    if host_api != {
        "prepare_mutation": {
            "parameters": ["tool_name", "arguments", "invocation_context", "now_ms"],
            "returns": "PreparedMutation",
            "network": "prepare_reads_only",
        },
        "recover_mutation": {
            "parameters": ["tool_name", "invocation_context", "now_ms"],
            "returns": "GrantRecovery",
            "network": "none",
            "allowed_caller": "trusted_host_via_mcp_service",
        },
        "reconcile_mutation": {
            "parameters": ["prepared_mutation", "invocation_context", "now_ms"],
            "returns": "ReconciliationResult",
            "return_schema_ref": "#/schema_components/reconciliation_result",
            "network": "reconciliation_reads_only",
            "allowed_caller": "trusted_host_via_mcp_service",
        },
    }:
        raise ValueError("boundary v2 MCP Host-private API mismatch")
    if "reconcile_mutation" in {
        tool.get("name") for tool in contract.get("direct_tools", [])
    }:
        raise ValueError("Host-private reconcile_mutation must not be a public tool")
    builder = contract.get("gateway_host_api", {}).get("build_execution_grant")
    if builder != {
        "parameters": ["prepared_mutation", "grant_id", "issued_at"],
        "returns": "ExecutionGrant",
        "persists": False,
        "claims": False,
        "trusted_binding_source": (
            "Gateway-owned invocation context supplies principal_id, "
            "account_binding_id, route_fingerprint, and credential_fingerprint."
        ),
        "copies_from_prepared_mutation": [
            "tool_name",
            "prepared_artifact_digest",
            "execution_binding_digest",
            "expires_at",
        ],
        "expires_at_source": "PreparedMutation.expires_at",
    }:
        raise ValueError("boundary v2 Gateway grant builder mismatch")
    grant_protocol = contract.get("grant_protocol")
    if not isinstance(grant_protocol, dict):
        raise ValueError("boundary v2 grant protocol is missing")
    outcome = grant_protocol.get("not_sent_terminal_outcome")
    if outcome != NOT_SENT_TERMINAL_OUTCOME or canonical_digest(outcome) != (
        NOT_SENT_TERMINAL_OUTCOME_DIGEST
    ):
        raise ValueError("boundary v2 terminal outcome digest mismatch")
    if grant_protocol.get("not_sent_terminal_outcome_digest") != (
        NOT_SENT_TERMINAL_OUTCOME_DIGEST
    ):
        raise ValueError("boundary v2 terminal outcome binding mismatch")
    if grant_protocol.get("terminal_receipt_reasons") != [
        "grant_expired",
        "prepared_expired",
        "revoked",
    ]:
        raise ValueError("boundary v2 terminal receipt reasons mismatch")
    if grant_protocol.get("restart_recovery") != "McpService.recover_mutation":
        raise ValueError("boundary v2 restart recovery mismatch")


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
    validate_reconciliation_result_contract(contract)
    if len(components["batch_item"].get("oneOf", [])) != 3:
        raise ValueError("batch item schema must define accepted, rejected, and unknown branches")
    if set(components["execution_grant"].get("$defs", {})) != {
        "issued_grant",
        "grant_claim",
        "grant_terminal_receipt",
        "grant_recovery",
    }:
        raise ValueError("execution grant return models mismatch")
    receipt = components["execution_grant"]["$defs"]["grant_terminal_receipt"]
    if receipt != {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "grant_id": {"type": "string", "minLength": 1},
            "state": {"const": "NOT_SENT_TERMINAL"},
            "terminal_state": {"const": "NOT_SENT_TERMINAL"},
            "resolved_at": {"type": "integer", "minimum": 0},
            "reason": {
                "type": "string",
                "enum": ["grant_expired", "prepared_expired", "revoked"],
            },
            "outcome_digest": {"const": NOT_SENT_TERMINAL_OUTCOME_DIGEST},
        },
        "required": [
            "grant_id",
            "state",
            "terminal_state",
            "resolved_at",
            "reason",
            "outcome_digest",
        ],
    }:
        raise ValueError("grant terminal receipt schema mismatch")
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
    if grant_api["claim"].get("strong_failure_guarantee") != {
        "durable_claim": "must_return_valid_grant_claim",
        "durable_terminal": "must_return_valid_grant_terminal_receipt",
        "invalid_return": "grant_state_unchanged",
        "process_termination": "recover_unfinalized_claim_to_review_required",
        "raised_exception": "grant_state_unchanged",
    }:
        raise ValueError("grant consumer claim failure guarantee mismatch")
    expected_return_refs = {
        "load": "#/schema_components/execution_grant/$defs/issued_grant",
        "recover": "#/schema_components/execution_grant/$defs/grant_recovery",
    }
    for operation, expected_ref in expected_return_refs.items():
        if grant_api[operation]["return_schema"].get("$ref") != expected_ref:
            raise ValueError(f"grant consumer API return reference mismatch: {operation}")
    if grant_api["claim"]["return_schema"] != {
        "oneOf": [
            {"$ref": "#/schema_components/execution_grant/$defs/grant_claim"},
            {
                "$ref": (
                    "#/schema_components/execution_grant/$defs/"
                    "grant_terminal_receipt"
                )
            },
        ]
    }:
        raise ValueError("grant consumer API claim return union mismatch")
    if grant_api["claim"].get("returns") != "GrantClaim | GrantTerminalReceipt":
        raise ValueError("grant consumer API claim return name mismatch")
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
    contract_v1: dict[str, Any] | None = None,
    manual_intent: dict[str, Any] | None = None,
    verify_current_source_evidence: bool = False,
) -> dict[str, Any]:
    if contract_v1 is None:
        contract_v1 = load_json(skill_root / CONTRACT_V1_REL)
    if manual_intent is None:
        manual_intent = load_json(skill_root / MANUAL_INTENT_REL)
    validate_historical_v1(contract_v1)
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
    if approval.get("approved_by") != "user" or approval.get("approved_at") != (
        "2026-11-27"
    ):
        raise ValueError("boundary v2 approval identity or date mismatch")
    if approval.get("authorization_source") != (
        "Current conversation: the user explicitly authorized D-028."
    ):
        raise ValueError("boundary v2 approval source mismatch")
    if set(approval.get("excluded", [])) != {
        "production",
        "T18",
        "push",
        "pull request",
        "tag",
        "publish",
    }:
        raise ValueError("boundary v2 approval exclusions mismatch")
    if approval.get("scope") != [
        "package 0.3.2 candidate",
        "Host-private reconciliation",
        "ReconciliationResult safe evidence",
        "manual intent evidence digest ownership",
        "two-stage confirmation contract",
    ]:
        raise ValueError("boundary v2 approval scope mismatch")
    validate_implementation_contract(contract)
    manual_intent_digest = validate_manual_intent_contract(
        manual_intent,
        contract,
    )
    validate_v2_revision(contract, contract_v1, manual_intent_digest)

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
    inventory_source_evidence = inventory.get("source_evidence_digests")
    contract_compatibility = contract.get("compatibility")
    contract_source_evidence = (
        contract_compatibility.get("source_evidence_digests")
        if isinstance(contract_compatibility, dict)
        else None
    )
    if (
        not isinstance(inventory_source_evidence, dict)
        or set(inventory_source_evidence) != APPROVED_SOURCE_EVIDENCE_NAMES
        or contract_source_evidence != inventory_source_evidence
        or any(
            not isinstance(value, str)
            or not value.startswith("sha256:")
            or len(value) != 71
            or any(character not in "0123456789abcdef" for character in value[7:])
            for value in inventory_source_evidence.values()
        )
    ):
        raise ValueError("historical source evidence digest mismatch")
    if verify_current_source_evidence:
        for name, path in _source_evidence_paths(skill_root).items():
            if inventory_source_evidence[name] != file_digest(path):
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
        "schema_version": contract["schema_version"],
        "capabilities": len(mapping_by_key),
        "field_decisions": field_decisions,
        "direct_tools": len(direct_tools),
        "legacy_writes": len(legacy_writes),
        "auto_operations": len(auto_operations),
        "removed_breaking": removed_breaking,
        "narrowed_replacements": replacements,
        "boundary_contract_digest": stored_contract_digest,
        "historical_boundary_contract_digest": HISTORICAL_V1_DIGEST,
        "manual_intent_schema_digest": manual_intent_digest,
        "inventory_digest": actual_inventory_digest,
        "matrix_digest": actual_matrix_digest,
        "source_evidence_mode": (
            "current" if verify_current_source_evidence else "historical"
        ),
    }


def validate_bundle(
    skill_root: Path = SKILL_ROOT,
    *,
    verify_current_source_evidence: bool = False,
) -> dict[str, Any]:
    return validate_documents(
        skill_root=skill_root,
        contract=load_json(skill_root / CONTRACT_REL),
        contract_v1=load_json(skill_root / CONTRACT_V1_REL),
        manual_intent=load_json(skill_root / MANUAL_INTENT_REL),
        inventory=load_json(skill_root / INVENTORY_REL),
        matrix=load_json(skill_root / MATRIX_REL),
        verify_current_source_evidence=verify_current_source_evidence,
    )


def copy_validation_fixture(source_root: Path, target_root: Path) -> None:
    required = {
        CONTRACT_V1_REL,
        CONTRACT_REL,
        MANUAL_INTENT_REL,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the tracked WEEX MCP boundary contract bundle."
    )
    parser.add_argument(
        "--verify-current-source-evidence",
        action="store_true",
        help="Also compare approved historical source digests with the current implementation files.",
    )
    args = parser.parse_args(argv)
    try:
        report = validate_bundle(
            SKILL_ROOT,
            verify_current_source_evidence=args.verify_current_source_evidence,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"MCP boundary contract validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
