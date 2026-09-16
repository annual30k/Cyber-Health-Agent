"""Tests for generic, read-only active-memory pattern discovery."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from cyber_health import CyberHealthService


class ActiveMemorySuggestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = CyberHealthService(Path(self.temp_dir.name) / "health.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_arbitrary_meal_type_and_food_name_can_form_a_suggestion(self) -> None:
        for index, day in enumerate(("2026-09-01", "2026-09-04", "2026-09-09")):
            self.service.log_meal(
                user_id="u_generic",
                occurred_at=f"{day}T15:00:00+08:00",
                meal_type="加餐",
                foods=[{"name": "紫薯酸奶"}],
                kcal_low=200,
                kcal_high=260,
                idempotency_key=f"meal-{index}",
            )

        before = self.service.get_profile("u_generic")
        result = self.service.get_memory_suggestions(
            user_id="u_generic",
            date="2026-09-10",
            window_days=30,
            limit=3,
        )
        after = self.service.get_profile("u_generic")

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["data"]["suggestions"]), 1)
        suggestion = result["data"]["suggestions"][0]
        self.assertEqual(suggestion["candidate_type"], "repeated_meal_pattern")
        self.assertIn("加餐", suggestion["statement"])
        self.assertIn("紫薯酸奶", suggestion["statement"])
        self.assertEqual(suggestion["evidence"]["evidence_count"], 3)
        self.assertTrue(suggestion["requires_user_confirmation"])
        self.assertEqual(before["state_version"], after["state_version"])

        conn = sqlite3.connect(self.service.store.database_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0], 0)
        finally:
            conn.close()

    def test_single_fact_does_not_trigger_and_suggestion_is_pure_read(self) -> None:
        self.service.log_meal(
            user_id="u_single",
            occurred_at="2026-09-09T15:00:00+08:00",
            meal_type="夜宵",
            foods=[{"name": "临时点心"}],
            kcal_low=100,
            kcal_high=150,
            idempotency_key="single-meal",
        )
        before = self.service.get_profile("u_single")
        result = self.service.get_memory_suggestions(
            user_id="u_single",
            date="2026-09-10",
        )
        after = self.service.get_profile("u_single")

        self.assertEqual(result["data"]["suggestions"], [])
        self.assertEqual(before["state_version"], after["state_version"])

    def test_arbitrary_activity_type_can_form_a_workout_suggestion(self) -> None:
        for index, day in enumerate(("2026-09-01", "2026-09-05", "2026-09-09")):
            self.service.log_workout(
                user_id="u_workout",
                date=day,
                idempotency_key=f"workout-{index}",
                activity_summary={
                    "activity_type": "室内攀岩",
                    "duration_min": 60,
                    "source": "user_report",
                    "user_confirmed": True,
                },
            )

        result = self.service.get_memory_suggestions(
            user_id="u_workout",
            date="2026-09-10",
            limit=3,
        )
        suggestions = result["data"]["suggestions"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["candidate_type"], "repeated_workout_pattern")
        self.assertIn("室内攀岩", suggestions[0]["statement"])

    def test_planned_only_workout_is_not_treated_as_completed(self) -> None:
        for index, day in enumerate(("2026-09-01", "2026-09-05", "2026-09-09")):
            self.service.log_workout(
                user_id="u_planned_only",
                date=day,
                idempotency_key=f"planned-{index}",
                planned_exercises=["用户计划动作"],
            )

        result = self.service.get_memory_suggestions(
            user_id="u_planned_only",
            date="2026-09-10",
            limit=3,
        )
        self.assertEqual(result["data"]["suggestions"], [])

    def test_daily_proposal_budget_suppresses_new_pattern_suggestions(self) -> None:
        for index, day in enumerate(("2026-09-01", "2026-09-05", "2026-09-09")):
            self.service.log_meal(
                user_id="u_budget",
                occurred_at=f"{day}T12:00:00+08:00",
                meal_type="自定义餐别",
                foods=[{"name": "自定义食物"}],
                kcal_low=100,
                kcal_high=150,
                idempotency_key=f"budget-meal-{index}",
            )
        for index in range(3):
            self.service.memory_action(
                user_id="u_budget",
                action_type="propose",
                idempotency_key=f"budget-proposal-{index}",
                payload={"candidate_key": f"manual-{index}", "content": "用户明确提出的候选"},
            )

        result = self.service.get_memory_suggestions(
            user_id="u_budget",
            date="2026-09-10",
            limit=3,
        )
        self.assertEqual(result["data"]["daily_proposal_count"], 3)
        self.assertEqual(result["data"]["suggestions"], [])
