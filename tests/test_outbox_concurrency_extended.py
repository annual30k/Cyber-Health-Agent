"""Comprehensive tests for outbox state machine, lease recovery, batch chunking, and physical TTL."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cyber_health import CyberHealthService, IdempotencyMismatchError


class MockWorkingProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, method: str, payload: dict) -> dict:
        self.calls.append({"method": method, "payload": payload})
        return {"status": "success", "echo": method}


class OutboxConcurrencyExtendedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test_outbox_ext.sqlite3"
        self.provider = MockWorkingProvider()
        self.service = CyberHealthService(self.db_path, memory_provider=self.provider)

    def test_concurrent_same_key_same_payload_replay(self) -> None:
        """Exact repeat returns cached result without invoking Provider a second time."""
        res1 = self.service.propose_memory_candidate(
            user_id="u1",
            method="memory.propose",
            payload={"topic": "hydration"},
            idempotency_key="key-replay-1",
        )
        self.assertEqual(res1["status"], "success")
        self.assertEqual(len(self.provider.calls), 1)

        res2 = self.service.propose_memory_candidate(
            user_id="u1",
            method="memory.propose",
            payload={"topic": "hydration"},
            idempotency_key="key-replay-1",
        )
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["operation_id"], res1["operation_id"])
        self.assertEqual(len(self.provider.calls), 1)

    def test_batch_over_fifty_items_chunking(self) -> None:
        """When outbox has >50 items, maintainer claims exactly 50 and leaves the rest pending."""
        now = datetime.now(UTC).isoformat()
        with self.service.store.connect() as conn:
            for i in range(65):
                intent_id = f"intent_bulk_{i:03d}"
                conn.execute(
                    """INSERT INTO memory_outbox(
                        intent_id, user_id, idempotency_key, method, payload_json, status, attempts, created_at
                    ) VALUES (?, 'u_bulk', ?, 'memory.bulk', '{"idx": 1}', 'pending', 0, ?)""",
                    (intent_id, f"key_{i}", now),
                )

        # First maintenance pass: claims and processes exactly 50
        m1 = self.service.maintain_memory(user_id="u_bulk", idempotency_key="maint-bulk-1")
        self.assertEqual(m1["data"]["outbox_processed"], 50)
        self.assertEqual(m1["data"]["sent_count"], 50)

        with self.service.store.connect() as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = 'u_bulk' AND status = 'pending'"
            ).fetchone()["c"]
        self.assertEqual(remaining, 15)

        # Second maintenance pass: claims and processes remaining 15
        m2 = self.service.maintain_memory(user_id="u_bulk", idempotency_key="maint-bulk-2")
        self.assertEqual(m2["data"]["outbox_processed"], 15)
        self.assertEqual(m2["data"]["sent_count"], 15)

        with self.service.store.connect() as conn:
            final_pending = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = 'u_bulk' AND status = 'pending'"
            ).fetchone()["c"]
        self.assertEqual(final_pending, 0)

    def test_crashed_worker_lease_recovery(self) -> None:
        """Rows left in_flight by a crashed worker with expired lease are safely recovered."""
        expired_time = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with self.service.store.connect() as conn:
            conn.execute(
                """INSERT INTO memory_outbox(
                    intent_id, user_id, idempotency_key, method, payload_json, status,
                    owner_token, lease_until, attempts, created_at
                ) VALUES ('intent_crashed_01', 'u_crash', 'k_crash', 'memory.propose',
                          '{"data": "orphan"}', 'in_flight', 'crashed_token_999', ?, 1, ?)""",
                (expired_time, expired_time),
            )

        m = self.service.maintain_memory(user_id="u_crash", idempotency_key="maint-crash-1")
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
        old_time = (datetime.now(UTC) - timedelta(days=45)).isoformat()
        recent_time = (datetime.now(UTC) - timedelta(days=5)).isoformat()

        with self.service.store.connect() as conn:
            # Old superseded record (should be deleted)
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, status, causation_id, state_version, created_at
                ) VALUES ('rec_old_superseded', 'u_ttl', 'meal', '2026-07-20', '{}', 'superseded', 'cause_1', 1, ?)""",
                (old_time,),
            )
            # Recent superseded record (should NOT be deleted)
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, status, causation_id, state_version, created_at
                ) VALUES ('rec_recent_superseded', 'u_ttl', 'meal', '2026-08-30', '{}', 'superseded', 'cause_2', 2, ?)""",
                (recent_time,),
            )
            # Old active record (should NEVER be deleted)
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, status, causation_id, state_version, created_at
                ) VALUES ('rec_old_active', 'u_ttl', 'meal', '2026-07-20', '{}', 'active', 'cause_3', 3, ?)""",
                (old_time,),
            )
            # Old sent outbox item (should be deleted)
            conn.execute(
                """INSERT INTO memory_outbox(
                    intent_id, user_id, idempotency_key, method, payload_json, status, attempts, created_at
                ) VALUES ('intent_old_sent', 'u_ttl', 'k_old', 'memory.propose', '{}', 'sent', 1, ?)""",
                (old_time,),
            )

        m = self.service.maintain_memory(user_id="u_ttl", prune_days=30, idempotency_key="maint-ttl-1")
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


if __name__ == "__main__":
    unittest.main()
