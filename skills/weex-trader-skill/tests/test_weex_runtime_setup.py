#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import weex_runtime_setup as runtime_setup  # noqa: E402


class RuntimeSetupTests(unittest.TestCase):
    def test_build_setup_report_installs_locked_requirements_with_current_interpreter(self) -> None:
        completed = [
            mock.Mock(returncode=0, stdout="pip 24.0", stderr=""),
            mock.Mock(returncode=0, stdout="installed", stderr=""),
        ]
        runtime_state = {
            "host": {
                "requirements_ready": True,
                "missing_modules": [],
            },
            "env_validation": {
                "ok": True,
                "issues": [],
            },
            "mcp": {
                "installed": False,
                "version": None,
                "expected_version": "0.2.0",
                "package_ok": False,
                "manifest_ok": False,
                "runtime_profile": "local_user",
                "trade_enabled": False,
                "tool_contract_version": "1",
                "manifest_digest": f"sha256:{'a' * 64}",
                "action_required": "install_verified_mcp_candidate",
            },
        }

        with mock.patch.object(runtime_setup.subprocess, "run", side_effect=completed) as run_mock:
            with mock.patch.object(
                runtime_setup,
                "refresh_agent_records",
                return_value={"runtime": runtime_state},
            ) as refresh_mock:
                report = runtime_setup.build_setup_report(language="zh")

        self.assertTrue(report["ok"])
        self.assertFalse(report["pip"]["ensurepip_attempted"])
        self.assertEqual(report["pip"]["install"]["returncode"], 0)
        self.assertEqual(report["runtime"]["mcp"], runtime_state["mcp"])
        refresh_mock.assert_called_once_with(preferred_language="zh", command="env.setup")

        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertEqual(commands[0], [sys.executable, "-m", "pip", "--version"])
        self.assertEqual(
            commands[1],
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--require-hashes",
                "-r",
                str(runtime_setup.requirements_lock_path()),
            ],
        )
        serialized_commands = json.dumps(commands)
        self.assertNotIn("weex-mcp", serialized_commands)
        self.assertNotIn("artifact", serialized_commands)

    def test_build_setup_report_bootstraps_ensurepip_when_pip_is_missing(self) -> None:
        completed = [
            mock.Mock(returncode=1, stdout="", stderr="missing pip"),
            mock.Mock(returncode=0, stdout="ensurepip ok", stderr=""),
            mock.Mock(returncode=0, stdout="pip 24.0", stderr=""),
            mock.Mock(returncode=0, stdout="installed", stderr=""),
        ]
        runtime_state = {
            "host": {
                "requirements_ready": True,
                "missing_modules": [],
            },
            "env_validation": {
                "ok": True,
                "issues": [],
            },
        }

        with mock.patch.object(runtime_setup.subprocess, "run", side_effect=completed) as run_mock:
            with mock.patch.object(
                runtime_setup,
                "refresh_agent_records",
                return_value={"runtime": runtime_state},
            ):
                report = runtime_setup.build_setup_report(language=None)

        self.assertTrue(report["ok"])
        self.assertTrue(report["pip"]["ensurepip_attempted"])
        self.assertEqual(report["pip"]["ensurepip"]["returncode"], 0)

        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertEqual(commands[0], [sys.executable, "-m", "pip", "--version"])
        self.assertEqual(commands[1], [sys.executable, "-m", "ensurepip", "--upgrade"])
        self.assertEqual(commands[2], [sys.executable, "-m", "pip", "--version"])

    def test_build_setup_report_fails_when_runtime_validation_stays_invalid(self) -> None:
        completed = [
            mock.Mock(returncode=0, stdout="pip 24.0", stderr=""),
            mock.Mock(returncode=0, stdout="installed", stderr=""),
        ]
        runtime_state = {
            "host": {
                "requirements_ready": True,
                "missing_modules": [],
            },
            "env_validation": {
                "ok": False,
                "issues": ["WEEX_API_TIMEOUT must be a positive number of seconds; got 'abc'."],
            },
        }

        with mock.patch.object(runtime_setup.subprocess, "run", side_effect=completed):
            with mock.patch.object(
                runtime_setup,
                "refresh_agent_records",
                return_value={"runtime": runtime_state},
            ):
                report = runtime_setup.build_setup_report(language="en")

        self.assertFalse(report["ok"])
        self.assertEqual(report["runtime"]["env_validation"]["issues"], runtime_state["env_validation"]["issues"])

    def test_main_pretty_prints_json(self) -> None:
        payload = {
            "ok": True,
            "python_executable": "/tmp/python",
            "requirements_path": "/tmp/requirements.txt",
            "pip": {
                "available_before": True,
                "ensurepip_attempted": False,
                "version_check": {"returncode": 0},
                "ensurepip": None,
                "install": {"returncode": 0},
            },
            "runtime": {
                "host": {
                    "requirements_ready": True,
                    "missing_modules": [],
                },
                "env_validation": {
                    "ok": True,
                    "issues": [],
                },
            },
        }

        with mock.patch.object(runtime_setup, "build_setup_report", return_value=payload):
            stream = io.StringIO()
            with mock.patch.object(sys, "stdout", stream):
                exit_code = runtime_setup.main(["--pretty"])

        self.assertEqual(exit_code, 0)
        rendered = json.loads(stream.getvalue())
        self.assertTrue(rendered["ok"])
        self.assertEqual(rendered["python_executable"], "/tmp/python")


if __name__ == "__main__":
    unittest.main()
