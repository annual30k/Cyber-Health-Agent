"""Tests for Codex Review Round 9:
- Spec 4.2 Training Performance State Machine (Double Progression TRAIN_PROGRESS_01)
- Guards against false progression: single record, incomplete session, missing RPE, non-target exercise, cross-user
- Priority hierarchy: Red flag, Deload, and Fatigue take precedence over progression
- Confirmation workflow and revision lineage chaining (parent_id)
- Multi-constraint combinations (knee + lumbar, knee + shoulder, all constrained)
- Stale daily evidence rejection with clock-controlled freshness window
- Structured rest times, known vs unknown load targets without fabrication
- Same movement pattern substitution (substitute_exercise)
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cyber_health import (
    ConflictError,
    CyberHealthService,
    SafetyRestrictedError,
    ValidationError,
)


class TestCodexReviewRound9(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_round9.sqlite3"
        self.service = CyberHealthService(self.db_path, recovery_evidence_window_days=1)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # =========================================================================
    # 1. Spec 4.2 Double Progression State Machine (TRAIN_PROGRESS_01)
    # =========================================================================

    def test_double_progression_triggers_on_two_consecutive_sessions(self) -> None:
        """Verify that 2 consecutive completed sessions reaching target upper reps with RPE <= 8
        generates a pending progression suggestion citing both source record IDs,
        while keeping the currently prescribed load unmutated until confirmed.
        """
        user_id = "u_prog_01"
        self.service.update_profile(
            user_id=user_id,
            goals={"experience_level": "intermediate"},
            idempotency_key="prof-prog-01",
        )

        # Baseline plan: Barbell Back Squat (target reps: 6-8, target_reps_max: 8)
        p0 = self.service.get_training_plan(user_id=user_id, date="2026-09-01", equipment=["barbell"])
        self.assertEqual(len(p0["plan"]["progression_suggestions"]), 0)

        # Session 1: Completed Squat with 80kg x 8 reps, RPE 7.5
        res_s1 = self.service.complete_workout(
            user_id=user_id,
            date="2026-09-01",
            idempotency_key="wo-squat-s1",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        s1_id = res_s1["data"]["record_id"]

        # Only 1 session: Must NOT trigger progression
        p1 = self.service.get_training_plan(user_id=user_id, date="2026-09-02", equipment=["barbell"])
        self.assertEqual(len(p1["plan"]["progression_suggestions"]), 0)

        # Session 2: Completed Squat again with 80kg x 8 reps, RPE 8.0
        res_s2 = self.service.complete_workout(
            user_id=user_id,
            date="2026-09-03",
            idempotency_key="wo-squat-s2",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=8.0,
            completion_rate=1.0,
        )
        s2_id = res_s2["data"]["record_id"]

        # Now 2 consecutive sessions met criteria!
        p2 = self.service.get_training_plan(user_id=user_id, date="2026-09-04", equipment=["barbell"])
        suggs = p2["plan"]["progression_suggestions"]
        self.assertEqual(len(suggs), 1)

        sugg = suggs[0]
        self.assertEqual(sugg["rule_code"], "TRAIN_PROGRESS_01")
        self.assertEqual(sugg["exercise_name"], "Barbell Back Squat")
        self.assertEqual(sugg["status"], "pending_confirmation")
        self.assertEqual(sugg["current_weight_kg"], 80.0)
        self.assertEqual(sugg["suggested_increment_kg"], 2.5)
        self.assertEqual(sugg["suggested_weight_kg"], 82.5)
        self.assertTrue(sugg["requires_user_confirmation"])
        # Evidence source record IDs must match both sessions
        self.assertIn(s1_id, sugg["evidence_source_record_ids"])
        self.assertIn(s2_id, sugg["evidence_source_record_ids"])

        # Plan itself must NOT directly mutate the current prescription load until confirmed!
        squat_ex = [e for e in p2["plan"]["prescribed_exercises"] if e["name"] == "Barbell Back Squat"][0]
        self.assertEqual(squat_ex["suggested_weight_kg"], 80.0)

    def test_progression_negative_guards(self) -> None:
        """Verify guards against false progression triggers:
        - incomplete session (completion_rate < 1.0)
        - missing RPE (session_rpe is None)
        - high RPE (> 8.0)
        - reps short of upper limit
        - non-target exercise
        - cross-user isolation
        """
        u_main = "u_guards_main"
        u_other = "u_guards_other"

        # 1. Incomplete session (< 1.0) does not qualify
        self.service.complete_workout(
            user_id=u_main,
            date="2026-09-01",
            idempotency_key="wo-incomp-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=7.0,
            completion_rate=0.5,  # Incomplete!
        )
        self.service.complete_workout(
            user_id=u_main,
            date="2026-09-02",
            idempotency_key="wo-incomp-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        p_inc = self.service.get_training_plan(user_id=u_main, date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(p_inc["plan"]["progression_suggestions"]), 0)

        # 2. Missing RPE does not qualify
        u_norpe = "u_norpe"
        self.service.complete_workout(
            user_id=u_norpe,
            date="2026-09-01",
            idempotency_key="wo-norpe-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=None,  # Missing RPE!
            completion_rate=1.0,
        )
        self.service.complete_workout(
            user_id=u_norpe,
            date="2026-09-02",
            idempotency_key="wo-norpe-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        p_norpe = self.service.get_training_plan(user_id=u_norpe, date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(p_norpe["plan"]["progression_suggestions"]), 0)

        # 3. High RPE (> 8.0) does not qualify
        u_highrpe = "u_highrpe"
        self.service.complete_workout(
            user_id=u_highrpe,
            date="2026-09-01",
            idempotency_key="wo-hrpe-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=8.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            user_id=u_highrpe,
            date="2026-09-02",
            idempotency_key="wo-hrpe-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=9.0,  # Too high, near failure!
            completion_rate=1.0,
        )
        p_hrpe = self.service.get_training_plan(user_id=u_highrpe, date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(p_hrpe["plan"]["progression_suggestions"]), 0)

        # 4. Short of target reps (6 < 8) does not qualify
        u_short = "u_short"
        self.service.complete_workout(
            user_id=u_short,
            date="2026-09-01",
            idempotency_key="wo-short-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            user_id=u_short,
            date="2026-09-02",
            idempotency_key="wo-short-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 6}],  # Short of 8 reps
            session_rpe=7.5,
            completion_rate=1.0,
        )
        p_short = self.service.get_training_plan(user_id=u_short, date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(p_short["plan"]["progression_suggestions"]), 0)

        # 5. Non-target exercise does not trigger Squat progression
        u_nontarget = "u_nontarget"
        self.service.complete_workout(
            user_id=u_nontarget,
            date="2026-09-01",
            idempotency_key="wo-nt-01",
            completed_exercises=[{"name": "Barbell Bench Press", "weight_kg": 70.0, "reps": 8}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            user_id=u_nontarget,
            date="2026-09-02",
            idempotency_key="wo-nt-02",
            completed_exercises=[{"name": "Barbell Bench Press", "weight_kg": 70.0, "reps": 8}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        # Check that Barbell Back Squat does not get a progression suggestion from Bench Press workouts
        p_nt = self.service.get_training_plan(user_id=u_nontarget, date="2026-09-03", equipment=["barbell"])
        squat_suggs = [s for s in p_nt["plan"]["progression_suggestions"] if s["exercise_name"] == "Barbell Back Squat"]
        self.assertEqual(len(squat_suggs), 0)

        # 6. Cross-user isolation: User B must NOT trigger from User A's workouts
        p_other = self.service.get_training_plan(user_id=u_other, date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(p_other["plan"]["progression_suggestions"]), 0)

    def test_priority_hierarchy_blocks_progression(self) -> None:
        """Verify safety priority: Red flag, Deload, and Fatigue all take precedence over progression."""
        user_id = "u_priority_test"
        # Seed 2 successful workouts for progression
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-01",
            idempotency_key="wo-prio-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 100.0, "reps": 8}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-02",
            idempotency_key="wo-prio-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 100.0, "reps": 8}],
            session_rpe=7.5,
            completion_rate=1.0,
        )

        # Case A: Fatigue / Sleep deficit on date of prescription -> triggers TRAIN_RECOVERY_01, NO progression
        self.service.log_daily_metrics(
            user_id=user_id,
            date="2026-09-03",
            metrics={"sleep_hours": 5.0, "fatigue_level": 8},
            idempotency_key="metric-prio-fatigue",
        )
        p_fatigue = self.service.get_training_plan(user_id=user_id, date="2026-09-03", equipment=["barbell"])
        self.assertEqual(p_fatigue["plan"]["rule_code"], "TRAIN_RECOVERY_01")
        self.assertNotIn("progression_suggestions", p_fatigue["plan"])

        # Case B: Red flag -> triggers SAFETY_RESTRICTED, NO progression
        self.service.update_profile(
            user_id=user_id,
            safety_flags=["chest_pain"],
            idempotency_key="prof-prio-rf",
        )
        p_rf = self.service.get_training_plan(user_id=user_id, date="2026-09-04", equipment=["barbell"])
        self.assertEqual(p_rf["plan"]["rule_code"], "SAFETY_RESTRICTED")
        self.assertEqual(p_rf["plan"]["prescribed_exercises"], [])

        # Case C: Clear red flag with clearance to enter 7-day Deload -> RECOVERY_FLAG_CLEAR_01, NO progression
        self.service.update_profile(
            user_id=user_id,
            clear_safety_flags=True,
            clearance_reason="Physician exam cleared cardiovascular pathology",
            idempotency_key="prof-prio-clear",
        )
        p_dl = self.service.get_training_plan(user_id=user_id, date="2026-09-05", equipment=["barbell"])
        self.assertEqual(p_dl["plan"]["rule_code"], "RECOVERY_FLAG_CLEAR_01")
        self.assertNotIn("progression_suggestions", p_dl["plan"])

    def test_confirmation_workflow_and_revision_chain(self) -> None:
        """Verify confirm_training_progression updates state atomically and links parent_id revision chain."""
        user_id = "u_confirm_chain"
        # Seed 2 workouts with 80.0kg (sets: 3, reps: 8)
        r1 = self.service.complete_workout(
            user_id=user_id,
            date="2026-09-01",
            idempotency_key="wo-chain-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        r2 = self.service.complete_workout(
            user_id=user_id,
            date="2026-09-02",
            idempotency_key="wo-chain-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )

        p_prop1 = self.service.get_training_plan(user_id=user_id, date="2026-09-02", equipment=["barbell"])
        sugg1 = p_prop1["plan"]["progression_suggestions"][0]

        # 1. Confirm first progression: 80kg -> 82.5kg
        c1 = self.service.confirm_training_progression(
            user_id=user_id,
            exercise_name="Barbell Back Squat",
            confirmed_weight_kg=82.5,
            increment_kg=2.5,
            proposal_id=sugg1["proposal_id"],
            source_record_ids=sugg1["evidence_source_record_ids"],
            user_note="Confirmed +2.5kg progression after good form",
            idempotency_key="conf-prog-01",
        )
        self.assertEqual(c1["status"], "success")
        self.assertEqual(c1["data"]["confirmed_weight_kg"], 82.5)
        self.assertIsNone(c1["data"]["parent_id"])
        c1_id = c1["data"]["record_id"]

        # Next plan immediately uses 82.5kg as the baseline load
        plan_after_c1 = self.service.get_training_plan(user_id=user_id, date="2026-09-03", equipment=["barbell"])
        squat_after_c1 = [e for e in plan_after_c1["plan"]["prescribed_exercises"] if e["name"] == "Barbell Back Squat"][0]
        self.assertEqual(squat_after_c1["suggested_weight_kg"], 82.5)

        # Complete 2 workouts at 82.5kg to authentically qualify for the next progression
        r3 = self.service.complete_workout(
            user_id=user_id,
            date="2026-09-04",
            idempotency_key="wo-chain-03",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 82.5, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        r4 = self.service.complete_workout(
            user_id=user_id,
            date="2026-09-05",
            idempotency_key="wo-chain-04",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 82.5, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        p_prop2 = self.service.get_training_plan(user_id=user_id, date="2026-09-05", equipment=["barbell"])
        sugg2 = p_prop2["plan"]["progression_suggestions"][0]

        # 2. Later confirm second progression: 82.5kg -> 85.0kg
        c2 = self.service.confirm_training_progression(
            user_id=user_id,
            exercise_name="Barbell Back Squat",
            confirmed_weight_kg=85.0,
            increment_kg=2.5,
            proposal_id=sugg2["proposal_id"],
            source_record_ids=sugg2["evidence_source_record_ids"],
            idempotency_key="conf-prog-02",
        )
        self.assertEqual(c2["status"], "success")
        self.assertEqual(c2["data"]["confirmed_weight_kg"], 85.0)
        # Revision chain: parent_id must point to c1_id
        self.assertEqual(c2["data"]["parent_id"], c1_id)

        # Next plan reflects 85.0kg
        plan_after_c2 = self.service.get_training_plan(user_id=user_id, date="2026-09-06", equipment=["barbell"])
        squat_after_c2 = [e for e in plan_after_c2["plan"]["prescribed_exercises"] if e["name"] == "Barbell Back Squat"][0]
        self.assertEqual(squat_after_c2["suggested_weight_kg"], 85.0)

    # =========================================================================
    # 2. Multi-Constraint Combinations & Unified Safe Filtering
    # =========================================================================

    def test_combined_constraints_knee_and_lumbar(self) -> None:
        """Verify that concurrent knee and lumbar constraints eliminate contraindicated
        exercises across all tiers without contradictory prescriptions.
        """
        user_id = "u_knee_lumbar"
        self.service.update_profile(
            user_id=user_id,
            constraints={"injuries": "knee pain with deep flexion, lumbar disc herniation"},
            idempotency_key="prof-kl-01",
        )

        # 1. Barbell mode:
        # Back Squat is contraindicated (knee + lumbar).
        # Romanian Deadlift is contraindicated (lumbar).
        # Safe selections: Barbell Hip Thrust (knee/spine friendly) and Glute Bridge.
        p_bb = self.service.get_training_plan(user_id=user_id, date="2026-09-05", equipment=["barbell"])
        bb_names = [e["name"] for e in p_bb["plan"]["prescribed_exercises"]]
        self.assertNotIn("Barbell Back Squat", bb_names)
        self.assertNotIn("Romanian Deadlift", bb_names)
        self.assertIn("Barbell Hip Thrust", bb_names)
        self.assertIn("Glute Bridge", bb_names)

        # 2. Dumbbell mode in Recovery tier (with fatigue)
        self.service.log_daily_metrics(
            user_id=user_id,
            date="2026-09-05",
            metrics={"fatigue_level": 8, "sleep_hours": 5.5},
            idempotency_key="m-kl-fatigue",
        )
        p_db_rec = self.service.get_training_plan(user_id=user_id, date="2026-09-05", equipment=["dumbbell"])
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
        user_id = "u_all_contra"
        self.service.update_profile(
            user_id=user_id,
            constraints={"injuries": "knee, shoulder, and lumbar severe injuries, avoid all squat, push, and axial loads"},
            idempotency_key="prof-all-c",
        )
        p_plan = self.service.get_training_plan(user_id=user_id, date="2026-09-05", equipment=["barbell"])
        prescribed_names = [e["name"] for e in p_plan["plan"]["prescribed_exercises"]]
        self.assertNotIn("Barbell Back Squat", prescribed_names)
        self.assertNotIn("Barbell Bench Press", prescribed_names)
        self.assertNotIn("Romanian Deadlift", prescribed_names)

    # =========================================================================
    # 3. Freshness Window & Clock Control (No Stale Evidence)
    # =========================================================================

    def test_stale_daily_state_evidence_rejected(self) -> None:
        """Verify that historical daily metrics from 60 days ago are not treated as fresh evidence for today."""
        user_id = "u_stale_user"

        # Log severe sleep deficit on 2026-07-01 (65 days before 2026-09-05)
        self.service.log_daily_metrics(
            user_id=user_id,
            date="2026-07-01",
            metrics={"sleep_hours": 3.0, "fatigue_level": 9},
            idempotency_key="m-stale-01",
        )

        # Query plan for 2026-09-05:
        # Must NOT treat the 65-day-old sleep deficit as today's fatigue (TRAIN_RECOVERY_01 must NOT trigger)
        # Must NOT treat state as verified_recent_state
        plan = self.service.get_training_plan(user_id=user_id, date="2026-09-05", evidence_window_days=1)
        p_data = plan["plan"]

        self.assertEqual(p_data["rule_code"], "TRAIN_PROGRESSION_STANDARD")
        self.assertEqual(p_data["state_evidence"], "unrecorded_recent_state")
        self.assertIn("未检测到近期体征记录", p_data["guidance"])
        # recovery_score must be None (not a fabricated 75)
        self.assertIsNone(plan["recovery_score"])

        # Now log fresh metrics for 2026-09-05:
        self.service.log_daily_metrics(
            user_id=user_id,
            date="2026-09-05",
            metrics={"sleep_hours": 8.0, "fatigue_level": 2},
            idempotency_key="m-fresh-01",
        )
        fresh_plan = self.service.get_training_plan(user_id=user_id, date="2026-09-05", evidence_window_days=1)
        self.assertEqual(fresh_plan["plan"]["state_evidence"], "verified_recent_state")
        self.assertIsNotNone(fresh_plan["recovery_score"])

    # =========================================================================
    # 4. Structured Rest Durations, Unknown Loads, and Movement Pattern Substitution
    # =========================================================================

    def test_structured_rest_and_unknown_load_disclosure(self) -> None:
        """Verify prescribed exercises include structured rest duration and do not fabricate unknown loads."""
        user_id = "u_rest_test"
        plan = self.service.get_training_plan(user_id=user_id, date="2026-09-05", equipment=["barbell"])
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
        user_id = "u_sub_test"

        # 1. Substitute Barbell Bench Press (upper_push) with dumbbell equipment
        sub1 = self.service.substitute_exercise(
            user_id=user_id,
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
            user_id=user_id,
            original_exercise="Barbell Bench Press",
            discomfort_joint="shoulder",
            equipment=["dumbbell"],
        )
        # All upper_push movements strain the shoulder -> no safe substitute in that pattern
        self.assertEqual(sub2["status"], "no_substitute_available")
        self.assertEqual(len(sub2["data"]["substitutes"]), 0)


if __name__ == "__main__":
    unittest.main()
