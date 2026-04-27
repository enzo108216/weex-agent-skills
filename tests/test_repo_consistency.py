#!/usr/bin/env python3
from __future__ import annotations

import ast
import io
import json
import re
import shutil
import subprocess
import tokenize
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
README = ROOT / "README.md"
MANIFEST = ROOT / "manifest.json"
FILE_INDEX = ROOT / "file-index.json"
AUTH_REFERENCE = ROOT / "references" / "auth-and-signing.md"
PROFILE_MANAGER_REFERENCE = ROOT / "references" / "profile-manager.md"
SCRIPT_OPERATIONS_REFERENCE = ROOT / "references" / "script-operations.md"
PROFILE_ONBOARDING_REFERENCE = ROOT / "references" / "profile-onboarding.md"
LINUX_VAULT_REFERENCE = ROOT / "references" / "linux-vault.md"
TROUBLESHOOTING_REFERENCE = ROOT / "references" / "troubleshooting.md"
REQUIREMENTS = ROOT / "requirements.txt"
DOC_FILES = (
    SKILL,
    README,
    MANIFEST,
    FILE_INDEX,
    PROFILE_MANAGER_REFERENCE,
    SCRIPT_OPERATIONS_REFERENCE,
    PROFILE_ONBOARDING_REFERENCE,
    LINUX_VAULT_REFERENCE,
    TROUBLESHOOTING_REFERENCE,
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
CJK_MARKDOWN_EXCLUDE_PREFIXES = (
    "docs/superpowers/specs/",
    "memory/",
    "plans/",
    "需求分析/",
    "需求资源/",
    "发版事项/",
)


def parse_requirement_names(text: str) -> set[str]:
    names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~]", line, maxsplit=1)[0].strip().lower()
        if name:
            names.add(name)
    return names


def extract_frontmatter_name(text: str) -> str:
    match = re.match(r"---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if not match:
        raise AssertionError("SKILL.md is missing YAML frontmatter")
    for line in match.group(1).splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("SKILL.md frontmatter is missing name")


def extract_repo_paths(text: str) -> set[str]:
    return set(re.findall(r"(?:scripts|references)/[A-Za-z0-9_./-]+", text))


def lines_with_trailing_backslash(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.rstrip().endswith("\\")]


def normalized_script_paths(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in (root / "scripts").iterdir()
        if path.is_file() and path.suffix in {".py", ".sh"}
    }


def extract_python_comments_and_docstrings(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    snippets: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            snippets.append(token.string)

    module = ast.parse(text)
    for node in [module, *ast.walk(module)]:
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                snippets.append(docstring)
    return snippets


def extract_shell_comments(path: Path) -> list[str]:
    comments: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            comments.append(stripped)
    return comments


class RepoConsistencyTests(unittest.TestCase):
    def test_docs_and_comments_do_not_contain_cjk_text(self) -> None:
        markdown_files = sorted(ROOT.rglob("*.md"))
        python_files = sorted((ROOT / "scripts").glob("*.py")) + sorted((ROOT / "tests").glob("*.py"))
        shell_files = sorted((ROOT / "scripts").glob("*.sh"))

        offenders: list[str] = []

        for path in markdown_files:
            rel_path = path.relative_to(ROOT).as_posix()
            if any(rel_path.startswith(prefix) for prefix in CJK_MARKDOWN_EXCLUDE_PREFIXES):
                continue
            text = path.read_text(encoding="utf-8")
            if CJK_RE.search(text):
                offenders.append(rel_path)

        for path in python_files:
            snippets = extract_python_comments_and_docstrings(path)
            if any(CJK_RE.search(snippet) for snippet in snippets):
                offenders.append(path.relative_to(ROOT).as_posix())

        for path in shell_files:
            snippets = extract_shell_comments(path)
            if any(CJK_RE.search(snippet) for snippet in snippets):
                offenders.append(path.relative_to(ROOT).as_posix())

        self.assertEqual(offenders, [])

    def test_non_zh_linux_wizard_entrypoints_do_not_contain_cjk_text(self) -> None:
        paths = (
            ROOT / "scripts" / "weex_linux_profile_wizard.sh",
            ROOT / "scripts" / "weex_linux_profile_wizard_en.sh",
        )

        offenders = [
            path.relative_to(ROOT).as_posix()
            for path in paths
            if CJK_RE.search(path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(offenders, [])

    def test_generator_dependencies_are_declared(self) -> None:
        requirements = parse_requirement_names(REQUIREMENTS.read_text(encoding="utf-8"))

        self.assertIn("cryptography", requirements)
        self.assertIn("requests", requirements)
        self.assertIn("beautifulsoup4", requirements)

    def test_split_references_exist(self) -> None:
        self.assertTrue(PROFILE_MANAGER_REFERENCE.exists())
        self.assertTrue(PROFILE_ONBOARDING_REFERENCE.exists())
        self.assertTrue(LINUX_VAULT_REFERENCE.exists())
        self.assertTrue(TROUBLESHOOTING_REFERENCE.exists())

    def test_readme_avoids_unpublished_hardcoded_install_source(self) -> None:
        readme_text = README.read_text(encoding="utf-8")

        self.assertNotIn(
            "Help me install this skill: https://github.com/drgnchan/weex-trader-skill",
            readme_text,
        )

    def test_readme_does_not_suggest_shipping_vault_or_profile_state(self) -> None:
        readme_text = README.read_text(encoding="utf-8")

        self.assertNotIn("includes the current vault/profile files", readme_text)

    def test_readme_does_not_imply_dedicated_spot_cancel_and_query_wrappers(self) -> None:
        readme_text = README.read_text(encoding="utf-8")

        self.assertNotIn("Place, cancel, and query orders", readme_text)

    def test_auth_reference_mentions_both_contract_and_spot_base_urls(self) -> None:
        auth_text = AUTH_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("https://api-contract.weex.com", auth_text)
        self.assertIn("https://api-spot.weex.com", auth_text)

    def test_auth_reference_describes_application_vault_consistently(self) -> None:
        auth_text = AUTH_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("Application Vault", auth_text)
        self.assertNotIn("Credential Manager", auth_text)
        self.assertNotIn("Keychain", auth_text)

    def test_skill_identity_matches_manifest(self) -> None:
        skill_name = extract_frontmatter_name(SKILL.read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(skill_name, manifest["identity"]["name"])
        self.assertEqual(manifest["identity"]["source_of_truth"], "SKILL.md")

    def test_skill_word_count_stays_compact(self) -> None:
        self.assertLessEqual(len(SKILL.read_text(encoding="utf-8").split()), 1400)

    def test_documented_repo_paths_exist(self) -> None:
        referenced_paths: set[str] = set()
        for path in DOC_FILES:
            referenced_paths.update(extract_repo_paths(path.read_text(encoding="utf-8")))

        self.assertTrue(referenced_paths, "expected at least one documented repo path")
        missing = sorted(path for path in referenced_paths if not (ROOT / path).exists())
        self.assertEqual(missing, [])

    def test_skill_links_to_split_setup_references(self) -> None:
        skill_text = SKILL.read_text(encoding="utf-8")
        readme_text = README.read_text(encoding="utf-8")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))

        self.assertIn("README.md", skill_text)
        self.assertIn("references/profile-manager.md", readme_text)
        self.assertIn("references/script-operations.md", readme_text)
        self.assertIn("references/profile-onboarding.md", skill_text)
        self.assertIn("references/script-operations.md", skill_text)
        self.assertIn("references/linux-vault.md", skill_text)
        self.assertIn("references/profile-onboarding.md", readme_text)
        self.assertIn("references/linux-vault.md", readme_text)
        self.assertEqual(
            manifest["read_order"]["open_if_needed"]["skill_overview"],
            "README.md",
        )
        self.assertEqual(
            manifest["read_order"]["open_if_needed"]["script_operations"],
            "references/script-operations.md",
        )
        self.assertEqual(
            manifest["read_order"]["open_if_needed"]["profile_manager"],
            "references/profile-manager.md",
        )
        self.assertEqual(
            manifest["read_order"]["open_if_needed"]["profile_onboarding"],
            "references/profile-onboarding.md",
        )
        self.assertEqual(
            manifest["read_order"]["open_if_needed"]["linux_vault"],
            "references/linux-vault.md",
        )
        self.assertEqual(
            manifest["read_order"]["open_if_needed"]["troubleshooting"],
            "references/troubleshooting.md",
        )
        self.assertIn("README.md", file_index["reference_guide"])
        self.assertIn("references/script-operations.md", file_index["reference_guide"])
        self.assertIn("references/profile-manager.md", file_index["reference_guide"])
        self.assertIn("references/profile-onboarding.md", file_index["reference_guide"])
        self.assertIn("references/linux-vault.md", file_index["reference_guide"])
        self.assertIn("references/troubleshooting.md", file_index["reference_guide"])

    def test_readme_routes_account_manager_details_to_dedicated_reference(self) -> None:
        readme_text = README.read_text(encoding="utf-8")

        self.assertIn("Profile manager guide", readme_text)
        self.assertNotIn("## Saved Profile Setup\n\nPrivate account and trading operations require a saved profile.\n\nCollect the full profile parameter set", readme_text)

    def test_readme_routes_linux_vault_details_to_dedicated_reference(self) -> None:
        readme_text = README.read_text(encoding="utf-8")

        self.assertIn("Linux vault modes, password handling, and lock/unlock flows", readme_text)
        self.assertNotIn("## Linux Vault Guidance", readme_text)
        self.assertNotIn("Complete vault setup parameter list", readme_text)
        self.assertNotIn("High-level guidance:", readme_text)

    def test_readme_keeps_api_credential_onboarding(self) -> None:
        readme_text = README.read_text(encoding="utf-8")

        self.assertIn("## Get API Credentials", readme_text)
        self.assertIn("API Management", readme_text)
        self.assertIn("Secret Key", readme_text)
        self.assertIn("Passphrase", readme_text)

    def test_script_operation_and_setup_references_define_command_context(self) -> None:
        expected = "Run the shell commands below from the skill root"

        self.assertIn(expected, SCRIPT_OPERATIONS_REFERENCE.read_text(encoding="utf-8"))
        self.assertIn(expected, PROFILE_ONBOARDING_REFERENCE.read_text(encoding="utf-8"))
        self.assertIn(expected, LINUX_VAULT_REFERENCE.read_text(encoding="utf-8"))

    def test_setup_docs_avoid_shell_specific_line_continuations(self) -> None:
        offenders: list[str] = []
        for path in (SCRIPT_OPERATIONS_REFERENCE, PROFILE_ONBOARDING_REFERENCE, LINUX_VAULT_REFERENCE):
            lines = lines_with_trailing_backslash(path.read_text(encoding="utf-8"))
            if lines:
                offenders.append(path.relative_to(ROOT).as_posix())

        self.assertEqual(offenders, [])

    def test_readme_keeps_natural_language_examples_and_module_summary(self) -> None:
        readme_text = README.read_text(encoding="utf-8")

        self.assertIn("## How to Use This Skill in Codex / Openclaw / Claude Code", readme_text)
        self.assertIn("Natural-language example", readme_text)
        self.assertIn("## Module quick-reference", readme_text)
        self.assertIn("Spot", readme_text)
        self.assertIn("Futures", readme_text)
        self.assertLess(readme_text.index("## How to Use This Skill in Codex / Openclaw / Claude Code"), readme_text.index("## Module quick-reference"))
        self.assertLess(readme_text.index("## Module quick-reference"), readme_text.index("## Saved Profile Setup"))
        self.assertNotIn("## Python Prerequisites", readme_text)
        self.assertNotIn("## Command Context", readme_text)
        self.assertNotIn("## Quick Start", readme_text)
        self.assertNotIn("## Trading Commands", readme_text)

    def test_readme_routes_technical_script_usage_to_dedicated_reference(self) -> None:
        readme_text = README.read_text(encoding="utf-8")
        script_ops_text = SCRIPT_OPERATIONS_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("references/script-operations.md", readme_text)
        self.assertIn("## Python Prerequisites", script_ops_text)
        self.assertIn("## Command Context", script_ops_text)
        self.assertIn("## Quick Start", script_ops_text)
        self.assertIn("## Trading Commands", script_ops_text)
        self.assertIn("## Regenerate Definitions", script_ops_text)

    def test_script_operations_documents_runtime_setup_helper(self) -> None:
        script_ops_text = SCRIPT_OPERATIONS_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("scripts/weex_runtime_setup.py", script_ops_text)
        self.assertIn("ensurepip", script_ops_text)
        self.assertIn("current interpreter", script_ops_text)

    def test_readme_and_indexes_link_troubleshooting_reference(self) -> None:
        readme_text = README.read_text(encoding="utf-8")
        skill_text = SKILL.read_text(encoding="utf-8")
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))

        self.assertIn("references/troubleshooting.md", readme_text)
        self.assertIn("references/troubleshooting.md", skill_text)
        self.assertIn("references/troubleshooting.md", file_index["reference_guide"])

    def test_skill_requires_manual_once_linux_vault_mode(self) -> None:
        skill_text = SKILL.read_text(encoding="utf-8").lower()

        self.assertIn("manual_once", skill_text)
        self.assertNotIn("auto_unlock", skill_text)

    def test_docs_require_user_provided_vault_password(self) -> None:
        combined = "\n".join(path.read_text(encoding="utf-8") for path in (SKILL, README))

        self.assertIn("Never generate a vault passphrase", combined)
        self.assertIn("must be explicitly chosen and provided by the user", combined)

    def test_docs_allow_ai_to_unlock_when_user_provides_secret(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL, README, LINUX_VAULT_REFERENCE)
        )

        self.assertIn("AI may autonomously execute vault commands", combined)
        self.assertIn("unlock", combined)
        self.assertIn("setup", combined)
        self.assertIn("change-password", combined)
        self.assertIn("after the user provides the secret", combined)

    def test_docs_require_second_confirmation_only_for_new_vault_secret(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL, README, LINUX_VAULT_REFERENCE)
        )

        self.assertIn("must clearly designate which value should be used as the vault password", combined)
        self.assertIn("Before any `setup` or `change-password` action that sets a new vault password", combined)
        self.assertIn("`unlock` only needs one passphrase entry", combined)

    def test_docs_describe_gui_vault_as_global_control_area(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL, README, PROFILE_ONBOARDING_REFERENCE)
        )

        self.assertIn("shared application vault", combined)
        self.assertIn("global vault control area", combined)
        self.assertIn("separate from per-profile credential fields", combined)

    def test_docs_route_windows_macos_vault_unlock_to_ui(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL, README, PROFILE_ONBOARDING_REFERENCE)
        )

        self.assertIn("Windows/macOS vault setup or unlock", combined)
        self.assertIn("launch the vault UI", combined)
        self.assertIn("AI should use the UI", combined)
        self.assertIn("must not silently decide, infer, generate, or substitute the vault password", combined)

    def test_script_operations_documents_vault_requested_action_values(self) -> None:
        script_ops_text = SCRIPT_OPERATIONS_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("Vault `--requested-action` values", script_ops_text)
        self.assertIn("`setup`", script_ops_text)
        self.assertIn("`unlock`", script_ops_text)
        self.assertIn("`status`", script_ops_text)
        self.assertIn("`lock`", script_ops_text)

    def test_script_operations_documents_windows_macos_vault_routing_rules(self) -> None:
        script_ops_text = SCRIPT_OPERATIONS_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("bare `setup` and bare `unlock` also open the vault UI by default", script_ops_text)
        self.assertIn("`status`, `lock`, `mode`, `change-password`", script_ops_text)
        self.assertIn("stay in the terminal", script_ops_text)
        self.assertIn("use `--cli`", script_ops_text)

    def test_profile_onboarding_explains_profile_env_var_examples_are_setup_inputs_only(self) -> None:
        onboarding_text = PROFILE_ONBOARDING_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("Private trading commands do not read those env vars directly at runtime.", onboarding_text)
        self.assertIn("Save credentials into a profile first", onboarding_text)
        self.assertIn("example variable names only", onboarding_text)
        self.assertIn("PROFILE_API_KEY", onboarding_text)
        self.assertIn("PROFILE_API_SECRET", onboarding_text)
        self.assertIn("PROFILE_API_PASSPHRASE", onboarding_text)

    def test_docs_explain_profile_request_parameters_comprehensively(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL, README, PROFILE_ONBOARDING_REFERENCE)
        )

        self.assertIn("Complete profile parameter list", combined)
        self.assertIn("Do not frame this as only the minimum fields needed to make private endpoints work", combined)
        self.assertIn("profile name", combined)
        self.assertIn("how later commands refer to the saved account", combined)
        self.assertIn("WEEX API Key", combined)
        self.assertIn("WEEX Secret Key", combined)
        self.assertIn("WEEX API Passphrase", combined)
        self.assertIn("description / note", combined)
        self.assertIn("contract_base_url", combined)
        self.assertIn("spot_base_url", combined)
        self.assertIn("official contract REST host", combined)
        self.assertIn("official spot REST host", combined)

    def test_docs_explain_linux_vault_parameters_comprehensively(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL, README, LINUX_VAULT_REFERENCE)
        )

        self.assertIn("Complete vault setup parameter list", combined)
        self.assertIn("Do not introduce vault setup as only the minimum combination needed to run setup", combined)
        self.assertIn("vault mode", combined)
        self.assertIn("vault password / passphrase", combined)
        self.assertIn("--password-file", combined)
        self.assertIn("--password-env", combined)
        self.assertIn("unlock immediately after setup", combined)

    def test_docs_explain_linux_vault_mode_risks_and_recommendations(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (SKILL, README, LINUX_VAULT_REFERENCE)
        )

        self.assertIn("security trade-offs", combined)
        self.assertIn("safer default for interactive/manual use", combined)
        self.assertIn("Recommend `manual_once`", combined)
        self.assertNotIn("Recommend `auto_unlock` only", combined)

    def test_docs_forbid_autonomous_lock_without_explicit_user_request(self) -> None:
        combined = "\n".join(path.read_text(encoding="utf-8") for path in (SKILL, README, LINUX_VAULT_REFERENCE))

        self.assertIn("Unless the user explicitly asks for `lock`", combined)
        self.assertIn("do not autonomously execute `weex_vault ... lock`", combined)

    def test_linux_vault_docs_cover_password_file_non_interactive_flow(self) -> None:
        vault_text = LINUX_VAULT_REFERENCE.read_text(encoding="utf-8")
        skill_text = SKILL.read_text(encoding="utf-8")

        self.assertIn("--password-file", vault_text)
        self.assertIn("Do not put vault passwords directly on argv", skill_text)

    def test_linux_vault_docs_and_index_cover_password_rotation(self) -> None:
        vault_text = LINUX_VAULT_REFERENCE.read_text(encoding="utf-8")
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))

        self.assertIn("change-password", vault_text)
        self.assertNotIn("mode --set-mode", vault_text)
        self.assertIn("change-password", file_index["file_guide"]["scripts/weex_vault_cli.py"]["surface"])

    def test_machine_readable_metadata_describes_application_vault_consistently(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))

        self.assertEqual(manifest["state"]["secure_store_backends"], ["Application Vault"])
        self.assertIn(
            "application-vault backend",
            file_index["file_guide"]["scripts/weex_profile_store.py"]["role"],
        )
        self.assertNotIn(
            "OS-keychain",
            file_index["file_guide"]["scripts/weex_profile_store.py"]["role"],
        )

    def test_docs_and_indexes_describe_agent_state_cache(self) -> None:
        combined = "\n".join(path.read_text(encoding="utf-8") for path in (SKILL, README))
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))

        self.assertIn("agent-init.json", combined)
        self.assertIn("agent-runtime.json", combined)
        self.assertIn("scripts/weex_agent_state.py", combined)
        self.assertIn("agent_state_paths", manifest["state"])
        self.assertEqual(
            manifest["state"]["agent_state_paths"]["init"],
            "~/.weex-trader-skill/agent-init.json",
        )
        self.assertEqual(
            manifest["state"]["agent_state_paths"]["runtime"],
            "~/.weex-trader-skill/agent-runtime.json",
        )
        self.assertIn("scripts/weex_agent_state.py", file_index["file_guide"])

    def test_skill_requires_ai_to_preflight_agent_state_on_every_turn(self) -> None:
        skill_text = SKILL.read_text(encoding="utf-8")

        self.assertIn("For every turn that uses this skill", skill_text)
        self.assertIn("before routing or UI launch", skill_text)
        self.assertIn("scripts/weex_agent_state.py --command skill.preflight --language <zh|en> --pretty", skill_text)

    def test_manifest_and_docs_do_not_publish_weex_profile_lang_override(self) -> None:
        combined = "\n".join(path.read_text(encoding="utf-8") for path in (SKILL, README))
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

        self.assertNotIn("WEEX_PROFILE_LANG", combined)
        self.assertNotIn("WEEX_PROFILE_LANG", manifest["state"]["env_vars"])

    def test_file_index_describes_cross_platform_vault_routing_consistently(self) -> None:
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))

        self.assertNotIn("headless/server Linux", file_index["file_families"]["linux_vault"]["role"])
        self.assertNotIn("keychain", " ".join(file_index["file_guide"]["scripts/weex_profile_store.py"]["when"]).lower())
        self.assertIn(
            "Windows/macOS vault setup or unlock",
            file_index["file_guide"]["scripts/weex_vault_cli.py"]["when"],
        )
        self.assertIn(
            "Linux vault credential setup",
            file_index["file_guide"]["scripts/weex_vault_cli.py"]["when"],
        )

    def test_file_index_covers_vault_ui_and_session_agent(self) -> None:
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))

        self.assertIn("scripts/weex_vault_manager_app.py", file_index["file_guide"])
        self.assertIn("scripts/weex_vault_agent.py", file_index["file_guide"])

    def test_file_index_ignore_by_default_covers_generated_noise(self) -> None:
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))

        self.assertIn(".pytest_cache/", file_index["ignore_by_default"])

    def test_file_index_covers_all_script_entrypoints(self) -> None:
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))
        script_paths = normalized_script_paths(ROOT)
        missing = sorted(script_paths - set(file_index["file_guide"]))

        self.assertEqual(missing, [])

    def test_compact_spot_reference_uses_valid_example_endpoint_keys(self) -> None:
        spot_reference_text = (ROOT / "references" / "spot-endpoints.md").read_text(encoding="utf-8")
        spot_definitions = json.loads((ROOT / "references" / "spot-api-definitions.json").read_text(encoding="utf-8"))
        valid_keys = {definition["key"] for definition in spot_definitions["definitions"]}

        example_keys = re.findall(r"--endpoint\s+([A-Za-z0-9_.-]+)", spot_reference_text)
        invalid = sorted(key for key in example_keys if key not in valid_keys)

        self.assertEqual(invalid, [])
        self.assertIn("spot.market.get_ticker_info", spot_reference_text)

    def test_skill_does_not_route_release_packaging_notes(self) -> None:
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))
        updates_text = (ROOT / "references" / "updates.md").read_text(encoding="utf-8")

        self.assertNotIn("references/updates.md", file_index["reference_guide"])
        self.assertNotIn("metadata.version", updates_text)
        self.assertNotIn("/Users/raymond/", updates_text)
        self.assertNotIn("fs-skill-creator", updates_text)

    def test_skill_ships_root_readme(self) -> None:
        self.assertTrue(README.exists())

    def test_long_references_include_contents_heading(self) -> None:
        long_references = [
            README,
            PROFILE_MANAGER_REFERENCE,
            PROFILE_ONBOARDING_REFERENCE,
            LINUX_VAULT_REFERENCE,
            TROUBLESHOOTING_REFERENCE,
            ROOT / "references" / "spot-api-definitions.md",
            ROOT / "references" / "contract-api-definitions.md",
        ]

        offenders: list[str] = []
        for path in long_references:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) <= 100:
                continue
            preview = "\n".join(lines[:25])
            if "## Contents" not in preview and "## Table of Contents" not in preview:
                offenders.append(path.relative_to(ROOT).as_posix())

        self.assertEqual(offenders, [])

    def test_linux_vault_notes_do_not_require_xdg_runtime_dir(self) -> None:
        vault_text = LINUX_VAULT_REFERENCE.read_text(encoding="utf-8")

        self.assertIn("local WEEX config directory", vault_text)
        self.assertNotIn("requires a session runtime directory", vault_text)
        self.assertNotIn("XDG_RUNTIME_DIR", vault_text)
        self.assertNotIn("/run/user/$UID", vault_text)

    def test_readme_avoids_agent_policy_language(self) -> None:
        readme_text = README.read_text(encoding="utf-8")

        self.assertNotIn("ask the user", readme_text)
        self.assertNotIn("the user's behalf", readme_text)
        self.assertNotIn("If the user is entering", readme_text)
        self.assertNotIn("do not silently decide", readme_text)
        self.assertNotIn("explicitly authorizes continuation", readme_text)

    def test_readme_warns_about_ai_chat_secret_exposure(self) -> None:
        readme_text = README.read_text(encoding="utf-8")

        self.assertIn("Openclaw", readme_text)
        self.assertIn("Telegram", readme_text)
        self.assertIn("server-side chat logs", readme_text)
        self.assertIn("AI-assisted handling of API keys, API secrets, passphrases, and vault passwords", readme_text)
        self.assertIn("If you choose to paste secrets into AI chat or let the AI operate on them directly", readme_text)

    def test_readme_warns_about_local_secret_transports_and_live_trading(self) -> None:
        readme_text = README.read_text(encoding="utf-8")

        self.assertIn("shell history", readme_text)
        self.assertIn("process list", readme_text)
        self.assertIn("environment variables", readme_text)
        self.assertIn("temporary password files", readme_text)
        self.assertIn("profiles.meta.json", readme_text)
        self.assertIn("real account", readme_text)
        self.assertIn("--confirm-live", readme_text)

    def test_profile_store_no_longer_keeps_platform_specific_secret_backends(self) -> None:
        profile_store_text = (ROOT / "scripts" / "weex_profile_store.py").read_text(encoding="utf-8")

        self.assertNotIn("def _store_secret_windows(", profile_store_text)
        self.assertNotIn("def _load_secret_windows(", profile_store_text)
        self.assertNotIn("def _delete_secret_windows(", profile_store_text)
        self.assertNotIn("def _store_secret_macos(", profile_store_text)
        self.assertNotIn("def _load_secret_macos(", profile_store_text)
        self.assertNotIn("def _delete_secret_macos(", profile_store_text)
        self.assertNotIn("Windows Credential Manager", profile_store_text)
        self.assertNotIn("macOS Keychain", profile_store_text)

    def test_runtime_scripts_do_not_keep_auto_unlock_residue(self) -> None:
        script_text = "\n".join(
            (ROOT / "scripts" / relative_path).read_text(encoding="utf-8")
            for relative_path in (
                "weex_agent_state.py",
                "weex_profile_manager_app.py",
                "weex_profile_store.py",
                "weex_vault_manager_app.py",
            )
        )

        self.assertNotIn("auto_unlock_env_var_name", script_text)
        self.assertNotIn("auto unlock env missing", script_text)
        self.assertNotIn("auto-unlock configuration", script_text)

    def test_ui_scripts_do_not_keep_legacy_unused_blocks(self) -> None:
        profile_manager_text = (ROOT / "scripts" / "weex_profile_manager_app.py").read_text(encoding="utf-8")
        vault_cli_text = (ROOT / "scripts" / "weex_vault_cli.py").read_text(encoding="utf-8")

        self.assertNotIn("_legacy_build_hero_unused", profile_manager_text)
        self.assertNotIn("_legacy_build_workspace_overview_unused", profile_manager_text)
        self.assertNotIn("_legacy_build_vault_workspace_unused", profile_manager_text)
        self.assertNotIn("_LEGACY_TEXTS = {", vault_cli_text)

    def test_agent_state_script_is_tracked_when_git_metadata_is_present(self) -> None:
        if not (ROOT / ".git").exists():
            self.skipTest("git metadata not present")
        git = shutil.which("git")
        if git is None:
            self.skipTest("git executable not available")

        completed = subprocess.run(
            [git, "ls-files", "--error-unmatch", "scripts/weex_agent_state.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
