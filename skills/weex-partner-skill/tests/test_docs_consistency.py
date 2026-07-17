#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
MANIFEST = ROOT / "manifest.json"
FILE_INDEX = ROOT / "file-index.json"
QUERY_POLICY = ROOT / "references" / "partner-query-policy.md"
OUTPUT_SCHEMA = ROOT / "references" / "partner-output-schema.md"
CLI = ROOT / "scripts" / "weex_partner_cli.py"
TESTS = ROOT / "tests"

EXPECTED_COMMANDS = {
    "list-referral-uids",
    "get-direct-trade-asset",
    "get-commission",
    "get-internal-withdrawals",
    "get-sub-agent-stats",
    "verify-referrals",
    "get-referral-assets",
    "get-referral-deal-data",
}


class PartnerDocsConsistencyTests(unittest.TestCase):
    def test_required_skill_files_exist(self) -> None:
        for path in (SKILL, MANIFEST, FILE_INDEX, QUERY_POLICY, OUTPUT_SCHEMA, CLI):
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(path.exists(), f"missing required Partner skill file: {path}")

    def test_skill_frontmatter_and_manifest_identity_match(self) -> None:
        self.assertTrue(SKILL.exists(), "Partner SKILL.md is not implemented yet; expected T1 RED.")
        self.assertTrue(MANIFEST.exists(), "Partner manifest is not implemented yet; expected T1 RED.")
        skill_text = SKILL.read_text(encoding="utf-8")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        name_match = re.search(r"^name:\s*(\S+)\s*$", skill_text, flags=re.MULTILINE)
        description_match = re.search(r"^description:\s*(.+)\s*$", skill_text, flags=re.MULTILINE)

        self.assertIsNotNone(name_match)
        self.assertEqual(name_match.group(1), "weex-partner-skill")
        self.assertIsNotNone(description_match)
        self.assertTrue(description_match.group(1).startswith("Use when "))
        self.assertEqual(manifest["identity"]["name"], "weex-partner-skill")

    def test_skill_routes_rest_profile_and_vault_to_trader(self) -> None:
        self.assertTrue(SKILL.exists(), "Partner SKILL.md is not implemented yet; expected T1 RED.")
        text = SKILL.read_text(encoding="utf-8")

        self.assertIn("weex-trader-skill", text)
        self.assertIn("profile", text.lower())
        self.assertIn("Vault", text)
        self.assertIn("scripts/weex_partner_cli.py", text)
        self.assertIn("read-only", text.lower())
        self.assertNotIn("Partner account type", text)

    def test_skill_routes_normal_orders_to_existing_trader_confirmation_gate(self) -> None:
        self.assertTrue(SKILL.exists(), "Partner SKILL.md is not implemented yet; expected T1 RED.")
        text = SKILL.read_text(encoding="utf-8").lower()

        self.assertIn("order", text)
        self.assertIn("weex-trader-skill", text)
        self.assertIn("confirm", text)
        self.assertIn("--confirm-live", text)

    def test_query_policy_documents_all_commands_and_minimum_time_rule(self) -> None:
        self.assertTrue(
            QUERY_POLICY.exists(),
            "Partner query policy is not implemented yet; expected T1 RED.",
        )
        text = QUERY_POLICY.read_text(encoding="utf-8")

        for command in EXPECTED_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, text)
        self.assertIn("official minimum", text.lower())
        self.assertIn("30", text)
        self.assertIn("90", text)
        self.assertIn("365", text)
        self.assertIn("UTC", text)

    def test_output_schema_requires_partial_and_pagination_disclosure(self) -> None:
        self.assertTrue(
            OUTPUT_SCHEMA.exists(),
            "Partner output schema is not implemented yet; expected T1 RED.",
        )
        text = OUTPUT_SCHEMA.read_text(encoding="utf-8")

        for field in (
            "complete",
            "partial",
            "has_more",
            "remaining_count",
            "next_page",
            "continuation",
            "actual_start",
            "actual_end",
        ):
            with self.subTest(field=field):
                self.assertIn(field, text)

    def test_file_index_accounts_for_every_portable_skill_file(self) -> None:
        self.assertTrue(FILE_INDEX.exists(), "Partner file index is not implemented yet; expected T1 RED.")
        index_text = FILE_INDEX.read_text(encoding="utf-8")
        indexed_paths = set(re.findall(r"(?:scripts|references|tests)/[A-Za-z0-9_./-]+", index_text))
        actual_paths = {
            path.relative_to(ROOT).as_posix()
            for family in (ROOT / "scripts", ROOT / "references", TESTS)
            if family.exists()
            for path in family.iterdir()
            if path.is_file() and path.name != "__init__.py"
        }
        self.assertEqual(indexed_paths, actual_paths)


if __name__ == "__main__":
    unittest.main()
