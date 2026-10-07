"""Local trend consolidation, dual-layer recall and provider evidence handling."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from cyber_health import CyberHealthService
from test_support import OWNER


class QueryMemoryProvider:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        if method == "query":
            return {"items": self.items}
        return {"acknowledged": True}


class EvidenceProvider:
    def call(self, method, payload):
        return {"items": [{"id": str(i), "content": "unreviewed protein observation"}
                          for i in range(5)]}


class TrendsAndRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test_mem_trends.sqlite3")
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_query_memory_dual_layer_and_safety_advisory(self) -> None:
        # Setup mock provider with long-term memory
        mock_prov = QueryMemoryProvider([
            {
                "candidate_id": "cand_whey_intolerance",
                "content": "Whey protein isolate causes mild bloating; plant-based preferred.",
                "occurred_at": "2026-08-01",
                "confirmation_status": "confirmed_wiki",
            }
        ])
        service = CyberHealthService(self.db_path, memory_provider=mock_prov)

        # Log local short-term meal
        service.log_meal(
            occurred_at="2026-09-04T08:00:00+08:00",
            meal_type="breakfast",
            foods=[{"name": "Whey protein shake", "amount_g": {"low": 40, "high": 50}}],
            kcal_low=180,
            kcal_high=200,
            protein_low=30,
            protein_high=32,
            idempotency_key="meal-bfast-1",
        )

        # Dual-layer query for 'protein'
        res = service.query_memory(query="protein", limit=5)
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["obsidian_provider_connected"])
        self.assertGreaterEqual(res["sqlite_facts_count"], 1)
        self.assertGreaterEqual(res["obsidian_memories_count"], 1)
        self.assertEqual(res["obsidian_memories"][0]["record_id"], "cand_whey_intolerance")
        self.assertIsNone(res["safety_advisory"])

        # Query with acute cardiovascular red-flag symptom
        res_flag = service.query_memory(query="severe chest pain while training", limit=5)
        self.assertIsNotNone(res_flag["safety_advisory"])
        self.assertIn("SAFETY_RESTRICTED", res_flag["safety_advisory"])

    def test_maintain_memory_local_trend_consolidation(self) -> None:
        # Log a meal in the past (40 days ago)
        self.service.log_meal(
            occurred_at="2026-07-20T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Rice and beef", "amount_g": {"low": 200, "high": 250}}],
            kcal_low=600,
            kcal_high=700,
            protein_low=40,
            protein_high=45,
            idempotency_key="meal-old-1",
        )

        # Run maintain_memory with prune_days=30
        res = self.service.maintain_memory(idempotency_key="maint-trend-01", prune_days=30)
        self.assertIn("consolidated_trends", res["data"])
        self.assertGreaterEqual(res["data"]["consolidated_trends"], 1)

        # Verify weekly trend record is persisted in SQLite domain_record
        with self.service.store.connect() as conn:
            trend_row = conn.execute(
                "SELECT * FROM domain_record WHERE user_id = 'owner' AND kind = 'weekly_nutrition_trend'",
            ).fetchone()
            self.assertIsNotNone(trend_row)
            self.assertIn("aggregated_meals", trend_row["body_json"])

    def test_query_memory_unconfirmed_status_and_limit(self) -> None:
        mock_prov = QueryMemoryProvider([
            {"id": f"obs_{i}", "content": f"Protein note {i}"}
            for i in range(10)
        ])
        service = CyberHealthService(self.db_path, memory_provider=mock_prov)
        res = service.query_memory(query="protein", limit=3)
        self.assertLessEqual(len(res["obsidian_memories"]), 3)
        for item in res["obsidian_memories"]:
            self.assertEqual(item["confirmation_status"], "unconfirmed")
            self.assertIsNone(item["confidence"])

    def test_query_memory_malformed_provider_response(self) -> None:
        class MalformedProvider:
            def call(self, method: str, payload: dict[str, Any]) -> Any:
                return "unexpected string instead of dict"

        service = CyberHealthService(self.db_path, memory_provider=MalformedProvider())
        res = service.query_memory(query="protein", limit=5)
        self.assertFalse(res["obsidian_provider_connected"])
        self.assertEqual(len(res["obsidian_memories"]), 0)
        self.assertTrue(any("PROVIDER_MALFORMED_RESPONSE" in w for w in res["warnings"]))

    def test_maintain_memory_multi_meal_per_day_vs_multi_day_aggregation(self) -> None:
        """Verify that multiple meals on one day are summed per day before averaging,

        preventing confounding 1-day multi-meal with multi-day intake.
        """
        # Day 1: 2026-07-20 (Monday) - 2 meals: 500 kcal and 700 kcal -> Day total: 1200 kcal
        self.service.log_meal(
            occurred_at="2026-07-20T08:00:00+08:00",
            meal_type="breakfast",
            foods=[{"name": "Oatmeal"}],
            kcal_low=500,
            kcal_high=500,
            protein_low=20,
            protein_high=20,
            idempotency_key="trend-meal-1",
        )
        self.service.log_meal(
            occurred_at="2026-07-20T18:00:00+08:00",
            meal_type="dinner",
            foods=[{"name": "Steak"}],
            kcal_low=700,
            kcal_high=700,
            protein_low=60,
            protein_high=60,
            idempotency_key="trend-meal-2",
        )
        # Day 2: 2026-07-21 (Tuesday) - 1 meal: 800 kcal -> Day total: 800 kcal
        self.service.log_meal(
            occurred_at="2026-07-21T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Chicken rice"}],
            kcal_low=800,
            kcal_high=800,
            protein_low=40,
            protein_high=40,
            idempotency_key="trend-meal-3",
        )

        res = self.service.maintain_memory(idempotency_key="maint-trend-calc", prune_days=30)
        self.assertGreaterEqual(res["data"]["consolidated_trends"], 1)

        with self.service.store.connect() as conn:
            row = conn.execute(
                "SELECT body_json FROM domain_record WHERE user_id = 'owner' AND kind = 'weekly_nutrition_trend' AND status = 'active'",
            ).fetchone()
            self.assertIsNotNone(row)
            import json
            body = json.loads(row["body_json"])
            # True daily average across 2 recorded days: (1200 + 800) / 2 = 1000 kcal
            # (Old broken meal average would have been (500+700+800)/3 = 667 kcal)
            self.assertEqual(body["avg_daily_kcal"], [1000, 1000])
            # Protein: Day 1: 20+60=80g, Day 2: 40g -> (80+40)/2 = 60g
            self.assertEqual(body["avg_daily_protein"], [60, 60])
            self.assertEqual(body["total_meals"], 3)
            self.assertEqual(body["recorded_days"], 2)
            self.assertEqual(body["window_days"], 7)
            self.assertEqual(body["missing_days"], 5)

    def test_maintain_memory_missing_days_no_zero_injection(self) -> None:
        """Verify that missing days are disclosed and not divided by 7, avoiding fake 0-calorie days."""
        self.service.log_meal(
            occurred_at="2026-07-15T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Pizza"}],
            kcal_low=2100,
            kcal_high=2100,
            protein_low=90,
            protein_high=90,
            idempotency_key="missing-meal-1",
        )
        self.service.maintain_memory(idempotency_key="maint-missing", prune_days=30)
        with self.service.store.connect() as conn:
            row = conn.execute(
                "SELECT body_json FROM domain_record WHERE user_id = 'owner' AND kind = 'weekly_nutrition_trend' AND status = 'active'",
            ).fetchone()
            import json
            body = json.loads(row["body_json"])
            # Average over the single recorded day must be 2100, not 2100/7=300
            self.assertEqual(body["avg_daily_kcal"], [2100, 2100])
            self.assertEqual(body["recorded_days"], 1)
            self.assertEqual(body["missing_days"], 6)

    def test_maintain_memory_repeated_run_idempotency(self) -> None:
        """Verify that repeated maintenance without data changes does not create duplicate trend revisions."""
        self.service.log_meal(
            occurred_at="2026-07-10T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Salad"}],
            kcal_low=500,
            kcal_high=500,
            protein_low=30,
            protein_high=30,
            idempotency_key="idemp-meal-1",
        )
        r1 = self.service.maintain_memory(idempotency_key="maint-idemp-1", prune_days=30)
        self.assertEqual(r1["data"]["consolidated_trends"], 1)

        # Run again with second key: should be idempotent and not create a new revision
        r2 = self.service.maintain_memory(idempotency_key="maint-idemp-2", prune_days=30)
        self.assertEqual(r2["data"]["consolidated_trends"], 0)

        with self.service.store.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as c FROM domain_record WHERE user_id = 'owner' AND kind = 'weekly_nutrition_trend'",
            ).fetchone()["c"]
            self.assertEqual(count, 1)

    def test_maintain_memory_late_arriving_meal_revision_chain(self) -> None:
        """Verify that late-arriving historical meal creates a new trend revision chained to previous via parent_id."""
        self.service.log_meal(
            occurred_at="2026-07-20T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Soup"}],
            kcal_low=400,
            kcal_high=400,
            protein_low=20,
            protein_high=20,
            idempotency_key="late-meal-1",
        )
        self.service.maintain_memory(idempotency_key="maint-late-1", prune_days=30)

        with self.service.store.connect() as conn:
            v1_row = conn.execute(
                "SELECT record_id, status FROM domain_record WHERE user_id = 'owner' AND kind = 'weekly_nutrition_trend' AND status = 'active'",
            ).fetchone()
            self.assertIsNotNone(v1_row)
            v1_id = v1_row["record_id"]

        # Log late-arriving meal in the same ISO week (2026-07-22)
        self.service.log_meal(
            occurred_at="2026-07-22T12:00:00+08:00",
            meal_type="dinner",
            foods=[{"name": "Salmon"}],
            kcal_low=600,
            kcal_high=600,
            protein_low=40,
            protein_high=40,
            idempotency_key="late-meal-2",
        )

        # Re-run maintenance: should supersede v1 and create chained v2
        r2 = self.service.maintain_memory(idempotency_key="maint-late-2", prune_days=30)
        self.assertEqual(r2["data"]["consolidated_trends"], 1)

        with self.service.store.connect() as conn:
            old_row = conn.execute(
                "SELECT status FROM domain_record WHERE record_id = ?",
                (v1_id,),
            ).fetchone()
            self.assertEqual(old_row["status"], "superseded")

            new_row = conn.execute(
                "SELECT record_id, parent_id, status, body_json FROM domain_record WHERE user_id = 'owner' AND kind = 'weekly_nutrition_trend' AND status = 'active'",
            ).fetchone()
            self.assertIsNotNone(new_row)
            self.assertEqual(new_row["parent_id"], v1_id)
            import json
            b2 = json.loads(new_row["body_json"])
            self.assertEqual(b2["total_meals"], 2)
            self.assertEqual(b2["recorded_days"], 2)

    def test_maintain_memory_supersedes_stale_trend_when_all_meals_deleted(self) -> None:
        """Verify that deleting the last meal in a historical week supersedes the stale trend and preserves lineage."""
        # 1. Log a historical meal in week 2026-W30 (2026-07-20)
        meal = self.service.log_meal(
            occurred_at="2026-07-20T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Chicken Rice"}],
            kcal_low=500,
            kcal_high=600,
            protein_low=30,
            protein_high=35,
            idempotency_key="del-meal-1",
        )
        meal_id = meal["data"]["meal_id"]

        # 2. Run maintenance: generates active weekly trend
        r1 = self.service.maintain_memory(idempotency_key="del-maint-1", prune_days=30)
        self.assertEqual(r1["data"]["consolidated_trends"], 1)

        with self.service.store.connect() as conn:
            v1_row = conn.execute(
                "SELECT record_id, status FROM domain_record WHERE user_id = ? AND kind = 'weekly_nutrition_trend' AND status = 'active'",
                (OWNER,),
            ).fetchone()
            self.assertIsNotNone(v1_row)
            v1_id = v1_row["record_id"]

        # 3. Delete the last meal in that week
        self.service.delete_meal(meal_id=meal_id, idempotency_key="del-meal-op-1")

        # 4. Run maintenance again: must detect no active meals, supersede v1, and record retraction chained to v1
        r2 = self.service.maintain_memory(idempotency_key="del-maint-2", prune_days=30)
        self.assertEqual(r2["data"]["consolidated_trends"], 1)

        with self.service.store.connect() as conn:
            # v1 is now superseded
            old_row = conn.execute("SELECT status FROM domain_record WHERE record_id = ?", (v1_id,)).fetchone()
            self.assertEqual(old_row["status"], "superseded")

            # No active weekly trend remains for this user
            active_trends = conn.execute(
                "SELECT record_id FROM domain_record WHERE user_id = ? AND kind = 'weekly_nutrition_trend' AND status = 'active'",
                (OWNER,),
            ).fetchall()
            self.assertEqual(len(active_trends), 0)

            # A retraction record exists chaining to v1
            retract_row = conn.execute(
                "SELECT record_id, parent_id, status, body_json FROM domain_record WHERE user_id = ? AND kind = 'weekly_nutrition_trend' AND parent_id = ?",
                (OWNER, v1_id),
            ).fetchone()
            self.assertIsNotNone(retract_row)
            self.assertEqual(retract_row["parent_id"], v1_id)
            self.assertEqual(retract_row["status"], "superseded")
            b = json.loads(retract_row["body_json"])
            self.assertEqual(b.get("total_meals"), 0)
            self.assertEqual(b.get("status"), "retracted")

        # 5. Repeat run idempotency: running maintenance a third time does nothing extra
        r3 = self.service.maintain_memory(idempotency_key="del-maint-3", prune_days=30)
        self.assertEqual(r3["data"]["consolidated_trends"], 0)


class MemoryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "test.db",
                                          memory_provider=EvidenceProvider())

    def test_missing_confirmation_is_not_promoted_to_confirmed_wiki(self):
        result = self.service.query_memory(query="protein", limit=2)
        for item in result["obsidian_memories"]:
            self.assertNotEqual(item["confirmation_status"], "confirmed_wiki")

    def test_provider_cannot_exceed_requested_result_limit(self):
        result = self.service.query_memory(query="protein", limit=2)
        self.assertLessEqual(len(result["obsidian_memories"]), 2)


if __name__ == "__main__":
    unittest.main()
