"""Comprehensive Unit Tests for Cyber Health Domain Capabilities:
- get_training_plan (standard, fatigue deload, safety restricted, 7-day deload)
- complete_workout (workout check-in, red flag detection, state transition)
- query_knowledge (evidence statements, clinical disclosures, non-diagnostic boundaries)
- export_data & import_data (portable fact export and idempotent restoration)
- update_schedule_event (delivery, acknowledgement, postponement, overdue compensation)
- MCP error envelope sanitization (sensitive input stripping)
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cyber_health import (
    CyberHealthService,
    ValidationError,
)
from cyber_health_mcp.server import create_mcp_server


class TestDomainRemaining(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_remaining.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_get_training_plan_states(self) -> None:
        """Verify training plan prescription across normal, fatigue, restricted, and deload states."""
        user_id = "u_plan_user"

        # 1. Normal state -> progressive overload
        res_std = self.service.get_training_plan(user_id=user_id, date="2026-09-04")
        self.assertEqual(res_std["status"], "success")
        self.assertEqual(res_std["plan"]["rule_code"], "TRAIN_PROGRESSION_STANDARD")
        self.assertEqual(res_std["plan"]["intensity_baseline_pct"], 100)
        self.assertTrue(len(res_std["plan"]["prescribed_exercises"]) > 0)

        # 2. Fatigue state: log metrics with fatigue >= 7
        self.service.log_daily_metrics(
            user_id=user_id,
            date="2026-09-04",
            metrics={"fatigue_level": 8, "sleep_hours": 5.0},
            idempotency_key="metric_fatigue_01",
        )
        res_fatigue = self.service.get_training_plan(user_id=user_id, date="2026-09-04")
        self.assertEqual(res_fatigue["plan"]["rule_code"], "TRAIN_RECOVERY_01")
        self.assertEqual(res_fatigue["plan"]["intensity_baseline_pct"], 70)

        # 3. Restricted mode: trigger red flag via workout check-in
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-04",
            idempotency_key="chk_redflag_01",
            discomfort_notes="锻炼中有严重胸痛伴大汗",
        )
        res_restricted = self.service.get_training_plan(user_id=user_id, date="2026-09-04")
        self.assertEqual(res_restricted["plan"]["rule_code"], "SAFETY_RESTRICTED")
        self.assertEqual(res_restricted["plan"]["intensity_baseline_pct"], 0)
        self.assertEqual(res_restricted["plan"]["prescribed_exercises"], [])

        # 4. Deload period: clear red flag with medical clearance
        self.service.update_profile(
            user_id=user_id,
            clear_safety_flags=True,
            clearance_reason="心内科急诊就诊排除ACS，医师出具复训许可证明",
            idempotency_key="clear_redflag_01",
        )
        res_deload = self.service.get_training_plan(user_id=user_id, date="2026-09-05")
        self.assertEqual(res_deload["plan"]["rule_code"], "RECOVERY_FLAG_CLEAR_01")
        self.assertEqual(res_deload["plan"]["intensity_baseline_pct"], 50)
        self.assertEqual(res_deload["plan"]["min_rir"], 3)

    def test_complete_workout_normal_and_red_flag(self) -> None:
        """Verify normal workout logging and acute red-flag symptom triggering restricted mode."""
        user_id = "u_workout_user"

        # Normal workout
        res = self.service.complete_workout(
            user_id=user_id,
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
            user_id=user_id,
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
        prof = self.service.get_profile(user_id)
        self.assertEqual(prof["safety_mode"], "restricted")

    def test_query_knowledge_disclosures(self) -> None:
        """Verify knowledge query returns evidence citations, non-diagnostic warnings, and clinical dependencies."""
        res = self.service.query_knowledge(query="protein intake recommendation for athletes", category="nutrition")
        self.assertEqual(res["status"], "success")
        self.assertIn("clinical_review_status", res["data"])
        self.assertEqual(
            res["data"]["clinical_review_status"],
            "evidence_rules_algorithmic_pending_licensed_physician_review",
        )
        self.assertIn("requires_credentialed_sports_dietitian_or_physician_for_individual_prescription", res["data"]["external_dependency"])
        self.assertTrue(len(res["data"]["evidence_items"]) > 0)
        self.assertIn("ISSN Position Stand", res["data"]["evidence_items"][0]["title"])
        self.assertTrue(any("NON_DIAGNOSTIC" in w for w in res["warnings"]))

    def test_export_and_import_data_roundtrip(self) -> None:
        """Verify export produces portable SQLite fact snapshot and import restores it idempotently."""
        user_id = "u_backup_user"

        # Seed data
        self.service.update_profile(
            user_id=user_id,
            goals={"target_kcal_low": 2000, "target_kcal_high": 2300},
            idempotency_key="prof_seed_01",
        )
        self.service.log_meal(
            user_id=user_id,
            occurred_at="2026-09-04T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "chicken breast", "amount_g": {"low": 200, "high": 200}}],
            kcal_low=300,
            kcal_high=350,
            idempotency_key="meal_seed_01",
        )
        self.service.complete_workout(
            user_id=user_id,
            date="2026-09-04",
            idempotency_key="wk_seed_01",
            completed_exercises=[{"name": "Pushup", "sets": 3, "reps": 15}],
        )

        # Export
        exported = self.service.export_data(user_id=user_id)
        self.assertEqual(exported["status"], "success")
        self.assertGreaterEqual(exported["data"]["facts"]["meal_count"], 1)
        self.assertGreaterEqual(exported["data"]["facts"]["domain_records_count"], 1)

        # Import into fresh database
        db_path_new = Path(self.temp_dir.name) / "test_imported.sqlite3"
        service_new = CyberHealthService(db_path_new)

        import_res = service_new.import_data(
            user_id=user_id,
            data=exported["data"],
            idempotency_key="import_01",
        )
        self.assertEqual(import_res["status"], "success")
        self.assertGreaterEqual(import_res["data"]["imported_meals"], 1)

        # Re-import same key -> exact replay
        replay_res = service_new.import_data(
            user_id=user_id,
            data=exported["data"],
            idempotency_key="import_01",
        )
        self.assertEqual(replay_res["operation_id"], import_res["operation_id"])

        # Check today query on new database reflects imported meal
        today = service_new.get_today(user_id=user_id, day="2026-09-04")
        self.assertGreaterEqual(today["nutrition"]["meal_count"], 1)

    def test_update_schedule_event_lifecycle(self) -> None:
        """Verify schedule event delivery, acknowledgement, postponement, and overdue compensation."""
        user_id = "u_sched_user"
        date = "2026-09-04"

        # Generate schedule
        sched_res = self.service.schedule_daily_reminders(
            user_id=user_id,
            date=date,
            idempotency_key="sched_gen_01",
        )
        events = sched_res["data"]["scheduled_events"]
        self.assertEqual(len(events), 5)
        target_event = events[0]
        ev_id = target_event["event_id"]

        # 1. Delivered
        d_res = self.service.update_schedule_event(
            user_id=user_id,
            event_id=ev_id,
            action="delivered",
            idempotency_key="ev_deliv_01",
        )
        self.assertEqual(d_res["data"]["status"], "delivered")
        self.assertEqual(d_res["data"]["delivery_attempts"], 1)

        # 2. Acknowledged
        a_res = self.service.update_schedule_event(
            user_id=user_id,
            event_id=ev_id,
            action="acknowledged",
            idempotency_key="ev_ack_01",
        )
        self.assertEqual(a_res["data"]["status"], "acknowledged")

        # 3. Postponed with new window
        p_res = self.service.update_schedule_event(
            user_id=user_id,
            event_id=ev_id,
            action="postponed",
            new_window_start="2026-09-04T09:00:00+08:00",
            new_window_end="2026-09-04T10:00:00+08:00",
            idempotency_key="ev_post_01",
        )
        self.assertEqual(p_res["data"]["status"], "pending")
        self.assertGreater(p_res["data"]["revision"], 1)

        # 4. Overdue compensation check
        with self.service.store.transaction() as conn:
            conn.execute("UPDATE schedule_event SET status = 'overdue' WHERE event_id = ?", (ev_id,))

        comp_res = self.service.update_schedule_event(
            user_id=user_id,
            event_id=ev_id,
            action="acknowledged",
            idempotency_key="ev_comp_01",
        )
        self.assertIsNotNone(comp_res["data"]["compensation"])
        self.assertEqual(comp_res["data"]["compensation"]["status"], "compensated")

    def test_mcp_err_envelope_sanitization(self) -> None:
        """Verify error envelope strips raw input values and does not leak raw sensitive text."""
        server = create_mcp_server(self.db_path)

        # Access the wrapped function or tool directly
        log_tool = None
        for tool in server._tool_manager.list_tools():
            if tool.name == "cyber_health_log_meal":
                log_tool = tool.fn
                break
        
        self.assertIsNotNone(log_tool)
        res = log_tool(
            user_id="u_sanit_user",
            occurred_at="2026-09-04T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "bread", "amount_g": {"low": 300, "high": 100}}],
            kcal_low=100,
            kcal_high=200,
            idempotency_key="key_err_01",
        )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["error"]["code"], "VALIDATION_ERROR")
        self.assertNotIn("input_value=", res["error"]["message"])


if __name__ == "__main__":
    unittest.main()
