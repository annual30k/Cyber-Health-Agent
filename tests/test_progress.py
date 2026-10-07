"""Weight trend against the goal and the weekly progress review."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from cyber_health import CyberHealthService, ValidationError
from cyber_health.domain.progress import classify_goal

END = date(2026, 10, 7)


class ProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = CyberHealthService(Path(tmp.name) / "progress.sqlite3")

    def goal(self, goal_type: str | None, **targets) -> None:
        goals = {"goal_type": goal_type, **targets} if goal_type else targets
        self.service.update_profile(goals=goals, idempotency_key=f"goal-{goal_type}-{len(targets)}")

    def weigh(self, start_kg: float, pct_per_week: float, days: int = 21, every: int = 2) -> None:
        for i in range(0, days, every):
            day = END - timedelta(days=days - 1 - i)
            weight = start_kg * (1 + pct_per_week / 100 * i / 7)
            self.service.log_daily_metrics(
                date=day.isoformat(), metrics={"weight_kg": round(weight, 3)}, idempotency_key=f"w-{day}"
            )

    def trend(self) -> dict:
        return self.service.get_weight_trend(date=END.isoformat())["data"]

    def test_goal_text_is_classified_in_chinese_and_english(self) -> None:
        self.assertEqual(classify_goal("减脂"), "fat_loss")
        self.assertEqual(classify_goal("Muscle gain"), "muscle_gain")
        self.assertEqual(classify_goal("维持体重"), "maintain")
        self.assertIsNone(classify_goal("提升体能表现"))
        self.assertIsNone(classify_goal(None))

    def test_too_few_weigh_ins_never_produce_a_rate(self) -> None:
        self.goal("减脂")
        self.weigh(80, -0.7, days=5, every=2)
        trend = self.trend()
        self.assertEqual(trend["assessment"], "insufficient_data")
        self.assertIsNone(trend["weekly_change_pct"])
        self.assertIsNone(trend["suggested_target_adjustment"])

    def test_fat_loss_on_track(self) -> None:
        self.goal("减脂")
        self.weigh(80, -0.7)
        trend = self.trend()
        self.assertEqual(trend["assessment"], "on_track")
        self.assertAlmostEqual(trend["weekly_change_pct"], -0.7, delta=0.05)
        self.assertIsNone(trend["suggested_target_adjustment"])

    def test_slow_fat_loss_suggests_but_never_applies_a_calorie_cut(self) -> None:
        self.goal("减脂", target_kcal_low=1700, target_kcal_high=1900)
        self.weigh(80, -0.2)
        trend = self.trend()
        self.assertEqual(trend["assessment"], "slower_than_target")
        self.assertEqual(trend["suggested_target_adjustment"]["kcal_per_day_delta_range"], [-200, -100])
        self.assertTrue(trend["suggested_target_adjustment"]["requires_user_confirmation"])
        self.assertFalse(trend["suggested_target_adjustment"]["applied"])
        goals = self.service.get_profile()["goals"]
        self.assertEqual((goals["target_kcal_low"], goals["target_kcal_high"]), (1700, 1900))

    def test_too_fast_fat_loss_suggests_eating_more(self) -> None:
        self.goal("减脂")
        self.weigh(80, -1.6)
        trend = self.trend()
        self.assertEqual(trend["assessment"], "faster_than_target")
        self.assertEqual(trend["suggested_target_adjustment"]["kcal_per_day_delta_range"], [100, 200])

    def test_slow_muscle_gain_suggests_eating_more(self) -> None:
        self.goal("增肌")
        self.weigh(70, 0.05)
        trend = self.trend()
        self.assertEqual(trend["assessment"], "slower_than_target")
        self.assertEqual(trend["suggested_target_adjustment"]["kcal_per_day_delta_range"], [100, 200])

    def test_maintain_drift_is_reported_without_a_suggestion(self) -> None:
        self.goal("维持")
        self.weigh(65, 0.6)
        trend = self.trend()
        self.assertEqual(trend["assessment"], "drifting_up")
        self.assertIsNone(trend["suggested_target_adjustment"])

    def test_unclassified_goal_reports_trend_only(self) -> None:
        self.goal("提升体能")
        self.weigh(75, -0.7)
        trend = self.trend()
        self.assertEqual(trend["assessment"], "goal_unclassified")
        self.assertIsNotNone(trend["weekly_change_pct"])
        self.assertIsNone(trend["goal_band_pct_per_week"])

    def test_weekly_review_discloses_gaps_and_averages_only_logged_days(self) -> None:
        self.goal("减脂", target_kcal_low=1700, target_kcal_high=1900, target_protein_low=120)
        for offset, kcal in ((0, 1800), (1, 2400), (3, 1500)):
            day = (END - timedelta(days=offset)).isoformat()
            self.service.log_meal(
                occurred_at=f"{day}T12:00:00+08:00", meal_type="lunch", foods=[], kcal_low=kcal - 100,
                kcal_high=kcal + 100, protein_low=110, protein_high=150, idempotency_key=f"meal-{day}",
            )
        self.service.complete_workout(date=END.isoformat(), completion_rate=1.0, idempotency_key="workout")
        self.service.log_daily_metrics(date=END.isoformat(), metrics={"sleep_hours": 5.5}, idempotency_key="sleep")

        data = self.service.weekly_review(date=END.isoformat())["data"]

        nutrition = data["nutrition"]
        self.assertEqual(nutrition["days_logged"], 3)
        self.assertEqual(nutrition["avg_kcal_mid_on_logged_days"], 1900.0)
        self.assertEqual(
            (nutrition["days_in_kcal_target"], nutrition["days_above_kcal_target"], nutrition["days_below_kcal_target"]),
            (1, 1, 1),
        )
        self.assertEqual(nutrition["days_protein_target_met"], 3)
        self.assertEqual(nutrition["days_main_meals_complete"], 0)
        self.assertEqual(data["workouts"]["days_completed"], 1)
        self.assertEqual(data["workouts"]["days_unrecorded"], 6)
        self.assertEqual(data["recovery"]["days_sleep_under_6h"], 1)
        self.assertEqual(len(data["data_gaps"]["days_without_meals"]), 4)
        self.assertEqual(data["period"], {"start": "2026-10-01", "end": "2026-10-07", "days": 7})
        self.assertTrue(any("3/7" in line for line in data["highlights"]))

    def test_progress_reads_never_mutate_state(self) -> None:
        self.goal("减脂")
        self.weigh(80, -0.7)
        before = self.service.get_profile()["state_version"]
        with self.service.store.connect() as conn:
            ops_before = conn.execute("SELECT COUNT(*) FROM operation_log").fetchone()[0]
        self.service.get_weight_trend(date=END.isoformat())
        self.service.weekly_review(date=END.isoformat(), days=14)
        with self.service.store.connect() as conn:
            ops_after = conn.execute("SELECT COUNT(*) FROM operation_log").fetchone()[0]
        self.assertEqual(self.service.get_profile()["state_version"], before)
        self.assertEqual(ops_after, ops_before)

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.weekly_review(date="2026-02-30")
        with self.assertRaises(ValidationError):
            self.service.weekly_review(date=END.isoformat(), days=3)
        with self.assertRaises(ValidationError):
            self.service.get_weight_trend(date=END.isoformat(), window_days=2)


if __name__ == "__main__":
    unittest.main()
