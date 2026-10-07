"""Evidence-based knowledge retrieval."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cyber_health import CyberHealthService


class KnowledgeQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_remaining.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_query_knowledge_disclosures(self) -> None:
        """Verify knowledge query returns evidence citations, non-diagnostic warnings, and clinical dependencies."""
        res = self.service.query_knowledge(query="protein intake recommendation for athletes", category="nutrition")
        self.assertEqual(res["status"], "success")
        self.assertIn("clinical_review_status", res["data"])
        self.assertEqual(
            res["data"]["clinical_review_status"],
            "evidence_rules_algorithmic_pending_licensed_physician_review",
        )
        self.assertIn("requires_credentialed_sports_dietitian_or_physician_for_individual_prescription", res["data"]["external_dependency"])
        self.assertTrue(len(res["data"]["evidence_items"]) > 0)
        self.assertIn("ISSN Position Stand", res["data"]["evidence_items"][0]["title"])
        self.assertTrue(any("NON_DIAGNOSTIC" in w for w in res["warnings"]))


if __name__ == "__main__":
    unittest.main()
