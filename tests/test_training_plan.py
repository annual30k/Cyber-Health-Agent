"""Training prescription, constraints, equipment and exercise substitution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from cyber_health import CyberHealthService


class QueryMemoryProvider:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        if method == "query":
            return {"items": self.items}
        return {"acknowledged": True}


class PrescriptionConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_round9.sqlite3"
        self.service = CyberHealthService(self.db_path, recovery_evidence_window_days=1)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_combined_constraints_knee_and_lumbar(self) -> None:
        """Verify that concurrent knee and lumbar constraints eliminate contraindicated
        exercises across all tiers without contradictory prescriptions.
        """
        self.service.update_profile(
            constraints={"injuries": "knee pain with deep flexion, lumbar disc herniation"},
            idempotency_key="prof-kl-01",
        )

        # 1. Barbell mode:
        # Back Squat is contraindicated (knee + lumbar).
        # Romanian Deadlift is contraindicated (lumbar).
        # Safe selections: Barbell Hip Thrust (knee/spine friendly) and Glute Bridge.
        p_bb = self.service.get_training_plan(date="2026-09-05", equipment=["barbell"])
        bb_names = [e["name"] for e in p_bb["plan"]["prescribed_exercises"]]
        self.assertNotIn("Barbell Back Squat", bb_names)
        self.assertNotIn("Romanian Deadlift", bb_names)
        self.assertIn("Barbell Hip Thrust", bb_names)
        self.assertIn("Glute Bridge", bb_names)

        # 2. Dumbbell mode in Recovery tier (with fatigue)
        self.service.log_daily_metrics(
            date="2026-09-05",
            metrics={"fatigue_level": 8, "sleep_hours": 5.5},
            idempotency_key="m-kl-fatigue",
        )
        p_db_rec = self.service.get_training_plan(date="2026-09-05", equipment=["dumbbell"])
        db_names = [e["name"] for e in p_db_rec["plan"]["prescribed_exercises"]]
        # Must NOT pick Goblet Squat (knee) or Dumbbell Romanian Deadlift (lumbar)
        self.assertNotIn("Dumbbell Goblet Squat", db_names)
        self.assertNotIn("Dumbbell Romanian Deadlift", db_names)
        # Must select safe alternative: Dumbbell Hip Thrust or Glute Bridge
        self.assertTrue(any(name in ("Dumbbell Hip Thrust", "Glute Bridge") for name in db_names))

    def test_combined_constraints_all_contraindicated_suspends_safely(self) -> None:
        """Verify that when concurrent constraints eliminate all safe candidates,
        it safely suspends specific resistance movements and provides non-diagnostic guidance.
        """
        self.service.update_profile(
            constraints={"injuries": "knee, shoulder, and lumbar severe injuries, avoid all squat, push, and axial loads"},
            idempotency_key="prof-all-c",
        )
        p_plan = self.service.get_training_plan(date="2026-09-05", equipment=["barbell"])
        prescribed_names = [e["name"] for e in p_plan["plan"]["prescribed_exercises"]]
        self.assertNotIn("Barbell Back Squat", prescribed_names)
        self.assertNotIn("Barbell Bench Press", prescribed_names)
        self.assertNotIn("Romanian Deadlift", prescribed_names)

    def test_structured_rest_and_unknown_load_disclosure(self) -> None:
        """Verify prescribed exercises include structured rest duration and do not fabricate unknown loads."""
        plan = self.service.get_training_plan(date="2026-09-05", equipment=["barbell"])
        exercises = plan["plan"]["prescribed_exercises"]
        self.assertTrue(len(exercises) > 0)

        for ex in exercises:
            # Structured rest seconds
            self.assertIn("rest_seconds", ex)
            self.assertGreater(ex["rest_seconds"], 0)
            # Rep ranges
            self.assertIn("target_reps_min", ex)
            self.assertIn("target_reps_max", ex)
            # Unknown load: user has no workout history, so suggested_weight_kg must be None, NOT fabricated
            self.assertIsNone(ex["suggested_weight_kg"])
            self.assertIn("负荷未知", ex["weight_guidance"])

    def test_movement_pattern_substitution(self) -> None:
        """Verify substitute_exercise finds same-movement-pattern alternatives respecting constraints."""

        # 1. Substitute Barbell Bench Press (upper_push) with dumbbell equipment
        sub1 = self.service.substitute_exercise(
            original_exercise="Barbell Bench Press",
            equipment=["dumbbell"],
        )
        self.assertEqual(sub1["status"], "success")
        self.assertEqual(sub1["data"]["movement_pattern"], "upper_push")
        sub_names = [c["name"] for c in sub1["data"]["substitutes"]]
        self.assertIn("Dumbbell Floor Press", sub_names)
        self.assertNotIn("Barbell Bench Press", sub_names)

        # 2. Substitute with shoulder discomfort: Dumbbell Floor Press and Pushup contraindicated
        sub2 = self.service.substitute_exercise(
            original_exercise="Barbell Bench Press",
            discomfort_joint="shoulder",
            equipment=["dumbbell"],
        )
        # All upper_push movements strain the shoulder -> no safe substitute in that pattern
        self.assertEqual(sub2["status"], "no_substitute_available")
        self.assertEqual(len(sub2["data"]["substitutes"]), 0)


class PrescriptionDecisionMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test_mem_trends.sqlite3")
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_training_prescription_unified_decision_matrix(self) -> None:
        """Verify unified decision matrix: constraints, equipment, experience, evidence, and non-diagnostic disclaimers."""
        # 1. Knee constraint substitution & unrecorded state disclosure
        self.service.update_profile(
            constraints={"knee_injury": "patellofemoral pain, avoid deep squat"},
            idempotency_key="prof-knee-01",
        )
        plan_knee = self.service.get_training_plan(date="2026-09-05", equipment=["barbell"])
        p_data = plan_knee["plan"]
        self.assertEqual(p_data["rule_code"], "TRAIN_PROGRESSION_STANDARD")
        self.assertEqual(p_data["state_evidence"], "unrecorded_recent_state")
        self.assertEqual(p_data["training_experience"], "unconfigured")
        self.assertEqual(p_data["equipment_mode"], "barbell")
        # Must not contain ungrounded "状态优良"
        self.assertNotIn("状态优良", p_data["guidance"])
        self.assertIn("建议每日录入晨起体征", p_data["guidance"])
        # Squat contraindicated: Barbell Back Squat replaced with Barbell Hip Thrust
        ex_names = [e["name"] for e in p_data["prescribed_exercises"]]
        self.assertNotIn("Barbell Back Squat", ex_names)
        self.assertIn("Barbell Hip Thrust", ex_names)
        # Disclaimer present
        self.assertIn("disclaimer", p_data)
        self.assertIn("不构成医疗处方", p_data["disclaimer"])

        # 2. Bodyweight-only equipment adaptation
        plan_bw = self.service.get_training_plan(date="2026-09-05", equipment=["bodyweight"])
        p_bw_data = plan_bw["plan"]
        self.assertEqual(p_bw_data["equipment_mode"], "bodyweight")
        bw_ex_names = [e["name"] for e in p_bw_data["prescribed_exercises"]]
        for name in bw_ex_names:
            self.assertNotIn("Barbell", name)
            self.assertNotIn("Dumbbell", name)

        # 3. Shoulder constraint in Deload mode: Incline Pushup replaced with Bird Dog
        self.service.update_profile(
            constraints={"shoulder": "rotator cuff tendinitis, avoid pushup"},
            safety_flags=["chest_pain"],
            idempotency_key="prof-shoulder-deload",
        )
        # Clear flag with clearance to enter 7-day deload
        self.service.update_profile(
            clear_safety_flags=True,
            clearance_reason="Physician clearance issued",
            idempotency_key="prof-shoulder-clear",
        )
        plan_deload = self.service.get_training_plan(date="2026-09-05")
        p_dl = plan_deload["plan"]
        self.assertEqual(p_dl["rule_code"], "RECOVERY_FLAG_CLEAR_01")
        dl_ex_names = [e["name"] for e in p_dl["prescribed_exercises"]]
        self.assertNotIn("Incline Pushup", dl_ex_names)
        self.assertIn("Bird Dog", dl_ex_names)


class TrainingPlanStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_remaining.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_get_training_plan_states(self) -> None:
        """Verify training plan prescription across normal, fatigue, restricted, and deload states."""

        # 1. Normal state -> progressive overload
        res_std = self.service.get_training_plan(date="2026-09-04")
        self.assertEqual(res_std["status"], "success")
        self.assertEqual(res_std["plan"]["rule_code"], "TRAIN_PROGRESSION_STANDARD")
        self.assertEqual(res_std["plan"]["intensity_baseline_pct"], 100)
        self.assertTrue(len(res_std["plan"]["prescribed_exercises"]) > 0)

        # 2. Fatigue state: log metrics with fatigue >= 7
        self.service.log_daily_metrics(
            date="2026-09-04",
            metrics={"fatigue_level": 8, "sleep_hours": 5.0},
            idempotency_key="metric_fatigue_01",
        )
        res_fatigue = self.service.get_training_plan(date="2026-09-04")
        self.assertEqual(res_fatigue["plan"]["rule_code"], "TRAIN_RECOVERY_01")
        self.assertEqual(res_fatigue["plan"]["intensity_baseline_pct"], 70)

        # 3. Restricted mode: trigger red flag via workout check-in
        self.service.complete_workout(
            date="2026-09-04",
            idempotency_key="chk_redflag_01",
            discomfort_notes="锻炼中有严重胸痛伴大汗",
        )
        res_restricted = self.service.get_training_plan(date="2026-09-04")
        self.assertEqual(res_restricted["plan"]["rule_code"], "SAFETY_RESTRICTED")
        self.assertEqual(res_restricted["plan"]["intensity_baseline_pct"], 0)
        self.assertEqual(res_restricted["plan"]["prescribed_exercises"], [])

        # 4. Deload period: clear red flag with medical clearance
        self.service.update_profile(
            clear_safety_flags=True,
            clearance_reason="心内科急诊就诊排除ACS，医师出具复训许可证明",
            idempotency_key="clear_redflag_01",
        )
        res_deload = self.service.get_training_plan(date="2026-09-05")
        self.assertEqual(res_deload["plan"]["rule_code"], "RECOVERY_FLAG_CLEAR_01")
        self.assertEqual(res_deload["plan"]["intensity_baseline_pct"], 50)
        self.assertEqual(res_deload["plan"]["min_rir"], 3)


if __name__ == "__main__":
    unittest.main()
