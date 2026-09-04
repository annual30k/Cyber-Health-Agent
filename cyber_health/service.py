"""Host-neutral domain operations for the P0 shared SQLite runtime."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import ConflictError, StoreBusyError
from .store import SQLiteStore


class CyberHealthService:
    def __init__(self, database_path: str | Path) -> None:
        self.store = SQLiteStore(database_path)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _response(operation_id: str, status: str, data: dict[str, Any], state_version: int, warnings: list[str] | None = None) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "status": status,
            "data": data,
            "warnings": warnings or [],
            "state_version": state_version,
        }

    @staticmethod
    def _request_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _ensure_profile(self, conn: Any, user_id: str, now: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO user_profile(user_id, updated_at) VALUES (?, ?)",
            (user_id, now),
        )

    def _idempotent_response(self, conn: Any, user_id: str, idempotency_key: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT response_json FROM operation_log WHERE user_id = ? AND idempotency_key = ?",
            (user_id, idempotency_key),
        ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def _record_operation(
        self,
        conn: Any,
        *,
        operation_id: str,
        user_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        action: str,
        before_version: int,
        response: dict[str, Any],
        now: str,
    ) -> None:
        conn.execute(
            """INSERT INTO operation_log(
                operation_id, user_id, idempotency_key, request_hash, action,
                result_status, before_version, after_version, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                operation_id,
                user_id,
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

    def get_profile(self, user_id: str) -> dict[str, Any]:
        now = self._now()
        with self.store.transaction() as conn:
            self._ensure_profile(conn, user_id, now)
            row = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            return {
                "user_id": user_id,
                "timezone": row["timezone"],
                "goals": json.loads(row["goals_json"]),
                "constraints": json.loads(row["constraints_json"]),
                "state_version": row["state_version"],
            }

    def log_meal(
        self,
        *,
        user_id: str,
        occurred_at: str,
        meal_type: str,
        foods: list[dict[str, Any]],
        kcal_low: int,
        kcal_high: int,
        idempotency_key: str,
        target_meal_id: str | None = None,
        expected_state_version: int | None = None,
        protein_low: int = 0,
        protein_high: int = 0,
    ) -> dict[str, Any]:
        if kcal_low < 0 or kcal_high < kcal_low:
            raise ValueError("Calories must be a non-negative ordered range")
        payload = {
            "user_id": user_id, "occurred_at": occurred_at, "meal_type": meal_type,
            "foods": foods, "kcal_low": kcal_low, "kcal_high": kcal_high,
            "target_meal_id": target_meal_id, "expected_state_version": expected_state_version,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"
        with self.store.transaction() as conn:
            existing = self._idempotent_response(conn, user_id, idempotency_key)
            if existing:
                return existing
            self._ensure_profile(conn, user_id, now)
            profile = conn.execute("SELECT state_version FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]
            if expected_state_version is not None and expected_state_version != before_version:
                raise ConflictError(f"Expected version {expected_state_version}, current version is {before_version}")
            if target_meal_id:
                original = conn.execute(
                    "SELECT meal_id, status FROM meal_log WHERE meal_id = ? AND user_id = ?",
                    (target_meal_id, user_id),
                ).fetchone()
                if not original or original["status"] != "active":
                    raise ValueError("Target meal does not exist or is no longer active")
                conn.execute("UPDATE meal_log SET status = 'superseded' WHERE meal_id = ?", (target_meal_id,))
            after_version = before_version + 1
            conn.execute("UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?", (after_version, now, user_id))
            meal_id = f"meal_{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO meal_log(
                    meal_id, user_id, occurred_at, meal_type, foods_json, kcal_low, kcal_high,
                    protein_low, protein_high, status, parent_meal_id, causation_id, state_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                (meal_id, user_id, occurred_at, meal_type, self.store.json(foods), kcal_low, kcal_high,
                 protein_low, protein_high, target_meal_id, operation_id, after_version, now),
            )
            totals = self._today_totals(conn, user_id, occurred_at[:10])
            response = self._response(operation_id, "success", {"meal_id": meal_id, "today_totals": totals}, after_version)
            self._record_operation(conn, operation_id=operation_id, user_id=user_id, idempotency_key=idempotency_key,
                                   payload=payload, action="log_meal", before_version=before_version, response=response, now=now)
            return response

    def _today_totals(self, conn: Any, user_id: str, day: str) -> dict[str, int]:
        row = conn.execute(
            """SELECT COALESCE(SUM(kcal_low), 0) AS kcal_low, COALESCE(SUM(kcal_high), 0) AS kcal_high,
                      COALESCE(SUM(protein_low), 0) AS protein_low, COALESCE(SUM(protein_high), 0) AS protein_high,
                      COUNT(*) AS meal_count
               FROM meal_log WHERE user_id = ? AND substr(occurred_at, 1, 10) = ? AND status = 'active'""",
            (user_id, day),
        ).fetchone()
        return dict(row)

    def get_today(self, user_id: str, day: str) -> dict[str, Any]:
        now = self._now()
        with self.store.transaction() as conn:
            self._ensure_profile(conn, user_id, now)
            profile = conn.execute("SELECT state_version FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            return {
                "date": day,
                "state_version": profile["state_version"],
                "nutrition": self._today_totals(conn, user_id, day),
            }

    def get_audit_trail(self, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT operation_id, action, result_status, before_version, after_version, created_at
                   FROM operation_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_schedule(self, user_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(UTC)
        now_iso = now.isoformat()
        with self.store.transaction() as conn:
            overdue = conn.execute(
                """UPDATE schedule_event SET status = 'overdue', updated_at = ?
                   WHERE user_id = ? AND status = 'pending' AND window_end < ?
                   RETURNING event_id, event_type, status, revision""",
                (now_iso, user_id, now_iso),
            ).fetchall()
            return [dict(row) | {"compensation_required": True} for row in overdue]

    def health_check(self) -> dict[str, Any]:
        with self.store.connect() as conn:
            conn.execute("SELECT 1").fetchone()
            pending = conn.execute("SELECT COUNT(*) AS count FROM schedule_event WHERE status = 'overdue'").fetchone()["count"]
        return {"overall_status": "ok", "components": {"sqlite": "ok", "memory_provider": "deferred"}, "pending_work": pending}


__all__ = ["CyberHealthService", "ConflictError", "StoreBusyError"]
