import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from typing import Any

from cyber_health.memory import MemoryUnavailable
from cyber_health.service import CyberHealthService
from cyber_health.store import SQLiteStore

UTC = timezone.utc


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


class TestCodexReviewRound13(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_round13.db")
        self.store = SQLiteStore(self.db_path)
        self.memory_provider = MockMemoryProvider()
        self.service = CyberHealthService(self.store, memory_provider=self.memory_provider)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_partial_workout_does_not_suppress_schedule_reminder(self):
        """Partial workout (e.g. completion_rate=0.2) must NOT suppress workout_reminder in get_schedule."""
        user_id = "u_partial_user"
        date = "2026-09-05"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="sched-init")

        # Complete a partial workout (completion_rate = 0.2)
        self.service.complete_workout(
            user_id=user_id,
            date=date,
            completed_exercises=[{"name": "深蹲", "sets": 1, "reps": 5}],
            completion_rate=0.2,
            idempotency_key="wo-partial",
        )

        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertIn("workout_pending", events)
        self.assertTrue(events["workout_pending"]["eligible"], "Partial workout must not suppress reminder")
        self.assertIsNone(events["workout_pending"]["suppression_reason"])

    def test_missing_completion_rate_does_not_suppress_schedule_reminder(self):
        """Workout without explicit completion_rate (None) must NOT suppress workout_reminder."""
        user_id = "u_nocr_user"
        date = "2026-09-05"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="sched-init")

        self.service.complete_workout(
            user_id=user_id,
            date=date,
            completed_exercises=[{"name": "慢跑", "duration_min": 10}],
            completion_rate=None,
            idempotency_key="wo-nocr",
        )

        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertIn("workout_pending", events)
        self.assertTrue(events["workout_pending"]["eligible"], "Missing completion_rate must not suppress reminder")
        self.assertIsNone(events["workout_pending"]["suppression_reason"])

    def test_full_workout_suppresses_schedule_reminder(self):
        """Full workout (completion_rate >= 1.0) must suppress workout_reminder."""
        user_id = "u_full_user"
        date = "2026-09-05"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="sched-init")

        self.service.complete_workout(
            user_id=user_id,
            date=date,
            completed_exercises=[{"name": "深蹲", "sets": 4, "reps": 8}],
            completion_rate=1.0,
            idempotency_key="wo-full",
        )

        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertIn("workout_pending", events)
        self.assertFalse(events["workout_pending"]["eligible"])
        self.assertEqual(events["workout_pending"]["suppression_reason"], "workout_already_completed")

    def test_superseded_plan_commit_ignored_when_latest_draft_exists(self):
        """When an earlier committed plan is superseded by a subsequent draft, morning_plan reminder is NOT suppressed."""
        user_id = "u_plan_user"
        date = "2026-09-05"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="sched-init")

        # 1. Commit plan for today -> morning_plan suppressed
        self.service.plan_tomorrow(
            user_id=user_id,
            date=date,
            commit=True,
            idempotency_key="plan-commit-1",
        )
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertFalse(events["morning_plan_not_locked"]["eligible"])
        self.assertEqual(events["morning_plan_not_locked"]["suppression_reason"], "morning_plan_already_committed")

        # 2. A new revision is generated in draft status, superseding the committed plan
        self.service.plan_tomorrow(
            user_id=user_id,
            date=date,
            commit=False,
            idempotency_key="plan-draft-2",
        )

        # 3. get_schedule must inspect only non-superseded plan: latest is draft, so reminder is eligible!
        events = {e["trigger_condition"]: e for e in self.service.get_schedule(user_id=user_id, date=date)}
        self.assertTrue(events["morning_plan_not_locked"]["eligible"], "Latest draft plan must keep reminder eligible")
        self.assertIsNone(events["morning_plan_not_locked"]["suppression_reason"])

    def test_get_schedule_pure_read_snapshot_isolation(self):
        """get_schedule runs in an isolated read snapshot and executes strictly ZERO mutations."""
        user_id = "u_purity_user"
        date = "2026-09-05"
        self.service.schedule_daily_reminders(user_id=user_id, date=date, idempotency_key="sched-purity")

        # Snapshot table counts before
        with self.store.connect() as conn:
            sched_cnt = conn.execute("SELECT COUNT(*) AS c FROM schedule_event").fetchone()["c"]
            prof_cnt = conn.execute("SELECT COUNT(*) AS c FROM user_profile").fetchone()["c"]
            rec_cnt = conn.execute("SELECT COUNT(*) AS c FROM domain_record").fetchone()["c"]
            op_cnt = conn.execute("SELECT COUNT(*) AS c FROM operation_log").fetchone()["c"]
            outbox_cnt = conn.execute("SELECT COUNT(*) AS c FROM memory_outbox").fetchone()["c"]

        # Call get_schedule with overdue time window
        future_now = datetime(2026, 9, 6, 23, 59, tzinfo=UTC)
        events = self.service.get_schedule(user_id=user_id, date=date, now=future_now)
        self.assertTrue(len(events) > 0)
        self.assertTrue(all(e["status"] == "overdue" for e in events))

        # Snapshot table counts after: strictly ZERO mutations
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM schedule_event").fetchone()["c"], sched_cnt)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM user_profile").fetchone()["c"], prof_cnt)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM domain_record").fetchone()["c"], rec_cnt)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM operation_log").fetchone()["c"], op_cnt)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS c FROM memory_outbox").fetchone()["c"], outbox_cnt)

    def test_daily_review_and_maintenance_hints_without_fake_rule_candidate(self):
        """daily_review does NOT insert fake memory rule candidates; maintenance hints reflect genuine due work."""
        user_id = "u_rev_user"
        date = "2026-09-05"

        # Log a meal and metric so review has content
        self.service.log_meal(
            user_id=user_id,
            occurred_at=f"{date}T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "鸡肉饭", "amount_g": {"low": 300, "high": 300}}],
            kcal_low=400,
            kcal_high=500,
            protein_low=30,
            protein_high=35,
            idempotency_key="lunch-1",
        )

        res = self.service.daily_review(
            user_id=user_id,
            date=date,
            idempotency_key="rev-k1",
        )

        # 1. Without due work, daily_review reports maintenance_recommended = False
        self.assertFalse(res["data"]["maintenance_recommended"])
        self.assertIsNone(res["data"]["suggested_action"])
        self.assertIsNone(res["data"]["maintenance_key"])

        # 2. Daily review strictly did NOT insert any fake rule candidate into memory_outbox
        with self.store.connect() as conn:
            outbox_cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = ?",
                (user_id,),
            ).fetchone()["c"]
            self.assertEqual(outbox_cnt, 0, "daily_review must not pollute memory_outbox with fake rule candidates")

        # 3. Propose a legitimate memory candidate while provider is offline so it defers to outbox
        self.memory_provider.enabled = False
        self.service.propose_memory_candidate(
            user_id=user_id,
            method="memory.propose",
            payload={"rule": "high_protein_preference"},
            idempotency_key="cand-prop-1",
        )
        self.memory_provider.enabled = True

        # 4. Now get_today accurately recommends maintenance with generation key
        today_res = self.service.get_today(user_id=user_id, day=date)
        self.assertTrue(today_res["data"]["maintenance_recommended"])
        self.assertEqual(today_res["data"]["suggested_action"], "cyber_health_maintain_memory")
        self.assertTrue(today_res["data"]["maintenance_key"].startswith(f"maint_{user_id}_{date}_g"))
        self.assertEqual(today_res["data"]["maintenance"]["pending_outbox_count"], 1)

    def test_scoped_maintenance_host_drain_and_idempotency(self):
        """Host executes maintain_memory to drain pending outbox; subsequent reads clear hint."""
        user_id = "u_drain_user"
        date = "2026-09-05"

        # Legitimate memory candidate proposition while provider is offline so it defers
        self.memory_provider.enabled = False
        self.service.propose_memory_candidate(
            user_id=user_id,
            method="memory.propose",
            payload={"rule": "maintain_test"},
            idempotency_key="cand-drain-1",
        )
        self.memory_provider.enabled = True

        # Verify get_today recommends maintenance with generation key
        t1 = self.service.get_today(user_id=user_id, day=date)
        self.assertTrue(t1["data"]["maintenance_recommended"])
        maint_key = t1["data"]["maintenance_key"]
        self.assertTrue(maint_key.startswith(f"maint_{user_id}_{date}_g"))

        # Host maintenance cycle using the recommended maintenance_key
        maint_res = self.service.maintain_memory(user_id=user_id, idempotency_key=maint_key)
        self.assertEqual(maint_res["status"], "success")

        # Outbox item is now sent
        with self.store.connect() as conn:
            outbox_row = conn.execute(
                "SELECT status FROM memory_outbox WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            self.assertEqual(outbox_row["status"], "sent")

        # Subsequent get_today reports recommended = False
        t2 = self.service.get_today(user_id=user_id, day=date)
        self.assertFalse(t2["data"]["maintenance_recommended"])
        self.assertEqual(t2["data"]["maintenance"]["pending_outbox_count"], 0)
        self.assertIsNone(t2["data"]["suggested_action"])

        # Host re-invoking with same idempotency key returns exact cached response
        maint_res_replay = self.service.maintain_memory(user_id=user_id, idempotency_key=maint_key)
        self.assertEqual(maint_res["operation_id"], maint_res_replay["operation_id"])

        # Host re-invoking with new key is idempotent no-op
        maint_res_noop = self.service.maintain_memory(user_id=user_id, idempotency_key="host-maint-2")
        self.assertEqual(maint_res_noop["status"], "success")

    def test_scoped_maintenance_provider_failure_non_blocking(self):
        """When MemoryProvider fails, maintain_memory defers safely without blocking facts operations."""
        user_id = "u_fail_user"
        date = "2026-09-05"

        failing_svc = CyberHealthService(self.store, memory_provider=FailingMemoryProvider())
        failing_svc.propose_memory_candidate(
            user_id=user_id,
            method="memory.propose",
            payload={"rule": "fail_test"},
            idempotency_key="cand-fail-1",
        )

        # Host executes maintain_memory while provider is failing
        maint_res = failing_svc.maintain_memory(user_id=user_id, idempotency_key="host-maint-fail")
        self.assertIn(maint_res["status"], ("success", "partial"))

        # Outbox item remains pending with attempts incremented
        with self.store.connect() as conn:
            outbox_row = conn.execute(
                "SELECT status, attempts FROM memory_outbox WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            self.assertEqual(outbox_row["status"], "pending")
            self.assertGreaterEqual(outbox_row["attempts"], 1)

        # Core facts operations continue functioning normally
        meal_res = failing_svc.log_meal(
            user_id=user_id,
            occurred_at=f"{date}T18:00:00+08:00",
            meal_type="dinner",
            foods=[{"name": "牛肉", "amount_g": {"low": 150, "high": 150}}],
            kcal_low=300,
            kcal_high=350,
            idempotency_key="meal-nonblocking",
        )
        self.assertEqual(meal_res["status"], "success")

        today_res = failing_svc.get_today(user_id=user_id, day=date)
        self.assertEqual(today_res["status"], "success")
        self.assertTrue(today_res["data"]["maintenance_recommended"])

    def test_stdio_mcp_scoped_maintenance_simulation(self):
        """Simulate MCP client discovering historical TTL meal -> observing hint -> executing maintain_memory -> clearing hint."""
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async def _run():
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            py_bin = os.path.join(root, ".venv", "bin", "python")
            server_params = StdioServerParameters(
                command=py_bin,
                args=["-m", "cyber_health_mcp", "--allow-all"],
                env=dict(os.environ, CYBER_HEALTH_DB=self.db_path, CYBER_HEALTH_MOCK_MEMORY="1"),
            )
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    user_id = "u_stdio_maint"
                    date = "2026-09-05"

                    # 1. Log historical meal older than 30 days requiring TTL compaction
                    meal_res = await session.call_tool(
                        "cyber_health_log_meal",
                        {
                            "user_id": user_id,
                            "occurred_at": "2026-07-20T12:00:00+08:00",
                            "meal_type": "lunch",
                            "foods": [{"name": "沙拉", "amount_g": {"low": 200, "high": 200}}],
                            "kcal_low": 200,
                            "kcal_high": 250,
                            "protein_low": 10,
                            "protein_high": 15,
                            "idempotency_key": "stdio-hist-meal-1",
                        },
                    )
                    meal_data = json.loads(meal_res.content[0].text)
                    self.assertEqual(meal_data["status"], "success")

                    # 2. Query get_today: observes maintenance hint with generation key
                    today_res = await session.call_tool(
                        "cyber_health_get_today",
                        {"user_id": user_id, "date": date},
                    )
                    today_data = json.loads(today_res.content[0].text)
                    self.assertTrue(today_data["data"]["maintenance_recommended"])
                    self.assertGreaterEqual(today_data["data"]["maintenance"]["ttl_meals_count"], 1)
                    maint_key = today_data["data"]["maintenance_key"]
                    self.assertTrue(maint_key.startswith(f"maint_{user_id}_{date}_g"))

                    # 3. Host executes maintain_memory with generation key
                    maint_res = await session.call_tool(
                        "cyber_health_maintain_memory",
                        {"user_id": user_id, "idempotency_key": maint_key},
                    )
                    maint_data = json.loads(maint_res.content[0].text)
                    self.assertEqual(maint_data["status"], "success")

                    # 4. Query get_today again: hint cleared
                    today_after = await session.call_tool(
                        "cyber_health_get_today",
                        {"user_id": user_id, "date": date},
                    )
                    after_data = json.loads(today_after.content[0].text)
                    self.assertFalse(after_data["data"]["maintenance_recommended"])
                    self.assertEqual(after_data["data"]["maintenance"]["ttl_meals_count"], 0)

        asyncio.run(_run())

    def test_stdio_mcp_unenabled_host_non_blocking_simulation(self):
        """Simulate real host unenabled state: maintain_memory returns partial/MEMORY_DEFERRED without blocking facts."""
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async def _run():
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            py_bin = os.path.join(root, ".venv", "bin", "python")
            # Without CYBER_HEALTH_MOCK_MEMORY: tests true unenabled host state
            server_params = StdioServerParameters(
                command=py_bin,
                args=["-m", "cyber_health_mcp", "--allow-all"],
                env=dict(os.environ, CYBER_HEALTH_DB=self.db_path),
            )
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    user_id = "u_stdio_unenabled"
                    date = "2026-09-05"

                    # 1. Propose candidate
                    cand_res = await session.call_tool(
                        "cyber_health_memory_action",
                        {
                            "user_id": user_id,
                            "action_type": "propose",
                            "payload": {"rule": "unenabled_host_test"},
                            "idempotency_key": "stdio-unenabled-cand",
                        },
                    )
                    cand_data = json.loads(cand_res.content[0].text)
                    self.assertIn(cand_data["status"], ("success", "partial"))

                    # 2. Host calls maintain_memory -> returns partial / MEMORY_DEFERRED safely
                    maint_res = await session.call_tool(
                        "cyber_health_maintain_memory",
                        {"user_id": user_id, "idempotency_key": "stdio-maint-unenabled"},
                    )
                    maint_data = json.loads(maint_res.content[0].text)
                    self.assertEqual(maint_data["status"], "partial")
                    self.assertTrue(any("MEMORY_DEFERRED" in w for w in maint_data.get("warnings", [])))

                    # 3. Facts queries remain completely unblocked
                    today_res = await session.call_tool(
                        "cyber_health_get_today",
                        {"user_id": user_id, "date": date},
                    )
                    today_data = json.loads(today_res.content[0].text)
                    self.assertEqual(today_data["status"], "success")
                    # Memory intent remains pending, so maintenance remains recommended
                    self.assertTrue(today_data["data"]["maintenance_recommended"])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
