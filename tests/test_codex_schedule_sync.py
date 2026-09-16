import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cyber_health.service import CyberHealthService
from cyber_health.store import SQLiteStore

UTC = timezone.utc


class TestCodexScheduleSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_schedule.db")
        self.store = SQLiteStore(self.db_path)
        self.service = CyberHealthService(self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_five_standard_windows_generated_with_stable_ids(self):
        """Standard schedule generates 5 windows: morning, lunch, workout, dinner, review."""
        user_id = "u_sched_test"
        date = "2026-09-04"
        res1 = self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="sched-k1")
        events1 = res1["data"]["scheduled_events"]
        self.assertEqual(len(events1), 5)

        event_types = [e["event_type"] for e in events1]
        self.assertEqual(event_types, ["MORNING_PLAN", "MEAL_CHECK", "WORKOUT_REMINDER", "MEAL_CHECK", "DAILY_REVIEW"])

        # Check conditions
        conditions = [e["trigger_condition"] for e in events1]
        self.assertEqual(
            conditions,
            ["morning_plan_not_locked", "lunch_not_logged", "workout_pending", "dinner_not_logged", "review_pending"],
        )

        # Distinct request replay produces identical IDs
        res2 = self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="sched-k2")
        events2 = res2["data"]["scheduled_events"]
        self.assertEqual([e["event_id"] for e in events1], [e["event_id"] for e in events2])

    def test_postponement_preserved_on_rescheduling(self):
        """User-postponed events retain their new window and revision > 1, never overridden by defaults."""
        user_id = "u_postpone_user"
        date = "2026-09-04"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="init_sched")

        lunch_id = f"sched_{user_id}_{date}_meal_check_lunch"
        new_start = "2026-09-04T14:30:00+08:00"
        new_end = "2026-09-04T15:30:00+08:00"

        # Postpone lunch
        postpone_res = self.service.update_schedule_event(
            user_id=user_id,
            event_id=lunch_id,
            action="postponed",
            new_window_start=new_start,
            new_window_end=new_end,
            idempotency_key="postpone-lunch",
        )
        self.assertEqual(postpone_res["data"]["status"], "pending")
        self.assertEqual(postpone_res["data"]["revision"], 2)

        # Re-run schedule_daily_reminders (e.g. host daily sync)
        res_resched = self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="resched-lunch")
        lunch_event = next(e for e in res_resched["data"]["scheduled_events"] if e["event_id"] == lunch_id)

        # Must preserve postponed window and revision
        self.assertEqual(lunch_event["window_start"], new_start)
        self.assertEqual(lunch_event["window_end"], new_end)
        self.assertEqual(lunch_event["revision"], 2)
        self.assertEqual(lunch_event["status"], "pending")

    def test_tombstone_snapshot_for_host_timer_cancellation(self):
        """Cancelled and skipped events are hidden by default, but exposed as tombstones when include_inactive=True."""
        user_id = "u_tombstone_user"
        date = "2026-09-04"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="init_sched")

        wo_id = f"sched_{user_id}_{date}_workout_reminder"
        dinner_id = f"sched_{user_id}_{date}_meal_check_dinner"

        # Cancel workout, skip dinner
        self.service.update_schedule_event(
            user_id=user_id,
            event_id=wo_id,
            action="cancelled",
            idempotency_key="cancel-wo",
        )
        self.service.update_schedule_event(
            user_id=user_id,
            event_id=dinner_id,
            action="skipped",
            idempotency_key="skip-dinner",
        )

        # Default query: only active pending/overdue
        active_events = self.service.get_schedule(user_id=user_id, date=date, include_inactive=False)
        self.assertEqual(len(active_events), 3)
        self.assertNotIn(wo_id, [e["event_id"] for e in active_events])
        self.assertNotIn(dinner_id, [e["event_id"] for e in active_events])

        # Full snapshot: include_inactive=True returns tombstones
        full_events = self.service.get_schedule(user_id=user_id, date=date, include_inactive=True)
        self.assertEqual(len(full_events), 5)

        wo_event = next(e for e in full_events if e["event_id"] == wo_id)
        self.assertEqual(wo_event["status"], "cancelled")
        self.assertFalse(wo_event["eligible"])
        self.assertEqual(wo_event["suppression_reason"], "event_cancelled")

        dinner_event = next(e for e in full_events if e["event_id"] == dinner_id)
        self.assertEqual(dinner_event["status"], "skipped")
        self.assertFalse(dinner_event["eligible"])
        self.assertEqual(dinner_event["suppression_reason"], "event_skipped")

    def test_read_purity_of_get_schedule(self):
        """get_schedule derives overdue in memory without updating DB or state_version."""
        user_id = "u_purity_user"
        date = "2026-09-04"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="init_sched")

        prof_before = self.service.get_profile(user_id)
        version_before = prof_before["state_version"]

        with self.store.connect() as conn:
            op_count_before = conn.execute("SELECT COUNT(*) AS c FROM operation_log WHERE user_id = ?", (user_id,)).fetchone()["c"]

        # Call get_schedule at 23:00 (all 5 windows have elapsed)
        now_late = datetime(2026, 9, 4, 23, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        events = self.service.get_schedule(user_id=user_id, date=date, now=now_late)

        # All events should be derived as overdue with compensation required
        self.assertEqual(len(events), 5)
        for e in events:
            self.assertEqual(e["status"], "overdue")
            self.assertTrue(e["compensation_required"])

        # Profile state version must be strictly untouched
        prof_after = self.service.get_profile(user_id)
        self.assertEqual(prof_after["state_version"], version_before)

        # Operation log must have ZERO new operations
        with self.store.connect() as conn:
            op_count_after = conn.execute("SELECT COUNT(*) AS c FROM operation_log WHERE user_id = ?", (user_id,)).fetchone()["c"]
            self.assertEqual(op_count_after, op_count_before)

            # In SQLite, schedule_event rows must still remain pending (not sneaky unversioned UPDATEs)
            raw_statuses = [r["status"] for r in conn.execute("SELECT status FROM schedule_event WHERE user_id = ?", (user_id,)).fetchall()]
            self.assertTrue(all(s == "pending" for s in raw_statuses))

    def test_dynamic_eligibility_suppression_rules(self):
        """Test trigger eligibility suppression by domain facts."""
        user_id = "u_facts_user"
        date = "2026-09-04"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="init_sched")

        # 1. Initially all 5 are eligible
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertTrue(events["morning_plan_not_locked"]["eligible"])
        self.assertTrue(events["lunch_not_logged"]["eligible"])
        self.assertTrue(events["workout_pending"]["eligible"])
        self.assertTrue(events["dinner_not_logged"]["eligible"])
        self.assertTrue(events["review_pending"]["eligible"])

        # 2. Lock morning plan -> morning_plan suppressed
        self.service.plan_tomorrow(user_id=user_id, date=date, commit=True, idempotency_key="commit-plan")
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertFalse(events["morning_plan_not_locked"]["eligible"])
        self.assertEqual(events["morning_plan_not_locked"]["suppression_reason"], "morning_plan_already_committed")

        # 3. Log lunch -> lunch_check suppressed
        self.service.log_meal(
            user_id=user_id,
            occurred_at=f"{date}T12:30:00+08:00",
            meal_type="lunch",
            foods=[{"name": "米饭", "amount_g": {"low": 150, "high": 150}}],
            kcal_low=300,
            kcal_high=350,
            idempotency_key="log-lunch",
        )
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertFalse(events["lunch_not_logged"]["eligible"])
        self.assertEqual(events["lunch_not_logged"]["suppression_reason"], "lunch_already_logged")
        # Dinner remains eligible
        self.assertTrue(events["dinner_not_logged"]["eligible"])

        # 4. Complete workout -> workout_reminder suppressed
        self.service.complete_workout(
            user_id=user_id,
            date=date,
            completed_exercises=[{"name": "深蹲", "sets": 3, "reps": 8}],
            completion_rate=1.0,
            idempotency_key="comp-wo",
        )
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertFalse(events["workout_pending"]["eligible"])
        self.assertEqual(events["workout_pending"]["suppression_reason"], "workout_already_completed")

        # 5. Log dinner -> dinner_check suppressed
        self.service.log_meal(
            user_id=user_id,
            occurred_at=f"{date}T19:00:00+08:00",
            meal_type="dinner",
            foods=[{"name": "鸡胸肉沙拉", "amount_g": {"low": 200, "high": 200}}],
            kcal_low=250,
            kcal_high=300,
            idempotency_key="log-dinner",
        )
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertFalse(events["dinner_not_logged"]["eligible"])
        self.assertEqual(events["dinner_not_logged"]["suppression_reason"], "dinner_already_logged")

        # 6. Complete daily review -> review suppressed
        self.service.daily_review(user_id=user_id, date=date, idempotency_key="rev-today")
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertFalse(events["review_pending"]["eligible"])
        self.assertEqual(events["review_pending"]["suppression_reason"], "daily_review_already_completed")

    def test_profile_reminders_disabled_suppresses_all(self):
        """When user disables reminders in profile, all schedule events are suppressed."""
        user_id = "u_disabled_reminders"
        date = "2026-09-04"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="init_sched")
        self.service.update_profile(
            user_id=user_id,
            constraints={"reminders_enabled": False},
            idempotency_key="disable-reminders",
        )

        events = self.service.get_schedule(user_id=user_id, date=date)
        self.assertEqual(len(events), 5)
        for e in events:
            self.assertFalse(e["eligible"])
            self.assertEqual(e["suppression_reason"], "reminders_disabled_by_user")

    def test_restricted_mode_suppresses_workout_reminder(self):
        """When user is in restricted mode, workout reminder is suppressed with safety_restricted_mode."""
        user_id = "u_restricted_user"
        date = "2026-09-04"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="init_sched")

        # Report acute red flag to trigger restricted mode
        self.service.complete_workout(
            user_id=user_id,
            date=date,
            discomfort_notes="胸痛且呼吸困难",
            idempotency_key="chest-pain-wo",
        )

        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertFalse(events["workout_pending"]["eligible"])
        self.assertEqual(events["workout_pending"]["suppression_reason"], "safety_restricted_mode")

    def test_mcp_stdio_schedule_sync_lifecycle(self):
        """End-to-end stdio JSON-RPC test simulating host schedule sync: pull -> postpone -> repull -> cancel -> tombstone."""
        import asyncio
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async def _run():
            py_bin = sys.executable
            server_params = StdioServerParameters(
                command=py_bin,
                args=["-m", "cyber_health_mcp", "--allow-all"],
                env=dict(os.environ, CYBER_HEALTH_DB=self.db_path),
            )
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    user_id = "u_stdio_sync"
                    date = "2026-09-04"

                    # 1. Generate daily reminders
                    gen_res = await session.call_tool(
                        "cyber_health_schedule_daily_reminders",
                        {"user_id": user_id, "date": date, "idempotency_key": "stdio-sched-gen"},
                    )
                    gen_data = json.loads(gen_res.content[0].text)
                    self.assertEqual(gen_data["status"], "success")
                    self.assertEqual(len(gen_data["data"]["scheduled_events"]), 5)

                    # 2. Pull schedule
                    pull_res = await session.call_tool(
                        "cyber_health_get_schedule",
                        {"user_id": user_id, "date": date},
                    )
                    pull_data = json.loads(pull_res.content[0].text)
                    self.assertEqual(len(pull_data["events"]), 5)
                    self.assertTrue(all(e["eligible"] for e in pull_data["events"]))

                    # 3. Postpone lunch event
                    lunch_id = f"sched_{user_id}_{date}_meal_check_lunch"
                    postpone_res = await session.call_tool(
                        "cyber_health_update_schedule_event",
                        {
                            "user_id": user_id,
                            "event_id": lunch_id,
                            "action": "postponed",
                            "new_window_start": "2026-09-04T14:00:00+08:00",
                            "new_window_end": "2026-09-04T15:00:00+08:00",
                            "idempotency_key": "stdio-postpone-lunch",
                        },
                    )
                    postpone_data = json.loads(postpone_res.content[0].text)
                    self.assertEqual(postpone_data["status"], "success")
                    self.assertEqual(postpone_data["data"]["revision"], 2)

                    # 4. Cancel workout reminder
                    wo_id = f"sched_{user_id}_{date}_workout_reminder"
                    cancel_res = await session.call_tool(
                        "cyber_health_update_schedule_event",
                        {
                            "user_id": user_id,
                            "event_id": wo_id,
                            "action": "cancelled",
                            "idempotency_key": "stdio-cancel-wo",
                        },
                    )
                    cancel_data = json.loads(cancel_res.content[0].text)
                    self.assertEqual(cancel_data["status"], "success")
                    self.assertEqual(cancel_data["data"]["status"], "cancelled")

                    # 5. Default pull excludes cancelled workout
                    pull_active = await session.call_tool(
                        "cyber_health_get_schedule",
                        {"user_id": user_id, "date": date},
                    )
                    active_data = json.loads(pull_active.content[0].text)
                    self.assertEqual(len(active_data["events"]), 4)

                    # 6. Snapshot pull with include_inactive=True returns tombstone for timer revocation
                    pull_full = await session.call_tool(
                        "cyber_health_get_schedule",
                        {"user_id": user_id, "date": date, "include_inactive": True},
                    )
                    full_data = json.loads(pull_full.content[0].text)
                    self.assertEqual(len(full_data["events"]), 5)
                    wo_event = next(e for e in full_data["events"] if e["event_id"] == wo_id)
                    self.assertEqual(wo_event["status"], "cancelled")
                    self.assertFalse(wo_event["eligible"])
                    self.assertEqual(wo_event["suppression_reason"], "event_cancelled")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
