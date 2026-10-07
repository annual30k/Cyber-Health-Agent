"""Local housekeeping for an installation: private permissions, backup retention, temp leftovers.

Health facts live in plain SQLite files, so the installation tree is made private to the
current user (directories 0700, data files 0600). Only artifacts this project names are
ever deleted: rolling update backups beyond the retention count, and orphaned sidecars of
an interrupted data migration. Pre-migration backups are always kept.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .windows_launchers import remove_stale_launchers

DEFAULT_BACKUP_RETENTION = 10
ROLLING_BACKUP_PATTERN = re.compile(r"^cyber-health-backup-\d{8}_\d{6}\.sqlite3$")
PRIVATE_DIRS = ("", "data", "data/backups", "config", "releases", "bin")
PRIVATE_FILE_GLOBS = ("data/*.sqlite3*", "data/backups/*", "config/*")


@dataclass
class HousekeepingStatus:
    permissions_tightened: list[str] = field(default_factory=list)
    backups_pruned: list[str] = field(default_factory=list)
    temp_files_removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False


def _chmod(path: Path, mode: int, status: HousekeepingStatus) -> None:
    if path.is_symlink() or not path.exists():
        return
    if stat.S_IMODE(path.stat().st_mode) == mode:
        return
    if not status.dry_run:
        try:
            path.chmod(mode)
        except OSError as exc:
            status.errors.append(f"chmod {path}: {exc}")
            return
    status.permissions_tightened.append(str(path))


def secure_permissions(target_dir: Path, status: HousekeepingStatus) -> None:
    """Make the installation readable only by its owner (no-op on Windows ACLs)."""
    if sys.platform == "win32":
        return
    for rel in PRIVATE_DIRS:
        _chmod(target_dir / rel if rel else target_dir, 0o700, status)
    for pattern in PRIVATE_FILE_GLOBS:
        for path in sorted(target_dir.glob(pattern)):
            if path.is_file():
                _chmod(path, 0o600, status)


def prune_backups(backups_dir: Path, status: HousekeepingStatus, keep: int = DEFAULT_BACKUP_RETENTION) -> None:
    """Keep the newest ``keep`` rolling update backups; never touch other files."""
    if keep < 1 or not backups_dir.is_dir():
        return
    rolling = sorted(
        (p for p in backups_dir.iterdir() if ROLLING_BACKUP_PATTERN.match(p.name) and p.is_file() and not p.is_symlink()),
        key=lambda p: p.name,
        reverse=True,
    )
    for backup in rolling[keep:]:
        for path in (backup, backup.with_name(backup.name + "-wal"), backup.with_name(backup.name + "-shm")):
            if not path.exists():
                continue
            if not status.dry_run:
                try:
                    path.unlink()
                except OSError as exc:
                    status.errors.append(f"remove {path}: {exc}")
                    continue
            status.backups_pruned.append(path.name)


def remove_orphaned_migration_sidecars(data_dir: Path, status: HousekeepingStatus) -> None:
    """Delete ``*.tmp_migration-wal/-shm`` left behind when the temp database itself is gone."""
    if not data_dir.is_dir():
        return
    for sidecar in sorted(data_dir.glob("*.tmp_migration-*")):
        if not sidecar.name.endswith(("-wal", "-shm")) or sidecar.is_symlink() or not sidecar.is_file():
            continue
        main = sidecar.with_name(sidecar.name.rsplit("-", 1)[0])
        if main.exists():
            continue
        if not status.dry_run:
            try:
                sidecar.unlink()
            except OSError as exc:
                status.errors.append(f"remove {sidecar}: {exc}")
                continue
        status.temp_files_removed.append(sidecar.name)


def run_housekeeping(
    target_dir: Path,
    *,
    keep_backups: int = DEFAULT_BACKUP_RETENTION,
    dry_run: bool = False,
) -> HousekeepingStatus:
    status = HousekeepingStatus(dry_run=dry_run)
    data_dir = target_dir / "data"
    remove_orphaned_migration_sidecars(data_dir, status)
    if not dry_run:
        venv_scripts = target_dir / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
        status.temp_files_removed.extend(remove_stale_launchers(venv_scripts))
    prune_backups(data_dir / "backups", status, keep=keep_backups)
    secure_permissions(target_dir, status)
    return status


def make_private(path: Path) -> None:
    """Restrict a freshly written data file to its owner."""
    if sys.platform != "win32" and path.is_file() and not path.is_symlink():
        os.chmod(path, 0o600)


def housekeeping_report_lines(status: HousekeepingStatus) -> list[str]:
    def summary(items: list[str]) -> str:
        return f"{len(items)}" + (f" ({', '.join(items[:3])}{', ...' if len(items) > 3 else ''})" if items else "")

    would = "would be " if status.dry_run else ""
    lines = [
        "--- Local Housekeeping ---",
        f"Permissions  : {summary(status.permissions_tightened)} {would}tightened",
        f"Old backups  : {summary(status.backups_pruned)} {would}pruned",
        f"Temp files   : {summary(status.temp_files_removed)} {would}removed",
    ]
    if status.errors:
        lines.append(f"Errors       : {'; '.join(status.errors)}")
    return [*lines, ""]
