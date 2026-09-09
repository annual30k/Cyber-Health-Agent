"""Deterministic unit tests for unified cyber-health CLI."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cyber_health.cli import main, run_status


class TestCyberHealthCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="cyber-health-cli-test-")
        self.test_dir = Path(self.temp_dir.name).resolve()
        self.target_dir = self.test_dir / ".cyber-health"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_run_status_uninstalled(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = run_status(str(self.target_dir), as_json=True)
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertFalse(data["installed"])
        self.assertEqual(data["version"], "not installed")

    def test_run_status_installed(self) -> None:
        self.target_dir.mkdir()
        cfg = self.target_dir / "config"
        cfg.mkdir()
        (cfg / "installation.json").write_text(
            json.dumps({"version": "0.2.4", "installed_at": "2026-09-08T12:00:00Z"})
        )
        data_dir = self.target_dir / "data"
        data_dir.mkdir()
        (data_dir / "cyber-health.sqlite3").write_bytes(b"")

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = run_status(str(self.target_dir), as_json=True)
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["installed"])
        self.assertEqual(data["version"], "0.2.4")

    def test_run_status_does_not_claim_foreign_openclaw_registration(self) -> None:
        buf = io.StringIO()
        result = mock.MagicMock(returncode=0, stdout=json.dumps({
            "command": "/usr/local/bin/foreign-mcp",
            "args": [],
            "cwd": "/opt/foreign",
        }), stderr="")
        with (
            mock.patch("cyber_health.cli.shutil.which", return_value="/usr/bin/openclaw"),
            mock.patch("cyber_health.cli.subprocess.run", return_value=result),
            mock.patch("sys.stdout", buf),
        ):
            code = run_status(str(self.target_dir), as_json=True)
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertFalse(data["openclaw_registered"])
        self.assertFalse(data["openclaw_details"]["ownership_verified"])

    def test_cli_help(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage: cyber-health", buf.getvalue())

    def test_cli_dispatch_status(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["status", "--target-dir", str(self.target_dir), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertFalse(data["installed"])

    def test_cli_dispatch_mcp_forwarding(self) -> None:
        """mcp subcommand must forward flags without prepending 'mcp'."""
        with mock.patch("cyber_health_mcp.server.main") as mock_mcp_main:
            mock_mcp_main.return_value = 0
            code = main(["mcp", "--db", "/tmp/custom.sqlite3", "--allow-all"])
            self.assertEqual(code, 0)
            mock_mcp_main.assert_called_once_with(["--db", "/tmp/custom.sqlite3", "--allow-all"])

    def test_mcp_server_main_with_explicit_argv(self) -> None:
        """server.main parses forwarded argv without touching sys.argv."""
        from cyber_health_mcp.server import main as server_main
        with mock.patch("cyber_health_mcp.server.create_mcp_server") as mock_create:
            mock_server_instance = mock.MagicMock()
            mock_create.return_value = mock_server_instance
            server_main(["--db", "/tmp/test_mcp.sqlite3", "--allow-all"])
            mock_create.assert_called_once_with(
                database_path=Path("/tmp/test_mcp.sqlite3"),
                allow_all_tools=True,
            )
            mock_server_instance.run.assert_called_once_with(transport="stdio")

    def test_get_default_db_path_priorities(self) -> None:
        """Tests priority: CYBER_HEALTH_DB > installation.json > ~/.cyber-health data > ./data."""
        from cyber_health_mcp.server import get_default_db_path

        # 1. Environment variable takes highest priority
        with mock.patch.dict(os.environ, {"CYBER_HEALTH_DB": "/custom/env_db.sqlite3"}):
            self.assertEqual(get_default_db_path(), Path("/custom/env_db.sqlite3"))

        # 2. installation.json metadata priority
        with mock.patch.dict(os.environ, {}, clear=True):
            fake_home = self.test_dir / "fake_home"
            fake_installed = fake_home / ".cyber-health"
            fake_cfg = fake_installed / "config"
            fake_cfg.mkdir(parents=True, exist_ok=True)
            custom_db = fake_installed / "data" / "meta_db.sqlite3"
            custom_db.parent.mkdir(parents=True, exist_ok=True)
            (fake_cfg / "installation.json").write_text(
                json.dumps({"db_path": str(custom_db)}), encoding="utf-8"
            )

            with mock.patch("pathlib.Path.home", return_value=fake_home):
                # A fresh installation may have metadata before the production
                # database is created; it must still resolve to the production path.
                self.assertEqual(get_default_db_path(), custom_db)

        # 3. Fallback to ~/.cyber-health/data/cyber-health.sqlite3 if it exists
        with mock.patch.dict(os.environ, {}, clear=True):
            fake_home2 = self.test_dir / "fake_home2"
            std_db = fake_home2 / ".cyber-health" / "data" / "cyber-health.sqlite3"
            std_db.parent.mkdir(parents=True, exist_ok=True)
            std_db.touch()

            with mock.patch("pathlib.Path.home", return_value=fake_home2):
                self.assertEqual(get_default_db_path(), std_db)

        # 4. Fallback to cwd ./data/cyber-health.sqlite3
        with mock.patch.dict(os.environ, {}, clear=True):
            empty_home = self.test_dir / "empty_home"
            with mock.patch("pathlib.Path.home", return_value=empty_home):
                self.assertEqual(get_default_db_path(), Path.cwd() / "data" / "cyber-health.sqlite3")


if __name__ == "__main__":
    unittest.main()
