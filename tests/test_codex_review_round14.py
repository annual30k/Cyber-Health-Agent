import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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


class TestCodexReviewRound14(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_round14.db")
        self.store = SQLiteStore(self.db_path)
        self.memory_provider = MockMemoryProvider()
        self.service = CyberHealthService(self.store, memory_provider=self.memory_provider)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unified_due_work_calculator_detects_ttl_meal_details_and_discloses_reasons(self):
        """Unified calculator detects historical meals older than 30 days needing compaction, with 0 outbox items."""
        user_id = "u_ttl_user"
        today_date = "2026-09-05"
        hist_occurred = "2026-07-20T12:00:00+08:00"

        # 1. Log a historical meal (>30 days ago) with food details
        self.service.log_meal(
            user_id=user_id,
            occurred_at=hist_occurred,
            meal_type="lunch",
            foods=[{"name": "沙拉", "amount_g": {"low": 200, "high": 200}}],
            kcal_low=250,
            kcal_high=300,
            protein_low=10,
            protein_high=15,
            idempotency_key="hist-meal-1",
        )

        # 2. Outbox is completely empty
        with self.store.connect() as conn:
            outbox_cnt = conn.execute("SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = ?", (user_id,)).fetchone()["c"]
            self.assertEqual(outbox_cnt, 0)

        # 3. get_today identifies TTL meal compaction due work
        t1 = self.service.get_today(user_id=user_id, day=today_date)
        self.assertTrue(t1["data"]["maintenance_recommended"])
        self.assertEqual(t1["data"]["suggested_action"], "cyber_health_maintain_memory")
        self.assertIn("ttl_meal_details_due", t1["data"]["maintenance_reason"])
        self.assertEqual(t1["data"]["maintenance"]["ttl_meals_count"], 1)
        self.assertEqual(t1["data"]["maintenance"]["pending_outbox_count"], 0)

        maint_key = t1["data"]["maintenance_key"]
        self.assertIsNotNone(maint_key)
        self.assertTrue(maint_key.startswith(f"maint_{user_id}_{today_date}_g"))

        # 4. Host executes maintain_memory with generation key
        maint_res = self.service.maintain_memory(user_id=user_id, idempotency_key=maint_key)
        self.assertEqual(maint_res["status"], "success")
        self.assertFalse(maint_res["data"]["has_more"])
        self.assertIsNone(maint_res["data"]["next_maintenance_key"])

        # 5. Verify meal foods_json was compacted to '[]' and weekly trend was consolidated
        with self.store.connect() as conn:
            meal_row = conn.execute("SELECT foods_json FROM meal_log WHERE user_id = ?", (user_id,)).fetchone()
            self.assertEqual(meal_row["foods_json"], "[]")
            trend_cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM domain_record WHERE user_id = ? AND kind = 'weekly_nutrition_trend'",
                (user_id,),
            ).fetchone()["c"]
            self.assertGreaterEqual(trend_cnt, 1)

        # 6. Subsequent get_today reports maintenance_recommended = False
        t2 = self.service.get_today(user_id=user_id, day=today_date)
        self.assertFalse(t2["data"]["maintenance_recommended"])
        self.assertIsNone(t2["data"]["maintenance_key"])
        self.assertIsNone(t2["data"]["maintenance_reason"])
        self.assertEqual(t2["data"]["maintenance"]["ttl_meals_count"], 0)

    def test_unified_due_work_calculator_detects_expired_in_flight_leases(self):
        """Unified calculator detects crashed worker expired leases and recovers them into sent."""
        user_id = "u_crash_user"
        today_date = "2026-09-05"

        # 1. Insert an outbox row in 'in_flight' status with lease_until in the past
        past_lease = "2026-09-01T00:00:00+00:00"
        intent_payload = {"rule": "crashed_worker_rule"}
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO memory_outbox(
                    intent_id, user_id, idempotency_key, request_hash, method, payload_json,
                    status, attempts, owner_token, lease_until, created_at, updated_at
                ) VALUES (?, ?, 'crashed-idem-1', 'reqhash-1', 'memory.propose', ?, 'in_flight', 0, 'worker_crashed', ?, ?, ?)""",
                (
                    f"intent_{user_id}_crash_1",
                    user_id,
                    json.dumps(intent_payload),
                    past_lease,
                    past_lease,
                    past_lease,
                ),
            )

        # 2. get_today discovers the expired lease
        t1 = self.service.get_today(user_id=user_id, day=today_date)
        self.assertTrue(t1["data"]["maintenance_recommended"])
        self.assertIn("expired_worker_leases", t1["data"]["maintenance_reason"])
        self.assertEqual(t1["data"]["maintenance"]["expired_lease_count"], 1)

        maint_key = t1["data"]["maintenance_key"]
        self.assertTrue(maint_key.startswith(f"maint_{user_id}_{today_date}_g"))

        # 3. Host calls maintain_memory -> recovers and marks sent
        maint_res = self.service.maintain_memory(user_id=user_id, idempotency_key=maint_key)
        self.assertEqual(maint_res["status"], "success")
        self.assertEqual(maint_res["data"]["sent_count"], 1)

        with self.store.connect() as conn:
            row = conn.execute("SELECT status FROM memory_outbox WHERE user_id = ?", (user_id,)).fetchone()
            self.assertEqual(row["status"], "sent")

        # 4. get_today now reports False
        t2 = self.service.get_today(user_id=user_id, day=today_date)
        self.assertFalse(t2["data"]["maintenance_recommended"])

    def test_repeated_get_today_idempotent_key_stability(self):
        """Repeated get_today calls produce identical maintenance_key; maintain_memory replays cached response."""
        user_id = "u_repeat_user"
        today_date = "2026-09-05"

        # Queue 2 items while provider is offline
        self.memory_provider.enabled = False
        for i in range(2):
            self.service.propose_memory_candidate(
                user_id=user_id,
                method="memory.propose",
                payload={"rule": f"rule_{i}"},
                idempotency_key=f"repeat-cand-{i}",
            )
        self.memory_provider.enabled = True

        # Call get_today 5 times: every call must produce the exact same maintenance_key
        keys = []
        for _ in range(5):
            res = self.service.get_today(user_id=user_id, day=today_date)
            self.assertTrue(res["data"]["maintenance_recommended"])
            keys.append(res["data"]["maintenance_key"])

        self.assertEqual(len(set(keys)), 1, "Repeated get_today without mutations must return identical key")
        stable_key = keys[0]

        # First maintain_memory call executes
        maint_1 = self.service.maintain_memory(user_id=user_id, idempotency_key=stable_key)
        self.assertEqual(maint_1["status"], "success")
        self.assertEqual(maint_1["data"]["sent_count"], 2)

        # Exact repeat call with same stable_key replays cached response
        maint_2 = self.service.maintain_memory(user_id=user_id, idempotency_key=stable_key)
        self.assertEqual(maint_1["operation_id"], maint_2["operation_id"])

    def test_fifty_one_plus_tasks_continuation_advances_without_idempotency_block(self):
        """When outbox has >50 items, pass 1 claims 50 (status partial) and provides next_maintenance_key to finish pass 2."""
        user_id = "u_batch_user"
        today_date = "2026-09-05"

        # Insert 55 pending outbox items directly
        now_iso = datetime.now(UTC).isoformat()
        with self.store.connect() as conn:
            for i in range(55):
                conn.execute(
                    """INSERT INTO memory_outbox(
                        intent_id, user_id, idempotency_key, request_hash, method, payload_json,
                        status, attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'memory.propose', '{"rule": "stress"}', 'pending', 0, ?, ?)""",
                    (f"intent_batch_{user_id}_{i:03d}", user_id, f"batch-idem-{i:03d}", f"hash-{i:03d}", now_iso, now_iso),
                )

        # Initial check
        t0 = self.service.get_today(user_id=user_id, day=today_date)
        self.assertTrue(t0["data"]["maintenance_recommended"])
        self.assertEqual(t0["data"]["maintenance"]["pending_outbox_count"], 55)
        key_pass1 = t0["data"]["maintenance_key"]

        # Pass 1: claims 50 items; 5 remain so status is partial and has_more is True
        maint_1 = self.service.maintain_memory(user_id=user_id, idempotency_key=key_pass1)
        self.assertEqual(maint_1["status"], "partial")
        self.assertEqual(maint_1["data"]["outbox_processed"], 50)
        self.assertEqual(maint_1["data"]["sent_count"], 50)
        self.assertTrue(maint_1["data"]["has_more"])
        key_pass2 = maint_1["data"]["next_maintenance_key"]

        # next_maintenance_key MUST be distinct from key_pass1 (new work generation!)
        self.assertIsNotNone(key_pass2)
        self.assertNotEqual(key_pass1, key_pass2)

        # Pass 2: host uses key_pass2 to claim the remaining 5 items without idempotency collision
        maint_2 = self.service.maintain_memory(user_id=user_id, idempotency_key=key_pass2)
        self.assertEqual(maint_2["status"], "success")
        self.assertEqual(maint_2["data"]["outbox_processed"], 5)
        self.assertEqual(maint_2["data"]["sent_count"], 5)
        self.assertFalse(maint_2["data"]["has_more"])
        self.assertIsNone(maint_2["data"]["next_maintenance_key"])

        # All 55 items are sent
        with self.store.connect() as conn:
            sent_cnt = conn.execute("SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = ? AND status = 'sent'", (user_id,)).fetchone()["c"]
            self.assertEqual(sent_cnt, 55)

        # get_today hint is cleared
        t_final = self.service.get_today(user_id=user_id, day=today_date)
        self.assertFalse(t_final["data"]["maintenance_recommended"])

    def test_partial_failure_followed_by_provider_recovery(self):
        """When Provider fails, status is partial, key advances for retry with backoff, and succeeds on recovery."""
        user_id = "u_retry_user"
        today_date = "2026-09-05"

        # Queue candidate while provider is failing
        failing_svc = CyberHealthService(self.store, memory_provider=FailingMemoryProvider())
        failing_svc.propose_memory_candidate(
            user_id=user_id,
            method="memory.propose",
            payload={"rule": "recovery_test"},
            idempotency_key="rec-cand-1",
        )

        t1 = failing_svc.get_today(user_id=user_id, day=today_date)
        key_fail_1 = t1["data"]["maintenance_key"]

        # Pass 1: maintain_memory while provider fails
        res1 = failing_svc.maintain_memory(user_id=user_id, idempotency_key=key_fail_1)
        self.assertEqual(res1["status"], "partial")
        self.assertEqual(res1["data"]["deferred_count"], 1)
        self.assertTrue(res1["data"]["has_more"])
        self.assertGreaterEqual(res1["data"]["retry_after_seconds"], 2)
        key_retry = res1["data"]["next_maintenance_key"]

        # Key advances because attempts incremented from 0 to 1
        self.assertIsNotNone(key_retry)
        self.assertNotEqual(key_fail_1, key_retry)

        # Immediate replay with old key returns cached partial without re-invoking failing provider
        res1_replay = failing_svc.maintain_memory(user_id=user_id, idempotency_key=key_fail_1)
        self.assertEqual(res1["operation_id"], res1_replay["operation_id"])

        # Provider recovers!
        failing_svc.memory_provider = MockMemoryProvider()

        # Pass 2: host calls with key_retry
        res2 = failing_svc.maintain_memory(user_id=user_id, idempotency_key=key_retry)
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["data"]["sent_count"], 1)
        self.assertFalse(res2["data"]["has_more"])

        # Outbox item is now sent
        with self.store.connect() as conn:
            row = conn.execute("SELECT status, attempts FROM memory_outbox WHERE user_id = ?", (user_id,)).fetchone()
            self.assertEqual(row["status"], "sent")
            self.assertGreaterEqual(row["attempts"], 2)

    def test_same_day_new_task_generates_fresh_key(self):
        """New task enqueued later on the same day generates a fresh key and is not blocked by morning key."""
        user_id = "u_sameday_user"
        today_date = "2026-09-05"

        # Morning: Task 1
        self.memory_provider.enabled = False
        self.service.propose_memory_candidate(
            user_id=user_id,
            method="memory.propose",
            payload={"rule": "morning_rule"},
            idempotency_key="morning-task",
        )
        self.memory_provider.enabled = True

        t_morning = self.service.get_today(user_id=user_id, day=today_date)
        key_morning = t_morning["data"]["maintenance_key"]

        # Maintain morning task
        m_morning = self.service.maintain_memory(user_id=user_id, idempotency_key=key_morning)
        self.assertEqual(m_morning["status"], "success")
        self.assertFalse(self.service.get_today(user_id=user_id, day=today_date)["data"]["maintenance_recommended"])

        # Evening: Task 2 arrives on same date
        self.memory_provider.enabled = False
        self.service.propose_memory_candidate(
            user_id=user_id,
            method="memory.propose",
            payload={"rule": "evening_rule"},
            idempotency_key="evening-task",
        )
        self.memory_provider.enabled = True

        t_evening = self.service.get_today(user_id=user_id, day=today_date)
        self.assertTrue(t_evening["data"]["maintenance_recommended"])
        key_evening = t_evening["data"]["maintenance_key"]

        # Evening key must be different from morning key!
        self.assertNotEqual(key_morning, key_evening)

        # Maintain evening task
        m_evening = self.service.maintain_memory(user_id=user_id, idempotency_key=key_evening)
        self.assertEqual(m_evening["status"], "success")
        self.assertEqual(m_evening["data"]["sent_count"], 1)

    def test_daily_review_does_not_spam_rule_proposals_or_bypass_propose_candidate(self):
        """daily_review does NOT insert fake propose candidates into outbox or invoke provider as health rules."""
        user_id = "u_rev_isolate"
        date = "2026-09-05"

        # Execute daily_review
        rev_res = self.service.daily_review(
            user_id=user_id,
            date=date,
            idempotency_key="rev-isolate-1",
            user_notes="无任何红旗症状，睡眠良好。",
        )
        self.assertEqual(rev_res["status"], "success")

        # 1. Check outbox: exactly 0 items
        with self.store.connect() as conn:
            cnt = conn.execute("SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = ?", (user_id,)).fetchone()["c"]
            self.assertEqual(cnt, 0, "daily_review must NOT write candidates into memory_outbox")

        # 2. Check Provider: exactly 0 calls made
        self.assertEqual(len(self.memory_provider.calls), 0)

        # 3. Memory candidates must come through propose_memory_candidate with 'memory.propose'
        prop_res = self.service.propose_memory_candidate(
            user_id=user_id,
            method="memory.propose",
            payload={"verified_health_rule": "lactose_intolerance"},
            idempotency_key="prop-rule-1",
        )
        self.assertEqual(prop_res["status"], "success")
        self.assertEqual(len(self.memory_provider.calls), 1)
        self.assertEqual(self.memory_provider.calls[0][0], "memory.propose")
        self.assertIn("verified_health_rule", self.memory_provider.calls[0][1])

    def test_stdio_mcp_continuation_end_to_end(self):
        """End-to-end stdio MCP client verifying 51+ continuation cycle and hint clearing."""
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async def _run():
            py_bin = sys.executable
            server_params = StdioServerParameters(
                command=py_bin,
                args=["-m", "cyber_health_mcp", "--allow-all"],
                env=dict(os.environ, CYBER_HEALTH_DB=self.db_path, CYBER_HEALTH_MOCK_MEMORY="1"),
            )
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    user_id = "owner"
                    date = "2026-09-05"

                    # 1. Insert 55 pending outbox records
                    now_iso = datetime.now(UTC).isoformat()
                    with self.store.connect() as conn:
                        for i in range(55):
                            conn.execute(
                                """INSERT INTO memory_outbox(
                                    intent_id, user_id, idempotency_key, request_hash, method, payload_json,
                                    status, attempts, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, 'memory.propose', '{"rule": "mcp_cont"}', 'pending', 0, ?, ?)""",
                                (f"intent_mcp_{user_id}_{i:03d}", user_id, f"mcp-idem-{i:03d}", f"mcphash-{i:03d}", now_iso, now_iso),
                            )

                    # 2. get_today returns maintenance_recommended = True
                    today_1 = await session.call_tool("cyber_health_get_today", {"date": date})
                    t1_data = json.loads(today_1.content[0].text)
                    self.assertTrue(t1_data["data"]["maintenance_recommended"])
                    key_pass1 = t1_data["data"]["maintenance_key"]

                    # 3. Maintain Pass 1
                    maint_1 = await session.call_tool(
                        "cyber_health_maintain_memory",
                        {"idempotency_key": key_pass1},
                    )
                    m1_data = json.loads(maint_1.content[0].text)
                    self.assertEqual(m1_data["status"], "partial")
                    self.assertEqual(m1_data["data"]["sent_count"], 50)
                    self.assertTrue(m1_data["data"]["has_more"])
                    key_pass2 = m1_data["data"]["next_maintenance_key"]
                    self.assertNotEqual(key_pass1, key_pass2)

                    # 4. Maintain Pass 2 with next_maintenance_key
                    maint_2 = await session.call_tool(
                        "cyber_health_maintain_memory",
                        {"idempotency_key": key_pass2},
                    )
                    m2_data = json.loads(maint_2.content[0].text)
                    self.assertEqual(m2_data["status"], "success")
                    self.assertEqual(m2_data["data"]["sent_count"], 5)
                    self.assertFalse(m2_data["data"]["has_more"])

                    # 5. get_today hint is cleared
                    today_2 = await session.call_tool("cyber_health_get_today", {"date": date})
                    t2_data = json.loads(today_2.content[0].text)
                    self.assertFalse(t2_data["data"]["maintenance_recommended"])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
