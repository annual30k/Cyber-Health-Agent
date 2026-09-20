"""Migration utilities for Cyber Health single-owner database architecture."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .store import SINGLE_USER_ID, ClosingConnection


class MigrationError(Exception):
    """Base exception for database migration errors."""


@dataclass
class MigrationReport:
    success: bool
    dry_run: bool
    db_path: str
    from_user: str | None
    to_user: str
    backup_path: str | None
    migrated_counts: dict[str, int]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_pre_migration_backup(db_file: Path) -> Path:
    """Create a point-in-time snapshot backup before modifying database state."""
    backups_dir = db_file.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_file = backups_dir / f"cyber-health-pre-owner-migration-{timestamp}.sqlite3"

    # Use SQLite VACUUM INTO for consistent online backup when file exists
    try:
        conn = sqlite3.connect(str(db_file), timeout=10.0, factory=ClosingConnection)
        with conn:
            conn.execute(f"VACUUM INTO '{backup_file.resolve()}'")
        conn.close()
    except (sqlite3.Error, OSError):
        # Fallback to copy if VACUUM INTO fails (e.g. empty or newly opened file)
        shutil.copy2(db_file, backup_file)

    return backup_file


def migrate_database_to_owner(
    db_path: str | Path,
    *,
    from_user: str | None = None,
    to_user: str = SINGLE_USER_ID,
    dry_run: bool = False,
) -> MigrationReport:
    """Migrate legacy user partitions in a Cyber Health SQLite database to single-owner format."""
    db_file = Path(db_path).resolve()
    if not db_file.is_file():
        raise MigrationError(f"Database file does not exist: {db_file}")

    target_tables = (
        "user_profile",
        "meal_log",
        "schedule_event",
        "operation_log",
        "domain_record",
        "memory_outbox",
    )

    conn = sqlite3.connect(str(db_file), timeout=5.0, isolation_level=None, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise MigrationError(f"Database integrity check failed: {integrity[0] if integrity else 'unknown'}")

        existing_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        # Find distinct foreign user_ids
        foreign_users: set[str] = set()
        for table in target_tables:
            if table not in existing_tables:
                continue
            rows = conn.execute(
                f"SELECT DISTINCT user_id FROM {table} WHERE user_id <> ?", (to_user,)
            ).fetchall()
            for r in rows:
                foreign_users.add(r[0])

        if not foreign_users:
            return MigrationReport(
                success=True,
                dry_run=dry_run,
                db_path=str(db_file),
                from_user=None,
                to_user=to_user,
                backup_path=None,
                migrated_counts={t: 0 for t in target_tables if t in existing_tables},
                message=f"Database already partitioned exclusively for '{to_user}'",
            )

        if from_user is not None:
            if from_user not in foreign_users:
                raise MigrationError(
                    f"Specified source user '{from_user}' not found in database foreign partitions: {sorted(foreign_users)}"
                )
            selected_source = from_user
        else:
            if len(foreign_users) > 1:
                raise MigrationError(
                    f"Multiple legacy users detected {sorted(foreign_users)}. Please specify --from-user explicitly."
                )
            selected_source = next(iter(foreign_users))

        # Count records to be migrated
        counts: dict[str, int] = {}
        for table in target_tables:
            if table not in existing_tables:
                continue
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (selected_source,)
            ).fetchone()
            counts[table] = row[0] if row else 0

        if dry_run:
            return MigrationReport(
                success=True,
                dry_run=True,
                db_path=str(db_file),
                from_user=selected_source,
                to_user=to_user,
                backup_path=None,
                migrated_counts=counts,
                message=f"Dry run: {sum(counts.values())} records from '{selected_source}' would be migrated to '{to_user}'",
            )

        # Create backup before mutation
        backup_path = create_pre_migration_backup(db_file)

        # Perform atomic migration
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Handle user_profile (primary key is user_id)
            if "user_profile" in existing_tables and counts.get("user_profile", 0) > 0:
                owner_profile = conn.execute(
                    "SELECT 1 FROM user_profile WHERE user_id = ?", (to_user,)
                ).fetchone()
                if owner_profile:
                    # Update source profile data into owner profile if owner profile is fresh/empty
                    conn.execute("DELETE FROM user_profile WHERE user_id = ?", (selected_source,))
                else:
                    conn.execute(
                        "UPDATE user_profile SET user_id = ? WHERE user_id = ?",
                        (to_user, selected_source),
                    )

            # Update remaining tables
            for table in ("meal_log", "schedule_event", "operation_log", "domain_record", "memory_outbox"):
                if table in existing_tables:
                    conn.execute(
                        f"UPDATE {table} SET user_id = ? WHERE user_id = ?",
                        (to_user, selected_source),
                    )

            fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_issues:
                conn.execute("ROLLBACK")
                raise MigrationError(f"Foreign key check failed after migration: {fk_issues}")

            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            raise MigrationError(f"Migration transaction failed: {exc}") from exc

        return MigrationReport(
            success=True,
            dry_run=False,
            db_path=str(db_file),
            from_user=selected_source,
            to_user=to_user,
            backup_path=str(backup_path),
            migrated_counts=counts,
            message=f"Successfully migrated {sum(counts.values())} records from '{selected_source}' to '{to_user}'",
        )
    finally:
        conn.close()
