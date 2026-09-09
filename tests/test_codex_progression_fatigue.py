"""Confirmation must consume the same actual metrics schema as planning."""
import tempfile
import unittest
from pathlib import Path
from cyber_health import CyberHealthService, SafetyRestrictedError, ValidationError


class ProgressionFatigueTests(unittest.TestCase):
    def test_sleep_deficit_blocks_previously_generated_proposal(self):
        """Preserve original Codex test: sleep deficit 5.9h via real API blocks progression confirmation."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"
            for day in ("2026-09-03", "2026-09-04"):
                s.complete_workout(user_id="u", date=day, idempotency_key=day,
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)
            proposal = s.get_training_plan(user_id="u", date="2026-09-05",
                equipment=["barbell"])["plan"]["progression_suggestions"][0]
            s.log_daily_metrics(user_id="u", date="2026-09-05",
                metrics={"sleep_hours": 5.9, "fatigue_level": 1}, idempotency_key="sleep")
            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(user_id="u", exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=proposal["suggested_weight_kg"],
                    source_record_ids=proposal["evidence_source_record_ids"],
                    proposal_id=proposal["proposal_id"], idempotency_key="confirm")

    def test_fatigue_level_seven_blocks_progression_via_real_api(self):
        """Acute fatigue level 7 logged via real API blocks progression suggestions and confirmation."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"
            for day in ("2026-09-03", "2026-09-04"):
                s.complete_workout(user_id="u", date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)
            # Before fatigue is logged, progression proposal is generated
            plan_before = s.get_training_plan(user_id="u", date="2026-09-05", equipment=["barbell"])
            proposals = plan_before["plan"]["progression_suggestions"]
            self.assertEqual(len(proposals), 1)
            prop = proposals[0]

            # Log fatigue_level=7 via real log_daily_metrics API
            s.log_daily_metrics(user_id="u", date="2026-09-05",
                metrics={"sleep_hours": 8.0, "fatigue_level": 7}, idempotency_key="fatigue_7")

            # Training plan now shifts to TRAIN_RECOVERY_01 and suppresses progression suggestions
            plan_after = s.get_training_plan(user_id="u", date="2026-09-05", equipment=["barbell"])
            self.assertEqual(plan_after["plan"]["rule_code"], "TRAIN_RECOVERY_01")
            self.assertEqual(len(plan_after["plan"].get("progression_suggestions", [])), 0)

            # Confirming previously captured proposal must raise SafetyRestrictedError
            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(user_id="u", exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=prop["suggested_weight_kg"],
                    source_record_ids=prop["evidence_source_record_ids"],
                    proposal_id=prop["proposal_id"], idempotency_key="confirm_f7")

    def test_sleep_hours_zero_not_falsy_blocks_progression(self):
        """sleep_hours=0.0 must not be treated as falsy/default 8.0 and must block progression."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"
            for day in ("2026-09-03", "2026-09-04"):
                s.complete_workout(user_id="u", date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)
            proposal = s.get_training_plan(user_id="u", date="2026-09-05",
                equipment=["barbell"])["plan"]["progression_suggestions"][0]

            # Log real metrics with sleep_hours=0.0
            s.log_daily_metrics(user_id="u", date="2026-09-05",
                metrics={"sleep_hours": 0.0, "fatigue_level": 1}, idempotency_key="sleep_0")

            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(user_id="u", exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=proposal["suggested_weight_kg"],
                    source_record_ids=proposal["evidence_source_record_ids"],
                    proposal_id=proposal["proposal_id"], idempotency_key="confirm_s0")

    def test_recovery_score_zero_not_falsy_blocks_progression(self):
        """recovery_score=0 must not be treated as falsy/default 100 and must block progression."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"
            for day in ("2026-09-03", "2026-09-04"):
                s.complete_workout(user_id="u", date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)
            proposal = s.get_training_plan(user_id="u", date="2026-09-05",
                equipment=["barbell"])["plan"]["progression_suggestions"][0]

            # Metrics causing real recovery_score to bottom out at 0
            log_res = s.log_daily_metrics(user_id="u", date="2026-09-05",
                metrics={"sleep_hours": 1.0, "fatigue_level": 10, "sleep_quality": "poor"},
                idempotency_key="rec_0")
            self.assertEqual(log_res["data"]["recovery_score"], 0)

            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(user_id="u", exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=proposal["suggested_weight_kg"],
                    source_record_ids=proposal["evidence_source_record_ids"],
                    proposal_id=proposal["proposal_id"], idempotency_key="confirm_r0")

    def test_future_daily_record_does_not_shadow_current_fatigue(self):
        """A future daily_state record (day > target_date) must not mask active acute fatigue."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-05T08:00:00+08:00"
            for day in ("2026-09-02", "2026-09-03"):
                s.complete_workout(user_id="u", date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)
            proposal = s.get_training_plan(user_id="u", date="2026-09-04",
                equipment=["barbell"])["plan"]["progression_suggestions"][0]

            # Current fatigue logged on Sep 4 (1 day ago relative to Sep 5)
            s.log_daily_metrics(user_id="u", date="2026-09-04",
                metrics={"sleep_hours": 4.0, "fatigue_level": 8}, idempotency_key="fatigue_sep4")

            # A future daily state is logged on Sep 7 with fresh metrics
            s.log_daily_metrics(user_id="u", date="2026-09-07",
                metrics={"sleep_hours": 8.5, "fatigue_level": 1}, idempotency_key="future_healthy_sep7")

            # On Sep 5, target date filtering (day <= 2026-09-05) must pick Sep 4 fatigue, not Sep 7
            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(user_id="u", exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=proposal["suggested_weight_kg"],
                    source_record_ids=proposal["evidence_source_record_ids"],
                    proposal_id=proposal["proposal_id"], idempotency_key="confirm_shadow")

    def test_future_workout_does_not_count_towards_progression(self):
        """Workouts logged with future dates must not qualify progression before their time."""
        with tempfile.TemporaryDirectory() as tmp:
            s = CyberHealthService(Path(tmp) / "test.db")
            s._now = lambda: "2026-09-04T12:00:00+08:00"
            # Past qualifying workout on Sep 2
            w_sep2 = s.complete_workout(user_id="u", date="2026-09-02", idempotency_key="wo_sep2",
                completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                      "reps": 8, "sets": 3}],
                session_rpe=7, completion_rate=1)
            # Sep 3 workout did NOT reach reps_max (only 7 reps)
            s.complete_workout(user_id="u", date="2026-09-03", idempotency_key="wo_sep3",
                completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                      "reps": 7, "sets": 3}],
                session_rpe=7, completion_rate=1)
            # Future workout on Sep 7 achieved 8 reps
            w_sep7 = s.complete_workout(user_id="u", date="2026-09-07", idempotency_key="wo_sep7",
                completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                      "reps": 8, "sets": 3}],
                session_rpe=7, completion_rate=1)

            # On Sep 4, workouts on Sep 7 must be ignored. Most recent before Sep 4 is Sep 3 (failed),
            # so no active qualifying progression proposal exists.
            with self.assertRaises(ValidationError) as ctx:
                s.confirm_training_progression(user_id="u", exercise_name="Barbell Back Squat",
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
            s.log_daily_metrics(user_id="u", date="2026-08-21",
                metrics={"sleep_hours": 3.0, "fatigue_level": 9}, idempotency_key="stale_fatigue")

            # Recent consecutive qualifying workouts on Sep 3 and Sep 4
            for day in ("2026-09-03", "2026-09-04"):
                s.complete_workout(user_id="u", date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)

            # On Sep 5, stale fatigue does not trigger TRAIN_RECOVERY_01
            plan = s.get_training_plan(user_id="u", date="2026-09-05", equipment=["barbell"])
            self.assertEqual(plan["plan"]["rule_code"], "TRAIN_PROGRESSION_STANDARD")
            proposals = plan["plan"]["progression_suggestions"]
            self.assertEqual(len(proposals), 1)
            prop = proposals[0]

            # Progression confirmation succeeds!
            confirm_res = s.confirm_training_progression(
                user_id="u",
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
            s.update_profile(user_id="u_tz", timezone="America/New_York", idempotency_key="prof_tz")

            # Workouts logged on local dates Sep 2 and Sep 3
            for day in ("2026-09-02", "2026-09-03"):
                s.complete_workout(user_id="u_tz", date=day, idempotency_key=f"wo_{day}",
                    completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80,
                                          "reps": 8, "sets": 3}],
                    session_rpe=7, completion_rate=1)

            # Proposal on local date 2026-09-04
            prop = s.get_training_plan(user_id="u_tz", date="2026-09-04",
                equipment=["barbell"])["plan"]["progression_suggestions"][0]

            # Fatigue logged on local date 2026-09-04
            s.log_daily_metrics(user_id="u_tz", date="2026-09-04",
                metrics={"sleep_hours": 4.5, "fatigue_level": 8}, idempotency_key="fatigue_ny")

            # Current UTC time is 2026-09-05T02:00:00Z.
            # In New York (EDT, UTC-4), this is 2026-09-04T22:00:00-04:00 (local date: 2026-09-04).
            s._now = lambda: "2026-09-05T02:00:00Z"

            # Should evaluate local date 2026-09-04, detecting the Sep 4 fatigue and blocking
            with self.assertRaises(SafetyRestrictedError):
                s.confirm_training_progression(user_id="u_tz", exercise_name="Barbell Back Squat",
                    confirmed_weight_kg=prop["suggested_weight_kg"],
                    source_record_ids=prop["evidence_source_record_ids"],
                    proposal_id=prop["proposal_id"], idempotency_key="confirm_tz")


if __name__ == "__main__":
    unittest.main()
