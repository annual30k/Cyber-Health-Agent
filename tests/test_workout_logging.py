"""Workout facts, wearable screenshot recall and workout completion."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from cyber_health import CyberHealthService


class WearableScreenshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "onboarding.sqlite3"

    @staticmethod
    def _tool(server, name: str):
        return next(tool.fn for tool in server._tool_manager.list_tools() if tool.name == name)

    def test_wearable_screenshot_facts_and_original_are_recalled_across_service_sessions(self) -> None:
        service = CyberHealthService(self.db_path)
        # A valid minimal PNG; the persistence contract is byte-preservation, not image interpretation.
        image_b64 = base64.b64encode(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        ).decode()
        saved = service.log_workout(
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
            date="2026-09-07",
            session_id=saved["data"]["session_id"],
            idempotency_key="wearable-walk-correction",
            activity_summary={"activity_type": "室内步行（爬坡）", "distance_km": 4.61, "source": "wearable_screenshot"},
        )
        self.assertEqual(corrected["data"]["session_id"], saved["data"]["session_id"])

        reloaded = CyberHealthService(self.db_path)
        sessions = reloaded.get_today("2026-09-07")["daily_review_readiness"]["workout_sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["activity_summary"]["distance_km"], 4.61)
        # Explicit correction replaces the summary while preserving source evidence.
        self.assertTrue(sessions[0]["source_image_saved"])
        exported = reloaded.export_data()
        body = exported["facts"]["domain_records"][0]["body_json"]
        self.assertIn(image_b64, body)


class CompleteWorkoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_remaining.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_complete_workout_normal_and_red_flag(self) -> None:
        """Verify normal workout logging and acute red-flag symptom triggering restricted mode."""

        # Normal workout
        res = self.service.complete_workout(
            date="2026-09-04",
            idempotency_key="wk_norm_01",
            completed_exercises=[{"name": "Squat", "weight_kg": 80, "reps": 8, "sets": 3}],
            session_rpe=7.5,
            completion_rate=1.0,
            discomfort_notes="无明显不适，肌肉充血感好",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["data"]["safety_mode"], "normal")
        self.assertEqual(res["data"]["detected_flags"], [])

        # Red-flag workout
        res_rf = self.service.complete_workout(
            date="2026-09-05",
            idempotency_key="wk_rf_01",
            completed_exercises=[],
            session_rpe=9.0,
            completion_rate=0.2,
            discomfort_notes="第三组突发撕裂样剧痛伴晕厥",
        )
        self.assertEqual(res_rf["data"]["safety_mode"], "restricted")
        self.assertTrue(len(res_rf["data"]["detected_flags"]) > 0)
        self.assertTrue(any("SAFETY_RESTRICTED" in w for w in res_rf["warnings"]))

        # Check profile is locked
        prof = self.service.get_profile()
        self.assertEqual(prof["safety_mode"], "restricted")


if __name__ == "__main__":
    unittest.main()
