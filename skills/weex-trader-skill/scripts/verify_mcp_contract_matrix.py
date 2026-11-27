#!/usr/bin/env python3
"""Verify that two exact local Trader revisions only accept their own MCP wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
from pathlib import Path
from typing import Any


_TRADER_PATH = Path("skills/weex-trader-skill")
_EVIDENCE_FILE = "mcp-contract-matrix.json"
_LABELS = ("old", "candidate")
_MODES = (False, True)
_MISMATCH_ACTION = "install_verified_mcp_candidate"
_CHILD = """
import json
import sys
from pathlib import Path

import weex_mcp_gateway as gateway

if Path(gateway.__file__).resolve() != Path(sys.argv[1]) / "weex_mcp_gateway.py":
    raise RuntimeError("Unexpected Trader import")
report = gateway.probe_local_mcp_runtime(trade_enabled=sys.argv[2] == "true")
print(json.dumps(report, allow_nan=False, separators=(",", ":")))
"""


def _matches(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _is_digest(value: Any) -> bool:
    return _matches(value, r"sha256:[0-9a-f]{64}")


def _validate_matrix_cases(cases: list[dict[str, Any]]) -> bool:
    remaining = {(skill, wheel, mode) for skill in _LABELS for wheel in _LABELS for mode in _MODES}
    skill_ids: dict[str, tuple[str, ...]] = {}
    wheel_ids: dict[str, tuple[str, ...]] = {}
    for case in cases:
        if not isinstance(case, dict):
            return False
        skill, wheel, mode = (case.get(key) for key in ("skill", "wheel", "trade_enabled"))
        if skill not in _LABELS or wheel not in _LABELS or type(mode) is not bool:
            return False
        key = (skill, wheel, mode)
        if key not in remaining:
            return False
        remaining.remove(key)
        matching = skill == wheel
        if (
            type(case.get("process_exit")) is not int
            or case["process_exit"] != 0
            or case.get("result") != ("ready" if matching else "contract_mismatch")
            or case.get("package_ok") is not matching
            or case.get("manifest_ok") is not matching
            or "action_required" not in case
            or case["action_required"] != (None if matching else _MISMATCH_ACTION)
            or any(type(case.get(field)) is not bool for field in ("private_read_ready", "mutation_ready"))
            or not _matches(case.get("skill_commit"), r"[0-9a-f]{40}")
            or not _matches(case.get("wheel_sha256"), r"[0-9a-f]{64}")
            or not _matches(case.get("package_version"), r"[0-9]+\.[0-9]+\.[0-9]+")
            or not _matches(case.get("expected_package_version"), r"[0-9]+\.[0-9]+\.[0-9]+")
            or (case["package_version"] == case["expected_package_version"]) != matching
        ):
            return False
        provenance = ("mapping_digest", "inventory_digest", "matrix_digest")
        manifests = ("manifest_digest", "boundary_static_manifest_digest")
        if not all(_is_digest(case.get(field)) for field in provenance):
            return False
        if matching:
            if not all(_is_digest(case.get(field)) for field in manifests):
                return False
        elif (
            any(field not in case or case[field] is not None for field in manifests)
            or case["private_read_ready"]
            or case["mutation_ready"]
        ):
            return False

        skill_id = (
            case["skill_commit"], case["expected_package_version"],
            *(case[field] for field in provenance),
        )
        wheel_id = (case["wheel_sha256"], case["package_version"])
        if skill_ids.setdefault(skill, skill_id) != skill_id or wheel_ids.setdefault(wheel, wheel_id) != wheel_id:
            return False
    return (
        not remaining
        and skill_ids["old"][0] != skill_ids["candidate"][0]
        and wheel_ids["old"][0] != wheel_ids["candidate"][0]
    )


def _file_hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _archive_revision(repository: Path, revision: str, destination: Path) -> str:
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    git_env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1")
    git = ["git", "-C", str(repository)]
    commit = subprocess.run(
        [*git, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
        env=git_env, capture_output=True, text=True, check=True, timeout=30,
    ).stdout.strip()
    if not _matches(commit, r"[0-9a-f]{40}"):
        raise ValueError("Skill revision did not resolve to an exact Git commit")
    destination.mkdir()
    with tempfile.TemporaryFile() as archive:
        subprocess.run(
            [*git, "archive", "--format=tar", commit], env=git_env, stdout=archive,
            stderr=subprocess.PIPE, check=True, timeout=60,
        )
        archive.seek(0)
        with tarfile.open(fileobj=archive) as source:
            source.extractall(destination, filter="data")
    return commit


def _read_pin(trader: Path) -> dict[str, Any]:
    references = trader / "references"
    mapping_path = references / "mcp-capability-mapping.json"
    pin = _load_object(mapping_path)["verified_mcp_candidate"]
    wheel_hash = pin["distribution_sha256"]["wheel"]
    if not _matches(wheel_hash, r"[0-9a-f]{64}") or not _matches(pin["package_version"], r"[0-9]+\.[0-9]+\.[0-9]+"):
        raise ValueError("Trader revision does not contain an exact MCP wheel pin")
    for field, filename in (
        ("inventory_digest", "mcp-compat-field-inventory-v1.json"),
        ("matrix_digest", "mcp-compat-field-source-matrix-v1.json"),
    ):
        if not _is_digest(pin[field]) or _load_object(references / filename)[field] != pin[field]:
            raise ValueError(f"Trader revision has a mismatched {field}")
    profiles = {}
    for profile in pin["compatibility_profiles"]:
        mode = profile["trade_enabled"]
        if (
            type(mode) is not bool
            or mode in profiles
            or not _is_digest(profile["manifest_digest"])
            or not _is_digest(profile["boundary_static_manifest_digest"])
        ):
            raise ValueError("Trader revision has invalid compatibility profile evidence")
        profiles[mode] = profile
    if set(profiles) != set(_MODES):
        raise ValueError("Trader revision is missing a compatibility profile")
    return {
        "version": pin["package_version"],
        "wheel_sha256": wheel_hash,
        "mapping_digest": "sha256:" + _file_hash(mapping_path),
        "inventory_digest": pin["inventory_digest"],
        "matrix_digest": pin["matrix_digest"],
        "profiles": profiles,
    }


def _python_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    return env


def _install_pinned_wheel(wheel: Path, wheel_hash: str, workdir: Path) -> Path:
    if wheel.suffix != ".whl" or not wheel.is_file():
        raise ValueError("MCP artifact must be a local wheel file")
    workdir.mkdir()
    staged_wheel = workdir / wheel.name
    shutil.copyfile(wheel, staged_wheel)
    if _file_hash(staged_wheel) != wheel_hash:
        raise ValueError("MCP wheel hash does not match the archived Trader pin")
    venv_dir = workdir / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    # Keep this venv path instead of resolving the executable's symlink.
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            str(python), "-B", "-s", "-P", "-m", "pip", "--isolated",
            "--disable-pip-version-check", "--no-input", "install", "--no-index", "--no-deps",
            "--no-cache-dir", "--require-hashes", f"{staged_wheel.as_uri()}#sha256={wheel_hash}",
        ],
        cwd=workdir, env=_python_environment(), capture_output=True, check=True, timeout=60,
    )
    return python


def _probe_case(scripts: Path, python: Path, *, trade_enabled: bool) -> tuple[int | None, dict[str, Any]]:
    env = _python_environment()
    env["PYTHONPATH"] = str(scripts)
    try:
        completed = subprocess.run(
            [str(python), "-B", "-s", "-P", "-c", _CHILD, str(scripts), str(trade_enabled).lower()],
            cwd=python.parent, env=env, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, {}
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError:
        status = {}
    return completed.returncode, status if isinstance(status, dict) else {}


def _matrix_case(
    skill: str, wheel: str, trader: Path, python: Path, commit: str,
    skill_pin: dict[str, Any], wheel_pin: dict[str, Any], *, trade_enabled: bool,
) -> dict[str, Any]:
    exit_code, status = _probe_case(trader / "scripts", python, trade_enabled=trade_enabled)
    profile = skill_pin["profiles"][trade_enabled]
    readable_report = (
        exit_code == 0
        and status.get("version") == wheel_pin["version"]
        and status.get("expected_version") == skill_pin["version"]
        and status.get("trade_enabled") is trade_enabled
    )
    result = "failed"
    if readable_report and skill == wheel and (
        status.get("package_ok") is True
        and status.get("manifest_ok") is True
        and "action_required" in status
        and status["action_required"] is None
        and status.get("manifest_digest") == profile["manifest_digest"]
        and status.get("boundary_manifest_digest") == profile["boundary_static_manifest_digest"]
    ):
        result = "ready"
    elif readable_report and skill != wheel and (
        status.get("package_ok") is False
        and status.get("manifest_ok") is False
        and status.get("private_read_ready") is False
        and status.get("mutation_ready") is False
        and status.get("action_required") == _MISMATCH_ACTION
    ):
        result = "contract_mismatch"
    return {
        "skill": skill,
        "wheel": wheel,
        "trade_enabled": trade_enabled,
        "skill_commit": commit,
        "wheel_sha256": wheel_pin["wheel_sha256"],
        "package_version": status.get("version"),
        "expected_package_version": skill_pin["version"],
        "manifest_digest": status.get("manifest_digest") if result == "ready" else None,
        "boundary_static_manifest_digest": status.get("boundary_manifest_digest") if result == "ready" else None,
        "expected_manifest_digest": profile["manifest_digest"],
        "expected_boundary_static_manifest_digest": profile["boundary_static_manifest_digest"],
        "mapping_digest": skill_pin["mapping_digest"],
        "inventory_digest": skill_pin["inventory_digest"],
        "matrix_digest": skill_pin["matrix_digest"],
        "process_exit": exit_code,
        "result": result,
        **{
            field: status.get(field) for field in (
                "package_ok", "manifest_ok", "private_read_ready", "mutation_ready", "action_required",
            )
        },
    }


def run_matrix(
    *, repository: Path, old_skill_revision: str, old_mcp_wheel: Path,
    candidate_skill_revision: str, candidate_mcp_wheel: Path, evidence_dir: Path,
) -> dict[str, Any]:
    revisions = dict(zip(_LABELS, (old_skill_revision, candidate_skill_revision), strict=True))
    wheels = dict(zip(_LABELS, (old_mcp_wheel, candidate_mcp_wheel), strict=True))
    evidence_path = evidence_dir / _EVIDENCE_FILE
    if evidence_path.exists():
        raise ValueError("evidence directory already contains a matrix report")
    with tempfile.TemporaryDirectory(prefix="weex-contract-matrix-") as tempdir:
        root = Path(tempdir).resolve()
        commits, traders, pins, interpreters = {}, {}, {}, {}
        for label in _LABELS:
            checkout = root / f"{label}-source"
            commits[label] = _archive_revision(repository, revisions[label], checkout)
            traders[label] = checkout / _TRADER_PATH
            pins[label] = _read_pin(traders[label])
            interpreters[label] = _install_pinned_wheel(
                wheels[label], pins[label]["wheel_sha256"], root / f"{label}-wheel",
            )
        cases = [
            _matrix_case(
                skill, wheel, traders[skill], interpreters[wheel], commits[skill], pins[skill], pins[wheel],
                trade_enabled=mode,
            )
            for skill in _LABELS for wheel in _LABELS for mode in _MODES
        ]
    report = {"schema_version": 1, "ok": _validate_matrix_cases(cases), "cases": cases}
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with evidence_path.open("x", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        target.write("\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-skill-revision", required=True)
    parser.add_argument("--old-mcp-wheel", type=Path, required=True)
    parser.add_argument("--candidate-skill-revision", required=True)
    parser.add_argument("--candidate-mcp-wheel", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run_matrix(repository=Path(__file__).resolve().parents[3], **vars(args))
    except subprocess.CalledProcessError as exc:
        print(f"MCP contract matrix setup command failed (exit {exc.returncode}).", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("MCP contract matrix setup command timed out.", file=sys.stderr)
        return 1
    except (KeyError, OSError, tarfile.TarError, TypeError, ValueError) as exc:
        print(f"MCP contract matrix setup failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": report["ok"], "evidence": str(args.evidence_dir / _EVIDENCE_FILE)}, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
