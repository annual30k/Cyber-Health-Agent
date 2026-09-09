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

    def test_uncertainty_aggregation_without_fake_confidence_intervals(self) -> None:
        """Aggregate estimate ranges transparently without inventing a confidence level."""
        user_id = "u_stats_user"
        date = "2026-09-08"

        # 1. Configure profile targets
        self.service.update_profile(
            user_id=user_id,
            idempotency_key="u_stats_prof",
            goals={
                "target_kcal_low": 2100,
                "target_kcal_high": 2300,
                "target_protein_low": 120,
                "target_protein_high": 150,
            },
        )

        # 2. Log 5 meals simulating the user's real day
        meals = [
            ("breakfast", 190, 230, 17, 22),
            ("lunch", 260, 320, 24, 30),
            ("snack", 80, 100, 0, 1),
            ("snack", 350, 500, 30, 45),
            ("dinner", 550, 750, 38, 50),
        ]
        for idx, (mtype, k_low, k_high, p_low, p_high) in enumerate(meals):
            self.service.log_meal(
                user_id=user_id,
                occurred_at=f"{date}T{8 + idx * 3:02d}:00:00+08:00",
                meal_type=mtype,
                foods=[{"name": f"Item {idx}"}],
                kcal_low=k_low,
                kcal_high=k_high,
                protein_low=p_low,
                protein_high=p_high,
                idempotency_key=f"u_stats_meal_{idx}",
            )

        # 3. Daily review
        review = self.service.daily_review(
            user_id=user_id,
            date=date,
            idempotency_key="u_stats_review",
        )
        data = review["data"]
        analysis = data["nutrition_analysis"]

        # Classic bounds are preserved
        self.assertEqual(analysis["intake_kcal_range"], [1430, 1900])
        self.assertEqual(analysis["intake_protein_g_range"], [109, 148])
        raw_kcal_spread = 1900 - 1430  # 470
        raw_protein_spread = 148 - 109  # 39

        # The heuristic uncertainty range contracts, but is not a confidence interval.
        kcal_uncertainty = analysis["intake_kcal_uncertainty_range"]
        stat_kcal_spread = kcal_uncertainty[1] - kcal_uncertainty[0]
        self.assertLess(stat_kcal_spread, raw_kcal_spread * 0.65)  # Contracted by >35%
        self.assertEqual(analysis["intake_kcal_mid"], 1665)
        self.assertIsNone(analysis["intake_kcal_ci90"])

        protein_uncertainty = analysis["intake_protein_uncertainty_range"]
        stat_protein_spread = protein_uncertainty[1] - protein_uncertainty[0]
        self.assertLess(stat_protein_spread, raw_protein_spread * 0.70)
        self.assertEqual(analysis["intake_protein_mid"], 128)
        self.assertIsNone(analysis["intake_protein_ci90"])

        # Target gap is arithmetic against a policy interval; it is not a CI.
        self.assertEqual(analysis["calorie_gap_mid"], 535)  # 2200 - 1665
        raw_gap_spread = analysis["calorie_target_gap_range"][1] - analysis["calorie_target_gap_range"][0]  # 870 - 200 = 670
        self.assertEqual(analysis["calorie_gap_uncertainty_range"], analysis["calorie_target_gap_range"])
        self.assertEqual(
            analysis["calorie_gap_uncertainty_range"][1] - analysis["calorie_gap_uncertainty_range"][0],
            raw_gap_spread,
        )
        self.assertIsNone(analysis["calorie_gap_ci90"])

        # The heuristic intake range remains inside the physical estimate bounds.
        self.assertGreaterEqual(kcal_uncertainty[0], 1430)
        self.assertLessEqual(kcal_uncertainty[1], 1900)
        self.assertGreaterEqual(analysis["intake_kcal_mid"], kcal_uncertainty[0])
        self.assertLessEqual(analysis["intake_kcal_mid"], kcal_uncertainty[1])
        self.assertEqual(analysis["protein_gap_uncertainty_range"], analysis["protein_target_gap_range"])
        self.assertIsNone(analysis["protein_gap_ci90"])

        # Check get_today remaining clamping consistency
        today = self.service.get_today(user_id=user_id, day=date)
        rem = today["remaining"]
        self.assertGreaterEqual(rem["kcal_mid"], rem["kcal_low"])
        self.assertLessEqual(rem["kcal_mid"], rem["kcal_high"])
        self.assertGreaterEqual(rem["protein_mid"], rem["protein_low"])
        self.assertLessEqual(rem["protein_mid"], rem["protein_high"])

        # Summary uses transparent heuristic terminology, never a false CI label.
        self.assertIn("1665 kcal", data["summary"])
        self.assertIn("128 g", data["summary"])
        self.assertIn("合成不确定性范围", data["summary"])
        self.assertNotIn("90%置信区间", data["summary"])
        self.assertNotIn("极差", data["summary"])

    def test_single_meal_interval_consistency(self) -> None:
        """A single meal keeps its estimate bounds and emits no unsupported CI."""
        user_id = "u_single_meal"
        date = "2026-09-08"
        self.service.update_profile(
            user_id=user_id,
            idempotency_key="u_single_prof",
            goals={
                "target_kcal_low": 2000,
                "target_kcal_high": 2200,
                "target_protein_low": 100,
                "target_protein_high": 120,
            },
        )
        self.service.log_meal(
            user_id=user_id,
            occurred_at=f"{date}T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Chicken rice"}],
            kcal_low=500,
            kcal_high=700,
            protein_low=30,
            protein_high=40,
            idempotency_key="u_single_meal_1",
        )
        review = self.service.daily_review(
            user_id=user_id,
            date=date,
            idempotency_key="u_single_review",
        )
        data = review["data"]
        analysis = data["nutrition_analysis"]

        self.assertEqual(analysis["intake_kcal_range"], [500, 700])
        self.assertEqual(analysis["intake_kcal_uncertainty_range"], [500, 700])
        self.assertIsNone(analysis["intake_kcal_ci90"])
        self.assertEqual(analysis["intake_kcal_mid"], 600)
        # Target gap remains the arithmetic policy-vs-intake interval.
        self.assertEqual(analysis["calorie_target_gap_range"], [1300, 1700])
        self.assertEqual(analysis["calorie_gap_uncertainty_range"], [1300, 1700])
        self.assertIsNone(analysis["calorie_gap_ci90"])
        self.assertEqual(analysis["calorie_gap_mid"], 1500)
        self.assertEqual(analysis["protein_target_gap_range"], [60, 90])
        self.assertEqual(analysis["protein_gap_uncertainty_range"], [60, 90])
        self.assertIsNone(analysis["protein_gap_ci90"])
        self.assertEqual(analysis["protein_gap_mid"], 75)
        self.assertIn("估算范围 500–700 kcal", data["summary"])
        self.assertNotIn("90%置信区间", data["summary"])

    def test_statistical_single_meal_vs_multi_meal_mathematical_properties(self) -> None:
        """Verify heuristic aggregation and the separation of policy gaps from uncertainty."""
        user_id = "u_math_test"
        date = "2026-09-08"
        self.service.update_profile(
            user_id=user_id,
            idempotency_key="u_math_prof",
            goals={
                "target_kcal_low": 2000,
                "target_kcal_high": 2200,
                "target_protein_low": 100,
                "target_protein_high": 120,
            },
        )

        # 1. Log First Meal (n=1)
        self.service.log_meal(
            user_id=user_id,
            occurred_at=f"{date}T08:00:00+08:00",
            meal_type="breakfast",
            foods=[{"name": "Oatmeal and eggs"}],
            kcal_low=400,
            kcal_high=600,
            protein_low=20,
            protein_high=30,
            idempotency_key="u_math_meal_1",
        )
        review1 = self.service.daily_review(
            user_id=user_id,
            date=date,
            idempotency_key="u_math_rev_1",
        )
        ana1 = review1["data"]["nutrition_analysis"]

        # For n=1 the uncertainty range equals the recorded estimate bounds.
        self.assertEqual(ana1["intake_kcal_uncertainty_range"], [400, 600])
        self.assertIsNone(ana1["intake_kcal_ci90"])
        self.assertEqual(ana1["intake_kcal_mid"], 500)
        self.assertEqual(ana1["calorie_target_gap_range"], [1400, 1800])
        self.assertEqual(ana1["calorie_gap_uncertainty_range"], [1400, 1800])
        self.assertIsNone(ana1["calorie_gap_ci90"])
        self.assertEqual(ana1["calorie_gap_mid"], 1600)  # 2100 - 500

        # 2. Log Second Meal (n=2) -> CLT applies
        self.service.log_meal(
            user_id=user_id,
            occurred_at=f"{date}T12:30:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Salmon and sweet potato"}],
            kcal_low=600,
            kcal_high=800,
            protein_low=35,
            protein_high=45,
            idempotency_key="u_math_meal_2",
        )
        review2 = self.service.daily_review(
            user_id=user_id,
            date=date,
            idempotency_key="u_math_rev_2",
        )
        ana2 = review2["data"]["nutrition_analysis"]

        # Intake bounds for n=2: sum of bounds = [1000, 1400], spread = 400.
        self.assertEqual(ana2["intake_kcal_range"], [1000, 1400])
        raw_spread = 1400 - 1000
        stat_spread = ana2["intake_kcal_uncertainty_range"][1] - ana2["intake_kcal_uncertainty_range"][0]
        # RSS half-width is a documented display heuristic, not a CI.
        self.assertLess(stat_spread, raw_spread * 0.75)
        self.assertEqual(ana2["intake_kcal_mid"], 1200)
        self.assertIsNone(ana2["intake_kcal_ci90"])

        # Strict containment guarantees for the heuristic range.
        self.assertGreaterEqual(ana2["intake_kcal_uncertainty_range"][0], ana2["intake_kcal_range"][0])
        self.assertLessEqual(ana2["intake_kcal_uncertainty_range"][1], ana2["intake_kcal_range"][1])
        self.assertGreaterEqual(ana2["intake_kcal_mid"], ana2["intake_kcal_uncertainty_range"][0])
        self.assertLessEqual(ana2["intake_kcal_mid"], ana2["intake_kcal_uncertainty_range"][1])

        # Target gap is always the raw arithmetic policy-vs-intake interval:
        # target = [2000, 2200], intake = [1000, 1400]
        # raw_gap = [2000 - 1400, 2200 - 1000] = [600, 1200]
        self.assertEqual(ana2["calorie_target_gap_range"], [600, 1200])
        raw_gap_spread = 1200 - 600  # 600
        self.assertEqual(ana2["calorie_gap_uncertainty_range"], ana2["calorie_target_gap_range"])
        self.assertEqual(
            ana2["calorie_gap_uncertainty_range"][1] - ana2["calorie_gap_uncertainty_range"][0],
            raw_gap_spread,
        )
        self.assertIsNone(ana2["calorie_gap_ci90"])
        self.assertEqual(ana2["calorie_gap_mid"], 900)  # 2100 - 1200

        self.assertGreaterEqual(ana2["calorie_gap_mid"], ana2["calorie_gap_uncertainty_range"][0])
        self.assertLessEqual(ana2["calorie_gap_mid"], ana2["calorie_gap_uncertainty_range"][1])

    def test_zero_variance_exact_meal_bounds(self) -> None:
        """When user logs exact values (low == high), variance is zero, mid equals value, and bounds match."""
        user_id = "u_exact_meal"
        date = "2026-09-08"
        self.service.update_profile(
            user_id=user_id,
            idempotency_key="u_exact_prof",
            goals={
                "target_kcal_low": 2000,
                "target_kcal_high": 2000,
                "target_protein_low": 100,
                "target_protein_high": 100,
            },
        )
        self.service.log_meal(
            user_id=user_id,
            occurred_at=f"{date}T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Measured meal"}],
            kcal_low=600,
            kcal_high=600,
            protein_low=40,
            protein_high=40,
            idempotency_key="u_exact_meal_1",
        )
        review = self.service.daily_review(
            user_id=user_id,
            date=date,
            idempotency_key="u_exact_rev",
        )
        analysis = review["data"]["nutrition_analysis"]
        self.assertEqual(analysis["intake_kcal_range"], [600, 600])
        self.assertEqual(analysis["intake_kcal_uncertainty_range"], [600, 600])
        self.assertIsNone(analysis["intake_kcal_ci90"])
        self.assertEqual(analysis["intake_kcal_mid"], 600)
        self.assertEqual(analysis["calorie_target_gap_range"], [1400, 1400])
        self.assertEqual(analysis["calorie_gap_uncertainty_range"], [1400, 1400])
        self.assertIsNone(analysis["calorie_gap_ci90"])
        self.assertEqual(analysis["calorie_gap_mid"], 1400)


if __name__ == "__main__":
    unittest.main()
