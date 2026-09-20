"""Tests for Obsidian memory integration robustness (CRLF and YAML variations)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cyber_health.health_memory import _project_declared
from cyber_health.obsidian_memory_provider import (
    ObsidianMemoryProvider,
    _project_is_declared,
)


class TestObsidianHardening(unittest.TestCase):
    def test_frontmatter_crlf_parsing(self) -> None:
        # Note with CRLF line endings
        crlf_text = "---\r\ntopic: sleep\r\nsensitivity: internal\r\nstatus: active\r\n---\r\n\r\n# Sleep Guidelines\r\nBody text"
        fields, body = ObsidianMemoryProvider._parse_frontmatter(crlf_text)
        self.assertEqual(fields.get("topic"), "sleep")
        self.assertEqual(fields.get("sensitivity"), "internal")
        self.assertEqual(fields.get("status"), "active")
        self.assertIn("# Sleep Guidelines", body)

    def test_frontmatter_standard_lf_parsing(self) -> None:
        # Note with standard LF line endings
        lf_text = "---\ntopic: nutrition\nsensitivity: private\n---\n# Nutrition Facts"
        fields, body = ObsidianMemoryProvider._parse_frontmatter(lf_text)
        self.assertEqual(fields.get("topic"), "nutrition")
        self.assertEqual(fields.get("sensitivity"), "private")
        self.assertIn("# Nutrition Facts", body)

    def test_project_declared_variations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault = Path(tmp_dir)
            system_dir = vault / "00-System"
            system_dir.mkdir(parents=True)
            projects_file = system_dir / "projects.yaml"

            # 1. Plain unquoted ID
            projects_file.write_text("- id: health-manager\n  path: 20-Projects/health-manager\n", encoding="utf-8")
            self.assertTrue(_project_is_declared(vault, "health-manager"))
            self.assertTrue(_project_declared(vault, "health-manager"))

            # 2. Double-quoted ID
            projects_file.write_text('- id: "health-manager"\n  path: 20-Projects/health-manager\n', encoding="utf-8")
            self.assertTrue(_project_is_declared(vault, "health-manager"))
            self.assertTrue(_project_declared(vault, "health-manager"))

            # 3. Single-quoted ID with trailing comment
            projects_file.write_text("- id: 'health-manager' # private local project\n", encoding="utf-8")
            self.assertTrue(_project_is_declared(vault, "health-manager"))
            self.assertTrue(_project_declared(vault, "health-manager"))

            # 4. Dict mapping with 'projects' key
            projects_file.write_text("projects:\n  - id: health-manager\n    name: Health\n", encoding="utf-8")
            self.assertTrue(_project_is_declared(vault, "health-manager"))
            self.assertTrue(_project_declared(vault, "health-manager"))

            # 5. Non-matching ID
            self.assertFalse(_project_is_declared(vault, "other-project"))
            self.assertFalse(_project_declared(vault, "other-project"))


if __name__ == "__main__":
    unittest.main()
