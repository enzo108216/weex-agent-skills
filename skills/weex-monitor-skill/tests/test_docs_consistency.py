#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
MANIFEST = ROOT / "manifest.json"
FILE_INDEX = ROOT / "file-index.json"
SCRIPT = ROOT / "scripts" / "weex_monitor_cli.py"


def extract_frontmatter_field(text: str, field_name: str) -> str:
    match = re.match(r"---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if not match:
        raise AssertionError("SKILL.md is missing YAML frontmatter")
    prefix = f"{field_name}:"
    for line in match.group(1).splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"SKILL.md frontmatter is missing {field_name}")


class MonitorDocsConsistencyTests(unittest.TestCase):
    def test_skill_frontmatter_and_manifest_identity_match(self) -> None:
        skill_text = SKILL.read_text(encoding="utf-8")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(extract_frontmatter_field(skill_text, "name"), "weex-monitor-skill")
        self.assertEqual(manifest["identity"]["name"], "weex-monitor-skill")
        self.assertIn("automated monitor", extract_frontmatter_field(skill_text, "description").lower())

    def test_skill_declares_trader_skill_as_live_execution_boundary(self) -> None:
        skill_text = SKILL.read_text(encoding="utf-8")

        self.assertIn("weex-trader-skill", skill_text)
        self.assertIn("--confirm-live", skill_text)
        self.assertIn("Never send mutating requests", skill_text)
        self.assertIn("does not own API credentials", skill_text)

    def test_skill_documents_ai_natural_language_to_dsl_rules(self) -> None:
        skill_text = SKILL.read_text(encoding="utf-8")

        self.assertIn("AI natural-language parsing", skill_text)
        self.assertIn("convert the user's monitor instruction into the Task DSL", skill_text)
        self.assertIn("ask for the missing field", skill_text)
        self.assertIn("profile is always required", skill_text)
        self.assertIn("symbol_price_monitor requires action.quantity", skill_text)
        self.assertIn("dry-run commands still write local SQLite task state and events", skill_text)
        self.assertIn("不要下单", skill_text)
        self.assertIn("BTCUSDT 多单未实现盈利大于 50", skill_text)

    def test_file_index_covers_script_and_script_avoids_trader_imports(self) -> None:
        file_index = json.loads(FILE_INDEX.read_text(encoding="utf-8"))
        script_text = SCRIPT.read_text(encoding="utf-8")
        tree = ast.parse(script_text)

        self.assertIn("scripts/weex_monitor_cli.py", file_index["file_guide"])
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        from_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        all_imports = imports | from_imports
        self.assertNotIn("weex_contract_api", all_imports)
        self.assertNotIn("weex_trade_guard", all_imports)
        self.assertNotIn("weex_profile_store", all_imports)


if __name__ == "__main__":
    unittest.main()
