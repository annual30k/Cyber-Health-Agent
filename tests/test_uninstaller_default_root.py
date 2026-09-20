"""Tests for CyberHealthUninstaller default project_root resolution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cyber_health.uninstall import CyberHealthUninstaller


class TestUninstallerDefaultRoot(unittest.TestCase):
    def test_explicit_project_root_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_dir = (Path(tmp_dir) / "custom_install").resolve()
            project_dir.mkdir(parents=True)
            uninstaller = CyberHealthUninstaller(project_root=project_dir)
            self.assertEqual(uninstaller.project_root, project_dir)
            self.assertEqual(uninstaller.db_path, project_dir / "data" / "cyber-health.sqlite3")

    def test_default_project_root_resolves_when_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resolved_tmp = Path(tmp_dir).resolve()
            fake_install = resolved_tmp / ".cyber-health"
            fake_config = fake_install / "config"
            fake_config.mkdir(parents=True)
            (fake_config / "installation.json").write_text("{}", encoding="utf-8")

            with patch("pathlib.Path.home", return_value=resolved_tmp):
                uninstaller = CyberHealthUninstaller(project_root=None)
                self.assertEqual(uninstaller.project_root, fake_install)
                self.assertEqual(uninstaller.db_path, fake_install / "data" / "cyber-health.sqlite3")


if __name__ == "__main__":
    unittest.main()
