#!/usr/bin/env python3
"""User-facing diagnostic helpers for WEEX skill runtime issues."""

from __future__ import annotations

import argparse
import importlib
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
    managed_runtime_setup_command,
    managed_venv_python,
    probe_runtime,
)
from weex_profile_language import resolve_language


TEXTS = {
    "en": {
        "parser_description": "Diagnose and repair common WEEX skill runtime problems.",
        "pretty_help": "Pretty-print JSON output for easier reading",
        "gui_help": "Inspect GUI runtime readiness",
        "gui_description": "Inspect the current Python runtime, required managed GUI runtime, and optionally repair GUI bootstrap state.",
        "gui_fix_help": "Provision or repair the required managed GUI runtime",
        "gui_accept_help": "Allow the GUI doctor to download the pinned managed GUI runtime when --fix is used",
        "mcp_help": "Inspect Local MCP runtime and encrypted approval-key readiness",
        "mcp_description": "Read-only Local MCP, Vault, and approval-key diagnostics. This command never installs, provisions, rotates, or repairs state.",
        "status_ok_system": "GUI is healthy on the current Python runtime.",
        "status_ok_managed": "The required managed GUI runtime is ready.",
        "status_not_ready": "GUI is not ready yet.",
        "recommend_fix": (
            "Ask the AI to install the managed GUI runtime; after your confirmation it will run "
            "scripts/weex_gui_bootstrap.py ensure --accept-managed-runtime --pretty."
        ),
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
        "label_mcp_runtime": "MCP runtime",
        "label_read_ready": "Read ready",
        "label_trade_ready": "Trade ready",
        "label_vault_state": "Vault state",
        "label_approval_key": "Approval key",
        "label_action_required": "Action required",
        "value_yes": "yes",
        "value_no": "no",
        "value_ready": "ready",
        "value_not_ready": "not ready",
        "value_none": "none",
    },
    "zh": {
        "parser_description": "诊断并修复常见的 WEEX 技能运行时问题。",
        "pretty_help": "以更易读的格式输出 JSON",
        "gui_help": "检查 GUI 运行时状态",
        "gui_description": "检查当前 Python 运行时、必需的受管 GUI 运行时，并在需要时修复 GUI bootstrap 状态。",
        "gui_fix_help": "创建或修复必需的受管 GUI 运行时",
        "gui_accept_help": "配合 --fix 使用，允许 GUI doctor 下载固定版本的受管 GUI 运行时",
        "mcp_help": "检查本地 MCP 运行时和加密 approval key 就绪状态",
        "mcp_description": "只读检查本地 MCP、Vault 和 approval key；不会安装、创建、轮换或修复任何状态。",
        "status_ok_system": "当前 Python 运行时的 GUI 状态正常。",
        "status_ok_managed": "必需的受管 GUI 运行时已经可用。",
        "status_not_ready": "GUI 运行时当前还没有就绪。",
        "recommend_fix": (
            "可以让 AI 询问并在你确认后代为安装受管 GUI 运行时；安装时会执行 "
            "scripts/weex_gui_bootstrap.py ensure --accept-managed-runtime --pretty。"
        ),
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
        "label_mcp_runtime": "MCP 运行时",
        "label_read_ready": "只读就绪",
        "label_trade_ready": "交易就绪",
        "label_vault_state": "Vault 状态",
        "label_approval_key": "Approval key",
        "label_action_required": "需要操作",
        "value_yes": "是",
        "value_no": "否",
        "value_ready": "已就绪",
        "value_not_ready": "未就绪",
        "value_none": "无",
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
    if ok and managed_status["usable"]:
        return t(language, "recommend_retry_gui")
    if ok and current_probe.usable:
        return t(language, "status_ok_system")
    return t(language, "recommend_fix")


def build_gui_report(language: str, *, fix: bool, accept_managed_runtime: bool = False) -> dict[str, Any]:
    resolved_language = resolve_language(language)
    os_family = platform.system()
    current_probe = probe_runtime(sys.executable)
    managed_status = _managed_runtime_status()
    bootstrap_disabled = os.getenv(BOOTSTRAP_DISABLE_ENV) == "1"
    fix_attempted = False
    fix_result = None
    fix_error = None
    managed_required = os_family in {"Windows", "Darwin"}
    managed_ready = bool(managed_status["usable"])

    if fix and managed_required and not managed_ready and not bootstrap_disabled:
        fix_attempted = True
        try:
            runtime_python, managed_probe, action = ensure_managed_gui_runtime(
                resolved_language,
                allow_network_install=accept_managed_runtime,
            )
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
    elif fix and bootstrap_disabled and managed_required and not managed_ready:
        fix_attempted = True
        fix_result = "disabled"

    managed_ready = bool(managed_status["usable"])
    ok = managed_ready if managed_required else current_probe.usable
    requires_user_consent = (
        managed_required
        and not ok
        and not bootstrap_disabled
        and (not fix_attempted or not accept_managed_runtime)
    )
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

    if managed_required and managed_status["usable"]:
        summary = t(resolved_language, "status_ok_managed")
    elif not managed_required and current_probe.usable:
        summary = t(resolved_language, "status_ok_system")
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
        "requires_user_consent": requires_user_consent,
        "setup_command": managed_runtime_setup_command(os_family),
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
    payload = build_gui_report(
        language,
        fix=args.fix,
        accept_managed_runtime=args.accept_managed_runtime,
    )
    if args.pretty:
        output_json(payload, pretty=True)
    else:
        print(render_gui_report(language, payload))
    return 0 if payload["ok"] else 1


def _probe_local_mcp_runtime(*, trade_enabled: bool = False) -> dict[str, Any]:
    gateway = importlib.import_module("weex_mcp_gateway")
    return gateway.probe_local_mcp_runtime(trade_enabled=trade_enabled)


def _load_profile_store_module() -> Any:
    return importlib.import_module("weex_profile_store")


def _failed_mcp_runtime_status(*, trade_enabled: bool) -> dict[str, Any]:
    return {
        "installed": False,
        "version": None,
        "expected_version": None,
        "package_ok": False,
        "manifest_ok": False,
        "runtime_profile": "local_user",
        "trade_enabled": trade_enabled,
        "tool_contract_version": None,
        "manifest_digest": None,
        "action_required": "repair_mcp_gateway",
    }


def _empty_approval_key_status() -> dict[str, Any]:
    return {
        "present": False,
        "valid": False,
        "length": 0,
        "fp_sha256": None,
    }


def _safe_approval_key_status(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return _empty_approval_key_status()
    present = raw.get("present") is True
    raw_length = raw.get("length")
    length = raw_length if isinstance(raw_length, int) and not isinstance(raw_length, bool) and 0 <= raw_length <= 4096 else 0
    raw_fingerprint = raw.get("fp_sha256")
    fingerprint = (
        raw_fingerprint
        if isinstance(raw_fingerprint, str)
        and len(raw_fingerprint) == 16
        and all(character in "0123456789abcdef" for character in raw_fingerprint)
        else None
    )
    valid = (
        raw.get("valid") is True
        and present
        and length >= 32
        and fingerprint is not None
    )
    return {
        "present": present,
        "valid": valid,
        "length": length,
        "fp_sha256": fingerprint,
    }


def _mcp_runtime_ready(runtime: dict[str, Any], *, trade_enabled: bool) -> bool:
    return bool(
        runtime.get("package_ok")
        and runtime.get("manifest_ok")
        and runtime.get("action_required") is None
        and runtime.get("trade_enabled") is trade_enabled
    )


def build_mcp_report(language: str) -> dict[str, Any]:
    resolve_language(language)
    agent_state = importlib.import_module("weex_agent_state")
    try:
        runtime = agent_state.project_mcp_status(
            _probe_local_mcp_runtime(trade_enabled=False)
        )
    except Exception:
        runtime = _failed_mcp_runtime_status(trade_enabled=False)
    try:
        trade_runtime = agent_state.project_mcp_status(
            _probe_local_mcp_runtime(trade_enabled=True)
        )
    except Exception:
        trade_runtime = _failed_mcp_runtime_status(trade_enabled=True)

    read_ready = _mcp_runtime_ready(runtime, trade_enabled=False)
    trade_contract_ready = _mcp_runtime_ready(
        trade_runtime,
        trade_enabled=True,
    )
    vault = {
        "configured": False,
        "state": "unavailable",
        "action_required": "repair",
    }
    approval_key = _empty_approval_key_status()
    action_required = runtime.get("action_required") or "repair_mcp_gateway"
    trade_ready = False

    if read_ready and not trade_contract_ready:
        action_required = trade_runtime.get("action_required") or "repair_mcp_gateway"
    elif read_ready:
        action_required = None
        try:
            store = _load_profile_store_module()
            raw_vault = store.vault_status()
        except Exception:
            raw_vault = None
            store = None

        raw_state = raw_vault.get("state") if isinstance(raw_vault, dict) else None
        state = raw_state if raw_state in {"uninitialized", "locked", "unlocked", "misconfigured"} else "unavailable"
        configured = bool(isinstance(raw_vault, dict) and raw_vault.get("configured") is True)
        vault_action = {
            "uninitialized": "setup",
            "locked": "unlock",
            "unlocked": None,
            "misconfigured": "repair",
            "unavailable": "repair",
        }[state]
        vault = {
            "configured": configured,
            "state": state,
            "action_required": vault_action,
        }

        if state == "uninitialized":
            action_required = "setup_vault"
        elif state == "locked":
            action_required = "unlock_vault"
        elif state != "unlocked" or store is None:
            action_required = "repair_vault"
        else:
            try:
                approval_key = _safe_approval_key_status(
                    store.local_mcp_approval_key_status()
                )
            except Exception:
                approval_key = _empty_approval_key_status()
                action_required = "repair_vault"
            else:
                trade_ready = bool(approval_key["valid"])
                action_required = (
                    None if trade_ready else "provision_local_mcp_approval_key"
                )

    return {
        "runtime": runtime,
        "trade_runtime": trade_runtime,
        "vault": vault,
        "approval_key": approval_key,
        "read_ready": read_ready,
        "trade_ready": trade_ready,
        "action_required": action_required,
    }


def render_mcp_report(language: str, payload: dict[str, Any]) -> str:
    resolved_language = resolve_language(language)
    yes = t(resolved_language, "value_yes")
    no = t(resolved_language, "value_no")
    runtime_label = (
        t(resolved_language, "value_ready")
        if payload["read_ready"]
        else t(resolved_language, "value_not_ready")
    )
    approval_label = (
        t(resolved_language, "value_ready")
        if payload["approval_key"]["valid"]
        else t(resolved_language, "value_not_ready")
    )
    action = payload["action_required"] or t(resolved_language, "value_none")
    return "\n".join(
        [
            f"{t(resolved_language, 'label_mcp_runtime')}: {runtime_label}",
            f"{t(resolved_language, 'label_read_ready')}: {yes if payload['read_ready'] else no}",
            f"{t(resolved_language, 'label_trade_ready')}: {yes if payload['trade_ready'] else no}",
            f"{t(resolved_language, 'label_vault_state')}: {payload['vault']['state']}",
            f"{t(resolved_language, 'label_approval_key')}: {approval_label}",
            f"{t(resolved_language, 'label_action_required')}: {action}",
        ]
    )


def cmd_mcp(args: argparse.Namespace, language: str) -> int:
    payload = build_mcp_report(language)
    if args.pretty:
        output_json(payload, pretty=True)
    else:
        print(render_mcp_report(language, payload))
    return 0 if payload["read_ready"] else 1


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
    p_gui.add_argument("--accept-managed-runtime", action="store_true", help=t(language, "gui_accept_help"))
    p_gui.add_argument("--pretty", action="store_true", help=t(language, "pretty_help"))
    p_mcp = sub.add_parser(
        "mcp",
        help=t(language, "mcp_help"),
        description=t(language, "mcp_description"),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p_mcp.add_argument("--pretty", action="store_true", help=t(language, "pretty_help"))
    return parser


def main(language: str | None = None, argv: Optional[list[str]] = None) -> int:
    parser_language = resolve_language(language)
    effective_argv = sys.argv[1:] if argv is None else argv
    parser = build_parser(parser_language)
    args = parser.parse_args(effective_argv)
    resolved_language = resolve_language(language or args.language)

    if args.command == "gui":
        return cmd_gui(args, resolved_language)
    if args.command == "mcp":
        return cmd_mcp(args, resolved_language)
    raise SystemExit(2)


if __name__ == "__main__":
    raise SystemExit(main())
