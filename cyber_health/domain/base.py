"""Shared service core: store, clock, response envelope and idempotency protocol."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import IdempotencyMismatchError
from ..memory import MemoryProvider, UnavailableMemoryProvider
from ..store import SINGLE_USER_ID, SQLiteStore
from .catalog import RED_FLAG_KEYWORDS

# Every fact belongs to the one person this installation serves; the column is kept
# as a fixed storage partition key so older databases stay readable.
OWNER_ID = SINGLE_USER_ID

# Idempotency keys exist to make retries safe; real retries arrive within minutes.
# After this window a key that collides with a *different* request is treated as
# reuse of a generic label (e.g. "lunch-1" on another day or in another session)
# rather than as a corrupted retry, so the new fact is written instead of rejected.
DEFAULT_IDEMPOTENCY_REPLAY_WINDOW = timedelta(hours=24)

# Errors that mean a stored timezone name is unusable, so the default zone applies.
_INVALID_ZONE_ERRORS = (ZoneInfoNotFoundError, ValueError, TypeError)
# Errors that mean a stored JSON body is malformed or shaped unexpectedly. Narrower than
# ``Exception`` so SQLite failures and programming errors are never silently skipped.
_MALFORMED_RECORD_ERRORS = (TypeError, ValueError, AttributeError, KeyError)


class ServiceCore:
    """Store, clock and the idempotent operation-log protocol shared by every domain mixin."""

    def __init__(
        self,
        database_path: str | Path | SQLiteStore,
        memory_provider: MemoryProvider | None = None,
        recovery_evidence_window_days: int = 1,
        clock: Callable[[], datetime] | None = None,
        idempotency_replay_window: timedelta = DEFAULT_IDEMPOTENCY_REPLAY_WINDOW,
    ) -> None:
        if isinstance(database_path, SQLiteStore):
            self.store = database_path
        else:
            self.store = SQLiteStore(database_path)
        self.memory_provider: MemoryProvider = memory_provider or UnavailableMemoryProvider()
        self.recovery_evidence_window_days: int = recovery_evidence_window_days
        # Injectable wall clock so date-sensitive rules (deload windows, leases, TTL) are testable.
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self.idempotency_replay_window: timedelta = idempotency_replay_window
        self.store.assert_single_owner()

    def _utcnow(self) -> datetime:
        now_dt = self._clock()
        if now_dt.tzinfo is None:
            return now_dt.replace(tzinfo=UTC)
        return now_dt.astimezone(UTC)

    def _now(self) -> str:
        return self._utcnow().isoformat()

    @staticmethod
    def _scan_for_red_flags(text: str) -> list[str]:
        return [kw for kw in RED_FLAG_KEYWORDS if kw in text]

    @staticmethod
    def _response(
        operation_id: str,
        status: str,
        data: dict[str, Any],
        state_version: int,
        warnings: list[str] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "status": status,
            "data": data,
            "warnings": warnings or [],
            "error": error,
            "state_version": state_version,
        }

    @staticmethod
    def _request_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _ensure_profile_in_tx(self, conn: Any, now: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO user_profile(user_id, updated_at) VALUES (?, ?)",
            (OWNER_ID, now),
        )

    @staticmethod
    def _make_intent_id(idempotency_key: str) -> str:
        serialized = json.dumps([OWNER_ID, idempotency_key], separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return f"intent_{digest[:32]}"

    def _check_idempotency(
        self,
        conn: Any,
        idempotency_key: str,
        action: str,
        payload: dict[str, Any],
        *,
        recompute_when_stale: bool = False,
        allow_key_reuse: bool = True,
    ) -> dict[str, Any] | None:
        """Return the cached response for a genuine retry, or ``None`` to execute.

        ``recompute_when_stale`` is for results derived from the whole fact state
        (daily review, tomorrow's plan): an identical request replays only while no
        other write has happened since, so a date-stable key never returns a stale
        review after a late meal is logged.  ``allow_key_reuse=False`` keeps the strict
        mismatch for callers whose external intent ids are derived from the key.
        """
        row = conn.execute(
            """SELECT operation_id, action, request_hash, result_status, after_version, response_json, created_at
               FROM operation_log WHERE user_id = ? AND idempotency_key = ?""",
            (OWNER_ID, idempotency_key),
        ).fetchone()
        if not row:
            return None
        current_hash = self._request_hash(payload)
        if row["action"] != action or row["request_hash"] != current_hash:
            if allow_key_reuse and self._outside_replay_window(row["created_at"]):
                self._retire_idempotency_key(conn, row["operation_id"], idempotency_key)
                return None
            raise IdempotencyMismatchError(
                f"Idempotency key '{idempotency_key}' already belongs to a different {row['action']} request "
                f"committed at {row['created_at']} (operation {row['operation_id']}); nothing was written. "
                "Reuse a key only to retry the identical call. If this is a new fact, retry with a fresh unique "
                "key such as '<tool>-<local date>-<random suffix>'. If it is the same fact, it is already "
                "recorded: read it back or revise it instead of logging it again."
            )
        if row["result_status"] == "pending":
            return None
        if recompute_when_stale:
            profile = conn.execute(
                "SELECT state_version FROM user_profile WHERE user_id = ?", (OWNER_ID,)
            ).fetchone()
            if profile is not None and profile["state_version"] != row["after_version"]:
                self._retire_idempotency_key(conn, row["operation_id"], idempotency_key)
                return None
        return json.loads(row["response_json"])

    def _outside_replay_window(self, created_at: str) -> bool:
        try:
            created = datetime.fromisoformat(created_at)
        except (AttributeError, ValueError):
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return self._utcnow() - created >= self.idempotency_replay_window

    @staticmethod
    def _retire_idempotency_key(conn: Any, operation_id: str, idempotency_key: str) -> None:
        """Free a key for a new operation while keeping the original row in the audit log."""
        conn.execute(
            "UPDATE operation_log SET idempotency_key = ? WHERE operation_id = ?",
            (f"{idempotency_key}#retired:{operation_id}", operation_id),
        )

    def _record_operation(
        self,
        conn: Any,
        *,
        operation_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        action: str,
        before_version: int,
        response: dict[str, Any],
        now: str,
    ) -> None:
        existing = conn.execute(
            "SELECT operation_id FROM operation_log WHERE user_id = ? AND idempotency_key = ?",
            (OWNER_ID, idempotency_key),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE operation_log
                   SET result_status = ?, after_version = ?, response_json = ?
                   WHERE operation_id = ?""",
                (response["status"], response["state_version"], self.store.json(response), existing["operation_id"]),
            )
        else:
            conn.execute(
                """INSERT INTO operation_log(
                    operation_id, user_id, idempotency_key, request_hash, action,
                    result_status, before_version, after_version, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    operation_id,
                    OWNER_ID,
                    idempotency_key,
                    self._request_hash(payload),
                    action,
                    response["status"],
                    before_version,
                    response["state_version"],
                    self.store.json(response),
                    now,
                ),
            )

    @staticmethod
    def _parse_day_in_timezone(occurred_at_iso: str, tz_name: str) -> str:
        try:
            tz = ZoneInfo(tz_name)
        except _INVALID_ZONE_ERRORS:
            tz = ZoneInfo("Asia/Shanghai")
        dt = datetime.fromisoformat(occurred_at_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)
        return dt.strftime("%Y-%m-%d")
