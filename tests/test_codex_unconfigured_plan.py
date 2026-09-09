"""Unconfigured nutrition goals must stay unknown in every plan entry point."""
import json
import tempfile
import unittest
from pathlib import Path
from cyber_health import CyberHealthService


class UnconfiguredPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = CyberHealthService(Path(self.tmp.name) / "test.db")

    def test_plan_does_not_invent_nutrition_targets(self):
        result = self.s.plan_tomorrow(user_id="u", date="2026-09-05", idempotency_key="plan")
        self.assertNotIn('unconfigured_default', json.dumps(result))
        self.assertNotIn('1800', json.dumps(result))

    def test_review_does_not_invent_nutrition_targets(self):
        result = self.s.daily_review(user_id="u", date="2026-09-04", idempotency_key="review")
        self.assertNotIn('unconfigured_default', json.dumps(result))
        self.assertNotIn('1800', json.dumps(result))
