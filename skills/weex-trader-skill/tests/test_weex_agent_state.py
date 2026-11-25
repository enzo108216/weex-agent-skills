#!/usr/bin/env python3
from __future__ import annotations

import os
import inspect
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

import weex_agent_state as agent_state  # noqa: E402
from weex_gui_bootstrap import RuntimeProbe  # noqa: E402


class AgentStateGuiRuntimeTests(unittest.TestCase):
    def test_runtime_state_can_skip_default_profile_credential_probe(self) -> None:
        self.assertIn(
            "probe_default_profile_usable",
            inspect.signature(agent_state.build_agent_runtime_state).parameters,
        )
        metadata_summary = {
            "count": 1,
            "default_profile_id": "profile-a",
            "default_profile_name": "main",
            "summary": [],
        }
        with mock.patch.object(agent_state, "_probe_required_modules", return_value=(True, [])):
            with mock.patch.object(agent_state, "validate_runtime_environment", return_value={"ok": True, "issues": []}):
                with mock.patch.object(agent_state, "_load_metadata_summary", return_value=metadata_summary):
                    with mock.patch.object(agent_state, "_load_vault_summary", return_value={"configured": True, "mode": "manual_once"}):
                        with mock.patch.object(agent_state, "_load_store_module", return_value=object()):
                            with mock.patch.object(agent_state, "_probe_default_profile_usable") as credential_probe:
                                payload = agent_state.build_agent_runtime_state(
                                    preferred_language="zh",
                                    command="partner.preflight",
                                    probe_default_profile_usable=False,
                                )

        credential_probe.assert_not_called()
        self.assertIsNone(payload["profiles"]["default_profile_usable"])

    def test_runtime_state_adds_safe_mcp_without_changing_legacy_host_readiness(self) -> None:
        mcp_status = {
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
        }
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(
                    agent_state,
                    "_probe_required_modules",
                    return_value=(False, ["cryptography"]),
                ), mock.patch.object(
                    agent_state,
                    "validate_runtime_environment",
                    return_value={"ok": True, "issues": []},
                ), mock.patch.object(
                    agent_state,
                    "_load_metadata_summary",
                    return_value={
                        "count": 0,
                        "default_profile_id": None,
                        "default_profile_name": None,
                        "summary": [],
                    },
                ), mock.patch.object(
                    agent_state,
                    "_load_vault_summary",
                    return_value={"configured": False, "mode": None},
                ), mock.patch.object(
                    agent_state,
                    "_load_store_module",
                    return_value=None,
                ), mock.patch.object(
                    agent_state,
                    "_probe_local_mcp_runtime",
                    return_value=mcp_status,
                ):
                    payload = agent_state.build_agent_runtime_state(
                        preferred_language="en",
                        command="contract.list-endpoints",
                    )

        self.assertFalse(payload["host"]["requirements_ready"])
        self.assertEqual(payload["host"]["missing_modules"], ["cryptography"])
        self.assertEqual(payload["mcp"], mcp_status)
        self.assertNotIn("mcp", agent_state.REQUIRED_MODULES)

    def test_mcp_probe_projection_and_runtime_cache_never_store_unsafe_fields(self) -> None:
        self.assertTrue(callable(agent_state.project_mcp_status))
        self.assertFalse(hasattr(agent_state, "_project_mcp_status"))
        raw = {
            "installed": True,
            "version": "0.2.0",
            "expected_version": "0.2.0",
            "package_ok": True,
            "manifest_ok": True,
            "runtime_profile": "local_user",
            "trade_enabled": False,
            "tool_contract_version": "1",
            "manifest_digest": f"sha256:{'a' * 64}",
            "action_required": None,
            "origin": "https://private.weex.tech",
            "profile": "profile-a",
            "path": "/private/artifact.whl",
            "key": "AK_LEAK",
        }
        module = SimpleNamespace(probe_local_mcp_runtime=lambda: dict(raw))
        with mock.patch.object(agent_state.importlib, "import_module", return_value=module):
            safe = agent_state._probe_local_mcp_runtime()
        self.assertEqual(
            set(safe),
            {
                "installed",
                "version",
                "expected_version",
                "package_ok",
                "manifest_ok",
                "runtime_profile",
                "trade_enabled",
                "tool_contract_version",
                "manifest_digest",
                "action_required",
            },
        )
        serialized = json.dumps(safe, sort_keys=True)
        for forbidden in ("private.weex.tech", "profile-a", "/private/", "AK_LEAK"):
            self.assertNotIn(forbidden, serialized)

        with mock.patch.object(
            agent_state.importlib,
            "import_module",
            side_effect=RuntimeError("SK_LEAK /private/gateway.py"),
        ):
            unavailable = agent_state._probe_local_mcp_runtime()
        self.assertEqual(unavailable["action_required"], "repair_mcp_gateway")
        self.assertNotIn("SK_LEAK", json.dumps(unavailable, sort_keys=True))

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(
                    agent_state,
                    "build_agent_runtime_state",
                    return_value={"schema_version": 1, "mcp": safe},
                ):
                    cached = agent_state.refresh_agent_runtime_state(command="test")
                cache_text = agent_state.agent_runtime_path().read_text(encoding="utf-8")
        self.assertEqual(cached["mcp"], safe)
        for forbidden in ("private.weex.tech", "profile-a", "/private/", "AK_LEAK"):
            self.assertNotIn(forbidden, cache_text)

    def test_mcp_projection_rejects_malformed_versions_trade_mode_and_actions(self) -> None:
        ready = {
            "installed": True,
            "version": "0.2.0",
            "expected_version": "0.2.0",
            "package_ok": True,
            "manifest_ok": True,
            "runtime_profile": "local_user",
            "trade_enabled": False,
            "tool_contract_version": "1",
            "manifest_digest": f"sha256:{'a' * 64}",
            "action_required": None,
        }
        malformed = (
            {"version": "AK_LEAK", "expected_version": "AK_LEAK"},
            {"tool_contract_version": "profile-a"},
            {"trade_enabled": "false"},
            {"action_required": "AK_LEAK"},
            {"action_required": "repair_mcp_runtime"},
        )
        for overrides in malformed:
            with self.subTest(overrides=overrides):
                raw = {**ready, **overrides}
                projected = agent_state.project_mcp_status(raw)

                self.assertFalse(projected["manifest_ok"])
                self.assertIsNotNone(projected["action_required"])
                serialized = json.dumps(projected, sort_keys=True)
                for forbidden in ("AK_LEAK", "profile-a"):
                    self.assertNotIn(forbidden, serialized)

    def test_mcp_projection_rejects_non_string_unhashable_actions(self) -> None:
        ready = {
            "installed": True,
            "version": "0.2.0",
            "expected_version": "0.2.0",
            "package_ok": True,
            "manifest_ok": True,
            "runtime_profile": "local_user",
            "trade_enabled": False,
            "tool_contract_version": "1",
            "manifest_digest": f"sha256:{'a' * 64}",
            "action_required": None,
        }
        for action in ({"key": "AK_LEAK"}, ["AK_LEAK"], {"AK_LEAK"}):
            with self.subTest(action_type=type(action).__name__):
                projected = agent_state.project_mcp_status(
                    {**ready, "action_required": action}
                )

                self.assertFalse(projected["manifest_ok"])
                self.assertEqual(
                    projected["action_required"],
                    "repair_mcp_gateway",
                )
                self.assertNotIn("AK_LEAK", json.dumps(projected, sort_keys=True))

    def test_preflight_requires_managed_runtime_even_when_current_runtime_has_tk(self) -> None:
        current_probe = RuntimeProbe(usable=True, reason="ok", returncode=0, tk_version="8.6", tcl_version="8.6")
        missing_managed_python = Path("/tmp/missing-weex-managed-python")

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(agent_state.platform, "system", return_value="Darwin"):
                    with mock.patch.object(agent_state.platform, "release", return_value="25.2.0"):
                        with mock.patch.object(agent_state, "probe_runtime", return_value=current_probe):
                            with mock.patch.object(agent_state, "managed_venv_python", return_value=missing_managed_python):
                                payload = agent_state.build_agent_init_state(preferred_language="en")

        self.assertTrue(payload["host"]["tkinter_available"])
        self.assertFalse(payload["host"]["gui_available"])
        self.assertEqual(payload["routes"]["profile_management"], "macos_cli_en")
        self.assertTrue(payload["host"]["gui_bootstrap_recommended"])
        self.assertFalse(payload["host"]["gui_runtime"]["ready"])
        self.assertEqual(payload["host"]["gui_runtime"]["action"], "explicit_setup_required")
        self.assertTrue(payload["host"]["gui_runtime"]["requires_user_consent"])
        self.assertIn("--accept-managed-runtime", payload["host"]["gui_runtime"]["error"])

    def test_preflight_reports_explicit_setup_required_without_auto_prepare(self) -> None:
        failing_probe = RuntimeProbe(usable=False, reason="tk_crashed", returncode=-6)

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(agent_state.platform, "system", return_value="Darwin"):
                    with mock.patch.object(agent_state.platform, "release", return_value="25.2.0"):
                        with mock.patch.object(agent_state, "probe_runtime", return_value=failing_probe):
                            payload = agent_state.build_agent_init_state(preferred_language="zh")

        self.assertFalse(payload["host"]["tkinter_available"])
        self.assertFalse(payload["host"]["gui_available"])
        self.assertEqual(payload["routes"]["profile_management"], "macos_cli_zh")
        self.assertTrue(payload["host"]["gui_bootstrap_recommended"])
        self.assertEqual(payload["host"]["gui_runtime"]["action"], "explicit_setup_required")
        self.assertFalse(payload["host"]["gui_runtime"]["attempted"])
        self.assertTrue(payload["host"]["gui_runtime"]["requires_user_consent"])
        self.assertEqual(
            payload["host"]["gui_runtime"]["setup_command"],
            "python3 scripts/weex_gui_bootstrap.py ensure --accept-managed-runtime --pretty",
        )
        self.assertIn("--accept-managed-runtime", payload["host"]["gui_runtime"]["error"])
        self.assertIn("让 AI", payload["host"]["gui_runtime"]["error"])

    def test_preflight_falls_back_to_cli_when_existing_managed_runtime_is_broken(self) -> None:
        current_probe = RuntimeProbe(usable=False, reason="tk_crashed", returncode=-6)
        managed_probe = RuntimeProbe(usable=False, reason="missing_modules", returncode=2, missing_modules=("cryptography",))
        managed_python = Path("/tmp/weex-managed-python")

        def fake_probe(path: str) -> RuntimeProbe:
            return managed_probe if path == str(managed_python) else current_probe

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                with mock.patch.object(agent_state.platform, "system", return_value="Darwin"):
                    with mock.patch.object(agent_state.platform, "release", return_value="25.2.0"):
                        with mock.patch.object(agent_state, "probe_runtime", side_effect=fake_probe):
                            with mock.patch.object(agent_state, "managed_venv_python", return_value=managed_python):
                                with mock.patch.object(Path, "exists", return_value=True):
                                    payload = agent_state.build_agent_init_state(preferred_language="en")

        self.assertFalse(payload["host"]["gui_available"])
        self.assertEqual(payload["routes"]["profile_management"], "macos_cli_en")
        self.assertTrue(payload["host"]["gui_bootstrap_recommended"])
        self.assertEqual(payload["host"]["gui_runtime"]["action"], "explicit_setup_required")
        self.assertTrue(payload["host"]["gui_runtime"]["requires_user_consent"])
        self.assertIn("--accept-managed-runtime", payload["host"]["gui_runtime"]["error"])
        self.assertIn("Ask the AI", payload["host"]["gui_runtime"]["error"])
        self.assertIn("cryptography", payload["host"]["gui_runtime"]["error"])

    def test_preflight_respects_disabled_gui_bootstrap_env(self) -> None:
        failing_probe = RuntimeProbe(usable=False, reason="tk_crashed", returncode=-6)

        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(
                os.environ,
                {
                    "WEEX_TRADER_SKILL_HOME": tempdir,
                    "WEEX_GUI_RUNTIME_DISABLE": "1",
                },
                clear=False,
            ):
                with mock.patch.object(agent_state.platform, "system", return_value="Darwin"):
                    with mock.patch.object(agent_state.platform, "release", return_value="25.2.0"):
                        with mock.patch.object(agent_state, "probe_runtime", return_value=failing_probe):
                            payload = agent_state.build_agent_init_state(preferred_language="en")

        self.assertFalse(payload["host"]["gui_available"])
        self.assertTrue(payload["host"]["gui_runtime"]["disabled"])
        self.assertFalse(payload["host"]["gui_runtime"]["attempted"])
        self.assertEqual(payload["routes"]["profile_management"], "macos_cli_en")

    def test_runtime_state_tracks_api_override_env_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(
                os.environ,
                {
                    "WEEX_TRADER_SKILL_HOME": tempdir,
                    "WEEX_LOCALE": "zh-CN",
                    "WEEX_API_BASE": "https://api.weex.tech",
                    "WEEX_CONTRACT_API_BASE": "https://contract.weex.tech",
                    "WEEX_SPOT_API_BASE": "https://spot.weex.com",
                },
                clear=False,
            ):
                with mock.patch.object(agent_state, "_probe_required_modules", return_value=(True, [])):
                    with mock.patch.object(agent_state, "_load_store_module", return_value=None):
                        payload = agent_state.build_agent_runtime_state(
                            preferred_language="en",
                            command="contract.list-endpoints",
                        )

        self.assertTrue(payload["env"]["WEEX_LOCALE"])
        self.assertTrue(payload["env"]["WEEX_API_BASE"])
        self.assertTrue(payload["env"]["WEEX_CONTRACT_API_BASE"])
        self.assertTrue(payload["env"]["WEEX_SPOT_API_BASE"])
        self.assertTrue(payload["env_validation"]["ok"])

    def test_runtime_state_reports_invalid_runtime_environment_values(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(
                os.environ,
                {
                    "WEEX_TRADER_SKILL_HOME": tempdir,
                    "WEEX_API_TIMEOUT": "abc",
                    "WEEX_API_BASE": "not-a-url",
                    "WEEX_CONTRACT_API_BASE": "https://contract.env.test",
                },
                clear=False,
            ):
                with mock.patch.object(agent_state, "_probe_required_modules", return_value=(True, [])):
                    with mock.patch.object(agent_state, "_load_store_module", return_value=None):
                        payload = agent_state.build_agent_runtime_state(
                            preferred_language="zh",
                            command="contract.place-order",
                        )

        self.assertFalse(payload["env_validation"]["ok"])
        self.assertGreaterEqual(len(payload["env_validation"]["issues"]), 2)
        self.assertTrue(any("WEEX_API_TIMEOUT" in issue for issue in payload["env_validation"]["issues"]))
        self.assertTrue(any("WEEX_API_BASE" in issue for issue in payload["env_validation"]["issues"]))
        self.assertTrue(any("WEEX_CONTRACT_API_BASE" in issue for issue in payload["env_validation"]["issues"]))

    def test_private_runtime_preflight_reports_missing_modules_and_invalid_env(self) -> None:
        with mock.patch.object(agent_state, "_probe_required_modules", return_value=(False, ["cryptography", "requests"])):
            with mock.patch.dict(
                os.environ,
                {
                    "WEEX_API_TIMEOUT": "abc",
                },
                clear=False,
            ):
                with self.assertRaises(agent_state.RuntimePreflightError) as exc_info:
                    agent_state.ensure_private_runtime_ready(command="spot.place-order")

        message = str(exc_info.exception)
        self.assertIn("Private WEEX command preflight failed", message)
        self.assertIn("cryptography", message)
        self.assertIn("requests", message)
        self.assertIn("WEEX_API_TIMEOUT", message)

    def test_private_runtime_preflight_can_auto_run_runtime_setup(self) -> None:
        with mock.patch.object(
            agent_state,
            "_probe_required_modules",
            side_effect=[(False, ["cryptography"]), (True, [])],
        ):
            with mock.patch.object(
                agent_state,
                "validate_runtime_environment",
                return_value={"ok": True, "issues": []},
            ):
                with mock.patch.object(
                    agent_state,
                    "_run_runtime_setup",
                    return_value={"returncode": 0, "payload": {"ok": True}},
                ) as setup_mock:
                    agent_state.ensure_private_runtime_ready(
                        command="contract.place-order",
                        auto_setup=True,
                        language="zh",
                    )

        setup_mock.assert_called_once_with(language="zh")

    def test_private_runtime_preflight_does_not_auto_run_setup_for_invalid_env(self) -> None:
        with mock.patch.object(agent_state, "_probe_required_modules", return_value=(False, ["cryptography"])):
            with mock.patch.object(
                agent_state,
                "validate_runtime_environment",
                return_value={"ok": False, "issues": ["WEEX_API_TIMEOUT must be a positive number of seconds; got 'abc'."]},
            ):
                with mock.patch.object(agent_state, "_run_runtime_setup") as setup_mock:
                    with self.assertRaises(agent_state.RuntimePreflightError):
                        agent_state.ensure_private_runtime_ready(
                            command="spot.place-order",
                            auto_setup=True,
                            language="en",
                        )

        setup_mock.assert_not_called()

    def test_private_runtime_preflight_clears_stale_profile_store_cache_after_auto_setup(self) -> None:
        stale_module = object()
        with mock.patch.object(
            agent_state,
            "_probe_required_modules",
            side_effect=[(False, ["cryptography"]), (True, [])],
        ):
            with mock.patch.object(
                agent_state,
                "validate_runtime_environment",
                return_value={"ok": True, "issues": []},
            ):
                with mock.patch.object(
                    agent_state,
                    "_run_runtime_setup",
                    return_value={"returncode": 0, "payload": {"ok": True}},
                ):
                    with mock.patch.dict(sys.modules, {"weex_profile_store": stale_module}, clear=False):
                        agent_state.ensure_private_runtime_ready(
                            command="contract.place-order",
                            auto_setup=True,
                            language=None,
                        )
                        self.assertNotIn("weex_profile_store", sys.modules)


if __name__ == "__main__":
    unittest.main()
