"""Nightly fact collection, daily review and tomorrow's plan."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cyber_health import CyberHealthService
from cyber_health.memory import MemoryUnavailable
from cyber_health.store import SQLiteStore
from test_support import OWNER, fixed_clock


class MockWorkingMemoryProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        return {"status": "ok", "candidate_id": f"cand_{len(self.calls)}"}




class MockMemoryProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.enabled: bool = True

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise MemoryUnavailable("Obsidian remote adapter offline")
        self.calls.append((method, payload))
        return {"status": "ok", "candidate_id": f"cand_{len(self.calls)}"}


class FailingMemoryProvider:
    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise MemoryUnavailable("Obsidian remote adapter connection timeout")


class DailyReviewAndPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_advanced.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_daily_review_and_plan_tomorrow(self) -> None:
        """Daily review distinguishes no_data from zero intake, and plan_tomorrow handles commit."""
        # Unrecorded day review
        rev_empty = self.service.daily_review(
            date="2026-09-04",
            idempotency_key="u4-rev-1",
        )
        self.assertEqual(rev_empty["data"]["recording_status"], "no_data")
        self.assertIn("未记录", rev_empty["data"]["summary"])

        # Commit tomorrow's plan
        plan_res = self.service.plan_tomorrow(
            date="2026-09-05",
            idempotency_key="u4-plan-commit",
            commit=True,
        )
        self.assertEqual(plan_res["data"]["status"], "committed")

        # Verify today status on Sept 5 reflects committed plan state
        today_sept5 = self.service.get_today("2026-09-05")
        self.assertEqual(today_sept5["plan_status"]["state"], "committed")


class UnconfiguredTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = CyberHealthService(Path(self.tmp.name) / "test.db")

    def test_plan_does_not_invent_nutrition_targets(self):
        result = self.s.plan_tomorrow(date="2026-09-05", idempotency_key="plan")
        self.assertNotIn('unconfigured_default', json.dumps(result))
        self.assertNotIn('1800', json.dumps(result))

    def test_review_does_not_invent_nutrition_targets(self):
        result = self.s.daily_review(date="2026-09-04", idempotency_key="review")
        self.assertNotIn('unconfigured_default', json.dumps(result))
        self.assertNotIn('1800', json.dumps(result))


class NightlyFactCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "onboarding.sqlite3"

    @staticmethod
    def _tool(server, name: str):
        return next(tool.fn for tool in server._tool_manager.list_tools() if tool.name == name)

    def test_today_reports_questions_for_unverified_meals_and_workout(self) -> None:
        service = CyberHealthService(self.db_path)

        today = service.get_today("2026-09-07")
        readiness = today["daily_review_readiness"]

        self.assertFalse(readiness["ready_to_finalize"])
        self.assertEqual(readiness["unverified_meal_types"], ["breakfast", "lunch", "dinner"])
        self.assertEqual(readiness["workout_status"], "unrecorded")
        self.assertEqual(len(readiness["questions"]), 2)

    def test_nightly_review_has_gap_workout_and_detailed_tomorrow_plan(self) -> None:
        service = CyberHealthService(self.db_path)
        service.update_profile(
            idempotency_key="profile",
            goals={
                "goal_type": "fat_loss",
                "activity_level": "moderate",
                "training_experience": "beginner",
                "target_kcal_low": 1900,
                "target_kcal_high": 2100,
                "target_protein_low": 120,
                "target_protein_high": 140,
            },
            constraints={
                "available_equipment": ["dumbbell"],
                "session_duration_min": 45,
            },
        )
        meals = [
            ("breakfast", 400, 500, 25, 30),
            ("lunch", 500, 600, 35, 40),
            ("dinner", 600, 600, 45, 50),
        ]
        for index, (meal_type, kcal_low, kcal_high, protein_low, protein_high) in enumerate(meals):
            service.log_meal(
                occurred_at=f"2026-09-07T{8 + index * 5:02d}:00:00+08:00",
                meal_type=meal_type,
                foods=[{"name": meal_type, "amount_g": {"low": 100, "high": 120}}],
                kcal_low=kcal_low,
                kcal_high=kcal_high,
                protein_low=protein_low,
                protein_high=protein_high,
                idempotency_key=f"meal-{index}",
            )
        service.log_workout(
            date="2026-09-07",
            planned_exercises=["Dumbbell Goblet Squat"],
            actual_sets=[{"exercise": "Dumbbell Goblet Squat", "sets": 3, "reps": 10}],
            rpe_avg=7.5,
            completion_rate=1.0,
            idempotency_key="workout",
        )

        ready = service.get_today("2026-09-07")["daily_review_readiness"]
        review = service.daily_review(
            date="2026-09-07",
            idempotency_key="review",
        )["data"]

        self.assertTrue(ready["ready_to_finalize"])
        self.assertEqual(review["nutrition_analysis"]["calorie_target_gap_range"], [200, 600])
        self.assertEqual(review["nutrition_analysis"]["protein_target_gap_range"], [0, 35])
        self.assertEqual(review["workout_analysis"]["status"], "completed")
        training = review["tomorrow_draft_plan"]["training_plan"]
        self.assertEqual(training["equipment_mode"], "dumbbell")
        self.assertEqual(training["target_duration_min"], 45)


class ReviewMaintenanceHintTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_round13.db")
        self.store = SQLiteStore(self.db_path)
        self.memory_provider = MockMemoryProvider()
        self.service = CyberHealthService(self.store, memory_provider=self.memory_provider, clock=fixed_clock())

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_daily_review_and_maintenance_hints_without_fake_rule_candidate(self):
        """daily_review does NOT insert fake memory rule candidates; maintenance hints reflect genuine due work."""
        date = "2026-09-05"

        # Log a meal and metric so review has content
        self.service.log_meal(
            occurred_at=f"{date}T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "鸡肉饭", "amount_g": {"low": 300, "high": 300}}],
            kcal_low=400,
            kcal_high=500,
            protein_low=30,
            protein_high=35,
            idempotency_key="lunch-1",
        )

        res = self.service.daily_review(
            date=date,
            idempotency_key="rev-k1",
        )

        # 1. Without due work, daily_review reports maintenance_recommended = False
        self.assertFalse(res["data"]["maintenance_recommended"])
        self.assertIsNone(res["data"]["suggested_action"])
        self.assertIsNone(res["data"]["maintenance_key"])

        # 2. Daily review strictly did NOT insert any fake rule candidate into memory_outbox
        with self.store.connect() as conn:
            outbox_cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = ?",
                (OWNER,),
            ).fetchone()["c"]
            self.assertEqual(outbox_cnt, 0, "daily_review must not pollute memory_outbox with fake rule candidates")

        # 3. Propose a legitimate memory candidate while provider is offline so it defers to outbox
        self.memory_provider.enabled = False
        self.service.propose_memory_candidate(
            method="memory.propose",
            payload={"rule": "high_protein_preference"},
            idempotency_key="cand-prop-1",
        )
        self.memory_provider.enabled = True

        # 4. Now get_today accurately recommends maintenance with generation key
        today_res = self.service.get_today(day=date)
        self.assertTrue(today_res["data"]["maintenance_recommended"])
        self.assertEqual(today_res["data"]["suggested_action"], "cyber_health_maintain_memory")
        self.assertTrue(today_res["data"]["maintenance_key"].startswith(f"maint_{OWNER}_{date}_g"))
        self.assertEqual(today_res["data"]["maintenance"]["pending_outbox_count"], 1)


class ReviewProposalDisciplineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_round14.db")
        self.store = SQLiteStore(self.db_path)
        self.memory_provider = MockMemoryProvider()
        self.service = CyberHealthService(self.store, memory_provider=self.memory_provider, clock=fixed_clock())

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_daily_review_does_not_spam_rule_proposals_or_bypass_propose_candidate(self):
        """daily_review does NOT insert fake propose candidates into outbox or invoke provider as health rules."""
        date = "2026-09-05"

        # Execute daily_review
        rev_res = self.service.daily_review(
            date=date,
            idempotency_key="rev-isolate-1",
            user_notes="无任何红旗症状，睡眠良好。",
        )
        self.assertEqual(rev_res["status"], "success")

        # 1. Check outbox: exactly 0 items
        with self.store.connect() as conn:
            cnt = conn.execute("SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = ?", (OWNER,)).fetchone()["c"]
            self.assertEqual(cnt, 0, "daily_review must NOT write candidates into memory_outbox")

        # 2. Check Provider: exactly 0 calls made
        self.assertEqual(len(self.memory_provider.calls), 0)

        # 3. Memory candidates must come through propose_memory_candidate with 'memory.propose'
        prop_res = self.service.propose_memory_candidate(
            method="memory.propose",
            payload={"verified_health_rule": "lactose_intolerance"},
            idempotency_key="prop-rule-1",
        )
        self.assertEqual(prop_res["status"], "success")
        self.assertEqual(len(self.memory_provider.calls), 1)
        self.assertEqual(self.memory_provider.calls[0][0], "memory.propose")
        self.assertIn("verified_health_rule", self.memory_provider.calls[0][1])


if __name__ == "__main__":
    unittest.main()
