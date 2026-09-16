"""Production-safe installer for Cyber Health Agent.

Installs Cyber Health Agent into a dedicated, isolated host directory (default: ~/.cyber-health),
sets up an isolated Python virtual environment, safely migrates existing SQLite data
with SHA256 integrity verification, and configures supported host MCP registrations.
Cyber Health remains an independent Python Core + stdio MCP server.
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
import stat
import subprocess
import sys
from typing import Any

from .uninstall import verify_cyber_health_command_signature
from .health_memory import HealthManagerMemoryStatus, inspect_health_manager_memory
from .codex_integration import (
    CodexRegistrationStatus,
    apply_codex_registration,
    find_codex_cli,
    plan_codex_registration,
)
from .hermes_integration import (
    HermesRegistrationStatus,
    apply_hermes_registration,
    find_hermes_cli,
    plan_hermes_registration,
)
from .memory_bootstrap import MemoryBootstrapStatus, MemoryBootstrapper
from .memory_plugin_release import (
    MemoryPluginReleaseError,
    MemoryPluginReleaseStatus,
    cache_memory_plugin_release,
    resolve_latest_memory_plugin_release,
)
from .core_release import CoreReleaseError, CoreReleaseStatus, cache_core_release, resolve_latest_core_release

FIXED_OPENCLAW_SERVER_NAME = "cyber-health"
DEFAULT_INSTALL_DIR_NAME = ".cyber-health"

PROTECTED_NAMES = (
    "obsidian-memory",
    "obsidian",
    "vault",
    "codex",
)

SYSTEM_BROAD_PATHS = {
    Path("/"),
    Path("/root"),
    Path("/home"),
    Path("/Users"),
    Path("/tmp"),
    Path("/var"),
    Path("/etc"),
    Path("/usr"),
    Path("/System"),
    Path("/Library"),
    Path("/Applications"),
    Path("/Volumes"),
}
# Keep this concrete path at import time.  Some callers (and our Windows-path
# simulation tests) temporarily replace ``pathlib.Path``; calling ``Path.home``
# while that replacement is active is not supported by Python 3.11.
USER_HOME_PATH = Path.home()

_DEFAULT_BIN = object()


class InstallerError(Exception):
    """Base exception for installer errors."""


class SafetyBoundaryError(InstallerError):
    """Raised when an operation touches a protected resource or invalid path."""


class DataMigrationError(InstallerError):
    """Raised when SQLite data validation or migration fails."""


def has_symlink_in_path(path: Path) -> bool:
    """Checks if path or any existing ancestor component is a symlink."""
    curr = path
    while True:
        try:
            if curr.is_symlink():
                return True
        except (OSError, ValueError):
            pass
        parent = curr.parent
        if parent == curr:
            break
        curr = parent
    return False


def is_strictly_inside_dir(path: Path, parent_dir: Path) -> bool:
    """Verifies that resolved path is strictly inside parent_dir."""
    try:
        res_path = path.resolve()
        res_parent = parent_dir.resolve()
        return res_parent in res_path.parents
    except Exception:
        return False


def get_venv_bin_dir(venv_dir: Path) -> Path:
    """Returns the executable directory within a virtual environment across platforms."""
    if sys.platform == "win32":
        return venv_dir / "Scripts"
    return venv_dir / "bin"


def get_executable_name(base_name: str) -> str:
    """Returns platform-specific executable filename (appending .exe on Windows)."""
    if sys.platform == "win32" and not base_name.lower().endswith(".exe"):
        return f"{base_name}.exe"
    return base_name


def is_system_broad_or_drive_root(path: Path) -> bool:
    """Checks whether path represents a root, drive root, or broad system directory."""
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path

    if resolved in SYSTEM_BROAD_PATHS or resolved == USER_HOME_PATH:
        return True

    if sys.platform == "win32":
        # A Windows drive root has exactly its anchor as the sole path part.
        # Avoid constructing another Path here so this check also remains safe
        # when a caller supplies a path-like implementation.
        if resolved.anchor and len(resolved.parts) == 1:
            return True
        win_dir = os.environ.get("WINDIR", "C:\\Windows")
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        for broad in (win_dir, prog_files, prog_files_x86):
            if broad and Path(broad).resolve() == resolved:
                return True

    if len(resolved.parts) <= 1:
        return True
    if len(resolved.parts) <= 2 and not resolved.drive:
        return True
    return False


def compute_sha256(file_path: Path) -> str:
    """Computes SHA256 digest of a regular file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_text(file_path: Path, content: str) -> None:
    """Replace a small metadata file atomically, cleaning up on failure."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_name(f".{file_path.name}.tmp-{os.getpid()}")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, file_path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def verify_sqlite_integrity(db_file: Path) -> tuple[bool, str]:
    """Runs PRAGMA integrity_check on SQLite database."""
    if not db_file.exists() or not db_file.is_file():
        return False, "Database file does not exist or is not a regular file"
    try:
        conn = sqlite3.connect(f"file:{db_file.resolve()}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        rows = cursor.fetchall()
        conn.close()
        if rows and rows[0][0] == "ok":
            return True, "ok"
        return False, f"Integrity check failed: {rows}"
    except Exception as exc:
        return False, f"SQLite integrity check exception: {exc}"


def safe_checkpoint_db(db_file: Path) -> tuple[bool, str]:
    """Safely checkpoints SQLite WAL into main database file with TRUNCATE."""
    if not db_file.exists() or not db_file.is_file() or has_symlink_in_path(db_file):
        return False, "Database file does not exist, is not a regular file, or has symlinks"
    try:
        conn = sqlite3.connect(str(db_file.resolve()))
        cursor = conn.cursor()
        rows = cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchall()
        conn.close()
        if rows and rows[0][0] != 0:
            return False, f"WAL checkpoint busy or incomplete: {rows}"
        return True, "ok"
    except Exception as exc:
        return False, f"WAL checkpoint exception: {exc}"


@dataclass
class DataMigrationStatus:
    source_db: str | None = None
    target_db: str = ""
    action: str = "none"  # "fresh_install", "migrated", "preserve_existing", "error"
    source_sha256: str | None = None
    target_sha256: str | None = None
    wal_migrated: bool = False
    shm_migrated: bool = False
    reason: str = ""
    executed: bool = False


@dataclass
class OpenClawRegistrationStatus:
    name: str = FIXED_OPENCLAW_SERVER_NAME
    detected: bool = False
    action: str = "none"  # "register", "update", "skip", "error"
    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    reason: str = ""
    executed: bool = False


@dataclass
class InstallReport:
    dry_run: bool
    success: bool
    version: str
    source_project_root: str
    target_dir: str
    venv_dir: str
    data: DataMigrationStatus
    openclaw: OpenClawRegistrationStatus
    core_release: CoreReleaseStatus = field(default_factory=CoreReleaseStatus)
    codex: CodexRegistrationStatus = field(default_factory=CodexRegistrationStatus)
    hermes: HermesRegistrationStatus = field(default_factory=HermesRegistrationStatus)
    memory_plugin: MemoryPluginReleaseStatus = field(default_factory=MemoryPluginReleaseStatus)
    memory: HealthManagerMemoryStatus = field(default_factory=HealthManagerMemoryStatus)
    memory_bootstrap: MemoryBootstrapStatus = field(default_factory=MemoryBootstrapStatus)
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


class CyberHealthInstaller:
    def __init__(
        self,
        project_root: Path | str | None = None,
        target_dir: Path | str | None = None,
        source_db: Path | str | None = None,
        openclaw_bin: str | None | object = _DEFAULT_BIN,
        openclaw_config: Path | str | None = None,
        openclaw_state_dir: Path | str | None = None,
        codex_bin: str | None | object = _DEFAULT_BIN,
        codex_home: Path | str | None = None,
        hermes_bin: str | None | object = _DEFAULT_BIN,
        hermes_home: Path | str | None = None,
        memory_vault: Path | str | None = None,
        memory_project_id: str | None = None,
        dry_run: bool = False,
        use_uv: bool = True,
        editable: bool = False,
        skip_openclaw: bool = False,
        skip_codex: bool = False,
        skip_hermes: bool = False,
        memory_plugin_release_resolver=resolve_latest_memory_plugin_release,
        core_release_resolver=resolve_latest_core_release,
    ):
        # Resolve source project root
        self.release_mode = project_root is None
        if project_root is None:
            self._raw_project_root = Path(__file__).resolve().parents[1]
        else:
            self._raw_project_root = Path(project_root)

        if has_symlink_in_path(self._raw_project_root):
            raise SafetyBoundaryError(f"Symlinked source project root rejected: {self._raw_project_root}")

        self.project_root = self._raw_project_root.resolve()
        self._validate_source_root(self.project_root)

        # Resolve target installation directory
        if target_dir is None:
            self._raw_target_dir = Path.home() / DEFAULT_INSTALL_DIR_NAME
        else:
            self._raw_target_dir = Path(target_dir)

        if has_symlink_in_path(self._raw_target_dir):
            raise SafetyBoundaryError(f"Symlinked target directory rejected: {self._raw_target_dir}")

        self.target_dir = self._raw_target_dir.resolve()
        self._validate_target_dir(self.target_dir)

        # Target directory sub-paths
        self.venv_dir = self.target_dir / "venv"
        self.data_dir = self.target_dir / "data"
        self.backups_dir = self.data_dir / "backups"
        self.config_dir = self.target_dir / "config"
        self.bin_dir = self.target_dir / "bin"
        self.plugins_dir = self.target_dir / "plugins"
        self.releases_dir = self.target_dir / "releases"
        self.target_db_path = self.data_dir / "cyber-health.sqlite3"

        # Resolve source DB path
        if source_db is not None:
            self._raw_source_db = Path(source_db)
        elif "CYBER_HEALTH_DB" in os.environ:
            self._raw_source_db = Path(os.environ["CYBER_HEALTH_DB"])
        else:
            self._raw_source_db = self.project_root / "data" / "cyber-health.sqlite3"

        if self._raw_source_db.exists() and has_symlink_in_path(self._raw_source_db):
            raise SafetyBoundaryError(f"Symlinked source database rejected: {self._raw_source_db}")

        self.source_db_path = self._raw_source_db.resolve() if self._raw_source_db.exists() else self._raw_source_db

        # OpenClaw CLI resolution
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
        self.use_uv = use_uv
        self.editable = editable
        self.skip_openclaw = skip_openclaw
        self.skip_codex = skip_codex
        self.skip_hermes = skip_hermes
        self.memory_vault = Path(memory_vault) if memory_vault else None
        self.memory_project_id = memory_project_id
        self.memory_plugin_release_resolver = memory_plugin_release_resolver
        self.core_release_resolver = core_release_resolver
        self.core_release_status = CoreReleaseStatus()
        self.core_release = None
        self.package_install_source: Path = self.project_root
        self.memory_plugin_archive = self.plugins_dir / "obsidian-memory-plugin.tgz"
        self.memory_plugin_status = MemoryPluginReleaseStatus()
        self.memory_bootstrapper = MemoryBootstrapper(
            vault_path=self.memory_vault,
            project_root=self.project_root,
            openclaw_bin=self.openclaw_bin,
            openclaw_env=self.get_openclaw_env(),
            plugin_archive=self.memory_plugin_archive,
            plugin_archive_available=False,
            project_id=self.memory_project_id,
            dry_run=self.dry_run,
        )

        # Read version from pyproject.toml or fallback
        self.version = self._detect_version()
        if self.editable and self.release_mode:
            raise InstallerError("--editable requires an explicit --project-root development checkout.")
        self.memory_status = HealthManagerMemoryStatus()

    def _validate_source_root(self, root: Path) -> None:
        if is_system_broad_or_drive_root(root):
            raise SafetyBoundaryError(f"Broad or system project root rejected: {root}")

    def _validate_target_dir(self, target: Path) -> None:
        if is_system_broad_or_drive_root(target):
            raise SafetyBoundaryError(f"Broad or system target directory rejected: {target}")

        target_str_lower = str(target).lower()
        for protected in PROTECTED_NAMES:
            if protected in target_str_lower:
                raise SafetyBoundaryError(
                    f"Target directory contains protected keyword '{protected}': {target}"
                )

    def _detect_version(self) -> str:
        pyproject = self.project_root / "pyproject.toml"
        if pyproject.exists():
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("version =") or line.startswith("version="):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"').strip("'")
        return "0.2.6"

    def prepare_core_release(self) -> CoreReleaseStatus:
        """Resolve the Core wheel in release mode; local roots remain development-only."""
        if not self.release_mode:
            return CoreReleaseStatus(action="local", version=self.version, reason="Using the explicit local development source.")
        try:
            self.core_release = self.core_release_resolver()
            self.version = self.core_release.version
            wheel_path = self.releases_dir / self.core_release.wheel_name
            if self.dry_run:
                status = CoreReleaseStatus("planned", self.core_release.version, str(wheel_path), self.core_release.sha256, "Resolved the latest stable Core Release; dry run will not download it.")
            else:
                status = cache_core_release(self.core_release, self.releases_dir)
            self.package_install_source = Path(status.wheel_path)
            return status
        except CoreReleaseError as exc:
            return CoreReleaseStatus(action="error", reason=str(exc))

    def get_openclaw_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.openclaw_config:
            env["OPENCLAW_CONFIG_PATH"] = str(self.openclaw_config)
        if self.openclaw_state_dir:
            env["OPENCLAW_STATE_DIR"] = str(self.openclaw_state_dir)
        return env

    def plan_data_migration(self) -> DataMigrationStatus:
        """Determines migration strategy with a safe source WAL normalization."""
        status = DataMigrationStatus(
            source_db=str(self.source_db_path),
            target_db=str(self.target_db_path),
        )

        if self.target_db_path.exists():
            # Target DB already exists: MUST preserve it!
            if not self.target_db_path.is_file() or self.target_db_path.is_symlink():
                status.action = "error"
                status.reason = f"Existing target database is not a regular file: {self.target_db_path}"
                return status

            ok, integrity_msg = verify_sqlite_integrity(self.target_db_path)
            if not ok:
                status.action = "error"
                status.reason = f"Existing target database integrity check failed: {integrity_msg}"
                return status

            status.action = "preserve_existing"
            status.target_sha256 = compute_sha256(self.target_db_path)
            status.reason = "Target database already exists; preserved existing user data (no overwrite)"
            return status

        # A database sidecar without its main file may still contain data from
        # an interrupted install.  Never delete it as cleanup; stop and leave
        # the recovery artifact untouched for explicit inspection.
        orphan_sidecars = [
            sidecar
            for sidecar in (
                self.target_db_path.parent / (self.target_db_path.name + "-wal"),
                self.target_db_path.parent / (self.target_db_path.name + "-shm"),
            )
            if sidecar.exists()
        ]
        if orphan_sidecars:
            status.action = "error"
            status.reason = (
                "Target database is missing but SQLite sidecar(s) remain; "
                f"refusing to delete recovery artifacts: {', '.join(map(str, orphan_sidecars))}"
            )
            return status

        # Target DB does not exist: check if source DB exists to migrate
        if self.source_db_path.exists():
            if not self.source_db_path.is_file() or self.source_db_path.is_symlink():
                status.action = "error"
                status.reason = f"Source database is not a regular file: {self.source_db_path}"
                return status

            # Checkpoint WAL so the source database file contains the latest
            # committed state before hashing.  Planning intentionally performs
            # this safe, bounded normalization because the plan's checksum is
            # later used to detect TOCTOU changes during execution.
            checkpoint_ok, checkpoint_msg = safe_checkpoint_db(self.source_db_path)
            if not checkpoint_ok:
                status.action = "error"
                status.reason = f"Source WAL checkpoint failed during planning: {checkpoint_msg}"
                return status

            ok, integrity_msg = verify_sqlite_integrity(self.source_db_path)
            if not ok:
                status.action = "error"
                status.reason = f"Source database integrity check failed: {integrity_msg}"
                return status

            status.action = "migrated"
            status.source_sha256 = compute_sha256(self.source_db_path)
            status.reason = "Source database verified; planned atomic migration with SHA256 validation"

            wal = self.source_db_path.parent / (self.source_db_path.name + "-wal")
            shm = self.source_db_path.parent / (self.source_db_path.name + "-shm")
            status.wal_migrated = wal.exists() and wal.is_file() and not wal.is_symlink()
            status.shm_migrated = shm.exists() and shm.is_file() and not shm.is_symlink()
            return status

        # Neither exists: clean initial setup
        status.action = "fresh_install"
        status.reason = "No existing database detected; will be initialized on first run"
        return status

    def plan_memory(self) -> HealthManagerMemoryStatus:
        """Inspect the health-manager long-term memory boundary before install."""
        if self.skip_openclaw:
            self.memory_status = HealthManagerMemoryStatus(
                state="unconfigured",
                reason="OpenClaw inspection was skipped; health-manager long-term memory was not verified.",
                warnings=["OpenClaw inspection was skipped; health-manager long-term memory was not verified."],
            )
        else:
            self.memory_status = inspect_health_manager_memory(
                self.openclaw_bin,
                self.get_openclaw_env(),
            )
        return self.memory_status

    def plan_memory_bootstrap(self) -> MemoryBootstrapStatus:
        return self.memory_bootstrapper.plan()

    @staticmethod
    def planned_memory_status(bootstrap: MemoryBootstrapStatus) -> HealthManagerMemoryStatus:
        return HealthManagerMemoryStatus(
            state="planned",
            vault_path=bootstrap.vault_path,
            project_id=bootstrap.project_id,
            project_path=bootstrap.project_path,
            provider_args=[
                "--memory-provider", "obsidian",
                "--memory-vault", bootstrap.vault_path or "",
                "--memory-project-id", bootstrap.project_id or "",
            ],
            reason="Memory bootstrap is planned from the explicitly selected Vault.",
        )

    @staticmethod
    def memory_bootstrap_ready(bootstrap: MemoryBootstrapStatus) -> bool:
        """Return true only for a fully validated, host-neutral Vault plan."""
        return (
            bootstrap.requested
            and bootstrap.obsidian_action == "verify"
            and bootstrap.vault_action in {"verify", "create", "append"}
            and bootstrap.plugin_action in {"verify", "install", "skip"}
            and bootstrap.config_action in {"verify", "configure", "skip"}
            and bool(bootstrap.vault_path)
            and bool(bootstrap.project_id)
            and bool(bootstrap.project_path)
        )

    def plan_openclaw(self) -> OpenClawRegistrationStatus:
        """Inspects OpenClaw configuration state and plans registration."""
        status = OpenClawRegistrationStatus(name=FIXED_OPENCLAW_SERVER_NAME)

        if self.skip_openclaw:
            status.action = "skip"
            status.reason = "OpenClaw registration skipped by request"
            return status

        target_mcp_bin = get_venv_bin_dir(self.venv_dir) / get_executable_name("cyber-health-mcp")
        status.command = str(target_mcp_bin)
        status.args = ["--db", str(self.target_db_path), "--allow-all", *self.memory_status.provider_args]
        status.cwd = str(self.target_dir)

        if not self.openclaw_bin:
            status.action = "skip"
            status.reason = "OpenClaw CLI executable not found; registration skipped"
            return status

        try:
            cmd = [self.openclaw_bin, "mcp", "show", FIXED_OPENCLAW_SERVER_NAME, "--json"]
            result = subprocess.run(
                cmd,
                env=self.get_openclaw_env(),
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
            )
        except Exception:
            status.action = "error"
            status.reason = "Failed to invoke OpenClaw CLI inspection executable"
            return status

        combined_output = (result.stderr + " " + result.stdout).strip()

        if result.returncode == 0:
            status.detected = True
            try:
                raw_data = json.loads(result.stdout)
            except Exception:
                status.action = "error"
                status.reason = "OpenClaw CLI inspection returned malformed JSON"
                return status

            if not isinstance(raw_data, dict):
                status.action = "error"
                status.reason = "OpenClaw CLI inspection returned non-dictionary response"
                return status

            existing_cmd = raw_data.get("command", "")
            args = raw_data.get("args", [])
            if not isinstance(args, list):
                args = []

            # Verify that existing server registration belongs to Cyber Health
            if not verify_cyber_health_command_signature(existing_cmd, args):
                status.action = "error"
                status.reason = (
                    f"Foreign MCP server registered under '{FIXED_OPENCLAW_SERVER_NAME}' "
                    f"(command: '{existing_cmd}'); refusing to overwrite (fail-closed)"
                )
                return status

            existing_db = None
            for i, a in enumerate(args):
                if a == "--db" and i + 1 < len(args):
                    existing_db = args[i + 1]

            if existing_cmd == status.command and existing_db == str(self.target_db_path):
                status.action = "update"
                status.reason = "OpenClaw MCP server already registered at target; will verify/refresh registration"
            else:
                status.action = "update"
                status.reason = "OpenClaw MCP server registered to previous Cyber Health path; will update registration"
            return status

        if (
            'No MCP server named "cyber-health"' in combined_output
            or 'No MCP server named \\"cyber-health\\"' in combined_output
            or "No MCP server named 'cyber-health'" in combined_output
        ):
            status.detected = False
            status.action = "register"
            status.reason = "OpenClaw MCP server not registered; planned new registration"
            return status

        status.action = "error"
        status.reason = f"OpenClaw inspection failed with exit code {result.returncode}"
        return status

    def plan_codex(self) -> CodexRegistrationStatus:
        """Inspect and plan the independent Codex stdio MCP registration."""
        target_mcp = get_venv_bin_dir(self.venv_dir) / get_executable_name("cyber-health-mcp")
        args = ["--db", str(self.target_db_path), "--allow-all", *self.memory_status.provider_args]
        return plan_codex_registration(
            self.codex_bin,
            self.target_dir,
            self.target_db_path,
            str(target_mcp),
            args,
            "",
            codex_home=self.codex_home,
            skip=self.skip_codex,
        )

    def plan_hermes(self) -> HermesRegistrationStatus:
        """Inspect and plan Hermes using its native discovery-first MCP CLI."""
        target_mcp = get_venv_bin_dir(self.venv_dir) / get_executable_name("cyber-health-mcp")
        args = ["--db", str(self.target_db_path), "--allow-all", *self.memory_status.provider_args]
        return plan_hermes_registration(
            self.hermes_bin,
            self.target_dir,
            self.target_db_path,
            str(target_mcp),
            args,
            hermes_home=self.hermes_home,
            skip=self.skip_hermes,
        )

    def prepare_memory_plugin_release(self) -> MemoryPluginReleaseStatus:
        """Resolve a stable public release only when OpenClaw needs an archive."""
        if not self.memory_vault or not self.openclaw_bin:
            return MemoryPluginReleaseStatus(
                reason="No OpenClaw plugin artifact is needed for this installation. Hermes uses its native plugin installer.",
            )
        try:
            release = self.memory_plugin_release_resolver()
            archive_path = self.plugins_dir / release.archive_name
            if self.dry_run:
                status = MemoryPluginReleaseStatus(
                    action="planned", version=release.version, archive_path=str(archive_path), sha256=release.sha256,
                    reason="Resolved the latest stable plugin Release; dry run will not download it.",
                )
            else:
                status = cache_memory_plugin_release(release, self.plugins_dir)
            self.memory_plugin_archive = Path(status.archive_path)
            self.memory_bootstrapper.plugin_archive = self.memory_plugin_archive
            self.memory_bootstrapper.plugin_archive_available = True
            return status
        except MemoryPluginReleaseError as exc:
            self.memory_bootstrapper.plugin_archive_available = False
            return MemoryPluginReleaseStatus(action="error", reason=str(exc))

    def execute_data_migration(self, data_plan: DataMigrationStatus) -> None:
        """Executes safe atomic migration of SQLite database and verifies checksums."""
        if data_plan.action != "migrated":
            data_plan.executed = True
            return

        if self.dry_run:
            return

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

        source = Path(data_plan.source_db)
        target = Path(data_plan.target_db)

        # TOCTOU defense
        if not source.exists() or not source.is_file() or source.is_symlink():
            raise DataMigrationError(f"Source database changed or invalid immediately before migration: {source}")

        # Checkpoint and check source integrity once more.  A failed checkpoint
        # must abort rather than copying only the main file while WAL pages may
        # still contain committed data.
        checkpoint_ok, checkpoint_msg = safe_checkpoint_db(source)
        if not checkpoint_ok:
            raise DataMigrationError(f"Source WAL checkpoint failed right before migration: {checkpoint_msg}")
        ok, msg = verify_sqlite_integrity(source)
        if not ok:
            raise DataMigrationError(f"Source integrity check failed right before migration: {msg}")

        # Atomic copy
        temp_target = target.with_suffix(".tmp_migration")
        temp_wal = temp_target.parent / (temp_target.name + "-wal")
        temp_shm = temp_target.parent / (temp_target.name + "-shm")
        try:
            shutil.copy2(source, temp_target)
            target_sha = compute_sha256(temp_target)
            if target_sha != data_plan.source_sha256:
                raise DataMigrationError(
                    f"Checksum mismatch during migration! Source: {data_plan.source_sha256}, Target: {target_sha}"
                )

            ok, integrity_msg = verify_sqlite_integrity(temp_target)
            if not ok:
                raise DataMigrationError(f"Target integrity check failed after copy: {integrity_msg}")

            # Safe checkpoint to fold any pages created during check back into main file
            checkpoint_ok, checkpoint_msg = safe_checkpoint_db(temp_target)
            if not checkpoint_ok:
                raise DataMigrationError(f"Temporary target WAL checkpoint failed: {checkpoint_msg}")
            for sc in (temp_wal, temp_shm):
                if sc.exists():
                    try:
                        sc.unlink()
                    except Exception:
                        pass

            temp_target.replace(target)
            data_plan.target_sha256 = target_sha

            # Verify target checkpoint and ensure no stale 0-byte sidecars remain.
            # A failed final checkpoint is not harmless: it can leave a target
            # whose sidecars no longer describe the copied main database.
            checkpoint_ok, checkpoint_msg = safe_checkpoint_db(target)
            if not checkpoint_ok:
                raise DataMigrationError(f"Final target WAL checkpoint failed: {checkpoint_msg}")
            target_sidecars = (
                target.parent / (target.name + "-wal"),
                target.parent / (target.name + "-shm"),
            )
            for sc in target_sidecars:
                if sc.exists() and sc.stat().st_size == 0:
                    try:
                        sc.unlink()
                    except Exception:
                        pass

            data_plan.executed = True
            data_plan.reason = "Successfully migrated database with matching SHA256 checksum"
        finally:
            for sc in (temp_target, temp_wal, temp_shm):
                if sc.exists():
                    try:
                        sc.unlink()
                    except Exception:
                        pass

    def setup_environment(self) -> None:
        """Initializes virtual environment and installs Cyber Health Agent package."""
        if self.dry_run:
            return

        self.target_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.bin_dir.mkdir(parents=True, exist_ok=True)

        uv_bin = shutil.which("uv") if self.use_uv else None
        venv_python = get_venv_bin_dir(self.venv_dir) / get_executable_name("python")

        if uv_bin:
            # Create venv with uv
            if not self.venv_dir.exists():
                cmd_venv = [uv_bin, "venv", str(self.venv_dir)]
                res = subprocess.run(cmd_venv, capture_output=True, text=True, check=False)
                if res.returncode != 0:
                    raise InstallerError(f"Failed to create virtual environment with uv: {res.stderr.strip()}")

            # Install package with uv pip
            install_args = [uv_bin, "pip", "install"]
            if self.editable:
                install_args.extend(["-e", str(self.package_install_source)])
            else:
                install_args.append(str(self.package_install_source))
            install_args.extend(["--python", str(venv_python)])

            res_install = subprocess.run(install_args, capture_output=True, text=True, check=False)
            if res_install.returncode != 0:
                raise InstallerError(f"Failed to install package with uv pip: {res_install.stderr.strip()}")
        else:
            # Fallback to standard Python venv
            if not self.venv_dir.exists():
                cmd_venv = [sys.executable, "-m", "venv", str(self.venv_dir)]
                res = subprocess.run(cmd_venv, capture_output=True, text=True, check=False)
                if res.returncode != 0:
                    raise InstallerError(f"Failed to create virtual environment: {res.stderr.strip()}")

            install_cmd = [str(venv_python), "-m", "pip", "install"]
            if self.editable:
                install_cmd.extend(["-e", str(self.package_install_source)])
            else:
                install_cmd.append(str(self.package_install_source))

            res_install = subprocess.run(install_cmd, capture_output=True, text=True, check=False)
            if res_install.returncode != 0:
                raise InstallerError(f"Failed to install package with pip: {res_install.stderr.strip()}")

        # Ensure executable wrappers/symlinks in bin/
        target_mcp = get_venv_bin_dir(self.venv_dir) / get_executable_name("cyber-health-mcp")
        if not target_mcp.exists():
            raise InstallerError(f"Installation failed: {target_mcp} executable was not created")

        if sys.platform == "win32":
            # On Windows, generate batch wrappers to avoid symlink privilege issues
            cmd_wrapper = self.bin_dir / "cyber-health-mcp.cmd"
            cmd_wrapper.write_text(f'@echo off\n"{target_mcp}" %*\n', encoding="utf-8")
            target_cli = get_venv_bin_dir(self.venv_dir) / get_executable_name("cyber-health")
            if target_cli.exists():
                cli_wrapper = self.bin_dir / "cyber-health.cmd"
                cli_wrapper.write_text(f'@echo off\n"{target_cli}" %*\n', encoding="utf-8")
        else:
            bin_link = self.bin_dir / "cyber-health-mcp"
            try:
                if bin_link.is_symlink() or bin_link.exists():
                    bin_link.unlink()
                bin_link.symlink_to(target_mcp)
            except Exception:
                pass

    def register_openclaw(self, openclaw_status: OpenClawRegistrationStatus) -> None:
        """Registers or updates MCP server in OpenClaw."""
        if openclaw_status.action not in ("register", "update") or self.skip_openclaw:
            return

        if self.dry_run:
            return

        if not self.openclaw_bin:
            return

        mcp_payload = json.dumps(
            {
                "command": openclaw_status.command,
                "args": openclaw_status.args,
                "cwd": openclaw_status.cwd,
                "connectionTimeoutMs": 20000,
                "requestTimeoutMs": 30000,
            }
        )

        cmd = [self.openclaw_bin, "mcp", "set", FIXED_OPENCLAW_SERVER_NAME, mcp_payload]
        res = subprocess.run(
            cmd,
            env=self.get_openclaw_env(),
            capture_output=True,
            text=True,
            timeout=20,
            shell=False,
        )

        if res.returncode != 0:
            raise InstallerError(
                f"Failed to register OpenClaw MCP server {FIXED_OPENCLAW_SERVER_NAME} (CLI code {res.returncode})"
            )

        openclaw_status.executed = True
        openclaw_status.reason = f"Successfully registered MCP server {FIXED_OPENCLAW_SERVER_NAME} in OpenClaw"

    def register_codex(self, codex_status: CodexRegistrationStatus) -> None:
        apply_codex_registration(
            self.codex_bin,
            codex_status,
            codex_home=self.codex_home,
            dry_run=self.dry_run,
        )

    def register_hermes(self, hermes_status: HermesRegistrationStatus) -> None:
        apply_hermes_registration(
            self.hermes_bin,
            hermes_status,
            self.target_dir,
            self.target_db_path,
            hermes_home=self.hermes_home,
            dry_run=self.dry_run,
        )

    def write_installation_metadata(
        self,
        codex_status: CodexRegistrationStatus,
        hermes_status: HermesRegistrationStatus,
        memory_bootstrap: MemoryBootstrapStatus,
        memory_plugin: MemoryPluginReleaseStatus,
        core_release: CoreReleaseStatus,
    ) -> None:
        """Records installation metadata to config/installation.json."""
        if self.dry_run:
            return

        meta = {
            "version": self.version,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "project_root": str(self.project_root),
            "target_dir": str(self.target_dir),
            "venv_dir": str(self.venv_dir),
            "db_path": str(self.target_db_path),
            "python_executable": str(get_venv_bin_dir(self.venv_dir) / get_executable_name("python")),
            "mcp_executable": str(get_venv_bin_dir(self.venv_dir) / get_executable_name("cyber-health-mcp")),
            "editable": self.editable,
            "core_release": core_release.to_dict(),
            "memory": self.memory_status.to_dict(),
            "memory_bootstrap": memory_bootstrap.to_dict(),
            "memory_plugin": memory_plugin.to_dict(),
            "codex": {
                "name": codex_status.name,
                "registered": codex_status.executed or codex_status.action == "update",
            },
            "hermes": {
                "name": hermes_status.name,
                "registered": hermes_status.executed or hermes_status.action == "verify",
                "probe_verified": hermes_status.probe_verified,
                "tool_count": hermes_status.tool_count,
            },
        }

        meta_file = self.config_dir / "installation.json"
        atomic_write_text(meta_file, json.dumps(meta, indent=2))

    def write_installation_failure_marker(
        self,
        phase: str,
        error: Exception,
        data_plan: DataMigrationStatus,
        openclaw_plan: OpenClawRegistrationStatus,
        codex_plan: CodexRegistrationStatus,
        hermes_plan: HermesRegistrationStatus,
    ) -> Path | None:
        """Persist a non-destructive recovery marker for a partial install."""
        if self.dry_run or not self.target_dir.exists():
            return None

        marker = self.config_dir / "install-failure.json"
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                marker,
                json.dumps(
                    {
                        "failed_at": datetime.now(timezone.utc).isoformat(),
                        "phase": phase,
                        "error": str(error),
                        "data_action": data_plan.action,
                        "data_executed": data_plan.executed,
                        "openclaw_action": openclaw_plan.action,
                        "openclaw_executed": openclaw_plan.executed,
                        "codex_action": codex_plan.action,
                        "codex_executed": codex_plan.executed,
                        "hermes_action": hermes_plan.action,
                        "hermes_executed": hermes_plan.executed,
                        "memory": self.memory_status.to_dict(),
                        "target_dir": str(self.target_dir),
                        "user_data_preserved": True,
                        "recovery": "Fix the reported cause and rerun cyber-health install; do not delete data manually.",
                    },
                    indent=2,
                ),
            )
            return marker
        except Exception:
            # The original installation error is more actionable than a
            # secondary failure while recording the marker.
            return None

    def run(self) -> InstallReport:
        # Phase 1: Planning and inspection, including safe source WAL normalization.
        data_plan = self.plan_data_migration()
        self.core_release_status = self.prepare_core_release()
        self.memory_plugin_status = self.prepare_memory_plugin_release()
        memory_bootstrap_plan = self.plan_memory_bootstrap()
        memory_plan = self.plan_memory()
        if self.memory_bootstrap_ready(memory_bootstrap_plan):
            memory_plan = self.planned_memory_status(memory_bootstrap_plan)
            self.memory_status = memory_plan
        openclaw_plan = self.plan_openclaw()
        codex_plan = self.plan_codex()
        hermes_plan = self.plan_hermes()

        # Phase 2: Fail-closed validation check
        refusal_reasons: list[str] = []
        if data_plan.action == "error":
            refusal_reasons.append(f"Data migration error: {data_plan.reason}")
        if self.core_release_status.action == "error":
            refusal_reasons.append(f"Core Release error: {self.core_release_status.reason}")
        if openclaw_plan.action == "error":
            refusal_reasons.append(f"OpenClaw registration error: {openclaw_plan.reason}")
        if codex_plan.action in ("error", "refused"):
            refusal_reasons.append(f"Codex registration error: {codex_plan.reason}")
        if hermes_plan.action in ("error", "refused"):
            refusal_reasons.append(f"Hermes registration error: {hermes_plan.reason}")
        if self.memory_plugin_status.action == "error":
            refusal_reasons.append(f"Memory plugin Release error: {self.memory_plugin_status.reason}")
        if memory_bootstrap_plan.requested and any(
            action in ("error", "install-required") for action in (
                memory_bootstrap_plan.obsidian_action,
                memory_bootstrap_plan.plugin_action,
                memory_bootstrap_plan.vault_action,
                memory_bootstrap_plan.config_action,
            )
        ):
            refusal_reasons.append(f"Memory bootstrap error: {memory_bootstrap_plan.reason}")

        if refusal_reasons:
            return InstallReport(
                dry_run=self.dry_run,
                success=False,
                version=self.version,
                source_project_root=str(self.project_root),
                target_dir=str(self.target_dir),
                venv_dir=str(self.venv_dir),
                data=data_plan,
                openclaw=openclaw_plan,
                core_release=self.core_release_status,
                codex=codex_plan,
                hermes=hermes_plan,
                memory_plugin=self.memory_plugin_status,
                memory=memory_plan,
                memory_bootstrap=memory_bootstrap_plan,
                message="; ".join(refusal_reasons),
            )

        # Phase 3: Dry-run check
        if self.dry_run:
            return InstallReport(
                dry_run=True,
                success=True,
                version=self.version,
                source_project_root=str(self.project_root),
                target_dir=str(self.target_dir),
                venv_dir=str(self.venv_dir),
                data=data_plan,
                openclaw=openclaw_plan,
                core_release=self.core_release_status,
                codex=codex_plan,
                hermes=hermes_plan,
                memory_plugin=self.memory_plugin_status,
                memory=memory_plan,
                memory_bootstrap=memory_bootstrap_plan,
                message="Dry run completed successfully (zero mutations)",
            )

        # Phase 4: Execution.  Keep failures reportable and recoverable: the
        # installer never removes user data as an implicit rollback.
        phase = "setup_environment"
        try:
            self.setup_environment()
            phase = "data_migration"
            self.execute_data_migration(data_plan)
            phase = "memory_bootstrap"
            if memory_bootstrap_plan.requested:
                self.memory_status = self.memory_bootstrapper.apply(memory_bootstrap_plan)
            phase = "openclaw_registration"
            self.register_openclaw(openclaw_plan)
            phase = "codex_registration"
            self.register_codex(codex_plan)
            phase = "hermes_registration"
            self.register_hermes(hermes_plan)
            phase = "metadata"
            self.write_installation_metadata(codex_plan, hermes_plan, memory_bootstrap_plan, self.memory_plugin_status, self.core_release_status)
            failure_marker = self.config_dir / "install-failure.json"
            if failure_marker.exists():
                failure_marker.unlink()
        except Exception as exc:
            marker = self.write_installation_failure_marker(
                phase, exc, data_plan, openclaw_plan, codex_plan, hermes_plan
            )
            marker_note = f" Recovery marker: {marker}." if marker else " Recovery marker could not be written."
            return InstallReport(
                dry_run=False,
                success=False,
                version=self.version,
                source_project_root=str(self.project_root),
                target_dir=str(self.target_dir),
                venv_dir=str(self.venv_dir),
                data=data_plan,
                openclaw=openclaw_plan,
                core_release=self.core_release_status,
                codex=codex_plan,
                hermes=hermes_plan,
                memory_plugin=self.memory_plugin_status,
                memory=self.memory_status,
                memory_bootstrap=memory_bootstrap_plan,
                message=(
                    f"Installation failed during {phase}: {exc}. "
                    "A partial installation may remain, but any existing user data was preserved; "
                    "rerun after fixing the cause."
                    + marker_note
                ),
            )

        return InstallReport(
            dry_run=False,
            success=True,
            version=self.version,
            source_project_root=str(self.project_root),
            target_dir=str(self.target_dir),
            venv_dir=str(self.venv_dir),
            data=data_plan,
            openclaw=openclaw_plan,
            core_release=self.core_release_status,
            codex=codex_plan,
            hermes=hermes_plan,
            memory_plugin=self.memory_plugin_status,
            memory=self.memory_status,
            memory_bootstrap=memory_bootstrap_plan,
            message="Installation completed successfully",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cyber-health-install",
        description="Production-safe Cyber Health Agent installer.",
    )
    parser.add_argument(
        "--target-dir",
        type=str,
        default=None,
        help="Target installation directory (defaults to ~/.cyber-health).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Source database path override (defaults to source project ./data/cyber-health.sqlite3).",
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
    parser.add_argument("--hermes-bin", type=str, default=_DEFAULT_BIN, help="Hermes CLI binary path override.")
    parser.add_argument("--hermes-home", type=str, default=None, help="Hermes home override (primarily for isolated testing).")
    parser.add_argument("--memory-vault", type=str, default=None, help="Explicit Obsidian Vault path to initialize and bind for health-manager memory.")
    parser.add_argument("--memory-project-id", type=str, default=None, help="Optional explicit health-manager project ID inside the selected Vault.")
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
        help="Do not use uv for virtualenv creation or package installation.",
    )
    parser.add_argument(
        "--editable",
        action="store_true",
        help="Install source package in editable development mode.",
    )
    parser.add_argument(
        "--skip-openclaw",
        action="store_true",
        help="Skip registering or updating OpenClaw MCP server configuration.",
    )
    parser.add_argument("--skip-codex", action="store_true", help="Skip Codex MCP registration.")
    parser.add_argument("--skip-hermes", action="store_true", help="Skip Hermes MCP registration.")
    return parser


def format_text_report(report: InstallReport) -> str:
    lines = [
        "============================================================",
        f"Cyber Health Agent Installer ({'DRY RUN' if report.dry_run else 'EXECUTE'})",
        "============================================================",
        f"Status       : {'SUCCESS' if report.success else 'FAILED'}",
        f"Version      : {report.version}",
        f"Source Root  : {report.source_project_root}",
        f"Target Dir   : {report.target_dir}",
        f"Venv Dir     : {report.venv_dir}",
        f"Message      : {report.message}",
        "",
        "--- Data & Storage ---",
        f"Action       : {report.data.action}",
        f"Source DB    : {report.data.source_db}",
        f"Target DB    : {report.data.target_db}",
        f"Source SHA   : {report.data.source_sha256 or 'N/A'}",
        f"Target SHA   : {report.data.target_sha256 or 'N/A'}",
        f"Reason       : {report.data.reason}",
        f"Executed     : {report.data.executed}",
        "",
        "--- Cyber Health Core Release ---",
        f"Action       : {report.core_release.action}",
        f"Version      : {report.core_release.version or 'N/A'}",
        f"Wheel        : {report.core_release.wheel_path or 'N/A'}",
        f"SHA-256      : {report.core_release.sha256 or 'N/A'}",
        f"Reason       : {report.core_release.reason}",
        "",
        "--- OpenClaw Integration ---",
        f"Server Name  : {report.openclaw.name}",
        f"Detected     : {report.openclaw.detected}",
        f"Action       : {report.openclaw.action}",
        f"Command      : {report.openclaw.command}",
        f"Args         : {' '.join(report.openclaw.args)}",
        f"Cwd          : {report.openclaw.cwd}",
        f"Reason       : {report.openclaw.reason}",
        f"Executed     : {report.openclaw.executed}",
        "",
        "--- Codex Integration ---",
        f"Server Name  : {report.codex.name}",
        f"Detected     : {report.codex.detected}",
        f"Action       : {report.codex.action}",
        f"Command      : {report.codex.command}",
        f"Args         : {' '.join(report.codex.args)}",
        f"Reason       : {report.codex.reason}",
        f"Executed     : {report.codex.executed}",
        "",
        "--- Hermes Integration ---",
        f"Server Name  : {report.hermes.name}",
        f"Detected     : {report.hermes.detected}",
        f"Action       : {report.hermes.action}",
        f"Command      : {report.hermes.command}",
        f"Args         : {' '.join(report.hermes.args)}",
        f"Probe        : {report.hermes.probe_verified}",
        f"Tool Count   : {report.hermes.tool_count or 'N/A'}",
        f"Reason       : {report.hermes.reason}",
        f"Executed     : {report.hermes.executed}",
        "",
        "--- Obsidian Memory Plugin Release ---",
        f"Action       : {report.memory_plugin.action}",
        f"Version      : {report.memory_plugin.version or 'N/A'}",
        f"Archive      : {report.memory_plugin.archive_path or 'N/A'}",
        f"SHA-256      : {report.memory_plugin.sha256 or 'N/A'}",
        f"Reason       : {report.memory_plugin.reason}",
        f"Executed     : {report.memory_plugin.executed}",
        "",
        "--- Health-Manager Long-Term Memory ---",
        f"State        : {report.memory.state}",
        f"Plugin       : {report.memory.plugin_id} (loaded={report.memory.plugin_loaded})",
        f"Vault        : {report.memory.vault_path or 'N/A'}",
        f"Project      : {report.memory.project_id or 'N/A'}",
        f"Reason       : {report.memory.reason}",
        *[f"Warning      : {warning}" for warning in report.memory.warnings],
        "",
        "--- Memory Bootstrap ---",
        f"Requested    : {report.memory_bootstrap.requested}",
        f"Obsidian     : {report.memory_bootstrap.obsidian_action}",
        f"Obsidian App : {report.memory_bootstrap.obsidian_app_path or 'N/A'}",
        f"Vault Action : {report.memory_bootstrap.vault_action}",
        f"Plugin Action: {report.memory_bootstrap.plugin_action}",
        f"Config Action: {report.memory_bootstrap.config_action}",
        f"Executed     : {report.memory_bootstrap.executed}",
        f"Reason       : {report.memory_bootstrap.reason}",
        "",
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
        installer = CyberHealthInstaller(
            project_root=args.project_root,
            target_dir=args.target_dir,
            source_db=args.db,
            openclaw_bin=args.openclaw_bin,
            openclaw_config=args.openclaw_config,
            openclaw_state_dir=args.openclaw_state_dir,
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            hermes_bin=args.hermes_bin,
            hermes_home=args.hermes_home,
            memory_vault=args.memory_vault,
            memory_project_id=args.memory_project_id,
            dry_run=args.dry_run,
            use_uv=not args.no_uv,
            editable=args.editable,
            skip_openclaw=args.skip_openclaw,
            skip_codex=args.skip_codex,
            skip_hermes=args.skip_hermes,
        )
        report = installer.run()
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
