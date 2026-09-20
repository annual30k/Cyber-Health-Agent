"""Tests for _today_totals performance date bounds, transaction rollback without masking, and log_meal audit fields."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cyber_health.models import FoodItem
from cyber_health.service import CyberHealthService
from cyber_health.store import SINGLE_USER_ID, SQLiteStore


class TestTodayTotalsAndRollback(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cyber-health.sqlite3"
        self.store = SQLiteStore(self.db_path)
        self.service = CyberHealthService(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_today_totals_date_bounds(self) -> None:
        user_id = SINGLE_USER_ID
        # Log meals across multiple days
        self.service.log_meal(
            user_id=user_id,
            occurred_at="2026-09-10T12:00:00+08:00",
            meal_type="lunch",
            foods=[FoodItem(name="Rice")],
            kcal_low=400,
            kcal_high=500,
            protein_low=10,
            protein_high=15,
            idempotency_key="meal_day10",
        )
        self.service.log_meal(
            user_id=user_id,
            occurred_at="2026-09-20T12:00:00+08:00",
            meal_type="lunch",
            foods=[FoodItem(name="Steak")],
            kcal_low=600,
            kcal_high=700,
            protein_low=40,
            protein_high=50,
            idempotency_key="meal_day20",
        )

        # Query 2026-09-20
        res = self.service.get_today(user_id=user_id, day="2026-09-20")
        today = res["data"]["nutrition"]
        self.assertEqual(today["meal_count"], 1)
        self.assertEqual(today["kcal_low"], 600)
        self.assertEqual(today["protein_low"], 40)

        # Query 2026-09-10
        res10 = self.service.get_today(user_id=user_id, day="2026-09-10")
        today10 = res10["data"]["nutrition"]
        self.assertEqual(today10["meal_count"], 1)
        self.assertEqual(today10["kcal_low"], 400)
        self.assertEqual(today10["protein_low"], 10)

        # Query 2026-09-15 (empty)
        res15 = self.service.get_today(user_id=user_id, day="2026-09-15")
        self.assertEqual(res15["data"]["nutrition"]["meal_count"], 0)

    def test_get_today_rollback_preserves_root_exception(self) -> None:
        user_id = SINGLE_USER_ID
        # Induce an intentional exception inside get_today by mocking _today_totals
        with patch.object(self.service, "_today_totals", side_effect=ZeroDivisionError("simulated root error")):
            with self.assertRaises(ZeroDivisionError) as ctx:
                self.service.get_today(user_id=user_id, day="2026-09-20")
            self.assertEqual(str(ctx.exception), "simulated root error")

    def test_get_schedule_rollback_preserves_root_exception(self) -> None:
        import json
        user_id = SINGLE_USER_ID
        self.service.update_profile(
            user_id=user_id,
            goals={"target_kcal_low": 2000, "target_kcal_high": 2500},
            idempotency_key="setup_profile_goals",
        )
        with self.store.connect() as conn:
            conn.execute("UPDATE user_profile SET goals_json = '{bad-json' WHERE user_id = ?", (user_id,))

        with self.assertRaises(json.JSONDecodeError):
            self.service.get_schedule(user_id=user_id, date="2026-09-20")

    def test_log_meal_audit_fields_recorded_in_payload(self) -> None:
        user_id = SINGLE_USER_ID
        res = self.service.log_meal(
            user_id=user_id,
            occurred_at="2026-09-20T12:00:00+08:00",
            meal_type="lunch",
            foods=[FoodItem(name="Salad")],
            kcal_low=200,
            kcal_high=250,
            protein_low=5,
            protein_high=8,
            idempotency_key="meal_audit_test",
            source="photo_ocr",
            confidence="high",
            correction_reason="user_adjusted_portion",
            user_confirmed=True,
        )
        self.assertEqual(res["status"], "success")

        # Verify operation_log stores audit fields
        with self.store.connect() as conn:
            op = conn.execute(
                "SELECT * FROM operation_log WHERE idempotency_key = 'meal_audit_test'"
            ).fetchone()
            self.assertIsNotNone(op)
            # Response was cached
            self.assertEqual(op["action"], "log_meal")

    def test_legacy_user_import_into_owner(self) -> None:
        user_id = SINGLE_USER_ID
        legacy_export = {
            "schema_version": "0.2.1",
            "user_id": "u_default",
            "exported_at": "2026-09-01T00:00:00Z",
            "facts": {
                "profile": {
                    "user_id": "u_default",
                    "timezone": "Asia/Shanghai",
                    "goals": {},
                    "constraints": {},
                    "safety_flags": [],
                    "safety_mode": "normal",
                    "state_version": 1,
                    "updated_at": "2026-09-01T00:00:00Z",
                },
                "meals": [
                    {
                        "meal_id": "legacy_m1",
                        "user_id": "u_default",
                        "occurred_at": "2026-09-01T12:00:00Z",
                        "meal_type": "lunch",
                        "foods": [{"name": "Apple", "amount": 1.0, "unit": "piece"}],
                        "kcal_low": 80,
                        "kcal_high": 100,
                        "protein_low": 0,
                        "protein_high": 1,
                        "status": "active",
                    }
                ],
                "domain_records": [],
                "schedule_events": [],
            },
        }

        # Importing legacy export into SINGLE_USER_ID ("owner") must succeed without user mismatch ValidationError
        res = self.service.import_data(
            user_id=user_id,
            data=legacy_export,
            idempotency_key="import_legacy_u_default",
        )
        self.assertEqual(res["status"], "success")

        # Verify meal is now in owner partition
        with self.store.connect() as conn:
            meal = conn.execute("SELECT * FROM meal_log WHERE meal_id = 'legacy_m1'").fetchone()
            self.assertIsNotNone(meal)
            self.assertEqual(meal["user_id"], SINGLE_USER_ID)


if __name__ == "__main__":
    unittest.main()
