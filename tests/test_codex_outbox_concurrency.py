"""Deterministic interleaving probes for outbox durability."""
import tempfile
import unittest
from pathlib import Path

from cyber_health import CyberHealthService


class OutboxInterleavingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")

    def test_namespace_encoding_has_no_delimiter_collision(self):
        for user, key in (("a:b", "c"), ("a", "b:c")):
            self.service.propose_memory_candidate(user_id=user, method="memory.propose",
                payload={"text": "test"}, idempotency_key=key)
        with self.service.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0], 2)

    def test_pending_request_hash_checked_before_second_external_call(self):
        service = self.service
        calls = []
        errors = []
        class Provider:
            def call(self, method, payload):
                calls.append(payload)
                if len(calls) == 1:
                    try:
                        service.propose_memory_candidate(user_id="u", method="memory.propose",
                            payload={"text": "different"}, idempotency_key="same")
                    except Exception as error:
                        errors.append(getattr(error, "code", type(error).__name__))
                return {"status": "success"}
        service.memory_provider = Provider()
        try:
            service.propose_memory_candidate(user_id="u", method="memory.propose",
                payload={"text": "original"}, idempotency_key="same")
        except Exception:
            pass
        self.assertEqual(calls.__len__(), 1)
        self.assertEqual(errors, ["IDEMPOTENCY_MISMATCH"])

    def test_second_maintainer_cannot_claim_first_maintainers_rows(self):
        service = self.service
        service.propose_memory_candidate(user_id="u", method="memory.propose",
            payload={"text": "test"}, idempotency_key="proposal")
        calls = []
        class Provider:
            def call(self, method, payload):
                calls.append(payload)
                if len(calls) == 1:
                    service.maintain_memory(user_id="u", idempotency_key="second")
                return {"status": "success"}
        service.memory_provider = Provider()
        service.maintain_memory(user_id="u", idempotency_key="first")
        self.assertEqual(len(calls), 1)
