#!/usr/bin/env python3
"""One-shot, best-effort local notification delivery for auto-trade claims."""

from __future__ import annotations

import platform
import shutil
import subprocess
from datetime import datetime
from typing import Any, Callable, Protocol


class NotificationState(Protocol):
    def claim_notifications(self, *, now: datetime | None = None) -> list[dict[str, Any]]: ...

    def complete_notification(
        self,
        *,
        notification_key: str,
        outcome: str,
        now: datetime | None = None,
    ) -> dict[str, Any]: ...


NotificationAdapter = Callable[[dict[str, Any]], Any]


def dispatch_notification_claims(
    state: NotificationState,
    adapter: NotificationAdapter,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Claim, attempt once, and record a result without changing business state."""
    results: list[dict[str, Any]] = []
    for claim in state.claim_notifications(now=now):
        try:
            adapter_result = adapter(claim)
            outcome = "UNKNOWN" if adapter_result == "UNKNOWN" else "DELIVERED"
        except Exception:
            outcome = "FAILED"
        state.complete_notification(
            notification_key=claim["notification_key"],
            outcome=outcome,
            now=now,
        )
        results.append(
            {
                "notification_key": claim["notification_key"],
                "kind": claim["kind"],
                "status": outcome,
            }
        )
    return results


def build_notification_text(claim: dict[str, Any]) -> tuple[str, str]:
    strategy = str(claim.get("strategy_name") or "WEEX strategy")
    if claim.get("kind") == "ACCEPTED_SUMMARY":
        title = f"WEEX auto-trade summary: {strategy}"
        modules = ", ".join(str(item) for item in claim.get("modules", []))
        symbols = ", ".join(str(item) for item in claim.get("symbols", []))
        body = (
            f"{claim.get('order_count', 0)} orders; {modules}; {symbols}; "
            f"estimated {claim.get('estimated_amount_u', 'unknown')} U; "
            f"remaining {claim.get('remaining_amount_u', 'unknown')} U"
        )
        return title, body
    title = f"WEEX auto-trade attention: {strategy}"
    body = f"{claim.get('event_type', 'UNKNOWN_EVENT')}; inspect the local event timeline"
    return title, body


class SystemNotificationAdapter:
    """Local OS adapter. Each invocation performs one bounded attempt and never retries."""

    def __init__(self, *, timeout_seconds: float = 7.0) -> None:
        if timeout_seconds <= 5.5:
            raise ValueError("timeout_seconds must exceed the Windows notification display duration")
        self.timeout_seconds = timeout_seconds

    def __call__(self, claim: dict[str, Any]) -> str | None:
        title, body = build_notification_text(claim)
        system = platform.system()
        if system == "Darwin":
            command = [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e",
                "end run",
                title,
                body,
            ]
        elif system == "Windows":
            executable = shutil.which("powershell") or shutil.which("pwsh")
            if executable is None:
                raise RuntimeError("local notification adapter is unavailable")
            command = [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "param($title,$body) Add-Type -AssemblyName System.Windows.Forms; "
                "$n=New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
                "$n.ShowBalloonTip(5000,$title,$body,[System.Windows.Forms.ToolTipIcon]::Info); "
                "Start-Sleep -Milliseconds 5500; $n.Dispose()",
                title,
                body,
            ]
        else:
            executable = shutil.which("notify-send")
            if executable is None:
                raise RuntimeError("local notification adapter is unavailable")
            command = [executable, title, body]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "UNKNOWN"
        if completed.returncode != 0:
            raise RuntimeError("local notification delivery failed")
        return None


__all__ = [
    "NotificationAdapter",
    "SystemNotificationAdapter",
    "build_notification_text",
    "dispatch_notification_claims",
]
