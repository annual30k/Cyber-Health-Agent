"""Production-safe Cyber Health Agent uninstaller.

Unregisters owned host integrations (OpenClaw/Codex/Hermes MCP entries, LaunchAgent) while
preserving database, WAL/SHM, exports, source, .venv, and user data by default.
Guarantees strict non-interference with obsidian-memory, Obsidian Vaults,
and unrelated host state.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
from typing import Any

from .codex_integration import (
    CodexRegistrationStatus,
    find_codex_cli,
    inspect_codex_registration,
    remove_codex_registration,
)
from .hermes_integration import (
    HermesRegistrationStatus,
    find_hermes_cli,
    inspect_hermes_registration,
    remove_hermes_registration,
)

PURGE_CONFIRMATION_TOKEN = "DELETE_CYBER_HEALTH_DATA"
FOREIGN_UNSET_CONFIRMATION_TOKEN = "UNSET_FOREIGN_CYBER_HEALTH"
FIXED_OPENCLAW_SERVER_NAME = "cyber-health"
DEFAULT_LAUNCHAGENT_LABEL = "ai.cyber-health.agent"
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


class UninstallerError(Exception):
    """Base exception for uninstaller errors."""


class OwnershipVerificationError(UninstallerError):
    """Raised when host integration ownership cannot be proven."""


class SafetyBoundaryError(UninstallerError):
    """Raised when an operation attempts to touch a protected resource."""


class PurgeValidationError(UninstallerError):
    """Raised when purge targets violate strict path and security constraints."""


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
    """Verifies that resolved path is strictly inside parent_dir using ancestor check."""
    try:
        res_path = path.resolve()
        res_parent = parent_dir.resolve()
        return res_parent in res_path.parents
    except Exception:
        return False


def verify_cyber_health_command_signature(command: str | None, args: list[str] | None) -> bool:
    """Verifies recognized command signature for Cyber Health MCP server."""
    if not command:
        return False
    cmd_name = Path(command).name.lower()
    if cmd_name in ("cyber-health-mcp", "cyber-health-mcp.exe", "cyber-health-mcp.cmd"):
        return True
    if cmd_name.startswith("python"):
        if args and isinstance(args, list):
            for i, arg in enumerate(args):
                if arg == "-m" and i + 1 < len(args) and args[i + 1] == "cyber_health_mcp":
                    return True
                if arg == "cyber_health_mcp" and i > 0 and args[i - 1] == "-m":
                    return True
    return False


@dataclass
class HostIntegrationStatus:
    name: str
    detected: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    ownership_proven: bool = False
    action: str = "none"  # "none", "unset", "unload_and_remove", "refused", "error"
    reason: str = ""
    executed: bool = False


@dataclass
class DataPreservationStatus:
    purge_requested: bool = False
    purge_confirmed: bool = False
    preserved_paths: list[str] = field(default_factory=list)
    purged_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Metadata for TOCTOU validation:
    # path_str -> (device, inode, size, mtime_ns, ctime_ns, resolved_path)
    targets_meta: dict[str, tuple[int, int, int, int, int, str]] = field(default_factory=dict)


@dataclass
class UninstallReport:
    dry_run: bool
    success: bool
    project_root: str
    openclaw: HostIntegrationStatus
    codex: CodexRegistrationStatus
    hermes: HermesRegistrationStatus
    launchagent: HostIntegrationStatus
    data: DataPreservationStatus
    protected_boundaries: dict[str, bool] = field(
        default_factory=lambda: {
            "obsidian_memory_preserved": True,
            "obsidian_vaults_preserved": True,
            "unrelated_codex_state_preserved": True,
            "unrelated_hermes_state_preserved": True,
            "source_and_venv_preserved": True,
        }
    )
    message: str = ""


_DEFAULT_BIN = object()


class CyberHealthUninstaller:
    def __init__(
        self,
        project_root: Path | str | None = None,
        db_path: Path | str | None = None,
        openclaw_bin: str | None | object = _DEFAULT_BIN,
        openclaw_config: Path | str | None = None,
        openclaw_state_dir: Path | str | None = None,
        codex_bin: str | None | object = _DEFAULT_BIN,
        codex_home: Path | str | None = None,
        hermes_bin: str | None | object = _DEFAULT_BIN,
        hermes_home: Path | str | None = None,
        launchagent_dir: Path | str | None = None,
        launchagent_label: str = DEFAULT_LAUNCHAGENT_LABEL,
        force_foreign_host_mcp: bool = False,
        confirm_foreign_unset: str | None = None,
        dry_run: bool = False,
        purge_data: bool = False,
        confirm_purge: str | None = None,
    ):
        # Raw paths preserved for symlink checking
        if project_root is not None:
            self._raw_project_root = Path(project_root)
        else:
            venv_prefix = Path(sys.prefix).resolve()
            default_target = (Path.home() / DEFAULT_INSTALL_DIR_NAME).resolve()
            if (venv_prefix.parent / "config" / "installation.json").is_file():
                self._raw_project_root = venv_prefix.parent
            elif default_target.is_dir() and (default_target / "config" / "installation.json").is_file():
                self._raw_project_root = default_target
            elif default_target.is_dir() and (default_target / "data").is_dir():
                self._raw_project_root = default_target
            else:
                self._raw_project_root = Path(__file__).resolve().parents[1]

        if has_symlink_in_path(self._raw_project_root):
            raise SafetyBoundaryError(f"Symlinked project root rejected: {self._raw_project_root}")

        self.project_root = self._raw_project_root.resolve()
        self._validate_project_root(self.project_root)

        if db_path is not None:
            self._raw_db_path = Path(db_path)
        elif "CYBER_HEALTH_DB" in os.environ:
            self._raw_db_path = Path(os.environ["CYBER_HEALTH_DB"])
        else:
            self._raw_db_path = self.project_root / "data" / "cyber-health.sqlite3"

        if has_symlink_in_path(self._raw_db_path):
            raise PurgeValidationError(f"Symlinked database path rejected: {self._raw_db_path}")

        self.db_path = self._raw_db_path.resolve()

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

        if launchagent_dir is not None:
            self.launchagent_dir = Path(launchagent_dir)
        else:
            self.launchagent_dir = Path.home() / "Library" / "LaunchAgents"

        if launchagent_label != DEFAULT_LAUNCHAGENT_LABEL:
            raise SafetyBoundaryError(
                f"LaunchAgent label is fixed to '{DEFAULT_LAUNCHAGENT_LABEL}'; custom labels are prohibited."
            )
        self.launchagent_label = DEFAULT_LAUNCHAGENT_LABEL

        self.force_foreign_host_mcp = force_foreign_host_mcp
        self.confirm_foreign_unset = confirm_foreign_unset
        self.dry_run = dry_run
        self.purge_data = purge_data
        self.confirm_purge = confirm_purge
        self.installed_root = (Path.home() / DEFAULT_INSTALL_DIR_NAME).resolve()

    def _validate_project_root(self, root: Path) -> None:
        if root in SYSTEM_BROAD_PATHS or root == Path.home():
            raise SafetyBoundaryError(f"Broad or system project root rejected: {root}")
        if len(root.parts) <= 2:
            raise SafetyBoundaryError(f"Root path too shallow rejected: {root}")

    def get_openclaw_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.openclaw_config:
            env["OPENCLAW_CONFIG_PATH"] = str(self.openclaw_config)
        if self.openclaw_state_dir:
            env["OPENCLAW_STATE_DIR"] = str(self.openclaw_state_dir)
        return env

    @staticmethod
    def _json_fingerprint(value: Any) -> str:
        """Return a stable digest without exposing the inspected configuration."""
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def inspect_openclaw(self) -> HostIntegrationStatus:
        status = HostIntegrationStatus(name=FIXED_OPENCLAW_SERVER_NAME)
        if not self.openclaw_bin:
            status.reason = "OpenClaw binary not found (skipped host check)"
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

        if result.returncode != 0:
            # Check for EXACT documented absent server indication
            if (
                'No MCP server named "cyber-health"' in combined_output
                or 'No MCP server named \\"cyber-health\\"' in combined_output
                or "No MCP server named 'cyber-health'" in combined_output
            ):
                status.detected = False
                status.action = "none"
                status.reason = "Not registered in OpenClaw (clean state / no-op)"
                return status

            # Any other non-zero exit is an inspection error (permission, invalid config, crash).
            # IMPORTANT: Never include raw stdout/stderr to prevent secret leakage.
            status.detected = True
            status.action = "error"
            status.reason = (
                f"OpenClaw CLI inspection failed with exit code {result.returncode} (CLI returned non-zero status)"
            )
            return status

        # Status returned 0: parse JSON
        status.detected = True
        try:
            raw_data = json.loads(result.stdout)
        except json.JSONDecodeError:
            status.action = "error"
            status.reason = "OpenClaw CLI inspection failed: malformed JSON response"
            return status

        if not isinstance(raw_data, dict):
            status.action = "error"
            status.reason = "OpenClaw CLI inspection failed: invalid response structure"
            return status

        # Sanitize details: never leak raw environment or unrelated host config
        sanitized_details = {
            "command": str(raw_data.get("command", "")),
            "args": [str(a) for a in raw_data.get("args", [])]
            if isinstance(raw_data.get("args"), list)
            else [],
            "cwd": str(raw_data.get("cwd", "")),
            "has_db_env": bool((raw_data.get("env") or {}).get("CYBER_HEALTH_DB")),
            "state_fingerprint": self._json_fingerprint(raw_data),
        }
        status.details = sanitized_details

        ownership = self.verify_openclaw_ownership(raw_data)
        status.ownership_proven = ownership

        if ownership:
            status.action = "unset"
            status.reason = "Verified project ownership matching command signature and path bindings"
        elif self.force_foreign_host_mcp:
            if self.confirm_foreign_unset == FOREIGN_UNSET_CONFIRMATION_TOKEN:
                status.action = "unset"
                status.reason = (
                    "Foreign registration override confirmed with "
                    f"--confirm-foreign-unset {FOREIGN_UNSET_CONFIRMATION_TOKEN}"
                )
            else:
                status.action = "refused"
                status.reason = (
                    "--force-foreign-host-mcp requires exact confirmation token: "
                    f"--confirm-foreign-unset {FOREIGN_UNSET_CONFIRMATION_TOKEN}"
                )
        else:
            status.action = "refused"
            status.reason = (
                "Ambiguous or unrelated registration: server command/cwd/database arguments "
                f"do not resolve to this installation ({self.project_root})"
            )

        return status

    def verify_openclaw_ownership(self, details: dict[str, Any]) -> bool:
        if not isinstance(details, dict):
            return False

        command = details.get("command")
        args = details.get("args") or []
        if not isinstance(args, list):
            args = []

        # 1. Recognized Cyber Health command signature is strictly required
        if not verify_cyber_health_command_signature(command, args):
            return False

        # Candidate roots: project root and standard installed root
        candidate_roots = [self.project_root]
        if hasattr(self, "installed_root") and self.installed_root.exists():
            candidate_roots.append(self.installed_root)

        # 2. At least one exact resolved binding pointing to this installation
        cwd = details.get("cwd")
        if cwd:
            raw_cwd = Path(cwd)
            if not has_symlink_in_path(raw_cwd):
                res_cwd = raw_cwd.resolve()
                for root in candidate_roots:
                    if res_cwd == root or root in res_cwd.parents:
                        return True

        if command:
            raw_cmd = Path(command)
            if not has_symlink_in_path(raw_cmd):
                res_cmd = raw_cmd.resolve()
                for root in candidate_roots:
                    if root in res_cmd.parents:
                        return True

        if args and isinstance(args, list):
            for i, arg in enumerate(args):
                if arg == "--db" and i + 1 < len(args):
                    raw_arg_db = Path(args[i + 1])
                    if not has_symlink_in_path(raw_arg_db):
                        res_arg_db = raw_arg_db.resolve()
                        for root in candidate_roots:
                            if res_arg_db == self.db_path or root in res_arg_db.parents:
                                return True

        env_vars = details.get("env") or {}
        if isinstance(env_vars, dict):
            db_env = env_vars.get("CYBER_HEALTH_DB")
            if db_env:
                raw_db = Path(db_env)
                if not has_symlink_in_path(raw_db):
                    res_db = raw_db.resolve()
                    for root in candidate_roots:
                        if res_db == self.db_path or root in res_db.parents:
                            return True

        return False

    def unregister_openclaw(self, status: HostIntegrationStatus) -> None:
        if status.action != "unset":
            return

        if self.dry_run:
            return

        if not self.openclaw_bin:
            return

        # TOCTOU Defense: Re-verify OpenClaw state immediately before unsetting
        fresh_status = self.inspect_openclaw()
        if (
            fresh_status.action != "unset"
            or fresh_status.details.get("state_fingerprint")
            != status.details.get("state_fingerprint")
        ):
            raise OwnershipVerificationError(
                "OpenClaw registration state changed or ownership no longer proven immediately before unsetting"
            )

        cmd = [self.openclaw_bin, "mcp", "unset", FIXED_OPENCLAW_SERVER_NAME]
        result = subprocess.run(
            cmd,
            env=self.get_openclaw_env(),
            capture_output=True,
            text=True,
            timeout=20,
            shell=False,
        )

        if result.returncode == 0:
            status.executed = True
            status.reason = f"Successfully unset MCP server {FIXED_OPENCLAW_SERVER_NAME}"
        else:
            err = (result.stderr + " " + result.stdout).strip()
            if "No MCP server named" in err or "not found" in err.lower():
                status.executed = True
                status.reason = "Already absent from OpenClaw (no-op)"
            else:
                # Sanitized error message: never leak raw output
                raise UninstallerError(
                    f"Failed to unregister OpenClaw MCP server {FIXED_OPENCLAW_SERVER_NAME} (CLI exit code {result.returncode})"
                )

    def inspect_codex(self) -> CodexRegistrationStatus:
        status = inspect_codex_registration(
            self.codex_bin,
            self.installed_root,
            self.installed_root / "data" / "cyber-health.sqlite3",
            codex_home=self.codex_home,
        )
        if status.action == "update":
            status.action = "remove"
            status.reason = "Verified owned Cyber Health Codex MCP registration"
        return status

    def unregister_codex(self, status: CodexRegistrationStatus) -> None:
        try:
            remove_codex_registration(
                self.codex_bin,
                status,
                self.installed_root,
                self.installed_root / "data" / "cyber-health.sqlite3",
                codex_home=self.codex_home,
                dry_run=self.dry_run,
            )
        except RuntimeError as exc:
            raise OwnershipVerificationError(str(exc)) from exc

    def inspect_hermes(self) -> HermesRegistrationStatus:
        status = inspect_hermes_registration(
            self.hermes_bin,
            self.installed_root,
            self.installed_root / "data" / "cyber-health.sqlite3",
            hermes_home=self.hermes_home,
        )
        if status.action == "verify":
            status.action = "remove"
            status.reason = "Verified owned Cyber Health Hermes MCP registration"
        return status

    def unregister_hermes(self, status: HermesRegistrationStatus) -> None:
        try:
            remove_hermes_registration(
                self.hermes_bin,
                status,
                self.installed_root,
                self.installed_root / "data" / "cyber-health.sqlite3",
                hermes_home=self.hermes_home,
                dry_run=self.dry_run,
            )
        except RuntimeError as exc:
            raise OwnershipVerificationError(str(exc)) from exc

    def inspect_launchagent(self) -> HostIntegrationStatus:
        status = HostIntegrationStatus(name=self.launchagent_label)
        plist_path = self.launchagent_dir / f"{self.launchagent_label}.plist"
        status.details = {"path": str(plist_path)}

        if not plist_path.exists():
            status.detected = False
            status.action = "none"
            status.reason = "LaunchAgent plist file does not exist (clean state / no-op)"
            return status

        if has_symlink_in_path(plist_path):
            status.detected = True
            status.action = "error"
            status.reason = f"Symlinked LaunchAgent plist rejected: {plist_path}"
            return status

        status.detected = True
        try:
            with open(plist_path, "rb") as f:
                plist_data = plistlib.load(f)
        except Exception:
            status.action = "error"
            status.reason = "Failed to parse LaunchAgent property list file"
            return status

        status.details["state_fingerprint"] = self._json_fingerprint(plist_data)

        ownership = self.verify_launchagent_ownership(plist_data)
        status.ownership_proven = ownership

        if ownership:
            status.action = "unload_and_remove"
            status.reason = "Verified project ownership matching label and paths"
        else:
            status.action = "refused"
            status.reason = "LaunchAgent does not reference this project installation"

        return status

    def verify_launchagent_ownership(self, plist: dict[str, Any]) -> bool:
        if not isinstance(plist, dict):
            return False

        if plist.get("Label") != self.launchagent_label:
            return False

        prog = plist.get("Program")
        args = plist.get("ProgramArguments") or []
        if not isinstance(args, list):
            args = []

        if not verify_cyber_health_command_signature(prog or (args[0] if args else None), args):
            return False

        cwd = plist.get("WorkingDirectory")
        if cwd:
            raw_cwd = Path(cwd)
            if not has_symlink_in_path(raw_cwd):
                if raw_cwd.resolve() == self.project_root:
                    return True

        if prog:
            raw_prog = Path(prog)
            if not has_symlink_in_path(raw_prog):
                if is_strictly_inside_dir(raw_prog, self.project_root):
                    return True

        if args:
            raw_arg0 = Path(args[0])
            if not has_symlink_in_path(raw_arg0):
                if is_strictly_inside_dir(raw_arg0, self.project_root):
                    return True

        return False

    def remove_launchagent(self, status: HostIntegrationStatus) -> None:
        if status.action != "unload_and_remove":
            return

        plist_path = self.launchagent_dir / f"{self.launchagent_label}.plist"

        if self.dry_run:
            return

        # TOCTOU Defense: Re-verify LaunchAgent state immediately before removal
        fresh_status = self.inspect_launchagent()
        if (
            fresh_status.action != "unload_and_remove"
            or not fresh_status.ownership_proven
            or fresh_status.details.get("state_fingerprint")
            != status.details.get("state_fingerprint")
        ):
            raise OwnershipVerificationError(
                "LaunchAgent file changed or ownership no longer proven immediately before removal"
            )

        launchctl = shutil.which("launchctl")
        if launchctl:
            uid = os.getuid()
            subprocess.run(
                [launchctl, "bootout", f"gui/{uid}", str(plist_path)],
                capture_output=True,
                text=True,
                shell=False,
            )
            subprocess.run(
                [launchctl, "unload", str(plist_path)],
                capture_output=True,
                text=True,
                shell=False,
            )

        if plist_path.exists():
            if has_symlink_in_path(plist_path):
                raise SafetyBoundaryError(
                    f"LaunchAgent plist was swapped to a symlink before removal: {plist_path}"
                )
            try:
                plist_path.unlink()
                status.executed = True
                status.reason = f"Successfully unloaded and removed {plist_path.name}"
            except Exception:
                raise UninstallerError(f"Failed to remove LaunchAgent file {plist_path.name}")

    def collect_approved_data_targets(self) -> list[Path]:
        """Collects strictly approved Cyber Health data targets (never arbitrary files)."""
        targets: list[Path] = []
        if self.db_path.exists() and self.db_path.is_file() and not self.db_path.is_symlink():
            targets.append(self.db_path)

        wal = self.db_path.parent / (self.db_path.name + "-wal")
        if wal.exists() and wal.is_file() and not wal.is_symlink():
            targets.append(wal)

        shm = self.db_path.parent / (self.db_path.name + "-shm")
        if shm.exists() and shm.is_file() and not shm.is_symlink():
            targets.append(shm)

        # Documented exports/snapshots only (exact file patterns)
        data_dir = self.project_root / "data"
        if data_dir.exists() and data_dir.is_dir() and not has_symlink_in_path(data_dir):
            for child in sorted(data_dir.iterdir()):
                if child.is_file() and not child.is_symlink():
                    name = child.name
                    if (name.startswith("export_") or name.startswith("snapshot_")) and name.endswith(".json"):
                        if child not in targets:
                            targets.append(child)

        return targets

    def validate_purge_candidate(self, target: Path) -> Path:
        """Strictly validates a purge target against symlinks, traversal, and broad boundaries."""
        if has_symlink_in_path(target):
            raise PurgeValidationError(f"Symlink or symlinked ancestor rejected: {target}")

        try:
            st = os.lstat(target)
        except OSError as exc:
            raise PurgeValidationError(f"Target cannot be accessed: {exc}")

        if stat.S_ISLNK(st.st_mode):
            raise PurgeValidationError(f"Symlink rejected for purge: {target}")

        if not stat.S_ISREG(st.st_mode):
            raise PurgeValidationError(f"Only regular files may be purged: {target}")

        resolved = target.resolve()

        if resolved in SYSTEM_BROAD_PATHS or resolved == Path.home():
            raise PurgeValidationError(f"Broad system path rejected for purge: {resolved}")

        if len(resolved.parts) <= 2:
            raise PurgeValidationError(f"Shallow root path rejected for purge: {resolved}")

        # Protect cross-project and system components
        path_str_lower = str(resolved).lower()
        for protected in PROTECTED_NAMES:
            if protected in path_str_lower:
                raise SafetyBoundaryError(
                    f"Path contains protected keyword '{protected}': {resolved}"
                )

        # Candidate must be strictly inside this project root
        if not is_strictly_inside_dir(resolved, self.project_root):
            raise PurgeValidationError(
                f"Path escapes approved project data boundaries: {resolved}"
            )

        return resolved

    def _trash_or_delete_file(self, path: Path) -> str:
        """Moves file to recoverable trash if practical; never reads or logs contents."""
        user_trash = Path.home() / ".Trash"
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        dest_name = f"cyber_health_{path.name}_{ts}"

        if (
            user_trash.exists()
            and os.access(user_trash, os.W_OK)
            and not has_symlink_in_path(user_trash)
        ):
            target_dest = user_trash / dest_name
            try:
                shutil.move(str(path), str(target_dest))
                return f"moved to {target_dest}"
            except Exception:
                pass

        # Local project .trash fallback with strict symlink and containment checks
        local_trash = self.project_root / ".trash"
        if has_symlink_in_path(local_trash):
            raise PurgeValidationError(
                f"Project-local trash directory is or contains a symlink: {local_trash}"
            )
        if not is_strictly_inside_dir(local_trash, self.project_root):
            raise PurgeValidationError(
                f"Project-local trash directory escapes project root: {local_trash}"
            )

        try:
            local_trash.mkdir(parents=True, exist_ok=True)
            if local_trash.is_symlink() or has_symlink_in_path(local_trash):
                raise PurgeValidationError("Symlinked local trash rejected")
            target_dest = local_trash / dest_name
            if has_symlink_in_path(target_dest) or not is_strictly_inside_dir(target_dest, self.project_root):
                raise PurgeValidationError("Local trash destination escapes project root")
            shutil.move(str(path), str(target_dest))
            return f"moved to {target_dest}"
        except PurgeValidationError:
            raise
        except Exception:
            pass

        # Final fallback: unlink file only
        path.unlink()
        return "unlinked file"

    def plan_data(self) -> DataPreservationStatus:
        """Prevalidates data actions; performs zero mutations."""
        status = DataPreservationStatus()
        approved_targets = self.collect_approved_data_targets()

        if not self.purge_data:
            status.purge_requested = False
            status.purge_confirmed = False
            status.preserved_paths = [str(p) for p in approved_targets]
            return status

        # Purge requested
        status.purge_requested = True

        # Requirement 3: Refuse destructive purge when OpenClaw host inspection is unavailable
        if not self.openclaw_bin:
            msg = (
                "--purge-data is refused: OpenClaw CLI is not available to verify "
                "and unregister active host integrations"
            )
            status.errors.append(msg)
            return status

        if self.confirm_purge != PURGE_CONFIRMATION_TOKEN:
            status.purge_confirmed = False
            msg = (
                f"--purge-data requires exact confirmation token: "
                f"--confirm-purge {PURGE_CONFIRMATION_TOKEN}"
            )
            status.errors.append(msg)
            return status

        status.purge_confirmed = True

        for target in approved_targets:
            try:
                val = self.validate_purge_candidate(target)
                st = os.lstat(val)
                status.purged_paths.append(str(val))
                status.targets_meta[str(val)] = (
                    st.st_dev,
                    st.st_ino,
                    st.st_size,
                    st.st_mtime_ns,
                    st.st_ctime_ns,
                    str(val.resolve()),
                )
            except Exception as exc:
                status.errors.append(str(exc))

        return status

    def execute_data_purge(self, data_status: DataPreservationStatus) -> None:
        """Executes purge of prevalidated data files with TOCTOU defenses; never prints or reads health contents."""
        if not self.purge_data or not data_status.purge_confirmed or self.dry_run:
            return

        executed_purged: list[str] = []
        for target_str in data_status.purged_paths:
            p = Path(target_str)
            if not p.exists():
                continue

            # TOCTOU Defense: re-validate immediately before moving/unlinking
            if has_symlink_in_path(p) or os.path.islink(p):
                raise PurgeValidationError(
                    f"Target was modified or swapped to a symlink before purge execution: {p}"
                )

            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                raise PurgeValidationError(
                    f"Target is no longer a regular file before purge execution: {p}"
                )

            # Re-validate candidate path rules
            self.validate_purge_candidate(p)

            # Verify device, inode, and resolved identity
            expected = data_status.targets_meta.get(target_str)
            if expected:
                exp_dev, exp_ino, exp_size, exp_mtime_ns, exp_ctime_ns, exp_resolved = expected
                if (
                    st.st_dev != exp_dev
                    or st.st_ino != exp_ino
                    or st.st_size != exp_size
                    or st.st_mtime_ns != exp_mtime_ns
                    or st.st_ctime_ns != exp_ctime_ns
                    or str(p.resolve()) != exp_resolved
                ):
                    raise PurgeValidationError(
                        f"Target file was replaced or swapped after planning phase: {p}"
                    )

            desc = self._trash_or_delete_file(p)
            executed_purged.append(f"{p} ({desc})")

        data_status.purged_paths = executed_purged

    def preflight_execution(
        self,
        openclaw_status: HostIntegrationStatus,
        codex_status: CodexRegistrationStatus,
        hermes_status: HermesRegistrationStatus,
        launchagent_status: HostIntegrationStatus,
        data_status: DataPreservationStatus,
    ) -> None:
        """Revalidate the complete plan once before the first mutation."""
        if openclaw_status.action == "unset":
            fresh_openclaw = self.inspect_openclaw()
            if (
                fresh_openclaw.action != "unset"
                or fresh_openclaw.details.get("state_fingerprint")
                != openclaw_status.details.get("state_fingerprint")
            ):
                raise OwnershipVerificationError(
                    "OpenClaw registration changed after planning; refusing all mutations"
                )

        if codex_status.action == "remove":
            fresh_codex = self.inspect_codex()
            if (
                fresh_codex.action != "remove"
                or fresh_codex.state_fingerprint != codex_status.state_fingerprint
            ):
                raise OwnershipVerificationError(
                    "Codex registration changed after planning; refusing all mutations"
                )

        if hermes_status.action == "remove":
            fresh_hermes = self.inspect_hermes()
            if (
                fresh_hermes.action != "remove"
                or fresh_hermes.state_fingerprint != hermes_status.state_fingerprint
            ):
                raise OwnershipVerificationError(
                    "Hermes registration changed after planning; refusing all mutations"
                )

        if launchagent_status.action == "unload_and_remove":
            fresh_launchagent = self.inspect_launchagent()
            if (
                fresh_launchagent.action != "unload_and_remove"
                or not fresh_launchagent.ownership_proven
                or fresh_launchagent.details.get("state_fingerprint")
                != launchagent_status.details.get("state_fingerprint")
            ):
                raise OwnershipVerificationError(
                    "LaunchAgent changed after planning; refusing all mutations"
                )

        if self.purge_data and data_status.purge_confirmed:
            for target_str in data_status.purged_paths:
                target = Path(target_str)
                expected = data_status.targets_meta.get(target_str)
                if not target.exists() or expected is None:
                    raise PurgeValidationError(
                        f"Purge target changed after planning: {target}"
                    )
                validated = self.validate_purge_candidate(target)
                st = os.lstat(validated)
                current = (
                    st.st_dev,
                    st.st_ino,
                    st.st_size,
                    st.st_mtime_ns,
                    st.st_ctime_ns,
                    str(validated.resolve()),
                )
                if current != expected:
                    raise PurgeValidationError(
                        f"Purge target changed after planning: {target}"
                    )

    def run(self) -> UninstallReport:
        # Phase 1: Planning and inspection (strictly read-only)
        openclaw_status = self.inspect_openclaw()
        codex_status = self.inspect_codex()
        hermes_status = self.inspect_hermes()
        launchagent_status = self.inspect_launchagent()
        data_plan = self.plan_data()

        # Phase 2: Fail-closed validation check
        refusal_reasons: list[str] = []
        if openclaw_status.action in ("error", "refused"):
            refusal_reasons.append(f"OpenClaw registration refused: {openclaw_status.reason}")
        if codex_status.action in ("error", "refused"):
            refusal_reasons.append(f"Codex registration refused: {codex_status.reason}")
        if hermes_status.action in ("error", "refused"):
            refusal_reasons.append(f"Hermes registration refused: {hermes_status.reason}")
        if launchagent_status.action in ("error", "refused"):
            refusal_reasons.append(f"LaunchAgent removal refused: {launchagent_status.reason}")
        if data_plan.errors:
            refusal_reasons.extend(data_plan.errors)

        if refusal_reasons:
            # Zero mutations performed
            return UninstallReport(
                dry_run=self.dry_run,
                success=False,
                project_root=str(self.project_root),
                openclaw=openclaw_status,
                codex=codex_status,
                hermes=hermes_status,
                launchagent=launchagent_status,
                data=data_plan,
                message="; ".join(refusal_reasons),
            )

        # Phase 3: Dry-run check
        if self.dry_run:
            return UninstallReport(
                dry_run=True,
                success=True,
                project_root=str(self.project_root),
                openclaw=openclaw_status,
                codex=codex_status,
                hermes=hermes_status,
                launchagent=launchagent_status,
                data=data_plan,
                message="Dry run completed successfully (zero mutations)",
            )

        # Phase 4: Execution
        self.preflight_execution(
            openclaw_status, codex_status, hermes_status, launchagent_status, data_plan
        )
        self.unregister_openclaw(openclaw_status)
        self.unregister_codex(codex_status)
        self.unregister_hermes(hermes_status)
        self.remove_launchagent(launchagent_status)
        self.execute_data_purge(data_plan)

        return UninstallReport(
            dry_run=False,
            success=True,
            project_root=str(self.project_root),
            openclaw=openclaw_status,
            codex=codex_status,
            hermes=hermes_status,
            launchagent=launchagent_status,
            data=data_plan,
            message="Uninstallation completed successfully",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cyber-health-uninstall",
        description="Production-safe Cyber Health Agent uninstaller.",
    )
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
        "--purge-data",
        action="store_true",
        help="Opt-in to remove Cyber Health database and data files.",
    )
    parser.add_argument(
        "--confirm-purge",
        type=str,
        default=None,
        help=f"Required verification token for --purge-data (must be '{PURGE_CONFIRMATION_TOKEN}').",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Database path override (defaults to CYBER_HEALTH_DB or ./data/cyber-health.sqlite3).",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="Project repository root override (defaults to repository containing this package).",
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
        help="OpenClaw config path override (sets OPENCLAW_CONFIG_PATH).",
    )
    parser.add_argument(
        "--openclaw-state-dir",
        type=str,
        default=None,
        help="OpenClaw state directory override (sets OPENCLAW_STATE_DIR).",
    )
    parser.add_argument("--codex-bin", type=str, default=_DEFAULT_BIN, help="Codex CLI binary path override.")
    parser.add_argument("--codex-home", type=str, default=None, help="Codex home override (primarily for isolated testing).")
    parser.add_argument("--hermes-bin", type=str, default=_DEFAULT_BIN, help="Hermes CLI binary path override.")
    parser.add_argument("--hermes-home", type=str, default=None, help="Hermes home override (primarily for isolated testing).")
    parser.add_argument(
        "--launchagent-dir",
        type=str,
        default=None,
        help="LaunchAgent directory override (default: ~/Library/LaunchAgents).",
    )
    parser.add_argument(
        "--force-foreign-host-mcp",
        action="store_true",
        help="Recovery-only override to unset OpenClaw MCP server even if ownership paths are ambiguous (requires --confirm-foreign-unset).",
    )
    parser.add_argument(
        "--confirm-foreign-unset",
        type=str,
        default=None,
        help=f"Mandatory confirmation token when using --force-foreign-host-mcp (must be '{FOREIGN_UNSET_CONFIRMATION_TOKEN}').",
    )
    return parser


def format_text_report(report: UninstallReport) -> str:
    lines = [
        "============================================================",
        f"Cyber Health Agent Uninstaller ({'DRY RUN' if report.dry_run else 'EXECUTE'})",
        "============================================================",
        f"Status       : {'SUCCESS' if report.success else 'FAILED'}",
        f"Project Root : {report.project_root}",
        f"Message      : {report.message}",
        "",
        "--- Host Integrations ---",
        f"OpenClaw MCP Server ({report.openclaw.name}):",
        f"  Detected        : {report.openclaw.detected}",
        f"  Ownership Proven: {report.openclaw.ownership_proven}",
        f"  Action          : {report.openclaw.action}",
        f"  Reason          : {report.openclaw.reason}",
        f"  Executed        : {report.openclaw.executed}",
        "",
        f"Codex MCP Server ({report.codex.name}):",
        f"  Detected        : {report.codex.detected}",
        f"  Ownership Proven: {report.codex.ownership_proven}",
        f"  Action          : {report.codex.action}",
        f"  Reason          : {report.codex.reason}",
        f"  Executed        : {report.codex.executed}",
        "",
        f"Hermes MCP Server ({report.hermes.name}):",
        f"  Detected        : {report.hermes.detected}",
        f"  Ownership Proven: {report.hermes.ownership_proven}",
        f"  Action          : {report.hermes.action}",
        f"  Reason          : {report.hermes.reason}",
        f"  Executed        : {report.hermes.executed}",
        "",
        f"LaunchAgent ({report.launchagent.name}):",
        f"  Detected        : {report.launchagent.detected}",
        f"  Ownership Proven: {report.launchagent.ownership_proven}",
        f"  Action          : {report.launchagent.action}",
        f"  Reason          : {report.launchagent.reason}",
        f"  Executed        : {report.launchagent.executed}",
        "",
        "--- Data & Storage ---",
        f"Purge Requested  : {report.data.purge_requested}",
        f"Purge Confirmed  : {report.data.purge_confirmed}",
    ]

    if report.data.preserved_paths:
        lines.append("Preserved Paths (Protected from modification):")
        for p in report.data.preserved_paths:
            lines.append(f"  + {p}")

    if report.data.purged_paths:
        lines.append("Purged / Trashed Paths:")
        for p in report.data.purged_paths:
            lines.append(f"  - {p}")

    if report.data.errors:
        lines.append("Data Errors:")
        for err in report.data.errors:
            lines.append(f"  ! {err}")

    lines.extend([
        "",
        "--- Protected Boundaries ---",
        "  + obsidian-memory: STRICTLY PRESERVED (Not a Cyber Health component)",
        "  + Obsidian Vaults: STRICTLY PRESERVED (Untouched)",
        "  + Codex Config:    ONLY cyber-health MCP entry managed; unrelated state preserved",
        "  + Hermes Config:   ONLY cyber-health MCP entry managed; unrelated state preserved",
        "  + Source & .venv:  STRICTLY PRESERVED (Untouched)",
        "============================================================",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        uninstaller = CyberHealthUninstaller(
            project_root=args.project_root,
            db_path=args.db,
            openclaw_bin=args.openclaw_bin,
            openclaw_config=args.openclaw_config,
            openclaw_state_dir=args.openclaw_state_dir,
            codex_bin=args.codex_bin,
            codex_home=args.codex_home,
            hermes_bin=args.hermes_bin,
            hermes_home=args.hermes_home,
            launchagent_dir=args.launchagent_dir,
            force_foreign_host_mcp=args.force_foreign_host_mcp,
            confirm_foreign_unset=args.confirm_foreign_unset,
            dry_run=args.dry_run,
            purge_data=args.purge_data,
            confirm_purge=args.confirm_purge,
        )
        report = uninstaller.run()
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
