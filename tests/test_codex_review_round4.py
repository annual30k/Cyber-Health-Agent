import tempfile
import unittest
from pathlib import Path

from cyber_health import CyberHealthService, IdempotencyMismatchError


class ReviewRoundFour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "source.sqlite3")

    def test_import_restores_safety_profile(self):
        self.service.update_profile(user_id="u", safety_flags=["严重胸痛"],
            timezone="America/New_York", idempotency_key="profile")
        exported = self.service.export_data(user_id="u")["data"]
        restored = CyberHealthService(Path(self.tmp.name) / "target.sqlite3")
        restored.import_data(user_id="u", data=exported, idempotency_key="import")
        profile = restored.get_profile("u")
        self.assertEqual(profile["safety_mode"], "restricted")
        self.assertEqual(profile["timezone"], "America/New_York")

    def test_import_hashes_values_not_just_keys(self):
        first = {"schema_version": "0.1.0", "facts": {"meals": [], "domain_records": []}}
        self.service.import_data(user_id="u", data=first, idempotency_key="import")
        with self.assertRaises(IdempotencyMismatchError):
            self.service.import_data(user_id="u", data=first | {"schema_version": "9.0.0"}, idempotency_key="import")

    def test_sleep_below_six_always_uses_recovery_rule(self):
        self.service.log_daily_metrics(user_id="u", date="2026-09-04",
            metrics={"sleep_hours": 5.9, "fatigue_level": 1}, idempotency_key="metrics")
        result = self.service.get_training_plan(user_id="u", date="2026-09-04")
        self.assertEqual(result["data"]["plan"]["rule_code"], "TRAIN_RECOVERY_01")

    def test_same_pending_request_does_not_steal_lease(self):
        service = self.service
        calls = []
        class Provider:
            def call(self, method, payload):
                calls.append(payload)
                if len(calls) == 1:
                    service.propose_memory_candidate(user_id="u", method="memory.propose",
                        payload={"text": "same"}, idempotency_key="same")
                return {"status": "success"}
        service.memory_provider = Provider()
        service.propose_memory_candidate(user_id="u", method="memory.propose",
            payload={"text": "same"}, idempotency_key="same")
        self.assertEqual(len(calls), 1)
