"""First-run intake must be actionable and must gate personalized plans."""

from __future__ import annotations

import tempfile
import unittest
import base64
from pathlib import Path

from cyber_health import CyberHealthService
from cyber_health_mcp.server import create_mcp_server


class OnboardingFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "onboarding.sqlite3"

    @staticmethod
    def _tool(server, name: str):
        return next(tool.fn for tool in server._tool_manager.list_tools() if tool.name == name)

    def test_new_profile_returns_grouped_intake_without_creating_a_row(self) -> None:
        service = CyberHealthService(self.db_path)

        profile = service.get_profile("new-user")

        self.assertFalse(profile["exists"])
        self.assertEqual(profile["onboarding"]["status"], "required")
        self.assertFalse(profile["onboarding"]["complete"])
        self.assertFalse(profile["onboarding"]["training_plan_ready"])
        self.assertFalse(profile["onboarding"]["nutrition_plan_ready"])
        self.assertIn("constraints.weight_kg", profile["onboarding"]["missing_fields"])
        self.assertIn("goals.goal_type", profile["onboarding"]["missing_fields"])
        automation = profile["daily_review_automation"]
        self.assertEqual(automation["declaration_key"], "cyber-health:daily-review:new-user")
        self.assertEqual(automation["schedule"]["expression"], "30 21 * * *")
        workflow = " ".join(automation["workflow"])
        self.assertIn("session search/history", workflow)
        self.assertIn("explicit user messages", workflow)
        self.assertIn("assistant estimates", workflow)
        with service.store.connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM user_profile").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_plan_tools_are_gated_until_relevant_intake_is_complete(self) -> None:
        server = create_mcp_server(self.db_path, allow_all_tools=True)
        training = self._tool(server, "cyber_health_get_training_plan")
        tomorrow = self._tool(server, "cyber_health_plan_tomorrow")

        training_result = training(date="2026-09-07")
        tomorrow_result = tomorrow(
            date="2026-09-08",
            idempotency_key="tomorrow-before-intake",
        )

        self.assertEqual(training_result["status"], "partial")
        self.assertTrue(training_result["onboarding_required"])
        self.assertIsNone(training_result["plan"])
        self.assertEqual(tomorrow_result["status"], "partial")
        self.assertTrue(tomorrow_result["onboarding_required"])
        with CyberHealthService(self.db_path).store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM user_profile").fetchone()["c"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM domain_record").fetchone()["c"], 0)

    def test_default_tool_surface_can_save_intake_and_unlock_plans(self) -> None:
        server = create_mcp_server(self.db_path, allow_all_tools=False)
        names = [tool.name for tool in server._tool_manager.list_tools()]
        self.assertIn("cyber_health_update_profile", names)

        update = self._tool(server, "cyber_health_update_profile")
        result = update(
            idempotency_key="first-intake",
            goals={
                "goal_type": "fat_loss",
                "activity_level": "moderate",
                "training_experience": "beginner",
                "target_kcal_low": 1900,
                "target_kcal_high": 2100,
            },
            constraints={
                "age_range": "30-39",
                "sex": "male",
                "height_cm": 175,
                "weight_kg": 78,
                "medical_conditions": [],
                "injuries": [],
                "allergens": [],
                "dietary_preferences": ["home_cooking"],
                "weekly_training_days": 3,
                "session_duration_min": 45,
                "available_equipment": ["dumbbell"],
            },
        )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["data"]["onboarding"]["complete"])
        self.assertTrue(result["data"]["onboarding"]["training_plan_ready"])
        self.assertTrue(result["data"]["onboarding"]["nutrition_plan_ready"])

    def test_today_reports_questions_for_unverified_meals_and_workout(self) -> None:
        service = CyberHealthService(self.db_path)

        today = service.get_today("new-user", "2026-09-07")
        readiness = today["daily_review_readiness"]

        self.assertFalse(readiness["ready_to_finalize"])
        self.assertEqual(readiness["unverified_meal_types"], ["breakfast", "lunch", "dinner"])
        self.assertEqual(readiness["workout_status"], "unrecorded")
        self.assertEqual(len(readiness["questions"]), 2)

    def test_nightly_review_has_gap_workout_and_detailed_tomorrow_plan(self) -> None:
        service = CyberHealthService(self.db_path)
        service.update_profile(
            user_id="review-user",
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
                user_id="review-user",
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
            user_id="review-user",
            date="2026-09-07",
            planned_exercises=["Dumbbell Goblet Squat"],
            actual_sets=[{"exercise": "Dumbbell Goblet Squat", "sets": 3, "reps": 10}],
            rpe_avg=7.5,
            completion_rate=1.0,
            idempotency_key="workout",
        )

        ready = service.get_today("review-user", "2026-09-07")["daily_review_readiness"]
        review = service.daily_review(
            user_id="review-user",
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

    def test_wearable_screenshot_facts_and_original_are_recalled_across_service_sessions(self) -> None:
        service = CyberHealthService(self.db_path)
        # A valid minimal PNG; the persistence contract is byte-preservation, not image interpretation.
        image_b64 = base64.b64encode(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        ).decode()
        saved = service.log_workout(
            user_id="wearable-user",
            date="2026-09-07",
            idempotency_key="wearable-walk",
            planned_exercises=["室内步行（爬坡）"],
            completion_rate=1.0,
            activity_summary={
                "activity_type": "室内步行（爬坡）",
                "duration_min": 59.0,
                "distance_km": 4.61,
                "active_kcal": 493,
                "total_kcal": 582,
                "avg_heart_rate_bpm": 141,
                "avg_pace_seconds_per_km": 774,
                "exertion": "适中",
                "source": "wearable_screenshot",
            },
            source_image={"media_type": "image/png", "filename": "walk.png", "data_base64": image_b64},
        )
        self.assertEqual(saved["data"]["activity_summary"]["active_kcal"], 493)
        self.assertTrue(saved["data"]["source_image"]["sha256"])

        corrected = service.log_workout(
            user_id="wearable-user",
            date="2026-09-07",
            session_id=saved["data"]["session_id"],
            idempotency_key="wearable-walk-correction",
            activity_summary={"activity_type": "室内步行（爬坡）", "distance_km": 4.61, "source": "wearable_screenshot"},
        )
        self.assertEqual(corrected["data"]["session_id"], saved["data"]["session_id"])

        reloaded = CyberHealthService(self.db_path)
        sessions = reloaded.get_today("wearable-user", "2026-09-07")["daily_review_readiness"]["workout_sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["activity_summary"]["distance_km"], 4.61)
        # Explicit correction replaces the summary while preserving source evidence.
        self.assertTrue(sessions[0]["source_image_saved"])
        exported = reloaded.export_data(user_id="wearable-user")
        body = exported["facts"]["domain_records"][0]["body_json"]
        self.assertIn(image_b64, body)


if __name__ == "__main__":
    unittest.main()
