#!/usr/bin/env python3
"""User-facing diagnostic helpers for WEEX skill runtime issues."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Optional

from weex_agent_state import refresh_agent_records
from weex_gui_bootstrap import (
    BOOTSTRAP_DISABLE_ENV,
    GuiBootstrapError,
    RuntimeProbe,
    bootstrap_root,
    ensure_managed_gui_runtime,
    managed_venv_python,
    probe_runtime,
)
from weex_profile_language import resolve_language


TEXTS = {
    "en": {
        "parser_description": "Diagnose and repair common WEEX skill runtime problems.",
        "pretty_help": "Pretty-print JSON output for easier reading",
        "gui_help": "Inspect GUI runtime readiness",
        "gui_description": "Inspect the current GUI runtime, managed fallback runtime, and optionally repair GUI bootstrap state.",
        "gui_fix_help": "Provision or repair the managed GUI runtime when the current interpreter is not GUI-safe",
        "status_ok_system": "GUI is healthy on the current Python runtime.",
        "status_ok_managed": "GUI fallback is healthy through the managed runtime.",
        "status_not_ready": "GUI is not ready yet.",
        "recommend_fix": "Run this command again with --fix to provision the managed GUI runtime.",
        "recommend_retry_gui": "Retry the same GUI entrypoint; it should auto-relaunch inside the managed runtime.",
        "recommend_disable": f"{BOOTSTRAP_DISABLE_ENV}=1 is set. Clear it or use terminal flows instead.",
        "recommend_linux": "GUI doctor is intended for Windows/macOS Tk issues. Use the terminal onboarding path on Linux.",
        "label_current": "Current Python",
        "label_current_status": "Current runtime",
        "label_managed": "Managed runtime",
        "label_fix": "Fix result",
        "label_recommendation": "Recommendation",
        "managed_missing": "missing",
        "managed_ready": "ready",
        "managed_broken": "broken",
        "fix_skipped": "not requested",
        "fix_created": "created",
        "fix_reused": "already ready",
        "fix_repaired": "repaired",
        "fix_failed": "failed",
        "fix_disabled": "disabled",
        "runtime_ok": "ok",
        "runtime_issue": "{reason}",
    },
    "zh": {
        "parser_description": "诊断并修复常见的 WEEX 技能运行时问题。",
        "pretty_help": "以更易读的格式输出 JSON",
        "gui_help": "检查 GUI 运行时状态",
        "gui_description": "检查当前 GUI 运行时、受管兜底运行时，并在需要时修复 GUI bootstrap 状态。",
        "gui_fix_help": "当当前解释器不适合启动 GUI 时，创建或修复受管 GUI 运行时",
        "status_ok_system": "当前 Python 运行时的 GUI 状态正常。",
        "status_ok_managed": "受管 GUI 运行时已经可用，可以作为兜底运行时。",
        "status_not_ready": "GUI 运行时当前还没有就绪。",
        "recommend_fix": "请带上 --fix 再执行一次这个命令，创建受管 GUI 运行时。",
        "recommend_retry_gui": "请重新启动同一个 GUI 入口；它会自动切换到受管运行时。",
        "recommend_disable": f"检测到 {BOOTSTRAP_DISABLE_ENV}=1。请清除这个环境变量，或者改用终端流程。",
        "recommend_linux": "这个 GUI doctor 主要用于 Windows/macOS 的 Tk 问题；Linux 请使用终端 onboarding 流程。",
        "label_current": "当前 Python",
        "label_current_status": "当前运行时",
        "label_managed": "受管运行时",
        "label_fix": "修复结果",
        "label_recommendation": "建议",
        "managed_missing": "不存在",
        "managed_ready": "可用",
        "managed_broken": "损坏",
        "fix_skipped": "未执行",
        "fix_created": "已创建",
        "fix_reused": "已可用",
        "fix_repaired": "已修复",
        "fix_failed": "失败",
        "fix_disabled": "已禁用",
        "runtime_ok": "正常",
        "runtime_issue": "{reason}",
    },
}


def t(language: str, key: str, **kwargs: object) -> str:
    text = TEXTS[language][key]
    if kwargs:
        return text.format(**kwargs)
    return text


def output_json(payload: dict[str, Any], pretty: bool) -> None:
    if pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
        return
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False))


def _probe_payload(probe: Optional[RuntimeProbe]) -> Optional[dict[str, Any]]:
    return probe.to_dict() if probe is not None else None


def _managed_runtime_status() -> dict[str, Any]:
    python_path = managed_venv_python()
    if not python_path.exists():
        return {
            "exists": False,
            "python_executable": str(python_path),
            "usable": False,
            "probe": None,
        }
    probe = probe_runtime(str(python_path))
    return {
        "exists": True,
        "python_executable": str(python_path),
        "usable": probe.usable,
        "probe": _probe_payload(probe),
    }


def _runtime_reason(language: str, probe: RuntimeProbe) -> str:
    if probe.usable:
        return t(language, "runtime_ok")
    return probe.summary(language)


def _build_recommendation(
    language: str,
    *,
    os_family: str,
    ok: bool,
    current_probe: RuntimeProbe,
    managed_status: dict[str, Any],
    bootstrap_disabled: bool,
    fix_attempted: bool,
    fix_succeeded: bool,
) -> str:
    if os_family not in {"Windows", "Darwin"}:
        return t(language, "recommend_linux")
    if bootstrap_disabled and not ok:
        return t(language, "recommend_disable")
    if fix_attempted and fix_succeeded:
        return t(language, "recommend_retry_gui")
    if ok and current_probe.usable:
        return t(language, "status_ok_system")
    if ok and managed_status["usable"]:
        return t(language, "recommend_retry_gui")
    return t(language, "recommend_fix")


def build_gui_report(language: str, *, fix: bool) -> dict[str, Any]:
    resolved_language = resolve_language(language)
    os_family = platform.system()
    current_probe = probe_runtime(sys.executable)
    managed_status = _managed_runtime_status()
    bootstrap_disabled = os.getenv(BOOTSTRAP_DISABLE_ENV) == "1"
    fix_attempted = False
    fix_result = None
    fix_error = None

    if fix and os_family in {"Windows", "Darwin"} and not current_probe.usable and not bootstrap_disabled:
        fix_attempted = True
        try:
            runtime_python, managed_probe, action = ensure_managed_gui_runtime(resolved_language)
        except GuiBootstrapError as exc:
            fix_result = "failed"
            fix_error = str(exc)
        else:
            fix_result = action
            managed_status = {
                "exists": True,
                "python_executable": str(runtime_python),
                "usable": managed_probe.usable,
                "probe": _probe_payload(managed_probe),
            }
            try:
                refresh_agent_records(preferred_language=resolved_language, command="doctor.gui.fix")
            except Exception:
                pass
    elif fix and bootstrap_disabled and not current_probe.usable:
        fix_attempted = True
        fix_result = "disabled"

    ok = current_probe.usable or bool(managed_status["usable"])
    recommendation = _build_recommendation(
        resolved_language,
        os_family=os_family,
        ok=ok,
        current_probe=current_probe,
        managed_status=managed_status,
        bootstrap_disabled=bootstrap_disabled,
        fix_attempted=fix_attempted,
        fix_succeeded=bool(ok and fix_result in {"created", "repaired", "reused"}),
    )

    if current_probe.usable:
        summary = t(resolved_language, "status_ok_system")
    elif managed_status["usable"]:
        summary = t(resolved_language, "status_ok_managed")
    else:
        summary = t(resolved_language, "status_not_ready")

    return {
        "ok": ok,
        "summary": summary,
        "language": resolved_language,
        "platform": os_family,
        "current_python": sys.executable,
        "bootstrap_root": str(bootstrap_root()),
        "bootstrap_disabled": bootstrap_disabled,
        "current_runtime": {
            "usable": current_probe.usable,
            "reason": current_probe.reason,
            "detail": _runtime_reason(resolved_language, current_probe),
            "probe": _probe_payload(current_probe),
        },
        "managed_runtime": managed_status,
        "fix": {
            "requested": fix,
            "attempted": fix_attempted,
            "result": fix_result,
            "error": fix_error,
        },
        "recommendation": recommendation,
    }


def render_gui_report(language: str, payload: dict[str, Any]) -> str:
    managed_status = payload["managed_runtime"]
    if not managed_status["exists"]:
        managed_label = t(language, "managed_missing")
    elif managed_status["usable"]:
        managed_label = t(language, "managed_ready")
    else:
        managed_label = t(language, "managed_broken")

    fix_result = payload["fix"]["result"]
    if not payload["fix"]["requested"]:
        fix_label = t(language, "fix_skipped")
    elif fix_result == "created":
        fix_label = t(language, "fix_created")
    elif fix_result == "repaired":
        fix_label = t(language, "fix_repaired")
    elif fix_result == "reused":
        fix_label = t(language, "fix_reused")
    elif fix_result == "disabled":
        fix_label = t(language, "fix_disabled")
    elif fix_result == "failed":
        fix_label = t(language, "fix_failed")
    else:
        fix_label = t(language, "fix_skipped")

    lines = [
        payload["summary"],
        f"{t(language, 'label_current')}: {payload['current_python']}",
        f"{t(language, 'label_current_status')}: {payload['current_runtime']['detail']}",
        f"{t(language, 'label_managed')}: {managed_label} ({managed_status['python_executable']})",
        f"{t(language, 'label_fix')}: {fix_label}",
        f"{t(language, 'label_recommendation')}: {payload['recommendation']}",
    ]
    if payload["fix"]["error"]:
        lines.append(payload["fix"]["error"])
    return "\n".join(lines)


def cmd_gui(args: argparse.Namespace, language: str) -> int:
    payload = build_gui_report(language, fix=args.fix)
    if args.pretty:
        output_json(payload, pretty=True)
    else:
        print(render_gui_report(language, payload))
    return 0 if payload["ok"] else 1


def build_parser(language: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=t(language, "parser_description"),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--language", default=None, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)
    p_gui = sub.add_parser(
        "gui",
        help=t(language, "gui_help"),
        description=t(language, "gui_description"),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p_gui.add_argument("--fix", action="store_true", help=t(language, "gui_fix_help"))
    p_gui.add_argument("--pretty", action="store_true", help=t(language, "pretty_help"))
    return parser


def main(language: str | None = None, argv: Optional[list[str]] = None) -> int:
    parser_language = resolve_language(language)
    effective_argv = sys.argv[1:] if argv is None else argv
    parser = build_parser(parser_language)
    args = parser.parse_args(effective_argv)
    resolved_language = resolve_language(language or args.language)

    if args.command == "gui":
        return cmd_gui(args, resolved_language)
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
