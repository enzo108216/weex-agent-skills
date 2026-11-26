#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_REL = Path("references/mcp-boundary-contract-v1.json")
BOUNDARY_VALIDATOR_REL = Path("scripts/validate_mcp_boundary_contract.py")
MCP_METADATA_REL = Path("src/weex_mcp/compatibility.py")


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def load_contract(skill_root: Path) -> dict[str, Any]:
    value = json.loads((skill_root / CONTRACT_REL).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("MCP boundary contract must contain an object")
    body = dict(value)
    stored_digest = body.pop("boundary_contract_digest", None)
    if stored_digest != canonical_digest(body):
        raise ValueError("MCP boundary contract digest mismatch")
    return value


def load_boundary_validator(skill_root: Path):
    path = skill_root / BOUNDARY_VALIDATOR_REL
    spec = importlib.util.spec_from_file_location("verify_mcp_boundary_pair_validator", path)
    if spec is None or spec.loader is None:
        raise ValueError("unable to load MCP boundary validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_mcp_metadata(contract: dict[str, Any]) -> dict[str, Any]:
    try:
        compatibility = contract["compatibility"]
        source_baselines = contract["source_baselines"]
        if not isinstance(compatibility, dict) or not isinstance(source_baselines, dict):
            raise TypeError("metadata sections must be objects")
        return {
            "status": contract["status"],
            "schema_version": contract["schema_version"],
            "boundary_contract_digest": contract["boundary_contract_digest"],
            "direct_tools_digest": contract["direct_tools_digest"],
            "legacy_mapping_digest": compatibility["legacy_mapping_digest"],
            "inventory_digest": compatibility["inventory_digest"],
            "matrix_digest": compatibility["matrix_digest"],
            "replacement_change_ids": sorted(contract["replacement_policies"]),
            "source_skill_baseline_commit": source_baselines["skill_commit"],
            "source_openapi_main": source_baselines["openapi_main"],
        }
    except (KeyError, TypeError) as exc:
        raise ValueError("boundary contract candidate metadata is incomplete") from exc


def read_mcp_metadata(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "BOUNDARY_CONTRACT_CANDIDATE"
            for target in node.targets
        ):
            assignments.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "BOUNDARY_CONTRACT_CANDIDATE"
            and node.value is not None
        ):
            assignments.append(node.value)
    if len(assignments) != 1:
        raise ValueError("MCP compatibility module must define one boundary contract candidate")
    value = ast.literal_eval(assignments[0])
    if not isinstance(value, dict):
        raise ValueError("MCP boundary contract candidate metadata must be an object")
    return value


def verify_pair(skill_root: Path, mcp_root: Path) -> dict[str, Any]:
    skill_root = skill_root.resolve()
    mcp_root = mcp_root.resolve()
    validator = load_boundary_validator(skill_root)
    validator.validate_bundle(skill_root)
    contract = load_contract(skill_root)
    expected = expected_mcp_metadata(contract)
    actual = read_mcp_metadata(mcp_root / MCP_METADATA_REL)
    if actual != expected:
        raise ValueError("MCP/Skill pair metadata mismatch")
    return {
        "ok": True,
        "boundary_contract_digest": expected["boundary_contract_digest"],
        "direct_tools_digest": expected["direct_tools_digest"],
        "inventory_digest": expected["inventory_digest"],
        "matrix_digest": expected["matrix_digest"],
        "source_skill_baseline_commit": expected["source_skill_baseline_commit"],
        "source_openapi_main": expected["source_openapi_main"],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the tracked Trader boundary contract against one WEEX MCP checkout."
    )
    parser.add_argument("--mcp-root", required=True, type=Path)
    parser.add_argument("--skill-root", default=SKILL_ROOT, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = verify_pair(args.skill_root, args.mcp_root)
    except (OSError, ValueError, SyntaxError, json.JSONDecodeError) as exc:
        print(f"MCP/Skill boundary pair validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
