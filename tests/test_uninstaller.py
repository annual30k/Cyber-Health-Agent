"""Deterministic isolated unit and integration tests for Cyber Health uninstaller.

Uses deterministic fake host tools inside temporary fixtures.
Guarantees zero mutations on real user host, zero interference with obsidian-memory,
Obsidian Vaults, or unrelated configurations.
"""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

from cyber_health.uninstall import (
    DEFAULT_LAUNCHAGENT_LABEL,
    FIXED_OPENCLAW_SERVER_NAME,
    FOREIGN_UNSET_CONFIRMATION_TOKEN,
    PURGE_CONFIRMATION_TOKEN,
    CyberHealthUninstaller,
    OwnershipVerificationError,
    PurgeValidationError,
    SafetyBoundaryError,
    format_text_report,
    main,
)
from test_support import make_python_command


class BaseFakeHostTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="cyber-health-uninstall-test-")
        self.test_dir = Path(self.temp_dir.name).resolve()

        # Isolated project root
        self.project_root = self.test_dir / "CyberHealthProject"
        self.project_root.mkdir(parents=True, exist_ok=True)
        (self.project_root / "pyproject.toml").write_text("[project]\nname='cyber-health-agent'\n")
        (self.project_root / ".venv").mkdir()
        (self.project_root / ".venv" / "bin").mkdir()
        (self.project_root / "dist").mkdir()
        self.data_dir = self.project_root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_dir / "cyber-health.sqlite3"
        self.db_path.write_bytes(b"FAKE_SQLITE_HEALTH_DATA")
        self.wal_path = self.data_dir / "cyber-health.sqlite3-wal"
        self.wal_path.write_bytes(b"FAKE_WAL_DATA")
        self.shm_path = self.data_dir / "cyber-health.sqlite3-shm"
        self.shm_path.write_bytes(b"FAKE_SHM_DATA")
        self.export_path = self.data_dir / "export_2026.json"
        self.export_path.write_text("{\"export\": true}")

        # Unknown file in data/ that should never be purged
        self.unknown_file = self.data_dir / "custom_user_notes.txt"
        self.unknown_file.write_text("IMPORTANT_USER_NOTES")
        self.unknown_subdir = self.data_dir / "user_subfolder"
        self.unknown_subdir.mkdir()
        (self.unknown_subdir / "subfile.txt").write_text("SUBFILE")

        # Isolated LaunchAgents dir
        self.launchagent_dir = self.test_dir / "LaunchAgents"
        self.launchagent_dir.mkdir(parents=True, exist_ok=True)

        # Isolated bin dir for fake host executables
        self.bin_dir = self.test_dir / "fake_bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)

        # State tracking for fake OpenClaw
        self.fake_openclaw_state_file = self.test_dir / "fake_openclaw_state.json"
        self.fake_openclaw_calls_file = self.test_dir / "fake_openclaw_calls.json"
        self.set_fake_openclaw_state({
            "servers": {},
            "show_mode": "normal",
            "show_error_message": "",
            "unset_mode": "normal",
        })

        # Fake OpenClaw executable script
        self.fake_openclaw_bin = self._create_fake_openclaw_binary()

        # State tracking for fake launchctl
        self.fake_launchctl_calls_file = self.test_dir / "fake_launchctl_calls.json"
        self.fake_launchctl_calls_file.write_text("[]")
        self.fake_launchctl_bin = self._create_fake_launchctl_binary()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def set_fake_openclaw_state(self, state: dict) -> None:
        self.fake_openclaw_state_file.write_text(json.dumps(state, indent=2))

    def get_fake_openclaw_state(self) -> dict:
        if not self.fake_openclaw_state_file.exists():
            return {}
        return json.loads(self.fake_openclaw_state_file.read_text())

    def get_fake_openclaw_calls(self) -> list[list[str]]:
        if not self.fake_openclaw_calls_file.exists():
            return []
        return json.loads(self.fake_openclaw_calls_file.read_text())

    def _create_fake_openclaw_binary(self) -> Path:
        script_code = f"""import sys, json
from pathlib import Path

state_file = Path({repr(str(self.fake_openclaw_state_file))})
calls_file = Path({repr(str(self.fake_openclaw_calls_file))})

calls = []
if calls_file.exists():
    try:
        calls = json.loads(calls_file.read_text())
    except Exception:
        pass
calls.append(sys.argv[1:])
calls_file.write_text(json.dumps(calls))

state = json.loads(state_file.read_text()) if state_file.exists() else {{}}
args = sys.argv[1:]

if len(args) >= 3 and args[0] == "mcp" and args[1] == "show":
    name = args[2]
    mode = state.get("show_mode", "normal")
    if mode == "error":
        sys.stderr.write(state.get("show_error_message", "OpenClaw CLI crash error") + "\\n")
        sys.exit(1)
    if mode == "corrupt_json":
        sys.stdout.write("{{ malformed invalid json payload\\n")
        sys.exit(0)
    if mode == "absent":
        sys.stderr.write(f'No MCP server named "{{name}}" in config\\n')
        sys.exit(1)
    
    servers = state.get("servers", {{}})
    if name in servers:
        sys.stdout.write(json.dumps(servers[name]) + "\\n")
        sys.exit(0)
    else:
        sys.stderr.write(f'No MCP server named "{{name}}" in config\\n')
        sys.exit(1)

if len(args) >= 3 and args[0] == "mcp" and args[1] == "unset":
    name = args[2]
    if state.get("unset_mode") == "error":
        sys.stderr.write("Failed to write openclaw config\\n")
        sys.exit(1)
    servers = state.get("servers", {{}})
    if name in servers:
        del servers[name]
        state["servers"] = servers
        state_file.write_text(json.dumps(state))
        sys.stdout.write(f'Removed MCP server "{{name}}"\\n')
        sys.exit(0)
    else:
        sys.stderr.write(f'No MCP server named "{{name}}" in config\\n')
        sys.exit(1)

sys.stderr.write(f"Unknown fake openclaw command: {{args}}\\n")
sys.exit(1)
"""
        return make_python_command(self.bin_dir, "openclaw", script_code)

    def _create_fake_launchctl_binary(self) -> Path:
        script_code = f"""import sys, json
from pathlib import Path

calls_file = Path({repr(str(self.fake_launchctl_calls_file))})
calls = []
if calls_file.exists():
    try:
        calls = json.loads(calls_file.read_text())
    except Exception:
        pass
calls.append(sys.argv[1:])
calls_file.write_text(json.dumps(calls))
sys.exit(0)
"""
        return make_python_command(self.bin_dir, "launchctl", script_code)

    def create_launchagent_plist(self, label: str, program_args: list[str]) -> Path:
        plist_path = self.launchagent_dir / f"{label}.plist"
        plist_data = {
            "Label": label,
            "ProgramArguments": program_args,
            "WorkingDirectory": str(self.project_root),
        }
        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)
        return plist_path


class TestDeterministicDryRunAndPurity(BaseFakeHostTest):
    def test_dry_run_purity(self) -> None:
        """Dry run deterministically plans actions without making any mutations."""
        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": str(self.project_root / ".venv/bin/python"),
                    "args": ["-m", "cyber_health_mcp"],
                    "cwd": str(self.project_root),
                    "env": {"CYBER_HEALTH_DB": str(self.db_path)},
                }
            }
        })
        plist_path = self.create_launchagent_plist(
            DEFAULT_LAUNCHAGENT_LABEL,
            [str(self.project_root / ".venv/bin/python"), "-m", "cyber_health_mcp"],
        )

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=True,
            purge_data=False,
        )

        report = uninstaller.run()
        self.assertTrue(report.dry_run)
        self.assertTrue(report.success)
        self.assertTrue(report.openclaw.detected)
        self.assertTrue(report.openclaw.ownership_proven)
        self.assertEqual(report.openclaw.action, "unset")
        self.assertFalse(report.openclaw.executed)

        self.assertTrue(report.launchagent.detected)
        self.assertTrue(report.launchagent.ownership_proven)
        self.assertEqual(report.launchagent.action, "unload_and_remove")
        self.assertFalse(report.launchagent.executed)

        # Verify zero mutations
        state = self.get_fake_openclaw_state()
        self.assertIn(FIXED_OPENCLAW_SERVER_NAME, state["servers"])
        self.assertTrue(plist_path.exists())
        self.assertTrue(self.db_path.exists())
        self.assertTrue(self.wal_path.exists())
        self.assertTrue(self.shm_path.exists())
        self.assertTrue(self.export_path.exists())
        self.assertTrue(self.unknown_file.exists())


class TestNormalUninstallDataPreservation(BaseFakeHostTest):
    def test_normal_uninstall_preserves_data(self) -> None:
        """Normal execution unregisters owned integrations while strictly preserving all data and sources."""
        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": str(self.project_root / ".venv/bin/python"),
                    "args": ["-m", "cyber_health_mcp"],
                    "cwd": str(self.project_root),
                    "env": {"CYBER_HEALTH_DB": str(self.db_path)},
                }
            }
        })
        plist_path = self.create_launchagent_plist(
            DEFAULT_LAUNCHAGENT_LABEL,
            [str(self.project_root / ".venv/bin/cyber-health-mcp")],
        )

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
            purge_data=False,
        )

        report = uninstaller.run()
        self.assertTrue(report.success)
        self.assertTrue(report.openclaw.executed)
        self.assertTrue(report.launchagent.executed)

        # OpenClaw server removed, LaunchAgent removed
        state = self.get_fake_openclaw_state()
        self.assertNotIn(FIXED_OPENCLAW_SERVER_NAME, state["servers"])
        self.assertFalse(plist_path.exists())

        # Strict preservation of all database, WAL, SHM, exports, sources, and .venv
        self.assertTrue(self.db_path.exists())
        self.assertEqual(self.db_path.read_bytes(), b"FAKE_SQLITE_HEALTH_DATA")
        self.assertTrue(self.wal_path.exists())
        self.assertTrue(self.shm_path.exists())
        self.assertTrue(self.export_path.exists())
        self.assertTrue(self.unknown_file.exists())
        self.assertTrue((self.project_root / ".venv").exists())
        self.assertTrue((self.project_root / "dist").exists())
        self.assertTrue((self.project_root / "pyproject.toml").exists())

        # Preserved paths reported
        self.assertIn(str(self.db_path), report.data.preserved_paths)
        self.assertEqual(len(report.data.purged_paths), 0)


class TestFailClosedOrderingAndValidation(BaseFakeHostTest):
    def test_fail_closed_zero_mutations_when_host_refused(self) -> None:
        """If OpenClaw registration is foreign/refused, ZERO mutations occur even with --purge-data."""
        foreign_dir = self.test_dir / "ForeignProject"
        foreign_dir.mkdir()
        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": "/usr/local/bin/python3",
                    "args": ["-m", "cyber_health_mcp"],
                    "cwd": str(foreign_dir),
                }
            }
        })
        plist_path = self.create_launchagent_plist(
            DEFAULT_LAUNCHAGENT_LABEL,
            [str(self.project_root / ".venv/bin/cyber-health-mcp")],
        )

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
            purge_data=True,
            confirm_purge=PURGE_CONFIRMATION_TOKEN,
        )

        report = uninstaller.run()
        self.assertFalse(report.success)
        self.assertEqual(report.openclaw.action, "refused")

        # FAIL-CLOSED CHECK: Zero mutations performed!
        # Data must NOT be purged
        self.assertTrue(self.db_path.exists())
        self.assertTrue(self.wal_path.exists())
        # LaunchAgent must NOT be removed
        self.assertTrue(plist_path.exists())
        # OpenClaw must NOT be unset
        state = self.get_fake_openclaw_state()
        self.assertIn(FIXED_OPENCLAW_SERVER_NAME, state["servers"])


class TestOwnershipRefusalAndPrefixCollision(BaseFakeHostTest):
    def test_prefix_collision_rejection(self) -> None:
        """A directory sharing a path prefix (e.g. ProjectEvil vs Project) must be rejected."""
        evil_project = self.test_dir / (self.project_root.name + "Evil")
        evil_project.mkdir()
        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": str(evil_project / "bin" / "python"),
                    "args": ["-m", "cyber_health_mcp"],
                    "cwd": str(evil_project),
                }
            }
        })

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
        )

        report = uninstaller.run()
        self.assertFalse(report.success)
        self.assertFalse(report.openclaw.ownership_proven)
        self.assertEqual(report.openclaw.action, "refused")

    def test_command_signature_spoofing_rejected(self) -> None:
        """Matching cwd but non-Cyber-Health command must be rejected."""
        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": "/bin/bash",
                    "args": ["-c", "echo hello"],
                    "cwd": str(self.project_root),
                }
            }
        })

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
        )

        report = uninstaller.run()
        self.assertFalse(report.success)
        self.assertEqual(report.openclaw.action, "refused")

    def test_foreign_override_requires_double_confirmation(self) -> None:
        """--force-foreign-host-mcp requires exact confirmation token."""
        foreign_dir = self.test_dir / "ForeignProject"
        foreign_dir.mkdir()
        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": "/usr/local/bin/python",
                    "args": ["-m", "cyber_health_mcp"],
                    "cwd": str(foreign_dir),
                }
            }
        })

        # Attempt 1: flag without confirmation token -> refused
        uninstaller_no_token = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            force_foreign_host_mcp=True,
            confirm_foreign_unset=None,
        )
        report1 = uninstaller_no_token.run()
        self.assertFalse(report1.success)
        self.assertEqual(report1.openclaw.action, "refused")

        # Attempt 2: flag with exact confirmation token -> unset
        uninstaller_with_token = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            force_foreign_host_mcp=True,
            confirm_foreign_unset=FOREIGN_UNSET_CONFIRMATION_TOKEN,
        )
        report2 = uninstaller_with_token.run()
        self.assertTrue(report2.success)
        self.assertEqual(report2.openclaw.action, "unset")


class TestOpenClawInspectionErrors(BaseFakeHostTest):
    def test_cli_crash_or_permission_denied_is_error_not_absent(self) -> None:
        """Any OpenClaw CLI error other than exact 'No MCP server named' is treated as error and redacts secrets."""
        secret_token = "FATAL_SECRET_TOKEN=MY_SECRET_KEY_12345"
        self.set_fake_openclaw_state({
            "show_mode": "error",
            "show_error_message": f"EACCES: permission denied opening openclaw.json, token={secret_token}",
        })

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
        )

        report = uninstaller.run()
        self.assertFalse(report.success)
        self.assertEqual(report.openclaw.action, "error")
        # Fixed safe category:
        self.assertIn("OpenClaw CLI inspection failed with exit code 1", report.openclaw.reason)

        # Ensure raw output and secrets are absent from the entire serialized report
        serialized_json = json.dumps(asdict(report))
        self.assertNotIn(secret_token, serialized_json)
        self.assertNotIn("MY_SECRET_KEY_12345", serialized_json)
        self.assertNotIn("permission denied", serialized_json)

        text_rep = format_text_report(report)
        self.assertNotIn(secret_token, text_rep)
        self.assertNotIn("MY_SECRET_KEY_12345", text_rep)
        self.assertNotIn("permission denied", text_rep)

    def test_corrupt_json_is_error(self) -> None:
        """Corrupt JSON returned by OpenClaw CLI fails closed."""
        self.set_fake_openclaw_state({
            "show_mode": "corrupt_json",
        })

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
        )

        report = uninstaller.run()
        self.assertFalse(report.success)
        self.assertEqual(report.openclaw.action, "error")
        self.assertIn("malformed JSON", report.openclaw.reason)


class TestSanitizedReportingNoEnvLeakage(BaseFakeHostTest):
    def test_report_redacts_raw_env_and_secrets(self) -> None:
        """Report details must never leak raw env variables or tokens."""
        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": str(self.project_root / ".venv/bin/python"),
                    "args": ["-m", "cyber_health_mcp"],
                    "cwd": str(self.project_root),
                    "env": {
                        "CYBER_HEALTH_DB": str(self.db_path),
                        "SUPER_SECRET_TOKEN": "SECRET_VALUE_12345",
                    },
                }
            }
        })

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=True,
        )

        report = uninstaller.run()
        self.assertTrue(report.success)
        details_str = json.dumps(report.openclaw.details)
        self.assertNotIn("SUPER_SECRET_TOKEN", details_str)
        self.assertNotIn("SECRET_VALUE_12345", details_str)


class TestPurgeSecurityAndBoundaries(BaseFakeHostTest):
    def test_purge_removes_only_approved_files_and_preserves_unknowns(self) -> None:
        """Purge removes strictly DB, WAL, SHM, and exports; never unknown files or directories."""
        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
            purge_data=True,
            confirm_purge=PURGE_CONFIRMATION_TOKEN,
        )

        report = uninstaller.run()
        self.assertTrue(report.success)

        # Approved items purged
        self.assertFalse(self.db_path.exists())
        self.assertFalse(self.wal_path.exists())
        self.assertFalse(self.shm_path.exists())
        self.assertFalse(self.export_path.exists())

        # Unknown files and folders in data/ strictly preserved!
        self.assertTrue(self.unknown_file.exists())
        self.assertEqual(self.unknown_file.read_text(), "IMPORTANT_USER_NOTES")
        self.assertTrue(self.unknown_subdir.exists())
        self.assertTrue((self.unknown_subdir / "subfile.txt").exists())

        # Source code and virtualenv preserved
        self.assertTrue((self.project_root / ".venv").exists())
        self.assertTrue((self.project_root / "dist").exists())

    def test_symlinked_root_or_ancestor_rejected(self) -> None:
        """Symlinked project root must be rejected immediately."""
        real_root = self.test_dir / "RealProject"
        real_root.mkdir()
        sym_root = self.test_dir / "SymProject"
        try:
            sym_root.symlink_to(real_root)
        except OSError:
            self.skipTest("Symlinks not supported")

        with self.assertRaises(SafetyBoundaryError):
            CyberHealthUninstaller(project_root=sym_root)

    def test_symlinked_db_rejected(self) -> None:
        """Symlinked DB path must be rejected."""
        real_db = self.test_dir / "real.db"
        real_db.write_bytes(b"DATA")
        sym_db = self.data_dir / "sym.db"
        try:
            sym_db.symlink_to(real_db)
        except OSError:
            self.skipTest("Symlinks not supported")

        with self.assertRaises(PurgeValidationError):
            CyberHealthUninstaller(project_root=self.project_root, db_path=sym_db)

    def test_path_escape_traversal_rejected(self) -> None:
        """Path traversal escaping project boundaries must be rejected."""
        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
        )
        outside_path = self.test_dir / "outside.sqlite3"
        with self.assertRaises(PurgeValidationError):
            uninstaller.validate_purge_candidate(outside_path)

        traversal = self.project_root / "data" / ".." / ".." / "outside.sqlite3"
        with self.assertRaises(PurgeValidationError):
            uninstaller.validate_purge_candidate(traversal)


class TestIdempotencyAndObsidianNonInterference(BaseFakeHostTest):
    def test_idempotency_second_run_is_clean_noop(self) -> None:
        """Second execution when integrations are absent reports success no-op."""
        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": str(self.project_root / ".venv/bin/python"),
                    "args": ["-m", "cyber_health_mcp"],
                    "cwd": str(self.project_root),
                }
            }
        })
        self.create_launchagent_plist(
            DEFAULT_LAUNCHAGENT_LABEL,
            [str(self.project_root / ".venv/bin/cyber-health-mcp")],
        )

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
        )

        # Run 1
        r1 = uninstaller.run()
        self.assertTrue(r1.success)

        # Run 2: integrations are now missing -> idempotent no-op success
        r2 = uninstaller.run()
        self.assertTrue(r2.success)
        self.assertEqual(r2.openclaw.action, "none")
        self.assertEqual(r2.launchagent.action, "none")

    def test_obsidian_memory_and_vaults_never_touched(self) -> None:
        """obsidian-memory and Obsidian Vaults are protected and untouched."""
        vault_dir = self.test_dir / "ObsidianVault"
        vault_dir.mkdir()
        (vault_dir / "note.md").write_text("# Obsidian Note")
        (vault_dir / ".obsidian").mkdir()

        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": str(self.project_root / ".venv/bin/python"),
                    "args": ["-m", "cyber_health_mcp"],
                    "cwd": str(self.project_root),
                },
                "obsidian-memory": {
                    "command": "node",
                    "args": ["/path/to/obsidian-memory"],
                },
            }
        })

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
            purge_data=True,
            confirm_purge=PURGE_CONFIRMATION_TOKEN,
        )

        report = uninstaller.run()
        self.assertTrue(report.success)

        # obsidian-memory is preserved
        state = self.get_fake_openclaw_state()
        self.assertIn("obsidian-memory", state["servers"])
        self.assertNotIn(FIXED_OPENCLAW_SERVER_NAME, state["servers"])

        # Vault files untouched
        self.assertTrue((vault_dir / "note.md").exists())
        self.assertEqual((vault_dir / "note.md").read_text(), "# Obsidian Note")

        # Attempt to target vault in candidate raises SafetyBoundaryError
        with self.assertRaises(SafetyBoundaryError):
            uninstaller.validate_purge_candidate(vault_dir / "note.md")


class TestCLIEntrypoint(BaseFakeHostTest):
    def test_cli_autodetects_openclaw_when_override_is_omitted(self) -> None:
        """The packaged CLI must inspect OpenClaw by default, not silently disable it."""
        with mock.patch(
            "cyber_health.uninstall.shutil.which",
            return_value=str(self.fake_openclaw_bin),
        ):
            code = main([
                "--dry-run",
                "--json",
                "--project-root", str(self.project_root),
                "--db", str(self.db_path),
                "--launchagent-dir", str(self.launchagent_dir),
            ])

        self.assertEqual(code, 0)
        self.assertIn(
            ["mcp", "show", FIXED_OPENCLAW_SERVER_NAME, "--json"],
            self.get_fake_openclaw_calls(),
        )

    def test_cli_dry_run_json(self) -> None:
        """CLI main function returns 0 with valid JSON report."""
        code = main([
            "--dry-run",
            "--json",
            "--project-root", str(self.project_root),
            "--db", str(self.db_path),
            "--openclaw-bin", str(self.fake_openclaw_bin),
            "--launchagent-dir", str(self.launchagent_dir),
        ])
        self.assertEqual(code, 0)

    def test_cli_purge_missing_token_fails(self) -> None:
        """CLI main function exits non-zero if purge token is omitted."""
        code = main([
            "--purge-data",
            "--project-root", str(self.project_root),
            "--db", str(self.db_path),
            "--openclaw-bin", str(self.fake_openclaw_bin),
            "--launchagent-dir", str(self.launchagent_dir),
        ])
        self.assertEqual(code, 1)


class TestLiveOpenClawProbe(unittest.TestCase):
    def test_live_openclaw_cli_in_sandbox_if_available(self) -> None:
        """Opt-in smoke test with real openclaw binary using sandbox config without touching host state."""
        real_openclaw = shutil.which("openclaw")
        if not real_openclaw:
            self.skipTest("openclaw CLI not found in PATH")

        with tempfile.TemporaryDirectory(prefix="cyber-health-live-probe-") as td:
            sandbox = Path(td).resolve()
            proj_root = sandbox / "CyberHealthProject"
            proj_root.mkdir()
            (proj_root / "pyproject.toml").write_text("[project]\nname='cyber-health-agent'\n")
            (proj_root / ".venv").mkdir()
            db_file = proj_root / "data" / "cyber-health.sqlite3"
            db_file.parent.mkdir(parents=True, exist_ok=True)
            db_file.write_bytes(b"DATA")

            cfg_path = sandbox / "openclaw.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "commands": {"native": "auto", "restart": True},
                        "gateway": {"mode": "local", "port": 18080},
                        "mcp": {
                            "servers": {
                                FIXED_OPENCLAW_SERVER_NAME: {
                                    "command": str(proj_root / ".venv/bin/python"),
                                    "args": ["-m", "cyber_health_mcp"],
                                    "cwd": str(proj_root),
                                    "env": {"CYBER_HEALTH_DB": str(db_file)},
                                },
                                "system-monitor": {
                                    "command": "python3",
                                    "args": ["-m", "http.server"],
                                    "cwd": "/usr/local/bin",
                                },
                                "local-tool": {
                                    "command": "node",
                                    "args": ["/usr/local/bin/tool.js"],
                                },
                            },
                        },
                    },
                    indent=2,
                )
            )

            uninstaller = CyberHealthUninstaller(
                project_root=proj_root,
                db_path=db_file,
                openclaw_bin=real_openclaw,
                openclaw_config=cfg_path,
                openclaw_state_dir=sandbox / "state",
                launchagent_dir=sandbox / "LaunchAgents",
                dry_run=False,
            )

            report = uninstaller.run()
            self.assertTrue(report.success)
            self.assertTrue(report.openclaw.executed)

            # Check config in sandbox
            updated_cfg = json.loads(cfg_path.read_text())
            servers = updated_cfg.get("mcp", {}).get("servers", {})
            self.assertNotIn(FIXED_OPENCLAW_SERVER_NAME, servers)
            self.assertIn("system-monitor", servers)


class TestTOCTOUDefense(BaseFakeHostTest):
    def test_post_plan_db_symlink_swap_fails_closed(self) -> None:
        """If a pre-planned database file is swapped for a symlink prior to execution, purge aborts with zero mutation."""
        outside_target = self.test_dir / "outside_secret.txt"
        outside_target.write_text("SENSITIVE_OUTSIDE_DATA")

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
            purge_data=True,
            confirm_purge=PURGE_CONFIRMATION_TOKEN,
        )

        # Plan phase
        data_plan = uninstaller.plan_data()
        self.assertTrue(data_plan.purge_confirmed)
        self.assertIn(str(self.db_path.resolve()), data_plan.purged_paths)

        # Adversary swaps db_path to a symlink targeting outside_target before execute_data_purge
        self.db_path.unlink()
        self.db_path.symlink_to(outside_target)

        with self.assertRaises(PurgeValidationError):
            uninstaller.execute_data_purge(data_plan)

        # Target outside file was untouched
        self.assertTrue(outside_target.exists())
        self.assertEqual(outside_target.read_text(), "SENSITIVE_OUTSIDE_DATA")

    def test_post_plan_db_inode_swap_fails_closed(self) -> None:
        """If a pre-planned file is replaced with a new file (different inode) before execution, purge fails closed."""
        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
            purge_data=True,
            confirm_purge=PURGE_CONFIRMATION_TOKEN,
        )

        # Plan phase
        data_plan = uninstaller.plan_data()
        self.assertTrue(data_plan.purge_confirmed)

        # Adversary deletes the original file and replaces it with a new file (new inode)
        self.db_path.unlink()
        self.db_path.write_bytes(b"REPLACED_AFTER_PLAN")

        with self.assertRaises(PurgeValidationError):
            uninstaller.execute_data_purge(data_plan)

        # File is preserved (not purged)
        self.assertTrue(self.db_path.exists())
        self.assertEqual(self.db_path.read_bytes(), b"REPLACED_AFTER_PLAN")

    def test_post_plan_openclaw_replacement_fails_closed(self) -> None:
        """If OpenClaw registration is swapped for a foreign server after planning, unset fails closed."""
        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": str(self.project_root / ".venv/bin/python"),
                    "args": ["-m", "cyber_health_mcp"],
                    "cwd": str(self.project_root),
                    "env": {"CYBER_HEALTH_DB": str(self.db_path)},
                }
            }
        })

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
        )

        status = uninstaller.inspect_openclaw()
        self.assertTrue(status.ownership_proven)
        self.assertEqual(status.action, "unset")

        # Adversary swaps the server in OpenClaw config to a foreign server after planning
        self.set_fake_openclaw_state({
            "servers": {
                FIXED_OPENCLAW_SERVER_NAME: {
                    "command": "/usr/local/bin/foreign-daemon",
                    "args": [],
                    "cwd": "/opt/foreign",
                    "env": {"CYBER_HEALTH_DB": "/var/foreign.db"},
                }
            }
        })

        with self.assertRaises(OwnershipVerificationError):
            uninstaller.unregister_openclaw(status)

        # Foreign server must NOT have been removed
        state = self.get_fake_openclaw_state()
        self.assertIn(FIXED_OPENCLAW_SERVER_NAME, state["servers"])
        self.assertEqual(state["servers"][FIXED_OPENCLAW_SERVER_NAME]["cwd"], "/opt/foreign")

    def test_post_plan_openclaw_owned_configuration_change_fails_closed(self) -> None:
        """A different but still-owned registration must not pass an exact-state recheck."""
        original = {
            "command": str(self.project_root / ".venv/bin/python"),
            "args": ["-m", "cyber_health_mcp"],
            "cwd": str(self.project_root),
            "env": {"CYBER_HEALTH_DB": str(self.db_path)},
        }
        self.set_fake_openclaw_state({"servers": {FIXED_OPENCLAW_SERVER_NAME: original}})
        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
        )
        status = uninstaller.inspect_openclaw()

        changed = dict(original)
        changed["args"] = ["-m", "cyber_health_mcp", "--replacement"]
        self.set_fake_openclaw_state({"servers": {FIXED_OPENCLAW_SERVER_NAME: changed}})

        with self.assertRaises(OwnershipVerificationError):
            uninstaller.unregister_openclaw(status)
        self.assertIn(FIXED_OPENCLAW_SERVER_NAME, self.get_fake_openclaw_state()["servers"])

    def test_post_plan_launchagent_replacement_fails_closed(self) -> None:
        """If LaunchAgent plist is swapped to foreign content after planning, removal fails closed."""
        plist_path = self.create_launchagent_plist(
            DEFAULT_LAUNCHAGENT_LABEL,
            [str(self.project_root / ".venv/bin/python"), "-m", "cyber_health_mcp"],
        )

        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
        )

        status = uninstaller.inspect_launchagent()
        self.assertTrue(status.ownership_proven)
        self.assertEqual(status.action, "unload_and_remove")

        # Adversary swaps plist content to foreign ownership
        foreign_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{DEFAULT_LAUNCHAGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/foreign-daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/opt/foreign</string>
</dict>
</plist>
"""
        plist_path.write_text(foreign_content)

        with self.assertRaises(OwnershipVerificationError):
            uninstaller.remove_launchagent(status)

        # File is preserved
        self.assertTrue(plist_path.exists())

    def test_post_plan_launchagent_owned_configuration_change_fails_closed(self) -> None:
        """A changed plist is refused even when its replacement still points at this project."""
        plist_path = self.create_launchagent_plist(
            DEFAULT_LAUNCHAGENT_LABEL,
            [str(self.project_root / ".venv/bin/python"), "-m", "cyber_health_mcp"],
        )
        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
        )
        status = uninstaller.inspect_launchagent()

        with open(plist_path, "rb") as f:
            changed = plistlib.load(f)
        changed["EnvironmentVariables"] = {"SAFE_REPLACEMENT": "1"}
        with open(plist_path, "wb") as f:
            plistlib.dump(changed, f)

        with self.assertRaises(OwnershipVerificationError):
            uninstaller.remove_launchagent(status)
        self.assertTrue(plist_path.exists())

    def test_global_preflight_prevents_earlier_host_mutation(self) -> None:
        """A later plan mismatch is caught before an earlier integration is unregistered."""
        owned = {
            "command": str(self.project_root / ".venv/bin/python"),
            "args": ["-m", "cyber_health_mcp"],
            "cwd": str(self.project_root),
            "env": {"CYBER_HEALTH_DB": str(self.db_path)},
        }
        self.set_fake_openclaw_state({"servers": {FIXED_OPENCLAW_SERVER_NAME: owned}})
        plist_path = self.create_launchagent_plist(
            DEFAULT_LAUNCHAGENT_LABEL,
            [str(self.project_root / ".venv/bin/python"), "-m", "cyber_health_mcp"],
        )
        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=str(self.fake_openclaw_bin),
            launchagent_dir=self.launchagent_dir,
        )
        planned_launchagent = uninstaller.inspect_launchagent()
        with open(plist_path, "rb") as f:
            changed = plistlib.load(f)
        changed["EnvironmentVariables"] = {"CHANGED_AFTER_PLAN": "1"}
        with open(plist_path, "wb") as f:
            plistlib.dump(changed, f)
        changed_launchagent = uninstaller.inspect_launchagent()

        with mock.patch.object(
            uninstaller,
            "inspect_launchagent",
            side_effect=[planned_launchagent, changed_launchagent],
        ):
            with self.assertRaises(OwnershipVerificationError):
                uninstaller.run()

        self.assertIn(FIXED_OPENCLAW_SERVER_NAME, self.get_fake_openclaw_state()["servers"])
        self.assertTrue(plist_path.exists())


class TestMissingHostInspectorWithPurge(BaseFakeHostTest):
    def test_missing_openclaw_bin_with_purge_refused(self) -> None:
        """When OpenClaw CLI is missing, --purge-data is refused and zero data is deleted."""
        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=None,
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
            purge_data=True,
            confirm_purge=PURGE_CONFIRMATION_TOKEN,
        )

        report = uninstaller.run()
        self.assertFalse(report.success)
        self.assertTrue(any("OpenClaw CLI is not available" in err for err in report.data.errors))

        # Assert ZERO data mutation
        self.assertTrue(self.db_path.exists())
        self.assertTrue((self.project_root / "data" / "cyber-health.sqlite3-wal").exists())
        self.assertTrue((self.project_root / "data" / "cyber-health.sqlite3-shm").exists())
        self.assertTrue((self.project_root / "data" / "export_2026.json").exists())

    def test_missing_openclaw_bin_without_purge_is_harmless(self) -> None:
        """When OpenClaw CLI is missing and purge is NOT requested, normal uninstall succeeds safely."""
        uninstaller = CyberHealthUninstaller(
            project_root=self.project_root,
            db_path=self.db_path,
            openclaw_bin=None,
            launchagent_dir=self.launchagent_dir,
            dry_run=False,
            purge_data=False,
        )

        report = uninstaller.run()
        self.assertTrue(report.success)
        self.assertIn("OpenClaw binary not found", report.openclaw.reason)
        # All data preserved
        self.assertTrue(self.db_path.exists())


class TestFixedLaunchAgentLabel(BaseFakeHostTest):
    def test_custom_launchagent_label_forbidden(self) -> None:
        """Attempting to initialize uninstaller with non-standard launchagent label raises SafetyBoundaryError."""
        with self.assertRaises(SafetyBoundaryError):
            CyberHealthUninstaller(
                project_root=self.project_root,
                db_path=self.db_path,
                openclaw_bin=str(self.fake_openclaw_bin),
                launchagent_dir=self.launchagent_dir,
                launchagent_label="com.apple.loginwindow",
            )


class TestTrashDestinationSafety(BaseFakeHostTest):
    def test_local_trash_symlink_rejected(self) -> None:
        """If project-local .trash fallback is a symlink or contains symlinks, it is rejected and purge fails closed."""
        fake_home = self.test_dir / "fake_nonwritable_home"
        fake_home.mkdir()
        fake_trash = fake_home / ".Trash"
        fake_trash.mkdir()
        # Make ~/.Trash non-writable to trigger project-local fallback
        os.chmod(fake_trash, 0o444)

        # Create a symlinked .trash inside project root pointing outside
        outside_trash = self.test_dir / "outside_trash"
        outside_trash.mkdir()
        local_trash = self.project_root / ".trash"
        local_trash.symlink_to(outside_trash)

        with mock.patch("pathlib.Path.home", return_value=fake_home):
            uninstaller = CyberHealthUninstaller(
                project_root=self.project_root,
                db_path=self.db_path,
                openclaw_bin=str(self.fake_openclaw_bin),
                launchagent_dir=self.launchagent_dir,
                dry_run=False,
                purge_data=True,
                confirm_purge=PURGE_CONFIRMATION_TOKEN,
            )

            # Execution of purge should fail closed due to symlinked trash destination
            plan = uninstaller.plan_data()
            try:
                with self.assertRaises(PurgeValidationError):
                    uninstaller.execute_data_purge(plan)
            finally:
                # Restore permissions so cleanup works
                os.chmod(fake_trash, 0o755)


if __name__ == "__main__":
    unittest.main()
