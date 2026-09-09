"""Provider results are evidence, not automatically confirmed knowledge."""
import tempfile
import unittest
from pathlib import Path
from cyber_health import CyberHealthService


class EvidenceProvider:
    def call(self, method, payload):
        return {"items": [{"id": str(i), "content": "unreviewed protein observation"}
                          for i in range(5)]}


class MemoryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "test.db",
                                          memory_provider=EvidenceProvider())

    def test_missing_confirmation_is_not_promoted_to_confirmed_wiki(self):
        result = self.service.query_memory(user_id="u", query="protein", limit=2)
        for item in result["obsidian_memories"]:
            self.assertNotEqual(item["confirmation_status"], "confirmed_wiki")

    def test_provider_cannot_exceed_requested_result_limit(self):
        result = self.service.query_memory(user_id="u", query="protein", limit=2)
        self.assertLessEqual(len(result["obsidian_memories"]), 2)
