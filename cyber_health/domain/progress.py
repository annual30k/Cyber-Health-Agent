"""Weight trend against the goal and the weekly progress review (both read-only)."""

from __future__ import annotations

import json
from datetime import date as Date
from datetime import datetime, timedelta
from typing import Any

from ..errors import ValidationError
from ..models import WeeklyReviewInput, WeightTrendInput
from .base import _MALFORMED_RECORD_ERRORS, OWNER_ID, ServiceCore

# Weekly change as a percentage of body weight. General coaching reference ranges, not
# medical targets; they only label the observed trend.
GOAL_RATE_BANDS_PCT: dict[str, tuple[float, float]] = {
    "fat_loss": (-1.0, -0.5),
    "muscle_gain": (0.25, 0.5),
    "maintain": (-0.25, 0.25),
}
_GOAL_KEYWORDS = (
    ("fat_loss", ("减脂", "减重", "减肥", "fat", "lose", "loss", "cut")),
    ("muscle_gain", ("增肌", "增重", "muscle", "gain", "bulk")),
    ("maintain", ("维持", "保持", "maintain", "maintenance")),
)
MIN_WEIGH_INS = 3
MIN_SPAN_DAYS = 7
WEIGHING_NOTE = "体重受水分、盐分和排便影响，单日波动不代表趋势；固定在晨起空腹时称重，趋势更可靠。"


def classify_goal(goal_type: Any) -> str | None:
    text = str(goal_type or "").lower()
    for goal, words in _GOAL_KEYWORDS:
        if any(word in text for word in words):
            return goal
    return None


def _day(value: str) -> Date:
    return datetime.strptime(value, "%Y-%m-%d").date()


class ProgressMixin(ServiceCore):
    """Weight trend against the goal and the weekly progress review (both read-only)."""

    def _weight_points(self, conn: Any, start: str, end: str) -> list[tuple[str, float]]:
        rows = conn.execute(
            """SELECT day, body_json FROM domain_record
               WHERE user_id = ? AND kind = 'daily_state' AND status = 'active' AND day >= ? AND day <= ?
               ORDER BY day ASC, created_at ASC""",
            (OWNER_ID, start, end),
        ).fetchall()
        points: dict[str, float] = {}
        for row in rows:
            try:
                weight = float(json.loads(row["body_json"]).get("metrics", {}).get("weight_kg"))
            except (*_MALFORMED_RECORD_ERRORS, TypeError):
                continue
            if 20.0 <= weight <= 500.0:
                points[row["day"]] = weight  # the latest record for a day wins
        return sorted(points.items())

    def _weight_trend(self, conn: Any, goals: dict[str, Any], end: str, window_days: int) -> dict[str, Any]:
        end_day = _day(end)
        start = (end_day - timedelta(days=window_days - 1)).isoformat()
        points = self._weight_points(conn, start, end)
        goal = classify_goal(goals.get("goal_type"))
        band = GOAL_RATE_BANDS_PCT.get(goal) if goal else None

        series = []
        for day, weight in points:
            current = _day(day)
            recent = [w for d, w in points if 0 <= (current - _day(d)).days <= 6]
            series.append({"date": day, "weight_kg": weight, "avg_7d": round(sum(recent) / len(recent), 2)})
        last_week = [w for d, w in points if (end_day - _day(d)).days <= 6]

        result: dict[str, Any] = {
            "window": {"start": start, "end": end, "days": window_days},
            "weigh_in_count": len(points),
            "series": series,
            "latest_avg_7d_kg": round(sum(last_week) / len(last_week), 2) if last_week else None,
            "weekly_change_kg": None,
            "weekly_change_pct": None,
            "goal": goal,
            "goal_band_pct_per_week": list(band) if band else None,
            "assessment": "insufficient_data",
            "guidance": None,
            "suggested_target_adjustment": None,
            "note": WEIGHING_NOTE,
        }
        span = (_day(points[-1][0]) - _day(points[0][0])).days if points else 0
        if len(points) < MIN_WEIGH_INS or span < MIN_SPAN_DAYS:
            result["guidance"] = (
                f"至少需要跨度 {MIN_SPAN_DAYS} 天以上的 {MIN_WEIGH_INS} 次体重记录才能判断趋势"
                f"（当前 {len(points)} 次，跨度 {span} 天）。"
            )
            return result

        # Least-squares slope over the recorded days; missing days are simply absent.
        xs = [(_day(d) - _day(points[0][0])).days for d, _ in points]
        ys = [w for _, w in points]
        mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sum((x - mean_x) ** 2 for x in xs)
        weekly_kg = slope * 7
        weekly_pct = weekly_kg / mean_y * 100
        result["weekly_change_kg"] = round(weekly_kg, 2)
        result["weekly_change_pct"] = round(weekly_pct, 2)

        if band is None:
            result["assessment"] = "goal_unclassified"
            result["guidance"] = "目标类型未设置或无法识别（减脂/增肌/维持），只报告趋势，不做达标判断。"
            return result

        low, high = band
        if low <= weekly_pct <= high:
            result["assessment"] = "on_track"
            result["guidance"] = f"每周变化 {weekly_pct:+.2f}%，在{_GOAL_LABELS[goal]}参考区间 {low:+.2f}% ~ {high:+.2f}% 内。"
            return result

        slower = weekly_pct > high if goal == "fat_loss" else weekly_pct < low
        if goal == "maintain":
            result["assessment"] = "drifting_up" if weekly_pct > high else "drifting_down"
            result["guidance"] = f"每周变化 {weekly_pct:+.2f}%，超出维持区间 ±0.25%。先确认记录是否完整，再决定是否调整热量。"
            return result
        result["assessment"] = "slower_than_target" if slower else "faster_than_target"
        direction = -1 if goal == "fat_loss" else 1
        if slower:
            delta = [100 * direction, 200 * direction]
            result["guidance"] = (
                f"每周变化 {weekly_pct:+.2f}%，慢于{_GOAL_LABELS[goal]}参考区间。先确认饮食记录是否完整；"
                "若持续两周以上，可考虑调整每日热量目标（需要你确认后才会修改）。"
            )
        else:
            delta = [-100 * direction, -200 * direction]
            result["guidance"] = (
                f"每周变化 {weekly_pct:+.2f}%，快于{_GOAL_LABELS[goal]}参考区间，可能影响恢复和训练表现；"
                "可考虑放缓速度（需要你确认后才会修改目标）。"
            )
        result["suggested_target_adjustment"] = {
            "kcal_per_day_delta_range": sorted(delta),
            "requires_user_confirmation": True,
            "applied": False,
        }
        return result

    def get_weight_trend(self, *, date: str, window_days: int = 28) -> dict[str, Any]:
        """Read-only weight trend for the window ending on ``date``, judged against the goal."""
        try:
            validated = WeightTrendInput(date=date, window_days=window_days)
        except Exception as err:
            raise ValidationError(str(err)) from err
        with self.store.connect() as conn:
            profile = conn.execute(
                "SELECT goals_json, state_version FROM user_profile WHERE user_id = ?", (OWNER_ID,)
            ).fetchone()
            goals = json.loads(profile["goals_json"]) if profile and profile["goals_json"] else {}
            trend = self._weight_trend(conn, goals, validated.date, validated.window_days)
        version = profile["state_version"] if profile else 0
        return self._response(f"op_read_weight_{validated.date}_{version}", "success", trend, version)

    def weekly_review(self, *, date: str, days: int = 7) -> dict[str, Any]:
        """Read-only review of the ``days`` ending on ``date``; unrecorded days are never zeros."""
        try:
            validated = WeeklyReviewInput(date=date, days=days)
        except Exception as err:
            raise ValidationError(str(err)) from err
        end_day = _day(validated.date)
        day_list = [(end_day - timedelta(days=offset)).isoformat() for offset in range(validated.days - 1, -1, -1)]

        with self.store.connect() as conn:
            profile = conn.execute(
                "SELECT timezone, goals_json, state_version FROM user_profile WHERE user_id = ?", (OWNER_ID,)
            ).fetchone()
            tz_name = profile["timezone"] if profile and profile["timezone"] else "Asia/Shanghai"
            goals = json.loads(profile["goals_json"]) if profile and profile["goals_json"] else {}
            targets = self._resolve_nutrition_targets(goals)
            daily: list[dict[str, Any]] = []
            for day in day_list:
                totals = self._today_totals(conn, day, tz_name)
                facts = self._daily_review_facts(conn, day, tz_name)
                state = conn.execute(
                    """SELECT body_json FROM domain_record WHERE user_id = ? AND kind = 'daily_state'
                       AND day = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1""",
                    (OWNER_ID, day),
                ).fetchone()
                metrics: dict[str, Any] = {}
                recovery = None
                if state:
                    try:
                        body = json.loads(state["body_json"])
                        metrics, recovery = dict(body.get("metrics") or {}), body.get("recovery_score")
                    except _MALFORMED_RECORD_ERRORS:
                        pass
                logged = totals["meal_count"] > 0
                daily.append({
                    "date": day,
                    "meals_logged": totals["meal_count"],
                    "main_meals_complete": not facts["unverified_meal_types"],
                    "kcal_mid": round((totals["kcal_low"] + totals["kcal_high"]) / 2) if logged else None,
                    "protein_mid": round((totals["protein_low"] + totals["protein_high"]) / 2) if logged else None,
                    "workout_status": facts["workout_status"],
                    "sleep_hours": metrics.get("sleep_hours"),
                    "fatigue_level": metrics.get("fatigue_level"),
                    "recovery_score": recovery,
                    "weight_kg": metrics.get("weight_kg"),
                })
            trend = self._weight_trend(conn, goals, validated.date, max(28, validated.days))

        data = _summarize_week(daily, targets, trend)
        data["period"] = {"start": day_list[0], "end": day_list[-1], "days": validated.days}
        version = profile["state_version"] if profile else 0
        return self._response(f"op_read_weekly_{validated.date}_{validated.days}_{version}", "success", data, version)


_GOAL_LABELS = {"fat_loss": "减脂", "muscle_gain": "增肌", "maintain": "维持"}


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _summarize_week(daily: list[dict[str, Any]], targets: dict[str, Any] | None, trend: dict[str, Any]) -> dict[str, Any]:
    total = len(daily)
    logged = [d for d in daily if d["kcal_mid"] is not None]
    nutrition: dict[str, Any] = {
        "days_logged": len(logged),
        "days_main_meals_complete": sum(1 for d in daily if d["main_meals_complete"]),
        "avg_kcal_mid_on_logged_days": _mean([d["kcal_mid"] for d in logged]),
        "avg_protein_mid_on_logged_days": _mean([d["protein_mid"] for d in logged]),
        "target_kcal_range": targets["kcal_range"] if targets else None,
        "days_in_kcal_target": None,
        "days_above_kcal_target": None,
        "days_below_kcal_target": None,
        "protein_target_g": targets["protein_target_g"] if targets else None,
        "days_protein_target_met": None,
    }
    if targets:
        low, high = targets["kcal_range"]
        nutrition["days_in_kcal_target"] = sum(1 for d in logged if low <= d["kcal_mid"] <= high)
        nutrition["days_above_kcal_target"] = sum(1 for d in logged if d["kcal_mid"] > high)
        nutrition["days_below_kcal_target"] = sum(1 for d in logged if d["kcal_mid"] < low)
        if targets["protein_target_g"] is not None:
            nutrition["days_protein_target_met"] = sum(1 for d in logged if d["protein_mid"] >= targets["protein_target_g"])

    workouts = {
        "days_completed": sum(1 for d in daily if d["workout_status"] == "completed"),
        "days_partial_or_rest_unconfirmed": sum(1 for d in daily if d["workout_status"] == "partial_or_rest_unconfirmed"),
        "days_unrecorded": sum(1 for d in daily if d["workout_status"] == "unrecorded"),
    }
    sleeps = [float(d["sleep_hours"]) for d in daily if d["sleep_hours"] is not None]
    fatigue = [float(d["fatigue_level"]) for d in daily if d["fatigue_level"] is not None]
    recovery = {
        "days_with_sleep": len(sleeps),
        "avg_sleep_hours": _mean(sleeps),
        "days_sleep_under_6h": sum(1 for h in sleeps if h < 6.0),
        "avg_fatigue_level": _mean(fatigue),
    }

    highlights = [f"饮食记录 {len(logged)}/{total} 天，其中三餐完整 {nutrition['days_main_meals_complete']} 天。"]
    if logged:
        line = f"记录日平均摄入约 {round(nutrition['avg_kcal_mid_on_logged_days'])} kcal"
        if targets:
            line += f"（目标 {targets['kcal_range'][0]}–{targets['kcal_range'][1]}），{nutrition['days_in_kcal_target']} 天在目标内"
        highlights.append(line + "。")
    if nutrition["days_protein_target_met"] is not None and logged:
        highlights.append(f"蛋白质达标 {nutrition['days_protein_target_met']}/{len(logged)} 个记录日。")
    highlights.append(f"完成训练 {workouts['days_completed']} 天，{workouts['days_unrecorded']} 天没有训练或休息记录。")
    if sleeps:
        highlights.append(f"平均睡眠 {recovery['avg_sleep_hours']} 小时，{recovery['days_sleep_under_6h']} 天少于 6 小时。")
    if trend["weekly_change_pct"] is not None:
        highlights.append(f"体重趋势每周 {trend['weekly_change_kg']:+.2f} kg（{trend['weekly_change_pct']:+.2f}%）。{trend['guidance']}")
    else:
        highlights.append(trend["guidance"])

    return {
        "nutrition": nutrition,
        "workouts": workouts,
        "recovery": recovery,
        "weight_trend": trend,
        "daily": daily,
        "data_gaps": {
            "days_without_meals": [d["date"] for d in daily if d["kcal_mid"] is None],
            "days_without_workout_record": [d["date"] for d in daily if d["workout_status"] == "unrecorded"],
        },
        "highlights": highlights,
        "note": "未记录的日子不计入平均值，也不当作零摄入或休息日。",
    }
