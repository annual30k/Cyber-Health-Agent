"""Untrusted backup validation must precede persistence."""
import tempfile
import unittest
from pathlib import Path

from cyber_health import CyberHealthService, ValidationError


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
