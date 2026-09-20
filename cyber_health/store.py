"""SQLite fact store with explicit transaction and idempotency boundaries."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import StoreBusyError

SINGLE_USER_ID = "owner"


class ClosingConnection(sqlite3.Connection):
    """The sqlite context manager commits/rolls back but otherwise leaks handles."""

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class SQLiteStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError("Use a file-backed database for cross-session persistence")
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=2.0, isolation_level=None, factory=ClosingConnection)
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
                    safety_flags_json TEXT NOT NULL DEFAULT '[]',
                    safety_mode TEXT NOT NULL DEFAULT 'normal',
                    deload_until TEXT,
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
                    prompt_hint TEXT,
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
                CREATE TABLE IF NOT EXISTS domain_record (
                    record_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    day TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    parent_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    causation_id TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_domain_user_kind_day
                    ON domain_record(user_id, kind, day, status);
                CREATE TABLE IF NOT EXISTS memory_outbox (
                    intent_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    idempotency_key TEXT,
                    request_hash TEXT,
                    method TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    owner_token TEXT,
                    lease_until TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_status_lease
                    ON memory_outbox(status, lease_until);
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    name TEXT PRIMARY KEY, version TEXT NOT NULL
                );
                INSERT OR IGNORE INTO schema_metadata VALUES ('core', '0.1.0');
                """
            )
            self._apply_migrations(conn)

    @staticmethod
    def _apply_migrations(conn: sqlite3.Connection) -> None:
        cursor = conn.execute("PRAGMA table_info(user_profile)")
        profile_cols = {row["name"] for row in cursor.fetchall()}
        if profile_cols:
            if "safety_flags_json" not in profile_cols:
                conn.execute("ALTER TABLE user_profile ADD COLUMN safety_flags_json TEXT NOT NULL DEFAULT '[]'")
            if "safety_mode" not in profile_cols:
                conn.execute("ALTER TABLE user_profile ADD COLUMN safety_mode TEXT NOT NULL DEFAULT 'normal'")
            if "deload_until" not in profile_cols:
                conn.execute("ALTER TABLE user_profile ADD COLUMN deload_until TEXT")

        cursor = conn.execute("PRAGMA table_info(schedule_event)")
        sched_cols = {row["name"] for row in cursor.fetchall()}
        if sched_cols:
            if "prompt_hint" not in sched_cols:
                conn.execute("ALTER TABLE schedule_event ADD COLUMN prompt_hint TEXT")

        cursor = conn.execute("PRAGMA table_info(memory_outbox)")
        outbox_cols = {row["name"] for row in cursor.fetchall()}
        if outbox_cols:
            if "idempotency_key" not in outbox_cols:
                conn.execute("ALTER TABLE memory_outbox ADD COLUMN idempotency_key TEXT")
            if "request_hash" not in outbox_cols:
                conn.execute("ALTER TABLE memory_outbox ADD COLUMN request_hash TEXT")
            if "owner_token" not in outbox_cols:
                conn.execute("ALTER TABLE memory_outbox ADD COLUMN owner_token TEXT")
            if "lease_until" not in outbox_cols:
                conn.execute("ALTER TABLE memory_outbox ADD COLUMN lease_until TEXT")
            if "updated_at" not in outbox_cols:
                conn.execute("ALTER TABLE memory_outbox ADD COLUMN updated_at TEXT")

    @contextmanager
    def transaction(self, attempts: int = 3) -> Iterator[sqlite3.Connection]:
        # Retry ONLY acquisition. A contextmanager may yield exactly once;
        # replaying the caller's transaction body after an error is not safe.
        conn = None
        for attempt in range(attempts):
            try:
                conn = self.connect()
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as error:
                if conn is not None:
                    conn.close()
                if not any(word in str(error).lower() for word in ("locked", "busy")):
                    raise
                if attempt == attempts - 1:
                    raise StoreBusyError("SQLite write lock could not be acquired") from error
                time.sleep(0.05 * (2**attempt))
        try:
            yield conn
            conn.commit()
        except sqlite3.OperationalError as error:
            conn.rollback()
            if any(word in str(error).lower() for word in ("locked", "busy")):
                raise StoreBusyError("SQLite transaction could not commit") from error
            raise
        except Exception:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
