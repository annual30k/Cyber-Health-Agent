import tempfile
import unittest
from pathlib import Path

from cyber_health import ConflictError, CyberHealthService


class CrossSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "health.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_committed_meal_is_visible_to_new_service_instance(self):
        first_session = CyberHealthService(self.database)
        response = first_session.log_meal(
            user_id="u_default",
            occurred_at="2026-09-03T12:30:00+08:00",
            meal_type="lunch",
            foods=[{"name": "rice", "amount_g": {"low": 150, "high": 180}}],
            kcal_low=195,
            kcal_high=234,
            idempotency_key="lunch-001",
        )

        new_session = CyberHealthService(self.database)
        today = new_session.get_today("u_default", "2026-09-03")

        self.assertEqual(response["status"], "success")
        self.assertEqual(today["state_version"], response["state_version"])
        self.assertEqual(today["nutrition"], {
            "kcal_low": 195, "kcal_high": 234, "protein_low": 0, "protein_high": 0, "meal_count": 1,
        })


    def test_same_idempotency_key_returns_original_operation(self):
        service = CyberHealthService(self.database)
        payload = dict(
            user_id="u_default", occurred_at="2026-09-03T12:30:00+08:00", meal_type="lunch",
            foods=[], kcal_low=100, kcal_high=150, idempotency_key="lunch-001",
        )
        first = service.log_meal(**payload)
        repeated = service.log_meal(**payload)

        self.assertEqual(repeated, first)
        self.assertEqual(len(service.get_audit_trail("u_default")), 1)


    def test_correction_creates_a_revision_without_overwriting_history(self):
        service = CyberHealthService(self.database)
        original = service.log_meal(
            user_id="u_default", occurred_at="2026-09-03T12:30:00+08:00", meal_type="lunch",
            foods=[], kcal_low=400, kcal_high=500, idempotency_key="lunch-001",
        )
        correction = service.log_meal(
            user_id="u_default", occurred_at="2026-09-03T12:30:00+08:00", meal_type="lunch",
            foods=[], kcal_low=200, kcal_high=250, idempotency_key="lunch-001-correction",
            target_meal_id=original["data"]["meal_id"], expected_state_version=original["state_version"],
        )

        self.assertEqual(correction["state_version"], 2)
        self.assertEqual(service.get_today("u_default", "2026-09-03")["nutrition"]["kcal_low"], 200)


    def test_stale_state_version_is_rejected(self):
        service = CyberHealthService(self.database)
        service.log_meal(
            user_id="u_default", occurred_at="2026-09-03T12:30:00+08:00", meal_type="lunch",
            foods=[], kcal_low=100, kcal_high=150, idempotency_key="lunch-001",
        )

        with self.assertRaises(ConflictError) as caught:
            service.log_meal(
                user_id="u_default", occurred_at="2026-09-03T13:00:00+08:00", meal_type="snack",
                foods=[], kcal_low=100, kcal_high=150, idempotency_key="snack-001", expected_state_version=0,
            )
        self.assertEqual(caught.exception.code, "CONFLICT_VERSION")
