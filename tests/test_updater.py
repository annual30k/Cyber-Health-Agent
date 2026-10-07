"""Deterministic unit tests for Cyber Health Agent updater.

Tests database snapshot backups, checksum validation, integrity checking,
and update workflow in isolated test fixtures.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cyber_health.command_shim import _shim_path, _target_path
from cyber_health.core_release import CoreRelease, CoreReleaseError
from cyber_health.install import compute_sha256, get_executable_name, get_venv_bin_dir, verify_sqlite_integrity
from cyber_health.memory_plugin_release import MemoryPluginRelease, MemoryPluginReleaseStatus
from cyber_health.update import (
    BackupStatus,
    CyberHealthUpdater,
    UpdateBackupError,
    UpdateReport,
    main,
)
from test_support import isolate_host_clis


class BaseUpdaterFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="cyber-health-update-test-")
        self.test_dir = Path(self.temp_dir.name).resolve()
        isolate_host_clis(self, self.test_dir, "update")

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
        ok, _msg = verify_sqlite_integrity(Path(status.backup_file))
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
        ok, _msg = verify_sqlite_integrity(backup_p)
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


class TestUpdateLinksCommand(BaseUpdaterFixture):
    """An update gives older installations the `cyber-health` PATH entry."""

    def report(self, success: bool) -> UpdateReport:
        return UpdateReport(
            dry_run=False, success=success, target_dir=str(self.target_dir), old_version="0.4.2",
            new_version="0.5.0", backup=BackupStatus(source_db=str(self.target_db)), message="done",
        )

    def test_successful_update_links_the_command(self) -> None:
        venv_bin = get_venv_bin_dir(self.venv_dir)
        venv_bin.mkdir(parents=True, exist_ok=True)
        _target_path(venv_bin).write_text("", encoding="utf-8")
        updater = CyberHealthUpdater(project_root=self.source_root, target_dir=self.target_dir, dry_run=False)
        with mock.patch.object(CyberHealthUpdater, "_run", return_value=self.report(True)):
            report = updater.run()
        self.assertEqual(report.command_shim.action, "linked")
        self.assertTrue(_shim_path(Path(os.environ["CYBER_HEALTH_USER_BIN_DIR"])).exists())

    def test_failed_update_leaves_path_alone(self) -> None:
        updater = CyberHealthUpdater(project_root=self.source_root, target_dir=self.target_dir, dry_run=False)
        with mock.patch.object(CyberHealthUpdater, "_run", return_value=self.report(False)):
            report = updater.run()
        self.assertEqual(report.command_shim.action, "skipped")
        self.assertFalse(Path(os.environ["CYBER_HEALTH_USER_BIN_DIR"]).exists())


class TestUpdateLifecycleImprovements(BaseUpdaterFixture):
    """Up-to-date short-circuit, --check, hand-off to the new version, private backups."""

    def release(self, version: str) -> CoreRelease:
        return CoreRelease(
            version, f"cyber_health_agent-{version}-py3-none-any.whl",
            f"https://github.com/annual30k/cyber-health-agent/releases/download/v{version}/w.whl", "a" * 64,
            f"https://github.com/annual30k/cyber-health-agent/releases/tag/v{version}",
        )

    def updater(self, version: str = "0.2.4", **kwargs) -> CyberHealthUpdater:
        return CyberHealthUpdater(
            target_dir=self.target_dir, openclaw_bin=None, codex_bin=None, hermes_bin=None,
            core_release_resolver=lambda: self.release(version), **kwargs,
        )

    def test_already_latest_skips_backup_and_reinstall(self) -> None:
        updater = self.updater("0.2.4")
        with mock.patch.object(CyberHealthUpdater, "upgrade_package") as upgrade:
            report = updater.run()
        self.assertTrue(report.success)
        self.assertTrue(report.up_to_date)
        self.assertFalse(report.backup.created)
        upgrade.assert_not_called()
        self.assertEqual(list(self.backups_dir.iterdir()), [])
        self.assertIn("Already up to date", report.message)

    def test_force_reinstalls_even_when_latest(self) -> None:
        updater = self.updater("0.2.4", force=True)
        with mock.patch.object(CyberHealthUpdater, "upgrade_package", return_value=True) as upgrade, \
                mock.patch.object(CyberHealthUpdater, "should_hand_off", return_value=False), \
                mock.patch.object(CyberHealthUpdater, "update_memory_plugin", return_value=MemoryPluginReleaseStatus()):
            report = updater.run()
        upgrade.assert_called_once()
        self.assertFalse(report.up_to_date)
        self.assertTrue(report.backup.created)

    def test_check_reports_available_update_without_mutation(self) -> None:
        result = self.updater("0.9.0").check_for_update()
        self.assertEqual((result["installed_version"], result["latest_version"]), ("0.2.4", "0.9.0"))
        self.assertTrue(result["update_available"])
        self.assertFalse(self.updater("0.2.4").check_for_update()["update_available"])
        self.assertEqual(list(self.backups_dir.iterdir()), [])

    def test_check_reports_resolver_errors(self) -> None:
        def broken():
            raise CoreReleaseError("offline")
        updater = CyberHealthUpdater(target_dir=self.target_dir, openclaw_bin=None, codex_bin=None, hermes_bin=None,
                                     core_release_resolver=broken)
        result = updater.check_for_update()
        self.assertIsNone(result["update_available"])
        self.assertIn("offline", result["error"])

    def test_new_version_finishes_its_own_upgrade(self) -> None:
        updater = self.updater("9.9.9", dry_run=False)
        child = {"dry_run": False, "success": True, "target_dir": str(self.target_dir), "old_version": "0.2.4",
                 "new_version": "9.9.9", "backup": {"created": True}, "finished_by": "9.9.9",
                 "future_field": "ignored", "message": "Update completed successfully"}
        completed = mock.MagicMock(returncode=0, stdout=json.dumps(child), stderr="")
        with mock.patch("cyber_health.update.subprocess.run", return_value=completed) as run:
            report = updater.hand_off("0.2.4", BackupStatus(created=True), True)
        cmd = run.call_args.args[0]
        self.assertIn("--finish-upgrade", cmd)
        self.assertEqual(cmd[1:4], ["-P", "-m", "cyber_health.update"])
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.target_dir))
        self.assertTrue(report.handed_off)
        self.assertEqual(report.finished_by, "9.9.9")
        self.assertFalse((self.config_dir / "update-handoff.json").exists())

    def test_hand_off_falls_back_when_new_version_cannot_finish(self) -> None:
        updater = self.updater("9.9.9", dry_run=False)
        broken = mock.MagicMock(returncode=2, stdout="", stderr="unrecognized arguments: --finish-upgrade")
        with mock.patch("cyber_health.update.subprocess.run", return_value=broken):
            self.assertIsNone(updater.hand_off("0.2.4", BackupStatus(created=True), True))

    def test_finish_upgrade_entry_runs_post_update_steps(self) -> None:
        handoff = self.config_dir / "handoff.json"
        handoff.write_text(json.dumps({"old_version": "0.2.4", "new_version": "9.9.9", "backup": {"created": True},
                                       "core_release": {"action": "downloaded"}, "package_updated": True}), encoding="utf-8")
        updater = self.updater("9.9.9", dry_run=False)
        finished = UpdateReport(dry_run=False, success=True, target_dir=str(self.target_dir), old_version="0.2.4",
                                new_version="9.9.9", backup=BackupStatus(created=True), message="done")
        with mock.patch.object(CyberHealthUpdater, "_finish", return_value=finished) as finish:
            report = updater.run_finish(handoff)
        self.assertEqual(finish.call_args.args[1], "0.2.4")
        self.assertTrue(report.success)
        self.assertNotEqual(report.command_shim.action, "none")

    def test_finish_upgrade_records_the_handed_off_version(self) -> None:
        """Regression: the child once wrote its source-tree fallback version ("0.2.6") into metadata."""
        handoff = self.config_dir / "handoff.json"
        handoff.write_text(json.dumps({"old_version": "0.2.4", "new_version": "9.9.9", "backup": {"created": True},
                                       "core_release": {"action": "downloaded", "version": "9.9.9"},
                                       "package_updated": True}), encoding="utf-8")
        updater = CyberHealthUpdater(target_dir=self.target_dir, openclaw_bin=None, codex_bin=None, hermes_bin=None,
                                     dry_run=False)
        with mock.patch.object(CyberHealthUpdater, "update_memory_plugin", return_value=MemoryPluginReleaseStatus()), \
                mock.patch.object(CyberHealthUpdater, "verify_schema_and_service", return_value=True), \
                mock.patch.object(CyberHealthUpdater, "verify_openclaw", return_value=True):
            report = updater.run_finish(handoff)
        self.assertTrue(report.success, report.message)
        self.assertEqual(report.new_version, "9.9.9")
        meta = json.loads((self.config_dir / "installation.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["version"], "9.9.9")

    def test_release_mode_version_fallback_is_the_running_package(self) -> None:
        import cyber_health

        updater = CyberHealthUpdater(target_dir=self.target_dir, openclaw_bin=None, codex_bin=None, hermes_bin=None)
        self.assertEqual(updater.new_version, cyber_health.__version__)

    def test_should_hand_off_only_for_a_real_version_change(self) -> None:
        venv_bin = get_venv_bin_dir(self.venv_dir)
        venv_bin.mkdir(parents=True, exist_ok=True)
        (venv_bin / get_executable_name("python")).write_text("", encoding="utf-8")
        updater = self.updater("9.9.9", dry_run=False)
        updater.prepare_core_release()
        self.assertTrue(updater.should_hand_off(True))
        self.assertFalse(updater.should_hand_off(False))
        self.assertFalse(self.updater("9.9.9", dry_run=True).should_hand_off(True))
        with mock.patch.dict(os.environ, {"CYBER_HEALTH_NO_HANDOFF": "1"}):
            self.assertFalse(updater.should_hand_off(True))

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_backup_snapshot_is_private(self) -> None:
        status = self.updater(dry_run=False).create_database_snapshot()
        self.assertEqual(os.stat(status.backup_file).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
