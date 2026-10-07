"""Profile intake, onboarding gates and the plan-unlocking flow."""

from __future__ import annotations

import tempfile
import unittest
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

        profile = service.get_profile()

        self.assertFalse(profile["exists"])
        self.assertEqual(profile["onboarding"]["status"], "required")
        self.assertFalse(profile["onboarding"]["complete"])
        self.assertFalse(profile["onboarding"]["training_plan_ready"])
        self.assertFalse(profile["onboarding"]["nutrition_plan_ready"])
        self.assertIn("constraints.weight_kg", profile["onboarding"]["missing_fields"])
        self.assertIn("goals.goal_type", profile["onboarding"]["missing_fields"])
        automation = profile["daily_review_automation"]
        self.assertEqual(automation["declaration_key"], "cyber-health:daily-review:owner")
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


if __name__ == "__main__":
    unittest.main()
