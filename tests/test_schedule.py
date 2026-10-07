"""Daily reminder windows, eligibility suppression, lifecycle and read purity."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cyber_health import CyberHealthService
from cyber_health.memory import MemoryUnavailable
from cyber_health.store import SINGLE_USER_ID, SQLiteStore
from test_support import OWNER, fixed_clock


class MockMemoryProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.enabled: bool = True

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise MemoryUnavailable("Obsidian remote adapter offline")
        self.calls.append((method, payload))
        return {"status": "ok", "candidate_id": f"cand_{len(self.calls)}"}


class FailingMemoryProvider:
    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise MemoryUnavailable("Obsidian remote adapter connection timeout")


class MockWorkingMemoryProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        return {"status": "ok", "candidate_id": f"cand_{len(self.calls)}"}


class ScheduleSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_schedule.db")
        self.store = SQLiteStore(self.db_path)
        self.service = CyberHealthService(self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_five_standard_windows_generated_with_stable_ids(self):
        """Standard schedule generates 5 windows: morning, lunch, workout, dinner, review."""
        date = "2026-09-04"
        res1 = self.service.schedule_daily_reminders(date=date, idempotency_key="sched-k1")
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
        res2 = self.service.schedule_daily_reminders(date=date, idempotency_key="sched-k2")
        events2 = res2["data"]["scheduled_events"]
        self.assertEqual([e["event_id"] for e in events1], [e["event_id"] for e in events2])

    def test_postponement_preserved_on_rescheduling(self):
        """User-postponed events retain their new window and revision > 1, never overridden by defaults."""
        date = "2026-09-04"
        self.service.schedule_daily_reminders(date=date, idempotency_key="init_sched")

        lunch_id = f"sched_{OWNER}_{date}_meal_check_lunch"
        new_start = "2026-09-04T14:30:00+08:00"
        new_end = "2026-09-04T15:30:00+08:00"

        # Postpone lunch
        postpone_res = self.service.update_schedule_event(
            event_id=lunch_id,
            action="postponed",
            new_window_start=new_start,
            new_window_end=new_end,
            idempotency_key="postpone-lunch",
        )
        self.assertEqual(postpone_res["data"]["status"], "pending")
        self.assertEqual(postpone_res["data"]["revision"], 2)

        # Re-run schedule_daily_reminders (e.g. host daily sync)
        res_resched = self.service.schedule_daily_reminders(date=date, idempotency_key="resched-lunch")
        lunch_event = next(e for e in res_resched["data"]["scheduled_events"] if e["event_id"] == lunch_id)

        # Must preserve postponed window and revision
        self.assertEqual(lunch_event["window_start"], new_start)
        self.assertEqual(lunch_event["window_end"], new_end)
        self.assertEqual(lunch_event["revision"], 2)
        self.assertEqual(lunch_event["status"], "pending")

    def test_tombstone_snapshot_for_host_timer_cancellation(self):
        """Cancelled and skipped events are hidden by default, but exposed as tombstones when include_inactive=True."""
        date = "2026-09-04"
        self.service.schedule_daily_reminders(date=date, idempotency_key="init_sched")

        wo_id = f"sched_{OWNER}_{date}_workout_reminder"
        dinner_id = f"sched_{OWNER}_{date}_meal_check_dinner"

        # Cancel workout, skip dinner
        self.service.update_schedule_event(
            event_id=wo_id,
            action="cancelled",
            idempotency_key="cancel-wo",
        )
        self.service.update_schedule_event(
            event_id=dinner_id,
            action="skipped",
            idempotency_key="skip-dinner",
        )

        # Default query: only active pending/overdue
        active_events = self.service.get_schedule(date=date, include_inactive=False)
        self.assertEqual(len(active_events), 3)
        self.assertNotIn(wo_id, [e["event_id"] for e in active_events])
        self.assertNotIn(dinner_id, [e["event_id"] for e in active_events])

        # Full snapshot: include_inactive=True returns tombstones
        full_events = self.service.get_schedule(date=date, include_inactive=True)
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
        date = "2026-09-04"
        self.service.schedule_daily_reminders(date=date, idempotency_key="init_sched")

        prof_before = self.service.get_profile()
        version_before = prof_before["state_version"]

        with self.store.connect() as conn:
            op_count_before = conn.execute("SELECT COUNT(*) AS c FROM operation_log WHERE user_id = ?", (OWNER,)).fetchone()["c"]

        # Call get_schedule at 23:00 (all 5 windows have elapsed)
        now_late = datetime(2026, 9, 4, 23, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        events = self.service.get_schedule(date=date, now=now_late)

        # All events should be derived as overdue with compensation required
        self.assertEqual(len(events), 5)
        for e in events:
            self.assertEqual(e["status"], "overdue")
            self.assertTrue(e["compensation_required"])

        # Profile state version must be strictly untouched
        prof_after = self.service.get_profile()
        self.assertEqual(prof_after["state_version"], version_before)

        # Operation log must have ZERO new operations
        with self.store.connect() as conn:
            op_count_after = conn.execute("SELECT COUNT(*) AS c FROM operation_log WHERE user_id = ?", (OWNER,)).fetchone()["c"]
            self.assertEqual(op_count_after, op_count_before)

            # In SQLite, schedule_event rows must still remain pending (not sneaky unversioned UPDATEs)
            raw_statuses = [r["status"] for r in conn.execute("SELECT status FROM schedule_event WHERE user_id = ?", (OWNER,)).fetchall()]
            self.assertTrue(all(s == "pending" for s in raw_statuses))

    def test_dynamic_eligibility_suppression_rules(self):
        """Test trigger eligibility suppression by domain facts."""
        date = "2026-09-04"
        self.service.schedule_daily_reminders(date=date, idempotency_key="init_sched")

        # 1. Initially all 5 are eligible
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertTrue(events["morning_plan_not_locked"]["eligible"])
        self.assertTrue(events["lunch_not_logged"]["eligible"])
        self.assertTrue(events["workout_pending"]["eligible"])
        self.assertTrue(events["dinner_not_logged"]["eligible"])
        self.assertTrue(events["review_pending"]["eligible"])

        # 2. Lock morning plan -> morning_plan suppressed
        self.service.plan_tomorrow(date=date, commit=True, idempotency_key="commit-plan")
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertFalse(events["morning_plan_not_locked"]["eligible"])
        self.assertEqual(events["morning_plan_not_locked"]["suppression_reason"], "morning_plan_already_committed")

        # 3. Log lunch -> lunch_check suppressed
        self.service.log_meal(
            occurred_at=f"{date}T12:30:00+08:00",
            meal_type="lunch",
            foods=[{"name": "米饭", "amount_g": {"low": 150, "high": 150}}],
            kcal_low=300,
            kcal_high=350,
            idempotency_key="log-lunch",
        )
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertFalse(events["lunch_not_logged"]["eligible"])
        self.assertEqual(events["lunch_not_logged"]["suppression_reason"], "lunch_already_logged")
        # Dinner remains eligible
        self.assertTrue(events["dinner_not_logged"]["eligible"])

        # 4. Complete workout -> workout_reminder suppressed
        self.service.complete_workout(
            date=date,
            completed_exercises=[{"name": "深蹲", "sets": 3, "reps": 8}],
            completion_rate=1.0,
            idempotency_key="comp-wo",
        )
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertFalse(events["workout_pending"]["eligible"])
        self.assertEqual(events["workout_pending"]["suppression_reason"], "workout_already_completed")

        # 5. Log dinner -> dinner_check suppressed
        self.service.log_meal(
            occurred_at=f"{date}T19:00:00+08:00",
            meal_type="dinner",
            foods=[{"name": "鸡胸肉沙拉", "amount_g": {"low": 200, "high": 200}}],
            kcal_low=250,
            kcal_high=300,
            idempotency_key="log-dinner",
        )
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertFalse(events["dinner_not_logged"]["eligible"])
        self.assertEqual(events["dinner_not_logged"]["suppression_reason"], "dinner_already_logged")

        # 6. Complete daily review -> review suppressed
        self.service.daily_review(date=date, idempotency_key="rev-today")
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertFalse(events["review_pending"]["eligible"])
        self.assertEqual(events["review_pending"]["suppression_reason"], "daily_review_already_completed")

    def test_profile_reminders_disabled_suppresses_all(self):
        """When user disables reminders in profile, all schedule events are suppressed."""
        date = "2026-09-04"
        self.service.schedule_daily_reminders(date=date, idempotency_key="init_sched")
        self.service.update_profile(
            constraints={"reminders_enabled": False},
            idempotency_key="disable-reminders",
        )

        events = self.service.get_schedule(date=date)
        self.assertEqual(len(events), 5)
        for e in events:
            self.assertFalse(e["eligible"])
            self.assertEqual(e["suppression_reason"], "reminders_disabled_by_user")

    def test_restricted_mode_suppresses_workout_reminder(self):
        """When user is in restricted mode, workout reminder is suppressed with safety_restricted_mode."""
        date = "2026-09-04"
        self.service.schedule_daily_reminders(date=date, idempotency_key="init_sched")

        # Report acute red flag to trigger restricted mode
        self.service.complete_workout(
            date=date,
            discomfort_notes="胸痛且呼吸困难",
            idempotency_key="chest-pain-wo",
        )

        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertFalse(events["workout_pending"]["eligible"])
        self.assertEqual(events["workout_pending"]["suppression_reason"], "safety_restricted_mode")

    def test_mcp_stdio_schedule_sync_lifecycle(self):
        """End-to-end stdio JSON-RPC test simulating host schedule sync: pull -> postpone -> repull -> cancel -> tombstone."""

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
                    date = "2026-09-04"

                    # 1. Generate daily reminders
                    gen_res = await session.call_tool(
                        "cyber_health_schedule_daily_reminders",
                        {"date": date, "idempotency_key": "stdio-sched-gen"},
                    )
                    gen_data = json.loads(gen_res.content[0].text)
                    self.assertEqual(gen_data["status"], "success")
                    self.assertEqual(len(gen_data["data"]["scheduled_events"]), 5)

                    # 2. Pull schedule
                    pull_res = await session.call_tool(
                        "cyber_health_get_schedule",
                        {"date": date},
                    )
                    pull_data = json.loads(pull_res.content[0].text)
                    self.assertEqual(len(pull_data["events"]), 5)
                    self.assertTrue(all(e["eligible"] for e in pull_data["events"]))

                    # 3. Postpone lunch event
                    lunch_id = f"sched_{OWNER}_{date}_meal_check_lunch"
                    postpone_res = await session.call_tool(
                        "cyber_health_update_schedule_event",
                        {
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
                    wo_id = f"sched_{OWNER}_{date}_workout_reminder"
                    cancel_res = await session.call_tool(
                        "cyber_health_update_schedule_event",
                        {
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
                        {"date": date},
                    )
                    active_data = json.loads(pull_active.content[0].text)
                    self.assertEqual(len(active_data["events"]), 4)

                    # 6. Snapshot pull with include_inactive=True returns tombstone for timer revocation
                    pull_full = await session.call_tool(
                        "cyber_health_get_schedule",
                        {"date": date, "include_inactive": True},
                    )
                    full_data = json.loads(pull_full.content[0].text)
                    self.assertEqual(len(full_data["events"]), 5)
                    wo_event = next(e for e in full_data["events"] if e["event_id"] == wo_id)
                    self.assertEqual(wo_event["status"], "cancelled")
                    self.assertFalse(wo_event["eligible"])
                    self.assertEqual(wo_event["suppression_reason"], "event_cancelled")

        asyncio.run(_run())


class ReminderSuppressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_round13.db")
        self.store = SQLiteStore(self.db_path)
        self.memory_provider = MockMemoryProvider()
        self.service = CyberHealthService(self.store, memory_provider=self.memory_provider, clock=fixed_clock())

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_partial_workout_does_not_suppress_schedule_reminder(self):
        """Partial workout (e.g. completion_rate=0.2) must NOT suppress workout_reminder in get_schedule."""
        date = "2026-09-05"
        self.service.schedule_daily_reminders(date=date, idempotency_key="sched-init")

        # Complete a partial workout (completion_rate = 0.2)
        self.service.complete_workout(
            date=date,
            completed_exercises=[{"name": "深蹲", "sets": 1, "reps": 5}],
            completion_rate=0.2,
            idempotency_key="wo-partial",
        )

        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertIn("workout_pending", events)
        self.assertTrue(events["workout_pending"]["eligible"], "Partial workout must not suppress reminder")
        self.assertIsNone(events["workout_pending"]["suppression_reason"])

    def test_missing_completion_rate_does_not_suppress_schedule_reminder(self):
        """Workout without explicit completion_rate (None) must NOT suppress workout_reminder."""
        date = "2026-09-05"
        self.service.schedule_daily_reminders(date=date, idempotency_key="sched-init")

        self.service.complete_workout(
            date=date,
            completed_exercises=[{"name": "慢跑", "duration_min": 10}],
            completion_rate=None,
            idempotency_key="wo-nocr",
        )

        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertIn("workout_pending", events)
        self.assertTrue(events["workout_pending"]["eligible"], "Missing completion_rate must not suppress reminder")
        self.assertIsNone(events["workout_pending"]["suppression_reason"])

    def test_full_workout_suppresses_schedule_reminder(self):
        """Full workout (completion_rate >= 1.0) must suppress workout_reminder."""
        date = "2026-09-05"
        self.service.schedule_daily_reminders(date=date, idempotency_key="sched-init")

        self.service.complete_workout(
            date=date,
            completed_exercises=[{"name": "深蹲", "sets": 4, "reps": 8}],
            completion_rate=1.0,
            idempotency_key="wo-full",
        )

        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertIn("workout_pending", events)
        self.assertFalse(events["workout_pending"]["eligible"])
        self.assertEqual(events["workout_pending"]["suppression_reason"], "workout_already_completed")

    def test_superseded_plan_commit_ignored_when_latest_draft_exists(self):
        """When an earlier committed plan is superseded by a subsequent draft, morning_plan reminder is NOT suppressed."""
        date = "2026-09-05"
        self.service.schedule_daily_reminders(date=date, idempotency_key="sched-init")

        # 1. Commit plan for today -> morning_plan suppressed
        self.service.plan_tomorrow(
            date=date,
            commit=True,
            idempotency_key="plan-commit-1",
        )
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertFalse(events["morning_plan_not_locked"]["eligible"])
        self.assertEqual(events["morning_plan_not_locked"]["suppression_reason"], "morning_plan_already_committed")

        # 2. A new revision is generated in draft status, superseding the committed plan
        self.service.plan_tomorrow(
            date=date,
            commit=False,
            idempotency_key="plan-draft-2",
        )

        # 3. get_schedule must inspect only non-superseded plan: latest is draft, so reminder is eligible!
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(date=date)}
        self.assertTrue(events["morning_plan_not_locked"]["eligible"], "Latest draft plan must keep reminder eligible")
        self.assertIsNone(events["morning_plan_not_locked"]["suppression_reason"])

    def test_get_schedule_pure_read_snapshot_isolation(self):
        """get_schedule runs in an isolated read snapshot and executes strictly ZERO mutations."""
        date = "2026-09-05"
        self.service.schedule_daily_reminders(date=date, idempotency_key="sched-purity")

        # Snapshot table counts before
        with self.store.connect() as conn:
            sched_cnt = conn.execute("SELECT COUNT(*) AS c FROM schedule_event").fetchone()["c"]
            prof_cnt = conn.execute("SELECT COUNT(*) AS c FROM user_profile").fetchone()["c"]
            rec_cnt = conn.execute("SELECT COUNT(*) AS c FROM domain_record").fetchone()["c"]
            op_cnt = conn.execute("SELECT COUNT(*) AS c FROM operation_log").fetchone()["c"]
            outbox_cnt = conn.execute("SELECT COUNT(*) AS c FROM memory_outbox").fetchone()["c"]

        # Call get_schedule with overdue time window
        future_now = datetime(2026, 9, 6, 23, 59, tzinfo=UTC)
        events = self.service.get_schedule(date=date, now=future_now)
        self.assertTrue(len(events) > 0)
        self.assertTrue(all(e["status"] == "overdue" for e in events))

        # Snapshot table counts after: strictly ZERO mutations
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM schedule_event").fetchone()["c"], sched_cnt)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM user_profile").fetchone()["c"], prof_cnt)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM domain_record").fetchone()["c"], rec_cnt)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM operation_log").fetchone()["c"], op_cnt)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM memory_outbox").fetchone()["c"], outbox_cnt)


class ScheduleIdentityAndTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")

    def test_schedule_ids_stable_across_distinct_requests(self):
        first = self.service.schedule_daily_reminders(date="2026-09-04", idempotency_key="one")
        second = self.service.schedule_daily_reminders(date="2026-09-04", idempotency_key="two")
        def ids(r):
            return {e["event_id"] for e in r["data"]["scheduled_events"]}
        self.assertEqual(ids(first), ids(second))

    def test_schedule_respects_new_york_timezone(self):
        self.service.update_profile(timezone="America/New_York", idempotency_key="profile")
        result = self.service.schedule_daily_reminders(date="2026-09-04", idempotency_key="schedule")
        event = next(e for e in result["data"]["scheduled_events"] if e["event_type"] == "MORNING_PLAN")
        dt = datetime.fromisoformat(event["window_start"])
        self.assertEqual(dt.astimezone(ZoneInfo("America/New_York")).hour, 7)


class ScheduleInstantComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "review.sqlite3")

    def meal(self, key, timestamp, kcal=100):
        return self.service.log_meal(occurred_at=timestamp,
            meal_type="breakfast", foods=[], kcal_low=kcal, kcal_high=kcal + 10,
            idempotency_key=key)

    def test_schedule_compares_instants_not_iso_strings(self):
        with self.service.store.transaction() as conn:
            conn.execute("""INSERT INTO schedule_event(event_id, user_id, event_type,
                window_start, window_end, status, created_at, updated_at)
                VALUES ('e', 'owner', 'DAILY_REVIEW', '2026-09-04T08:00:00+08:00',
                '2026-09-04T09:00:00+08:00', 'pending', '2026-09-04', '2026-09-04')""")
        events = self.service.get_schedule(now=datetime(2026, 9, 4, 2, tzinfo=UTC))
        self.assertEqual(events[0]["status"], "overdue")


class OverdueCompensationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_advanced.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_schedule_lifecycle_and_overdue_compensation(self) -> None:
        """Expired schedule events transition to overdue with compensation_required=True."""
        now = datetime.now(UTC)
        past_end = (now - timedelta(minutes=15)).isoformat()
        past_start = (now - timedelta(minutes=45)).isoformat()

        # Seed a schedule event that is already past its window
        with self.service.store.transaction() as conn:
            conn.execute(
                """INSERT INTO schedule_event(
                    event_id, user_id, event_type, window_start, window_end, status,
                    revision, delivery_attempts, prompt_hint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 1, 0, 'Late night review', ?, ?)""",
                ("sched-001", "owner", "DAILY_REVIEW", past_start, past_end, past_start, past_start),
            )

        # Call get_schedule
        events = self.service.get_schedule(now=now)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], "sched-001")
        self.assertEqual(events[0]["status"], "overdue")
        self.assertTrue(events[0]["compensation_required"])

        # Acknowledge the overdue event
        ack = self.service.acknowledge_schedule_event(
            event_id="sched-001",
            action="acknowledged",
            idempotency_key="ack-sched-001",
        )
        self.assertEqual(ack["status"], "success")

        # Subsequent get_schedule should return no pending/overdue events
        events_after = self.service.get_schedule(now=now)
        self.assertEqual(len(events_after), 0)


class ScheduleEventLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_remaining.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_update_schedule_event_lifecycle(self) -> None:
        """Verify schedule event delivery, acknowledgement, postponement, and overdue compensation."""
        date = "2026-09-04"

        # Generate schedule
        sched_res = self.service.schedule_daily_reminders(
            date=date,
            idempotency_key="sched_gen_01",
        )
        events = sched_res["data"]["scheduled_events"]
        self.assertEqual(len(events), 5)
        target_event = events[0]
        ev_id = target_event["event_id"]

        # 1. Delivered
        d_res = self.service.update_schedule_event(
            event_id=ev_id,
            action="delivered",
            idempotency_key="ev_deliv_01",
        )
        self.assertEqual(d_res["data"]["status"], "delivered")
        self.assertEqual(d_res["data"]["delivery_attempts"], 1)

        # 2. Acknowledged
        a_res = self.service.update_schedule_event(
            event_id=ev_id,
            action="acknowledged",
            idempotency_key="ev_ack_01",
        )
        self.assertEqual(a_res["data"]["status"], "acknowledged")

        # 3. Postponed with new window
        p_res = self.service.update_schedule_event(
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
            event_id=ev_id,
            action="acknowledged",
            idempotency_key="ev_comp_01",
        )
        self.assertIsNotNone(comp_res["data"]["compensation"])
        self.assertEqual(comp_res["data"]["compensation"]["status"], "compensated")


class ScheduleRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cyber-health.sqlite3"
        self.store = SQLiteStore(self.db_path)
        self.service = CyberHealthService(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_get_schedule_rollback_preserves_root_exception(self) -> None:
        import json
        user_id = SINGLE_USER_ID
        self.service.update_profile(
            goals={"target_kcal_low": 2000, "target_kcal_high": 2500},
            idempotency_key="setup_profile_goals",
        )
        with self.store.connect() as conn:
            conn.execute("UPDATE user_profile SET goals_json = '{bad-json' WHERE user_id = ?", (user_id,))

        with self.assertRaises(json.JSONDecodeError):
            self.service.get_schedule(date="2026-09-20")


if __name__ == "__main__":
    unittest.main()
