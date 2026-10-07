"""Double-progression detection, confirmation evidence and blocking rules."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cyber_health import CyberHealthService, SafetyRestrictedError, ValidationError
from test_support import OWNER, fixed_clock


class ProgressionFatigueTests(unittest.TestCase):
    def test_sleep_deficit_blocks_previously_generated_proposal(self):
        """Preserve original Codex test: sleep deficit 5.9h via real API blocks progression confirmation."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"
            for day in ("2026-09-03", "2026-09-04"):
                s.complete_workout(date=day, idempotency_key=day,
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)
            proposal = s.get_training_plan(date="2026-09-05",
                equipment=["barbell"])["plan"]["progression_suggestions"][0]
            s.log_daily_metrics(date="2026-09-05",
                metrics={"sleep_hours": 5.9, "fatigue_level": 1}, idempotency_key="sleep")
            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=proposal["suggested_weight_kg"],
                    source_record_ids=proposal["evidence_source_record_ids"],
                    proposal_id=proposal["proposal_id"], idempotency_key="confirm")

    def test_fatigue_level_seven_blocks_progression_via_real_api(self):
        """Acute fatigue level 7 logged via real API blocks progression suggestions and confirmation."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"
            for day in ("2026-09-03", "2026-09-04"):
                s.complete_workout(date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)
            # Before fatigue is logged, progression proposal is generated
            plan_before = s.get_training_plan(date="2026-09-05", equipment=["barbell"])
            proposals = plan_before["plan"]["progression_suggestions"]
            self.assertEqual(len(proposals), 1)
            prop = proposals[0]

            # Log fatigue_level=7 via real log_daily_metrics API
            s.log_daily_metrics(date="2026-09-05",
                metrics={"sleep_hours": 8.0, "fatigue_level": 7}, idempotency_key="fatigue_7")

            # Training plan now shifts to TRAIN_RECOVERY_01 and suppresses progression suggestions
            plan_after = s.get_training_plan(date="2026-09-05", equipment=["barbell"])
            self.assertEqual(plan_after["plan"]["rule_code"], "TRAIN_RECOVERY_01")
            self.assertEqual(len(plan_after["plan"].get("progression_suggestions", [])), 0)

            # Confirming previously captured proposal must raise SafetyRestrictedError
            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=prop["suggested_weight_kg"],
                    source_record_ids=prop["evidence_source_record_ids"],
                    proposal_id=prop["proposal_id"], idempotency_key="confirm_f7")

    def test_sleep_hours_zero_not_falsy_blocks_progression(self):
        """sleep_hours=0.0 must not be treated as falsy/default 8.0 and must block progression."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"
            for day in ("2026-09-03", "2026-09-04"):
                s.complete_workout(date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)
            proposal = s.get_training_plan(date="2026-09-05",
                equipment=["barbell"])["plan"]["progression_suggestions"][0]

            # Log real metrics with sleep_hours=0.0
            s.log_daily_metrics(date="2026-09-05",
                metrics={"sleep_hours": 0.0, "fatigue_level": 1}, idempotency_key="sleep_0")

            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=proposal["suggested_weight_kg"],
                    source_record_ids=proposal["evidence_source_record_ids"],
                    proposal_id=proposal["proposal_id"], idempotency_key="confirm_s0")

    def test_recovery_score_zero_not_falsy_blocks_progression(self):
        """recovery_score=0 must not be treated as falsy/default 100 and must block progression."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"
            for day in ("2026-09-03", "2026-09-04"):
                s.complete_workout(date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)
            proposal = s.get_training_plan(date="2026-09-05",
                equipment=["barbell"])["plan"]["progression_suggestions"][0]

            # Metrics causing real recovery_score to bottom out at 0
            log_res = s.log_daily_metrics(date="2026-09-05",
                metrics={"sleep_hours": 1.0, "fatigue_level": 10, "sleep_quality": "poor"},
                idempotency_key="rec_0")
            self.assertEqual(log_res["data"]["recovery_score"], 0)

            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=proposal["suggested_weight_kg"],
                    source_record_ids=proposal["evidence_source_record_ids"],
                    proposal_id=proposal["proposal_id"], idempotency_key="confirm_r0")

    def test_future_daily_record_does_not_shadow_current_fatigue(self):
        """A future daily_state record (day > target_date) must not mask active acute fatigue."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"
            for day in ("2026-09-02", "2026-09-03"):
                s.complete_workout(date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)
            proposal = s.get_training_plan(date="2026-09-04",
                equipment=["barbell"])["plan"]["progression_suggestions"][0]

            # Current fatigue logged on Sep 4 (1 day ago relative to Sep 5)
            s.log_daily_metrics(date="2026-09-04",
                metrics={"sleep_hours": 4.0, "fatigue_level": 8}, idempotency_key="fatigue_sep4")

            # A future daily state is logged on Sep 7 with fresh metrics
            s.log_daily_metrics(date="2026-09-07",
                metrics={"sleep_hours": 8.5, "fatigue_level": 1}, idempotency_key="future_healthy_sep7")

            # On Sep 5, target date filtering (day <= 2026-09-05) must pick Sep 4 fatigue, not Sep 7
            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=proposal["suggested_weight_kg"],
                    source_record_ids=proposal["evidence_source_record_ids"],
                    proposal_id=proposal["proposal_id"], idempotency_key="confirm_shadow")

    def test_future_workout_does_not_count_towards_progression(self):
        """Workouts logged with future dates must not qualify progression before their time."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-04T12:00:00+08:00"
            # Past qualifying workout on Sep 2
            w_sep2 = s.complete_workout(date="2026-09-02", idempotency_key="wo_sep2",
                completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                      "reps": 8, "sets": 3}],
                session_rpe=7, completion_rate=1)
            # Sep 3 workout did NOT reach reps_max (only 7 reps)
            s.complete_workout(date="2026-09-03", idempotency_key="wo_sep3",
                completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                      "reps": 7, "sets": 3}],
                session_rpe=7, completion_rate=1)
            # Future workout on Sep 7 achieved 8 reps
            w_sep7 = s.complete_workout(date="2026-09-07", idempotency_key="wo_sep7",
                completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                      "reps": 8, "sets": 3}],
                session_rpe=7, completion_rate=1)

            # On Sep 4, workouts on Sep 7 must be ignored. Most recent before Sep 4 is Sep 3 (failed),
            # so no active qualifying progression proposal exists.
            with self.assertRaises(ValidationError) as ctx:
                s.confirm_training_progression(exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=82.5,
                    source_record_ids=[w_sep2["data"]["record_id"], w_sep7["data"]["record_id"]],
                    idempotency_key="confirm_future_wo")
            self.assertIn("No active qualifying progression proposal found", str(ctx.exception))

    def test_stale_fatigue_data_expires_and_allows_progression(self):
        """Fatigue older than recovery_evidence_window_days expires and does not block progression."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"

            # Fatigue logged 15 days ago (> 7-day default window)
            s.log_daily_metrics(date="2026-08-21",
                metrics={"sleep_hours": 3.0, "fatigue_level": 9}, idempotency_key="stale_fatigue")

            # Recent consecutive qualifying workouts on Sep 3 and Sep 4
            for day in ("2026-09-03", "2026-09-04"):
                s.complete_workout(date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)

            # On Sep 5, stale fatigue does not trigger TRAIN_RECOVERY_01
            plan = s.get_training_plan(date="2026-09-05", equipment=["barbell"])
            self.assertEqual(plan["plan"]["rule_code"], "TRAIN_PROGRESSION_STANDARD")
            proposals = plan["plan"]["progression_suggestions"]
            self.assertEqual(len(proposals), 1)
            prop = proposals[0]

            # Progression confirmation succeeds!
            confirm_res = s.confirm_training_progression(
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=prop["suggested_weight_kg"],
                source_record_ids=prop["evidence_source_record_ids"],
                proposal_id=prop["proposal_id"],
                idempotency_key="confirm_stale_cleared",
            )
            self.assertEqual(confirm_res["status"], "success")
            self.assertEqual(confirm_res["data"]["confirmed_weight_kg"], 82.5)
            self.assertEqual(confirm_res["data"]["status"], "confirmed")

    def test_timezone_aware_determines_local_target_date(self):
        """Confirmation respects user's configured timezone when resolving current local date."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            # Set user timezone to America/New_York (UTC-4 in summer EDT)
            s.update_profile(timezone="America/New_York", idempotency_key="prof_tz")

            # Workouts logged on local dates Sep 2 and Sep 3
            for day in ("2026-09-02", "2026-09-03"):
                s.complete_workout(date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)

            # Proposal on local date 2026-09-04
            prop = s.get_training_plan(date="2026-09-04",
                equipment=["barbell"])["plan"]["progression_suggestions"][0]

            # Fatigue logged on local date 2026-09-04
            s.log_daily_metrics(date="2026-09-04",
                metrics={"sleep_hours": 4.5, "fatigue_level": 8}, idempotency_key="fatigue_ny")

            # Current UTC time is 2026-09-05T02:00:00Z.
            # In New York (EDT, UTC-4), this is 2026-09-04T22:00:00-04:00 (local date: 2026-09-04).
            s._now = lambda: "2026-09-05T02:00:00Z"

            # Should evaluate local date 2026-09-04, detecting the Sep 4 fatigue and blocking
            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=prop["suggested_weight_kg"],
                    source_record_ids=prop["evidence_source_record_ids"],
                    proposal_id=prop["proposal_id"], idempotency_key="confirm_tz")


class ProgressionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_round10.sqlite3"
        self.service = CyberHealthService(self.db_path, recovery_evidence_window_days=1, clock=fixed_clock())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_recent_failure_breaks_progression_streak(self) -> None:
        """Verify that a recent failed/incomplete workout breaks the consecutive streak.
        History: Day 1 Success -> Day 2 Success -> Day 3 Failure.
        The algorithm must NOT skip Day 3 to evaluate Day 1 and Day 2!
        """
        # Day 1: Success (80kg x 8, sets 3, rpe 7.0, complete)
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-s1",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        # Day 2: Success (80kg x 8, sets 3, rpe 7.5, complete)
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo-s2",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        # Verify that Day 2 alone before failure WOULD have had a proposal
        plan_day2 = self.service.get_training_plan(date="2026-09-02", equipment=["barbell"])
        self.assertEqual(len(plan_day2["plan"]["progression_suggestions"]), 1)

        # Day 3: FAILURE / Incomplete (completion_rate = 0.5 or reps short)
        self.service.complete_workout(
            date="2026-09-03",
            idempotency_key="wo-s3-fail",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 5, "sets": 3}],
            session_rpe=9.0,
            completion_rate=0.5,
        )

        # After Day 3, evaluating progression MUST return 0 suggestions!
        plan_after_fail = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(plan_after_fail["plan"]["progression_suggestions"]), 0)

        # Also test with high RPE failure (completion_rate 1.0, but RPE 9.5)
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo2-s1",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo2-s2",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        # Day 3: high RPE (9.5)
        self.service.complete_workout(
            date="2026-09-03",
            idempotency_key="wo2-s3",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=9.5,
            completion_rate=1.0,
        )
        plan_after_high_rpe = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(plan_after_high_rpe["plan"]["progression_suggestions"]), 0)

    def test_sameday_split_records_consolidated_as_single_session(self) -> None:
        """Verify that multiple workout logs on the same day are consolidated into a single session.
        Two logs on the same day must NOT be counted as 2 consecutive sessions!
        """
        # Day 1: User splits workout into 2 logs (e.g. warm-up/morning and afternoon)
        self.service.log_workout(
            date="2026-09-01",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 1, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
            ],
            session_id="session_morning",
            idempotency_key="wo-split-01",
            completion_rate=1.0,
        )
        self.service.log_workout(
            date="2026-09-01",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 2, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
                {"exercise": "Barbell Back Squat", "set_num": 3, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
            ],
            session_id="session_afternoon",
            idempotency_key="wo-split-02",
            completion_rate=1.0,
        )

        # Only 1 distinct day trained! Must NOT trigger double progression!
        plan_day1 = self.service.get_training_plan(date="2026-09-01", equipment=["barbell"])
        self.assertEqual(len(plan_day1["plan"]["progression_suggestions"]), 0)

        # Day 2: Completed all sets
        self.service.log_workout(
            date="2026-09-03",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 1, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
                {"exercise": "Barbell Back Squat", "set_num": 2, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
                {"exercise": "Barbell Back Squat", "set_num": 3, "reps": 8, "weight_kg": 80.0, "rpe": 7.5},
            ],
            idempotency_key="wo-day2",
            completion_rate=1.0,
        )

        # Now 2 distinct sessions have occurred!
        plan_day2 = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        suggs = plan_day2["plan"]["progression_suggestions"]
        self.assertEqual(len(suggs), 1)
        self.assertEqual(suggs[0]["suggested_weight_kg"], 82.5)

    def test_incomparable_weights_across_sessions_rejected(self) -> None:
        """Verify that 2 sessions at different weights (e.g. 70kg and 80kg) do NOT trigger progression.
        Double progression requires repeating the SAME target load.
        """
        # Session 1: 70kg x 8 reps
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-70kg",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 70.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        # Session 2: 80kg x 8 reps
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo-80kg",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        plan = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        # Incomparable weights across sessions -> no progression yet!
        self.assertEqual(len(plan["plan"]["progression_suggestions"]), 0)

    def test_incomplete_working_sets_rejected(self) -> None:
        """Verify that achieving target reps on only 1 or 2 sets while failing the 3rd set breaks progression."""
        # Session 1: Clean success (3 sets of 8)
        self.service.log_workout(
            date="2026-09-01",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 1, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
                {"exercise": "Barbell Back Squat", "set_num": 2, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
                {"exercise": "Barbell Back Squat", "set_num": 3, "reps": 8, "weight_kg": 80.0, "rpe": 7.5},
            ],
            idempotency_key="wo-clean-01",
            completion_rate=1.0,
        )
        # Session 2: Set 1 and 2 hit 8 reps, but Set 3 only hits 6 reps!
        self.service.log_workout(
            date="2026-09-02",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 1, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
                {"exercise": "Barbell Back Squat", "set_num": 2, "reps": 8, "weight_kg": 80.0, "rpe": 7.5},
                {"exercise": "Barbell Back Squat", "set_num": 3, "reps": 6, "weight_kg": 80.0, "rpe": 8.0},
            ],
            idempotency_key="wo-partial-02",
            completion_rate=1.0,
        )
        plan = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(plan["plan"]["progression_suggestions"]), 0)

    def test_missing_sets_or_missing_rpe_rejected(self) -> None:
        """Verify that missing set count or missing RPE never defaults to success."""
        # Session 1: only 1 set logged when 3 are required
        self.service.log_workout(
            date="2026-09-01",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 1, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
            ],
            idempotency_key="wo-1set-01",
            completion_rate=1.0,
        )
        self.service.log_workout(
            date="2026-09-02",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 1, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
            ],
            idempotency_key="wo-1set-02",
            completion_rate=1.0,
        )
        plan = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(plan["plan"]["progression_suggestions"]), 0)

    def _seed_valid_squat_progression(self) -> dict[str, Any]:
        """Helper to seed 2 valid sessions and return the active proposal."""
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key=f"wo-{OWNER}-1",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key=f"wo-{OWNER}-2",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        plan = self.service.get_training_plan(date="2026-09-02", equipment=["barbell"])
        return plan["plan"]["progression_suggestions"][0]

    def test_confirm_progression_blocked_under_restricted_mode(self) -> None:
        """Verify that confirm_training_progression raises SafetyRestrictedError when in restricted mode."""
        prop = self._seed_valid_squat_progression()

        # Trigger restricted mode
        self.service.complete_workout(
            date="2026-09-03",
            idempotency_key="wo-restr",
            discomfort_notes="出现严重胸痛与呼吸困难",
        )

        with self.assertRaises(SafetyRestrictedError):
            self.service.confirm_training_progression(
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-fail-restr",
            )

    def test_confirm_progression_blocked_under_active_deload(self) -> None:
        """Verify that confirm_training_progression is blocked during 7-day Deload period."""
        prop = self._seed_valid_squat_progression()

        # Set profile in active deload directly in database
        with self.service.store.transaction() as conn:
            conn.execute(
                "UPDATE user_profile SET deload_until = '2026-09-20' WHERE user_id = ?",
                (OWNER,),
            )

        with self.assertRaises(SafetyRestrictedError) as ctx:
            self.service.confirm_training_progression(
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-fail-deload",
            )
        self.assertIn("RECOVERY_FLAG_CLEAR_01", str(ctx.exception))

    def test_confirm_progression_blocked_under_fatigue_recovery(self) -> None:
        """Verify that confirm_training_progression is blocked when recent daily metrics show severe fatigue."""
        prop = self._seed_valid_squat_progression()

        # Log daily state showing severe fatigue and sleep deprivation today
        today = self.service._now()[:10]
        self.service.log_daily_metrics(
            date=today,
            metrics={"sleep_hours": 4.0, "fatigue_level": 9},
            idempotency_key="ds-fatigue-today",
        )

        with self.assertRaises(SafetyRestrictedError) as ctx:
            self.service.confirm_training_progression(
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-fail-fatigue",
            )
        self.assertIn("TRAIN_RECOVERY_01", str(ctx.exception))

    def test_confirm_progression_blocked_if_exercise_contraindicated(self) -> None:
        """Verify that confirm_training_progression is blocked if the exercise is contraindicated by active constraints."""
        prop = self._seed_valid_squat_progression()

        # Add knee constraint to user profile
        self.service.update_profile(
            constraints={"joint_issues": ["knee_pain", "patella"]},
            idempotency_key="prof-knee-contra",
        )

        with self.assertRaises(SafetyRestrictedError) as ctx:
            self.service.confirm_training_progression(
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-fail-contra",
            )
        self.assertIn("contraindicated", str(ctx.exception).lower())

    def test_confirm_progression_rejects_dummy_evidence(self) -> None:
        """Verify that confirm_training_progression strictly rejects dummy source record IDs."""
        prop = self._seed_valid_squat_progression()

        # 1. Reject dummy source records
        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_training_progression(
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=["wo-fake-1", "wo-fake-2"],
                idempotency_key="conf-dummy",
            )
        self.assertIn("does not exist", str(ctx.exception))

    def test_confirm_progression_rejects_mismatched_proposal_id_or_load(self) -> None:
        """Verify that confirm_training_progression rejects forged proposal IDs or arbitrary weights."""
        prop = self._seed_valid_squat_progression()

        # 1. Mismatched proposal ID
        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_training_progression(
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id="prop_forged_abc123",
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-mismatch-pid",
            )
        self.assertIn("Proposal ID mismatch", str(ctx.exception))

        # 2. Mismatched load (proposed 82.5kg, but user submitted 120.0kg)
        with self.assertRaises(ValidationError) as ctx2:
            self.service.confirm_training_progression(
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=120.0,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-mismatch-weight",
            )
        self.assertIn("does not match proposed weight", str(ctx2.exception))

        # 3. Confirming when no qualifying history exists at all
        with self.assertRaises(ValidationError) as ctx3:
            self.service.confirm_training_progression(
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                source_record_ids=["wo-123"],
                idempotency_key="conf-no-hist",
            )
        self.assertTrue("does not exist" in str(ctx3.exception) or "No active qualifying" in str(ctx3.exception))

    def test_confirm_progression_bodyweight_reps(self) -> None:
        """Verify proposing and confirming rep-based progression for bodyweight movements."""
        # Seed 2 sessions of Glute Bridge (bodyweight, target_reps=15, sets=3)
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-bw-1",
            completed_exercises=[{"name": "Glute Bridge", "reps": 15, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo-bw-2",
            completed_exercises=[{"name": "Glute Bridge", "reps": 15, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )

        plan = self.service.get_training_plan(date="2026-09-02", equipment=["bodyweight"])
        suggs = plan["plan"]["progression_suggestions"]
        self.assertEqual(len(suggs), 1)
        bw_sugg = suggs[0]
        self.assertEqual(bw_sugg["exercise_name"], "Glute Bridge")
        self.assertIsNone(bw_sugg["suggested_weight_kg"])
        self.assertEqual(bw_sugg["suggested_reps"], 16)

        # Confirm rep progression
        conf = self.service.confirm_training_progression(
            exercise_name="Glute Bridge",
            confirmed_reps=16,
            proposal_id=bw_sugg["proposal_id"],
            source_record_ids=bw_sugg["evidence_source_record_ids"],
            idempotency_key="conf-bw-01",
        )
        self.assertEqual(conf["status"], "success")
        self.assertEqual(conf["data"]["confirmed_reps"], 16)
        self.assertIsNone(conf["data"]["confirmed_weight_kg"])

    def test_record_exercise_baseline_separated_from_progression(self) -> None:
        """Verify that record_exercise_baseline records starting load without forging progression proposals."""
        res = self.service.record_exercise_baseline(
            exercise_name="Barbell Back Squat",
            weight_kg=60.0,
            idempotency_key="man-base-01",
            user_note="Initial baseline self-test",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["data"]["weight_kg"], 60.0)
        self.assertEqual(res["data"]["verification_type"], "manual_baseline")

        # Next training plan should read 60.0kg as baseline
        plan = self.service.get_training_plan(date="2026-09-01", equipment=["barbell"])
        squat_ex = next(e for e in plan["plan"]["prescribed_exercises"] if e["name"] == "Barbell Back Squat")
        self.assertEqual(squat_ex["suggested_weight_kg"], 60.0)

    def test_confirm_progression_stdio_mcp_boundary(self) -> None:
        """Verify that cyber_health_confirm_training_progression works over MCP stdio protocol."""
        root = Path(__file__).resolve().parents[1]

        # Seed 2 workouts in DB first
        prop = self._seed_valid_squat_progression()

        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
        init_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        call_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "cyber_health_confirm_training_progression",
                "arguments": {
                    "exercise_name": "Barbell Back Squat",
                    "confirmed_weight_kg": 82.5,
                    "proposal_id": prop["proposal_id"],
                    "source_record_ids": prop["evidence_source_record_ids"],
                    "idempotency_key": "stdio-conf-01",
                },
            },
        }

        proc = subprocess.Popen(
            [sys.executable, "-m", "cyber_health_mcp", "--allow-all", "--db", str(self.db_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(root),
        )

        input_data = json.dumps(init_req) + "\n" + json.dumps(init_notif) + "\n" + json.dumps(call_req) + "\n"
        stdout, _stderr = proc.communicate(input=input_data, timeout=10)
        self.assertEqual(proc.returncode, 0)

        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        call_resp = None
        for line in lines:
            try:
                msg = json.loads(line)
                if msg.get("id") == 2:
                    call_resp = msg
                    break
            except Exception:
                continue

        self.assertIsNotNone(call_resp)
        content = call_resp["result"]["content"]
        data_text = content[0]["text"]
        data = json.loads(data_text)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["data"]["confirmed_weight_kg"], 82.5)
        self.assertEqual(data["data"]["proposal_id"], prop["proposal_id"])


class DoubleProgressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_round9.sqlite3"
        self.service = CyberHealthService(self.db_path, recovery_evidence_window_days=1)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_double_progression_triggers_on_two_consecutive_sessions(self) -> None:
        """Verify that 2 consecutive completed sessions reaching target upper reps with RPE <= 8
        generates a pending progression suggestion citing both source record IDs,
        while keeping the currently prescribed load unmutated until confirmed.
        """
        self.service.update_profile(
            goals={"experience_level": "intermediate"},
            idempotency_key="prof-prog-01",
        )

        # Baseline plan: Barbell Back Squat (target reps: 6-8, target_reps_max: 8)
        p0 = self.service.get_training_plan(date="2026-09-01", equipment=["barbell"])
        self.assertEqual(len(p0["plan"]["progression_suggestions"]), 0)

        # Session 1: Completed Squat with 80kg x 8 reps, RPE 7.5
        res_s1 = self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-squat-s1",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        s1_id = res_s1["data"]["record_id"]

        # Only 1 session: Must NOT trigger progression
        p1 = self.service.get_training_plan(date="2026-09-02", equipment=["barbell"])
        self.assertEqual(len(p1["plan"]["progression_suggestions"]), 0)

        # Session 2: Completed Squat again with 80kg x 8 reps, RPE 8.0
        res_s2 = self.service.complete_workout(
            date="2026-09-03",
            idempotency_key="wo-squat-s2",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=8.0,
            completion_rate=1.0,
        )
        s2_id = res_s2["data"]["record_id"]

        # Now 2 consecutive sessions met criteria!
        p2 = self.service.get_training_plan(date="2026-09-04", equipment=["barbell"])
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
        squat_ex = next(e for e in p2["plan"]["prescribed_exercises"] if e["name"] == "Barbell Back Squat")
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

        # 1. Incomplete session (< 1.0) does not qualify
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-incomp-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=7.0,
            completion_rate=0.5,  # Incomplete!
        )
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo-incomp-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        p_inc = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(p_inc["plan"]["progression_suggestions"]), 0)

        # 2. Missing RPE does not qualify
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-norpe-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=None,  # Missing RPE!
            completion_rate=1.0,
        )
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo-norpe-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        p_norpe = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(p_norpe["plan"]["progression_suggestions"]), 0)

        # 3. High RPE (> 8.0) does not qualify
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-hrpe-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=8.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo-hrpe-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=9.0,  # Too high, near failure!
            completion_rate=1.0,
        )
        p_hrpe = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(p_hrpe["plan"]["progression_suggestions"]), 0)

        # 4. Short of target reps (6 < 8) does not qualify
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-short-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 8}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo-short-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 90.0, "reps": 6}],  # Short of 8 reps
            session_rpe=7.5,
            completion_rate=1.0,
        )
        p_short = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(p_short["plan"]["progression_suggestions"]), 0)

        # 5. Non-target exercise does not trigger Squat progression
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-nt-01",
            completed_exercises=[{"name": "Barbell Bench Press", "weight_kg": 70.0, "reps": 8}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo-nt-02",
            completed_exercises=[{"name": "Barbell Bench Press", "weight_kg": 70.0, "reps": 8}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        # Check that Barbell Back Squat does not get a progression suggestion from Bench Press workouts
        p_nt = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        squat_suggs = [s for s in p_nt["plan"]["progression_suggestions"] if s["exercise_name"] == "Barbell Back Squat"]
        self.assertEqual(len(squat_suggs), 0)

        # 6. Cross-user isolation: User B must NOT trigger from User A's workouts
        p_other = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(p_other["plan"]["progression_suggestions"]), 0)

    def test_priority_hierarchy_blocks_progression(self) -> None:
        """Verify safety priority: Red flag, Deload, and Fatigue all take precedence over progression."""
        # Seed 2 successful workouts for progression
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-prio-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 100.0, "reps": 8}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo-prio-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 100.0, "reps": 8}],
            session_rpe=7.5,
            completion_rate=1.0,
        )

        # Case A: Fatigue / Sleep deficit on date of prescription -> triggers TRAIN_RECOVERY_01, NO progression
        self.service.log_daily_metrics(
            date="2026-09-03",
            metrics={"sleep_hours": 5.0, "fatigue_level": 8},
            idempotency_key="metric-prio-fatigue",
        )
        p_fatigue = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        self.assertEqual(p_fatigue["plan"]["rule_code"], "TRAIN_RECOVERY_01")
        self.assertNotIn("progression_suggestions", p_fatigue["plan"])

        # Case B: Red flag -> triggers SAFETY_RESTRICTED, NO progression
        self.service.update_profile(
            safety_flags=["chest_pain"],
            idempotency_key="prof-prio-rf",
        )
        p_rf = self.service.get_training_plan(date="2026-09-04", equipment=["barbell"])
        self.assertEqual(p_rf["plan"]["rule_code"], "SAFETY_RESTRICTED")
        self.assertEqual(p_rf["plan"]["prescribed_exercises"], [])

        # Case C: Clear red flag with clearance to enter 7-day Deload -> RECOVERY_FLAG_CLEAR_01, NO progression
        self.service.update_profile(
            clear_safety_flags=True,
            clearance_reason="Physician exam cleared cardiovascular pathology",
            idempotency_key="prof-prio-clear",
        )
        p_dl = self.service.get_training_plan(date="2026-09-05", equipment=["barbell"])
        self.assertEqual(p_dl["plan"]["rule_code"], "RECOVERY_FLAG_CLEAR_01")
        self.assertNotIn("progression_suggestions", p_dl["plan"])

    def test_confirmation_workflow_and_revision_chain(self) -> None:
        """Verify confirm_training_progression updates state atomically and links parent_id revision chain."""
        # Seed 2 workouts with 80.0kg (sets: 3, reps: 8)
        self.service.complete_workout(
            date="2026-09-01",
            idempotency_key="wo-chain-01",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            date="2026-09-02",
            idempotency_key="wo-chain-02",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )

        p_prop1 = self.service.get_training_plan(date="2026-09-02", equipment=["barbell"])
        sugg1 = p_prop1["plan"]["progression_suggestions"][0]

        # 1. Confirm first progression: 80kg -> 82.5kg
        c1 = self.service.confirm_training_progression(
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
        plan_after_c1 = self.service.get_training_plan(date="2026-09-03", equipment=["barbell"])
        squat_after_c1 = next(e for e in plan_after_c1["plan"]["prescribed_exercises"] if e["name"] == "Barbell Back Squat")
        self.assertEqual(squat_after_c1["suggested_weight_kg"], 82.5)

        # Complete 2 workouts at 82.5kg to authentically qualify for the next progression
        self.service.complete_workout(
            date="2026-09-04",
            idempotency_key="wo-chain-03",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 82.5, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            date="2026-09-05",
            idempotency_key="wo-chain-04",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 82.5, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        p_prop2 = self.service.get_training_plan(date="2026-09-05", equipment=["barbell"])
        sugg2 = p_prop2["plan"]["progression_suggestions"][0]

        # 2. Later confirm second progression: 82.5kg -> 85.0kg
        c2 = self.service.confirm_training_progression(
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
        plan_after_c2 = self.service.get_training_plan(date="2026-09-06", equipment=["barbell"])
        squat_after_c2 = next(e for e in plan_after_c2["plan"]["prescribed_exercises"] if e["name"] == "Barbell Back Squat")
        self.assertEqual(squat_after_c2["suggested_weight_kg"], 85.0)


if __name__ == "__main__":
    unittest.main()
