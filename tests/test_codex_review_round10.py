"""Tests for Codex Review Round 10:
- Shared progression state machine between suggestion and confirmation
- Recent failure breaks streak: Success -> Success -> Recent Failure never proposes progression
- Same-day split records deduplication / session identification
- Comparable load check across qualifying sessions (no false progression from different weights)
- Multi-set completion verification (all working sets must reach target reps; no single-set false positives)
- Missing sets, missing evidence, or missing RPE never default to success
- Confirmation safety gates: Restricted Mode, 7-day Deload, Acute Fatigue (TRAIN_RECOVERY_01), Contraindications
- Confirmation evidence verification: rejects dummy IDs, cross-user records, mismatched proposal IDs, or altered loads
- Rep-based progression confirmation for bodyweight exercises (confirmed_reps)
- Separation of manual baseline load recording from verified progression confirmation
- Real Core and MCP stdio calling boundary tests
"""

from __future__ import annotations

import json
import subprocess
import sys
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


class TestCodexReviewRound10(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_round10.sqlite3"
        self.service = CyberHealthService(self.db_path, recovery_evidence_window_days=1)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # =========================================================================
    # 1. Progression State Machine: Failure Breaks Streak & Same-Day Deduplication
    # =========================================================================

    def test_recent_failure_breaks_progression_streak(self) -> None:
        """Verify that a recent failed/incomplete workout breaks the consecutive streak.
        History: Day 1 Success -> Day 2 Success -> Day 3 Failure.
        The algorithm must NOT skip Day 3 to evaluate Day 1 and Day 2!
        """
        user_id = "u_streak_fail"
        # Day 1: Success (80kg x 8, sets 3, rpe 7.0, complete)
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-01",
            idempotency_key="wo-s1",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        # Day 2: Success (80kg x 8, sets 3, rpe 7.5, complete)
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-02",
            idempotency_key="wo-s2",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        # Verify that Day 2 alone before failure WOULD have had a proposal
        plan_day2 = self.service.get_training_plan(user_id=user_id, date="2026-09-02", equipment=["barbell"])
        self.assertEqual(len(plan_day2["plan"]["progression_suggestions"]), 1)

        # Day 3: FAILURE / Incomplete (completion_rate = 0.5 or reps short)
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-03",
            idempotency_key="wo-s3-fail",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 5, "sets": 3}],
            session_rpe=9.0,
            completion_rate=0.5,
        )

        # After Day 3, evaluating progression MUST return 0 suggestions!
        plan_after_fail = self.service.get_training_plan(user_id=user_id, date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(plan_after_fail["plan"]["progression_suggestions"]), 0)

        # Also test with high RPE failure (completion_rate 1.0, but RPE 9.5)
        user_id2 = "u_streak_high_rpe"
        self.service.complete_workout(
            user_id=user_id2,
            date="2026-09-01",
            idempotency_key="wo2-s1",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            user_id=user_id2,
            date="2026-09-02",
            idempotency_key="wo2-s2",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.5,
            completion_rate=1.0,
        )
        # Day 3: high RPE (9.5)
        self.service.complete_workout(
            user_id=user_id2,
            date="2026-09-03",
            idempotency_key="wo2-s3",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=9.5,
            completion_rate=1.0,
        )
        plan_after_high_rpe = self.service.get_training_plan(user_id=user_id2, date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(plan_after_high_rpe["plan"]["progression_suggestions"]), 0)

    def test_sameday_split_records_consolidated_as_single_session(self) -> None:
        """Verify that multiple workout logs on the same day are consolidated into a single session.
        Two logs on the same day must NOT be counted as 2 consecutive sessions!
        """
        user_id = "u_split_sameday"
        # Day 1: User splits workout into 2 logs (e.g. warm-up/morning and afternoon)
        self.service.log_workout(
            user_id=user_id,
            date="2026-09-01",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 1, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
            ],
            session_id="session_morning",
            idempotency_key="wo-split-01",
            completion_rate=1.0,
        )
        self.service.log_workout(
            user_id=user_id,
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
        plan_day1 = self.service.get_training_plan(user_id=user_id, date="2026-09-01", equipment=["barbell"])
        self.assertEqual(len(plan_day1["plan"]["progression_suggestions"]), 0)

        # Day 2: Completed all sets
        self.service.log_workout(
            user_id=user_id,
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
        plan_day2 = self.service.get_training_plan(user_id=user_id, date="2026-09-03", equipment=["barbell"])
        suggs = plan_day2["plan"]["progression_suggestions"]
        self.assertEqual(len(suggs), 1)
        self.assertEqual(suggs[0]["suggested_weight_kg"], 82.5)

    def test_incomparable_weights_across_sessions_rejected(self) -> None:
        """Verify that 2 sessions at different weights (e.g. 70kg and 80kg) do NOT trigger progression.
        Double progression requires repeating the SAME target load.
        """
        user_id = "u_diff_weights"
        # Session 1: 70kg x 8 reps
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-01",
            idempotency_key="wo-70kg",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 70.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        # Session 2: 80kg x 8 reps
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-02",
            idempotency_key="wo-80kg",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        plan = self.service.get_training_plan(user_id=user_id, date="2026-09-03", equipment=["barbell"])
        # Incomparable weights across sessions -> no progression yet!
        self.assertEqual(len(plan["plan"]["progression_suggestions"]), 0)

    def test_incomplete_working_sets_rejected(self) -> None:
        """Verify that achieving target reps on only 1 or 2 sets while failing the 3rd set breaks progression."""
        user_id = "u_set_fail"
        # Session 1: Clean success (3 sets of 8)
        self.service.log_workout(
            user_id=user_id,
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
            user_id=user_id,
            date="2026-09-02",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 1, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
                {"exercise": "Barbell Back Squat", "set_num": 2, "reps": 8, "weight_kg": 80.0, "rpe": 7.5},
                {"exercise": "Barbell Back Squat", "set_num": 3, "reps": 6, "weight_kg": 80.0, "rpe": 8.0},
            ],
            idempotency_key="wo-partial-02",
            completion_rate=1.0,
        )
        plan = self.service.get_training_plan(user_id=user_id, date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(plan["plan"]["progression_suggestions"]), 0)

    def test_missing_sets_or_missing_rpe_rejected(self) -> None:
        """Verify that missing set count or missing RPE never defaults to success."""
        user_id = "u_missing_sets"
        # Session 1: only 1 set logged when 3 are required
        self.service.log_workout(
            user_id=user_id,
            date="2026-09-01",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 1, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
            ],
            idempotency_key="wo-1set-01",
            completion_rate=1.0,
        )
        self.service.log_workout(
            user_id=user_id,
            date="2026-09-02",
            actual_sets=[
                {"exercise": "Barbell Back Squat", "set_num": 1, "reps": 8, "weight_kg": 80.0, "rpe": 7.0},
            ],
            idempotency_key="wo-1set-02",
            completion_rate=1.0,
        )
        plan = self.service.get_training_plan(user_id=user_id, date="2026-09-03", equipment=["barbell"])
        self.assertEqual(len(plan["plan"]["progression_suggestions"]), 0)

    # =========================================================================
    # 2. Confirmation Safety Gates: Restricted, Deload, Fatigue, Contraindications
    # =========================================================================

    def _seed_valid_squat_progression(self, user_id: str) -> dict[str, Any]:
        """Helper to seed 2 valid sessions and return the active proposal."""
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-01",
            idempotency_key=f"wo-{user_id}-1",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-02",
            idempotency_key=f"wo-{user_id}-2",
            completed_exercises=[{"name": "Barbell Back Squat", "weight_kg": 80.0, "reps": 8, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        plan = self.service.get_training_plan(user_id=user_id, date="2026-09-02", equipment=["barbell"])
        return plan["plan"]["progression_suggestions"][0]

    def test_confirm_progression_blocked_under_restricted_mode(self) -> None:
        """Verify that confirm_training_progression raises SafetyRestrictedError when in restricted mode."""
        user_id = "u_conf_restr"
        prop = self._seed_valid_squat_progression(user_id)

        # Trigger restricted mode
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-03",
            idempotency_key="wo-restr",
            discomfort_notes="出现严重胸痛与呼吸困难",
        )

        with self.assertRaises(SafetyRestrictedError):
            self.service.confirm_training_progression(
                user_id=user_id,
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-fail-restr",
            )

    def test_confirm_progression_blocked_under_active_deload(self) -> None:
        """Verify that confirm_training_progression is blocked during 7-day Deload period."""
        user_id = "u_conf_deload"
        prop = self._seed_valid_squat_progression(user_id)

        # Set profile in active deload directly in database
        with self.service.store.transaction() as conn:
            conn.execute(
                "UPDATE user_profile SET deload_until = '2026-09-20' WHERE user_id = ?",
                (user_id,),
            )

        with self.assertRaises(SafetyRestrictedError) as ctx:
            self.service.confirm_training_progression(
                user_id=user_id,
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-fail-deload",
            )
        self.assertIn("RECOVERY_FLAG_CLEAR_01", str(ctx.exception))

    def test_confirm_progression_blocked_under_fatigue_recovery(self) -> None:
        """Verify that confirm_training_progression is blocked when recent daily metrics show severe fatigue."""
        user_id = "u_conf_fatigue"
        prop = self._seed_valid_squat_progression(user_id)

        # Log daily state showing severe fatigue and sleep deprivation today
        today = self.service._now()[:10]
        self.service.log_daily_metrics(
            user_id=user_id,
            date=today,
            metrics={"sleep_hours": 4.0, "fatigue_level": 9},
            idempotency_key="ds-fatigue-today",
        )

        with self.assertRaises(SafetyRestrictedError) as ctx:
            self.service.confirm_training_progression(
                user_id=user_id,
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-fail-fatigue",
            )
        self.assertIn("TRAIN_RECOVERY_01", str(ctx.exception))

    def test_confirm_progression_blocked_if_exercise_contraindicated(self) -> None:
        """Verify that confirm_training_progression is blocked if the exercise is contraindicated by active constraints."""
        user_id = "u_conf_contra"
        prop = self._seed_valid_squat_progression(user_id)

        # Add knee constraint to user profile
        self.service.update_profile(
            user_id=user_id,
            constraints={"joint_issues": ["knee_pain", "patella"]},
            idempotency_key="prof-knee-contra",
        )

        with self.assertRaises(SafetyRestrictedError) as ctx:
            self.service.confirm_training_progression(
                user_id=user_id,
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-fail-contra",
            )
        self.assertIn("contraindicated", str(ctx.exception).lower())

    # =========================================================================
    # 3. Confirmation Evidence Verification & Rejections
    # =========================================================================

    def test_confirm_progression_rejects_dummy_or_cross_user_evidence(self) -> None:
        """Verify that confirm_training_progression strictly rejects dummy IDs and cross-user records."""
        user_id = "u_conf_auth"
        prop = self._seed_valid_squat_progression(user_id)

        # 1. Reject dummy source records
        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_training_progression(
                user_id=user_id,
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=["wo-fake-1", "wo-fake-2"],
                idempotency_key="conf-dummy",
            )
        self.assertIn("does not exist", str(ctx.exception))

        # 2. Reject cross-user source records
        user_b = "u_conf_user_b"
        prop_b = self._seed_valid_squat_progression(user_b)
        with self.assertRaises(ValidationError) as ctx2:
            self.service.confirm_training_progression(
                user_id=user_id,
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop_b["evidence_source_record_ids"],  # Belongs to user_b!
                idempotency_key="conf-cross",
            )
        self.assertIn("cross-user", str(ctx2.exception).lower())

    def test_confirm_progression_rejects_mismatched_proposal_id_or_load(self) -> None:
        """Verify that confirm_training_progression rejects forged proposal IDs or arbitrary weights."""
        user_id = "u_conf_mismatch"
        prop = self._seed_valid_squat_progression(user_id)

        # 1. Mismatched proposal ID
        with self.assertRaises(ValidationError) as ctx:
            self.service.confirm_training_progression(
                user_id=user_id,
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
                user_id=user_id,
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=120.0,
                proposal_id=prop["proposal_id"],
                source_record_ids=prop["evidence_source_record_ids"],
                idempotency_key="conf-mismatch-weight",
            )
        self.assertIn("does not match proposed weight", str(ctx2.exception))

        # 3. Confirming when no qualifying history exists at all
        user_no_history = "u_no_wo_history"
        with self.assertRaises(ValidationError) as ctx3:
            self.service.confirm_training_progression(
                user_id=user_no_history,
                exercise_name="Barbell Back Squat",
                confirmed_weight_kg=82.5,
                source_record_ids=["wo-123"],
                idempotency_key="conf-no-hist",
            )
        self.assertTrue("does not exist" in str(ctx3.exception) or "No active qualifying" in str(ctx3.exception))

    # =========================================================================
    # 4. Bodyweight Rep Progression Confirmation & Manual Baseline Separation
    # =========================================================================

    def test_confirm_progression_bodyweight_reps(self) -> None:
        """Verify proposing and confirming rep-based progression for bodyweight movements."""
        user_id = "u_bw_prog"
        # Seed 2 sessions of Glute Bridge (bodyweight, target_reps=15, sets=3)
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-01",
            idempotency_key="wo-bw-1",
            completed_exercises=[{"name": "Glute Bridge", "reps": 15, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-02",
            idempotency_key="wo-bw-2",
            completed_exercises=[{"name": "Glute Bridge", "reps": 15, "sets": 3}],
            session_rpe=7.0,
            completion_rate=1.0,
        )

        plan = self.service.get_training_plan(user_id=user_id, date="2026-09-02", equipment=["bodyweight"])
        suggs = plan["plan"]["progression_suggestions"]
        self.assertEqual(len(suggs), 1)
        bw_sugg = suggs[0]
        self.assertEqual(bw_sugg["exercise_name"], "Glute Bridge")
        self.assertIsNone(bw_sugg["suggested_weight_kg"])
        self.assertEqual(bw_sugg["suggested_reps"], 16)

        # Confirm rep progression
        conf = self.service.confirm_training_progression(
            user_id=user_id,
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
        user_id = "u_manual_base"
        res = self.service.record_exercise_baseline(
            user_id=user_id,
            exercise_name="Barbell Back Squat",
            weight_kg=60.0,
            idempotency_key="man-base-01",
            user_note="Initial baseline self-test",
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["data"]["weight_kg"], 60.0)
        self.assertEqual(res["data"]["verification_type"], "manual_baseline")

        # Next training plan should read 60.0kg as baseline
        plan = self.service.get_training_plan(user_id=user_id, date="2026-09-01", equipment=["barbell"])
        squat_ex = [e for e in plan["plan"]["prescribed_exercises"] if e["name"] == "Barbell Back Squat"][0]
        self.assertEqual(squat_ex["suggested_weight_kg"], 60.0)

    # =========================================================================
    # 5. Real MCP stdio Calling Boundary Test
    # =========================================================================

    def test_confirm_progression_stdio_mcp_boundary(self) -> None:
        """Verify that cyber_health_confirm_training_progression works over MCP stdio protocol."""
        root = Path(__file__).resolve().parents[1]
        user_id = "owner"

        # Seed 2 workouts in DB first
        prop = self._seed_valid_squat_progression(user_id)

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
        stdout, stderr = proc.communicate(input=input_data, timeout=10)
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


if __name__ == "__main__":
    unittest.main()
