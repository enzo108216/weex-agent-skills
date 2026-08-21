import json
import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"


class CodexModelEvalContractTests(unittest.TestCase):
    def run_wrapper(self, *args):
        return subprocess.run(
            ["node", str(EVALS / "scripts" / "run_codex_promptfoo.cjs"), *args],
            cwd=EVALS,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_codex_auth_check_is_available_and_redacted(self):
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        if not (codex_home / "config.toml").is_file() or not (codex_home / "auth.json").is_file():
            self.skipTest("Codex local authentication is not configured")
        result = self.run_wrapper("check-auth", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["auth_present"])
        self.assertTrue(payload["provider"])
        self.assertTrue(payload["model"])
        self.assertNotIn("OPENAI_API_KEY", result.stdout)
        self.assertNotIn("LITELLM_API_KEY", result.stdout)
        self.assertNotIn("Bearer ", result.stdout)

    def test_codex_model_catalog_has_all_skill_groups(self):
        catalog = json.loads(
            (EVALS / "cases" / "codex-model-tests.json").read_text(encoding="utf-8")
        )
        groups = {item["vars"]["skill"] for item in catalog}
        self.assertTrue({"weex-analysis-skill", "weex-monitor-skill", "weex-partner-skill", "weex-trader-skill"} <= groups)
        self.assertGreaterEqual(len(catalog), 8)

    def test_codex_promptfoo_config_and_html_script_are_declared(self):
        config = EVALS / "promptfooconfig.codex.yaml"
        package = json.loads((EVALS / "package.json").read_text(encoding="utf-8"))
        self.assertTrue(config.exists())
        self.assertIn("eval:codex:html", package["scripts"])
        self.assertIn("eval:codex:json", package["scripts"])
        self.assertIn("run_codex_promptfoo.cjs", package["scripts"]["eval:codex:html"])

    def test_codex_runtime_does_not_contain_literal_secret(self):
        provider = (EVALS / "scripts" / "codex_runtime.cjs").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("jp-", provider)
        self.assertNotIn("OPENAI_API_KEY=", provider)
        self.assertIn("auth.json", provider)

    def test_incomplete_investment_case_accepts_safe_refusal(self):
        catalog = json.loads(
            (EVALS / "cases" / "codex-model-tests.json").read_text(encoding="utf-8")
        )
        investment_case = next(
            item
            for item in catalog
            if item["description"] == "Codex does not turn analysis into investment advice"
        )
        self.assertIn("refuse", set(investment_case["vars"]["expected_route"].split("|")))
        investment_tokens = set(investment_case["vars"]["must_include_any"].split("|"))
        self.assertTrue({"无法", "不能保证"} & investment_tokens)

        partner_case = next(
            item
            for item in catalog
            if item["description"] == "Codex does not let Partner skill place an order"
        )
        self.assertIn("clarify", set(partner_case["vars"]["expected_route"].split("|")))

    def test_source_of_truth_case_accepts_safe_clarification(self):
        catalog = json.loads(
            (EVALS / "cases" / "codex-model-tests.json").read_text(encoding="utf-8")
        )
        case = next(
            item
            for item in catalog
            if item["description"] == "Codex preserves skills as source of truth"
        )
        self.assertIn("clarify", set(case["vars"]["expected_route"].split("|")))
