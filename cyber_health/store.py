"""SQLite fact store with explicit transaction and idempotency boundaries."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import StoreBusyError


class SQLiteStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=2.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=2000")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS user_profile (
                    user_id TEXT PRIMARY KEY,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    goals_json TEXT NOT NULL DEFAULT '{}',
                    constraints_json TEXT NOT NULL DEFAULT '{}',
                    state_version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meal_log (
                    meal_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    meal_type TEXT NOT NULL,
                    foods_json TEXT NOT NULL,
                    kcal_low INTEGER NOT NULL,
                    kcal_high INTEGER NOT NULL,
                    protein_low INTEGER NOT NULL DEFAULT 0,
                    protein_high INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'deleted')),
                    parent_meal_id TEXT REFERENCES meal_log(meal_id),
                    causation_id TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_meal_user_date
                    ON meal_log(user_id, occurred_at, status);
                CREATE TABLE IF NOT EXISTS schedule_event (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'acknowledged', 'skipped', 'overdue', 'cancelled')),
                    revision INTEGER NOT NULL DEFAULT 1,
                    delivery_attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_log (
                    operation_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    action TEXT NOT NULL,
                    result_status TEXT NOT NULL,
                    before_version INTEGER NOT NULL,
                    after_version INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, idempotency_key)
                );
                """
            )

    @contextmanager
    def transaction(self, attempts: int = 3) -> Iterator[sqlite3.Connection]:
        for attempt in range(attempts):
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
                return
            except sqlite3.OperationalError as error:
                conn.rollback()
                if "locked" not in str(error).lower() or attempt == attempts - 1:
                    raise StoreBusyError("SQLite write lock could not be acquired") from error
                time.sleep(0.05 * (2**attempt))
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

