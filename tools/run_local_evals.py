#!/usr/bin/env python3
"""Run the repository's deterministic, offline Skill evaluation suite."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


ROOT = Path(__file__).resolve().parents[1]
CASE_CATALOG = ROOT / "evals" / "cases" / "promptfoo-tests.json"
EXPECTED_DISCLAIMER = (
    "Disclaimer: This result is generated solely from the current input data and is for reference only. "
    "It does not constitute any investment or trading advice. Please make your own independent judgment "
    "based on real-time data, official rules, and your own risk tolerance. Responsibility for related "
    "decisions and execution rests solely with the user."
)

CaseHandler = Callable[[], dict[str, Any]]


def load_case_catalog() -> list[dict[str, Any]]:
    payload = json.loads(CASE_CATALOG.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("eval case catalog must be a JSON array")
    cases: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("eval case must be an object")
        variables = item.get("vars")
        if not isinstance(variables, dict) or not variables.get("case_id"):
            raise ValueError("eval case must contain vars.case_id")
        case_id = str(variables["case_id"])
        cases.append(
            {
                "case_id": case_id,
                "description": str(item.get("description") or case_id),
                "group": case_id.split(".", 1)[0],
            }
        )
    return cases


def _case_meta(case_id: str) -> dict[str, str]:
    known = {item["case_id"]: item for item in load_case_catalog()}
    if case_id not in known:
        raise KeyError(f"unknown local eval case: {case_id}")
    return known[case_id]


def _ok(case_id: str, details: dict[str, Any], *, summary: str = "") -> dict[str, Any]:
    meta = _case_meta(case_id)
    return {
        "case_id": case_id,
        "group": meta["group"],
        "ok": True,
        "summary": summary or meta["description"],
        "details": details,
    }


def _fail(case_id: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = _case_meta(case_id)
    return {
        "case_id": case_id,
        "group": meta["group"],
        "ok": False,
        "summary": message,
        "details": details or {},
    }


def _clean_eval_environment(monitor_home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("WEEX_")
        and key
        not in {
            "API_KEY",
            "API_SECRET",
            "API_PASSPHRASE",
            "WEEX_API_KEY",
            "WEEX_API_SECRET",
            "WEEX_API_PASSPHRASE",
        }
    }
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "WEEX_EVAL_OFFLINE": "1",
            "WEEX_MONITOR_SKILL_HOME": str(monitor_home),
        }
    )
    return env


@contextmanager
def _isolated_environment() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="weex-local-eval-") as directory:
        home = Path(directory)
        previous = os.environ.get("WEEX_MONITOR_SKILL_HOME")
        os.environ["WEEX_MONITOR_SKILL_HOME"] = str(home)
        try:
            yield home
        finally:
            if previous is None:
                os.environ.pop("WEEX_MONITOR_SKILL_HOME", None)
            else:
                os.environ["WEEX_MONITOR_SKILL_HOME"] = previous


def _run_json_cli(
    script: Path,
    args: list[str],
    payload: dict[str, Any],
    *,
    monitor_home: Path,
) -> tuple[int, Any, str]:
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=_clean_eval_environment(monitor_home),
    )
    try:
        output: Any = json.loads(completed.stdout)
    except json.JSONDecodeError:
        output = completed.stdout.strip()
    return completed.returncode, output, completed.stderr.strip()


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _analysis_cli(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    with _isolated_environment() as home:
        return_code, output, stderr = _run_json_cli(
            ROOT / "skills" / "weex-analysis-skill" / "scripts" / "weex_analysis_cli.py",
            [command, "--input", "-"],
            payload,
            monitor_home=home,
        )
    if return_code != 0 or not isinstance(output, dict):
        raise AssertionError(f"{command} failed: rc={return_code}, stderr={stderr}, output={output}")
    return output


def _load_monitor() -> Any:
    return _load_module(
        ROOT / "skills" / "weex-monitor-skill" / "scripts" / "weex_monitor_cli.py",
        "weex_local_eval_monitor",
    )


def _load_prepare() -> Any:
    scripts_dir = ROOT / "skills" / "weex-analysis-skill" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return _load_module(
        scripts_dir / "weex_analysis_prepare.py",
        "weex_local_eval_prepare",
    )


def _load_partner() -> Any:
    return _load_module(
        ROOT / "skills" / "weex-partner-skill" / "scripts" / "weex_partner_cli.py",
        "weex_local_eval_partner",
    )


def _base_monitor_task() -> dict[str, Any]:
    return {
        "task_type": "position_pnl_monitor",
        "profile": "eval-profile",
        "trading_mode": "demo",
        "market": "futures",
        "symbol": "BTCUSDT",
        "position_side": "LONG",
        "frequency_seconds": 5,
        "condition": {
            "metric": "unrealized_pnl",
            "operator": ">",
            "threshold": "50",
        },
        "action": {
            "type": "market_close",
            "target": "LONG",
        },
        "callback": {"type": "current_thread"},
    }


def _case_analysis_empty_order_risk() -> dict[str, Any]:
    result = _analysis_cli("analyze-order-risk", {})
    reasons = result.get("degraded_reasons", [])
    required = {"order_risk_missing_order_preview", "order_risk_missing_account_snapshot"}
    if not result.get("partial") or not required <= set(reasons):
        raise AssertionError(f"empty order risk was not fail-closed: {result}")
    if result.get("disclaimer") != EXPECTED_DISCLAIMER:
        raise AssertionError("analysis disclaimer missing")
    return _ok(
        "analysis.empty_order_risk",
        {"partial": result["partial"], "degraded_reasons": reasons},
    )


def _case_analysis_missing_fill_fields() -> dict[str, Any]:
    result = _analysis_cli(
        "analyze-fills",
        {
            "fills": [
                {"symbol": "BTCUSDT", "side": "sell", "quantity": 0.01, "price": 65000},
                {
                    "symbol": "ETHUSDT",
                    "side": "sell",
                    "quantity": 0.5,
                    "price": 3080,
                    "realized_pnl": 40,
                    "fee": 1.4,
                },
            ]
        },
    )
    required = {"fills_missing_realized_pnl", "fills_missing_fee"}
    if not result.get("partial") or not required <= set(result.get("degraded_reasons", [])):
        raise AssertionError(f"missing fill fields were not degraded: {result}")
    return _ok(
        "analysis.missing_fill_fields",
        {"partial": result["partial"], "degraded_reasons": result["degraded_reasons"]},
    )


def _case_analysis_snapshot_missing_fields() -> dict[str, Any]:
    result = _analysis_cli(
        "analyze-snapshot",
        {"positions": [{"symbol": "BTCUSDT", "side": "long", "quantity": 0.01}]},
    )
    required = {
        "snapshot_missing_equity",
        "snapshot_missing_available_balance",
        "snapshot_position_missing_mark_price",
        "snapshot_position_missing_leverage",
    }
    if not result.get("partial") or not required <= set(result.get("degraded_reasons", [])):
        raise AssertionError(f"snapshot missing fields were not degraded: {result}")
    return _ok(
        "analysis.snapshot_missing_fields",
        {"partial": result["partial"], "degraded_reasons": result["degraded_reasons"]},
    )


def _case_analysis_replay_scope_inheritance() -> dict[str, Any]:
    prepare = _load_prepare()
    payload = {
        "analysis_type": "replay",
        "market": "futures",
        "account_scope": "sim_futures",
        "orders": [
            {
                "order_id": "btc-open",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "quantity": 0.01,
                "time": 1710000000000,
            }
        ],
        "fills": [
            {
                "trade_id": "btc-fill",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "quantity": 0.01,
                "price": 65000,
                "time": 1710000000000,
            }
        ],
    }
    result = prepare.prepare_replay_payload(payload, account_scopes={"sim_futures"})
    if len(result.get("orders", [])) != 1 or len(result.get("fills", [])) != 1:
        raise AssertionError(f"top-level account scope filtered valid rows: {result}")
    return _ok(
        "analysis.replay_scope_inheritance",
        {"orders": len(result["orders"]), "fills": len(result["fills"])},
    )


def _case_monitor_missing_trading_mode() -> dict[str, Any]:
    monitor = _load_monitor()
    task = _base_monitor_task()
    task.pop("trading_mode")
    try:
        monitor.normalize_task(task, now_ms=1000)
    except monitor.MonitorInputError as exc:
        message = str(exc)
        if "trading_mode is required" not in message:
            raise AssertionError(message)
        return _ok(
            "monitor.missing_trading_mode",
            {"error_type": type(exc).__name__, "message": message},
        )
    raise AssertionError("monitor accepted missing trading_mode")


def _case_monitor_price_condition_rejected() -> dict[str, Any]:
    monitor = _load_monitor()
    task = _base_monitor_task()
    task["condition"] = {"metric": "price", "operator": ">", "threshold": "70000"}
    try:
        monitor.normalize_task(task, now_ms=1000)
    except monitor.MonitorInputError as exc:
        message = str(exc)
        if "official conditional orders" not in message:
            raise AssertionError(message)
        return _ok(
            "monitor.price_condition_rejected",
            {"error_type": type(exc).__name__, "message": message},
        )
    raise AssertionError("monitor accepted a price-threshold local task")


def _case_monitor_confirmation_token_binding() -> dict[str, Any]:
    monitor = _load_monitor()
    with _isolated_environment():
        prepared = monitor.prepare_confirmation(_base_monitor_task(), now_ms=1000, language="zh")
        tampered = dict(prepared["task"])
        tampered["condition"] = dict(tampered["condition"])
        tampered["condition"]["threshold"] = "999"
        try:
            monitor.confirm_task(
                tampered,
                confirm_monitor=True,
                confirmation_token=prepared["confirmation_token"],
                now_ms=1001,
            )
        except monitor.MonitorInputError as exc:
            if "does not match monitor task details" not in str(exc):
                raise AssertionError(str(exc))
            return _ok(
                "monitor.confirmation_token_binding",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
    raise AssertionError("monitor accepted a token for tampered task details")


def _case_monitor_dry_run_trigger() -> dict[str, Any]:
    monitor = _load_monitor()
    task = monitor.normalize_task(_base_monitor_task(), now_ms=1000)
    result = monitor.evaluate_pnl_task(
        task,
        [
            {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "size": "0.01",
                "unrealizePnl": "55",
            }
        ],
    )
    if not result.get("triggered") or result.get("execution_delegate") != "weex-trader-skill":
        raise AssertionError(f"dry-run trigger plan invalid: {result}")
    if result.get("close_order", {}).get("quantity") != "0.01":
        raise AssertionError(f"close quantity mismatch: {result}")
    return _ok(
        "monitor.dry_run_trigger",
        {"triggered": True, "close_order": result["close_order"]},
    )


def _case_monitor_order_baseline_quantity() -> dict[str, Any]:
    monitor = _load_monitor()
    task = monitor.normalize_task(
        {
            "task_type": "order_baseline_pnl_monitor",
            "profile": "eval-profile",
            "trading_mode": "demo",
            "market": "futures",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "baseline": {"entry_price": "70000", "quantity": "0.01"},
            "condition": {
                "metric": "baseline_unrealized_pnl",
                "operator": ">",
                "threshold": "0",
            },
            "action": {"type": "market_close", "target": "LONG"},
            "callback": {"type": "current_thread"},
        },
        now_ms=1000,
    )
    result = monitor.evaluate_order_baseline_pnl_task(
        task,
        [
            {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "quantity": "0.05",
                "current_price": "70010",
            }
        ],
    )
    if not result.get("triggered") or result["close_order"].get("quantity") != "0.01":
        raise AssertionError(f"baseline quantity was not fixed: {result}")
    return _ok(
        "monitor.order_baseline_quantity",
        {
            "triggered": result["triggered"],
            "baseline_unrealized_pnl": result["trigger_snapshot"]["baseline_unrealized_pnl"],
            "close_quantity": result["close_order"]["quantity"],
        },
    )


def _case_partner_read_only_catalog() -> dict[str, Any]:
    partner = _load_partner()
    expected = {
        "list-referral-uids",
        "get-direct-trade-asset",
        "get-commission",
        "get-sub-agent-stats",
        "verify-referrals",
        "get-referral-assets",
        "get-referral-deal-data",
    }
    actual = set(partner.OPERATION_POLICIES)
    if actual != expected:
        raise AssertionError(f"unexpected Partner operation catalog: {sorted(actual)}")
    definitions = json.loads(
        (
            ROOT
            / "skills"
            / "weex-trader-skill"
            / "references"
            / "partner-api-definitions.json"
        ).read_text(encoding="utf-8")
    )
    definitions_by_endpoint = {
        item["key"]: item for item in definitions["definitions"] if isinstance(item, dict)
    }
    non_read = [
        operation
        for operation in expected
        if definitions_by_endpoint[
            partner.OPERATION_POLICIES[operation].endpoint
        ].get("operation_class") != "read"
    ]
    if non_read:
        raise AssertionError(f"non-read Partner operation found: {non_read}")
    return _ok("partner.read_only_catalog", {"operations": sorted(actual)})


def _case_partner_natural_language_fixture() -> dict[str, Any]:
    fixture_path = ROOT / "skills" / "weex-partner-skill" / "references" / "natural-language-regression.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise AssertionError("Partner natural-language fixture is empty")
    ids = [item.get("id") for item in scenarios]
    if len(ids) != len(set(ids)) or any(not item for item in ids):
        raise AssertionError("Partner natural-language fixture ids are not unique")
    route_operations = {
        item.get("expected", {}).get("operation")
        for item in scenarios
        if item.get("expected", {}).get("disposition") == "route"
    }
    if len(route_operations) != 7:
        raise AssertionError(f"fixture does not cover seven Partner routes: {route_operations}")
    return _ok(
        "partner.natural_language_fixture",
        {"scenario_count": len(scenarios), "route_operations": sorted(route_operations)},
    )


def _case_partner_missing_uid() -> dict[str, Any]:
    partner = _load_partner()
    request = {
        "operation": "get-commission",
        "profile": "eval-profile",
        "scope": {},
        "filters": {},
        "result_mode": "summary_with_first_20",
    }
    try:
        partner.plan_query(request)
    except partner.PartnerQueryError as exc:
        if exc.code != "scope_confirmation_required":
            raise AssertionError(f"unexpected Partner error: {exc.code}: {exc}")
        return _ok(
            "partner.missing_uid",
            {"error_type": type(exc).__name__, "code": exc.code},
        )
    raise AssertionError("Partner query accepted missing UID/all scope")


def _read_text(*relative_paths: str) -> str:
    return "\n".join((ROOT / relative_path).read_text(encoding="utf-8") for relative_path in relative_paths)


def _case_trader_confirmation_boundary() -> dict[str, Any]:
    text = _read_text(
        "skills/weex-trader-skill/SKILL.md",
        "skills/weex-trader-skill/scripts/weex_contract_api.py",
        "skills/weex-trader-skill/scripts/weex_spot_api.py",
        "skills/weex-trader-skill/scripts/weex_trade_guard.py",
    )
    required = {"--confirm-live", "--confirm-demo", "risk_signature", "intent_id"}
    missing = sorted(value for value in required if value not in text)
    if missing:
        raise AssertionError(f"Trader confirmation boundary is undocumented or absent: {missing}")
    return _ok("trader.confirmation_boundary", {"required_tokens": sorted(required)})


def _case_trader_secret_transport() -> dict[str, Any]:
    text = _read_text("skills/weex-trader-skill/SKILL.md", "skills/weex-trader-skill/README.md")
    required = {
        "--secrets-stdin-json",
        "--prompt-secrets",
        "--api-key-env",
        "--api-secret-env",
        "--api-passphrase-env",
    }
    missing = sorted(value for value in required if value not in text)
    if missing:
        raise AssertionError(f"Trader secret transport guidance missing: {missing}")
    return _ok("trader.secret_transport", {"documented_transports": sorted(required)})


def _case_repository_offline_safety() -> dict[str, Any]:
    paths = [
        ROOT / "evals" / "providers" / "local_provider.cjs",
        ROOT / "evals" / "graders" / "local_assertion.cjs",
    ]
    forbidden_patterns = {
        r"\brequests\b": "direct requests import",
        r"\burllib\b": "direct urllib import",
        r"https?://": "network URL",
        r"--confirm-live": "live mutation confirmation",
        r"--confirm-demo": "demo mutation confirmation",
    }
    violations: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern, label in forbidden_patterns.items():
            if re.search(pattern, text, flags=re.IGNORECASE):
                violations.append(f"{path.relative_to(ROOT)}: {label}")
    if violations:
        raise AssertionError("offline eval safety violation: " + "; ".join(violations))
    return _ok(
        "repository.offline_safety",
        {"checked_files": [str(path.relative_to(ROOT)) for path in paths if path.exists()]},
    )


def _case_repository_source_of_truth() -> dict[str, Any]:
    skill_dirs = [
        path
        for path in sorted((ROOT / "skills").iterdir())
        if path.is_dir() and (path / "SKILL.md").exists()
    ]
    if len(skill_dirs) != 4:
        raise AssertionError(f"expected four installable Skills, found {len(skill_dirs)}")
    missing = [
        str(path.relative_to(ROOT))
        for path in skill_dirs
        if not (path / "manifest.json").exists() or not (path / "file-index.json").exists()
    ]
    if missing:
        raise AssertionError(f"Skill catalog metadata missing: {missing}")
    copied_skill_files: list[Path] = []
    for directory in (
        ROOT / "evals" / "cases",
        ROOT / "evals" / "providers",
        ROOT / "evals" / "graders",
        ROOT / "evals" / "scripts",
        ROOT / "evals" / "tests",
    ):
        if directory.exists():
            copied_skill_files.extend(directory.rglob("SKILL.md"))
    if copied_skill_files:
        raise AssertionError("evals must not copy SKILL.md")
    return _ok(
        "repository.source_of_truth",
        {"skill_dirs": [path.name for path in skill_dirs], "source_of_truth": "skills/"},
    )


HANDLERS: dict[str, CaseHandler] = {
    "analysis.empty_order_risk": _case_analysis_empty_order_risk,
    "analysis.missing_fill_fields": _case_analysis_missing_fill_fields,
    "analysis.snapshot_missing_fields": _case_analysis_snapshot_missing_fields,
    "analysis.replay_scope_inheritance": _case_analysis_replay_scope_inheritance,
    "monitor.missing_trading_mode": _case_monitor_missing_trading_mode,
    "monitor.price_condition_rejected": _case_monitor_price_condition_rejected,
    "monitor.confirmation_token_binding": _case_monitor_confirmation_token_binding,
    "monitor.dry_run_trigger": _case_monitor_dry_run_trigger,
    "monitor.order_baseline_quantity": _case_monitor_order_baseline_quantity,
    "partner.read_only_catalog": _case_partner_read_only_catalog,
    "partner.natural_language_fixture": _case_partner_natural_language_fixture,
    "partner.missing_uid": _case_partner_missing_uid,
    "trader.confirmation_boundary": _case_trader_confirmation_boundary,
    "trader.secret_transport": _case_trader_secret_transport,
    "repository.offline_safety": _case_repository_offline_safety,
    "repository.source_of_truth": _case_repository_source_of_truth,
}


def run_case(case_id: str) -> dict[str, Any]:
    if case_id not in HANDLERS:
        return _fail(case_id, "case handler is missing")
    try:
        result = HANDLERS[case_id]()
        if not result.get("ok"):
            return result
        return result
    except Exception as exc:
        return _fail(
            case_id,
            f"{type(exc).__name__}: {exc}",
            details={"error_type": type(exc).__name__, "message": str(exc)},
        )


def run_all() -> dict[str, Any]:
    cases = [run_case(item["case_id"]) for item in load_case_catalog()]
    failed = [item["case_id"] for item in cases if not item.get("ok")]
    return {
        "ok": not failed,
        "suite": "weex-local-deterministic-evals",
        "case_count": len(cases),
        "passed": len(cases) - len(failed),
        "failed": failed,
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline WEEX Skill evaluation cases.")
    parser.add_argument("--case-id", help="Run one case for Promptfoo.")
    parser.add_argument("--list", action="store_true", help="List available case ids.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args(argv)

    if args.list:
        payload: Any = [item["case_id"] for item in load_case_catalog()]
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0

    payload = run_case(args.case_id) if args.case_id else run_all()
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
