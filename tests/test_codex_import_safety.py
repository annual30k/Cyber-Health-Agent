"""Migration must not silently undo newer safety facts or ignore collisions."""
import copy
import tempfile
import unittest
from pathlib import Path
from cyber_health import CyberHealthService, ConflictError


class ImportSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")

    def test_old_backup_cannot_silently_clear_current_restriction(self):
        self.s.update_profile(user_id="u", idempotency_key="initial")
        backup = self.s.export_data(user_id="u")["data"]
        self.s.log_daily_metrics(user_id="u", date="2026-09-04",
            metrics={"notes": "严重胸痛"}, idempotency_key="symptoms")
        try:
            self.s.import_data(user_id="u", data=backup, idempotency_key="restore")
        except ConflictError:
            pass
        self.assertEqual(self.s.get_profile("u")["safety_mode"], "restricted")

    def test_duplicate_meal_different_protein_is_conflict(self):
        self.s.log_meal(user_id="u", occurred_at="2026-09-04T12:00:00+08:00",
            meal_type="lunch", foods=[], kcal_low=400, kcal_high=500,
            protein_low=20, protein_high=30, idempotency_key="meal")
        backup = copy.deepcopy(self.s.export_data(user_id="u")["data"])
        backup["facts"]["meals"][0]["protein_high"] = 90
        with self.assertRaises(ConflictError):
            self.s.import_data(user_id="u", data=backup, idempotency_key="restore")
