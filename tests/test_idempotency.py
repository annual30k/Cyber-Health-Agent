"""Idempotent writes, key reuse, replays and optimistic version checks."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from cyber_health import (
    ConflictError,
    CyberHealthService,
    IdempotencyMismatchError,
    ValidationError,
)
from test_support import FIXED_NOW

USER = "owner"


DAY = "2026-09-05"


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class IdempotencyKeyReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = MutableClock(FIXED_NOW)
        self.service = CyberHealthService(Path(tmp.name) / "idem.sqlite3", clock=self.clock)

    def _log_meal(self, key: str, *, day: str = DAY, kcal: int = 500) -> dict:
        return self.service.log_meal(
            occurred_at=f"{day}T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "rice"}],
            kcal_low=kcal,
            kcal_high=kcal + 100,
            idempotency_key=key,
        )

    def _count(self, table: str) -> int:
        with self.service.store.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?", (USER,)).fetchone()["c"]

    def test_collision_within_retry_window_is_rejected_with_actionable_message(self) -> None:
        self._log_meal("lunch-1")
        self.clock.advance(timedelta(hours=1))
        with self.assertRaises(IdempotencyMismatchError) as caught:
            self._log_meal("lunch-1", kcal=800)
        message = str(caught.exception)
        self.assertIn("log_meal", message)
        self.assertIn("nothing was written", message)
        self.assertIn("fresh unique key", message)
        self.assertEqual(self._count("meal_log"), 1)

    def test_generic_key_reused_on_a_later_day_records_the_new_fact(self) -> None:
        first = self._log_meal("lunch-1")
        self.clock.advance(timedelta(days=1))
        second = self._log_meal("lunch-1", day="2026-09-06", kcal=650)

        self.assertEqual(second["status"], "success")
        self.assertNotEqual(second["operation_id"], first["operation_id"])
        self.assertEqual(self._count("meal_log"), 2)
        with self.service.store.connect() as conn:
            keys = sorted(
                row["idempotency_key"]
                for row in conn.execute("SELECT idempotency_key FROM operation_log WHERE user_id = ?", (USER,))
            )
        # The original operation stays in the audit log under a retired key.
        self.assertEqual(keys, ["lunch-1", f"lunch-1#retired:{first['operation_id']}"])

    def test_identical_request_after_window_still_replays_instead_of_duplicating(self) -> None:
        first = self._log_meal("lunch-1")
        self.clock.advance(timedelta(days=3))
        replay = self._log_meal("lunch-1")
        self.assertEqual(replay, first)
        self.assertEqual(self._count("meal_log"), 1)

    def test_key_reused_by_a_different_tool_after_window(self) -> None:
        self._log_meal("today")
        self.clock.advance(timedelta(days=2))
        res = self.service.log_daily_metrics(
            date="2026-09-07", metrics={"sleep_hours": 7.5}, idempotency_key="today"
        )
        self.assertEqual(res["status"], "success")

    def test_date_stable_review_key_recomputes_after_new_facts(self) -> None:
        self._log_meal("lunch-a", kcal=500)
        first = self.service.daily_review(date=DAY, idempotency_key=f"review-{DAY}")
        self.assertEqual(first["data"]["nutrition_analysis"]["intake_kcal_range"], [500, 600])

        # A genuine retry with nothing changed in between replays the cached review.
        retry = self.service.daily_review(date=DAY, idempotency_key=f"review-{DAY}")
        self.assertEqual(retry, first)

        # A late dinner makes the cached review stale; the same key must not hide it.
        self.service.log_meal(
            occurred_at=f"{DAY}T19:00:00+08:00",
            meal_type="dinner",
            foods=[{"name": "fish"}],
            kcal_low=300,
            kcal_high=400,
            idempotency_key="dinner-a",
        )
        refreshed = self.service.daily_review(date=DAY, idempotency_key=f"review-{DAY}")
        self.assertNotEqual(refreshed["operation_id"], first["operation_id"])
        self.assertEqual(refreshed["data"]["nutrition_analysis"]["intake_kcal_range"], [800, 1000])

        again = self.service.daily_review(date=DAY, idempotency_key=f"review-{DAY}")
        self.assertEqual(again, refreshed)

    def test_plan_tomorrow_replays_only_while_state_is_unchanged(self) -> None:
        first = self.service.plan_tomorrow(date=DAY, idempotency_key="plan")
        self.assertEqual(self.service.plan_tomorrow(date=DAY, idempotency_key="plan"), first)

        self.service.log_daily_metrics(
            date=DAY, metrics={"sleep_hours": 5.0, "fatigue_level": 8}, idempotency_key="metrics"
        )
        refreshed = self.service.plan_tomorrow(date=DAY, idempotency_key="plan")
        self.assertNotEqual(refreshed["data"]["plan_id"], first["data"]["plan_id"])

    def test_memory_proposal_keys_are_never_recycled(self) -> None:
        self.service.propose_memory_candidate(
            method="memory.propose", payload={"text": "a"}, idempotency_key="remember"
        )
        self.clock.advance(timedelta(days=5))
        with self.assertRaises(IdempotencyMismatchError):
            self.service.propose_memory_candidate(
                method="memory.propose", payload={"text": "b"}, idempotency_key="remember"
            )


class ReplayAndConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_health.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_idempotency_exact_replay_vs_mismatch_error(self) -> None:
        """Exact repeat returns cached operation; different payload with same key raises IDEMPOTENCY_MISMATCH."""
        payload = {
            "occurred_at": "2026-09-04T12:00:00+08:00",
            "meal_type": "lunch",
            "foods": [{"name": "beef noodles"}],
            "kcal_low": 500,
            "kcal_high": 650,
            "protein_low": 25,
            "protein_high": 35,
            "idempotency_key": "bob-idemp-001",
        }

        # First execution
        first_res = self.service.log_meal(**payload)
        self.assertEqual(first_res["status"], "success")

        # Exact repeat
        replay_res = self.service.log_meal(**payload)
        self.assertEqual(replay_res, first_res)

        # Mismatch: same key, but different calories
        mismatch_payload = dict(payload)
        mismatch_payload["kcal_low"] = 700
        mismatch_payload["kcal_high"] = 900

        with self.assertRaises(IdempotencyMismatchError) as caught:
            self.service.log_meal(**mismatch_payload)
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_MISMATCH")

        # Verify only 1 meal and 1 operation log exist
        with self.service.store.connect() as conn:
            meals_count = conn.execute("SELECT COUNT(*) AS c FROM meal_log WHERE user_id = 'owner'").fetchone()["c"]
            ops_count = conn.execute("SELECT COUNT(*) AS c FROM operation_log WHERE user_id = 'owner'").fetchone()["c"]
            self.assertEqual(meals_count, 1)
            self.assertEqual(ops_count, 1)

    def test_optimistic_conflict_version_rejected(self) -> None:
        """Providing an outdated expected_state_version raises CONFLICT_VERSION."""
        self.service.log_meal(
            occurred_at="2026-09-04T09:00:00+08:00",
            meal_type="breakfast",
            foods=[],
            kcal_low=200,
            kcal_high=250,
            idempotency_key="carol-bk",
        )

        with self.assertRaises(ConflictError) as caught:
            self.service.log_meal(
                occurred_at="2026-09-04T13:00:00+08:00",
                meal_type="lunch",
                foods=[],
                kcal_low=400,
                kcal_high=500,
                idempotency_key="carol-lunch",
                expected_state_version=0,  # Stale, currently 1
            )
        self.assertEqual(caught.exception.code, "CONFLICT_VERSION")


class CrossSessionReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "health.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_same_idempotency_key_returns_original_operation(self):
        service = CyberHealthService(self.database)
        payload = {
            "occurred_at": "2026-09-03T12:30:00+08:00", "meal_type": "lunch",
            "foods": [], "kcal_low": 100, "kcal_high": 150, "idempotency_key": "lunch-001",
        }
        first = service.log_meal(**payload)
        repeated = service.log_meal(**payload)

        self.assertEqual(repeated, first)
        self.assertEqual(len(service.get_audit_trail()), 1)

    def test_stale_state_version_is_rejected(self):
        service = CyberHealthService(self.database)
        service.log_meal(
            occurred_at="2026-09-03T12:30:00+08:00", meal_type="lunch",
            foods=[], kcal_low=100, kcal_high=150, idempotency_key="lunch-001",
        )

        with self.assertRaises(ConflictError) as caught:
            service.log_meal(
                occurred_at="2026-09-03T13:00:00+08:00", meal_type="snack",
                foods=[], kcal_low=100, kcal_high=150, idempotency_key="snack-001", expected_state_version=0,
            )
        self.assertEqual(caught.exception.code, "CONFLICT_VERSION")


class RequiredKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "review.sqlite3")

    def meal(self, key, timestamp, kcal=100):
        return self.service.log_meal(occurred_at=timestamp,
            meal_type="breakfast", foods=[], kcal_low=kcal, kcal_high=kcal + 10,
            idempotency_key=key)

    def test_profile_write_requires_idempotency_key(self):
        with self.assertRaises((ValidationError, TypeError)):
            self.service.update_profile(goals={"goal": "maintain"})


if __name__ == "__main__":
    unittest.main()
