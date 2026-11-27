#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_REL = Path("references/mcp-boundary-contract-v2.json")
BOUNDARY_VALIDATOR_REL = Path("scripts/validate_mcp_boundary_contract.py")
MCP_METADATA_REL = Path("src/weex_mcp/compatibility.py")
MCP_VERSION_REL = Path("src/weex_mcp/_version.py")
MCP_SERVER_REL = Path("server.json")
MCP_PYPROJECT_REL = Path("pyproject.toml")
MAPPING_REL = Path("references/mcp-capability-mapping.json")
OFFICIAL_MCP_REPOSITORY = "https://github.com/weex-labs/weex-mcp"
OFFICIAL_MCP_URLS = {
    "Homepage": OFFICIAL_MCP_REPOSITORY,
    "Repository": OFFICIAL_MCP_REPOSITORY,
    "Issues": f"{OFFICIAL_MCP_REPOSITORY}/issues",
    "Documentation": f"{OFFICIAL_MCP_REPOSITORY}#readme",
}
HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


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
            "source_mcp_baseline_commit": source_baselines["mcp_commit"],
            "source_openapi_main": source_baselines["openapi_main"],
        }
    except (KeyError, TypeError) as exc:
        raise ValueError("boundary contract candidate metadata is incomplete") from exc


def expected_runtime_versions(contract: dict[str, Any]) -> dict[str, str]:
    try:
        versions = contract["candidate_versions"]
        if not isinstance(versions, dict):
            raise TypeError("candidate versions must be an object")
        expected = {
            key: value
            for key, value in versions.items()
            if key not in {"intent", "status"}
        }
        if not expected or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in expected.items()
        ):
            raise TypeError("runtime versions must contain strings")
        return expected
    except (KeyError, TypeError) as exc:
        raise ValueError("boundary contract runtime versions are incomplete") from exc


def read_literal_assignment(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            assignments.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            assignments.append(node.value)
    if len(assignments) != 1:
        raise ValueError(f"{path.name} must define one {name} assignment")
    try:
        return ast.literal_eval(assignments[0])
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{path.name} {name} must be a literal") from exc


def read_mcp_metadata(path: Path) -> dict[str, Any]:
    value = read_literal_assignment(path, "BOUNDARY_CONTRACT_CANDIDATE")
    if not isinstance(value, dict):
        raise ValueError("MCP boundary contract candidate metadata must be an object")
    return value


def validate_mcp_package_surfaces(
    mcp_root: Path,
    *,
    contract: dict[str, Any],
) -> None:
    package_version = contract.get("candidate_versions", {}).get("package")
    if not isinstance(package_version, str):
        raise ValueError("boundary contract package version is incomplete")
    compatibility_path = mcp_root / MCP_METADATA_REL
    runtime_versions = read_literal_assignment(
        compatibility_path,
        "_BOUNDARY_CANDIDATE_VERSIONS",
    )
    if runtime_versions != expected_runtime_versions(contract):
        raise ValueError("MCP runtime candidate versions mismatch")
    source_version = read_literal_assignment(mcp_root / MCP_VERSION_REL, "__version__")
    if source_version != package_version:
        raise ValueError("MCP _version.py package version mismatch")

    server = json.loads((mcp_root / MCP_SERVER_REL).read_text(encoding="utf-8"))
    if not isinstance(server, dict) or server.get("version") != package_version:
        raise ValueError("MCP server.json root version mismatch")
    packages = server.get("packages")
    if (
        not isinstance(packages, list)
        or len(packages) != 1
        or not isinstance(packages[0], dict)
        or packages[0].get("version") != package_version
    ):
        raise ValueError("MCP server.json package version mismatch")
    repository = server.get("repository")
    if (
        not isinstance(repository, dict)
        or repository.get("source") != "github"
        or repository.get("url") != OFFICIAL_MCP_REPOSITORY
    ):
        raise ValueError("MCP server.json must use the official repository")

    pyproject = tomllib.loads(
        (mcp_root / MCP_PYPROJECT_REL).read_text(encoding="utf-8")
    )
    project = pyproject.get("project")
    urls = project.get("urls") if isinstance(project, dict) else None
    if urls != OFFICIAL_MCP_URLS:
        raise ValueError("MCP pyproject official repository URLs mismatch")


def validate_artifact_pin_candidate(
    *,
    contract: dict[str, Any],
    mapping: dict[str, Any],
) -> dict[str, Any]:
    candidate = mapping.get("verified_mcp_candidate")
    if not isinstance(candidate, dict):
        raise ValueError("Trader artifact pin candidate is missing")
    package_version = contract.get("candidate_versions", {}).get("package")
    if candidate.get("package_version") != package_version:
        raise ValueError("Trader artifact pin package mismatch")
    expected_keys = {
        "official_repository",
        "package_version",
        "tool_contract_version",
        "manifest_version",
        "source_commit",
        "publication_status",
        "distribution_sha256",
        "versions",
        "boundary_contract_digest",
        "direct_tools_digest",
        "legacy_mapping_digest",
        "inventory_digest",
        "matrix_digest",
        "source_catalog_digests",
        "compatibility_profiles",
    }
    if set(candidate) - expected_keys or (
        expected_keys - {"distribution_sha256"}
    ) - set(candidate):
        raise ValueError("Trader artifact pin candidate fields mismatch")
    versions = expected_runtime_versions(contract)
    compatibility = contract.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ValueError("boundary contract compatibility metadata is incomplete")
    if (
        candidate.get("official_repository") != OFFICIAL_MCP_REPOSITORY
        or candidate.get("tool_contract_version") != versions.get("tool_contract")
        or candidate.get("manifest_version") != int(versions["manifest"])
        or candidate.get("publication_status") != "unpublished"
        or candidate.get("versions") != versions
        or candidate.get("boundary_contract_digest")
        != contract.get("boundary_contract_digest")
        or candidate.get("direct_tools_digest") != contract.get("direct_tools_digest")
        or candidate.get("legacy_mapping_digest")
        != compatibility.get("legacy_mapping_digest")
        or candidate.get("inventory_digest") != compatibility.get("inventory_digest")
        or candidate.get("matrix_digest") != compatibility.get("matrix_digest")
        or candidate.get("source_catalog_digests")
        != compatibility.get("source_catalog_digests")
    ):
        raise ValueError("Trader artifact pin candidate metadata mismatch")
    source_commit = candidate.get("source_commit")
    if not isinstance(source_commit, str) or COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ValueError("Trader artifact pin source commit is invalid")
    distributions = candidate.get("distribution_sha256")
    if (
        not isinstance(distributions, dict)
        or set(distributions) != {"wheel", "sdist"}
        or any(
            not isinstance(value, str) or HEX_64_PATTERN.fullmatch(value) is None
            for value in distributions.values()
        )
    ):
        raise ValueError("Trader artifact pin distribution hashes are invalid")
    profiles = candidate.get("compatibility_profiles")
    if not isinstance(profiles, list) or len(profiles) != 2:
        raise ValueError("Trader artifact pin compatibility profiles are invalid")
    expected_profile_fields = {
        "runtime_profile",
        "trade_enabled",
        "tool_count",
        "tool_set_digest",
        "schema_digest",
        "manifest_digest",
        "boundary_static_manifest_digest",
    }
    seen_trade_modes: set[bool] = set()
    for profile in profiles:
        if (
            not isinstance(profile, dict)
            or set(profile) != expected_profile_fields
            or profile.get("runtime_profile") != "local_user"
            or not isinstance(profile.get("trade_enabled"), bool)
            or isinstance(profile.get("tool_count"), bool)
            or not isinstance(profile.get("tool_count"), int)
            or profile["tool_count"] <= 0
            or any(
                not isinstance(profile.get(field), str)
                or DIGEST_PATTERN.fullmatch(profile[field]) is None
                for field in (
                    "tool_set_digest",
                    "schema_digest",
                    "manifest_digest",
                    "boundary_static_manifest_digest",
                )
            )
        ):
            raise ValueError("Trader artifact pin compatibility profiles are invalid")
        seen_trade_modes.add(profile["trade_enabled"])
    if seen_trade_modes != {False, True}:
        raise ValueError("Trader artifact pin compatibility profile matrix is incomplete")
    return candidate


def verify_pair(
    skill_root: Path,
    mcp_root: Path,
    *,
    require_artifact_pin: bool = False,
) -> dict[str, Any]:
    skill_root = skill_root.resolve()
    mcp_root = mcp_root.resolve()
    validator = load_boundary_validator(skill_root)
    validator.validate_bundle(skill_root)
    contract = load_contract(skill_root)
    expected = expected_mcp_metadata(contract)
    actual = read_mcp_metadata(mcp_root / MCP_METADATA_REL)
    if actual != expected:
        raise ValueError("MCP/Skill pair metadata mismatch")
    validate_mcp_package_surfaces(mcp_root, contract=contract)
    report = {
        "ok": True,
        "phase": "artifact_pin" if require_artifact_pin else "source_contract",
        "boundary_contract_digest": expected["boundary_contract_digest"],
        "direct_tools_digest": expected["direct_tools_digest"],
        "inventory_digest": expected["inventory_digest"],
        "matrix_digest": expected["matrix_digest"],
        "source_skill_baseline_commit": expected["source_skill_baseline_commit"],
        "source_mcp_baseline_commit": expected["source_mcp_baseline_commit"],
        "source_openapi_main": expected["source_openapi_main"],
    }
    if require_artifact_pin:
        mapping = json.loads((skill_root / MAPPING_REL).read_text(encoding="utf-8"))
        if not isinstance(mapping, dict):
            raise ValueError("Trader capability mapping must contain an object")
        candidate = validate_artifact_pin_candidate(
            contract=contract,
            mapping=mapping,
        )
        report["artifact_source_commit"] = candidate["source_commit"]
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the tracked Trader boundary contract against one WEEX MCP checkout."
    )
    parser.add_argument("--mcp-root", required=True, type=Path)
    parser.add_argument("--skill-root", default=SKILL_ROOT, type=Path)
    parser.add_argument(
        "--require-artifact-pin",
        action="store_true",
        help=(
            "Also require the Trader mapping top candidate and its wheel/sdist "
            "hashes to match the source contract."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = verify_pair(
            args.skill_root,
            args.mcp_root,
            require_artifact_pin=args.require_artifact_pin,
        )
    except (KeyError, OSError, TypeError, ValueError, SyntaxError, json.JSONDecodeError) as exc:
        print(f"MCP/Skill boundary pair validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
