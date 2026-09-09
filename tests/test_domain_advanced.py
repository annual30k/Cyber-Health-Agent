"""Advanced Domain and Edge Case Tests for Cyber Health Agent.

Verifies:
1. Meal deletion & repeat meal shortcuts
2. Daily metrics, recovery score calculation & TRAIN_RECOVERY_01
3. Red flag detection & Restricted Mode (SAFETY_RESTRICTED)
4. Return-to-play 7-day Deload protocol (RECOVERY_FLAG_CLEAR_01)
5. Daily review (no_data vs active) & tomorrow plan commit
6. Schedule event lifecycle & overdue compensation
7. Memory outbox queueing & maintain_memory retry
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cyber_health import (
    CyberHealthService,
    MemoryProvider,
    MemoryUnavailable,
    SafetyRestrictedError,
    UnavailableMemoryProvider,
    ValidationError,
)


class MockWorkingMemoryProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        return {"status": "ok", "candidate_id": f"cand_{len(self.calls)}"}


class TestDomainAdvanced(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_advanced.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_meal_delete_and_repeat(self) -> None:
        """Deleting a meal soft-deletes and recalculates totals; repeating a meal copies foods and nutrients."""
        # 1. Log breakfast
        bk = self.service.log_meal(
            user_id="u_user1",
            occurred_at="2026-09-04T08:00:00+08:00",
            meal_type="breakfast",
            foods=[{"name": "eggs", "amount_g": {"low": 100, "high": 120}}],
            kcal_low=150,
            kcal_high=180,
            protein_low=12,
            protein_high=15,
            idempotency_key="u1-bk-1",
        )
        meal1_id = bk["data"]["meal_id"]

        # 2. Log lunch
        lunch = self.service.log_meal(
            user_id="u_user1",
            occurred_at="2026-09-04T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "salad"}],
            kcal_low=300,
            kcal_high=400,
            protein_low=10,
            protein_high=15,
            idempotency_key="u1-lunch-1",
        )

        today_before_del = self.service.get_today("u_user1", "2026-09-04")
        self.assertEqual(today_before_del["nutrition"]["meal_count"], 2)
        self.assertEqual(today_before_del["nutrition"]["kcal_low"], 450)

        # 3. Delete lunch (mistake entry)
        del_res = self.service.delete_meal(
            user_id="u_user1",
            meal_id=lunch["data"]["meal_id"],
            idempotency_key="u1-del-lunch",
            reason="Double logged by mistake",
        )
        self.assertEqual(del_res["status"], "success")
        self.assertEqual(del_res["data"]["deleted_meal_id"], lunch["data"]["meal_id"])

        today_after_del = self.service.get_today("u_user1", "2026-09-04")
        self.assertEqual(today_after_del["nutrition"]["meal_count"], 1)
        self.assertEqual(today_after_del["nutrition"]["kcal_low"], 150)

        # 4. Repeat breakfast on next day using repeat_meal="yesterday"
        bk_repeat = self.service.log_meal(
            user_id="u_user1",
            occurred_at="2026-09-05T08:00:00+08:00",
            meal_type="breakfast",
            foods=[],
            kcal_low=0,
            kcal_high=0,
            repeat_meal=meal1_id,
            idempotency_key="u1-bk-2",
        )
        today_sept5 = self.service.get_today("u_user1", "2026-09-05")
        self.assertEqual(today_sept5["nutrition"]["meal_count"], 1)
        self.assertEqual(today_sept5["nutrition"]["kcal_low"], 150)
        self.assertEqual(today_sept5["nutrition"]["protein_low"], 12)

    def test_daily_metrics_and_recovery_score(self) -> None:
        """Sleep < 6 or fatigue >= 7 triggers TRAIN_RECOVERY_01 and lowers recovery score."""
        res = self.service.log_daily_metrics(
            user_id="u_user2",
            date="2026-09-04",
            metrics={
                "weight_kg": 72.5,
                "sleep_hours": 5.0,
                "sleep_quality": "poor",
                "fatigue_level": 8,
                "soreness_locations": ["腿部酸痛"],
                "steps": 4500,
            },
            idempotency_key="u2-metrics-1",
        )
        self.assertEqual(res["status"], "success")
        self.assertIn("TRAIN_RECOVERY_01", res["data"]["triggered_rules"])
        # Expected score: 100 - (2 * 15) - (7 * 6) - 15 = 100 - 30 - 42 - 15 = 13
        self.assertLess(res["data"]["recovery_score"], 50)
        self.assertIsNotNone(res["data"]["coaching_alert"])

    def test_safety_red_flag_and_deload_protocol(self) -> None:
        """Red flag symptom triggers Restricted Mode; clearing it requires clearance_reason and initiates 7-day Deload."""
        # 1. Log metrics reporting severe chest pain (red flag)
        res_flag = self.service.log_daily_metrics(
            user_id="u_user3",
            date="2026-09-04",
            metrics={
                "soreness_locations": ["严重胸痛", "呼吸困难"],
            },
            idempotency_key="u3-rf-1",
        )
        self.assertIn("TRAIN_SAFETY_01", res_flag["data"]["triggered_rules"])

        prof = self.service.get_profile("u_user3")
        self.assertEqual(prof["safety_mode"], "restricted")
        self.assertIn("严重胸痛", prof["safety_flags"])

        # 2. Attempting to log a workout in restricted mode must raise SafetyRestrictedError
        with self.assertRaises(SafetyRestrictedError) as caught:
            self.service.log_workout(
                user_id="u_user3",
                date="2026-09-04",
                planned_exercises=["Bench Press"],
                idempotency_key="u3-wo-fail",
            )
        self.assertEqual(caught.exception.code, "SAFETY_RESTRICTED")

        # 3. Attempting to clear safety flags without clearance reason must fail
        with self.assertRaises(ValidationError):
            self.service.update_profile(
                user_id="u_user3",
                clear_safety_flags=True,
                clearance_reason="",
                idempotency_key="u3-clear-bad",
            )

        # 4. Clear safety flags with valid clearance reason
        clear_res = self.service.update_profile(
            user_id="u_user3",
            clear_safety_flags=True,
            clearance_reason="Cardiac check complete, symptoms cleared, doctor signed return-to-play",
            idempotency_key="u3-clear-rf",
        )
        self.assertEqual(clear_res["data"]["safety_mode"], "normal")
        self.assertEqual(len(clear_res["data"]["safety_flags"]), 0)
        self.assertIsNotNone(clear_res["data"]["deload_until"])
        self.assertTrue(any("RECOVERY_FLAG_CLEAR_01" in w for w in clear_res["warnings"]))

        # 5. Now workout logging succeeds, but issues deload warning
        wo_res = self.service.log_workout(
            user_id="u_user3",
            date="2026-09-04",
            planned_exercises=["Goblet Squat"],
            actual_sets=[{"exercise": "Goblet Squat", "weight_kg": 12, "reps": 10, "rir": 4}],
            idempotency_key="u3-wo-ok",
        )
        self.assertEqual(wo_res["status"], "success")
        self.assertTrue(any("Deload Period" in w for w in wo_res["warnings"]))

    def test_daily_review_and_plan_tomorrow(self) -> None:
        """Daily review distinguishes no_data from zero intake, and plan_tomorrow handles commit."""
        # Unrecorded day review
        rev_empty = self.service.daily_review(
            user_id="u_user4",
            date="2026-09-04",
            idempotency_key="u4-rev-1",
        )
        self.assertEqual(rev_empty["data"]["recording_status"], "no_data")
        self.assertIn("未记录", rev_empty["data"]["summary"])

        # Commit tomorrow's plan
        plan_res = self.service.plan_tomorrow(
            user_id="u_user4",
            date="2026-09-05",
            idempotency_key="u4-plan-commit",
            commit=True,
        )
        self.assertEqual(plan_res["data"]["status"], "committed")

        # Verify today status on Sept 5 reflects committed plan state
        today_sept5 = self.service.get_today("u_user4", "2026-09-05")
        self.assertEqual(today_sept5["plan_status"]["state"], "committed")

    def test_schedule_lifecycle_and_overdue_compensation(self) -> None:
        """Expired schedule events transition to overdue with compensation_required=True."""
        now = datetime.now(UTC)
        past_end = (now - timedelta(minutes=15)).isoformat()
        past_start = (now - timedelta(minutes=45)).isoformat()

        # Seed a schedule event that is already past its window
        with self.service.store.transaction() as conn:
            conn.execute(
                """INSERT INTO schedule_event(
                    event_id, user_id, event_type, window_start, window_end, status,
                    revision, delivery_attempts, prompt_hint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 1, 0, 'Late night review', ?, ?)""",
                ("sched-001", "u_user5", "DAILY_REVIEW", past_start, past_end, past_start, past_start),
            )

        # Call get_schedule
        events = self.service.get_schedule("u_user5", now=now)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], "sched-001")
        self.assertEqual(events[0]["status"], "overdue")
        self.assertTrue(events[0]["compensation_required"])

        # Acknowledge the overdue event
        ack = self.service.acknowledge_schedule_event(
            user_id="u_user5",
            event_id="sched-001",
            action="acknowledged",
            idempotency_key="ack-sched-001",
        )
        self.assertEqual(ack["status"], "success")

        # Subsequent get_schedule should return no pending/overdue events
        events_after = self.service.get_schedule("u_user5", now=now)
        self.assertEqual(len(events_after), 0)

    def test_memory_outbox_queueing_and_maintain_retry(self) -> None:
        """When MemoryProvider is unavailable, candidate is stored in outbox; maintain_memory retries it."""
        # 1. Propose memory with default UnavailableMemoryProvider
        res = self.service.propose_memory_candidate(
            user_id="u_user6",
            method="memory.propose",
            payload={"insight": "Lactose sensitivity observed"},
            idempotency_key="u6-prop-1",
        )
        self.assertEqual(res["status"], "partial")
        self.assertTrue(any("MEMORY_DEFERRED" in w for w in res["warnings"]))

        # Check outbox count
        with self.service.store.connect() as conn:
            pending_count = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = 'u_user6' AND status = 'pending'"
            ).fetchone()["c"]
            self.assertEqual(pending_count, 1)

        # 2. Maintain memory while still unavailable -> stays in outbox
        m1 = self.service.maintain_memory(user_id="u_user6", idempotency_key="u6-maint-1")
        self.assertEqual(m1["status"], "partial")
        self.assertEqual(m1["data"]["deferred_count"], 1)

        # 3. Attach working mock MemoryProvider
        mock_provider = MockWorkingMemoryProvider()
        service_with_memory = CyberHealthService(self.db_path, memory_provider=mock_provider)
        m2 = service_with_memory.maintain_memory(user_id="u_user6", idempotency_key="u6-maint-2")
        self.assertEqual(m2["status"], "success")
        self.assertEqual(m2["data"]["sent_count"], 1)
        self.assertEqual(len(mock_provider.calls), 1)

        # Check outbox is now empty of pending items
        with self.service.store.connect() as conn:
            pending_count_after = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = 'u_user6' AND status = 'pending'"
            ).fetchone()["c"]
            self.assertEqual(pending_count_after, 0)


if __name__ == "__main__":
    unittest.main()
