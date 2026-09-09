"""Independent specification regressions. Temporary databases only."""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from cyber_health import CyberHealthService, ValidationError


class CodexReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "review.sqlite3")

    def meal(self, key, timestamp, kcal=100):
        return self.service.log_meal(user_id="u", occurred_at=timestamp,
            meal_type="breakfast", foods=[], kcal_low=kcal, kcal_high=kcal + 10,
            idempotency_key=key)

    def test_profile_write_requires_idempotency_key(self):
        with self.assertRaises((ValidationError, TypeError)):
            self.service.update_profile(user_id="u", goals={"goal": "maintain"})

    def test_calendar_date_must_be_real(self):
        with self.assertRaises(ValidationError):
            self.service.log_daily_metrics(user_id="u", date="2026-99-99",
                metrics={"sleep_hours": 8}, idempotency_key="invalid-date")

    def test_food_amount_range_cannot_be_inverted(self):
        with self.assertRaises(ValidationError):
            self.service.log_meal(user_id="u", occurred_at="2026-09-04T08:00:00+08:00",
                meal_type="breakfast", foods=[{"name": "rice", "amount_g": {"low": 200, "high": 100}}],
                kcal_low=100, kcal_high=200, idempotency_key="bad-food")

    def test_yesterday_repeat_does_not_copy_today(self):
        self.meal("yesterday", "2026-09-03T08:00:00+08:00", 100)
        self.meal("today", "2026-09-04T08:00:00+08:00", 500)
        repeated = self.service.log_meal(user_id="u", occurred_at="2026-09-04T09:00:00+08:00",
            meal_type="breakfast", repeat_meal="yesterday_breakfast", idempotency_key="repeat")
        self.assertEqual(repeated["data"]["today_totals"]["kcal_low"], 600)

    def test_workout_red_flag_is_persisted(self):
        self.service.log_workout(user_id="u", date="2026-09-04",
            discomfort_notes="严重胸痛", idempotency_key="red-flag")
        self.assertEqual(self.service.get_profile("u")["safety_mode"], "restricted")

    def test_restricted_plan_never_prescribes_strength_training(self):
        self.service.log_daily_metrics(user_id="u", date="2026-09-04",
            metrics={"notes": "严重胸痛"}, idempotency_key="red")
        try:
            result = self.service.plan_tomorrow(user_id="u", date="2026-09-05", idempotency_key="plan")
        except Exception as error:
            self.assertEqual(getattr(error, "code", None), "SAFETY_RESTRICTED")
        else:
            self.assertNotIn("标准力量训练", str(result))

    def test_schedule_compares_instants_not_iso_strings(self):
        with self.service.store.transaction() as conn:
            conn.execute("""INSERT INTO schedule_event(event_id, user_id, event_type,
                window_start, window_end, status, created_at, updated_at)
                VALUES ('e', 'u', 'DAILY_REVIEW', '2026-09-04T08:00:00+08:00',
                '2026-09-04T09:00:00+08:00', 'pending', '2026-09-04', '2026-09-04')""")
        events = self.service.get_schedule("u", now=datetime(2026, 9, 4, 2, tzinfo=UTC))
        self.assertEqual(events[0]["status"], "overdue")

    def test_correction_requires_expected_version(self):
        first = self.meal("first", "2026-09-04T08:00:00+08:00")
        with self.assertRaises(ValidationError):
            self.service.log_meal(user_id="u", occurred_at="2026-09-04T08:00:00+08:00",
                meal_type="breakfast", kcal_low=50, kcal_high=60,
                target_meal_id=first["data"]["meal_id"], idempotency_key="correction")
