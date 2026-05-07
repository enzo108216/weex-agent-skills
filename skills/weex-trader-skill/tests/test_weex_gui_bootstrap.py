#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import weex_gui_bootstrap as bootstrap  # noqa: E402


class GuiBootstrapTests(unittest.TestCase):
    def test_probe_runtime_classifies_macos_tk_crash(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["python3", "-c", "probe"],
            returncode=0,
            stdout='{"usable": true, "tk_version": 8.5, "tcl_version": 8.5, "tkinter_path": "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/lib/python3.9/lib-dynload/_tkinter.cpython-39-darwin.so", "missing_modules": []}\n',
            stderr="",
        )

        with mock.patch.object(bootstrap.platform, "system", return_value="Darwin"):
            with mock.patch.object(bootstrap.subprocess, "run", return_value=completed):
                with mock.patch.object(
                    bootstrap,
                    "_linked_library_paths",
                    return_value=(
                        "/System/Library/Frameworks/Tcl.framework/Versions/8.5/Tcl",
                        "/System/Library/Frameworks/Tk.framework/Versions/8.5/Tk",
                    ),
                ):
                    probe = bootstrap.probe_runtime("/usr/bin/python3")

        self.assertFalse(probe.usable)
        self.assertEqual(probe.reason, "tk_crashed")

    def test_probe_runtime_reports_missing_modules(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["python3", "-c", "probe"],
            returncode=2,
            stdout='{"usable": false, "missing_modules": ["cryptography"], "tk_version": 8.6, "tcl_version": 8.6}\n',
            stderr="",
        )

        with mock.patch.object(bootstrap.subprocess, "run", return_value=completed):
            with mock.patch.object(bootstrap, "_linked_library_paths", return_value=()):
                probe = bootstrap.probe_runtime("/usr/bin/python3")

        self.assertFalse(probe.usable)
        self.assertEqual(probe.reason, "missing_modules")
        self.assertEqual(probe.missing_modules, ("cryptography",))

    def test_maybe_reexec_skips_when_already_active(self) -> None:
        with mock.patch.dict(os.environ, {bootstrap.BOOTSTRAP_ACTIVE_ENV: "1"}, clear=False):
            with mock.patch.object(bootstrap, "probe_runtime") as probe_mock:
                bootstrap.maybe_reexec_under_managed_gui_runtime(
                    "en",
                    entrypoint_path=SCRIPTS / "weex_profile_manager_app.py",
                    argv=[],
                )

        probe_mock.assert_not_called()

    def test_maybe_reexec_executes_managed_runtime_on_darwin(self) -> None:
        failing_probe = bootstrap.RuntimeProbe(usable=False, reason="tk_crashed", returncode=-6)
        managed_probe = bootstrap.RuntimeProbe(usable=True, reason="ok", returncode=0, tk_version="9.0", tcl_version="9.0")

        with mock.patch.object(bootstrap.platform, "system", return_value="Darwin"):
            with mock.patch.object(bootstrap, "probe_runtime", return_value=failing_probe):
                with mock.patch.object(
                    bootstrap,
                    "ensure_managed_gui_runtime",
                    return_value=(Path("/tmp/gui-python"), managed_probe, "created"),
                ):
                    with mock.patch.object(bootstrap.os, "execve", side_effect=RuntimeError("exec")) as exec_mock:
                        with self.assertRaises(RuntimeError):
                            bootstrap.maybe_reexec_under_managed_gui_runtime(
                                "en",
                                entrypoint_path=SCRIPTS / "weex_profile_manager_app.py",
                                argv=["--help"],
                            )

        exec_mock.assert_called_once()
        args, _kwargs = exec_mock.call_args
        self.assertEqual(args[0], "/tmp/gui-python")
        self.assertEqual(args[1][:3], ["/tmp/gui-python", str(SCRIPTS / "weex_profile_manager_app.py"), "--help"])

    def test_cli_help_renders(self) -> None:
        parser = bootstrap.build_parser()

        with self.assertRaises(SystemExit) as exc_info:
            parser.parse_args(["--help"])

        self.assertEqual(exc_info.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
