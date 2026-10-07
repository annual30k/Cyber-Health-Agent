"""Versioned SQLite schema migrations recorded in PRAGMA user_version."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cyber_health.store import SCHEMA_VERSION, SchemaVersionError, SQLiteStore


def _user_version(db: Path) -> int:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _columns(db: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(db)) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


class StoreMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "cyber-health.sqlite3"

    def test_fresh_database_is_stamped_with_latest_version(self) -> None:
        SQLiteStore(self.db)
        self.assertEqual(_user_version(self.db), SCHEMA_VERSION)

    def test_unversioned_legacy_database_gains_late_columns(self) -> None:
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(
                """
                CREATE TABLE user_profile (
                    user_id TEXT PRIMARY KEY,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    goals_json TEXT NOT NULL DEFAULT '{}',
                    constraints_json TEXT NOT NULL DEFAULT '{}',
                    state_version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO user_profile(user_id, updated_at) VALUES ('owner', '2026-01-01T00:00:00+00:00');
                """
            )
        self.assertEqual(_user_version(self.db), 0)

        SQLiteStore(self.db)

        self.assertEqual(_user_version(self.db), SCHEMA_VERSION)
        self.assertTrue({"safety_flags_json", "safety_mode", "deload_until"} <= _columns(self.db, "user_profile"))
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute("SELECT safety_mode, safety_flags_json FROM user_profile").fetchone()
        self.assertEqual(row, ("normal", "[]"))

    def test_reopening_is_a_no_op(self) -> None:
        SQLiteStore(self.db)
        SQLiteStore(self.db)
        self.assertEqual(_user_version(self.db), SCHEMA_VERSION)

    def test_database_from_a_newer_release_is_refused(self) -> None:
        SQLiteStore(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        with self.assertRaises(SchemaVersionError):
            SQLiteStore(self.db)


if __name__ == "__main__":
    unittest.main()
