"""P0 Core Contract Tests for Cyber Health Agent.

Verifies:
1. Multi-session lifecycle & cross-session consistency
2. Idempotency exact replay vs parameter mismatch rejection (IDEMPOTENCY_MISMATCH)
3. Optimistic version conflict detection (CONFLICT_VERSION)
4. Read-only purity (get_profile / get_today never insert into DB)
5. Timezone-aware day grouping
6. Distinction between missing data and zero intake
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cyber_health import (
    ConflictError,
    CyberHealthService,
    IdempotencyMismatchError,
    ValidationError,
)


class TestP0Contracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_health.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_read_only_purity_never_mutates_database(self) -> None:
        """Reading profile or today on a non-existent user must NOT insert any rows."""
        profile = self.service.get_profile("u_nonexistent")
        self.assertFalse(profile["exists"])
        self.assertEqual(profile["state_version"], 0)

        today = self.service.get_today("u_nonexistent", "2026-09-04")
        self.assertEqual(today["state_version"], 0)
        self.assertEqual(today["nutrition"]["meal_count"], 0)
        self.assertTrue(today["plan_status"]["missing_data"])

        # Check raw database table to ensure 0 rows in user_profile
        with self.service.store.connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM user_profile").fetchone()["c"]
            self.assertEqual(count, 0)

    def test_multi_session_lifecycle(self) -> None:
        """Simulate 5 independent sessions interacting with the same physical database."""
        # Session 1: Check non-existent profile and record initial breakfast
        s1 = CyberHealthService(self.db_path)
        p1 = s1.get_profile("u_alice")
        self.assertEqual(p1["state_version"], 0)

        res_meal1 = s1.log_meal(
            user_id="u_alice",
            occurred_at="2026-09-04T08:00:00+08:00",
            meal_type="breakfast",
            foods=[{"name": "oatmeal", "amount_g": {"low": 50, "high": 60}}],
            kcal_low=180,
            kcal_high=220,
            protein_low=6,
            protein_high=8,
            idempotency_key="alice-bk-01",
        )
        self.assertEqual(res_meal1["status"], "success")
        self.assertEqual(res_meal1["state_version"], 1)

        # Session 2: Check today status from fresh instance
        s2 = CyberHealthService(self.db_path)
        today2 = s2.get_today("u_alice", "2026-09-04")
        self.assertEqual(today2["state_version"], 1)
        self.assertEqual(today2["nutrition"]["meal_count"], 1)
        self.assertEqual(today2["nutrition"]["kcal_low"], 180)
        self.assertFalse(today2["plan_status"]["missing_data"])

        # Session 3: Add lunch
        s3 = CyberHealthService(self.db_path)
        res_meal2 = s3.log_meal(
            user_id="u_alice",
            occurred_at="2026-09-04T12:30:00+08:00",
            meal_type="lunch",
            foods=[{"name": "chicken salad"}],
            kcal_low=450,
            kcal_high=550,
            protein_low=35,
            protein_high=45,
            idempotency_key="alice-lunch-01",
            expected_state_version=1,
        )
        self.assertEqual(res_meal2["state_version"], 2)

        # Session 4: Revise lunch (user ate less)
        s4 = CyberHealthService(self.db_path)
        res_rev = s4.log_meal(
            user_id="u_alice",
            occurred_at="2026-09-04T12:30:00+08:00",
            meal_type="lunch",
            foods=[{"name": "half chicken salad"}],
            kcal_low=250,
            kcal_high=300,
            protein_low=20,
            protein_high=25,
            idempotency_key="alice-lunch-01-rev",
            target_meal_id=res_meal2["data"]["meal_id"],
            expected_state_version=2,
        )
        self.assertEqual(res_rev["state_version"], 3)

        # Session 5: Read audit trail and confirm reconciled daily balance
        s5 = CyberHealthService(self.db_path)
        final_today = s5.get_today("u_alice", "2026-09-04")
        self.assertEqual(final_today["state_version"], 3)
        # Total active meals = breakfast (180-220) + revised lunch (250-300) = 430-520 kcal
        self.assertEqual(final_today["nutrition"]["meal_count"], 2)
        self.assertEqual(final_today["nutrition"]["kcal_low"], 430)
        self.assertEqual(final_today["nutrition"]["kcal_high"], 520)

        trail = s5.get_audit_trail("u_alice")
        self.assertEqual(len(trail), 3)

    def test_idempotency_exact_replay_vs_mismatch_error(self) -> None:
        """Exact repeat returns cached operation; different payload with same key raises IDEMPOTENCY_MISMATCH."""
        payload = {
            "user_id": "u_bob",
            "occurred_at": "2026-09-04T12:00:00+08:00",
            "meal_type": "lunch",
            "foods": [{"name": "beef noodles"}],
            "kcal_low": 500,
            "kcal_high": 650,
            "protein_low": 25,
            "protein_high": 35,
            "idempotency_key": "bob-idemp-001",
        }

        # First execution
        first_res = self.service.log_meal(**payload)
        self.assertEqual(first_res["status"], "success")

        # Exact repeat
        replay_res = self.service.log_meal(**payload)
        self.assertEqual(replay_res, first_res)

        # Mismatch: same key, but different calories
        mismatch_payload = dict(payload)
        mismatch_payload["kcal_low"] = 700
        mismatch_payload["kcal_high"] = 900

        with self.assertRaises(IdempotencyMismatchError) as caught:
            self.service.log_meal(**mismatch_payload)
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_MISMATCH")

        # Verify only 1 meal and 1 operation log exist
        with self.service.store.connect() as conn:
            meals_count = conn.execute("SELECT COUNT(*) AS c FROM meal_log WHERE user_id = 'u_bob'").fetchone()["c"]
            ops_count = conn.execute("SELECT COUNT(*) AS c FROM operation_log WHERE user_id = 'u_bob'").fetchone()["c"]
            self.assertEqual(meals_count, 1)
            self.assertEqual(ops_count, 1)

    def test_optimistic_conflict_version_rejected(self) -> None:
        """Providing an outdated expected_state_version raises CONFLICT_VERSION."""
        self.service.log_meal(
            user_id="u_carol",
            occurred_at="2026-09-04T09:00:00+08:00",
            meal_type="breakfast",
            foods=[],
            kcal_low=200,
            kcal_high=250,
            idempotency_key="carol-bk",
        )

        with self.assertRaises(ConflictError) as caught:
            self.service.log_meal(
                user_id="u_carol",
                occurred_at="2026-09-04T13:00:00+08:00",
                meal_type="lunch",
                foods=[],
                kcal_low=400,
                kcal_high=500,
                idempotency_key="carol-lunch",
                expected_state_version=0,  # Stale, currently 1
            )
        self.assertEqual(caught.exception.code, "CONFLICT_VERSION")

    def test_timezone_aware_day_aggregation(self) -> None:
        """Occurred_at timestamps in UTC must be converted to user timezone (Asia/Shanghai) for day calculation."""
        # User profile timezone defaults to Asia/Shanghai (UTC+8)
        # 2026-09-03T16:30:00Z -> In Shanghai (+8h) this is 2026-09-04T00:30:00+08:00 (i.e. Sept 4)
        self.service.log_meal(
            user_id="u_dave",
            occurred_at="2026-09-03T16:30:00Z",
            meal_type="late_snack",
            foods=[],
            kcal_low=150,
            kcal_high=200,
            idempotency_key="dave-snack-1",
        )

        # 2026-09-04T15:30:00Z -> In Shanghai (+8h) this is 2026-09-04T23:30:00+08:00 (i.e. Sept 4)
        self.service.log_meal(
            user_id="u_dave",
            occurred_at="2026-09-04T15:30:00Z",
            meal_type="late_snack_2",
            foods=[],
            kcal_low=100,
            kcal_high=150,
            idempotency_key="dave-snack-2",
        )

        # Sept 3 in Shanghai should have 0 meals
        sept3 = self.service.get_today("u_dave", "2026-09-03")
        self.assertEqual(sept3["nutrition"]["meal_count"], 0)
        self.assertTrue(sept3["plan_status"]["missing_data"])

        # Sept 4 in Shanghai should aggregate both meals
        sept4 = self.service.get_today("u_dave", "2026-09-04")
        self.assertEqual(sept4["nutrition"]["meal_count"], 2)
        self.assertEqual(sept4["nutrition"]["kcal_low"], 250)
        self.assertEqual(sept4["nutrition"]["kcal_high"], 350)
        self.assertFalse(sept4["plan_status"]["missing_data"])

    def test_validation_errors(self) -> None:
        """Invalid inputs (e.g. kcal_high < kcal_low) must raise ValidationError."""
        with self.assertRaises(ValidationError):
            self.service.log_meal(
                user_id="u_eva",
                occurred_at="2026-09-04T12:00:00+08:00",
                meal_type="lunch",
                foods=[],
                kcal_low=500,
                kcal_high=400,  # Invalid range
                idempotency_key="eva-invalid",
            )


if __name__ == "__main__":
    unittest.main()
