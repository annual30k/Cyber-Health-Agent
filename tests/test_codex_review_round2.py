"""Second independent review: durability, scheduling and callable entrypoint."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cyber_health import CyberHealthService


class ReviewRoundTwo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")

    def test_console_entrypoint_is_callable(self):
        import cyber_health_mcp
        self.assertTrue(callable(getattr(cyber_health_mcp, "main", None)))

    def test_outbox_same_key_different_users_do_not_overwrite(self):
        for user in ("one", "two"):
            self.service.propose_memory_candidate(user_id=user, method="memory.propose",
                payload={"text": "synthetic test"}, idempotency_key="same-key")
        with self.service.store.connect() as conn:
            users = {r[0] for r in conn.execute("SELECT user_id FROM memory_outbox")}
        self.assertEqual(users, {"one", "two"})

    def test_memory_intent_committed_before_external_io(self):
        observed = []
        service = self.service
        class Provider:
            def call(self, method, payload):
                with service.store.connect() as conn:
                    observed.append(conn.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0])
                return {"status": "success"}
        service.memory_provider = Provider()
        service.propose_memory_candidate(user_id="u", method="memory.propose",
            payload={"text": "synthetic test"}, idempotency_key="first")
        self.assertGreater(observed[0], 0)

    def test_schedule_ids_stable_across_distinct_requests(self):
        first = self.service.schedule_daily_reminders(user_id="u", date="2026-09-04", idempotency_key="one")
        second = self.service.schedule_daily_reminders(user_id="u", date="2026-09-04", idempotency_key="two")
        ids = lambda r: {e["event_id"] for e in r["data"]["scheduled_events"]}
        self.assertEqual(ids(first), ids(second))

    def test_schedule_respects_new_york_timezone(self):
        self.service.update_profile(user_id="u", timezone="America/New_York", idempotency_key="profile")
        result = self.service.schedule_daily_reminders(user_id="u", date="2026-09-04", idempotency_key="schedule")
        event = next(e for e in result["data"]["scheduled_events"] if e["event_type"] == "MORNING_PLAN")
        dt = datetime.fromisoformat(event["window_start"].replace("Z", "+00:00"))
        self.assertEqual(dt.astimezone(ZoneInfo("America/New_York")).hour, 7)

    def test_profile_red_flag_sets_restricted_mode(self):
        self.service.update_profile(user_id="u", safety_flags=["严重胸痛"], idempotency_key="profile")
        self.assertEqual(self.service.get_profile("u")["safety_mode"], "restricted")

    def test_today_uses_read_transaction(self):
        traced = []
        original = self.service.store.connect
        def connect():
            conn = original()
            conn.set_trace_callback(traced.append)
            return conn
        self.service.store.connect = connect
        self.service.get_today("u", "2026-09-04")
        self.assertTrue(any(stmt.upper().startswith("BEGIN") for stmt in traced), traced)
