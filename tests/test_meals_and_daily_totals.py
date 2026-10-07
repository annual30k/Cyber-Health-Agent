"""Meal facts, revisions, read snapshots and daily intake aggregation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cyber_health import (
    CyberHealthService,
    ValidationError,
)
from cyber_health.models import FoodItem
from cyber_health.store import SQLiteStore


class MockWorkingMemoryProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        return {"status": "ok", "candidate_id": f"cand_{len(self.calls)}"}


class QueryMemoryProvider:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        if method == "query":
            return {"items": self.items}
        return {"acknowledged": True}


class MealInputValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "review.sqlite3")

    def meal(self, key, timestamp, kcal=100):
        return self.service.log_meal(occurred_at=timestamp,
            meal_type="breakfast", foods=[], kcal_low=kcal, kcal_high=kcal + 10,
            idempotency_key=key)

    def test_calendar_date_must_be_real(self):
        with self.assertRaises(ValidationError):
            self.service.log_daily_metrics(date="2026-99-99",
                metrics={"sleep_hours": 8}, idempotency_key="invalid-date")

    def test_food_amount_range_cannot_be_inverted(self):
        with self.assertRaises(ValidationError):
            self.service.log_meal(occurred_at="2026-09-04T08:00:00+08:00",
                meal_type="breakfast", foods=[{"name": "rice", "amount_g": {"low": 200, "high": 100}}],
                kcal_low=100, kcal_high=200, idempotency_key="bad-food")

    def test_yesterday_repeat_does_not_copy_today(self):
        self.meal("yesterday", "2026-09-03T08:00:00+08:00", 100)
        self.meal("today", "2026-09-04T08:00:00+08:00", 500)
        repeated = self.service.log_meal(occurred_at="2026-09-04T09:00:00+08:00",
            meal_type="breakfast", repeat_meal="yesterday_breakfast", idempotency_key="repeat")
        self.assertEqual(repeated["data"]["today_totals"]["kcal_low"], 600)

    def test_correction_requires_expected_version(self):
        first = self.meal("first", "2026-09-04T08:00:00+08:00")
        with self.assertRaises(ValidationError):
            self.service.log_meal(occurred_at="2026-09-04T08:00:00+08:00",
                meal_type="breakfast", kcal_low=50, kcal_high=60,
                target_meal_id=first["data"]["meal_id"], idempotency_key="correction")


class CrossSessionMealTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "health.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_committed_meal_is_visible_to_new_service_instance(self):
        first_session = CyberHealthService(self.database)
        response = first_session.log_meal(
            occurred_at="2026-09-03T12:30:00+08:00",
            meal_type="lunch",
            foods=[{"name": "rice", "amount_g": {"low": 150, "high": 180}}],
            kcal_low=195,
            kcal_high=234,
            idempotency_key="lunch-001",
        )

        new_session = CyberHealthService(self.database)
        today = new_session.get_today("2026-09-03")

        self.assertEqual(response["status"], "success")
        self.assertEqual(today["state_version"], response["state_version"])
        self.assertEqual(today["nutrition"], {
            "kcal_low": 195, "kcal_high": 234, "protein_low": 0, "protein_high": 0, "meal_count": 1,
        })

    def test_correction_creates_a_revision_without_overwriting_history(self):
        service = CyberHealthService(self.database)
        original = service.log_meal(
            occurred_at="2026-09-03T12:30:00+08:00", meal_type="lunch",
            foods=[], kcal_low=400, kcal_high=500, idempotency_key="lunch-001",
        )
        correction = service.log_meal(
            occurred_at="2026-09-03T12:30:00+08:00", meal_type="lunch",
            foods=[], kcal_low=200, kcal_high=250, idempotency_key="lunch-001-correction",
            target_meal_id=original["data"]["meal_id"], expected_state_version=original["state_version"],
        )

        self.assertEqual(correction["state_version"], 2)
        self.assertEqual(service.get_today("2026-09-03")["nutrition"]["kcal_low"], 200)


class MealAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_advanced.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_meal_delete_and_repeat(self) -> None:
        """Deleting a meal soft-deletes and recalculates totals; repeating a meal copies foods and nutrients."""
        # 1. Log breakfast
        bk = self.service.log_meal(
            occurred_at="2026-09-04T08:00:00+08:00",
            meal_type="breakfast",
            foods=[{"name": "eggs", "amount_g": {"low": 100, "high": 120}}],
            kcal_low=150,
            kcal_high=180,
            protein_low=12,
            protein_high=15,
            idempotency_key="u1-bk-1",
        )
        meal1_id = bk["data"]["meal_id"]

        # 2. Log lunch
        lunch = self.service.log_meal(
            occurred_at="2026-09-04T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "salad"}],
            kcal_low=300,
            kcal_high=400,
            protein_low=10,
            protein_high=15,
            idempotency_key="u1-lunch-1",
        )

        today_before_del = self.service.get_today("2026-09-04")
        self.assertEqual(today_before_del["nutrition"]["meal_count"], 2)
        self.assertEqual(today_before_del["nutrition"]["kcal_low"], 450)

        # 3. Delete lunch (mistake entry)
        del_res = self.service.delete_meal(
            meal_id=lunch["data"]["meal_id"],
            idempotency_key="u1-del-lunch",
            reason="Double logged by mistake",
        )
        self.assertEqual(del_res["status"], "success")
        self.assertEqual(del_res["data"]["deleted_meal_id"], lunch["data"]["meal_id"])

        today_after_del = self.service.get_today("2026-09-04")
        self.assertEqual(today_after_del["nutrition"]["meal_count"], 1)
        self.assertEqual(today_after_del["nutrition"]["kcal_low"], 150)

        # 4. Repeat breakfast on next day using repeat_meal="yesterday"
        self.service.log_meal(
            occurred_at="2026-09-05T08:00:00+08:00",
            meal_type="breakfast",
            foods=[],
            kcal_low=0,
            kcal_high=0,
            repeat_meal=meal1_id,
            idempotency_key="u1-bk-2",
        )
        today_sept5 = self.service.get_today("2026-09-05")
        self.assertEqual(today_sept5["nutrition"]["meal_count"], 1)
        self.assertEqual(today_sept5["nutrition"]["kcal_low"], 150)
        self.assertEqual(today_sept5["nutrition"]["protein_low"], 12)

    def test_uncertainty_aggregation_without_fake_confidence_intervals(self) -> None:
        """Aggregate estimate ranges transparently without inventing a confidence level."""
        date = "2026-09-08"

        # 1. Configure profile targets
        self.service.update_profile(
            idempotency_key="u_stats_prof",
            goals={
                "target_kcal_low": 2100,
                "target_kcal_high": 2300,
                "target_protein_low": 120,
                "target_protein_high": 150,
            },
        )

        # 2. Log 5 meals simulating the user's real day
        meals = [
            ("breakfast", 190, 230, 17, 22),
            ("lunch", 260, 320, 24, 30),
            ("snack", 80, 100, 0, 1),
            ("snack", 350, 500, 30, 45),
            ("dinner", 550, 750, 38, 50),
        ]
        for idx, (mtype, k_low, k_high, p_low, p_high) in enumerate(meals):
            self.service.log_meal(
                occurred_at=f"{date}T{8 + idx * 3:02d}:00:00+08:00",
                meal_type=mtype,
                foods=[{"name": f"Item {idx}"}],
                kcal_low=k_low,
                kcal_high=k_high,
                protein_low=p_low,
                protein_high=p_high,
                idempotency_key=f"u_stats_meal_{idx}",
            )

        # 3. Daily review
        review = self.service.daily_review(
            date=date,
            idempotency_key="u_stats_review",
        )
        data = review["data"]
        analysis = data["nutrition_analysis"]

        # Classic bounds are preserved
        self.assertEqual(analysis["intake_kcal_range"], [1430, 1900])
        self.assertEqual(analysis["intake_protein_g_range"], [109, 148])
        raw_kcal_spread = 1900 - 1430  # 470
        raw_protein_spread = 148 - 109  # 39

        # The heuristic uncertainty range contracts, but is not a confidence interval.
        kcal_uncertainty = analysis["intake_kcal_uncertainty_range"]
        stat_kcal_spread = kcal_uncertainty[1] - kcal_uncertainty[0]
        self.assertLess(stat_kcal_spread, raw_kcal_spread * 0.65)  # Contracted by >35%
        self.assertEqual(analysis["intake_kcal_mid"], 1665)
        self.assertIsNone(analysis["intake_kcal_ci90"])

        protein_uncertainty = analysis["intake_protein_uncertainty_range"]
        stat_protein_spread = protein_uncertainty[1] - protein_uncertainty[0]
        self.assertLess(stat_protein_spread, raw_protein_spread * 0.70)
        self.assertEqual(analysis["intake_protein_mid"], 128)
        self.assertIsNone(analysis["intake_protein_ci90"])

        # Target gap is arithmetic against a policy interval; it is not a CI.
        self.assertEqual(analysis["calorie_gap_mid"], 535)  # 2200 - 1665
        raw_gap_spread = analysis["calorie_target_gap_range"][1] - analysis["calorie_target_gap_range"][0]  # 870 - 200 = 670
        self.assertEqual(analysis["calorie_gap_uncertainty_range"], analysis["calorie_target_gap_range"])
        self.assertEqual(
            analysis["calorie_gap_uncertainty_range"][1] - analysis["calorie_gap_uncertainty_range"][0],
            raw_gap_spread,
        )
        self.assertIsNone(analysis["calorie_gap_ci90"])

        # The heuristic intake range remains inside the physical estimate bounds.
        self.assertGreaterEqual(kcal_uncertainty[0], 1430)
        self.assertLessEqual(kcal_uncertainty[1], 1900)
        self.assertGreaterEqual(analysis["intake_kcal_mid"], kcal_uncertainty[0])
        self.assertLessEqual(analysis["intake_kcal_mid"], kcal_uncertainty[1])
        self.assertEqual(analysis["protein_gap_uncertainty_range"], analysis["protein_target_gap_range"])
        self.assertIsNone(analysis["protein_gap_ci90"])

        # Check get_today remaining clamping consistency
        today = self.service.get_today(day=date)
        rem = today["remaining"]
        self.assertGreaterEqual(rem["kcal_mid"], rem["kcal_low"])
        self.assertLessEqual(rem["kcal_mid"], rem["kcal_high"])
        self.assertGreaterEqual(rem["protein_mid"], rem["protein_low"])
        self.assertLessEqual(rem["protein_mid"], rem["protein_high"])

        # Summary uses transparent heuristic terminology, never a false CI label.
        self.assertIn("1665 kcal", data["summary"])
        self.assertIn("128 g", data["summary"])
        self.assertIn("合成不确定性范围", data["summary"])
        self.assertNotIn("90%置信区间", data["summary"])
        self.assertNotIn("极差", data["summary"])

    def test_single_meal_interval_consistency(self) -> None:
        """A single meal keeps its estimate bounds and emits no unsupported CI."""
        date = "2026-09-08"
        self.service.update_profile(
            idempotency_key="u_single_prof",
            goals={
                "target_kcal_low": 2000,
                "target_kcal_high": 2200,
                "target_protein_low": 100,
                "target_protein_high": 120,
            },
        )
        self.service.log_meal(
            occurred_at=f"{date}T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Chicken rice"}],
            kcal_low=500,
            kcal_high=700,
            protein_low=30,
            protein_high=40,
            idempotency_key="u_single_meal_1",
        )
        review = self.service.daily_review(
            date=date,
            idempotency_key="u_single_review",
        )
        data = review["data"]
        analysis = data["nutrition_analysis"]

        self.assertEqual(analysis["intake_kcal_range"], [500, 700])
        self.assertEqual(analysis["intake_kcal_uncertainty_range"], [500, 700])
        self.assertIsNone(analysis["intake_kcal_ci90"])
        self.assertEqual(analysis["intake_kcal_mid"], 600)
        # Target gap remains the arithmetic policy-vs-intake interval.
        self.assertEqual(analysis["calorie_target_gap_range"], [1300, 1700])
        self.assertEqual(analysis["calorie_gap_uncertainty_range"], [1300, 1700])
        self.assertIsNone(analysis["calorie_gap_ci90"])
        self.assertEqual(analysis["calorie_gap_mid"], 1500)
        self.assertEqual(analysis["protein_target_gap_range"], [60, 90])
        self.assertEqual(analysis["protein_gap_uncertainty_range"], [60, 90])
        self.assertIsNone(analysis["protein_gap_ci90"])
        self.assertEqual(analysis["protein_gap_mid"], 75)
        self.assertIn("估算范围 500–700 kcal", data["summary"])
        self.assertNotIn("90%置信区间", data["summary"])

    def test_statistical_single_meal_vs_multi_meal_mathematical_properties(self) -> None:
        """Verify heuristic aggregation and the separation of policy gaps from uncertainty."""
        date = "2026-09-08"
        self.service.update_profile(
            idempotency_key="u_math_prof",
            goals={
                "target_kcal_low": 2000,
                "target_kcal_high": 2200,
                "target_protein_low": 100,
                "target_protein_high": 120,
            },
        )

        # 1. Log First Meal (n=1)
        self.service.log_meal(
            occurred_at=f"{date}T08:00:00+08:00",
            meal_type="breakfast",
            foods=[{"name": "Oatmeal and eggs"}],
            kcal_low=400,
            kcal_high=600,
            protein_low=20,
            protein_high=30,
            idempotency_key="u_math_meal_1",
        )
        review1 = self.service.daily_review(
            date=date,
            idempotency_key="u_math_rev_1",
        )
        ana1 = review1["data"]["nutrition_analysis"]

        # For n=1 the uncertainty range equals the recorded estimate bounds.
        self.assertEqual(ana1["intake_kcal_uncertainty_range"], [400, 600])
        self.assertIsNone(ana1["intake_kcal_ci90"])
        self.assertEqual(ana1["intake_kcal_mid"], 500)
        self.assertEqual(ana1["calorie_target_gap_range"], [1400, 1800])
        self.assertEqual(ana1["calorie_gap_uncertainty_range"], [1400, 1800])
        self.assertIsNone(ana1["calorie_gap_ci90"])
        self.assertEqual(ana1["calorie_gap_mid"], 1600)  # 2100 - 500

        # 2. Log Second Meal (n=2) -> CLT applies
        self.service.log_meal(
            occurred_at=f"{date}T12:30:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Salmon and sweet potato"}],
            kcal_low=600,
            kcal_high=800,
            protein_low=35,
            protein_high=45,
            idempotency_key="u_math_meal_2",
        )
        review2 = self.service.daily_review(
            date=date,
            idempotency_key="u_math_rev_2",
        )
        ana2 = review2["data"]["nutrition_analysis"]

        # Intake bounds for n=2: sum of bounds = [1000, 1400], spread = 400.
        self.assertEqual(ana2["intake_kcal_range"], [1000, 1400])
        raw_spread = 1400 - 1000
        stat_spread = ana2["intake_kcal_uncertainty_range"][1] - ana2["intake_kcal_uncertainty_range"][0]
        # RSS half-width is a documented display heuristic, not a CI.
        self.assertLess(stat_spread, raw_spread * 0.75)
        self.assertEqual(ana2["intake_kcal_mid"], 1200)
        self.assertIsNone(ana2["intake_kcal_ci90"])

        # Strict containment guarantees for the heuristic range.
        self.assertGreaterEqual(ana2["intake_kcal_uncertainty_range"][0], ana2["intake_kcal_range"][0])
        self.assertLessEqual(ana2["intake_kcal_uncertainty_range"][1], ana2["intake_kcal_range"][1])
        self.assertGreaterEqual(ana2["intake_kcal_mid"], ana2["intake_kcal_uncertainty_range"][0])
        self.assertLessEqual(ana2["intake_kcal_mid"], ana2["intake_kcal_uncertainty_range"][1])

        # Target gap is always the raw arithmetic policy-vs-intake interval:
        # target = [2000, 2200], intake = [1000, 1400]
        # raw_gap = [2000 - 1400, 2200 - 1000] = [600, 1200]
        self.assertEqual(ana2["calorie_target_gap_range"], [600, 1200])
        raw_gap_spread = 1200 - 600  # 600
        self.assertEqual(ana2["calorie_gap_uncertainty_range"], ana2["calorie_target_gap_range"])
        self.assertEqual(
            ana2["calorie_gap_uncertainty_range"][1] - ana2["calorie_gap_uncertainty_range"][0],
            raw_gap_spread,
        )
        self.assertIsNone(ana2["calorie_gap_ci90"])
        self.assertEqual(ana2["calorie_gap_mid"], 900)  # 2100 - 1200

        self.assertGreaterEqual(ana2["calorie_gap_mid"], ana2["calorie_gap_uncertainty_range"][0])
        self.assertLessEqual(ana2["calorie_gap_mid"], ana2["calorie_gap_uncertainty_range"][1])

    def test_zero_variance_exact_meal_bounds(self) -> None:
        """When user logs exact values (low == high), variance is zero, mid equals value, and bounds match."""
        date = "2026-09-08"
        self.service.update_profile(
            idempotency_key="u_exact_prof",
            goals={
                "target_kcal_low": 2000,
                "target_kcal_high": 2000,
                "target_protein_low": 100,
                "target_protein_high": 100,
            },
        )
        self.service.log_meal(
            occurred_at=f"{date}T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Measured meal"}],
            kcal_low=600,
            kcal_high=600,
            protein_low=40,
            protein_high=40,
            idempotency_key="u_exact_meal_1",
        )
        review = self.service.daily_review(
            date=date,
            idempotency_key="u_exact_rev",
        )
        analysis = review["data"]["nutrition_analysis"]
        self.assertEqual(analysis["intake_kcal_range"], [600, 600])
        self.assertEqual(analysis["intake_kcal_uncertainty_range"], [600, 600])
        self.assertIsNone(analysis["intake_kcal_ci90"])
        self.assertEqual(analysis["intake_kcal_mid"], 600)
        self.assertEqual(analysis["calorie_target_gap_range"], [1400, 1400])
        self.assertEqual(analysis["calorie_gap_uncertainty_range"], [1400, 1400])
        self.assertIsNone(analysis["calorie_gap_ci90"])
        self.assertEqual(analysis["calorie_gap_mid"], 1400)


class RemainingCaloriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test_mem_trends.sqlite3")
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_get_remaining_calories_unconfigured_and_configured(self) -> None:
        # 1. Unconfigured goals
        res_unconf = self.service.get_remaining_calories(date="2026-09-04")
        self.assertEqual(res_unconf["status"], "success")
        self.assertIsNone(res_unconf["remaining_ranges"])
        self.assertIn("unconfigured", res_unconf["suggestion"])

        # 2. Configure goals: 2000-2200 kcal, 140-160g protein
        self.service.update_profile(
            idempotency_key="up-1",
            goals={
                "target_kcal_low": 2000,
                "target_kcal_high": 2200,
                "target_protein_low": 140,
                "target_protein_high": 160,
            },
        )

        # 3. Query before meals
        res_before = self.service.get_remaining_calories(date="2026-09-04")
        self.assertEqual(res_before["status"], "success")
        self.assertEqual(res_before["priority_nutrients"], ["protein"])
        self.assertIn("140-160g protein", res_before["suggestion"])

        # 4. Log high-protein meal
        self.service.log_meal(
            occurred_at="2026-09-04T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "Chicken breast", "amount_g": {"low": 300, "high": 350}}],
            kcal_low=700,
            kcal_high=800,
            protein_low=150,
            protein_high=160,
            idempotency_key="meal-lunch-1",
        )

        # 5. Protein goal achieved
        res_after = self.service.get_remaining_calories(date="2026-09-04")
        self.assertEqual(res_after["priority_nutrients"], ["calories"])
        self.assertIn("Protein target met", res_after["suggestion"])


class DailyTotalsContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_health.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_read_only_purity_never_mutates_database(self) -> None:
        """Reading profile or today on a non-existent user must NOT insert any rows."""
        profile = self.service.get_profile()
        self.assertFalse(profile["exists"])
        self.assertEqual(profile["state_version"], 0)

        today = self.service.get_today("2026-09-04")
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
        p1 = s1.get_profile()
        self.assertEqual(p1["state_version"], 0)

        res_meal1 = s1.log_meal(
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
        today2 = s2.get_today("2026-09-04")
        self.assertEqual(today2["state_version"], 1)
        self.assertEqual(today2["nutrition"]["meal_count"], 1)
        self.assertEqual(today2["nutrition"]["kcal_low"], 180)
        self.assertFalse(today2["plan_status"]["missing_data"])

        # Session 3: Add lunch
        s3 = CyberHealthService(self.db_path)
        res_meal2 = s3.log_meal(
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
        final_today = s5.get_today("2026-09-04")
        self.assertEqual(final_today["state_version"], 3)
        # Total active meals = breakfast (180-220) + revised lunch (250-300) = 430-520 kcal
        self.assertEqual(final_today["nutrition"]["meal_count"], 2)
        self.assertEqual(final_today["nutrition"]["kcal_low"], 430)
        self.assertEqual(final_today["nutrition"]["kcal_high"], 520)

        trail = s5.get_audit_trail()
        self.assertEqual(len(trail), 3)

    def test_timezone_aware_day_aggregation(self) -> None:
        """Occurred_at timestamps in UTC must be converted to user timezone (Asia/Shanghai) for day calculation."""
        # User profile timezone defaults to Asia/Shanghai (UTC+8)
        # 2026-09-03T16:30:00Z -> In Shanghai (+8h) this is 2026-09-04T00:30:00+08:00 (i.e. Sept 4)
        self.service.log_meal(
            occurred_at="2026-09-03T16:30:00Z",
            meal_type="late_snack",
            foods=[],
            kcal_low=150,
            kcal_high=200,
            idempotency_key="dave-snack-1",
        )

        # 2026-09-04T15:30:00Z -> In Shanghai (+8h) this is 2026-09-04T23:30:00+08:00 (i.e. Sept 4)
        self.service.log_meal(
            occurred_at="2026-09-04T15:30:00Z",
            meal_type="late_snack_2",
            foods=[],
            kcal_low=100,
            kcal_high=150,
            idempotency_key="dave-snack-2",
        )

        # Sept 3 in Shanghai should have 0 meals
        sept3 = self.service.get_today("2026-09-03")
        self.assertEqual(sept3["nutrition"]["meal_count"], 0)
        self.assertTrue(sept3["plan_status"]["missing_data"])

        # Sept 4 in Shanghai should aggregate both meals
        sept4 = self.service.get_today("2026-09-04")
        self.assertEqual(sept4["nutrition"]["meal_count"], 2)
        self.assertEqual(sept4["nutrition"]["kcal_low"], 250)
        self.assertEqual(sept4["nutrition"]["kcal_high"], 350)
        self.assertFalse(sept4["plan_status"]["missing_data"])

    def test_validation_errors(self) -> None:
        """Invalid inputs (e.g. kcal_high < kcal_low) must raise ValidationError."""
        with self.assertRaises(ValidationError):
            self.service.log_meal(
                occurred_at="2026-09-04T12:00:00+08:00",
                meal_type="lunch",
                foods=[],
                kcal_low=500,
                kcal_high=400,  # Invalid range
                idempotency_key="eva-invalid",
            )


class TodaySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")

    def test_today_uses_read_transaction(self):
        traced = []
        original = self.service.store.connect
        def connect():
            conn = original()
            conn.set_trace_callback(traced.append)
            return conn
        self.service.store.connect = connect
        self.service.get_today("2026-09-04")
        self.assertTrue(any(stmt.upper().startswith("BEGIN") for stmt in traced), traced)


class TodayTotalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cyber-health.sqlite3"
        self.store = SQLiteStore(self.db_path)
        self.service = CyberHealthService(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_today_totals_date_bounds(self) -> None:
        # Log meals across multiple days
        self.service.log_meal(
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
        res = self.service.get_today(day="2026-09-20")
        today = res["data"]["nutrition"]
        self.assertEqual(today["meal_count"], 1)
        self.assertEqual(today["kcal_low"], 600)
        self.assertEqual(today["protein_low"], 40)

        # Query 2026-09-10
        res10 = self.service.get_today(day="2026-09-10")
        today10 = res10["data"]["nutrition"]
        self.assertEqual(today10["meal_count"], 1)
        self.assertEqual(today10["kcal_low"], 400)
        self.assertEqual(today10["protein_low"], 10)

        # Query 2026-09-15 (empty)
        res15 = self.service.get_today(day="2026-09-15")
        self.assertEqual(res15["data"]["nutrition"]["meal_count"], 0)

    def test_get_today_rollback_preserves_root_exception(self) -> None:
        # Induce an intentional exception inside get_today by mocking _today_totals
        with patch.object(self.service, "_today_totals", side_effect=ZeroDivisionError("simulated root error")):
            with self.assertRaises(ZeroDivisionError) as ctx:
                self.service.get_today(day="2026-09-20")
            self.assertEqual(str(ctx.exception), "simulated root error")

    def test_log_meal_audit_fields_recorded_in_payload(self) -> None:
        res = self.service.log_meal(
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


if __name__ == "__main__":
    unittest.main()
