from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate_mcp_boundary_contract.py"
PAIR_VALIDATOR_PATH = ROOT / "scripts" / "verify_mcp_boundary_pair.py"
CONTRACT_V1_PATH = ROOT / "references" / "mcp-boundary-contract-v1.json"
CONTRACT_PATH = ROOT / "references" / "mcp-boundary-contract-v2.json"
MANUAL_INTENT_PATH = ROOT / "references" / "manual-mutation-intent-v2.json"
INVENTORY_PATH = ROOT / "references" / "mcp-compat-field-inventory-v1.json"
MATRIX_PATH = ROOT / "references" / "mcp-compat-field-source-matrix-v1.json"
MAPPING_PATH = ROOT / "references" / "mcp-capability-mapping.json"
OFFICIAL_MCP_REPOSITORY = "https://github.com/weex-labs/weex-mcp"


def load_validator():
    if not VALIDATOR_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "validate_mcp_boundary_contract_test",
        VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_pair_validator():
    if not PAIR_VALIDATOR_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "verify_mcp_boundary_pair_test",
        PAIR_VALIDATOR_PATH,
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_mcp_pair_fixture(
    mcp_root: Path,
    *,
    contract: dict,
    pair_validator,
) -> None:
    compatibility = mcp_root / "src" / "weex_mcp" / "compatibility.py"
    compatibility.parent.mkdir(parents=True)
    runtime_versions = {
        key: value
        for key, value in contract["candidate_versions"].items()
        if key not in {"intent", "status"}
    }
    compatibility.write_text(
        "\n".join(
            [
                f"_BOUNDARY_CANDIDATE_VERSIONS = {runtime_versions!r}",
                (
                    "BOUNDARY_CONTRACT_CANDIDATE = "
                    f"{pair_validator.expected_mcp_metadata(contract)!r}"
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (compatibility.parent / "_version.py").write_text(
        f"__version__ = {contract['candidate_versions']['package']!r}\n",
        encoding="utf-8",
    )
    package_version = contract["candidate_versions"]["package"]
    (mcp_root / "server.json").write_text(
        json.dumps(
            {
                "version": package_version,
                "repository": {
                    "source": "github",
                    "url": OFFICIAL_MCP_REPOSITORY,
                },
                "packages": [{"version": package_version}],
            }
        ),
        encoding="utf-8",
    )
    (mcp_root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                "name = 'weex-mcp'",
                "[project.urls]",
                f"Homepage = '{OFFICIAL_MCP_REPOSITORY}'",
                f"Repository = '{OFFICIAL_MCP_REPOSITORY}'",
                f"Issues = '{OFFICIAL_MCP_REPOSITORY}/issues'",
                f"Documentation = '{OFFICIAL_MCP_REPOSITORY}#readme'",
                "",
            ]
        ),
        encoding="utf-8",
    )


def copy_pair_skill_fixture(validator, target_root: Path) -> None:
    validator.copy_validation_fixture(ROOT, target_root)
    target_validator = target_root / "scripts" / VALIDATOR_PATH.name
    target_validator.parent.mkdir(parents=True, exist_ok=True)
    target_validator.write_bytes(VALIDATOR_PATH.read_bytes())


class McpBoundaryContractTests(unittest.TestCase):
    def test_v1_contract_remains_immutable_historical_evidence(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_V1_PATH.read_text(encoding="utf-8"))
        body = dict(contract)
        stored = body.pop("boundary_contract_digest")
        self.assertEqual(
            stored,
            "sha256:07d3e11874c1f8956680b87e725061f486c9a001236bbf105b12be8dce2322a1",
        )
        self.assertEqual(stored, validator.canonical_digest(body))
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["candidate_versions"]["package"], "0.3.0")
        self.assertEqual(contract["candidate_versions"]["grant"], "1")

    def test_machine_contract_contains_implementation_complete_models(self) -> None:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

        self.assertEqual(contract["schema_version"], 2)
        self.assertEqual(contract["candidate_versions"]["package"], "0.3.2")
        self.assertEqual(contract["candidate_versions"]["grant"], "2")
        self.assertEqual(
            contract["source_baselines"],
            {
                "skill_commit": "650303cfc7264bc83b7c71569745e6852c5c2e71",
                "mcp_commit": "e684b575bb7b7c44edd4f2a897137d020a79b4fb",
                "openapi_main": "cc9b95b9db6e61c7e10176b5b70fb05b05b0146c",
            },
        )
        self.assertIn("D-028", contract["approval"]["authorization_source"])
        self.assertEqual(
            contract["owners"]["mcp"],
            [
                "schema",
                "canonicalization",
                "REST",
                "signing",
                "hard_validation",
                "single_write_attempt",
                "outcome",
                "reconciliation_read_execution",
                "reconciliation_evidence_digest",
            ],
        )
        self.assertEqual(
            contract["supersedes"]["boundary_contract_digest"],
            "sha256:07d3e11874c1f8956680b87e725061f486c9a001236bbf105b12be8dce2322a1",
        )

        self.assertEqual(
            set(contract["schema_components"]),
            {
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
            },
        )
        self.assertEqual(contract["schema_resolution"]["root"], "document")
        self.assertEqual(
            set(contract["schema_components"]["execution_grant"]["$defs"]),
            {
                "issued_grant",
                "grant_claim",
                "grant_terminal_receipt",
                "grant_recovery",
            },
        )
        receipt = contract["schema_components"]["execution_grant"]["$defs"][
            "grant_terminal_receipt"
        ]
        self.assertEqual(
            receipt["properties"]["reason"]["enum"],
            ["grant_expired", "prepared_expired", "revoked"],
        )
        self.assertEqual(
            receipt["properties"]["outcome_digest"]["const"],
            "sha256:8f587cdbad0e314e82c20fefe6edf4379243e56eb154cb8ca47b198db2198027",
        )
        self.assertEqual(
            contract["grant_consumer_api"]["claim"]["return_schema"]["oneOf"],
            [
                {"$ref": "#/schema_components/execution_grant/$defs/grant_claim"},
                {
                    "$ref": "#/schema_components/execution_grant/$defs/grant_terminal_receipt"
                },
            ],
        )
        self.assertEqual(
            contract["mcp_host_private_api"]["recover_mutation"]["returns"],
            "GrantRecovery",
        )
        self.assertEqual(
            contract["mcp_host_private_api"]["reconcile_mutation"],
            {
                "parameters": [
                    "prepared_mutation",
                    "invocation_context",
                    "now_ms",
                ],
                "returns": "ReconciliationResult",
                "return_schema_ref": (
                    "#/schema_components/reconciliation_result"
                ),
                "network": "reconciliation_reads_only",
                "allowed_caller": "trusted_host_via_mcp_service",
            },
        )
        self.assertNotIn(
            "reconcile_mutation",
            {tool["name"] for tool in contract["direct_tools"]},
        )
        self.assertEqual(
            contract["gateway_host_api"]["build_execution_grant"]["returns"],
            "ExecutionGrant",
        )
        self.assertIn(
            "resolved_weex_credential",
            contract["schema_components"]["credential_binding"]["$defs"],
        )
        self.assertEqual(
            len(contract["schema_components"]["prepared_mutation"]["allOf"][0]["oneOf"]),
            14,
        )
        self.assertEqual(
            len(contract["schema_components"]["batch_item"]["oneOf"]),
            3,
        )
        self.assertEqual(
            set(contract["grant_consumer_api"]),
            {"load", "claim", "finalize", "recover"},
        )
        self.assertEqual(
            set(contract["credential_resolver_api"]),
            {"parameters", "parameter_schemas", "returns", "return_schema_ref"},
        )
        self.assertEqual(
            set(contract["permissions"]),
            {"market", "account", "partner", "trade"},
        )
        self.assertEqual(
            {name: len(entries) for name, entries in contract["permissions"].items()},
            {"market": 13, "account": 15, "partner": 7, "trade": 19},
        )
        self.assertEqual(
            contract["grant_consumer_api"]["claim"]["parameters"],
            [
                "grant_id",
                "principal_id",
                "account_binding_id",
                "tool_name",
                "prepared_artifact_digest",
                "execution_binding_digest",
                "now",
            ],
        )
        self.assertEqual(
            contract["grant_consumer_api"]["claim"]["strong_failure_guarantee"],
            {
                "durable_claim": "must_return_valid_grant_claim",
                "durable_terminal": "must_return_valid_grant_terminal_receipt",
                "invalid_return": "grant_state_unchanged",
                "process_termination": "recover_unfinalized_claim_to_review_required",
                "raised_exception": "grant_state_unchanged",
            },
        )
        for operation in contract["grant_consumer_api"].values():
            self.assertEqual(operation["allowed_caller"], "mcp_dispatch_only")
            self.assertEqual(
                set(operation["parameter_schemas"]),
                set(operation["parameters"]),
            )
            self.assertIn("return_schema", operation)
        self.assertEqual(
            len(contract["schema_components"]["outcome"]["oneOf"]),
            6,
        )
        self.assertEqual(
            len(contract["schema_components"]["reconciliation"]["oneOf"]),
            3,
        )
        query_reconciliation = contract["schema_components"]["reconciliation"]["$defs"]["query"]
        self.assertFalse(
            query_reconciliation["properties"]["arguments"]["additionalProperties"]
        )
        self.assertFalse(
            query_reconciliation["properties"]["expected"]["additionalProperties"]
        )
        self.assertEqual(len(contract["direct_tools"]), 14)
        tools_by_name = {tool["name"]: tool for tool in contract["direct_tools"]}
        for tool in contract["direct_tools"]:
            with self.subTest(tool=tool["name"]):
                for schema_name in ("input_schema", "output_schema"):
                    schema = tool[schema_name]
                    self.assertEqual(schema["type"], "object")
                    self.assertFalse(schema["additionalProperties"])
                    self.assertTrue(schema["properties"])
                self.assertIn("market", tool["input_schema"]["properties"])
                self.assertIn("trading_mode", tool["input_schema"]["properties"])
                self.assertIn("outcome", tool["output_schema"]["properties"])
                self.assertEqual(len(tool["output_schema"]["oneOf"]), 6)
                self.assertEqual(
                    set(tool["permission_refs"]),
                    set(tool["market_modes"]),
                )
                for branch in tool["output_schema"]["oneOf"]:
                    if "error" in branch.get("required", []):
                        self.assertFalse(
                            branch["properties"]["error"]["properties"]["retryable"]["const"]
                        )
        for tool_name in (
            "trade_place_batch_orders",
            "trade_cancel_batch_orders",
            "trade_place_tp_sl",
        ):
            result = tools_by_name[tool_name]["output_schema"]["properties"]["result"]
            self.assertIn("allOf", result["properties"]["items"]["items"])
            self.assertNotIn("x-item-identity-required", result)

    def test_reconciliation_result_closes_safe_evidence_and_binds_digest(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        result = contract["schema_components"]["reconciliation_result"]

        self.assertEqual(result["type"], "object")
        self.assertFalse(result["additionalProperties"])
        self.assertEqual(
            result["required"],
            [
                "tool_name",
                "prepared_artifact_digest",
                "status",
                "result",
                "observed_at",
                "evidence",
                "evidence_digest",
            ],
        )
        self.assertEqual(
            result["properties"]["status"]["enum"],
            [
                "RESOLVED_ACCEPTED",
                "UNRESOLVED",
                "MANUAL_REVIEW_REQUIRED",
            ],
        )
        conditions = validator._conditional_map(result["allOf"], "status")
        self.assertEqual(
            conditions["RESOLVED_ACCEPTED"]["then"]["properties"]["result"],
            {"const": "ACCEPTED"},
        )
        for status in ("UNRESOLVED", "MANUAL_REVIEW_REQUIRED"):
            self.assertEqual(
                conditions[status]["then"]["properties"]["result"],
                {"type": "null"},
            )

        definitions = result["$defs"]
        self.assertEqual(
            set(definitions),
            {
                "safe_observation",
                "query_evidence",
                "per_item_evidence",
                "manual_evidence",
            },
        )
        safe_observation = definitions["safe_observation"]
        self.assertFalse(safe_observation["additionalProperties"])
        self.assertEqual(
            set(safe_observation["properties"]),
            {
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
            },
        )
        self.assertNotIn("raw", safe_observation["properties"])
        self.assertNotIn("expected", safe_observation["properties"])
        self.assertNotIn("error_message", safe_observation["properties"])

        query = definitions["query_evidence"]
        self.assertFalse(query["additionalProperties"])
        self.assertEqual(query["properties"]["kind"], {"const": "query"})
        self.assertEqual(
            query["properties"]["read_tool"],
            {
                "type": "string",
                "enum": [
                    "account_get_orders",
                    "account_get_conditional_orders",
                    "account_get_symbol_config",
                    "account_get_positions",
                    "account_get_position",
                ],
            },
        )
        self.assertEqual(
            query["properties"]["error_code"]["oneOf"][0]["enum"],
            [
                "read_failed",
                "invalid_response",
                "identity_not_found",
                "identity_ambiguous",
                "search_incomplete",
                "state_mismatch",
            ],
        )
        per_item = definitions["per_item_evidence"]
        self.assertFalse(per_item["additionalProperties"])
        self.assertEqual(
            per_item["properties"]["items"]["items"]["properties"]["query"],
            {
                "$ref": (
                    "#/schema_components/reconciliation_result/$defs/"
                    "query_evidence"
                )
            },
        )
        manual = definitions["manual_evidence"]
        self.assertFalse(manual["additionalProperties"])
        self.assertEqual(manual["properties"]["kind"], {"const": "manual"})
        self.assertEqual(
            result["properties"]["evidence"]["oneOf"],
            [
                {
                    "$ref": (
                        "#/schema_components/reconciliation_result/$defs/"
                        "query_evidence"
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
                        "#/schema_components/reconciliation_result/$defs/"
                        "manual_evidence"
                    )
                },
            ],
        )
        self.assertEqual(
            result["x-evidence-digest"],
            {
                "algorithm": "sha256",
                "domain": "weex-mcp/reconciliation-evidence/v1\\0",
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
            },
        )

    def test_tracked_bundle_is_self_contained_and_valid(self) -> None:
        validator = load_validator()
        self.assertIsNotNone(validator, "MCP boundary validator is missing")
        report = validator.validate_bundle(ROOT)

        self.assertTrue(report["ok"])
        self.assertEqual(report["capabilities"], 79)
        self.assertEqual(report["field_decisions"], 2448)
        self.assertEqual(report["direct_tools"], 14)
        self.assertEqual(report["legacy_writes"], 21)
        self.assertEqual(report["auto_operations"], 6)
        self.assertEqual(report["removed_breaking"], 0)
        self.assertEqual(report["narrowed_replacements"], 151)
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(
            report["manual_intent_schema_digest"],
            json.loads(MANUAL_INTENT_PATH.read_text(encoding="utf-8"))[
                "schema_digest"
            ],
        )
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["approval"]["status"], "USER_CONFIRMED_CANDIDATE")
        self.assertEqual(contract["approval"]["approved_at"], "2026-11-27")
        self.assertIn("production", contract["approval"]["excluded"])

    def test_manual_intent_v2_schema_binds_fsm_reconcile_and_terminal_audit(self) -> None:
        validator = load_validator()
        schema = json.loads(MANUAL_INTENT_PATH.read_text(encoding="utf-8"))
        body = dict(schema)
        stored = body.pop("schema_digest")
        self.assertEqual(stored, validator.canonical_digest(body))
        self.assertEqual(schema["intent_version"], 2)
        self.assertEqual(
            schema["properties"]["contract_versions"]["properties"]["package"],
            {"const": "0.3.2"},
        )
        self.assertEqual(
            schema["properties"]["state"]["enum"],
            [
                "UNCLAIMED",
                "CONFIRMED",
                "CLAIMED",
                "NOT_SENT_TERMINAL",
                "DEFINITIVE_REJECTION",
                "ACCEPTED",
                "REVIEW_REQUIRED",
                "LEGACY_INVALIDATED",
            ],
        )
        self.assertIn("terminal_audit_record", schema["$defs"])
        self.assertEqual(
            schema["properties"]["reconciliation"]["properties"]["status"][
                "enum"
            ],
            [
                "NOT_REQUIRED",
                "PENDING",
                "RESOLVED_ACCEPTED",
                "RESOLVED_REJECTED",
                "RESOLVED_NOT_SENT",
                "UNRESOLVED",
                "MANUAL_REVIEW_REQUIRED",
            ],
        )
        self.assertEqual(
            schema["x-single-slot"]["blocking_states"],
            ["UNCLAIMED", "CONFIRMED", "CLAIMED", "REVIEW_REQUIRED"],
        )
        self.assertEqual(
            schema["x-single-slot"]["unclaimed_expiry_policy"],
            "expired_unclaimed_is_archivable_and_replaceable",
        )
        self.assertEqual(
            set(schema["properties"]["tool_name"]["enum"]),
            {
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
            },
        )
        self.assertEqual(len(schema["oneOf"]), 2)
        complete_v2, legacy_envelope = schema["oneOf"]
        self.assertIn("prepared_mutation", complete_v2["required"])
        self.assertNotIn("prepared_mutation", legacy_envelope["required"])
        legacy_forbidden = {
            rule["required"][0]
            for rule in legacy_envelope["not"]["anyOf"]
        }
        self.assertIn("prepared_mutation", legacy_forbidden)
        self.assertIn("grant", legacy_forbidden)
        self.assertEqual(
            legacy_envelope["properties"]["reconciliation"]["properties"][
                "template"
            ],
            {"type": "null"},
        )
        state_conditions = validator._conditional_map(schema["allOf"], "state")
        self.assertEqual(set(state_conditions), set(schema["properties"]["state"]["enum"]))
        reconciliation = schema["properties"]["reconciliation"]
        reconcile_conditions = validator._conditional_map(
            reconciliation["allOf"],
            "status",
        )
        self.assertEqual(
            set(reconcile_conditions),
            set(reconciliation["properties"]["status"]["enum"]),
        )
        self.assertEqual(
            reconcile_conditions["RESOLVED_ACCEPTED"]["then"]["properties"][
                "result"
            ],
            {"const": "ACCEPTED"},
        )
        audit_intent = schema["$defs"]["terminal_audit_record"]["properties"][
            "intent"
        ]
        self.assertEqual(len(audit_intent["allOf"][1]["oneOf"]), 2)
        self.assertIn(
            (
                "Skill persists the evidence_digest returned by MCP "
                "ReconciliationResult byte-for-byte and never computes or replaces it."
            ),
            schema["x-invariants"],
        )

    def test_validator_rejects_manual_intent_terminal_grant_drift(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        manual_intent = json.loads(MANUAL_INTENT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        states = validator._conditional_map(manual_intent["allOf"], "state")
        grant = states["ACCEPTED"]["then"]["oneOf"][0]["properties"]["grant"]
        grant["allOf"][1]["properties"]["state"] = {"const": "ISSUED"}
        manual_body = dict(manual_intent)
        manual_body.pop("schema_digest")
        manual_intent["schema_digest"] = validator.canonical_digest(manual_body)
        contract["manual_intent_contract"]["schema_digest"] = manual_intent[
            "schema_digest"
        ]
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(contract_body)

        with self.assertRaisesRegex(ValueError, "terminal grant"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                manual_intent=manual_intent,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_manual_intent_reconciliation_result_drift(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        manual_intent = json.loads(MANUAL_INTENT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        reconciliation = manual_intent["properties"]["reconciliation"]
        statuses = validator._conditional_map(reconciliation["allOf"], "status")
        statuses["RESOLVED_ACCEPTED"]["then"]["properties"]["result"] = {
            "const": "NOT_SENT"
        }
        manual_body = dict(manual_intent)
        manual_body.pop("schema_digest")
        manual_intent["schema_digest"] = validator.canonical_digest(manual_body)
        contract["manual_intent_contract"]["schema_digest"] = manual_intent[
            "schema_digest"
        ]
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(contract_body)

        with self.assertRaisesRegex(ValueError, "reconciliation result"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                manual_intent=manual_intent,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_open_reconciliation_evidence(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        contract["schema_components"]["reconciliation_result"]["$defs"][
            "safe_observation"
        ]["additionalProperties"] = True
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(
            contract_body
        )

        with self.assertRaisesRegex(ValueError, "safe observation.*closed"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_reconciliation_evidence_digest_drift(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        contract["schema_components"]["reconciliation_result"][
            "x-evidence-digest"
        ]["domain"] = "wrong-domain"
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(
            contract_body
        )

        with self.assertRaisesRegex(ValueError, "evidence digest contract"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_public_style_reconcile_parameters(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        contract["mcp_host_private_api"]["reconcile_mutation"]["parameters"] = [
            "intent_id",
            "reconciliation_template",
            "tool_name",
            "arguments",
            "invocation_context",
            "now_ms",
        ]
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(
            contract_body
        )

        with self.assertRaisesRegex(ValueError, "Host-private API"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_skill_owned_reconciliation_digest(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        manual_intent = json.loads(MANUAL_INTENT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        manual_intent["x-invariants"] = [
            value
            for value in manual_intent["x-invariants"]
            if "evidence_digest returned by MCP" not in value
        ]
        manual_body = dict(manual_intent)
        manual_body.pop("schema_digest")
        manual_intent["schema_digest"] = validator.canonical_digest(manual_body)
        contract["manual_intent_contract"]["schema_digest"] = manual_intent[
            "schema_digest"
        ]
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(
            contract_body
        )

        with self.assertRaisesRegex(ValueError, "binding invariants"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                manual_intent=manual_intent,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_manual_intent_component_version_drift(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        manual_intent = json.loads(MANUAL_INTENT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        manual_intent["properties"]["contract_versions"]["properties"]["grant"] = {
            "const": "9"
        }
        manual_body = dict(manual_intent)
        manual_body.pop("schema_digest")
        manual_intent["schema_digest"] = validator.canonical_digest(manual_body)
        contract["manual_intent_contract"]["schema_digest"] = manual_intent[
            "schema_digest"
        ]
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(
            contract_body
        )

        with self.assertRaisesRegex(ValueError, "manual intent contract versions"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                manual_intent=manual_intent,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_reconciliation_read_tool_schema_drift(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        contract["schema_components"]["reconciliation_result"]["$defs"][
            "query_evidence"
        ]["properties"]["read_tool"] = {"type": "string", "minLength": 1}
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(
            contract_body
        )

        with self.assertRaisesRegex(ValueError, "query evidence contract"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_reconciliation_owner_drift(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        contract["owners"]["mcp"].remove("outcome")
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(
            contract_body
        )

        with self.assertRaisesRegex(ValueError, "owner contract"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_v2_preserves_v1_direct_reconciliation_templates_byte_for_byte(self) -> None:
        validator = load_validator()
        historical = json.loads(CONTRACT_V1_PATH.read_text(encoding="utf-8"))
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        historical_templates = [
            tool["prepare_contract"]["reconciliation_template_schema"]
            for tool in historical["direct_tools"]
        ]
        current_templates = [
            tool["prepare_contract"]["reconciliation_template_schema"]
            for tool in contract["direct_tools"]
        ]

        self.assertEqual(current_templates, historical_templates)
        self.assertEqual(
            validator.canonical_digest(current_templates),
            "sha256:15f053ad9e955a19f6110f062a9d6e4fdd5d6290ffd612d283e11bb05b3b8337",
        )

    def test_validator_rejects_unresolved_manual_intent_reference(self) -> None:
        validator = load_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        manual_intent = json.loads(MANUAL_INTENT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        manual_intent["properties"]["prepared_mutation"]["$ref"] = (
            "mcp-boundary-contract-v2.json#/schema_components/missing"
        )
        manual_body = dict(manual_intent)
        manual_body.pop("schema_digest")
        manual_intent["schema_digest"] = validator.canonical_digest(manual_body)
        contract["manual_intent_contract"]["schema_digest"] = manual_intent[
            "schema_digest"
        ]
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(contract_body)

        with self.assertRaisesRegex(ValueError, "unresolved JSON Schema reference"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                manual_intent=manual_intent,
                inventory=inventory,
                matrix=matrix,
            )

    def test_historical_source_evidence_is_bound_and_current_audit_detects_drift(self) -> None:
        validator = load_validator()
        self.assertIsNotNone(validator, "MCP boundary validator is missing")

        report = validator.validate_bundle(ROOT)
        self.assertEqual(report["source_evidence_mode"], "historical")

        with self.assertRaisesRegex(ValueError, "source evidence digest mismatch: gateway"):
            validator.validate_bundle(
                ROOT,
                verify_current_source_evidence=True,
            )

        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        contract["compatibility"]["source_evidence_digests"]["gateway"] = (
            "sha256:" + "0" * 64
        )
        contract_body = dict(contract)
        contract_body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(
            contract_body
        )
        with self.assertRaisesRegex(ValueError, "historical source evidence"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

        source_paths = validator._source_evidence_paths(ROOT)
        expected = inventory["source_evidence_digests"]
        original_file_digest = validator.file_digest

        def controlled_digest(path: Path) -> str:
            for name, source_path in source_paths.items():
                if path == source_path:
                    return (
                        "sha256:" + "f" * 64
                        if name == "partner_field_catalog"
                        else expected[name]
                    )
            return original_file_digest(path)

        with mock.patch.object(
            validator,
            "file_digest",
            side_effect=controlled_digest,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "source evidence digest mismatch: partner_field_catalog",
            ):
                validator.validate_documents(
                    skill_root=ROOT,
                    contract=json.loads(CONTRACT_PATH.read_text(encoding="utf-8")),
                    inventory=inventory,
                    matrix=matrix,
                    verify_current_source_evidence=True,
                )

    def test_narrowed_replacements_are_explicit(self) -> None:
        validator = load_validator()
        self.assertIsNotNone(validator, "MCP boundary validator is missing")
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))

        policies = contract["replacement_policies"]
        self.assertEqual(set(policies), {"BC-ERR-001", "BC-PARTNER-001"})
        replacements = [
            field
            for entry in matrix["entries"]
            for field in entry["fields"]
            if field.get("compatibility") == "narrowed_typed_replacement"
        ]
        self.assertEqual(len(replacements), 151)
        for field in replacements:
            self.assertIn(field["replacement_change_id"], policies)
            self.assertTrue(field["release_note_ref"])
            self.assertTrue(field["compat_test_ref"])

    def test_tampered_target_path_is_rejected(self) -> None:
        validator = load_validator()
        self.assertIsNotNone(validator, "MCP boundary validator is missing")
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        matrix["entries"][0]["fields"][0]["source_path"] = "/raw"
        matrix["matrix_digest"] = validator.canonical_digest(
            validator.matrix_digest_body(matrix)
        )

        with self.assertRaisesRegex(ValueError, "target output schema|raw"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_cross_tool_target_path_is_rejected_even_when_globally_valid(self) -> None:
        validator = load_validator()
        self.assertIsNotNone(validator, "MCP boundary validator is missing")
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        by_key = {entry["legacy_key"]: entry for entry in matrix["entries"]}
        field = next(
            item
            for item in by_key["spot.order.place_order"]["fields"]
            if item["scenario"] == "success"
            and item["legacy_path"] == "/data/orderId"
        )
        field["source_path"] = "/structured_content/result/items/*/upstream_order_id"
        matrix["matrix_digest"] = validator.canonical_digest(
            validator.matrix_digest_body(matrix)
        )

        with self.assertRaisesRegex(ValueError, "capability target output schema"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_cross_repo_pair_validator_detects_metadata_drift(self) -> None:
        pair_validator = load_pair_validator()
        self.assertIsNotNone(pair_validator, "cross-repo pair validator is missing")
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        expected = pair_validator.expected_mcp_metadata(contract)
        self.assertEqual(
            expected["source_mcp_baseline_commit"],
            "e684b575bb7b7c44edd4f2a897137d020a79b4fb",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            mcp_root = Path(temp_dir) / "mcp"
            write_mcp_pair_fixture(
                mcp_root,
                contract=contract,
                pair_validator=pair_validator,
            )
            report = pair_validator.verify_pair(ROOT, mcp_root)
            self.assertTrue(report["ok"])
            self.assertEqual(report["phase"], "source_contract")
            compatibility = mcp_root / "src" / "weex_mcp" / "compatibility.py"
            compatibility.write_text(
                compatibility.read_text(encoding="utf-8").replace(
                    "'status': 'CANDIDATE_NEEDS_IMPLEMENTATION'",
                    "'status': 'drifted'",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "pair metadata mismatch"):
                pair_validator.verify_pair(ROOT, mcp_root)

    def test_cross_repo_pair_validator_checks_runtime_package_and_repository_surfaces(
        self,
    ) -> None:
        pair_validator = load_pair_validator()
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp_dir:
            mcp_root = Path(temp_dir) / "mcp"
            write_mcp_pair_fixture(
                mcp_root,
                contract=contract,
                pair_validator=pair_validator,
            )
            compatibility = mcp_root / "src" / "weex_mcp" / "compatibility.py"
            version_path = compatibility.parent / "_version.py"
            server_path = mcp_root / "server.json"
            pyproject_path = mcp_root / "pyproject.toml"

            original = compatibility.read_text(encoding="utf-8")
            compatibility.write_text(
                original.replace("'package': '0.3.2'", "'package': '9.9.9'", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "runtime candidate versions"):
                pair_validator.verify_pair(ROOT, mcp_root)
            compatibility.write_text(original, encoding="utf-8")

            version_path.write_text("__version__ = '9.9.9'\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "_version.py"):
                pair_validator.verify_pair(ROOT, mcp_root)
            version_path.write_text("__version__ = '0.3.2'\n", encoding="utf-8")

            server = json.loads(server_path.read_text(encoding="utf-8"))
            server["packages"][0]["version"] = "9.9.9"
            server_path.write_text(json.dumps(server), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "server.json package version"):
                pair_validator.verify_pair(ROOT, mcp_root)
            server["packages"][0]["version"] = "0.3.2"
            server["repository"]["url"] = "https://github.com/example/fork"
            server_path.write_text(json.dumps(server), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "official repository"):
                pair_validator.verify_pair(ROOT, mcp_root)
            server["repository"]["url"] = OFFICIAL_MCP_REPOSITORY
            server_path.write_text(json.dumps(server), encoding="utf-8")

            pyproject_path.write_text(
                pyproject_path.read_text(encoding="utf-8").replace(
                    f"Issues = '{OFFICIAL_MCP_REPOSITORY}/issues'",
                    "Issues = 'https://github.com/example/fork/issues'",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "official repository URLs"):
                pair_validator.verify_pair(ROOT, mcp_root)

    def test_pair_source_phase_does_not_require_future_artifact_hashes(self) -> None:
        validator = load_validator()
        pair_validator = load_pair_validator()
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_root = Path(temp_dir) / "skill"
            mcp_root = Path(temp_dir) / "mcp"
            copy_pair_skill_fixture(validator, skill_root)
            mapping_path = skill_root / "references" / MAPPING_PATH.name
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            mapping["verified_mcp_candidate"].pop("distribution_sha256")
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
            contract = json.loads(
                (skill_root / "references" / CONTRACT_PATH.name).read_text(
                    encoding="utf-8"
                )
            )
            write_mcp_pair_fixture(
                mcp_root,
                contract=contract,
                pair_validator=pair_validator,
            )

            report = pair_validator.verify_pair(skill_root, mcp_root)

        self.assertEqual(report["phase"], "source_contract")

    def test_pair_artifact_pin_phase_validates_mapping_top_candidate(self) -> None:
        validator = load_validator()
        pair_validator = load_pair_validator()
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_root = Path(temp_dir) / "skill"
            mcp_root = Path(temp_dir) / "mcp"
            copy_pair_skill_fixture(validator, skill_root)
            contract = json.loads(
                (skill_root / "references" / CONTRACT_PATH.name).read_text(
                    encoding="utf-8"
                )
            )
            write_mcp_pair_fixture(
                mcp_root,
                contract=contract,
                pair_validator=pair_validator,
            )
            mapping_path = skill_root / "references" / MAPPING_PATH.name
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            candidate = mapping["verified_mcp_candidate"]
            candidate["package_version"] = "0.3.1"
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "artifact pin package"):
                pair_validator.verify_pair(
                    skill_root,
                    mcp_root,
                    require_artifact_pin=True,
                )

            candidate.update(
                {
                    "official_repository": OFFICIAL_MCP_REPOSITORY,
                    "package_version": contract["candidate_versions"]["package"],
                    "tool_contract_version": contract["candidate_versions"][
                        "tool_contract"
                    ],
                    "manifest_version": int(
                        contract["candidate_versions"]["manifest"]
                    ),
                    "source_commit": "f" * 40,
                    "versions": {
                        key: value
                        for key, value in contract["candidate_versions"].items()
                        if key not in {"intent", "status"}
                    },
                    "boundary_contract_digest": contract[
                        "boundary_contract_digest"
                    ],
                    "direct_tools_digest": contract["direct_tools_digest"],
                    "legacy_mapping_digest": contract["compatibility"][
                        "legacy_mapping_digest"
                    ],
                    "inventory_digest": contract["compatibility"][
                        "inventory_digest"
                    ],
                    "matrix_digest": contract["compatibility"]["matrix_digest"],
                    "source_catalog_digests": contract["compatibility"][
                        "source_catalog_digests"
                    ],
                }
            )
            candidate.pop("distribution_sha256")
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "distribution hashes"):
                pair_validator.verify_pair(
                    skill_root,
                    mcp_root,
                    require_artifact_pin=True,
                )

            candidate["distribution_sha256"] = {
                "wheel": "a" * 64,
                "sdist": "b" * 64,
            }
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
            report = pair_validator.verify_pair(
                skill_root,
                mcp_root,
                require_artifact_pin=True,
            )

        self.assertEqual(report["phase"], "artifact_pin")
        self.assertEqual(report["artifact_source_commit"], "f" * 40)

    def test_cross_repo_pair_validator_normalizes_malformed_contract_errors(self) -> None:
        pair_validator = load_pair_validator()
        self.assertIsNotNone(pair_validator, "cross-repo pair validator is missing")

        with self.assertRaisesRegex(ValueError, "candidate metadata"):
            pair_validator.expected_mcp_metadata({})

    def test_validator_rejects_incomplete_permission_matrix(self) -> None:
        validator = load_validator()
        self.assertIsNotNone(validator, "MCP boundary validator is missing")
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        contract["permissions"]["market"].pop("market_ping")
        body = dict(contract)
        body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(body)

        with self.assertRaisesRegex(ValueError, "permission matrix"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_same_size_permission_key_drift(self) -> None:
        validator = load_validator()
        self.assertIsNotNone(validator, "MCP boundary validator is missing")
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        entry = contract["permissions"]["market"].pop("market_ping")
        contract["permissions"]["market"]["market_fake"] = entry
        body = dict(contract)
        body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(body)

        with self.assertRaisesRegex(ValueError, "permission matrix keys"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_incomplete_direct_tool_schema(self) -> None:
        validator = load_validator()
        self.assertIsNotNone(validator, "MCP boundary validator is missing")
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        contract["direct_tools"][0]["input_schema"]["properties"].pop("market")
        contract["direct_tools_digest"] = validator.canonical_digest(
            contract["direct_tools"]
        )
        body = dict(contract)
        body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(body)

        with self.assertRaisesRegex(ValueError, "input schema"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_validator_rejects_claim_failure_guarantee_drift(self) -> None:
        validator = load_validator()
        self.assertIsNotNone(validator, "MCP boundary validator is missing")
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        contract["grant_consumer_api"]["claim"]["strong_failure_guarantee"][
            "raised_exception"
        ] = "review_required"
        body = dict(contract)
        body.pop("boundary_contract_digest")
        contract["boundary_contract_digest"] = validator.canonical_digest(body)

        with self.assertRaisesRegex(ValueError, "claim failure guarantee"):
            validator.validate_documents(
                skill_root=ROOT,
                contract=contract,
                inventory=inventory,
                matrix=matrix,
            )

    def test_bundle_validation_does_not_read_ignored_requirement_directories(self) -> None:
        validator = load_validator()
        self.assertIsNotNone(validator, "MCP boundary validator is missing")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir) / "weex-trader-skill"
            validator.copy_validation_fixture(ROOT, temp_root)
            report = validator.validate_bundle(temp_root)

        self.assertTrue(report["ok"])


if __name__ == "__main__":
    unittest.main()
