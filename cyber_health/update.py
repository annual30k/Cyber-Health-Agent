"""Production-safe updater for Cyber Health Agent.

Updates Cyber Health Agent in ~/.cyber-health (or custom target directory),
creates an atomic timestamped database snapshot backup with SHA256 and integrity checks,
upgrades the package and dependencies, runs SQLite schema verification,
and refreshes supported host MCP registrations.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

from .install import (
    FIXED_OPENCLAW_SERVER_NAME,
    DEFAULT_INSTALL_DIR_NAME,
    PROTECTED_NAMES,
    SYSTEM_BROAD_PATHS,
    compute_sha256,
    atomic_write_text,
    get_executable_name,
    get_venv_bin_dir,
    has_symlink_in_path,
    is_system_broad_or_drive_root,
    safe_checkpoint_db,
    verify_sqlite_integrity,
)
from .service import CyberHealthService
from .uninstall import verify_cyber_health_command_signature
from .health_memory import HealthManagerMemoryStatus, inspect_health_manager_memory
from .obsidian_memory_provider import ObsidianMemoryProvider
from .codex_integration import apply_codex_registration, find_codex_cli, plan_codex_registration

_DEFAULT_BIN = object()


class UpdaterError(Exception):
    """Base exception for updater errors."""


class UpdateBackupError(UpdaterError):
    """Raised when creating an automated snapshot backup fails."""


@dataclass
class BackupStatus:
    created: bool = False
    source_db: str = ""
    backup_file: str = ""
    sha256: str = ""
    reason: str = ""


@dataclass
class UpdateReport:
    dry_run: bool
    success: bool
    target_dir: str
    old_version: str
    new_version: str
    backup: BackupStatus
    memory: HealthManagerMemoryStatus = field(default_factory=HealthManagerMemoryStatus)
    package_updated: bool = False
    schema_verified: bool = False
    openclaw_verified: bool = False
    codex_verified: bool = False
    message: str = ""
    protected_boundaries: dict[str, bool] = field(
        default_factory=lambda: {
            "obsidian_memory_preserved": True,
            "obsidian_vaults_preserved": True,
            "unrelated_codex_state_preserved": True,
            "source_repo_preserved": True,
        }
    )


class CyberHealthUpdater:
    def __init__(
        self,
        project_root: Path | str | None = None,
        target_dir: Path | str | None = None,
        openclaw_bin: str | None | object = _DEFAULT_BIN,
        openclaw_config: Path | str | None = None,
        openclaw_state_dir: Path | str | None = None,
        codex_bin: str | None | object = _DEFAULT_BIN,
        codex_home: Path | str | None = None,
        dry_run: bool = False,
        use_uv: bool = True,
    ):
        if project_root is None:
            self._raw_project_root = Path(__file__).resolve().parents[1]
        else:
            self._raw_project_root = Path(project_root)

        if has_symlink_in_path(self._raw_project_root):
            raise UpdaterError(f"Symlinked source project root rejected: {self._raw_project_root}")

        self.project_root = self._raw_project_root.resolve()

        if target_dir is None:
            self._raw_target_dir = Path.home() / DEFAULT_INSTALL_DIR_NAME
        else:
            self._raw_target_dir = Path(target_dir)

        if has_symlink_in_path(self._raw_target_dir):
            raise UpdaterError(f"Symlinked target directory rejected: {self._raw_target_dir}")

        self.target_dir = self._raw_target_dir.resolve()
        self._validate_target_dir(self.target_dir)

        self.venv_dir = self.target_dir / "venv"
        self.data_dir = self.target_dir / "data"
        self.backups_dir = self.data_dir / "backups"
        self.config_dir = self.target_dir / "config"
        self.target_db_path = self.data_dir / "cyber-health.sqlite3"

        if openclaw_bin is _DEFAULT_BIN:
            self.openclaw_bin = shutil.which("openclaw")
        else:
            self.openclaw_bin = str(openclaw_bin) if openclaw_bin else None
        self.openclaw_config = Path(openclaw_config) if openclaw_config else None
        self.openclaw_state_dir = Path(openclaw_state_dir) if openclaw_state_dir else None
        if codex_bin is _DEFAULT_BIN:
            self.codex_bin = find_codex_cli()
        else:
            self.codex_bin = str(codex_bin) if codex_bin else None
        self.codex_home = Path(codex_home) if codex_home else None

        self.dry_run = dry_run
        self.use_uv = use_uv
        self.new_version = self._detect_source_version()
        self.memory_status = HealthManagerMemoryStatus()

    def _validate_target_dir(self, target: Path) -> None:
        if is_system_broad_or_drive_root(target):
            raise UpdaterError(f"Broad or system target directory rejected: {target}")

        target_str_lower = str(target).lower()
        for protected in PROTECTED_NAMES:
            if protected in target_str_lower:
                raise UpdaterError(
                    f"Target directory contains protected keyword '{protected}': {target}"
                )

    def _detect_source_version(self) -> str:
        pyproject = self.project_root / "pyproject.toml"
        if pyproject.exists():
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("version =") or line.startswith("version="):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"').strip("'")
        return "0.2.6"

    def get_openclaw_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.openclaw_config:
            env["OPENCLAW_CONFIG_PATH"] = str(self.openclaw_config)
        if self.openclaw_state_dir:
            env["OPENCLAW_STATE_DIR"] = str(self.openclaw_state_dir)
        return env

    def inspect_installation(self) -> dict[str, Any]:
        """Reads metadata from existing installation."""
        if not self.target_dir.exists():
            raise UpdaterError(f"Target installation directory does not exist: {self.target_dir}")

        meta_file = self.config_dir / "installation.json"
        if meta_file.exists():
            try:
                return json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        return {
            "version": "unknown",
            "installed_at": "unknown",
            "target_dir": str(self.target_dir),
            "db_path": str(self.target_db_path),
        }

    def create_database_snapshot(self) -> BackupStatus:
        """Creates a timestamped snapshot of current production database with SHA256 and integrity check."""
        status = BackupStatus(source_db=str(self.target_db_path))

        if not self.target_db_path.exists():
            status.reason = "No existing database found to backup (clean state)"
            return status

        if not self.target_db_path.is_file() or self.target_db_path.is_symlink():
            raise UpdateBackupError(f"Target database is not a regular file: {self.target_db_path}")

        # Checkpoint WAL to flush all active transactions before backup.  Do not
        # copy the main file if checkpointing failed, or committed WAL pages could
        # be silently omitted from the snapshot.
        checkpoint_ok, checkpoint_msg = safe_checkpoint_db(self.target_db_path)
        if not checkpoint_ok:
            raise UpdateBackupError(f"Active database WAL checkpoint failed: {checkpoint_msg}")

        ok, msg = verify_sqlite_integrity(self.target_db_path)
        if not ok:
            raise UpdateBackupError(f"Active database failed integrity check before backup: {msg}")

        source_sha = compute_sha256(self.target_db_path)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_file = self.backups_dir / f"cyber-health-backup-{ts}.sqlite3"
        backup_wal = backup_file.parent / (backup_file.name + "-wal")
        backup_shm = backup_file.parent / (backup_file.name + "-shm")
        status.backup_file = str(backup_file)

        if self.dry_run:
            status.created = False
            status.sha256 = source_sha
            status.reason = f"Dry run: planned backup to {backup_file.name}"
            return status

        self.backups_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.target_db_path, backup_file)

        backup_sha = compute_sha256(backup_file)
        if backup_sha != source_sha:
            for f in (backup_file, backup_wal, backup_shm):
                if f.exists():
                    try:
                        f.unlink()
                    except Exception:
                        pass
            raise UpdateBackupError(f"Backup checksum mismatch! Source: {source_sha}, Backup: {backup_sha}")

        ok, msg = verify_sqlite_integrity(backup_file)
        if not ok:
            for f in (backup_file, backup_wal, backup_shm):
                if f.exists():
                    try:
                        f.unlink()
                    except Exception:
                        pass
            raise UpdateBackupError(f"Backup failed SQLite integrity check: {msg}")

        # Clean up any sidecars created during the integrity check
        checkpoint_ok, checkpoint_msg = safe_checkpoint_db(backup_file)
        if not checkpoint_ok:
            for f in (backup_file, backup_wal, backup_shm):
                if f.exists():
                    try:
                        f.unlink()
                    except Exception:
                        pass
            raise UpdateBackupError(f"Backup WAL checkpoint failed: {checkpoint_msg}")
        for sc in (backup_wal, backup_shm):
            if sc.exists():
                try:
                    sc.unlink()
                except Exception:
                    pass

        status.created = True
        status.sha256 = backup_sha
        status.reason = f"Successfully created verified snapshot backup at {backup_file.name}"
        return status

    def upgrade_package(self) -> bool:
        """Upgrades the package in target venv using the source repository."""
        if self.dry_run:
            return True

        uv_bin = shutil.which("uv") if self.use_uv else None
        venv_python = get_venv_bin_dir(self.venv_dir) / get_executable_name("python")

        if not venv_python.exists():
            raise UpdaterError(f"Virtual environment python executable not found: {venv_python}")

        if uv_bin:
            cmd = [uv_bin, "pip", "install", "--upgrade", str(self.project_root), "--python", str(venv_python)]
        else:
            cmd = [str(venv_python), "-m", "pip", "install", "--upgrade", str(self.project_root)]

        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise UpdaterError(f"Failed to upgrade package in virtual environment: {res.stderr.strip()}")

        return True

    def verify_schema_and_service(self) -> bool:
        """Initializes CyberHealthService on production DB to verify/apply schema updates."""
        if self.dry_run:
            return True

        if not self.target_db_path.exists():
            return True

        try:
            provider = None
            if self.memory_status.connected:
                provider = ObsidianMemoryProvider(
                    self.memory_status.vault_path or "",
                    self.memory_status.project_id or "",
                )
            service = CyberHealthService(self.target_db_path, memory_provider=provider)
            # Run read check
            service.get_today(user_id="probe_check", day="2026-09-09")
            if self.memory_status.connected and service.health_check()["components"]["memory_provider"] != "ok":
                raise UpdaterError("Configured health-manager Obsidian Memory provider did not respond to ping")
            return True
        except Exception as exc:
            raise UpdaterError(f"Post-update schema verification failed: {exc}")

    def verify_openclaw(self) -> bool:
        """Verifies OpenClaw MCP registration for cyber-health with strict fail-closed semantics."""
        if self.dry_run:
            return True

        if not self.openclaw_bin:
            return True

        cmd = [self.openclaw_bin, "mcp", "show", FIXED_OPENCLAW_SERVER_NAME, "--json"]
        try:
            res = subprocess.run(
                cmd,
                env=self.get_openclaw_env(),
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
            )
        except Exception:
            return False

        target_mcp = get_venv_bin_dir(self.venv_dir) / get_executable_name("cyber-health-mcp")
        expected_args = ["--db", str(self.target_db_path), "--allow-all", *self.memory_status.provider_args]
        mcp_payload = json.dumps(
            {
                "command": str(target_mcp),
                "args": expected_args,
                "cwd": str(self.target_dir),
                "connectionTimeoutMs": 20000,
                "requestTimeoutMs": 30000,
            }
        )

        if res.returncode == 0:
            try:
                raw_data = json.loads(res.stdout)
            except Exception:
                # Malformed JSON in stdout: fail closed, do NOT overwrite
                return False

            if not isinstance(raw_data, dict):
                return False

            cmd_val = raw_data.get("command")
            args_val = raw_data.get("args")
            if not isinstance(args_val, list):
                args_val = []

            # Check command signature: if foreign registration, strictly fail closed
            if not verify_cyber_health_command_signature(cmd_val, args_val):
                return False

            current_cmd = str(cmd_val or "")
            current_cwd = str(raw_data.get("cwd") or "")
            current_args = [str(a) for a in args_val]
            if (
                current_cmd == str(target_mcp)
                and current_args == expected_args
                and current_cwd == str(self.target_dir)
            ):
                return True

            # Existing Cyber Health registration pointing to outdated binary or path: refresh it
            set_cmd = [self.openclaw_bin, "mcp", "set", FIXED_OPENCLAW_SERVER_NAME, mcp_payload]
            try:
                set_res = subprocess.run(
                    set_cmd,
                    env=self.get_openclaw_env(),
                    capture_output=True,
                    text=True,
                    timeout=20,
                    shell=False,
                )
                return set_res.returncode == 0
            except Exception:
                return False

        # Non-zero returncode: inspect combined output
        combined_output = (res.stderr + " " + res.stdout).strip()
        is_absent = (
            'No MCP server named "cyber-health"' in combined_output
            or 'No MCP server named \\"cyber-health\\"' in combined_output
            or "No MCP server named 'cyber-health'" in combined_output
        )
        if not is_absent:
            # Failed due to CLI crash, permission denied, or other unexpected issue:
            # strictly fail closed, do NOT overwrite or attempt set
            return False

        # Genuinely confirmed absent: register it
        set_cmd = [self.openclaw_bin, "mcp", "set", FIXED_OPENCLAW_SERVER_NAME, mcp_payload]
        try:
            set_res = subprocess.run(
                set_cmd,
                env=self.get_openclaw_env(),
                capture_output=True,
                text=True,
                timeout=20,
                shell=False,
            )
            return set_res.returncode == 0
        except Exception:
            return False

    def verify_codex(self) -> bool:
        """Verify or refresh the single owned Codex MCP registration."""
        target_mcp = get_venv_bin_dir(self.venv_dir) / get_executable_name("cyber-health-mcp")
        expected_args = ["--db", str(self.target_db_path), "--allow-all", *self.memory_status.provider_args]
        status = plan_codex_registration(
            self.codex_bin,
            self.target_dir,
            self.target_db_path,
            str(target_mcp),
            expected_args,
            "",
            codex_home=self.codex_home,
        )
        if status.action in ("error", "refused"):
            return False
        try:
            apply_codex_registration(
                self.codex_bin,
                status,
                codex_home=self.codex_home,
                dry_run=self.dry_run,
            )
            return True
        except Exception:
            return False

    def update_metadata(self, old_meta: dict[str, Any], backup_status: BackupStatus) -> None:
        """Updates config/installation.json with new version details."""
        if self.dry_run:
            return

        old_meta["version"] = self.new_version
        old_meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        if backup_status.backup_file:
            old_meta["last_backup"] = backup_status.backup_file
        old_meta["memory"] = self.memory_status.to_dict()
        old_meta["codex"] = {
            "name": "cyber-health",
            "registered": bool(self.codex_bin),
        }

        meta_file = self.config_dir / "installation.json"
        atomic_write_text(meta_file, json.dumps(old_meta, indent=2))

    def run(self) -> UpdateReport:
        old_meta = self.inspect_installation()
        old_version = old_meta.get("version", "unknown")
        backup_status = BackupStatus(source_db=str(self.target_db_path))
        if self.openclaw_bin:
            self.memory_status = inspect_health_manager_memory(
                self.openclaw_bin,
                self.get_openclaw_env(),
            )
        else:
            self.memory_status = HealthManagerMemoryStatus(
                reason="OpenClaw executable was not found; health-manager long-term memory was not inspected.",
                warnings=["OpenClaw executable was not found; health-manager long-term memory was not inspected."],
            )

        try:
            backup_status = self.create_database_snapshot()
            pkg_updated = self.upgrade_package()

            schema_ok = self.verify_schema_and_service()
            if not schema_ok:
                return UpdateReport(
                    dry_run=self.dry_run,
                    success=False,
                    target_dir=str(self.target_dir),
                    old_version=old_version,
                    new_version=self.new_version,
                    backup=backup_status,
                    memory=self.memory_status,
                    package_updated=pkg_updated,
                    schema_verified=False,
                    openclaw_verified=False,
                    message=(
                        "Update aborted (fail-closed): SQLite schema verification failed. "
                        "The package may already have been upgraded; the verified database "
                        f"backup is available at {backup_status.backup_file or 'the backup directory'}."
                    ),
                )

            openclaw_ok = self.verify_openclaw()
            if not openclaw_ok:
                return UpdateReport(
                    dry_run=self.dry_run,
                    success=False,
                    target_dir=str(self.target_dir),
                    old_version=old_version,
                    new_version=self.new_version,
                    backup=backup_status,
                    memory=self.memory_status,
                    package_updated=pkg_updated,
                    schema_verified=schema_ok,
                    openclaw_verified=False,
                    message=(
                        "Update aborted (fail-closed): OpenClaw registration verification failed. "
                        "The package may already have been upgraded; the verified database "
                        f"backup is available at {backup_status.backup_file or 'the backup directory'}."
                    ),
                )

            codex_ok = self.verify_codex()
            if not codex_ok:
                return UpdateReport(
                    dry_run=self.dry_run,
                    success=False,
                    target_dir=str(self.target_dir),
                    old_version=old_version,
                    new_version=self.new_version,
                    backup=backup_status,
                    memory=self.memory_status,
                    package_updated=pkg_updated,
                    schema_verified=schema_ok,
                    openclaw_verified=True,
                    codex_verified=False,
                    message=(
                        "Update aborted (fail-closed): Codex registration verification failed. "
                        "The package may already have been upgraded; the verified database "
                        f"backup is available at {backup_status.backup_file or 'the backup directory'}."
                    ),
                )

            self.update_metadata(old_meta, backup_status)

            return UpdateReport(
                dry_run=self.dry_run,
                success=True,
                target_dir=str(self.target_dir),
                old_version=old_version,
                new_version=self.new_version,
                backup=backup_status,
                memory=self.memory_status,
                package_updated=pkg_updated,
                schema_verified=True,
                openclaw_verified=True,
                codex_verified=True,
                message="Update completed successfully",
            )
        except Exception as exc:
            return UpdateReport(
                dry_run=self.dry_run,
                success=False,
                target_dir=str(self.target_dir),
                old_version=old_version,
                new_version=self.new_version,
                backup=backup_status if backup_status.created else BackupStatus(reason=f"Update failed: {exc}"),
                memory=self.memory_status,
                message=str(exc),
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cyber-health-update",
        description="Production-safe Cyber Health Agent updater.",
    )
    parser.add_argument(
        "--target-dir",
        type=str,
        default=None,
        help="Target installation directory (defaults to ~/.cyber-health).",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="Source project repository root override.",
    )
    parser.add_argument(
        "--openclaw-bin",
        type=str,
        default=_DEFAULT_BIN,
        help="OpenClaw CLI binary path override.",
    )
    parser.add_argument(
        "--openclaw-config",
        type=str,
        default=None,
        help="OpenClaw config path override.",
    )
    parser.add_argument(
        "--openclaw-state-dir",
        type=str,
        default=None,
        help="OpenClaw state directory override.",
    )
    parser.add_argument("--codex-bin", type=str, default=_DEFAULT_BIN, help="Codex CLI binary path override.")
    parser.add_argument("--codex-home", type=str, default=None, help="Codex home override (primarily for isolated testing).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform inspection and deterministic reporting without making changes.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output report in JSON format.",
    )
    parser.add_argument(
        "--no-uv",
        action="store_true",
        help="Do not use uv for virtualenv package installation.",
    )
    return parser


def format_text_report(report: UpdateReport) -> str:
    lines = [
        "============================================================",
        f"Cyber Health Agent Updater ({'DRY RUN' if report.dry_run else 'EXECUTE'})",
        "============================================================",
        f"Status       : {'SUCCESS' if report.success else 'FAILED'}",
        f"Target Dir   : {report.target_dir}",
        f"Old Version  : {report.old_version}",
        f"New Version  : {report.new_version}",
        f"Message      : {report.message}",
        "",
        "--- Database Snapshot ---",
        f"Backup Created: {report.backup.created}",
        f"Backup File   : {report.backup.backup_file or 'N/A'}",
        f"Backup SHA256 : {report.backup.sha256 or 'N/A'}",
        f"Backup Reason : {report.backup.reason}",
        "",
        "--- Verification ---",
        f"Package Upgraded : {report.package_updated}",
        f"Schema Verified  : {report.schema_verified}",
        f"OpenClaw Verified: {report.openclaw_verified}",
        f"Codex Verified   : {report.codex_verified}",
        "",
        "--- Health-Manager Long-Term Memory ---",
        f"State            : {report.memory.state}",
        f"Plugin           : {report.memory.plugin_id} (loaded={report.memory.plugin_loaded})",
        f"Vault            : {report.memory.vault_path or 'N/A'}",
        f"Project          : {report.memory.project_id or 'N/A'}",
        f"Reason           : {report.memory.reason}",
        *[f"Warning          : {warning}" for warning in report.memory.warnings],
        "",
        "--- Protected Boundaries ---",
        "  + obsidian-memory: STRICTLY PRESERVED (Untouched)",
        "  + Obsidian Vaults: STRICTLY PRESERVED (Untouched)",
        "  + Codex Config:    ONLY cyber-health MCP entry managed; unrelated state preserved",
        "  + Source Repo:     STRICTLY PRESERVED (Untouched)",
        "============================================================",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        updater = CyberHealthUpdater(
            project_root=args.project_root,
            target_dir=args.target_dir,
            openclaw_bin=args.openclaw_bin,
            openclaw_config=args.openclaw_config,
            openclaw_state_dir=args.openclaw_state_dir,
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            dry_run=args.dry_run,
            use_uv=not args.no_uv,
        )
        report = updater.run()
    except Exception as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "success": False,
                        "error": str(exc),
                        "type": exc.__class__.__name__,
                    },
                    indent=2,
                )
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print(format_text_report(report))

    return 0 if report.success else 1


if __name__ == "__main__":
    sys.exit(main())
