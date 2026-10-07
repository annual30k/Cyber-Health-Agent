"""Memory proposals, actions, the durable outbox and maintenance draining."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cyber_health import (
    CyberHealthService,
    ValidationError,
)
from cyber_health.memory import MemoryUnavailable
from cyber_health.store import SQLiteStore
from test_support import FIXED_NOW, OWNER, fixed_clock


class MockWorkingProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, method: str, payload: dict) -> dict:
        self.calls.append({"method": method, "payload": payload})
        return {"status": "success", "echo": method}


class MockWorkingMemoryProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        return {"status": "ok", "candidate_id": f"cand_{len(self.calls)}"}




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


class QueryMemoryProvider:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        if method == "query":
            return {"items": self.items}
        return {"acknowledged": True}


class OutboxInterleavingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")

    def test_namespace_encoding_has_no_delimiter_collision(self):
        keys = ("owner:a", "a", "owner\"a", '["owner","a"]')
        for key in keys:
            self.service.propose_memory_candidate(method="memory.propose",
                payload={"text": "test"}, idempotency_key=key)
        with self.service.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0], len(keys))

    def test_pending_request_hash_checked_before_second_external_call(self):
        service = self.service
        calls = []
        errors = []
        class Provider:
            def call(self, method, payload):
                calls.append(payload)
                if len(calls) == 1:
                    try:
                        service.propose_memory_candidate(method="memory.propose",
                            payload={"text": "different"}, idempotency_key="same")
                    except Exception as error:
                        errors.append(getattr(error, "code", type(error).__name__))
                return {"status": "success"}
        service.memory_provider = Provider()
        with contextlib.suppress(Exception):
            service.propose_memory_candidate(method="memory.propose",
                payload={"text": "original"}, idempotency_key="same")
        self.assertEqual(calls.__len__(), 1)
        self.assertEqual(errors, ["IDEMPOTENCY_MISMATCH"])

    def test_second_maintainer_cannot_claim_first_maintainers_rows(self):
        service = self.service
        service.propose_memory_candidate(method="memory.propose",
            payload={"text": "test"}, idempotency_key="proposal")
        calls = []
        class Provider:
            def call(self, method, payload):
                calls.append(payload)
                if len(calls) == 1:
                    service.maintain_memory(idempotency_key="second")
                return {"status": "success"}
        service.memory_provider = Provider()
        service.maintain_memory(idempotency_key="first")
        self.assertEqual(len(calls), 1)


class OutboxBatchAndLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test_outbox_ext.sqlite3"
        self.provider = MockWorkingProvider()
        self.service = CyberHealthService(self.db_path, memory_provider=self.provider, clock=fixed_clock())

    def test_concurrent_same_key_same_payload_replay(self) -> None:
        """Exact repeat returns cached result without invoking Provider a second time."""
        res1 = self.service.propose_memory_candidate(
            method="memory.propose",
            payload={"topic": "hydration"},
            idempotency_key="key-replay-1",
        )
        self.assertEqual(res1["status"], "success")
        self.assertEqual(len(self.provider.calls), 1)

        res2 = self.service.propose_memory_candidate(
            method="memory.propose",
            payload={"topic": "hydration"},
            idempotency_key="key-replay-1",
        )
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["operation_id"], res1["operation_id"])
        self.assertEqual(len(self.provider.calls), 1)

    def test_batch_over_fifty_items_chunking(self) -> None:
        """When outbox has >50 items, maintainer claims exactly 50 and leaves the rest pending."""
        now = FIXED_NOW.isoformat()
        with self.service.store.connect() as conn:
            for i in range(65):
                intent_id = f"intent_bulk_{i:03d}"
                conn.execute(
                    """INSERT INTO memory_outbox(
                        intent_id, user_id, idempotency_key, method, payload_json, status, attempts, created_at
                    ) VALUES (?, 'owner', ?, 'memory.bulk', '{"idx": 1}', 'pending', 0, ?)""",
                    (intent_id, f"key_{i}", now),
                )

        # First maintenance pass: claims and processes exactly 50
        m1 = self.service.maintain_memory(idempotency_key="maint-bulk-1")
        self.assertEqual(m1["data"]["outbox_processed"], 50)
        self.assertEqual(m1["data"]["sent_count"], 50)

        with self.service.store.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = 'owner' AND status = 'pending'"
            ).fetchone()["c"]
        self.assertEqual(remaining, 15)

        # Second maintenance pass: claims and processes remaining 15
        m2 = self.service.maintain_memory(idempotency_key="maint-bulk-2")
        self.assertEqual(m2["data"]["outbox_processed"], 15)
        self.assertEqual(m2["data"]["sent_count"], 15)

        with self.service.store.connect() as conn:
            final_pending = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = 'owner' AND status = 'pending'"
            ).fetchone()["c"]
        self.assertEqual(final_pending, 0)

    def test_crashed_worker_lease_recovery(self) -> None:
        """Rows left in_flight by a crashed worker with expired lease are safely recovered."""
        expired_time = (FIXED_NOW - timedelta(minutes=5)).isoformat()
        with self.service.store.connect() as conn:
            conn.execute(
                """INSERT INTO memory_outbox(
                    intent_id, user_id, idempotency_key, method, payload_json, status,
                    owner_token, lease_until, attempts, created_at
                ) VALUES ('intent_crashed_01', 'owner', 'k_crash', 'memory.propose',
                          '{"data": "orphan"}', 'in_flight', 'crashed_token_999', ?, 1, ?)""",
                (expired_time, expired_time),
            )

        m = self.service.maintain_memory(idempotency_key="maint-crash-1")
        self.assertEqual(m["data"]["sent_count"], 1)

        with self.service.store.connect() as conn:
            row = conn.execute(
                "SELECT status, owner_token, lease_until FROM memory_outbox WHERE intent_id = 'intent_crashed_01'"
            ).fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertIsNone(row["owner_token"])
        self.assertIsNone(row["lease_until"])

    def test_real_physical_ttl_pruning(self) -> None:
        """Physical deletion of superseded domain records and sent outbox items older than prune_days."""
        old_time = (FIXED_NOW - timedelta(days=45)).isoformat()
        recent_time = (FIXED_NOW - timedelta(days=5)).isoformat()

        with self.service.store.connect() as conn:
            # Old superseded record (should be deleted)
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, status, causation_id, state_version, created_at
                ) VALUES ('rec_old_superseded', 'owner', 'meal', '2026-07-20', '{}', 'superseded', 'cause_1', 1, ?)""",
                (old_time,),
            )
            # Recent superseded record (should NOT be deleted)
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, status, causation_id, state_version, created_at
                ) VALUES ('rec_recent_superseded', 'owner', 'meal', '2026-08-30', '{}', 'superseded', 'cause_2', 2, ?)""",
                (recent_time,),
            )
            # Old active record (should NEVER be deleted)
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, status, causation_id, state_version, created_at
                ) VALUES ('rec_old_active', 'owner', 'meal', '2026-07-20', '{}', 'active', 'cause_3', 3, ?)""",
                (old_time,),
            )
            # Old sent outbox item (should be deleted)
            conn.execute(
                """INSERT INTO memory_outbox(
                    intent_id, user_id, idempotency_key, method, payload_json, status, attempts, created_at
                ) VALUES ('intent_old_sent', 'owner', 'k_old', 'memory.propose', '{}', 'sent', 1, ?)""",
                (old_time,),
            )

        m = self.service.maintain_memory(prune_days=30, idempotency_key="maint-ttl-1")
        self.assertGreaterEqual(m["data"]["purged_superseded_count"], 1)
        self.assertGreaterEqual(m["data"]["purged_outbox_count"], 1)
        self.assertTrue(m["data"]["audit_chain_preserved"])

        with self.service.store.connect() as conn:
            # Old superseded record is deleted
            old_sup = conn.execute(
                "SELECT record_id FROM domain_record WHERE record_id = 'rec_old_superseded'"
            ).fetchone()
            self.assertIsNone(old_sup)

            # Recent superseded record remains
            recent_sup = conn.execute(
                "SELECT record_id FROM domain_record WHERE record_id = 'rec_recent_superseded'"
            ).fetchone()
            self.assertIsNotNone(recent_sup)

            # Old active record remains intact
            old_act = conn.execute(
                "SELECT record_id FROM domain_record WHERE record_id = 'rec_old_active'"
            ).fetchone()
            self.assertIsNotNone(old_act)

            # Old sent outbox item is deleted
            old_outbox = conn.execute(
                "SELECT intent_id FROM memory_outbox WHERE intent_id = 'intent_old_sent'"
            ).fetchone()
            self.assertIsNone(old_outbox)


class IntentPreReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")

    def test_memory_intent_committed_before_external_io(self):
        observed = []
        service = self.service
        class Provider:
            def call(self, method, payload):
                with service.store.connect() as conn:
                    observed.append(conn.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0])
                return {"status": "success"}
        service.memory_provider = Provider()
        service.propose_memory_candidate(method="memory.propose",
            payload={"text": "synthetic test"}, idempotency_key="first")
        self.assertGreater(observed[0], 0)


class LeaseReentrancyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "source.sqlite3")

    def test_same_pending_request_does_not_steal_lease(self):
        service = self.service
        calls = []
        class Provider:
            def call(self, method, payload):
                calls.append(payload)
                if len(calls) == 1:
                    service.propose_memory_candidate(method="memory.propose",
                        payload={"text": "same"}, idempotency_key="same")
                return {"status": "success"}
        service.memory_provider = Provider()
        service.propose_memory_candidate(method="memory.propose",
            payload={"text": "same"}, idempotency_key="same")
        self.assertEqual(len(calls), 1)


class DeferredOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_advanced.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_memory_outbox_queueing_and_maintain_retry(self) -> None:
        """When MemoryProvider is unavailable, candidate is stored in outbox; maintain_memory retries it."""
        # 1. Propose memory with default UnavailableMemoryProvider
        res = self.service.propose_memory_candidate(
            method="memory.propose",
            payload={"insight": "Lactose sensitivity observed"},
            idempotency_key="u6-prop-1",
        )
        self.assertEqual(res["status"], "partial")
        self.assertTrue(any("MEMORY_DEFERRED" in w for w in res["warnings"]))

        # Check outbox count
        with self.service.store.connect() as conn:
            pending_count = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = 'owner' AND status = 'pending'"
            ).fetchone()["c"]
            self.assertEqual(pending_count, 1)

        # 2. Maintain memory while still unavailable -> stays in outbox
        m1 = self.service.maintain_memory(idempotency_key="u6-maint-1")
        self.assertEqual(m1["status"], "partial")
        self.assertEqual(m1["data"]["deferred_count"], 1)

        # 3. Attach working mock MemoryProvider
        mock_provider = MockWorkingMemoryProvider()
        service_with_memory = CyberHealthService(self.db_path, memory_provider=mock_provider)
        m2 = service_with_memory.maintain_memory(idempotency_key="u6-maint-2")
        self.assertEqual(m2["status"], "success")
        self.assertEqual(m2["data"]["sent_count"], 1)
        self.assertEqual(len(mock_provider.calls), 1)

        # Check outbox is now empty of pending items
        with self.service.store.connect() as conn:
            pending_count_after = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = 'owner' AND status = 'pending'"
            ).fetchone()["c"]
            self.assertEqual(pending_count_after, 0)


class ScopedMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_round13.db")
        self.store = SQLiteStore(self.db_path)
        self.memory_provider = MockMemoryProvider()
        self.service = CyberHealthService(self.store, memory_provider=self.memory_provider, clock=fixed_clock())

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scoped_maintenance_host_drain_and_idempotency(self):
        """Host executes maintain_memory to drain pending outbox; subsequent reads clear hint."""
        date = "2026-09-05"

        # Legitimate memory candidate proposition while provider is offline so it defers
        self.memory_provider.enabled = False
        self.service.propose_memory_candidate(
            method="memory.propose",
            payload={"rule": "maintain_test"},
            idempotency_key="cand-drain-1",
        )
        self.memory_provider.enabled = True

        # Verify get_today recommends maintenance with generation key
        t1 = self.service.get_today(day=date)
        self.assertTrue(t1["data"]["maintenance_recommended"])
        maint_key = t1["data"]["maintenance_key"]
        self.assertTrue(maint_key.startswith(f"maint_{OWNER}_{date}_g"))

        # Host maintenance cycle using the recommended maintenance_key
        maint_res = self.service.maintain_memory(idempotency_key=maint_key)
        self.assertEqual(maint_res["status"], "success")

        # Outbox item is now sent
        with self.store.connect() as conn:
            outbox_row = conn.execute(
                "SELECT status FROM memory_outbox WHERE user_id = ?",
                (OWNER,),
            ).fetchone()
            self.assertEqual(outbox_row["status"], "sent")

        # Subsequent get_today reports recommended = False
        t2 = self.service.get_today(day=date)
        self.assertFalse(t2["data"]["maintenance_recommended"])
        self.assertEqual(t2["data"]["maintenance"]["pending_outbox_count"], 0)
        self.assertIsNone(t2["data"]["suggested_action"])

        # Host re-invoking with same idempotency key returns exact cached response
        maint_res_replay = self.service.maintain_memory(idempotency_key=maint_key)
        self.assertEqual(maint_res["operation_id"], maint_res_replay["operation_id"])

        # Host re-invoking with new key is idempotent no-op
        maint_res_noop = self.service.maintain_memory(idempotency_key="host-maint-2")
        self.assertEqual(maint_res_noop["status"], "success")

    def test_scoped_maintenance_provider_failure_non_blocking(self):
        """When MemoryProvider fails, maintain_memory defers safely without blocking facts operations."""
        date = "2026-09-05"

        failing_svc = CyberHealthService(self.store, memory_provider=FailingMemoryProvider())
        failing_svc.propose_memory_candidate(
            method="memory.propose",
            payload={"rule": "fail_test"},
            idempotency_key="cand-fail-1",
        )

        # Host executes maintain_memory while provider is failing
        maint_res = failing_svc.maintain_memory(idempotency_key="host-maint-fail")
        self.assertIn(maint_res["status"], ("success", "partial"))

        # Outbox item remains pending with attempts incremented
        with self.store.connect() as conn:
            outbox_row = conn.execute(
                "SELECT status, attempts FROM memory_outbox WHERE user_id = ?",
                (OWNER,),
            ).fetchone()
            self.assertEqual(outbox_row["status"], "pending")
            self.assertGreaterEqual(outbox_row["attempts"], 1)

        # Core facts operations continue functioning normally
        meal_res = failing_svc.log_meal(
            occurred_at=f"{date}T18:00:00+08:00",
            meal_type="dinner",
            foods=[{"name": "牛肉", "amount_g": {"low": 150, "high": 150}}],
            kcal_low=300,
            kcal_high=350,
            idempotency_key="meal-nonblocking",
        )
        self.assertEqual(meal_res["status"], "success")

        today_res = failing_svc.get_today(day=date)
        self.assertEqual(today_res["status"], "success")
        self.assertTrue(today_res["data"]["maintenance_recommended"])

    def test_stdio_mcp_scoped_maintenance_simulation(self):
        """Simulate MCP client discovering historical TTL meal -> observing hint -> executing maintain_memory -> clearing hint."""
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
                    date = "2026-09-05"

                    # 1. Log historical meal older than 30 days requiring TTL compaction
                    meal_res = await session.call_tool(
                        "cyber_health_log_meal",
                        {
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
                        {"date": date},
                    )
                    today_data = json.loads(today_res.content[0].text)
                    self.assertTrue(today_data["data"]["maintenance_recommended"])
                    self.assertGreaterEqual(today_data["data"]["maintenance"]["ttl_meals_count"], 1)
                    maint_key = today_data["data"]["maintenance_key"]
                    self.assertTrue(maint_key.startswith(f"maint_{OWNER}_{date}_g"))

                    # 3. Host executes maintain_memory with generation key
                    maint_res = await session.call_tool(
                        "cyber_health_maintain_memory",
                        {"idempotency_key": maint_key},
                    )
                    maint_data = json.loads(maint_res.content[0].text)
                    self.assertEqual(maint_data["status"], "success")

                    # 4. Query get_today again: hint cleared
                    today_after = await session.call_tool(
                        "cyber_health_get_today",
                        {"date": date},
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
            py_bin = sys.executable
            # Without CYBER_HEALTH_MOCK_MEMORY: tests true unenabled host state
            server_params = StdioServerParameters(
                command=py_bin,
                args=["-m", "cyber_health_mcp", "--allow-all"],
                env=dict(os.environ, CYBER_HEALTH_DB=self.db_path),
            )
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    date = "2026-09-05"

                    # 1. Propose candidate
                    cand_res = await session.call_tool(
                        "cyber_health_memory_action",
                        {
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
                        {"idempotency_key": "stdio-maint-unenabled"},
                    )
                    maint_data = json.loads(maint_res.content[0].text)
                    self.assertEqual(maint_data["status"], "partial")
                    self.assertTrue(any("MEMORY_DEFERRED" in w for w in maint_data.get("warnings", [])))

                    # 3. Facts queries remain completely unblocked
                    today_res = await session.call_tool(
                        "cyber_health_get_today",
                        {"date": date},
                    )
                    today_data = json.loads(today_res.content[0].text)
                    self.assertEqual(today_data["status"], "success")
                    # Memory intent remains pending, so maintenance remains recommended
                    self.assertTrue(today_data["data"]["maintenance_recommended"])

        asyncio.run(_run())


class MaintenanceContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_round14.db")
        self.store = SQLiteStore(self.db_path)
        self.memory_provider = MockMemoryProvider()
        self.service = CyberHealthService(self.store, memory_provider=self.memory_provider, clock=fixed_clock())

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unified_due_work_calculator_detects_ttl_meal_details_and_discloses_reasons(self):
        """Unified calculator detects historical meals older than 30 days needing compaction, with 0 outbox items."""
        today_date = "2026-09-05"
        hist_occurred = "2026-07-20T12:00:00+08:00"

        # 1. Log a historical meal (>30 days ago) with food details
        self.service.log_meal(
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
            outbox_cnt = conn.execute("SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = ?", (OWNER,)).fetchone()["c"]
            self.assertEqual(outbox_cnt, 0)

        # 3. get_today identifies TTL meal compaction due work
        t1 = self.service.get_today(day=today_date)
        self.assertTrue(t1["data"]["maintenance_recommended"])
        self.assertEqual(t1["data"]["suggested_action"], "cyber_health_maintain_memory")
        self.assertIn("ttl_meal_details_due", t1["data"]["maintenance_reason"])
        self.assertEqual(t1["data"]["maintenance"]["ttl_meals_count"], 1)
        self.assertEqual(t1["data"]["maintenance"]["pending_outbox_count"], 0)

        maint_key = t1["data"]["maintenance_key"]
        self.assertIsNotNone(maint_key)
        self.assertTrue(maint_key.startswith(f"maint_{OWNER}_{today_date}_g"))

        # 4. Host executes maintain_memory with generation key
        maint_res = self.service.maintain_memory(idempotency_key=maint_key)
        self.assertEqual(maint_res["status"], "success")
        self.assertFalse(maint_res["data"]["has_more"])
        self.assertIsNone(maint_res["data"]["next_maintenance_key"])

        # 5. Verify meal foods_json was compacted to '[]' and weekly trend was consolidated
        with self.store.connect() as conn:
            meal_row = conn.execute("SELECT foods_json FROM meal_log WHERE user_id = ?", (OWNER,)).fetchone()
            self.assertEqual(meal_row["foods_json"], "[]")
            trend_cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM domain_record WHERE user_id = ? AND kind = 'weekly_nutrition_trend'",
                (OWNER,),
            ).fetchone()["c"]
            self.assertGreaterEqual(trend_cnt, 1)

        # 6. Subsequent get_today reports maintenance_recommended = False
        t2 = self.service.get_today(day=today_date)
        self.assertFalse(t2["data"]["maintenance_recommended"])
        self.assertIsNone(t2["data"]["maintenance_key"])
        self.assertIsNone(t2["data"]["maintenance_reason"])
        self.assertEqual(t2["data"]["maintenance"]["ttl_meals_count"], 0)

    def test_unified_due_work_calculator_detects_expired_in_flight_leases(self):
        """Unified calculator detects crashed worker expired leases and recovers them into sent."""
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
                    f"intent_{OWNER}_crash_1",
                    OWNER,
                    json.dumps(intent_payload),
                    past_lease,
                    past_lease,
                    past_lease,
                ),
            )

        # 2. get_today discovers the expired lease
        t1 = self.service.get_today(day=today_date)
        self.assertTrue(t1["data"]["maintenance_recommended"])
        self.assertIn("expired_worker_leases", t1["data"]["maintenance_reason"])
        self.assertEqual(t1["data"]["maintenance"]["expired_lease_count"], 1)

        maint_key = t1["data"]["maintenance_key"]
        self.assertTrue(maint_key.startswith(f"maint_{OWNER}_{today_date}_g"))

        # 3. Host calls maintain_memory -> recovers and marks sent
        maint_res = self.service.maintain_memory(idempotency_key=maint_key)
        self.assertEqual(maint_res["status"], "success")
        self.assertEqual(maint_res["data"]["sent_count"], 1)

        with self.store.connect() as conn:
            row = conn.execute("SELECT status FROM memory_outbox WHERE user_id = ?", (OWNER,)).fetchone()
            self.assertEqual(row["status"], "sent")

        # 4. get_today now reports False
        t2 = self.service.get_today(day=today_date)
        self.assertFalse(t2["data"]["maintenance_recommended"])

    def test_repeated_get_today_idempotent_key_stability(self):
        """Repeated get_today calls produce identical maintenance_key; maintain_memory replays cached response."""
        today_date = "2026-09-05"

        # Queue 2 items while provider is offline
        self.memory_provider.enabled = False
        for i in range(2):
            self.service.propose_memory_candidate(
                method="memory.propose",
                payload={"rule": f"rule_{i}"},
                idempotency_key=f"repeat-cand-{i}",
            )
        self.memory_provider.enabled = True

        # Call get_today 5 times: every call must produce the exact same maintenance_key
        keys = []
        for _ in range(5):
            res = self.service.get_today(day=today_date)
            self.assertTrue(res["data"]["maintenance_recommended"])
            keys.append(res["data"]["maintenance_key"])

        self.assertEqual(len(set(keys)), 1, "Repeated get_today without mutations must return identical key")
        stable_key = keys[0]

        # First maintain_memory call executes
        maint_1 = self.service.maintain_memory(idempotency_key=stable_key)
        self.assertEqual(maint_1["status"], "success")
        self.assertEqual(maint_1["data"]["sent_count"], 2)

        # Exact repeat call with same stable_key replays cached response
        maint_2 = self.service.maintain_memory(idempotency_key=stable_key)
        self.assertEqual(maint_1["operation_id"], maint_2["operation_id"])

    def test_fifty_one_plus_tasks_continuation_advances_without_idempotency_block(self):
        """When outbox has >50 items, pass 1 claims 50 (status partial) and provides next_maintenance_key to finish pass 2."""
        today_date = "2026-09-05"

        # Insert 55 pending outbox items directly
        now_iso = FIXED_NOW.isoformat()
        with self.store.connect() as conn:
            for i in range(55):
                conn.execute(
                    """INSERT INTO memory_outbox(
                        intent_id, user_id, idempotency_key, request_hash, method, payload_json,
                        status, attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'memory.propose', '{"rule": "stress"}', 'pending', 0, ?, ?)""",
                    (f"intent_batch_{OWNER}_{i:03d}", OWNER, f"batch-idem-{i:03d}", f"hash-{i:03d}", now_iso, now_iso),
                )

        # Initial check
        t0 = self.service.get_today(day=today_date)
        self.assertTrue(t0["data"]["maintenance_recommended"])
        self.assertEqual(t0["data"]["maintenance"]["pending_outbox_count"], 55)
        key_pass1 = t0["data"]["maintenance_key"]

        # Pass 1: claims 50 items; 5 remain so status is partial and has_more is True
        maint_1 = self.service.maintain_memory(idempotency_key=key_pass1)
        self.assertEqual(maint_1["status"], "partial")
        self.assertEqual(maint_1["data"]["outbox_processed"], 50)
        self.assertEqual(maint_1["data"]["sent_count"], 50)
        self.assertTrue(maint_1["data"]["has_more"])
        key_pass2 = maint_1["data"]["next_maintenance_key"]

        # next_maintenance_key MUST be distinct from key_pass1 (new work generation!)
        self.assertIsNotNone(key_pass2)
        self.assertNotEqual(key_pass1, key_pass2)

        # Pass 2: host uses key_pass2 to claim the remaining 5 items without idempotency collision
        maint_2 = self.service.maintain_memory(idempotency_key=key_pass2)
        self.assertEqual(maint_2["status"], "success")
        self.assertEqual(maint_2["data"]["outbox_processed"], 5)
        self.assertEqual(maint_2["data"]["sent_count"], 5)
        self.assertFalse(maint_2["data"]["has_more"])
        self.assertIsNone(maint_2["data"]["next_maintenance_key"])

        # All 55 items are sent
        with self.store.connect() as conn:
            sent_cnt = conn.execute("SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = ? AND status = 'sent'", (OWNER,)).fetchone()["c"]
            self.assertEqual(sent_cnt, 55)

        # get_today hint is cleared
        t_final = self.service.get_today(day=today_date)
        self.assertFalse(t_final["data"]["maintenance_recommended"])

    def test_partial_failure_followed_by_provider_recovery(self):
        """When Provider fails, status is partial, key advances for retry with backoff, and succeeds on recovery."""
        today_date = "2026-09-05"

        # Queue candidate while provider is failing
        failing_svc = CyberHealthService(self.store, memory_provider=FailingMemoryProvider())
        failing_svc.propose_memory_candidate(
            method="memory.propose",
            payload={"rule": "recovery_test"},
            idempotency_key="rec-cand-1",
        )

        t1 = failing_svc.get_today(day=today_date)
        key_fail_1 = t1["data"]["maintenance_key"]

        # Pass 1: maintain_memory while provider fails
        res1 = failing_svc.maintain_memory(idempotency_key=key_fail_1)
        self.assertEqual(res1["status"], "partial")
        self.assertEqual(res1["data"]["deferred_count"], 1)
        self.assertTrue(res1["data"]["has_more"])
        self.assertGreaterEqual(res1["data"]["retry_after_seconds"], 2)
        key_retry = res1["data"]["next_maintenance_key"]

        # Key advances because attempts incremented from 0 to 1
        self.assertIsNotNone(key_retry)
        self.assertNotEqual(key_fail_1, key_retry)

        # Immediate replay with old key returns cached partial without re-invoking failing provider
        res1_replay = failing_svc.maintain_memory(idempotency_key=key_fail_1)
        self.assertEqual(res1["operation_id"], res1_replay["operation_id"])

        # Provider recovers!
        failing_svc.memory_provider = MockMemoryProvider()

        # Pass 2: host calls with key_retry
        res2 = failing_svc.maintain_memory(idempotency_key=key_retry)
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["data"]["sent_count"], 1)
        self.assertFalse(res2["data"]["has_more"])

        # Outbox item is now sent
        with self.store.connect() as conn:
            row = conn.execute("SELECT status, attempts FROM memory_outbox WHERE user_id = ?", (OWNER,)).fetchone()
            self.assertEqual(row["status"], "sent")
            self.assertGreaterEqual(row["attempts"], 2)

    def test_same_day_new_task_generates_fresh_key(self):
        """New task enqueued later on the same day generates a fresh key and is not blocked by morning key."""
        today_date = "2026-09-05"

        # Morning: Task 1
        self.memory_provider.enabled = False
        self.service.propose_memory_candidate(
            method="memory.propose",
            payload={"rule": "morning_rule"},
            idempotency_key="morning-task",
        )
        self.memory_provider.enabled = True

        t_morning = self.service.get_today(day=today_date)
        key_morning = t_morning["data"]["maintenance_key"]

        # Maintain morning task
        m_morning = self.service.maintain_memory(idempotency_key=key_morning)
        self.assertEqual(m_morning["status"], "success")
        self.assertFalse(self.service.get_today(day=today_date)["data"]["maintenance_recommended"])

        # Evening: Task 2 arrives on same date
        self.memory_provider.enabled = False
        self.service.propose_memory_candidate(
            method="memory.propose",
            payload={"rule": "evening_rule"},
            idempotency_key="evening-task",
        )
        self.memory_provider.enabled = True

        t_evening = self.service.get_today(day=today_date)
        self.assertTrue(t_evening["data"]["maintenance_recommended"])
        key_evening = t_evening["data"]["maintenance_key"]

        # Evening key must be different from morning key!
        self.assertNotEqual(key_morning, key_evening)

        # Maintain evening task
        m_evening = self.service.maintain_memory(idempotency_key=key_evening)
        self.assertEqual(m_evening["status"], "success")
        self.assertEqual(m_evening["data"]["sent_count"], 1)

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
                                (f"intent_mcp_{OWNER}_{i:03d}", OWNER, f"mcp-idem-{i:03d}", f"mcphash-{i:03d}", now_iso, now_iso),
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


class MemoryActionAndPruningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test_mem_trends.sqlite3")
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_maintain_memory_prunes_foods_json_detail_preserves_macros(self) -> None:
        """Verify that maintenance safely sets foods_json='[]' while preserving numeric macros and timestamps."""
        logged = self.service.log_meal(
            occurred_at="2026-07-12T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Secret Recipe Spicy Stew", "amount_g": {"low": 300, "high": 350}}],
            kcal_low=650,
            kcal_high=750,
            protein_low=35,
            protein_high=45,
            idempotency_key="detail-meal-1",
        )
        meal_id = logged.get("meal_id") or logged["data"]["meal_id"]
        # Verify foods_json is populated before maintenance
        with self.service.store.connect() as conn:
            m_before = conn.execute("SELECT foods_json FROM meal_log WHERE meal_id = ?", (meal_id,)).fetchone()
            self.assertIn("Secret Recipe", m_before["foods_json"])

        self.service.maintain_memory(idempotency_key="maint-detail-1", prune_days=30)

        # Verify foods_json is cleared to '[]' but macros remain intact
        with self.service.store.connect() as conn:
            m_after = conn.execute(
                "SELECT foods_json, kcal_low, kcal_high, protein_low, protein_high, status FROM meal_log WHERE meal_id = ?",
                (meal_id,),
            ).fetchone()
            self.assertEqual(m_after["foods_json"], "[]")
            self.assertEqual(m_after["kcal_low"], 650)
            self.assertEqual(m_after["kcal_high"], 750)
            self.assertEqual(m_after["protein_low"], 35)
            self.assertEqual(m_after["protein_high"], 45)
            self.assertEqual(m_after["status"], "active")

    def test_memory_action_validation_blocks_unconfirmed_delete_and_invalid_action(self) -> None:
        """Verify that unknown actions and unconfirmed deletes fail with ValidationError before any DB/IO."""
        mock_prov = QueryMemoryProvider()
        service = CyberHealthService(self.db_path, memory_provider=mock_prov)

        # 1. Unconfirmed delete must fail immediately
        with self.assertRaises(ValidationError) as ctx1:
            service.memory_action(
                action_type="delete",
                target_note_path="wiki/Rule.md",
                confirmed=False,
                idempotency_key="act-del-unconf",
            )
        self.assertIn("explicit confirmation", str(ctx1.exception))
        self.assertEqual(len(mock_prov.calls), 0)

        # 2. Unknown action must fail immediately
        with self.assertRaises(ValidationError) as ctx2:
            service.memory_action(
                action_type="arbitrary_illegal_op",
                payload={"something": "bad"},
                idempotency_key="act-illegal",
            )
        self.assertIn("Invalid memory action", str(ctx2.exception))
        self.assertEqual(len(mock_prov.calls), 0)

        # 3. Confirm without candidate_id must fail immediately
        with self.assertRaises(ValidationError) as ctx3:
            service.memory_action(
                action_type="confirm",
                confirmed=True,
                idempotency_key="act-conf-no-id",
            )
        self.assertIn("candidate_id", str(ctx3.exception))
        self.assertEqual(len(mock_prov.calls), 0)

        # Ensure no database records were inserted during failed attempts
        with service.store.connect() as conn:
            op_count = conn.execute("SELECT COUNT(*) as c FROM operation_log WHERE user_id = 'owner'").fetchone()["c"]
            self.assertEqual(op_count, 0)
            outbox_count = conn.execute("SELECT COUNT(*) as c FROM memory_outbox WHERE user_id = 'owner'").fetchone()["c"]
            self.assertEqual(outbox_count, 0)

        # 4. Valid confirmed delete succeeds and calls provider
        res_del = service.memory_action(
            action_type="delete",
            target_note_path="wiki/Rule.md",
            confirmed=True,
            idempotency_key="act-del-conf",
        )
        self.assertEqual(res_del["status"], "success")
        self.assertEqual(len(mock_prov.calls), 1)
        self.assertEqual(mock_prov.calls[0][0], "memory.delete")

    def test_memory_action_cannot_smuggle_confirmation_in_payload(self) -> None:
        mock_prov = QueryMemoryProvider()
        service = CyberHealthService(self.db_path, memory_provider=mock_prov)
        for action_type, confirmed, payload in (
            ("action", False, {"candidate_id": "cand-example", "action_type": "confirm"}),
            ("action", False, {"candidate_id": "cand-example", "action": "delete"}),
            ("confirm", False, {"candidate_id": "cand-example", "confirmed": True}),
            ("reject", False, {"candidate_id": "cand-example", "action_type": "confirm"}),
        ):
            with self.subTest(action_type=action_type, payload=payload):
                with self.assertRaises(ValidationError):
                    service.memory_action(
                        action_type=action_type,
                        confirmed=confirmed, payload=payload,
                        idempotency_key=f"reject-{action_type}-{len(mock_prov.calls)}",
                    )
        self.assertEqual(mock_prov.calls, [])

        accepted = service.memory_action(
            action_type="action", confirmed=True,
            payload={"candidate_id": "cand-example", "action_type": "confirm"},
            idempotency_key="confirmed-generic-action",
        )
        self.assertEqual(accepted["status"], "success")
        self.assertEqual(mock_prov.calls[0][0], "memory.action")
        self.assertIs(mock_prov.calls[0][1]["confirmed"], True)


if __name__ == "__main__":
    unittest.main()
