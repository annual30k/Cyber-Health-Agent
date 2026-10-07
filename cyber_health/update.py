"""Production-safe updater for Cyber Health Agent.

Updates Cyber Health Agent in ~/.cyber-health (or custom target directory),
creates an atomic timestamped database snapshot backup with SHA256 and integrity checks,
upgrades the package and dependencies, runs SQLite schema verification,
and refreshes supported host MCP registrations.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import typing
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .codex_integration import apply_codex_registration, find_codex_cli, plan_codex_registration
from .command_shim import USER_BIN_ENV, CommandShimStatus, command_shim_note, command_shim_report_lines, ensure_command_shim
from .core_release import CoreRelease, CoreReleaseError, CoreReleaseStatus, cache_core_release, resolve_latest_core_release
from .health_memory import HealthManagerMemoryStatus, inspect_health_manager_memory
from .hermes_integration import (
    apply_hermes_registration,
    find_hermes_cli,
    plan_hermes_registration,
)
from .housekeeping import DEFAULT_BACKUP_RETENTION, HousekeepingStatus, housekeeping_report_lines, make_private, run_housekeeping
from .install import (
    DEFAULT_INSTALL_DIR_NAME,
    FIXED_OPENCLAW_SERVER_NAME,
    PROTECTED_NAMES,
    atomic_write_text,
    compute_sha256,
    get_executable_name,
    get_venv_bin_dir,
    has_symlink_in_path,
    is_system_broad_or_drive_root,
    safe_checkpoint_db,
    verify_sqlite_integrity,
)
from .memory_plugin_release import (
    MemoryPluginReleaseError,
    MemoryPluginReleaseStatus,
    cache_memory_plugin_release,
    resolve_latest_memory_plugin_release,
)
from .obsidian_memory_provider import ObsidianMemoryProvider
from .service import CyberHealthService
from .uninstall import verify_cyber_health_command_signature
from .windows_launchers import move_aside_launchers, remove_stale_launchers, restore_launchers

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
    core_release: CoreReleaseStatus = field(default_factory=CoreReleaseStatus)
    memory_plugin: MemoryPluginReleaseStatus = field(default_factory=MemoryPluginReleaseStatus)
    package_updated: bool = False
    schema_verified: bool = False
    openclaw_verified: bool = False
    codex_verified: bool = False
    hermes_verified: bool = False
    command_shim: CommandShimStatus = field(default_factory=CommandShimStatus)
    housekeeping: HousekeepingStatus = field(default_factory=HousekeepingStatus)
    up_to_date: bool = False
    handed_off: bool = False
    finished_by: str = __version__
    message: str = ""
    protected_boundaries: dict[str, bool] = field(
        default_factory=lambda: {
            "obsidian_memory_preserved": True,
            "obsidian_vaults_preserved": True,
            "unrelated_codex_state_preserved": True,
            "unrelated_hermes_state_preserved": True,
            "source_repo_preserved": True,
        }
    )


class CyberHealthUpdater:
    def __init__(
        self,
        project_root: Path | str | None = None,
        target_dir: Path | str | None = None,
        openclaw_bin: str | object | None = _DEFAULT_BIN,
        openclaw_config: Path | str | None = None,
        openclaw_state_dir: Path | str | None = None,
        codex_bin: str | object | None = _DEFAULT_BIN,
        codex_home: Path | str | None = None,
        hermes_bin: str | object | None = _DEFAULT_BIN,
        hermes_home: Path | str | None = None,
        dry_run: bool = False,
        use_uv: bool = True,
        memory_plugin_release_resolver=resolve_latest_memory_plugin_release,
        core_release_resolver=resolve_latest_core_release,
        user_bin_dir: Path | str | None = None,
        force: bool = False,
        keep_backups: int = DEFAULT_BACKUP_RETENTION,
    ):
        self.release_mode = project_root is None
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
        self.plugins_dir = self.target_dir / "plugins"
        self.releases_dir = self.target_dir / "releases"
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
        if hermes_bin is _DEFAULT_BIN:
            self.hermes_bin = find_hermes_cli()
        else:
            self.hermes_bin = str(hermes_bin) if hermes_bin else None
        self.hermes_home = Path(hermes_home) if hermes_home else None

        self.dry_run = dry_run
        self.user_bin_dir = Path(user_bin_dir) if user_bin_dir is not None else None
        self.force = force
        self.keep_backups = keep_backups
        self.handoff_note = ""
        self.use_uv = use_uv
        self.new_version = self._detect_source_version()
        self.memory_status = HealthManagerMemoryStatus()
        self.memory_plugin_release_resolver = memory_plugin_release_resolver
        self.core_release_resolver = core_release_resolver
        self.core_release_status = CoreReleaseStatus()
        self.core_release = None
        self.memory_plugin_status = MemoryPluginReleaseStatus()

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
                if line.startswith(("version =", "version=")):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"').strip("'")
        # Release mode has no source tree: the running package is the installed version.
        return __version__

    def prepare_core_release(self) -> CoreReleaseStatus:
        if not self.release_mode:
            return CoreReleaseStatus(action="local", version=self.new_version, reason="Using the explicit local development source.")
        try:
            self.core_release = self.core_release_resolver()
            self.new_version = self.core_release.version
            return CoreReleaseStatus("planned", self.core_release.version, str(self.releases_dir / self.core_release.wheel_name), self.core_release.sha256, "Resolved the latest stable Core Release.")
        except CoreReleaseError as exc:
            return CoreReleaseStatus(action="error", reason=str(exc))

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
            except (OSError, ValueError):
                pass

        return {
            "version": "unknown",
            "installed_at": "unknown",
            "target_dir": str(self.target_dir),
            "db_path": str(self.target_db_path),
        }

    @staticmethod
    def memory_from_installation_metadata(metadata: dict[str, Any]) -> HealthManagerMemoryStatus:
        """Revalidate a previously consented host-neutral Vault binding.

        Updates have no ``--memory-vault`` prompt. They may therefore reuse only
        the explicit binding recorded by a successful installation, and only
        after the filesystem provider validates its current scope.
        """
        raw = metadata.get("memory")
        if not isinstance(raw, dict) or raw.get("state") != "connected":
            return HealthManagerMemoryStatus(reason="No previously connected long-term memory binding was recorded.")
        vault_path = raw.get("vault_path")
        project_id = raw.get("project_id")
        if not isinstance(vault_path, str) or not isinstance(project_id, str):
            return HealthManagerMemoryStatus(reason="Recorded long-term memory binding is incomplete.")
        try:
            provider = ObsidianMemoryProvider(vault_path, project_id)
        except Exception as exc:  # noqa: BLE001 - provider construction must degrade to an invalid memory status
            return HealthManagerMemoryStatus(
                state="invalid",
                vault_path=vault_path,
                project_id=project_id,
                reason=f"Recorded long-term memory binding no longer validates: {exc}",
            )
        return HealthManagerMemoryStatus(
            state="connected",
            plugin_loaded=False,
            vault_path=str(provider.vault_path),
            project_id=provider.project_id,
            project_path=str(provider.project_path),
            provider_args=[
                "--memory-provider", "obsidian",
                "--memory-vault", str(provider.vault_path),
                "--memory-project-id", provider.project_id,
            ],
            reason="Revalidated the previously consented Vault/project connection from installation metadata.",
        )

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
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
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
                    with contextlib.suppress(OSError):
                        f.unlink()
            raise UpdateBackupError(f"Backup checksum mismatch! Source: {source_sha}, Backup: {backup_sha}")

        ok, msg = verify_sqlite_integrity(backup_file)
        if not ok:
            for f in (backup_file, backup_wal, backup_shm):
                if f.exists():
                    with contextlib.suppress(OSError):
                        f.unlink()
            raise UpdateBackupError(f"Backup failed SQLite integrity check: {msg}")

        # Clean up any sidecars created during the integrity check
        checkpoint_ok, checkpoint_msg = safe_checkpoint_db(backup_file)
        if not checkpoint_ok:
            for f in (backup_file, backup_wal, backup_shm):
                if f.exists():
                    with contextlib.suppress(OSError):
                        f.unlink()
            raise UpdateBackupError(f"Backup WAL checkpoint failed: {checkpoint_msg}")
        for sc in (backup_wal, backup_shm):
            if sc.exists():
                with contextlib.suppress(OSError):
                    sc.unlink()

        make_private(backup_file)
        status.created = True
        status.sha256 = backup_sha
        status.reason = f"Successfully created verified snapshot backup at {backup_file.name}"
        return status

    def upgrade_package(self) -> bool:
        """Upgrades from a verified Core wheel, or explicit local dev source."""
        if self.dry_run:
            return True

        uv_bin = shutil.which("uv") if self.use_uv else None
        venv_python = get_venv_bin_dir(self.venv_dir) / get_executable_name("python")

        if not venv_python.exists():
            raise UpdaterError(f"Virtual environment python executable not found: {venv_python}")

        package_source: Path = self.project_root
        if self.release_mode:
            if self.core_release is None:
                raise UpdaterError("Core Release was not resolved before upgrade.")
            status = cache_core_release(self.core_release, self.releases_dir)
            self.core_release_status = status
            package_source = Path(status.wheel_path)
        if uv_bin:
            cmd = [uv_bin, "pip", "install", "--upgrade", str(package_source), "--python", str(venv_python)]
        else:
            cmd = [str(venv_python), "-m", "pip", "install", "--upgrade", str(package_source)]

        scripts_dir = get_venv_bin_dir(self.venv_dir)
        moved = move_aside_launchers(scripts_dir)  # Windows: the running launcher is locked
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except (OSError, subprocess.SubprocessError):
            restore_launchers(moved)
            raise
        if res.returncode != 0:
            restore_launchers(moved)
            raise UpdaterError(f"Failed to upgrade package in virtual environment: {res.stderr.strip()}")
        restore_launchers(moved)  # keeps any launcher the install did not recreate
        remove_stale_launchers(scripts_dir)
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
            service.get_today(day="2026-09-09")
            if self.memory_status.connected and service.health_check()["components"]["memory_provider"] != "ok":
                raise UpdaterError("Configured health-manager Obsidian Memory provider did not respond to ping")
            return True
        except Exception as exc:
            raise UpdaterError(f"Post-update schema verification failed: {exc}") from exc

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
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
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
            except (TypeError, ValueError):
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
                    check=False,
                )
                return set_res.returncode == 0
            except (OSError, subprocess.SubprocessError):
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
                check=False,
            )
            return set_res.returncode == 0
        except (OSError, subprocess.SubprocessError):
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
        except Exception:  # noqa: BLE001 - registration failure counts as unverified (fail-closed)
            return False

    def verify_hermes(self) -> bool:
        """Verify or refresh Hermes and require real tool discovery."""
        target_mcp = get_venv_bin_dir(self.venv_dir) / get_executable_name("cyber-health-mcp")
        expected_args = ["--db", str(self.target_db_path), "--allow-all", *self.memory_status.provider_args]
        status = plan_hermes_registration(
            self.hermes_bin,
            self.target_dir,
            self.target_db_path,
            str(target_mcp),
            expected_args,
            hermes_home=self.hermes_home,
        )
        if status.action in ("error", "refused"):
            return False
        try:
            apply_hermes_registration(
                self.hermes_bin,
                status,
                self.target_dir,
                self.target_db_path,
                hermes_home=self.hermes_home,
                dry_run=self.dry_run,
            )
            return True
        except Exception:  # noqa: BLE001 - registration failure counts as unverified (fail-closed)
            return False

    def update_memory_plugin(self, metadata: dict[str, Any]) -> MemoryPluginReleaseStatus:
        """Refresh only an OpenClaw plugin release previously managed by Core."""
        prior = metadata.get("memory_plugin")
        if not self.openclaw_bin or not isinstance(prior, dict) or prior.get("action") not in {"downloaded", "reused"}:
            return MemoryPluginReleaseStatus(reason="No Cyber Health-managed OpenClaw plugin Release to update.")
        try:
            release = self.memory_plugin_release_resolver()
            archive_path = self.plugins_dir / release.archive_name
            if prior.get("version") == release.version and prior.get("sha256") == release.sha256:
                return MemoryPluginReleaseStatus(
                    action="reused", version=release.version, archive_path=str(archive_path), sha256=release.sha256,
                    reason="The installed plugin Release is already the latest stable verified version.",
                )
            if self.dry_run:
                return MemoryPluginReleaseStatus(
                    action="planned", version=release.version, archive_path=str(archive_path), sha256=release.sha256,
                    reason="Resolved the latest stable plugin Release; dry run will not install it.",
                )
            status = cache_memory_plugin_release(release, self.plugins_dir)
            result = subprocess.run(
                [self.openclaw_bin, "plugins", "install", status.archive_path, "--force", "--accept-capabilities", "--acknowledge-install-policy-warning"],
                env=self.get_openclaw_env(), capture_output=True, text=True, timeout=45, shell=False,
                check=False,
            )
            if result.returncode != 0:
                raise UpdaterError(f"OpenClaw could not update obsidian-memory-plugin: {result.stderr.strip()}")
            status.reason = "Installed the latest SHA-256-verified plugin Release through OpenClaw."
            return status
        except MemoryPluginReleaseError as exc:
            return MemoryPluginReleaseStatus(action="error", reason=str(exc))

    def update_metadata(self, old_meta: dict[str, Any], backup_status: BackupStatus) -> None:
        """Updates config/installation.json with new version details."""
        if self.dry_run:
            return

        old_meta["version"] = self.new_version
        old_meta["updated_at"] = datetime.now(UTC).isoformat()
        if backup_status.backup_file:
            old_meta["last_backup"] = backup_status.backup_file
        old_meta["memory"] = self.memory_status.to_dict()
        old_meta["core_release"] = self.core_release_status.to_dict()
        old_meta["memory_plugin"] = self.memory_plugin_status.to_dict()
        old_meta["codex"] = {
            "name": "cyber-health",
            "registered": bool(self.codex_bin),
        }
        old_meta["hermes"] = {
            "name": "cyber-health",
            "registered": bool(self.hermes_bin),
        }

        meta_file = self.config_dir / "installation.json"
        atomic_write_text(meta_file, json.dumps(old_meta, indent=2))

    def run(self) -> UpdateReport:
        report = self._run()
        if not report.handed_off:
            self.post_update(report)
        return report

    def post_update(self, report: UpdateReport) -> None:
        """Local steps run by whichever version finished the update."""
        if report.success:
            # Older installations gain the PATH entry and private permissions on their next update.
            report.command_shim = ensure_command_shim(
                self.target_dir, get_venv_bin_dir(self.venv_dir), bin_dir=self.user_bin_dir, dry_run=self.dry_run
            )
            report.housekeeping = run_housekeeping(self.target_dir, keep_backups=self.keep_backups, dry_run=self.dry_run)
        else:
            report.command_shim = CommandShimStatus(action="skipped", reason="Update did not complete.")
        notes = [self.handoff_note, command_shim_note(report.command_shim)]
        if report.housekeeping.errors:
            notes.append(f"Housekeeping issues: {'; '.join(report.housekeeping.errors)}")
        for note in filter(None, notes):
            report.message = f"{report.message}. {note}"

    def resolve_memory_status(self, old_meta: dict[str, Any]) -> HealthManagerMemoryStatus:
        recorded_memory = self.memory_from_installation_metadata(old_meta)
        if recorded_memory.connected or not self.openclaw_bin:
            return recorded_memory
        return inspect_health_manager_memory(self.openclaw_bin, self.get_openclaw_env())

    # -- hand-off: let the freshly installed version finish its own upgrade -------------

    def should_hand_off(self, pkg_updated: bool) -> bool:
        venv_python = get_venv_bin_dir(self.venv_dir) / get_executable_name("python")
        return (
            not self.dry_run
            and pkg_updated
            and self.new_version != __version__
            and venv_python.exists()
            and os.environ.get("CYBER_HEALTH_NO_HANDOFF", "") != "1"
        )

    def hand_off(self, old_version: str, backup_status: BackupStatus, pkg_updated: bool) -> UpdateReport | None:
        """Run ``_finish`` with the new code; ``None`` means fall back to finishing in-process."""
        handoff = {
            "old_version": old_version,
            "new_version": self.new_version,
            "backup": asdict(backup_status),
            "core_release": asdict(self.core_release_status),
            "package_updated": pkg_updated,
        }
        handoff_file = self.config_dir / "update-handoff.json"
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            handoff_file.write_text(json.dumps(handoff), encoding="utf-8")
            make_private(handoff_file)
            cmd = [
                # -P keeps the caller's working directory (e.g. a source checkout) off sys.path,
                # so the child imports the newly installed package and nothing else.
                str(get_venv_bin_dir(self.venv_dir) / get_executable_name("python")), "-P", "-m", "cyber_health.update",
                "--json", "--finish-upgrade", str(handoff_file), "--target-dir", str(self.target_dir),
                "--openclaw-bin", self.openclaw_bin or "", "--codex-bin", self.codex_bin or "",
                "--hermes-bin", self.hermes_bin or "", "--keep-backups", str(self.keep_backups),
            ]
            for flag, value in (
                ("--openclaw-config", self.openclaw_config), ("--openclaw-state-dir", self.openclaw_state_dir),
                ("--codex-home", self.codex_home), ("--hermes-home", self.hermes_home),
            ):
                if value:
                    cmd += [flag, str(value)]
            if not self.release_mode:
                cmd += ["--project-root", str(self.project_root)]
            if not self.use_uv:
                cmd.append("--no-uv")
            env = dict(os.environ)
            if self.user_bin_dir is not None:
                env[USER_BIN_ENV] = str(self.user_bin_dir)
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=900, env=env, cwd=str(self.target_dir), check=False
            )
            payload = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        finally:
            with contextlib.suppress(OSError):
                handoff_file.unlink()
        if not isinstance(payload, dict) or "backup" not in payload or "target_dir" not in payload:
            return None
        report = _dataclass_from_dict(UpdateReport, payload)
        report.handed_off = True
        return report

    def run_finish(self, handoff_file: Path) -> UpdateReport:
        """Entry point for ``--finish-upgrade``: complete an upgrade started by the previous version."""
        handoff = json.loads(Path(handoff_file).read_text(encoding="utf-8"))
        old_meta = self.inspect_installation()
        old_version = str(handoff.get("old_version") or old_meta.get("version", "unknown"))
        backup_status = _dataclass_from_dict(BackupStatus, handoff.get("backup") or {})
        self.core_release_status = _dataclass_from_dict(CoreReleaseStatus, handoff.get("core_release") or {})
        # The version just installed is the one the previous updater resolved, not this source tree.
        self.new_version = str(handoff.get("new_version") or self.new_version)
        self.memory_status = self.resolve_memory_status(old_meta)
        try:
            report = self._finish(old_meta, old_version, backup_status, bool(handoff.get("package_updated")))
        except Exception as exc:  # noqa: BLE001 - fail-closed boundary: any failure is reported, never raised past the report
            report = UpdateReport(
                dry_run=self.dry_run, success=False, target_dir=str(self.target_dir), old_version=old_version,
                new_version=self.new_version, backup=backup_status, memory=self.memory_status,
                core_release=self.core_release_status, package_updated=True, message=str(exc),
            )
        self.post_update(report)
        return report

    def check_for_update(self) -> dict[str, Any]:
        """Read-only comparison of the installed version with the latest stable Core Release."""
        installed = "unknown"
        meta_file = self.config_dir / "installation.json"
        with contextlib.suppress(OSError, ValueError, AttributeError):
            installed = str(json.loads(meta_file.read_text(encoding="utf-8")).get("version", "unknown"))
        return check_core_update(installed, self.core_release_resolver)

    def _finish(
        self,
        old_meta: dict[str, Any],
        old_version: str,
        backup_status: BackupStatus,
        pkg_updated: bool,
    ) -> UpdateReport:
        """Everything after the package upgrade: plugin refresh, verification and metadata."""
        self.memory_plugin_status = self.update_memory_plugin(old_meta)
        if self.memory_plugin_status.action == "error":
            return UpdateReport(
                dry_run=self.dry_run,
                success=False,
                target_dir=str(self.target_dir),
                old_version=old_version,
                new_version=self.new_version,
                backup=backup_status,
                memory=self.memory_status,
                core_release=self.core_release_status,
                memory_plugin=self.memory_plugin_status,
                package_updated=pkg_updated,
                message=f"Memory plugin Release update failed: {self.memory_plugin_status.reason}",
            )

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
                core_release=self.core_release_status,
                memory_plugin=self.memory_plugin_status,
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
                core_release=self.core_release_status,
                memory_plugin=self.memory_plugin_status,
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
                core_release=self.core_release_status,
                memory_plugin=self.memory_plugin_status,
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

        hermes_ok = self.verify_hermes()
        if not hermes_ok:
            return UpdateReport(
                dry_run=self.dry_run,
                success=False,
                target_dir=str(self.target_dir),
                old_version=old_version,
                new_version=self.new_version,
                backup=backup_status,
                memory=self.memory_status,
                memory_plugin=self.memory_plugin_status,
                package_updated=pkg_updated,
                schema_verified=schema_ok,
                openclaw_verified=True,
                codex_verified=True,
                hermes_verified=False,
                message=(
                    "Update aborted (fail-closed): Hermes registration verification failed. "
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
            core_release=self.core_release_status,
            memory_plugin=self.memory_plugin_status,
            package_updated=pkg_updated,
            schema_verified=True,
            openclaw_verified=True,
            codex_verified=True,
            hermes_verified=True,
            message="Update completed successfully",
        )

    def _run(self) -> UpdateReport:
        old_meta = self.inspect_installation()
        old_version = old_meta.get("version", "unknown")
        backup_status = BackupStatus(source_db=str(self.target_db_path))
        self.core_release_status = self.prepare_core_release()
        self.memory_status = self.resolve_memory_status(old_meta)

        try:
            if self.core_release_status.action == "error":
                return UpdateReport(dry_run=self.dry_run, success=False, target_dir=str(self.target_dir), old_version=old_version, new_version=self.new_version, backup=backup_status, memory=self.memory_status, core_release=self.core_release_status, message=f"Core Release update failed: {self.core_release_status.reason}")
            if self.release_mode and not self.force and old_version == self.new_version:
                return UpdateReport(
                    dry_run=self.dry_run,
                    success=True,
                    target_dir=str(self.target_dir),
                    old_version=old_version,
                    new_version=self.new_version,
                    backup=BackupStatus(source_db=str(self.target_db_path), reason="Already up to date; no backup was needed."),
                    memory=self.memory_status,
                    core_release=CoreReleaseStatus(
                        action="current", version=self.new_version, reason="The installed version is the latest stable Core Release."
                    ),
                    up_to_date=True,
                    message=f"Already up to date ({self.new_version}); nothing was reinstalled. Use --force to reinstall and re-verify.",
                )
            backup_status = self.create_database_snapshot()
            pkg_updated = self.upgrade_package()
            if self.should_hand_off(pkg_updated):
                relayed = self.hand_off(old_version, backup_status, pkg_updated)
                if relayed is not None:
                    return relayed
                self.handoff_note = (
                    "The new version could not finish the upgrade itself, so the previous updater verified it."
                )
            return self._finish(old_meta, old_version, backup_status, pkg_updated)
        except Exception as exc:  # noqa: BLE001 - fail-closed boundary: any failure is reported, never raised past the report
            return UpdateReport(
                dry_run=self.dry_run,
                success=False,
                target_dir=str(self.target_dir),
                old_version=old_version,
                new_version=self.new_version,
                backup=backup_status if backup_status.created else BackupStatus(reason=f"Update failed: {exc}"),
                memory=self.memory_status,
                core_release=self.core_release_status,
                memory_plugin=self.memory_plugin_status,
                message=str(exc),
            )


def check_core_update(installed_version: str, resolver: Callable[[], CoreRelease] = resolve_latest_core_release) -> dict[str, Any]:
    try:
        release = resolver()
    except CoreReleaseError as exc:
        return {"installed_version": installed_version, "latest_version": None, "update_available": None, "error": str(exc)}
    return {
        "installed_version": installed_version,
        "latest_version": release.version,
        "update_available": release.version != installed_version,
        "release_url": release.release_url,
        "error": None,
    }


def _dataclass_from_dict(cls: Any, data: dict[str, Any]) -> Any:
    """Rebuild a (nested) report dataclass, ignoring fields this version does not know."""
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for item in dataclasses.fields(cls):
        if item.name not in data:
            continue
        value, kind = data[item.name], hints.get(item.name)
        if dataclasses.is_dataclass(kind) and isinstance(value, dict):
            value = _dataclass_from_dict(kind, value)
        kwargs[item.name] = value
    return cls(**kwargs)


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
    parser.add_argument("--force", action="store_true", help="Reinstall and re-verify even when already up to date.")
    parser.add_argument("--check", action="store_true", help="Only report whether a newer stable Release exists.")
    parser.add_argument(
        "--keep-backups", type=int, default=DEFAULT_BACKUP_RETENTION, help="Rolling update backups to keep (default 10)."
    )
    parser.add_argument("--finish-upgrade", type=str, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--codex-home", type=str, default=None, help="Codex home override (primarily for isolated testing).")
    parser.add_argument("--hermes-bin", type=str, default=_DEFAULT_BIN, help="Hermes CLI binary path override.")
    parser.add_argument("--hermes-home", type=str, default=None, help="Hermes home override (primarily for isolated testing).")
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
        f"Hermes Verified  : {report.hermes_verified}",
        "",
        "--- Cyber Health Core Release ---",
        f"Action           : {report.core_release.action}",
        f"Version          : {report.core_release.version or 'N/A'}",
        f"Wheel            : {report.core_release.wheel_path or 'N/A'}",
        f"SHA-256          : {report.core_release.sha256 or 'N/A'}",
        f"Reason           : {report.core_release.reason}",
        "",
        "--- Health-Manager Long-Term Memory ---",
        f"State            : {report.memory.state}",
        f"Plugin           : {report.memory.plugin_id} (loaded={report.memory.plugin_loaded})",
        f"Vault            : {report.memory.vault_path or 'N/A'}",
        f"Project          : {report.memory.project_id or 'N/A'}",
        f"Reason           : {report.memory.reason}",
        *[f"Warning          : {warning}" for warning in report.memory.warnings],
        "",
        *command_shim_report_lines(report.command_shim),
        *housekeeping_report_lines(report.housekeeping),
        "--- Protected Boundaries ---",
        "  + obsidian-memory: STRICTLY PRESERVED (Untouched)",
        "  + Obsidian Vaults: STRICTLY PRESERVED (Untouched)",
        "  + Codex Config:    ONLY cyber-health MCP entry managed; unrelated state preserved",
        "  + Hermes Config:   ONLY cyber-health MCP entry managed; unrelated state preserved",
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
            hermes_bin=args.hermes_bin,
            hermes_home=args.hermes_home,
            dry_run=args.dry_run,
            use_uv=not args.no_uv,
            force=args.force,
            keep_backups=args.keep_backups,
        )
        if args.check:
            result = updater.check_for_update()
            if args.json:
                print(json.dumps(result, indent=2))
            elif result["error"]:
                print(f"Could not check for updates: {result['error']}", file=sys.stderr)
            elif result["update_available"]:
                print(f"Update available: {result['installed_version']} -> {result['latest_version']} ({result['release_url']})")
            else:
                print(f"Up to date: {result['installed_version']}")
            return 1 if result["error"] else 0
        report = updater.run_finish(Path(args.finish_upgrade)) if args.finish_upgrade else updater.run()
    except Exception as exc:  # noqa: BLE001 - CLI boundary: any failure is reported as JSON or stderr with exit code 1
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
