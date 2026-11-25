#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import weex_doctor as doctor  # noqa: E402
from weex_gui_bootstrap import RuntimeProbe  # noqa: E402


class DoctorGuiTests(unittest.TestCase):
    def test_build_gui_report_requires_managed_runtime_even_when_system_runtime_is_usable(self) -> None:
        current_probe = RuntimeProbe(usable=True, reason="ok", returncode=0, tk_version="8.6", tcl_version="8.6")

        with mock.patch.object(doctor.platform, "system", return_value="Darwin"):
            with mock.patch.object(doctor, "probe_runtime", return_value=current_probe):
                with mock.patch.object(doctor, "_managed_runtime_status", return_value={
                    "exists": False,
                    "python_executable": "/tmp/managed-python",
                    "usable": False,
                    "probe": None,
                }):
                    payload = doctor.build_gui_report("en", fix=False)

        self.assertFalse(payload["ok"])
        self.assertIn("--accept-managed-runtime", payload["recommendation"])
        self.assertTrue(payload["requires_user_consent"])
        self.assertEqual(payload["summary"], "GUI is not ready yet.")

    def test_build_gui_report_recommends_fix_when_no_gui_runtime_is_ready(self) -> None:
        failing_probe = RuntimeProbe(usable=False, reason="tk_crashed", returncode=-6)

        with mock.patch.object(doctor.platform, "system", return_value="Darwin"):
            with mock.patch.object(doctor, "probe_runtime", return_value=failing_probe):
                with mock.patch.object(doctor, "_managed_runtime_status", return_value={
                    "exists": False,
                    "python_executable": "/tmp/managed-python",
                    "usable": False,
                    "probe": None,
                }):
                    with mock.patch.object(doctor, "ensure_managed_gui_runtime") as ensure_mock:
                        payload = doctor.build_gui_report("en", fix=False)

        ensure_mock.assert_not_called()
        self.assertFalse(payload["ok"])
        self.assertIn("--accept-managed-runtime", payload["recommendation"])
        self.assertIn("Ask the AI", payload["recommendation"])
        self.assertTrue(payload["requires_user_consent"])
        self.assertEqual(
            payload["setup_command"],
            "python3 scripts/weex_gui_bootstrap.py ensure --accept-managed-runtime --pretty",
        )
        self.assertEqual(payload["current_runtime"]["reason"], "tk_crashed")

    def test_build_gui_report_repairs_managed_runtime_when_fix_is_requested(self) -> None:
        failing_probe = RuntimeProbe(usable=False, reason="tk_crashed", returncode=-6)
        managed_probe = RuntimeProbe(usable=True, reason="ok", returncode=0, tk_version="9.0", tcl_version="9.0")

        with mock.patch.object(doctor.platform, "system", return_value="Darwin"):
            with mock.patch.object(doctor, "probe_runtime", return_value=failing_probe):
                with mock.patch.object(doctor, "_managed_runtime_status", return_value={
                    "exists": False,
                    "python_executable": "/tmp/managed-python",
                    "usable": False,
                    "probe": None,
                }):
                    with mock.patch.object(
                        doctor,
                        "ensure_managed_gui_runtime",
                        return_value=(Path("/tmp/managed-python"), managed_probe, "created"),
                    ) as ensure_mock:
                        with mock.patch.object(doctor, "refresh_agent_records") as refresh_mock:
                            payload = doctor.build_gui_report("zh", fix=True, accept_managed_runtime=True)

        ensure_mock.assert_called_once_with("zh", allow_network_install=True)
        refresh_mock.assert_called_once_with(preferred_language="zh", command="doctor.gui.fix")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["fix"]["result"], "created")
        self.assertFalse(payload["requires_user_consent"])
        self.assertTrue(payload["managed_runtime"]["usable"])

    def test_render_gui_report_is_human_readable(self) -> None:
        payload = {
            "summary": "GUI is healthy on the current Python runtime.",
            "current_python": "/tmp/python",
            "current_runtime": {
                "detail": "ok",
            },
            "managed_runtime": {
                "exists": False,
                "usable": False,
                "python_executable": "/tmp/managed-python",
            },
            "fix": {
                "requested": False,
                "result": None,
                "error": None,
            },
            "recommendation": "No action needed.",
        }

        report = doctor.render_gui_report("en", payload)

        self.assertIn("Current Python", report)
        self.assertIn("/tmp/python", report)
        self.assertIn("Recommendation", report)

    def test_cmd_gui_pretty_prints_json(self) -> None:
        payload = {
            "ok": True,
            "summary": "ok",
            "current_python": "/tmp/python",
            "current_runtime": {"detail": "ok"},
            "managed_runtime": {
                "exists": False,
                "usable": False,
                "python_executable": "/tmp/managed-python",
            },
            "fix": {"requested": False, "result": None, "error": None},
            "recommendation": "none",
        }
        args = mock.Mock(fix=False, pretty=True, accept_managed_runtime=False)

        with mock.patch.object(doctor, "build_gui_report", return_value=payload):
            stream = io.StringIO()
            with mock.patch.object(sys, "stdout", stream):
                exit_code = doctor.cmd_gui(args, "en")

        self.assertEqual(exit_code, 0)
        self.assertIn('"summary": "ok"', stream.getvalue())


class DoctorMcpTests(unittest.TestCase):
    def _runtime_status(
        self,
        *,
        ready: bool = True,
        trade_enabled: bool = False,
    ) -> dict[str, object]:
        return {
            "installed": ready,
            "version": "0.2.0" if ready else "0.1.0",
            "expected_version": "0.2.0",
            "package_ok": ready,
            "manifest_ok": ready,
            "runtime_profile": "local_user",
            "trade_enabled": trade_enabled,
            "tool_contract_version": "1",
            "manifest_digest": f"sha256:{'a' * 64}",
            "action_required": None if ready else "install_verified_mcp_candidate",
        }

    def _runtime_probe(
        self,
        *,
        read_ready: bool = True,
        trade_ready: bool = True,
    ):
        def probe(*, trade_enabled: bool = False):
            return self._runtime_status(
                ready=trade_ready if trade_enabled else read_ready,
                trade_enabled=trade_enabled,
            )

        return probe

    def _store(self, *, state: str, key_status: dict[str, object] | None):
        key_probe = mock.Mock(
            return_value=key_status,
        )
        store = SimpleNamespace(
            vault_status=mock.Mock(
                return_value={
                    "configured": state != "uninitialized",
                    "state": state,
                    "action_required": {
                        "uninitialized": "setup",
                        "locked": "unlock",
                        "unlocked": None,
                    }.get(state, "repair"),
                    "vault_data_path": "/private/vault.enc",
                }
            ),
            local_mcp_approval_key_status=key_probe,
            get_or_create_local_mcp_approval_key=mock.Mock(
                side_effect=AssertionError("doctor must not create approval keys")
            ),
        )
        return store, key_probe

    def test_mcp_report_distinguishes_read_and_trade_readiness_without_mutation(self) -> None:
        states = (
            ("uninitialized", None, False, "setup_vault"),
            ("locked", None, False, "unlock_vault"),
            (
                "unlocked",
                {"present": False, "valid": False, "length": 0, "fp_sha256": None},
                False,
                "provision_local_mcp_approval_key",
            ),
            (
                "unlocked",
                {"present": True, "valid": True, "length": 64, "fp_sha256": "b" * 16},
                True,
                None,
            ),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            sentinel = Path(tempdir) / "vault.enc"
            sentinel.write_bytes(b"encrypted-vault-bytes")
            before = sentinel.read_bytes()
            for state, key_status, trade_ready, action_required in states:
                with self.subTest(state=state, trade_ready=trade_ready):
                    store, key_probe = self._store(state=state, key_status=key_status)
                    with mock.patch.object(
                        doctor,
                        "_probe_local_mcp_runtime",
                        side_effect=self._runtime_probe(),
                    ), mock.patch.object(
                        doctor,
                        "_load_profile_store_module",
                        return_value=store,
                    ):
                        report = doctor.build_mcp_report("en")

                    self.assertTrue(report["read_ready"])
                    self.assertTrue(report["runtime"]["manifest_ok"])
                    self.assertTrue(report["trade_runtime"]["manifest_ok"])
                    self.assertTrue(report["trade_runtime"]["trade_enabled"])
                    self.assertEqual(report["trade_ready"], trade_ready)
                    self.assertEqual(report["action_required"], action_required)
                    self.assertEqual(sentinel.read_bytes(), before)
                    store.get_or_create_local_mcp_approval_key.assert_not_called()
                    if state == "unlocked":
                        key_probe.assert_called_once_with()
                    else:
                        key_probe.assert_not_called()
                    serialized = json.dumps(report, sort_keys=True)
                    for forbidden in (
                        "/private/vault.enc",
                        "api-spot.weex.com",
                        "profile-a",
                        "credential_ref",
                        "approval-key-secret",
                    ):
                        self.assertNotIn(forbidden, serialized)

    def test_mcp_report_prioritizes_safe_package_action_and_handles_probe_exceptions(self) -> None:
        store, _ = self._store(
            state="unlocked",
            key_status={"present": True, "valid": True, "length": 64, "fp_sha256": "b" * 16},
        )
        with mock.patch.object(
            doctor,
            "_probe_local_mcp_runtime",
            side_effect=self._runtime_probe(read_ready=False, trade_ready=False),
        ), mock.patch.object(doctor, "_load_profile_store_module", return_value=store):
            mismatch = doctor.build_mcp_report("zh")
        self.assertFalse(mismatch["read_ready"])
        self.assertFalse(mismatch["trade_ready"])
        self.assertEqual(mismatch["action_required"], "install_verified_mcp_candidate")

        with mock.patch.object(
            doctor,
            "_probe_local_mcp_runtime",
            side_effect=RuntimeError("AK_LEAK /private/path"),
        ):
            failed = doctor.build_mcp_report("en")
        self.assertFalse(failed["read_ready"])
        self.assertEqual(failed["action_required"], "repair_mcp_gateway")
        self.assertNotIn("AK_LEAK", json.dumps(failed, sort_keys=True))
        self.assertNotIn("/private/path", json.dumps(failed, sort_keys=True))

    def test_mcp_report_rejects_conflicting_runtime_action_before_vault_probe(self) -> None:
        runtime = self._runtime_status()
        runtime["action_required"] = "repair_mcp_runtime"

        def probe(*, trade_enabled: bool = False):
            if trade_enabled:
                return self._runtime_status(trade_enabled=True)
            return dict(runtime)

        with mock.patch.object(
            doctor,
            "_probe_local_mcp_runtime",
            side_effect=probe,
        ), mock.patch.object(doctor, "_load_profile_store_module") as store_loader:
            report = doctor.build_mcp_report("en")

        self.assertFalse(report["read_ready"])
        self.assertFalse(report["trade_ready"])
        self.assertEqual(report["action_required"], "repair_mcp_runtime")
        store_loader.assert_not_called()

    def test_mcp_report_requires_trade_on_contract_before_vault_probe(self) -> None:
        store, key_probe = self._store(
            state="unlocked",
            key_status={"present": True, "valid": True, "length": 64, "fp_sha256": "b" * 16},
        )

        def probe(*, trade_enabled: bool = False):
            status = self._runtime_status(trade_enabled=trade_enabled)
            if trade_enabled:
                status["manifest_ok"] = False
                status["manifest_digest"] = f"sha256:{'c' * 64}"
                status["action_required"] = "repair_mcp_contract"
            return status

        with mock.patch.object(
            doctor,
            "_probe_local_mcp_runtime",
            side_effect=probe,
        ), mock.patch.object(
            doctor,
            "_load_profile_store_module",
            return_value=store,
        ) as store_loader:
            report = doctor.build_mcp_report("en")

        self.assertTrue(report["read_ready"])
        self.assertFalse(report["trade_ready"])
        self.assertTrue(report["runtime"]["manifest_ok"])
        self.assertFalse(report["trade_runtime"]["manifest_ok"])
        self.assertEqual(report["action_required"], "repair_mcp_contract")
        self.assertFalse(report["approval_key"]["valid"])
        store_loader.assert_not_called()
        key_probe.assert_not_called()

    def test_mcp_parser_has_no_fix_and_json_and_text_render_are_safe(self) -> None:
        parser = doctor.build_parser("en")
        args = parser.parse_args(["mcp", "--pretty"])
        self.assertEqual(args.command, "mcp")
        self.assertFalse(hasattr(args, "fix"))
        with self.assertRaises(SystemExit):
            parser.parse_args(["mcp", "--fix"])
        self.assertIn("mcp", parser.format_help())

        store, _ = self._store(
            state="unlocked",
            key_status={"present": True, "valid": True, "length": 64, "fp_sha256": "b" * 16},
        )
        with mock.patch.object(
            doctor,
            "_probe_local_mcp_runtime",
            side_effect=self._runtime_probe(),
        ), mock.patch.object(doctor, "_load_profile_store_module", return_value=store):
            report = doctor.build_mcp_report("en")
        rendered = doctor.render_mcp_report("en", report)
        self.assertIn("Read ready", rendered)
        self.assertIn("Trade ready", rendered)

        with mock.patch.object(doctor, "build_mcp_report", return_value=report):
            stream = io.StringIO()
            with mock.patch.object(sys, "stdout", stream):
                exit_code = doctor.cmd_mcp(mock.Mock(pretty=True), "en")
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stream.getvalue())["read_ready"], True)


if __name__ == "__main__":
    unittest.main()
