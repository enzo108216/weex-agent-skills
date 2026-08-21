#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import run_local_evals  # noqa: E402


class LocalEvalHarnessTests(unittest.TestCase):
    def test_case_catalog_covers_all_skill_groups(self) -> None:
        groups = {case["group"] for case in run_local_evals.load_case_catalog()}
        self.assertTrue({"analysis", "monitor", "partner", "trader", "repository"} <= groups)

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

    def test_local_eval_summary_is_machine_readable(self) -> None:
        summary = run_local_evals.run_all()
        self.assertTrue(summary["ok"], summary)
        self.assertGreaterEqual(summary["case_count"], 15)
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
