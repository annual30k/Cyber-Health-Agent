"""Portable export and verified, atomic import of facts."""

from __future__ import annotations

import contextlib
import copy
import tempfile
import unittest
from pathlib import Path

from cyber_health import ConflictError, CyberHealthService, IdempotencyMismatchError, ValidationError
from cyber_health.store import SINGLE_USER_ID, SQLiteStore


class ImportSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")

    def test_old_backup_cannot_silently_clear_current_restriction(self):
        self.s.update_profile(idempotency_key="initial")
        backup = self.s.export_data()["data"]
        self.s.log_daily_metrics(date="2026-09-04",
            metrics={"notes": "严重胸痛"}, idempotency_key="symptoms")
        with contextlib.suppress(ConflictError):
            self.s.import_data(data=backup, idempotency_key="restore")
        self.assertEqual(self.s.get_profile()["safety_mode"], "restricted")

    def test_duplicate_meal_different_protein_is_conflict(self):
        self.s.log_meal(occurred_at="2026-09-04T12:00:00+08:00",
            meal_type="lunch", foods=[], kcal_low=400, kcal_high=500,
            protein_low=20, protein_high=30, idempotency_key="meal")
        backup = copy.deepcopy(self.s.export_data()["data"])
        backup["facts"]["meals"][0]["protein_high"] = 90
        with self.assertRaises(ConflictError):
            self.s.import_data(data=backup, idempotency_key="restore")


class ImportValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")
        self.s.update_profile(idempotency_key="initial")

    def backup(self):
        data = self.s.export_data()["data"]
        # Semantic validation must not depend on an optional checksum.
        data.pop("checksum", None)
        return data

    def test_unknown_minor_schema_rejected(self):
        data = self.backup()
        data["schema_version"] = "0.999.999"
        with self.assertRaises(ValidationError):
            self.s.import_data(data=data, idempotency_key="bad-schema")

    def test_malformed_profile_json_rejected_atomically(self):
        data = self.backup()
        data["facts"]["profile"]["goals_json"] = "{broken"
        before = self.s.get_profile()
        with self.assertRaises(ValidationError):
            self.s.import_data(data=data, idempotency_key="bad-json")
        self.assertEqual(self.s.get_profile(), before)

    def test_nonexistent_timezone_rejected(self):
        data = self.backup()
        data["facts"]["profile"]["timezone"] = "Imaginary/Nowhere"
        with self.assertRaises(ValidationError):
            self.s.import_data(data=data, idempotency_key="bad-zone")

    def test_invalid_safety_mode_rejected(self):
        data = self.backup()
        data["facts"]["profile"]["safety_mode"] = "anything-goes"
        with self.assertRaises(ValidationError):
            self.s.import_data(data=data, idempotency_key="bad-mode")


class ImportSafetyProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "source.sqlite3")

    def test_import_restores_safety_profile(self):
        self.service.update_profile(safety_flags=["严重胸痛"],
            timezone="America/New_York", idempotency_key="profile")
        exported = self.service.export_data()["data"]
        restored = CyberHealthService(Path(self.tmp.name) / "target.sqlite3")
        restored.import_data(data=exported, idempotency_key="import")
        profile = restored.get_profile()
        self.assertEqual(profile["safety_mode"], "restricted")
        self.assertEqual(profile["timezone"], "America/New_York")

    def test_import_hashes_values_not_just_keys(self):
        first = {"schema_version": "0.1.0", "facts": {"meals": [], "domain_records": []}}
        self.service.import_data(data=first, idempotency_key="import")
        with self.assertRaises(IdempotencyMismatchError):
            self.service.import_data(data=first | {"schema_version": "9.0.0"}, idempotency_key="import")


class ExportImportRoundtripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_remaining.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_export_and_import_data_roundtrip(self) -> None:
        """Verify export produces portable SQLite fact snapshot and import restores it idempotently."""

        # Seed data
        self.service.update_profile(
            goals={"target_kcal_low": 2000, "target_kcal_high": 2300},
            idempotency_key="prof_seed_01",
        )
        self.service.log_meal(
            occurred_at="2026-09-04T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "chicken breast", "amount_g": {"low": 200, "high": 200}}],
            kcal_low=300,
            kcal_high=350,
            idempotency_key="meal_seed_01",
        )
        self.service.complete_workout(
            date="2026-09-04",
            idempotency_key="wk_seed_01",
            completed_exercises=[{"name": "Pushup", "sets": 3, "reps": 15}],
        )

        # Export
        exported = self.service.export_data()
        self.assertEqual(exported["status"], "success")
        self.assertGreaterEqual(exported["data"]["facts"]["meal_count"], 1)
        self.assertGreaterEqual(exported["data"]["facts"]["domain_records_count"], 1)

        # Import into fresh database
        db_path_new = Path(self.temp_dir.name) / "test_imported.sqlite3"
        service_new = CyberHealthService(db_path_new)

        import_res = service_new.import_data(
            data=exported["data"],
            idempotency_key="import_01",
        )
        self.assertEqual(import_res["status"], "success")
        self.assertGreaterEqual(import_res["data"]["imported_meals"], 1)

        # Re-import same key -> exact replay
        replay_res = service_new.import_data(
            data=exported["data"],
            idempotency_key="import_01",
        )
        self.assertEqual(replay_res["operation_id"], import_res["operation_id"])

        # Check today query on new database reflects imported meal
        today = service_new.get_today(day="2026-09-04")
        self.assertGreaterEqual(today["nutrition"]["meal_count"], 1)


class LegacyImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cyber-health.sqlite3"
        self.store = SQLiteStore(self.db_path)
        self.service = CyberHealthService(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_legacy_user_import_into_owner(self) -> None:
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
