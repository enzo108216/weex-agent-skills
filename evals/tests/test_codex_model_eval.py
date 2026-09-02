import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"


class CodexModelEvalContractTests(unittest.TestCase):
    def run_wrapper(self, *args, env=None):
        return subprocess.run(
            ["node", str(EVALS / "scripts" / "run_codex_promptfoo.cjs"), *args],
            cwd=EVALS,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def run_node(self, source, *, env=None):
        return subprocess.run(
            ["node", "-e", source],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_codex_auth_check_is_available_and_redacted(self):
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        target_env = {
            "WEEX_CODEX_EVAL_MODEL": os.environ.get("WEEX_CODEX_EVAL_MODEL", ""),
            "WEEX_CODEX_EVAL_MODEL_PROVIDER": os.environ.get(
                "WEEX_CODEX_EVAL_MODEL_PROVIDER", ""
            ),
            "WEEX_CODEX_EVAL_REASONING_EFFORT": os.environ.get(
                "WEEX_CODEX_EVAL_REASONING_EFFORT", ""
            ),
        }
        provider_key = os.environ.get("LITELLM_API_KEY", "")
        if not (codex_home / "config.toml").is_file() or not all(target_env.values()) or not provider_key:
            self.skipTest("Codex local evaluation target is not configured")
        env = {"PATH": os.environ["PATH"], "HOME": str(Path.home()), **target_env, "LITELLM_API_KEY": provider_key}
        if "CODEX_HOME" in os.environ:
            env["CODEX_HOME"] = os.environ["CODEX_HOME"]
        result = self.run_wrapper("check-auth", "--json", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["auth_present"])
        self.assertTrue(payload["provider"])
        self.assertTrue(payload["model"])
        self.assertEqual(payload["model"], target_env["WEEX_CODEX_EVAL_MODEL"])
        self.assertNotIn("OPENAI_API_KEY", result.stdout)
        self.assertNotIn("LITELLM_API_KEY", result.stdout)
        self.assertNotIn("Bearer ", result.stdout)

    def test_runtime_uses_non_secret_target_environment_and_never_reads_auth_file(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            (codex_home / "config.toml").write_text(
                "\n".join(
                    [
                        'model = "gpt-test"',
                        'model_provider = "custom"',
                        "[model_providers.custom]",
                        'base_url = "https://gateway.example.test/v1"',
                        'wire_api = "responses"',
                    ]
                ),
                encoding="utf-8",
            )
            env = {
                "PATH": os.environ["PATH"],
                "HOME": directory,
                "CODEX_HOME": directory,
                "WEEX_CODEX_EVAL_MODEL": "gpt-test",
                "WEEX_CODEX_EVAL_MODEL_PROVIDER": "custom",
                "WEEX_CODEX_EVAL_REASONING_EFFORT": "high",
            }
            result = self.run_node(
                "const runtime = require('./evals/scripts/codex_runtime.cjs'); "
                "process.stdout.write(JSON.stringify(runtime.readCodexRuntime()));",
                env=env,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["model"], "gpt-test")
        self.assertEqual(payload["providerName"], "custom")
        self.assertEqual(payload["reasoningEffort"], "high")
        self.assertNotIn("authPath", payload)

    def test_runtime_fails_closed_when_target_model_does_not_match_current_config(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            (codex_home / "config.toml").write_text(
                'model = "gpt-current"\nmodel_provider = "custom"\n'
                "[model_providers.custom]\n"
                'base_url = "https://gateway.example.test/v1"\n'
                'wire_api = "responses"\n',
                encoding="utf-8",
            )
            env = {
                "PATH": os.environ["PATH"],
                "HOME": directory,
                "CODEX_HOME": directory,
                "WEEX_CODEX_EVAL_MODEL": "gpt-stale",
                "WEEX_CODEX_EVAL_MODEL_PROVIDER": "custom",
                "WEEX_CODEX_EVAL_REASONING_EFFORT": "high",
            }
            result = self.run_node(
                "const runtime = require('./evals/scripts/codex_runtime.cjs'); runtime.readCodexRuntime();",
                env=env,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WEEX_CODEX_EVAL_MODEL", result.stderr)

    def test_model_process_environment_is_allowlisted(self):
        result = self.run_node(
            "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
            "process.stdout.write(JSON.stringify(runtime.buildEvalProcessEnv({"
            "PATH:'/bin', HOME:'/tmp/home', CODEX_HOME:'/tmp/codex', "
            "WEEX_CODEX_EVAL_MODEL:'gpt-test', WEEX_CODEX_EVAL_MODEL_PROVIDER:'custom', "
            "WEEX_CODEX_EVAL_REASONING_EFFORT:'high', LITELLM_API_KEY:'gateway-secret', "
            "OPENAI_API_KEY:'secret', OTHER_SECRET:'secret'}, 'LITELLM_API_KEY')));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["WEEX_CODEX_EVAL_MODEL"], "gpt-test")
        self.assertEqual(payload["LITELLM_API_KEY"], "gateway-secret")
        self.assertEqual(payload["PROMPTFOO_DISABLE_UPDATE"], "1")
        self.assertEqual(payload["PROMPTFOO_DISABLE_SHARING"], "1")
        self.assertIn(".promptfoo", payload["PROMPTFOO_CONFIG_DIR"])
        self.assertIn(".promptfoo", payload["PROMPTFOO_LOG_DIR"])
        self.assertNotIn("OPENAI_API_KEY", payload)
        self.assertNotIn("OTHER_SECRET", payload)

    def test_deterministic_promptfoo_environment_is_allowlisted(self):
        result = self.run_node(
            "const runtime = require('./evals/scripts/run_promptfoo.cjs'); "
            "process.stdout.write(JSON.stringify(runtime.buildEvalProcessEnv({"
            "PATH:'/bin', HOME:'/tmp/home', GITHUB_TOKEN:'secret', "
            "LITELLM_API_KEY:'secret', UNRELATED_SETTING:'drop'})));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["PATH"], "/bin")
        self.assertNotIn("GITHUB_TOKEN", payload)
        self.assertNotIn("LITELLM_API_KEY", payload)
        self.assertNotIn("UNRELATED_SETTING", payload)
        self.assertEqual(payload["PROMPTFOO_DISABLE_UPDATE"], "1")

    def test_deterministic_runner_rejects_a_different_promptfoo_config(self):
        result = self.run_node(
            "const runtime = require('./evals/scripts/run_promptfoo.cjs'); "
            "try { runtime.validateLocalEvalArguments(['eval','--config','other.yaml']); process.exit(2); } "
            "catch (error) { process.stdout.write(error.message); }"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("promptfooconfig.yaml", result.stdout)

    def test_eval_defaults_to_repeated_runs_but_export_does_not_get_eval_flags(self):
        result = self.run_node(
            "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
            "process.stdout.write(JSON.stringify([runtime.addDefaultRepeat(['eval'], {WEEX_CODEX_EVAL_REPEAT:'3'}), "
            "runtime.addDefaultRepeat(['export','eval','eval-id'], {WEEX_CODEX_EVAL_REPEAT:'3'})]));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload[0][-2:], ["--repeat", "3"])
        self.assertEqual(payload[1], ["export", "eval", "eval-id"])
        result = self.run_node(
            "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
            "process.stdout.write(JSON.stringify(runtime.addDefaultRepeat(['eval','--repeat=1'], {WEEX_CODEX_EVAL_REPEAT:'3'})));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["eval", "--repeat=1"])

    def test_model_case_catalog_is_validated_before_provider_run(self):
        result = self.run_node(
            "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
            "process.stdout.write(JSON.stringify(runtime.validateModelCaseCatalog()));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertGreaterEqual(payload["caseCount"], 50)

    def test_artifact_safety_checker_rejects_secret_values_and_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "result.json"
            artifact.write_text('{"LITELLM_API_KEY":"gateway-secret"}', encoding="utf-8")
            result = self.run_node(
                "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
                f"try {{ runtime.assertArtifactSafety({json.dumps(str(artifact))}, {{"
                "LITELLM_API_KEY:'gateway-secret'}); process.exit(2); } "
                "catch (error) { process.stdout.write(error.message); }"
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sensitive", result.stdout)

    def test_artifact_safety_checker_allows_documented_secret_field_names(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "result.json"
            artifact.write_text(
                '{"doc":"api_secret and api_passphrase are field names"}',
                encoding="utf-8",
            )
            result = self.run_node(
                "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
                f"process.stdout.write(String(runtime.assertArtifactSafety({json.dumps(str(artifact))}, {{}})));"
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "true")

    def test_output_paths_collects_all_variadic_output_files(self):
        result = self.run_node(
            "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
            "process.stdout.write(JSON.stringify(runtime.outputPaths(['eval','--output','a.json','b.html'])));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["a.json", "b.html"])

    def test_managed_artifact_completeness_rejects_partial_results(self):
        artifacts = EVALS / "artifacts"
        artifacts.mkdir(exist_ok=True)
        artifact = artifacts / "codex-completeness-test.json"
        try:
            artifact.write_text(
                json.dumps({
                    "evalId": "eval-test",
                    "results": {
                        "results": [{}],
                        "stats": {"successes": 1, "failures": 0, "errors": 0},
                    },
                }),
                encoding="utf-8",
            )
            result = self.run_node(
                "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
                "try { runtime.validateManagedArtifactCompleteness("
                "['eval','--repeat','1','--output','artifacts/codex-completeness-test.json']); process.exit(2); } "
                "catch (error) { process.stdout.write(error.message); }"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("result count mismatch", result.stdout)
        finally:
            artifact.unlink(missing_ok=True)

    def test_compact_html_artifact_injects_summary_table_styles(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "report.html"
            artifact.write_text("<html><head></head><body><table></table></body></html>", encoding="utf-8")
            result = self.run_node(
                "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
                f"process.stdout.write(String(runtime.compactHtmlArtifact({json.dumps(str(artifact))})));"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "true")
            content = artifact.read_text(encoding="utf-8")
            self.assertIn('id="weex-report-compact-table"', content)
            self.assertIn("-webkit-line-clamp: 3", content)
            self.assertIn('data-variable-name="scenario_type"', content)
            self.assertIn('id="weex-report-column-layout"', content)
            self.assertNotIn("nth-child(6)", content)

    def test_compact_html_artifact_upgrades_legacy_fixed_column_styles(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "legacy-report.html"
            artifact.write_text(
                '<html><head><style id="weex-report-compact-table">'
                'th:nth-child(6){display:none}</style></head><body><table></table></body></html>',
                encoding="utf-8",
            )
            result = self.run_node(
                "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
                f"process.stdout.write(String(runtime.compactHtmlArtifact({json.dumps(str(artifact))})));"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "true")
            content = artifact.read_text(encoding="utf-8")
            self.assertIn('id="weex-report-column-layout"', content)
            self.assertIn('data-variable-name="scenario_type"', content)
            self.assertNotIn("nth-child(6)", content)

    def test_model_case_catalog_requires_skill_and_query_for_guided_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "cases.json"
            catalog.write_text(
                json.dumps(
                    [{
                        "description": "bad",
                        "vars": {
                            "case_id": "bad-guided",
                            "routing_mode": "guided_policy",
                            "scenario_type": "positive_read_only",
                            "language": "zh",
                            "expected_route": "trader",
                            "requires_confirmation": False,
                            "must_not_execute": True,
                        },
                    }]
                ),
                encoding="utf-8",
            )
            result = self.run_node(
                "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
                f"try {{ runtime.validateModelCaseCatalog({json.dumps(str(catalog))}); process.exit(2); }} "
                "catch (error) { process.stdout.write(error.message); }"
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, "skill|query")

    def test_model_case_catalog_accepts_auto_router_without_skill_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / "cases.json"
            catalog.write_text(
                json.dumps(
                    [{
                        "description": "auto router",
                        "vars": {
                            "case_id": "router-analysis",
                            "routing_mode": "auto_router",
                            "scenario_type": "cross_skill_route",
                            "language": "zh",
                            "query": "分析这份账户快照",
                            "expected_route": "analysis",
                            "expected_operation": "analyze-snapshot",
                            "requires_confirmation": False,
                            "must_not_execute": True,
                            "must_include_any": "分析|快照",
                        },
                    }]
                ),
                encoding="utf-8",
            )
            result = self.run_node(
                "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
                f"process.stdout.write(JSON.stringify(runtime.validateModelCaseCatalog({json.dumps(str(catalog))})));"
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["caseCount"], 1)

    def test_model_runner_rejects_a_different_promptfoo_config(self):
        result = self.run_node(
            "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
            "try { runtime.validateModelEvalArguments(['eval','--config','other.yaml']); process.exit(2); } "
            "catch (error) { process.stdout.write(error.message); }"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("promptfooconfig.codex.yaml", result.stdout)

    def test_model_runner_pins_default_promptfoo_config(self):
        result = self.run_node(
            "const runtime = require('./evals/scripts/run_codex_promptfoo.cjs'); "
            "process.stdout.write(JSON.stringify(runtime.validateModelEvalArguments(['eval'])));"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[:3], ["eval", "--config", "promptfooconfig.codex.yaml"])

    def test_runtime_rejects_reasoning_effort_unsupported_by_promptfoo(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            (codex_home / "config.toml").write_text(
                'model = "gpt-test"\nmodel_provider = "custom"\n'
                "[model_providers.custom]\n"
                'base_url = "https://gateway.example.test/v1"\n'
                'wire_api = "responses"\n',
                encoding="utf-8",
            )
            env = {
                "CODEX_HOME": directory,
                "WEEX_CODEX_EVAL_MODEL": "gpt-test",
                "WEEX_CODEX_EVAL_MODEL_PROVIDER": "custom",
                "WEEX_CODEX_EVAL_REASONING_EFFORT": "none",
            }
            result = self.run_node(
                "const runtime = require('./evals/scripts/codex_runtime.cjs'); "
                "try { runtime.readCodexRuntime(); process.exit(2); } "
                "catch (error) { process.stdout.write(error.message); }",
                env={"PATH": os.environ["PATH"], **env},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reasoning", result.stdout)

    def test_runtime_supports_a_noncustom_codex_provider_without_custom_base_url(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            (codex_home / "config.toml").write_text(
                'model = "gpt-test"\nmodel_provider = "openai"\n',
                encoding="utf-8",
            )
            env = {
                "CODEX_HOME": directory,
                "WEEX_CODEX_EVAL_MODEL": "gpt-test",
                "WEEX_CODEX_EVAL_MODEL_PROVIDER": "openai",
                "WEEX_CODEX_EVAL_REASONING_EFFORT": "high",
            }
            result = self.run_node(
                "const runtime = require('./evals/scripts/codex_runtime.cjs'); "
                "process.stdout.write(JSON.stringify(runtime.readCodexRuntime()));",
                env={"PATH": os.environ["PATH"], **env},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["providerName"], "openai")

    def test_codex_model_catalog_has_all_skill_groups(self):
        catalog = json.loads(
            (EVALS / "cases" / "codex-model-tests.json").read_text(encoding="utf-8")
        )
        groups = {item["vars"].get("skill") for item in catalog if item["vars"].get("skill")}
        self.assertTrue({"weex-analysis-skill", "weex-monitor-skill", "weex-partner-skill", "weex-trader-skill"} <= groups)
        self.assertGreaterEqual(len(catalog), 50)

    def test_model_catalog_has_full_query_coverage_contract(self):
        catalog = json.loads(
            (EVALS / "cases" / "codex-model-tests.json").read_text(encoding="utf-8")
        )
        case_ids = [item["vars"].get("case_id") for item in catalog]
        self.assertTrue(all(case_ids))
        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertTrue(all(item["vars"].get("scenario_type") for item in catalog))
        self.assertTrue(all(item["vars"].get("language") in {"zh", "en"} for item in catalog))
        self.assertTrue(all(isinstance(item["vars"].get("requires_confirmation"), bool) for item in catalog))
        self.assertTrue(all(item["vars"].get("must_not_execute") is True for item in catalog))

        guided_counts = {}
        for item in catalog:
            variables = item["vars"]
            if variables.get("routing_mode") == "guided_policy":
                guided_counts[variables["skill"]] = guided_counts.get(variables["skill"], 0) + 1
        self.assertGreaterEqual(guided_counts.get("weex-analysis-skill", 0), 8)
        self.assertGreaterEqual(guided_counts.get("weex-monitor-skill", 0), 10)
        self.assertGreaterEqual(guided_counts.get("weex-partner-skill", 0), 17)
        self.assertGreaterEqual(guided_counts.get("weex-trader-skill", 0), 12)

        auto_router = [item for item in catalog if item["vars"].get("routing_mode") == "auto_router"]
        self.assertGreaterEqual(len(auto_router), 8)
        self.assertTrue(all("skill" not in item["vars"] for item in auto_router))
        for route in ("analysis", "monitor", "partner", "trader"):
            self.assertTrue(
                any(item["vars"].get("language") == "en" and route in item["vars"]["expected_route"].split("|") for item in catalog),
                f"missing English coverage for {route}",
            )

        partner_operations = {
            item["vars"].get("expected_operation")
            for item in catalog
            if item["vars"].get("skill") == "weex-partner-skill"
            and item["vars"].get("expected_operation") not in {None, "none"}
        }
        self.assertEqual(
            partner_operations,
            {
                "list-referral-uids",
                "get-direct-trade-asset",
                "get-commission",
                "get-sub-agent-stats",
                "verify-referrals",
                "get-referral-assets",
                "get-referral-deal-data",
            },
        )

    def test_codex_promptfoo_config_and_html_script_are_declared(self):
        config = EVALS / "promptfooconfig.codex.yaml"
        package = json.loads((EVALS / "package.json").read_text(encoding="utf-8"))
        config_text = config.read_text(encoding="utf-8")
        self.assertTrue(config.exists())
        self.assertIn("eval:codex:html", package["scripts"])
        self.assertIn("eval:codex:json", package["scripts"])
        self.assertIn("run_codex_promptfoo.cjs", package["scripts"]["eval:codex:html"])
        self.assertIn("artifacts/codex-model-eval.json", package["scripts"]["eval:codex:html"])
        self.assertIn("artifacts/codex-model-eval.html", package["scripts"]["eval:codex:json"])
        self.assertIn("{{ env.WEEX_CODEX_EVAL_MODEL }}", config_text)
        self.assertIn("{{ env.WEEX_CODEX_EVAL_MODEL_PROVIDER }}", config_text)
        self.assertIn("inherit_process_env: true", config_text)
        self.assertIn("ignore_default_excludes: false", config_text)
        self.assertIn('routing_mode == "auto_router"', config_text)
        self.assertIn("operation:", config_text)
        self.assertIn("without a script path or command prefix", config_text)
        self.assertRegex(
            config_text,
            r"required:\s*\n\s*- route\s*\n\s*- operation",
        )

    def test_codex_runtime_does_not_read_auth_file_or_literal_secret(self):
        provider = (EVALS / "scripts" / "codex_runtime.cjs").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("jp-", provider)
        self.assertNotIn("OPENAI_API_KEY=", provider)
        self.assertNotIn("auth.json", provider)
        self.assertNotIn("OPENAI_API_KEY", provider)

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

    def test_model_catalog_is_exact_and_has_safe_positive_coverage(self):
        catalog = json.loads(
            (EVALS / "cases" / "codex-model-tests.json").read_text(encoding="utf-8")
        )
        ids = [item["description"] for item in catalog]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(catalog), 50)
        self.assertTrue(
            any(item["vars"].get("scenario_type") == "positive_read_only" for item in catalog),
            "a successful read-only route must be distinguishable from a blocked mutation",
        )
