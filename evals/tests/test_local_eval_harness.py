#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import run_local_evals  # noqa: E402
from tools.weex_eval_offline_guard import network_blocked  # noqa: E402


class LocalEvalHarnessTests(unittest.TestCase):
    def test_case_catalog_covers_all_skill_groups(self) -> None:
        cases = run_local_evals.load_case_catalog()
        groups = {case["group"] for case in cases}
        self.assertTrue({"analysis", "monitor", "partner", "trader", "repository"} <= groups)
        self.assertEqual({case["case_id"] for case in cases}, set(run_local_evals.HANDLERS))

    def test_case_catalog_rejects_duplicate_case_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "cases.json"
            catalog.write_text(
                json.dumps(
                    [
                        {"vars": {"case_id": "analysis.empty_order_risk"}},
                        {"vars": {"case_id": "analysis.empty_order_risk"}},
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.object(run_local_evals, "CASE_CATALOG", catalog):
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    run_local_evals.load_case_catalog()

    def test_run_all_rejects_catalog_handler_mismatch(self) -> None:
        with mock.patch.dict(run_local_evals.HANDLERS, {"repository.orphan": lambda: {}}, clear=False):
            summary = run_local_evals.run_all()
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["failed"], ["catalog"])
        self.assertIn("orphan_handlers", summary["error"]["message"])

    def test_unknown_case_returns_machine_readable_failure(self) -> None:
        result = run_local_evals.run_case("does.not.exist")
        self.assertFalse(result["ok"])
        self.assertEqual(result["case_id"], "does.not.exist")
        self.assertEqual(result["group"], "unknown")

    def test_deterministic_eval_environment_strips_provider_secrets(self) -> None:
        with mock.patch.dict(
            run_local_evals.os.environ,
            {
                "LITELLM_API_KEY": "provider-secret",
                "CUSTOM_ACCESS_TOKEN": "access-secret",
                "GITHUB_TOKEN": "github-secret",
                "AUTH_TOKEN": "auth-secret",
                "PRIVATE_TOKEN": "private-secret",
                "UNRELATED_SETTING": "kept",
            },
            clear=False,
        ):
            environment = run_local_evals._clean_eval_environment(Path("/tmp/eval-home"))
        self.assertNotIn("LITELLM_API_KEY", environment)
        self.assertNotIn("CUSTOM_ACCESS_TOKEN", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("AUTH_TOKEN", environment)
        self.assertNotIn("PRIVATE_TOKEN", environment)
        self.assertNotIn("UNRELATED_SETTING", environment)
        self.assertEqual(environment["HOME"], "/tmp/eval-home")

    def test_analysis_empty_order_risk_case_passes(self) -> None:
        result = run_local_evals.run_case("analysis.empty_order_risk")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["group"], "analysis")
        self.assertIn("order_risk_missing_order_preview", result["details"]["degraded_reasons"])

    def test_monitor_missing_trading_mode_fails_closed(self) -> None:
        result = run_local_evals.run_case("monitor.missing_trading_mode")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["details"]["error_type"], "MonitorInputError")
        self.assertIn("trading_mode is required", result["details"]["message"])

    def test_offline_network_guard_blocks_socket_egress(self) -> None:
        result = run_local_evals.run_case("repository.offline_network_guard")
        self.assertTrue(result["ok"], result)

    def test_offline_network_guard_blocks_udp_sendto(self) -> None:
        result = run_local_evals.run_case("repository.offline_udp_guard")
        self.assertTrue(result["ok"], result)

    def test_offline_network_guard_blocks_unwrapped_subprocess(self) -> None:
        with network_blocked():
            with self.assertRaisesRegex(RuntimeError, "blocked network access"):
                subprocess.run(["/bin/echo", "not allowed"], check=False)

    def test_partner_fixture_case_validates_full_contract(self) -> None:
        fixture = {
            "schema_version": 1,
            "scenarios": [
                {
                    "id": "bad-route",
                    "prompt": "test",
                    "expected": {"disposition": "route", "operation": "not-allowlisted"},
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            run_local_evals.validate_partner_natural_language_fixture(fixture)

    def test_local_eval_summary_is_machine_readable(self) -> None:
        summary = run_local_evals.run_all()
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["case_count"], len(run_local_evals.load_case_catalog()))
        self.assertGreaterEqual(summary["case_count"], 17)
        self.assertEqual(summary["failed"], [])

    def test_cli_json_entrypoint_returns_zero_when_all_cases_pass(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "run_local_evals.py"), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["failed"], [])


if __name__ == "__main__":
    unittest.main()
