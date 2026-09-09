"""Deterministic unit and integration tests for Cyber Health Agent installer.

Tests isolated installation, database migration, OpenClaw auto-registration,
dry-run behavior, and safety boundary enforcement.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from cyber_health.install import (
    FIXED_OPENCLAW_SERVER_NAME,
    CyberHealthInstaller,
    DataMigrationError,
    SafetyBoundaryError,
    compute_sha256,
    format_text_report,
    main,
    verify_sqlite_integrity,
)


class BaseInstallerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="cyber-health-install-test-")
        self.test_dir = Path(self.temp_dir.name).resolve()

        # Isolated source project
        self.source_root = self.test_dir / "CyberHealthSource"
        self.source_root.mkdir(parents=True, exist_ok=True)
        (self.source_root / "pyproject.toml").write_text(
            '[project]\nname = "cyber-health-agent"\nversion = "0.2.4"\n', encoding="utf-8"
        )
        self.source_data = self.source_root / "data"
        self.source_data.mkdir(parents=True, exist_ok=True)
        self.source_db = self.source_data / "cyber-health.sqlite3"

        # Create a valid SQLite database in source
        conn = sqlite3.connect(self.source_db)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE test_fact (id INTEGER PRIMARY KEY, note TEXT);")
        cursor.execute("INSERT INTO test_fact (note) VALUES ('Initial Fact');")
        conn.commit()
        conn.close()

        # Isolated target installation directory
        self.target_dir = self.test_dir / ".cyber-health"

        # Fake bin dir
        self.bin_dir = self.test_dir / "fake_bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)

        # Fake OpenClaw CLI
        self.fake_openclaw_state = self.test_dir / "fake_openclaw_state.json"
        self.fake_openclaw_calls = self.test_dir / "fake_openclaw_calls.json"
        self.set_fake_openclaw_state({
            "servers": {},
            "show_mode": "absent",
            "set_mode": "success",
        })
        self.fake_openclaw_calls.write_text("[]", encoding="utf-8")
        self.fake_openclaw_bin = self.bin_dir / "openclaw"
        self._create_fake_openclaw_binary()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def set_fake_openclaw_state(self, state: dict) -> None:
        self.fake_openclaw_state.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _create_fake_openclaw_binary(self) -> None:
        script = f"""#!/usr/bin/env python3
import sys
import json
from pathlib import Path

state_file = Path(r"{self.fake_openclaw_state}")
calls_file = Path(r"{self.fake_openclaw_calls}")

calls = json.loads(calls_file.read_text(encoding="utf-8")) if calls_file.exists() else []
calls.append(sys.argv[1:])
calls_file.write_text(json.dumps(calls), encoding="utf-8")

state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {{}}

if len(sys.argv) >= 3 and sys.argv[1] == "mcp" and sys.argv[2] == "show":
    mode = state.get("show_mode", "absent")
    if mode == "absent":
        sys.stderr.write('No MCP server named "cyber-health"\\n')
        sys.exit(1)
    elif mode == "registered":
        servers = state.get("servers", {{}})
        srv = servers.get("cyber-health", {{
            "command": "/installed/cyber-health-mcp",
            "args": ["--db", "/installed/data/cyber-health.sqlite3", "--allow-all"],
            "cwd": "/installed",
        }})
        sys.stdout.write(json.dumps(srv) + "\\n")
        sys.exit(0)
    else:
        sys.stderr.write("Fake OpenClaw error\\n")
        sys.exit(2)

elif len(sys.argv) >= 3 and sys.argv[1] == "mcp" and sys.argv[2] == "set":
    mode = state.get("set_mode", "success")
    if mode == "success":
        server_name = sys.argv[3]
        payload = json.loads(sys.argv[4])
        servers = state.setdefault("servers", {{}})
        servers[server_name] = payload
        state["show_mode"] = "registered"
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        sys.stdout.write("OK\\n")
        sys.exit(0)
    else:
        sys.stderr.write("Fake set error\\n")
        sys.exit(1)

sys.exit(0)
"""
        self.fake_openclaw_bin.write_text(script, encoding="utf-8")
        self.fake_openclaw_bin.chmod(0o755)


class TestCyberHealthInstaller(BaseInstallerFixture):
    def test_safety_boundary_rejection(self) -> None:
        # Broad system path rejected
        with self.assertRaises(SafetyBoundaryError):
            CyberHealthInstaller(project_root=self.source_root, target_dir=Path("/Users"))

        # Protected name rejected
        with self.assertRaises(SafetyBoundaryError):
            CyberHealthInstaller(
                project_root=self.source_root,
                target_dir=self.test_dir / "obsidian-memory-extension",
            )

        # Symlink in target path rejected
        symlink_target = self.test_dir / "symlink_dir"
        os.symlink(self.source_root, symlink_target)
        with self.assertRaises(SafetyBoundaryError):
            CyberHealthInstaller(project_root=self.source_root, target_dir=symlink_target)

    def test_plan_data_migration_migrated(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=True,
        )
        plan = installer.plan_data_migration()
        self.assertEqual(plan.action, "migrated")
        self.assertIsNotNone(plan.source_sha256)
        self.assertEqual(plan.source_sha256, compute_sha256(self.source_db))
        self.assertIn("planned atomic migration", plan.reason)

    def test_plan_data_migration_preserve_existing(self) -> None:
        # Create target DB first
        target_data = self.target_dir / "data"
        target_data.mkdir(parents=True, exist_ok=True)
        target_db = target_data / "cyber-health.sqlite3"
        conn = sqlite3.connect(target_db)
        conn.execute("CREATE TABLE target_data (id INTEGER);")
        conn.commit()
        conn.close()

        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=True,
        )
        plan = installer.plan_data_migration()
        self.assertEqual(plan.action, "preserve_existing")
        self.assertIn("preserved existing user data", plan.reason)

    def test_plan_data_migration_fresh_install(self) -> None:
        # Source DB does not exist
        self.source_db.unlink()
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=True,
        )
        plan = installer.plan_data_migration()
        self.assertEqual(plan.action, "fresh_install")

    def test_plan_data_migration_preserves_orphan_target_sidecars(self) -> None:
        target_data = self.target_dir / "data"
        target_data.mkdir(parents=True, exist_ok=True)
        orphan_wal = target_data / "cyber-health.sqlite3-wal"
        orphan_wal.write_bytes(b"possible-recovery-artifact")

        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=True,
        )
        plan = installer.plan_data_migration()
        self.assertEqual(plan.action, "error")
        self.assertIn("sidecar", plan.reason)
        self.assertTrue(orphan_wal.exists())

    def test_plan_data_migration_aborts_when_wal_checkpoint_fails(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=True,
        )
        with mock.patch("cyber_health.install.safe_checkpoint_db", return_value=(False, "database is busy")):
            plan = installer.plan_data_migration()
        self.assertEqual(plan.action, "error")
        self.assertIn("checkpoint failed", plan.reason)

    def test_dry_run_zero_mutations(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=True,
        )
        report = installer.run()
        self.assertTrue(report.success)
        self.assertTrue(report.dry_run)
        self.assertFalse(self.target_dir.exists())

    def test_execute_data_migration_checksum_verified(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )
        plan = installer.plan_data_migration()
        self.assertEqual(plan.action, "migrated")

        installer.execute_data_migration(plan)
        self.assertTrue(plan.executed)
        self.assertTrue(installer.target_db_path.exists())

        # Checksum matches
        self.assertEqual(compute_sha256(self.source_db), compute_sha256(installer.target_db_path))

        # Integrity check passes
        ok, msg = verify_sqlite_integrity(installer.target_db_path)
        self.assertTrue(ok)

        # Source DB is strictly preserved
        self.assertTrue(self.source_db.exists())

    def test_execute_data_migration_aborts_when_wal_checkpoint_fails(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )
        plan = installer.plan_data_migration()
        with mock.patch("cyber_health.install.safe_checkpoint_db", return_value=(False, "database is busy")):
            with self.assertRaises(DataMigrationError):
                installer.execute_data_migration(plan)
        self.assertFalse(installer.target_db_path.exists())

    def test_execute_data_migration_wal_sidecar_cleanup(self) -> None:
        """Source DB in WAL mode must migrate cleanly without leaving .tmp_migration sidecars."""
        conn = sqlite3.connect(self.source_db)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("INSERT INTO test_fact (note) VALUES ('WAL Fact');")
        conn.commit()
        conn.close()

        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )
        plan = installer.plan_data_migration()
        self.assertEqual(plan.action, "migrated")

        installer.execute_data_migration(plan)
        self.assertTrue(plan.executed)
        self.assertTrue(installer.target_db_path.exists())

        # Verify no orphan .tmp_migration files or sidecars remain
        target_dir_files = [p.name for p in installer.data_dir.iterdir()]
        for fname in target_dir_files:
            self.assertNotIn("tmp_migration", fname)

        # Integrity check passes
        ok, msg = verify_sqlite_integrity(installer.target_db_path)
        self.assertTrue(ok)

    def test_openclaw_registration_flow(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )
        plan = installer.plan_openclaw()
        self.assertEqual(plan.action, "register")

        installer.register_openclaw(plan)
        self.assertTrue(plan.executed)

        # Verify state in fake OpenClaw
        state = json.loads(self.fake_openclaw_state.read_text(encoding="utf-8"))
        self.assertIn("cyber-health", state["servers"])
        self.assertEqual(state["servers"]["cyber-health"]["cwd"], str(self.target_dir))

    def test_cli_main_dry_run_json(self) -> None:
        argv = [
            "--project-root", str(self.source_root),
            "--target-dir", str(self.target_dir),
            "--openclaw-bin", str(self.fake_openclaw_bin),
            "--dry-run",
            "--json",
        ]
        ret = main(argv)
        self.assertEqual(ret, 0)
        self.assertFalse(self.target_dir.exists())

    def test_platform_helpers_win32_and_posix(self) -> None:
        from cyber_health.install import get_executable_name, get_venv_bin_dir

        base = Path("/app/test_venv")
        with mock.patch("sys.platform", "win32"):
            self.assertEqual(get_venv_bin_dir(base), base / "Scripts")
            self.assertEqual(get_executable_name("python"), "python.exe")
            self.assertEqual(get_executable_name("cyber-health-mcp"), "cyber-health-mcp.exe")
            self.assertEqual(get_executable_name("already.exe"), "already.exe")

        with mock.patch("sys.platform", "darwin"):
            self.assertEqual(get_venv_bin_dir(base), base / "bin")
            self.assertEqual(get_executable_name("python"), "python")
            self.assertEqual(get_executable_name("cyber-health-mcp"), "cyber-health-mcp")

    def test_is_system_broad_or_drive_root(self) -> None:
        from cyber_health.install import is_system_broad_or_drive_root

        self.assertTrue(is_system_broad_or_drive_root(Path("/")))
        self.assertTrue(is_system_broad_or_drive_root(Path.home()))

        with mock.patch("sys.platform", "win32"):
            drive_root = mock.MagicMock(spec=Path)
            drive_root.resolve.return_value = drive_root
            drive_root.anchor = "C:\\"
            drive_root.parts = ("C:\\",)
            # When resolved equals Path(anchor)
            with mock.patch("pathlib.Path", return_value=drive_root):
                self.assertTrue(is_system_broad_or_drive_root(drive_root))

    def test_plan_openclaw_malformed_json_fails_closed(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )
        bad_res = mock.MagicMock(returncode=0, stdout="INTERNAL_ERROR: {malformed json", stderr="")
        with mock.patch("subprocess.run", return_value=bad_res):
            plan = installer.plan_openclaw()
            self.assertEqual(plan.action, "error")
            self.assertIn("malformed JSON", plan.reason)

    def test_plan_openclaw_non_dict_json_fails_closed(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )
        non_dict_res = mock.MagicMock(returncode=0, stdout=json.dumps(["unexpected", "list"]), stderr="")
        with mock.patch("subprocess.run", return_value=non_dict_res):
            plan = installer.plan_openclaw()
            self.assertEqual(plan.action, "error")
            self.assertIn("non-dictionary", plan.reason)

    def test_plan_openclaw_foreign_command_fails_closed(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )
        foreign_data = {
            "command": "/usr/local/bin/foreign-mcp-tool",
            "args": ["--port", "9000"],
            "cwd": "/opt/other",
        }
        foreign_res = mock.MagicMock(returncode=0, stdout=json.dumps(foreign_data), stderr="")
        with mock.patch("subprocess.run", return_value=foreign_res):
            plan = installer.plan_openclaw()
            self.assertEqual(plan.action, "error")
            self.assertIn("Foreign MCP server", plan.reason)
            self.assertIn("refusing to overwrite", plan.reason)

    def test_installer_run_fails_closed_on_openclaw_foreign_command(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )
        foreign_data = {
            "command": "/usr/local/bin/alien-agent",
            "args": [],
            "cwd": "/opt/alien",
        }
        foreign_res = mock.MagicMock(returncode=0, stdout=json.dumps(foreign_data), stderr="")
        with mock.patch("subprocess.run", return_value=foreign_res):
            report = installer.run()
            self.assertFalse(report.success)
            self.assertEqual(report.openclaw.action, "error")
            self.assertFalse(self.target_dir.exists())

    def test_partial_install_returns_recovery_marker_without_deleting_data(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )

        def setup_partial_target() -> None:
            installer.target_dir.mkdir(parents=True, exist_ok=True)
            installer.data_dir.mkdir(parents=True, exist_ok=True)
            (installer.target_db_path).write_bytes(b"user-data-placeholder")

        with (
            mock.patch.object(installer, "setup_environment", side_effect=setup_partial_target),
            mock.patch.object(installer, "execute_data_migration"),
            mock.patch.object(installer, "register_openclaw", side_effect=RuntimeError("set failed")),
        ):
            report = installer.run()

        self.assertFalse(report.success)
        self.assertIn("partial", report.message.lower())
        self.assertIn("user data was preserved", report.message.lower())
        marker = installer.config_dir / "install-failure.json"
        self.assertTrue(marker.exists())
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(marker_data["phase"], "openclaw_registration")
        self.assertTrue(installer.target_db_path.exists())

    def test_plan_openclaw_cyber_health_command_updates(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )
        old_ch_data = {
            "command": "/old/.cyber-health/venv/bin/cyber-health-mcp",
            "args": ["--db", "/old/.cyber-health/data/cyber-health.sqlite3", "--allow-all"],
            "cwd": "/old/.cyber-health",
        }
        valid_res = mock.MagicMock(returncode=0, stdout=json.dumps(old_ch_data), stderr="")
        with mock.patch("subprocess.run", return_value=valid_res):
            plan = installer.plan_openclaw()
            self.assertEqual(plan.action, "update")
            self.assertIn("update registration", plan.reason)

    def test_safe_checkpoint_db_busy_detection(self) -> None:
        from cyber_health.install import safe_checkpoint_db

        test_db = self.source_root / "test_busy.sqlite3"
        conn = sqlite3.connect(test_db)
        conn.execute("CREATE TABLE t (x INT);")
        conn.commit()
        conn.close()

        mock_conn = mock.MagicMock()
        mock_cursor = mock.MagicMock()
        mock_cursor.fetchall.return_value = [(1, 10, 5)]
        mock_conn.cursor.return_value = mock_cursor
        with mock.patch("sqlite3.connect", return_value=mock_conn):
            ok, msg = safe_checkpoint_db(test_db)
            self.assertFalse(ok)
            self.assertIn("busy or incomplete", msg)


    def test_windows_batch_wrapper_generation(self) -> None:
        installer = CyberHealthInstaller(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=str(self.fake_openclaw_bin),
            dry_run=False,
        )
        installer.target_dir.mkdir(parents=True, exist_ok=True)
        installer.bin_dir.mkdir(parents=True, exist_ok=True)
        venv_scripts = installer.venv_dir / "Scripts"
        venv_scripts.mkdir(parents=True, exist_ok=True)
        fake_mcp = venv_scripts / "cyber-health-mcp.exe"
        fake_mcp.write_bytes(b"")

        with (
            mock.patch("sys.platform", "win32"),
            mock.patch("shutil.which", return_value=None),
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = mock.MagicMock(returncode=0)
            installer.setup_environment()
            cmd_file = installer.bin_dir / "cyber-health-mcp.cmd"
            self.assertTrue(cmd_file.exists())
            content = cmd_file.read_text(encoding="utf-8")
            self.assertIn("@echo off", content)
            self.assertIn("cyber-health-mcp.exe", content)


if __name__ == "__main__":
    unittest.main()
