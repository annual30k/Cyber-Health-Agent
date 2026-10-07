"""Red flags, restricted mode, deload protocol and recovery evidence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from cyber_health import (
    CyberHealthService,
    SafetyRestrictedError,
    ValidationError,
)


class MockWorkingMemoryProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        return {"status": "ok", "candidate_id": f"cand_{len(self.calls)}"}


class RedFlagPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "review.sqlite3")

    def meal(self, key, timestamp, kcal=100):
        return self.service.log_meal(occurred_at=timestamp,
            meal_type="breakfast", foods=[], kcal_low=kcal, kcal_high=kcal + 10,
            idempotency_key=key)

    def test_workout_red_flag_is_persisted(self):
        self.service.log_workout(date="2026-09-04",
            discomfort_notes="严重胸痛", idempotency_key="red-flag")
        self.assertEqual(self.service.get_profile()["safety_mode"], "restricted")

    def test_restricted_plan_never_prescribes_strength_training(self):
        self.service.log_daily_metrics(date="2026-09-04",
            metrics={"notes": "严重胸痛"}, idempotency_key="red")
        try:
            result = self.service.plan_tomorrow(date="2026-09-05", idempotency_key="plan")
        except Exception as error:
            self.assertEqual(getattr(error, "code", None), "SAFETY_RESTRICTED")
        else:
            self.assertNotIn("标准力量训练", str(result))


class ProfileRedFlagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")

    def test_profile_red_flag_sets_restricted_mode(self):
        self.service.update_profile(safety_flags=["严重胸痛"], idempotency_key="profile")
        self.assertEqual(self.service.get_profile()["safety_mode"], "restricted")


class SleepRecoveryRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "source.sqlite3")

    def test_sleep_below_six_always_uses_recovery_rule(self):
        self.service.log_daily_metrics(date="2026-09-04",
            metrics={"sleep_hours": 5.9, "fatigue_level": 1}, idempotency_key="metrics")
        result = self.service.get_training_plan(date="2026-09-04")
        self.assertEqual(result["data"]["plan"]["rule_code"], "TRAIN_RECOVERY_01")


class RecoveryAndDeloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_advanced.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_daily_metrics_and_recovery_score(self) -> None:
        """Sleep < 6 or fatigue >= 7 triggers TRAIN_RECOVERY_01 and lowers recovery score."""
        res = self.service.log_daily_metrics(
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
            date="2026-09-04",
            metrics={
                "soreness_locations": ["严重胸痛", "呼吸困难"],
            },
            idempotency_key="u3-rf-1",
        )
        self.assertIn("TRAIN_SAFETY_01", res_flag["data"]["triggered_rules"])

        prof = self.service.get_profile()
        self.assertEqual(prof["safety_mode"], "restricted")
        self.assertIn("严重胸痛", prof["safety_flags"])

        # 2. Attempting to log a workout in restricted mode must raise SafetyRestrictedError
        with self.assertRaises(SafetyRestrictedError) as caught:
            self.service.log_workout(
                date="2026-09-04",
                planned_exercises=["Bench Press"],
                idempotency_key="u3-wo-fail",
            )
        self.assertEqual(caught.exception.code, "SAFETY_RESTRICTED")

        # 3. Attempting to clear safety flags without clearance reason must fail
        with self.assertRaises(ValidationError):
            self.service.update_profile(
                clear_safety_flags=True,
                clearance_reason="",
                idempotency_key="u3-clear-bad",
            )

        # 4. Clear safety flags with valid clearance reason
        clear_res = self.service.update_profile(
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
            date="2026-09-04",
            planned_exercises=["Goblet Squat"],
            actual_sets=[{"exercise": "Goblet Squat", "weight_kg": 12, "reps": 10, "rir": 4}],
            idempotency_key="u3-wo-ok",
        )
        self.assertEqual(wo_res["status"], "success")
        self.assertTrue(any("Deload Period" in w for w in wo_res["warnings"]))


class RecoveryEvidenceFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_round9.sqlite3"
        self.service = CyberHealthService(self.db_path, recovery_evidence_window_days=1)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_stale_daily_state_evidence_rejected(self) -> None:
        """Verify that historical daily metrics from 60 days ago are not treated as fresh evidence for today."""

        # Log severe sleep deficit on 2026-07-01 (65 days before 2026-09-05)
        self.service.log_daily_metrics(
            date="2026-07-01",
            metrics={"sleep_hours": 3.0, "fatigue_level": 9},
            idempotency_key="m-stale-01",
        )

        # Query plan for 2026-09-05:
        # Must NOT treat the 65-day-old sleep deficit as today's fatigue (TRAIN_RECOVERY_01 must NOT trigger)
        # Must NOT treat state as verified_recent_state
        plan = self.service.get_training_plan(date="2026-09-05", evidence_window_days=1)
        p_data = plan["plan"]

        self.assertEqual(p_data["rule_code"], "TRAIN_PROGRESSION_STANDARD")
        self.assertEqual(p_data["state_evidence"], "unrecorded_recent_state")
        self.assertIn("未检测到近期体征记录", p_data["guidance"])
        # recovery_score must be None (not a fabricated 75)
        self.assertIsNone(plan["recovery_score"])

        # Now log fresh metrics for 2026-09-05:
        self.service.log_daily_metrics(
            date="2026-09-05",
            metrics={"sleep_hours": 8.0, "fatigue_level": 2},
            idempotency_key="m-fresh-01",
        )
        fresh_plan = self.service.get_training_plan(date="2026-09-05", evidence_window_days=1)
        self.assertEqual(fresh_plan["plan"]["state_evidence"], "verified_recent_state")
        self.assertIsNotNone(fresh_plan["recovery_score"])


if __name__ == "__main__":
    unittest.main()
