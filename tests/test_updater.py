"""Deterministic unit tests for Cyber Health Agent updater.

Tests database snapshot backups, checksum validation, integrity checking,
and update workflow in isolated test fixtures.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from cyber_health.install import compute_sha256, get_executable_name, get_venv_bin_dir, verify_sqlite_integrity
from cyber_health.update import (
    CyberHealthUpdater,
    UpdateBackupError,
    UpdaterError,
    format_text_report,
    main,
)
from cyber_health.memory_plugin_release import MemoryPluginRelease
from cyber_health.core_release import CoreRelease


class BaseUpdaterFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="cyber-health-update-test-")
        self.test_dir = Path(self.temp_dir.name).resolve()

        # Source project
        self.source_root = self.test_dir / "CyberHealthSource"
        self.source_root.mkdir(parents=True, exist_ok=True)
        (self.source_root / "pyproject.toml").write_text(
            '[project]\nname = "cyber-health-agent"\nversion = "0.2.5"\n', encoding="utf-8"
        )

        # Installed target directory
        self.target_dir = self.test_dir / ".cyber-health"
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir = self.target_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.target_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir = self.data_dir / "backups"
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.venv_dir = self.target_dir / "venv"
        self.venv_bin = self.venv_dir / "bin"
        self.venv_bin.mkdir(parents=True, exist_ok=True)
        (self.venv_bin / "python").write_text("#!/bin/sh\nexit 0\n")
        (self.venv_bin / "python").chmod(0o755)

        # Existing metadata
        meta = {
            "version": "0.2.4",
            "installed_at": "2026-09-08T00:00:00Z",
            "target_dir": str(self.target_dir),
        }
        (self.config_dir / "installation.json").write_text(json.dumps(meta), encoding="utf-8")

        # Existing target database
        self.target_db = self.data_dir / "cyber-health.sqlite3"
        conn = sqlite3.connect(self.target_db)
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT);")
        cursor.execute("INSERT INTO users (id, name) VALUES ('u1', 'Alice');")
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()


class TestCyberHealthUpdater(BaseUpdaterFixture):
    def test_release_mode_resolves_core_wheel_without_local_source(self) -> None:
        release = CoreRelease("0.3.0", "cyber_health_agent-0.3.0-py3-none-any.whl", "https://github.com/annual30k/cyber-health-agent/releases/download/v0.3.0/cyber_health_agent-0.3.0-py3-none-any.whl", "a" * 64, "https://github.com/annual30k/cyber-health-agent/releases/tag/v0.3.0")
        updater = CyberHealthUpdater(target_dir=self.target_dir, dry_run=True, core_release_resolver=lambda: release)
        status = updater.prepare_core_release()
        self.assertEqual(status.action, "planned")
        self.assertEqual(updater.new_version, "0.3.0")

    def test_plugin_update_only_plans_when_a_new_verified_release_exists(self) -> None:
        release = MemoryPluginRelease(
            version="0.4.0",
            archive_name="obsidian-memory-plugin-0.4.0.tgz",
            archive_url="https://github.com/annual30k/obsidian-memory-plugin/releases/download/v0.4.0/obsidian-memory-plugin-0.4.0.tgz",
            sha256="a" * 64,
            release_url="https://github.com/annual30k/obsidian-memory-plugin/releases/tag/v0.4.0",
        )
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin="/usr/bin/true",
            dry_run=True,
            memory_plugin_release_resolver=lambda: release,
        )
        planned = updater.update_memory_plugin({"memory_plugin": {"action": "downloaded", "version": "0.3.9", "sha256": "b" * 64}})
        current = updater.update_memory_plugin({"memory_plugin": {"action": "downloaded", "version": "0.4.0", "sha256": "a" * 64}})
        self.assertEqual(planned.action, "planned")
        self.assertEqual(current.action, "reused")
    def test_inspect_installation(self) -> None:
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            dry_run=True,
        )
        meta = updater.inspect_installation()
        self.assertEqual(meta.get("version"), "0.2.4")
        self.assertEqual(updater.new_version, "0.2.5")

    def test_create_database_snapshot_verified(self) -> None:
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            dry_run=False,
        )
        status = updater.create_database_snapshot()
        self.assertTrue(status.created)
        self.assertTrue(Path(status.backup_file).exists())
        self.assertEqual(status.sha256, compute_sha256(self.target_db))

        # Check backup file SQLite integrity
        ok, msg = verify_sqlite_integrity(Path(status.backup_file))
        self.assertTrue(ok)

    def test_create_database_snapshot_dry_run(self) -> None:
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            dry_run=True,
        )
        status = updater.create_database_snapshot()
        self.assertFalse(status.created)
        self.assertIn("Dry run", status.reason)
        # No backup files created
        backups = list(self.backups_dir.glob("*.sqlite3"))
        self.assertEqual(len(backups), 0)

    def test_corrupted_database_backup_refusal(self) -> None:
        # Corrupt the target DB
        self.target_db.write_bytes(b"CORRUPTED_NON_SQLITE_DATA")
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            dry_run=False,
        )
        with self.assertRaises(UpdateBackupError):
            updater.create_database_snapshot()

    def test_snapshot_aborts_when_wal_checkpoint_fails(self) -> None:
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            dry_run=False,
        )
        with mock.patch("cyber_health.update.safe_checkpoint_db", return_value=(False, "database is busy")):
            with self.assertRaises(UpdateBackupError):
                updater.create_database_snapshot()
        self.assertEqual(list(self.backups_dir.glob("*.sqlite3")), [])

    def test_updater_cli_dry_run_json(self) -> None:
        argv = [
            "--project-root", str(self.source_root),
            "--target-dir", str(self.target_dir),
            "--dry-run",
            "--json",
        ]
        ret = main(argv)
        self.assertEqual(ret, 0)

    def test_update_revalidates_stored_hermes_or_codex_memory_without_openclaw(self) -> None:
        vault = self.test_dir / "Vault"
        project_id = "Cyber-Health-Agent-test"
        project = vault / "20-Projects" / project_id
        project.mkdir(parents=True)
        (vault / "00-System").mkdir()
        (vault / "00-System" / "projects.yaml").write_text(
            f"projects:\n  - id: {project_id}\n    roots: []\n    scope: private\n",
            encoding="utf-8",
        )
        metadata_file = self.config_dir / "installation.json"
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        metadata["memory"] = {
            "state": "connected",
            "vault_path": str(vault),
            "project_id": project_id,
        }
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")

        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin=None,
            codex_bin=None,
            hermes_bin=None,
            dry_run=True,
        )
        report = updater.run()

        self.assertTrue(report.success)
        self.assertTrue(report.memory.connected)
        self.assertFalse(report.memory.plugin_loaded)
        self.assertEqual(report.memory.provider_args[-1], project_id)

    def test_updater_fail_closed_on_schema_failure(self) -> None:
        """When schema verification fails, updater fails closed: success=False and metadata untouched."""
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            dry_run=False,
        )
        with mock.patch.object(updater, "upgrade_package", return_value=True):
            with mock.patch.object(updater, "verify_schema_and_service", return_value=False):
                report = updater.run()

        self.assertFalse(report.success)
        self.assertFalse(report.schema_verified)
        self.assertFalse(report.openclaw_verified)
        self.assertIn("fail-closed", report.message.lower())
        self.assertIn("backup", report.message.lower())

        # Metadata must NOT be updated
        meta = json.loads((self.config_dir / "installation.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["version"], "0.2.4")

    def test_updater_fail_closed_on_openclaw_failure(self) -> None:
        """When OpenClaw registration verification fails, updater fails closed without updating metadata."""
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            dry_run=False,
        )
        with mock.patch.object(updater, "upgrade_package", return_value=True):
            with mock.patch.object(updater, "verify_schema_and_service", return_value=True):
                with mock.patch.object(updater, "verify_openclaw", return_value=False):
                    report = updater.run()

        self.assertFalse(report.success)
        self.assertTrue(report.schema_verified)
        self.assertFalse(report.openclaw_verified)
        self.assertIn("fail-closed", report.message.lower())
        self.assertIn("backup", report.message.lower())

        # Metadata must NOT be updated
        meta = json.loads((self.config_dir / "installation.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["version"], "0.2.4")

    def test_updater_wal_checkpoint_and_sidecar_cleanup(self) -> None:
        """Target database in WAL mode is safely checkpointed and snapshot backup leaves no orphan sidecars."""
        conn = sqlite3.connect(self.target_db)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("INSERT INTO users (id, name) VALUES ('u2', 'Bob');")
        conn.commit()
        conn.close()

        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            dry_run=False,
        )
        status = updater.create_database_snapshot()
        self.assertTrue(status.created)
        self.assertTrue(Path(status.backup_file).exists())

        # Check that no stray -wal or -shm sidecars remain for the backup file in backups/
        backup_p = Path(status.backup_file)
        self.assertFalse((backup_p.parent / (backup_p.name + "-wal")).exists())
        self.assertFalse((backup_p.parent / (backup_p.name + "-shm")).exists())

        # Integrity check on backup file passes
        ok, msg = verify_sqlite_integrity(backup_p)
        self.assertTrue(ok)

    def test_verify_openclaw_permission_denied_fails_closed(self) -> None:
        """When OpenClaw CLI cannot be executed due to permission/OS error, fail closed."""
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin="/fake/bin/openclaw",
            dry_run=False,
        )
        with mock.patch("subprocess.run", side_effect=PermissionError("Permission denied")):
            self.assertFalse(updater.verify_openclaw())

    def test_verify_openclaw_cli_crash_fails_closed(self) -> None:
        """When OpenClaw CLI crashes or returns non-zero without absent indicator, fail closed."""
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin="/fake/bin/openclaw",
            dry_run=False,
        )
        crash_res = mock.Mock(returncode=2, stderr="fatal error: panic / segfault", stdout="")
        with mock.patch("subprocess.run", return_value=crash_res) as mock_run:
            self.assertFalse(updater.verify_openclaw())
            # Must NOT blindly attempt mcp set
            self.assertEqual(mock_run.call_count, 1)

    def test_verify_openclaw_corrupted_json_fails_closed(self) -> None:
        """When OpenClaw CLI returns malformed JSON, fail closed and never overwrite."""
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin="/fake/bin/openclaw",
            dry_run=False,
        )
        corrupt_res = mock.Mock(returncode=0, stdout="INTERNAL_SERVER_ERROR: {not valid json", stderr="")
        with mock.patch("subprocess.run", return_value=corrupt_res) as mock_run:
            self.assertFalse(updater.verify_openclaw())
            self.assertEqual(mock_run.call_count, 1)

    def test_verify_openclaw_foreign_registration_fails_closed(self) -> None:
        """When cyber-health is registered to a foreign/unrelated binary, fail closed."""
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin="/fake/bin/openclaw",
            dry_run=False,
        )
        foreign_data = {
            "command": "/usr/local/bin/some-other-tool",
            "args": ["--port", "8080"],
            "cwd": "/opt/other",
        }
        foreign_res = mock.Mock(returncode=0, stdout=json.dumps(foreign_data), stderr="")
        with mock.patch("subprocess.run", return_value=foreign_res) as mock_run:
            self.assertFalse(updater.verify_openclaw())
            # Must NOT overwrite foreign server
            self.assertEqual(mock_run.call_count, 1)

    def test_verify_openclaw_confirmed_absent_triggers_set(self) -> None:
        """When OpenClaw CLI explicitly confirms server is absent, register it."""
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin="/fake/bin/openclaw",
            dry_run=False,
        )
        absent_res = mock.Mock(returncode=1, stderr='Error: No MCP server named "cyber-health" found', stdout="")
        set_res = mock.Mock(returncode=0, stderr="", stdout="Server cyber-health configured")

        def run_side_effect(cmd, **kwargs):
            if "show" in cmd:
                return absent_res
            if "set" in cmd:
                return set_res
            return mock.Mock(returncode=0)

        with mock.patch("subprocess.run", side_effect=run_side_effect) as mock_run:
            self.assertTrue(updater.verify_openclaw())
            self.assertEqual(mock_run.call_count, 2)
            set_call = mock_run.call_args_list[1][0][0]
            self.assertIn("set", set_call)
            self.assertIn("cyber-health", set_call)

    def test_verify_openclaw_already_up_to_date(self) -> None:
        """When registration is already pointing to current venv and db, return True without set."""
        updater = CyberHealthUpdater(
            project_root=self.source_root,
            target_dir=self.target_dir,
            openclaw_bin="/fake/bin/openclaw",
            dry_run=False,
        )
        target_mcp = str(get_venv_bin_dir(updater.venv_dir) / get_executable_name("cyber-health-mcp"))
        current_data = {
            "command": target_mcp,
            "args": ["--db", str(updater.target_db_path), "--allow-all"],
            "cwd": str(updater.target_dir),
        }
        valid_res = mock.Mock(returncode=0, stdout=json.dumps(current_data), stderr="")
        with mock.patch("subprocess.run", return_value=valid_res) as mock_run:
            self.assertTrue(updater.verify_openclaw())
            # Exactly 1 call (show), no set needed
            self.assertEqual(mock_run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
