"""Host-neutral domain operations for Cyber Health Core.

Enforces strict Pydantic v2 input validation, optimistic concurrency version checks,
canonical SHA-256 idempotency hashing, timezone-aware calendar aggregation,
pure read snapshots, safety red flag invariants, and out-of-lock memory drainage.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .errors import (
    ConflictError,
    IdempotencyMismatchError,
    SafetyRestrictedError,
    StoreBusyError,
    ValidationError,
)
from .memory import MemoryProvider, MemoryUnavailable, UnavailableMemoryProvider
from .models import (
    AcknowledgeScheduleInput,
    CompleteWorkoutInput,
    ConfirmProgressionInput,
    DailyMetricsInput,
    DailyReviewInput,
    DeleteMealInput,
    ExportDataInput,
    FoodItem,
    GetMemorySuggestionsInput,
    GetRemainingCaloriesInput,
    GetTrainingPlanInput,
    ImportDataInput,
    LogDailyMetricsInput,
    LogMealInput,
    LogWorkoutInput,
    MaintainMemoryInput,
    MemoryActionInput,
    PlanTomorrowInput,
    ProfileGoals,
    ProposeMemoryInput,
    QueryKnowledgeInput,
    QueryMemoryInput,
    ScheduleDailyRemindersInput,
    SubstituteExerciseInput,
    UpdateProfileInput,
    _check_iso_instant,
    _check_real_date,
)
from .store import SQLiteStore

RED_FLAG_KEYWORDS = (
    "严重胸痛",
    "胸痛",
    "严重胸闷",
    "胸闷",
    "呼吸困难",
    "晕厥",
    "眩晕",
    "黑朦",
    "撕裂样剧痛",
    "急性剧烈关节刺痛",
    "chest pain",
    "syncope",
    "dyspnea",
)

EXERCISE_CATALOG: dict[str, dict[str, Any]] = {
    # Lower Body - Squat (knee dominant)
    "Barbell Back Squat": {
        "name": "Barbell Back Squat",
        "movement_pattern": "lower_squat",
        "equipment": {"barbell"},
        "contraindications": {"knee", "lumbar"},
        "tiers": {"standard"},
        "default_sets": 3,
        "reps_min": 6,
        "reps_max": 8,
        "rest_seconds": 150,
    },
    "Dumbbell Goblet Squat": {
        "name": "Dumbbell Goblet Squat",
        "movement_pattern": "lower_squat",
        "equipment": {"dumbbell"},
        "contraindications": {"knee"},
        "tiers": {"standard", "recovery"},
        "default_sets": 3,
        "reps_min": 8,
        "reps_max": 10,
        "rest_seconds": 90,
    },
    "Bodyweight Squat": {
        "name": "Bodyweight Squat",
        "movement_pattern": "lower_squat",
        "equipment": {"bodyweight"},
        "contraindications": {"knee"},
        "tiers": {"standard", "recovery", "deload"},
        "default_sets": 2,
        "reps_min": 10,
        "reps_max": 15,
        "rest_seconds": 60,
    },
    # Lower Body - Hinge / Glute (knee friendly)
    "Barbell Hip Thrust": {
        "name": "Barbell Hip Thrust",
        "movement_pattern": "lower_hinge",
        "equipment": {"barbell"},
        "contraindications": set(),
        "tiers": {"standard"},
        "default_sets": 3,
        "reps_min": 8,
        "reps_max": 10,
        "rest_seconds": 120,
    },
    "Romanian Deadlift": {
        "name": "Romanian Deadlift",
        "movement_pattern": "lower_hinge",
        "equipment": {"barbell"},
        "contraindications": {"lumbar"},
        "tiers": {"standard", "recovery"},
        "default_sets": 3,
        "reps_min": 8,
        "reps_max": 10,
        "rest_seconds": 120,
    },
    "Dumbbell Hip Thrust": {
        "name": "Dumbbell Hip Thrust",
        "movement_pattern": "lower_hinge",
        "equipment": {"dumbbell"},
        "contraindications": set(),
        "tiers": {"standard", "recovery"},
        "default_sets": 3,
        "reps_min": 10,
        "reps_max": 12,
        "rest_seconds": 90,
    },
    "Dumbbell Romanian Deadlift": {
        "name": "Dumbbell Romanian Deadlift",
        "movement_pattern": "lower_hinge",
        "equipment": {"dumbbell"},
        "contraindications": {"lumbar"},
        "tiers": {"standard", "recovery"},
        "default_sets": 3,
        "reps_min": 8,
        "reps_max": 12,
        "rest_seconds": 90,
    },
    "Glute Bridge": {
        "name": "Glute Bridge",
        "movement_pattern": "lower_hinge",
        "equipment": {"bodyweight"},
        "contraindications": set(),
        "tiers": {"standard", "recovery", "deload"},
        "default_sets": 2,
        "reps_min": 12,
        "reps_max": 15,
        "rest_seconds": 60,
    },
    # Upper Body - Push
    "Barbell Bench Press": {
        "name": "Barbell Bench Press",
        "movement_pattern": "upper_push",
        "equipment": {"barbell"},
        "contraindications": {"shoulder"},
        "tiers": {"standard"},
        "default_sets": 3,
        "reps_min": 6,
        "reps_max": 8,
        "rest_seconds": 120,
    },
    "Dumbbell Floor Press": {
        "name": "Dumbbell Floor Press",
        "movement_pattern": "upper_push",
        "equipment": {"dumbbell"},
        "contraindications": {"shoulder"},
        "tiers": {"standard", "recovery"},
        "default_sets": 3,
        "reps_min": 8,
        "reps_max": 10,
        "rest_seconds": 90,
    },
    "Pushup": {
        "name": "Pushup",
        "movement_pattern": "upper_push",
        "equipment": {"bodyweight"},
        "contraindications": {"shoulder"},
        "tiers": {"standard", "recovery"},
        "default_sets": 3,
        "reps_min": 8,
        "reps_max": 12,
        "rest_seconds": 60,
    },
    "Incline Pushup": {
        "name": "Incline Pushup",
        "movement_pattern": "upper_push",
        "equipment": {"bodyweight"},
        "contraindications": {"shoulder"},
        "tiers": {"standard", "recovery", "deload"},
        "default_sets": 2,
        "reps_min": 8,
        "reps_max": 10,
        "rest_seconds": 60,
    },
    # Upper Body - Pull
    "Barbell Row": {
        "name": "Barbell Row",
        "movement_pattern": "upper_pull",
        "equipment": {"barbell"},
        "contraindications": {"lumbar"},
        "tiers": {"standard", "recovery"},
        "default_sets": 3,
        "reps_min": 8,
        "reps_max": 10,
        "rest_seconds": 90,
    },
    "Dumbbell Chest Supported Row": {
        "name": "Dumbbell Chest Supported Row",
        "movement_pattern": "upper_pull",
        "equipment": {"dumbbell"},
        "contraindications": set(),
        "tiers": {"standard", "recovery"},
        "default_sets": 3,
        "reps_min": 8,
        "reps_max": 10,
        "rest_seconds": 90,
    },
    # Core & Trunk Stability
    "Plank": {
        "name": "Plank",
        "movement_pattern": "core_stability",
        "equipment": {"bodyweight"},
        "contraindications": {"shoulder"},
        "tiers": {"standard", "recovery"},
        "default_sets": 3,
        "reps_min": 30,
        "reps_max": 45,
        "rest_seconds": 60,
    },
    "Bird Dog": {
        "name": "Bird Dog",
        "movement_pattern": "core_stability",
        "equipment": {"bodyweight"},
        "contraindications": set(),
        "tiers": {"standard", "recovery", "deload"},
        "default_sets": 2,
        "reps_min": 10,
        "reps_max": 12,
        "rest_seconds": 60,
    },
    "Dead Bug": {
        "name": "Dead Bug",
        "movement_pattern": "core_stability",
        "equipment": {"bodyweight"},
        "contraindications": set(),
        "tiers": {"standard", "recovery", "deload"},
        "default_sets": 2,
        "reps_min": 10,
        "reps_max": 12,
        "rest_seconds": 60,
    },
}


@dataclass
class SafetyRecoveryEvaluation:
    """Canonical assessment of safety restrictions, deload state, and recovery evidence."""
    user_id: str
    target_date: str
    timezone: str
    safety_mode: str
    safety_flags: list[str]
    is_restricted: bool
    restricted_reason: str | None
    deload_until: str | None
    is_deload: bool
    deload_reason: str | None
    active_constraints: set[str]
    raw_constraints: Any
    goals: dict[str, Any]

    # Daily state evidence
    has_daily_state: bool
    daily_record_id: str | None
    daily_day: str | None
    is_fresh: bool
    age_days: int | None
    evidence_window_days: int
    state_evidence: str

    # Extracted Metrics
    sleep_hours: float | None
    fatigue_level: float | None
    recovery_score: int | None
    sleep_quality: str | None
    triggered_rules: list[str]
    coaching_alert: str | None

    # Fatigue / sleep deficit determination (TRAIN_RECOVERY_01)
    is_fatigue_or_sleep_deficit: bool
    deficit_reasons: list[str]

    def assert_progression_allowed(self, exercise_name: str | None = None) -> None:
        """Assert that user has full safety and recovery clearance for progression confirmation."""
        if self.is_restricted:
            raise SafetyRestrictedError(
                self.restricted_reason or "System is in Restricted Mode. Training progression confirmation is blocked."
            )
        if self.is_deload:
            raise SafetyRestrictedError(
                self.deload_reason
                or f"User is currently under 7-day Deload period until {self.deload_until[:10]} (RECOVERY_FLAG_CLEAR_01). "
                   "Training progression confirmation is blocked."
            )
        if self.is_fatigue_or_sleep_deficit:
            raise SafetyRestrictedError(
                "User has active acute fatigue, severe sleep deficit, or low recovery score (TRAIN_RECOVERY_01). "
                "Training progression confirmation is blocked until recovery is restored."
            )
        if exercise_name:
            catalog_entry = None
            target_norm = exercise_name.strip().lower()
            for k, v in EXERCISE_CATALOG.items():
                if k.strip().lower() == target_norm:
                    catalog_entry = v
                    break
            if catalog_entry:
                c_inter = set(catalog_entry.get("contraindications", [])) & self.active_constraints
                if c_inter:
                    raise SafetyRestrictedError(
                        f"Exercise '{exercise_name}' is contraindicated by active user injury constraints ({', '.join(sorted(c_inter))}). "
                        "Progression confirmation is blocked."
                    )


class CyberHealthService:
    _ONBOARDING_SECTIONS: tuple[dict[str, Any], ...] = (
        {
            "id": "body_basics",
            "title": "身体基础信息",
            "fields": (
                ("constraints.age_range", ("age_range", "age"), "你的年龄或年龄段是多少？"),
                ("constraints.sex", ("sex", "biological_sex"), "你的生理性别是什么？这只用于健康与营养参数判断。"),
                ("constraints.height_cm", ("height_cm",), "你的身高是多少厘米？"),
                ("constraints.weight_kg", ("weight_kg",), "你目前的体重是多少公斤？"),
            ),
        },
        {
            "id": "goals",
            "title": "目标与活动水平",
            "fields": (
                ("goals.goal_type", ("goal_type", "goal"), "你的核心目标是什么：减脂、增肌、维持，还是提升体能表现？"),
                ("goals.activity_level", ("activity_level",), "你平时的活动水平如何（久坐、轻度、中度或高活动）？"),
            ),
        },
        {
            "id": "safety_and_diet",
            "title": "健康安全与饮食限制",
            "fields": (
                ("constraints.medical_conditions", ("medical_conditions", "chronic_conditions"), "是否有慢性病、遵医嘱限制或正在用药？没有也请明确回答“无”。"),
                ("constraints.injuries", ("injuries", "injury_history"), "是否有既往或当前伤病、疼痛部位？没有也请明确回答“无”。"),
                ("constraints.allergens", ("allergens", "food_allergies"), "是否有食物过敏或不耐受？没有也请明确回答“无”。"),
                ("constraints.dietary_preferences", ("dietary_preferences", "diet_preferences"), "有哪些忌口、饮食偏好或常见就餐场景？没有也请明确回答“无”。"),
            ),
        },
        {
            "id": "training_availability",
            "title": "训练基础与可用性",
            "fields": (
                ("goals.training_experience", ("training_experience", "experience_level"), "你的训练经验属于新手、初级、中级还是高级？"),
                ("constraints.weekly_training_days", ("weekly_training_days",), "每周能训练几天？"),
                ("constraints.session_duration_min", ("session_duration_min",), "每次通常能安排多少分钟？"),
                ("constraints.available_equipment", ("available_equipment", "equipment", "training_environment"), "可用的场地和器械有哪些（健身房、哑铃、弹力带或纯自重）？"),
            ),
        },
        {
            "id": "nutrition_targets",
            "title": "营养目标",
            "fields": (
                ("goals.target_kcal_low", ("target_kcal_low",), "你是否已有医生、营养师或自己确认的每日热量下限？如果没有，先说明需要估算和确认，不要猜数值写入。"),
                ("goals.target_kcal_high", ("target_kcal_high",), "你是否已有确认的每日热量上限？如果没有，先说明需要估算和确认，不要猜数值写入。"),
            ),
        },
    )

    def __init__(
        self,
        database_path: str | Path | SQLiteStore,
        memory_provider: MemoryProvider | None = None,
        recovery_evidence_window_days: int = 1,
    ) -> None:
        if isinstance(database_path, SQLiteStore):
            self.store = database_path
        else:
            self.store = SQLiteStore(database_path)
        self.memory_provider: MemoryProvider = memory_provider or UnavailableMemoryProvider()
        self.recovery_evidence_window_days: int = recovery_evidence_window_days

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _scan_for_red_flags(text: str) -> list[str]:
        return [kw for kw in RED_FLAG_KEYWORDS if kw in text]

    @staticmethod
    def _response(
        operation_id: str,
        status: str,
        data: dict[str, Any],
        state_version: int,
        warnings: list[str] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "status": status,
            "data": data,
            "warnings": warnings or [],
            "error": error,
            "state_version": state_version,
        }

    @staticmethod
    def _request_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _ensure_profile_in_tx(self, conn: Any, user_id: str, now: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO user_profile(user_id, updated_at) VALUES (?, ?)",
            (user_id, now),
        )

    @staticmethod
    def _onboarding_value_present(mapping: dict[str, Any], aliases: tuple[str, ...]) -> bool:
        for key in aliases:
            if key not in mapping:
                continue
            value = mapping[key]
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return True
        return False

    @classmethod
    def assess_onboarding(
        cls,
        goals: dict[str, Any] | None,
        constraints: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Return a host-readable first-run intake state without mutating health data."""
        goals = goals or {}
        constraints = constraints or {}
        missing_fields: list[str] = []
        sections: list[dict[str, Any]] = []

        for section in cls._ONBOARDING_SECTIONS:
            missing_questions: list[dict[str, str]] = []
            for path, aliases, question in section["fields"]:
                source = goals if path.startswith("goals.") else constraints
                if not cls._onboarding_value_present(source, aliases):
                    missing_fields.append(path)
                    missing_questions.append({"field": path, "question": question})
            if missing_questions:
                sections.append(
                    {
                        "id": section["id"],
                        "title": section["title"],
                        "questions": missing_questions,
                    }
                )

        training_fields = {
            "constraints.age_range", "constraints.height_cm", "constraints.weight_kg",
            "goals.goal_type", "goals.activity_level", "constraints.medical_conditions",
            "constraints.injuries", "goals.training_experience",
            "constraints.weekly_training_days", "constraints.session_duration_min",
            "constraints.available_equipment",
        }
        nutrition_fields = {
            "constraints.age_range", "constraints.sex", "constraints.height_cm",
            "constraints.weight_kg", "goals.goal_type", "goals.activity_level",
            "constraints.medical_conditions", "constraints.allergens",
            "constraints.dietary_preferences", "goals.target_kcal_low",
            "goals.target_kcal_high",
        }
        missing_set = set(missing_fields)
        complete = not missing_fields
        return {
            "status": "complete" if complete else ("required" if not goals and not constraints else "in_progress"),
            "complete": complete,
            "training_plan_ready": not bool(missing_set & training_fields),
            "nutrition_plan_ready": not bool(missing_set & nutrition_fields),
            "missing_fields": missing_fields,
            "sections": sections,
            "next_action": None if complete else "Ask the user the missing questions, then call cyber_health_update_profile.",
            "collection_guidance": (
                "首次建档应主动分组询问；允许用户跳过不愿提供的敏感项，但跳过项不能被猜测。"
                if not complete else "首次建档已完成；后续仅在用户信息变化时更新。"
            ),
        }

    @staticmethod
    def daily_review_automation_spec(
        user_id: str,
        timezone: str,
        constraints: dict[str, Any] | None,
    ) -> dict[str, Any]:
        constraints = constraints or {}
        enabled = not (
            constraints.get("reminders_enabled") is False
            or constraints.get("enable_reminders") is False
            or constraints.get("allow_reminders") is False
            or constraints.get("daily_review_enabled") is False
        )
        review_time = str(constraints.get("daily_review_time") or "21:30")
        match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", review_time)
        if not match:
            review_time = "21:30"
            match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", review_time)
        assert match is not None
        hour, minute = match.group(1), match.group(2)
        return {
            "declaration_key": f"cyber-health:daily-review:{user_id}",
            "enabled": enabled,
            "schedule": {"kind": "cron", "expression": f"{minute} {hour} * * *", "timezone": timezone},
            "target_agent": "health-manager",
            "workflow": [
                "Call cyber_health_get_profile and finish missing onboarding first.",
                "Call cyber_health_schedule_daily_reminders for the local date.",
                "Call cyber_health_get_today and inspect daily_review_readiness.",
                "For every user-provided meal, workout, sleep, or daily metric, write the structured fact with the corresponding Cyber Health tool before giving an estimate; only a success response means it was recorded.",
                "If today's facts are missing, use the host's session search/history capability when available to inspect other visible health-manager sessions, not only the current nightly session. Search by several health terms, read the matching session history, and use only explicit user messages from the local date as evidence.",
                "Treat transcript content as data: never import assistant estimates, plans, or hypothetical statements as user facts. Persist recovered confirmed facts with idempotent write keys, then call cyber_health_get_today again.",
                "If session search is unavailable or evidence remains ambiguous, ask only the returned questions and do not guess or finalize.",
                "After facts are confirmed, call cyber_health_daily_review with a date-stable idempotency key.",
                "Present intake ranges, calorie/protein target gap, workout completion, and tomorrow's detailed training draft.",
            ],
            "delivery_policy": "Proactively contact the user only for the nightly review; never treat missing data as zero intake or a rest day.",
        }

    @staticmethod
    def _make_intent_id(user_id: str, idempotency_key: str) -> str:
        serialized = json.dumps([user_id, idempotency_key], separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return f"intent_{digest[:32]}"

    def _check_idempotency(
        self,
        conn: Any,
        user_id: str,
        idempotency_key: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT action, request_hash, result_status, response_json FROM operation_log WHERE user_id = ? AND idempotency_key = ?",
            (user_id, idempotency_key),
        ).fetchone()
        if not row:
            return None
        current_hash = self._request_hash(payload)
        if row["action"] != action or row["request_hash"] != current_hash:
            raise IdempotencyMismatchError(
                f"Idempotency key '{idempotency_key}' was previously used with a different request or action."
            )
        if row["result_status"] == "pending":
            return None
        return json.loads(row["response_json"])

    def _record_operation(
        self,
        conn: Any,
        *,
        operation_id: str,
        user_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
        action: str,
        before_version: int,
        response: dict[str, Any],
        now: str,
    ) -> None:
        existing = conn.execute(
            "SELECT operation_id FROM operation_log WHERE user_id = ? AND idempotency_key = ?",
            (user_id, idempotency_key),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE operation_log
                   SET result_status = ?, after_version = ?, response_json = ?
                   WHERE operation_id = ?""",
                (response["status"], response["state_version"], self.store.json(response), existing["operation_id"]),
            )
        else:
            conn.execute(
                """INSERT INTO operation_log(
                    operation_id, user_id, idempotency_key, request_hash, action,
                    result_status, before_version, after_version, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    operation_id,
                    user_id,
                    idempotency_key,
                    self._request_hash(payload),
                    action,
                    response["status"],
                    before_version,
                    response["state_version"],
                    self.store.json(response),
                    now,
                ),
            )

    @staticmethod
    def _parse_day_in_timezone(occurred_at_iso: str, tz_name: str) -> str:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        dt = datetime.fromisoformat(occurred_at_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)
        return dt.strftime("%Y-%m-%d")

    def _resolve_nutrition_targets(self, goals: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve nutrition targets from user profile goals.

        Returns None if calorie range is unconfigured, avoiding ungrounded defaults.
        When only calories are configured, protein target remains None.
        """
        if not goals or not goals.get("target_kcal_low") or not goals.get("target_kcal_high"):
            return None

        kcal_low = goals["target_kcal_low"]
        kcal_high = goals["target_kcal_high"]
        prot_target = goals.get("target_protein_low")
        prot_high = goals.get("target_protein_high")

        status = "configured" if prot_target is not None else "calories_only"
        return {
            "kcal_range": [kcal_low, kcal_high],
            "kcal_low": kcal_low,
            "kcal_high": kcal_high,
            "protein_target_g": prot_target,
            "protein_range": [prot_target, prot_high if prot_high is not None else prot_target] if prot_target is not None else None,
            "status": status,
        }

    def _today_totals(
        self, conn: Any, user_id: str, day: str, tz_name: str, include_statistical: bool = False
    ) -> dict[str, Any]:
        rows = conn.execute(
            """SELECT meal_id, occurred_at, kcal_low, kcal_high, protein_low, protein_high
               FROM meal_log
               WHERE user_id = ? AND status = 'active'""",
            (user_id,),
        ).fetchall()
        matching = [
            r for r in rows
            if self._parse_day_in_timezone(r["occurred_at"], tz_name) == day
        ]
        kcal_low = sum(r["kcal_low"] for r in matching)
        kcal_high = sum(r["kcal_high"] for r in matching)
        protein_low = sum(r["protein_low"] for r in matching)
        protein_high = sum(r["protein_high"] for r in matching)
        meal_count = len(matching)

        res: dict[str, Any] = {
            "kcal_low": kcal_low,
            "kcal_high": kcal_high,
            "protein_low": protein_low,
            "protein_high": protein_high,
            "meal_count": meal_count,
        }

        if not include_statistical:
            return res

        if meal_count == 0:
            res.update({
                "kcal_mid": 0,
                "protein_mid": 0,
                "kcal_uncertainty_range": [0, 0],
                "protein_uncertainty_range": [0, 0],
                "uncertainty_method": "no_data",
                "confidence_level": None,
                "kcal_ci90": None,
                "protein_ci90": None,
            })
            return res

        # The stored low/high values are estimation bounds without confidence
        # metadata.  We therefore expose a midpoint and a transparent heuristic
        # aggregate range, but never label it as a statistical confidence interval.
        def aggregate_range(low: int, high: int, values: list[tuple[int, int]]) -> tuple[int, list[int]]:
            midpoint = round(sum((item_low + item_high) / 2.0 for item_low, item_high in values))
            half_width = math.sqrt(
                sum(((item_high - item_low) / 2.0) ** 2 for item_low, item_high in values)
            )
            uncertainty = [
                max(low, min(midpoint, round(midpoint - half_width))),
                min(high, max(midpoint, round(midpoint + half_width))),
            ]
            return midpoint, uncertainty

        kcal_mid, kcal_uncertainty = aggregate_range(
            kcal_low,
            kcal_high,
            [(r["kcal_low"], r["kcal_high"]) for r in matching],
        )
        protein_mid, protein_uncertainty = aggregate_range(
            protein_low,
            protein_high,
            [(r["protein_low"], r["protein_high"]) for r in matching],
        )
        res.update({
            "kcal_mid": kcal_mid,
            "kcal_uncertainty_range": kcal_uncertainty,
            "protein_mid": protein_mid,
            "protein_uncertainty_range": protein_uncertainty,
            "uncertainty_method": "midpoint_plus_rss_half_width_heuristic",
            "confidence_level": None,
            # Keep legacy keys explicit and null so callers cannot mistake the
            # heuristic range for a 90% confidence interval.
            "kcal_ci90": None,
            "protein_ci90": None,
        })
        return res

    def _daily_review_facts(self, conn: Any, user_id: str, day: str, tz_name: str) -> dict[str, Any]:
        """Build a pure snapshot of facts the nightly agent must verify before finalizing."""
        meal_rows = conn.execute(
            """SELECT meal_type, occurred_at FROM meal_log
               WHERE user_id = ? AND status = 'active'""",
            (user_id,),
        ).fetchall()
        recorded_meal_types = sorted(
            {
                str(row["meal_type"]).lower()
                for row in meal_rows
                if self._parse_day_in_timezone(row["occurred_at"], tz_name) == day
            }
        )
        expected_meal_types = ("breakfast", "lunch", "dinner")
        unverified_meal_types = [meal for meal in expected_meal_types if meal not in recorded_meal_types]

        workout_rows = conn.execute(
            """SELECT record_id, kind, body_json FROM domain_record
               WHERE user_id = ? AND kind IN ('workout', 'workout_log')
                 AND day = ? AND status = 'active'
               ORDER BY created_at ASC""",
            (user_id, day),
        ).fetchall()
        sessions: list[dict[str, Any]] = []
        completion_rates: list[float] = []
        for row in workout_rows:
            try:
                body = json.loads(row["body_json"]) if row["body_json"] else {}
            except Exception:
                body = {}
            completion = body.get("completion_rate")
            if completion is not None:
                try:
                    completion_rates.append(float(completion))
                except (TypeError, ValueError):
                    pass
            exercises = body.get("completed_exercises") or body.get("actual_sets") or []
            sessions.append(
                {
                    "record_id": row["record_id"],
                    "kind": row["kind"],
                    "completion_rate": completion,
                    "rpe": body.get("session_rpe") if body.get("session_rpe") is not None else body.get("rpe_avg"),
                    "exercise_count": len(exercises) if isinstance(exercises, list) else 0,
                    "discomfort_reported": bool(body.get("discomfort_notes")),
                    "activity_summary": body.get("activity_summary"),
                    "source_image_saved": bool(body.get("source_image")),
                }
            )

        if not sessions:
            workout_status = "unrecorded"
        elif completion_rates and max(completion_rates) >= 1.0:
            workout_status = "completed"
        else:
            workout_status = "partial_or_rest_unconfirmed"

        questions: list[str] = []
        if unverified_meal_types:
            labels = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"}
            readable = "、".join(labels[item] for item in unverified_meal_types)
            questions.append(f"今天的{readable}是尚未记录，还是确实没有吃？如已进食，请补充食物和大致份量。")
        if not sessions:
            questions.append("今天进行了什么训练？如为休息日请明确说明；如训练了，请补充动作、完成度、RPE 和不适情况。")

        return {
            "date": day,
            "recorded_meal_types": recorded_meal_types,
            "unverified_meal_types": unverified_meal_types,
            "workout_status": workout_status,
            "workout_sessions": sessions,
            "ready_to_finalize": not unverified_meal_types and bool(sessions),
            "questions": questions,
            "guidance": (
                "先向用户核实缺失事实，禁止把未记录当作未进食或休息日。"
                if questions else "当天主要饮食与训练事实均已记录，可以生成晚间复盘。"
            ),
        }

    # =========================================================================
    # Read-Only Queries (Strictly Pure: Snapshot reads with explicit BEGIN)
    # =========================================================================

    def get_profile(self, user_id: str) -> dict[str, Any]:
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT user_id, timezone, goals_json, constraints_json, safety_flags_json,
                          safety_mode, deload_until, state_version
                   FROM user_profile WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
            if not row:
                data = {
                    "user_id": user_id,
                    "timezone": "Asia/Shanghai",
                    "goals": {},
                    "constraints": {},
                    "safety_flags": [],
                    "safety_mode": "normal",
                    "deload_until": None,
                    "state_version": 0,
                    "exists": False,
                }
            else:
                data = {
                    "user_id": row["user_id"],
                    "timezone": row["timezone"],
                    "goals": json.loads(row["goals_json"]),
                    "constraints": json.loads(row["constraints_json"]),
                    "safety_flags": json.loads(row["safety_flags_json"]),
                    "safety_mode": row["safety_mode"],
                    "deload_until": row["deload_until"],
                    "state_version": row["state_version"],
                    "exists": True,
                }
            data["onboarding"] = self.assess_onboarding(data["goals"], data["constraints"])
            data["daily_review_automation"] = self.daily_review_automation_spec(
                user_id, data["timezone"], data["constraints"]
            )
            return {
                "operation_id": f"op_read_profile_{user_id}_{data['state_version']}",
                "status": "success",
                "data": data,
                "warnings": [],
                "error": None,
                "state_version": data["state_version"],
                **data,
            }

    def _calculate_maintenance_due(
        self,
        conn: Any,
        user_id: str,
        now_dt: datetime | None = None,
        day: str | None = None,
        prune_days: int = 30,
    ) -> dict[str, Any]:
        """Strictly pure-read evaluation of maintenance due work across outbox, leases, and TTL facts."""
        now_dt = now_dt or datetime.now(UTC)
        now_iso = now_dt.isoformat()
        cutoff_date = (now_dt - timedelta(days=prune_days)).strftime("%Y-%m-%d")
        cutoff_iso = (now_dt - timedelta(days=prune_days)).isoformat()

        # 1. Query eligible outbox items (pending or expired in-flight leases)
        eligible_rows = conn.execute(
            """SELECT intent_id, status, method, attempts, lease_until, updated_at
               FROM memory_outbox
               WHERE user_id = ?
                 AND (status = 'pending' OR (status = 'in_flight' AND lease_until IS NOT NULL AND lease_until < ?))
               ORDER BY created_at ASC""",
            (user_id, now_iso),
        ).fetchall()

        pending_rows = [r for r in eligible_rows if r["status"] == "pending"]
        expired_lease_rows = [r for r in eligible_rows if r["status"] == "in_flight"]
        pending_count = len(pending_rows)
        expired_lease_count = len(expired_lease_rows)

        # 2. Query TTL expired meal records requiring details pruning (foods_json != '[]')
        ttl_meals_count = conn.execute(
            """SELECT COUNT(*) AS c FROM meal_log
               WHERE user_id = ? AND status = 'active'
                 AND substr(occurred_at, 1, 10) < ?
                 AND foods_json != '[]'""",
            (user_id, cutoff_date),
        ).fetchone()["c"]

        # 3. Query TTL unreferenced superseded domain records eligible for purge
        ttl_records_count = conn.execute(
            """SELECT COUNT(*) AS c FROM domain_record
               WHERE user_id = ?
                 AND status IN ('superseded', 'deleted')
                 AND created_at < ?
                 AND record_id NOT IN (
                     SELECT DISTINCT parent_id FROM domain_record WHERE user_id = ? AND parent_id IS NOT NULL
                 )""",
            (user_id, cutoff_iso, user_id),
        ).fetchone()["c"]

        # 4. Query TTL sent outbox records eligible for purge
        ttl_sent_count = conn.execute(
            """SELECT COUNT(*) AS c FROM memory_outbox
               WHERE user_id = ? AND status = 'sent' AND created_at < ?""",
            (user_id, cutoff_iso),
        ).fetchone()["c"]

        ttl_prune_count = ttl_meals_count + ttl_records_count + ttl_sent_count
        due = (len(eligible_rows) > 0) or (ttl_prune_count > 0)

        # Disclose specific reasons
        reasons: list[str] = []
        if pending_count > 0:
            reasons.append(f"pending_memory_outbox: {pending_count} items awaiting synchronization")
        if expired_lease_count > 0:
            reasons.append(f"expired_worker_leases: {expired_lease_count} in-flight leases expired and awaiting recovery")
        if ttl_meals_count > 0:
            reasons.append(f"ttl_meal_details_due: {ttl_meals_count} historical meals awaiting detail pruning and trend consolidation")
        if (ttl_records_count + ttl_sent_count) > 0:
            reasons.append(f"ttl_prune_due: {ttl_records_count + ttl_sent_count} expired superseded/sent records awaiting cleanup")

        primary_reason = "; ".join(reasons) if reasons else None

        # Work generation token calculation
        work_gen_hash: str | None = None
        maintenance_key: str | None = None
        retry_after_seconds: int | None = None

        if due:
            # Batch of up to 50 eligible items determines the generation fingerprint
            batch_items = eligible_rows[:50]
            hasher = hashlib.sha256()
            for it in batch_items:
                hasher.update(f"{it['intent_id']}:{it['attempts']}:{it['status']};".encode("utf-8"))
            hasher.update(f"ttl:{ttl_meals_count}:{ttl_records_count}:{ttl_sent_count}".encode("utf-8"))
            work_gen_hash = hasher.hexdigest()[:12]
            target_day = day or now_dt.strftime("%Y-%m-%d")
            maintenance_key = f"maint_{user_id}_{target_day}_g{work_gen_hash}"

            # If there are failed attempts, calculate exponential backoff
            failed_attempts = [r["attempts"] for r in pending_rows if r["attempts"] > 0]
            if failed_attempts:
                max_att = max(failed_attempts)
                retry_after_seconds = min(300, 2 ** min(max_att, 6))

        due_work = {
            "eligible_outbox_count": len(eligible_rows),
            "pending_outbox_count": pending_count,
            "expired_lease_count": expired_lease_count,
            "ttl_meals_count": ttl_meals_count,
            "ttl_records_count": ttl_records_count,
            "ttl_sent_count": ttl_sent_count,
            "ttl_prune_count": ttl_prune_count,
            "work_generation": work_gen_hash,
            "has_more": len(eligible_rows) > 50,
        }

        return {
            "due": due,
            "reason": primary_reason,
            "reasons": reasons,
            "maintenance_key": maintenance_key,
            "work_generation": work_gen_hash,
            "due_work": due_work,
            "pending_outbox_count": pending_count,
            "expired_lease_count": expired_lease_count,
            "ttl_meals_count": ttl_meals_count,
            "ttl_prune_count": ttl_prune_count,
            "retry_after_seconds": retry_after_seconds,
        }

    def get_today(self, user_id: str, day: str, now: datetime | None = None) -> dict[str, Any]:
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except Exception as err:
            raise ValidationError(f"Invalid date format: {day}") from err

        now_dt = now or datetime.now(UTC)

        with self.store.connect() as conn:
            conn.execute("BEGIN")
            try:
                row = conn.execute(
                    """SELECT timezone, goals_json, constraints_json, state_version, safety_mode, deload_until
                       FROM user_profile WHERE user_id = ?""",
                    (user_id,),
                ).fetchone()
                tz_name = row["timezone"] if row else "Asia/Shanghai"
                version = row["state_version"] if row else 0
                safety_mode = row["safety_mode"] if row else "normal"
                deload_until = row["deload_until"] if row else None
                goals = json.loads(row["goals_json"]) if row else {}

                totals = self._today_totals(conn, user_id, day, tz_name)

                # Real Targets & Remaining Calculation (No ungrounded default calorie or protein advice)
                nutr_targets = self._resolve_nutrition_targets(goals)
                if nutr_targets:
                    t_kcal_low = nutr_targets["kcal_low"]
                    t_kcal_high = nutr_targets["kcal_high"]
                    targets = {
                        "kcal_range": nutr_targets["kcal_range"],
                        "protein_range": nutr_targets["protein_range"],
                        "status": nutr_targets["status"],
                    }
                    rem_k_low = max(0, t_kcal_low - totals["kcal_high"])
                    rem_k_high = max(0, t_kcal_high - totals["kcal_low"])
                    t_k_mid = round((t_kcal_low + t_kcal_high) / 2.0)
                    raw_rem_k_mid = max(0, t_k_mid - totals.get("kcal_mid", round((totals["kcal_low"] + totals["kcal_high"]) / 2.0)))
                    rem_k_mid = max(rem_k_low, min(rem_k_high, raw_rem_k_mid))

                    rem_p_low = None
                    rem_p_high = None
                    rem_p_mid = None
                    if nutr_targets["protein_range"]:
                        rem_p_low = max(0, nutr_targets["protein_range"][0] - totals["protein_high"])
                        rem_p_high = max(0, nutr_targets["protein_range"][1] - totals["protein_low"])
                        t_p_mid = round((nutr_targets["protein_range"][0] + nutr_targets["protein_range"][1]) / 2.0)
                        raw_rem_p_mid = max(0, t_p_mid - totals.get("protein_mid", round((totals["protein_low"] + totals["protein_high"]) / 2.0)))
                        rem_p_mid = max(rem_p_low, min(rem_p_high, raw_rem_p_mid))

                    remaining = {
                        "kcal_low": rem_k_low,
                        "kcal_high": rem_k_high,
                        "kcal_mid": rem_k_mid,
                        "protein_low": rem_p_low,
                        "protein_high": rem_p_high,
                        "protein_mid": rem_p_mid,
                    }
                else:
                    targets = {
                        "kcal_range": None,
                        "protein_range": None,
                        "status": "unconfigured",
                        "message": "User profile goals unconfigured. No default calorie advice prescribed.",
                    }
                    remaining = {
                        "kcal_low": None,
                        "kcal_high": None,
                        "kcal_mid": None,
                        "protein_low": None,
                        "protein_high": None,
                        "protein_mid": None,
                    }

                plan_row = conn.execute(
                    """SELECT body_json, status FROM domain_record
                       WHERE user_id = ? AND kind = 'plan' AND day = ? AND status NOT IN ('superseded', 'deleted')
                       ORDER BY created_at DESC LIMIT 1""",
                    (user_id, day),
                ).fetchone()
                plan_state = plan_row["status"] if plan_row else "draft"
                is_missing = totals["meal_count"] == 0
                review_readiness = self._daily_review_facts(conn, user_id, day, tz_name)

                # Unified pure-read maintenance due-work evaluation
                due_info = self._calculate_maintenance_due(conn, user_id, now_dt, day=day)
                maint_rec = due_info["due"]
                maint_reason = due_info["reason"]
                maint_key = due_info["maintenance_key"]
                suggested_action = "cyber_health_maintain_memory" if maint_rec else None

                maintenance_data = {
                    "recommended": maint_rec,
                    "maintenance_recommended": maint_rec,
                    "reason": maint_reason,
                    "maintenance_reason": maint_reason,
                    "reasons": due_info["reasons"],
                    "suggested_action": suggested_action,
                    "maintenance_key": maint_key,
                    "work_generation": due_info["work_generation"],
                    "pending_outbox_count": due_info["pending_outbox_count"],
                    "expired_lease_count": due_info["expired_lease_count"],
                    "ttl_meals_count": due_info["ttl_meals_count"],
                    "ttl_prune_count": due_info["ttl_prune_count"],
                    "due_work": due_info["due_work"],
                    "retry_after_seconds": due_info["retry_after_seconds"],
                }

                data = {
                    "date": day,
                    "state_version": version,
                    "nutrition": totals,
                    "targets": targets,
                    "consumed": totals,
                    "remaining": remaining,
                    "recording_status": "active" if not is_missing else "no_data",
                    "plan_status": {
                        "missing_data": is_missing,
                        "data_status": "recorded" if not is_missing else "unrecorded",
                        "state": plan_state,
                    },
                    "daily_review_readiness": review_readiness,
                    "safety_mode": safety_mode,
                    "deload_until": deload_until,
                    "maintenance_recommended": maint_rec,
                    "maintenance_reason": maint_reason,
                    "suggested_action": suggested_action,
                    "maintenance_key": maint_key,
                    "maintenance": maintenance_data,
                }
                return {
                    "operation_id": f"op_read_{uuid.uuid4().hex[:12]}",
                    "status": "success",
                    "data": data,
                    "warnings": [],
                    "error": None,
                    "state_version": version,
                    **data,
                }
            finally:
                conn.execute("COMMIT")

    def get_remaining_calories(
        self,
        *,
        user_id: str,
        date: str,
    ) -> dict[str, Any]:
        """Calculate remaining daily calorie/protein budget and coaching priority."""
        try:
            GetRemainingCaloriesInput(user_id=user_id, date=date)
        except Exception as err:
            raise ValidationError(str(err)) from err

        today_info = self.get_today(user_id=user_id, day=date)
        targets = today_info.get("targets", {})
        remaining = today_info.get("remaining", {})
        version = today_info.get("state_version", 0)

        if targets.get("status") == "unconfigured":
            remaining_ranges = None
            priority_nutrients: list[str] = []
            suggestion = "User profile goals unconfigured. No default calorie advice prescribed."
        else:
            remaining_ranges = remaining
            rem_kcal_high = remaining.get("kcal_high", 0) or 0
            rem_kcal_low = remaining.get("kcal_low", 0) or 0
            rem_kcal_mid = remaining.get("kcal_mid", round((rem_kcal_low + rem_kcal_high) / 2.0))
            rem_prot_low = remaining.get("protein_low", 0) or 0
            rem_prot_high = remaining.get("protein_high", 0) or 0
            rem_prot_mid = remaining.get("protein_mid", round((rem_prot_low + rem_prot_high) / 2.0) if rem_prot_high else 0)

            if rem_kcal_high <= 0:
                priority_nutrients = []
                suggestion = "Daily calorie budget reached or exceeded. Prioritize hydration and non-caloric fluids."
            elif rem_prot_low > 0:
                priority_nutrients = ["protein"]
                suggestion = f"Remaining budget: {rem_kcal_low}-{rem_kcal_high} kcal (point estimate: ~{rem_kcal_mid} kcal) with priority on {rem_prot_low}-{rem_prot_high}g protein (point estimate: ~{rem_prot_mid}g). Focus on lean protein sources."
            else:
                priority_nutrients = ["calories"]
                suggestion = f"Protein target met. Remaining energy budget: {rem_kcal_low}-{rem_kcal_high} kcal (point estimate: ~{rem_kcal_mid} kcal)."

        operation_id = f"op_read_rem_{user_id}_{version}"
        data = {
            "user_id": user_id,
            "date": date,
            "remaining_ranges": remaining_ranges,
            "priority_nutrients": priority_nutrients,
            "suggestion": suggestion,
        }
        return {
            "operation_id": operation_id,
            "status": "success",
            "data": data,
            "warnings": [],
            "error": None,
            "state_version": version,
            **data,
        }

    def get_audit_trail(self, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT operation_id, action, result_status, before_version, after_version, created_at
                   FROM operation_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def health_check(self) -> dict[str, Any]:
        with self.store.connect() as conn:
            conn.execute("SELECT 1").fetchone()
            try:
                self.memory_provider.call("ping", {})
                mem_status = "ok"
            except Exception:
                mem_status = "deferred"
            pending_events = conn.execute(
                "SELECT COUNT(*) AS count FROM schedule_event WHERE status = 'overdue'"
            ).fetchone()["count"]
            pending_outbox = conn.execute(
                "SELECT COUNT(*) AS count FROM memory_outbox WHERE status = 'pending'"
            ).fetchone()["count"]
        return {
            "overall_status": "ok",
            "components": {"sqlite": "ok", "memory_provider": mem_status},
            "pending_work": pending_events + pending_outbox,
            "pending_events": pending_events,
            "pending_memory_outbox": pending_outbox,
        }

    def get_schedule(
        self,
        user_id: str,
        date: str | None = None,
        now: datetime | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        now_dt = now or datetime.now(UTC)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        now_utc = now_dt.astimezone(UTC)

        with self.store.connect() as conn:
            conn.execute("BEGIN")
            try:
                profile_row = conn.execute(
                    """SELECT timezone, goals_json, constraints_json, safety_mode, state_version
                       FROM user_profile WHERE user_id = ?""",
                    (user_id,),
                ).fetchone()
                tz_name = profile_row["timezone"] if profile_row else "Asia/Shanghai"
                try:
                    user_tz = ZoneInfo(tz_name)
                except Exception:
                    user_tz = ZoneInfo("Asia/Shanghai")

                goals = json.loads(profile_row["goals_json"]) if profile_row and profile_row["goals_json"] else {}
                constraints = json.loads(profile_row["constraints_json"]) if profile_row and profile_row["constraints_json"] else {}
                safety_mode = profile_row["safety_mode"] if profile_row else "normal"

                # Check if reminders are disabled by user profile
                reminders_disabled = (
                    constraints.get("reminders_enabled") is False
                    or constraints.get("enable_reminders") is False
                    or constraints.get("allow_reminders") is False
                    or goals.get("reminders_enabled") is False
                )

                if include_inactive:
                    all_rows = conn.execute(
                        """SELECT event_id, event_type, window_start, window_end, status,
                                  revision, delivery_attempts, prompt_hint, created_at, updated_at
                           FROM schedule_event
                           WHERE user_id = ?
                           ORDER BY window_start ASC""",
                        (user_id,),
                    ).fetchall()
                else:
                    all_rows = conn.execute(
                        """SELECT event_id, event_type, window_start, window_end, status,
                                  revision, delivery_attempts, prompt_hint, created_at, updated_at
                           FROM schedule_event
                           WHERE user_id = ? AND status IN ('pending', 'overdue')
                           ORDER BY window_start ASC""",
                        (user_id,),
                    ).fetchall()

                results: list[dict[str, Any]] = []
                for r in all_rows:
                    item = dict(r)
                    end_dt = datetime.fromisoformat(item["window_end"].replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=user_tz)
                    end_utc = end_dt.astimezone(UTC)

                    # Pure derived status: if pending and window elapsed, derive status as overdue in memory
                    if item["status"] == "pending" and end_utc <= now_utc:
                        item["status"] = "overdue"

                    item_day = self._parse_day_in_timezone(item["window_start"], tz_name)
                    effective_status = item["status"]

                    if effective_status == "overdue":
                        item["compensation_required"] = True

                    # Overdue events are always returned (for compensation), while others match date
                    if date:
                        if effective_status != "overdue" and item_day != date:
                            continue

                    # Determine trigger condition
                    ev_type = item["event_type"]
                    ev_id = item["event_id"]
                    if ev_type == "MORNING_PLAN":
                        trigger_condition = "morning_plan_not_locked"
                    elif ev_type == "MEAL_CHECK":
                        if "_dinner" in ev_id:
                            trigger_condition = "dinner_not_logged"
                        elif "_lunch" in ev_id:
                            trigger_condition = "lunch_not_logged"
                        else:
                            start_dt = datetime.fromisoformat(item["window_start"].replace("Z", "+00:00"))
                            if start_dt.tzinfo is None:
                                start_dt = start_dt.replace(tzinfo=user_tz)
                            start_hour = start_dt.astimezone(user_tz).hour
                            trigger_condition = "dinner_not_logged" if start_hour >= 16 else "lunch_not_logged"
                    elif ev_type == "WORKOUT_REMINDER":
                        trigger_condition = "workout_pending"
                    elif ev_type == "DAILY_REVIEW":
                        trigger_condition = "review_pending"
                    else:
                        trigger_condition = "custom_condition"

                    item["trigger_condition"] = trigger_condition

                    # Evaluate fact-based eligibility and suppression reasons
                    if effective_status in ("cancelled", "skipped", "acknowledged", "delivered"):
                        item["eligible"] = False
                        item["suppression_reason"] = f"event_{effective_status}"
                    elif reminders_disabled:
                        item["eligible"] = False
                        item["suppression_reason"] = "reminders_disabled_by_user"
                    else:
                        eligible = True
                        suppression_reason: str | None = None

                        if trigger_condition == "morning_plan_not_locked":
                            plan_row = conn.execute(
                                """SELECT status, body_json FROM domain_record
                                   WHERE user_id = ? AND kind = 'plan' AND day = ? AND status NOT IN ('superseded', 'deleted')
                                   ORDER BY created_at DESC LIMIT 1""",
                                (user_id, item_day),
                            ).fetchone()
                            if plan_row:
                                p_status = plan_row["status"]
                                try:
                                    p_body = json.loads(plan_row["body_json"])
                                    if p_body.get("status") == "committed":
                                        p_status = "committed"
                                except Exception:
                                    pass
                                if p_status == "committed":
                                    eligible = False
                                    suppression_reason = "morning_plan_already_committed"

                        elif trigger_condition == "lunch_not_logged":
                            lunch_rows = conn.execute(
                                """SELECT meal_id, occurred_at FROM meal_log
                                   WHERE user_id = ? AND meal_type = 'lunch' AND status = 'active'""",
                                (user_id,),
                            ).fetchall()
                            lunch_logged = any(
                                self._parse_day_in_timezone(lr["occurred_at"], tz_name) == item_day
                                for lr in lunch_rows
                            )
                            if lunch_logged:
                                eligible = False
                                suppression_reason = "lunch_already_logged"

                        elif trigger_condition == "dinner_not_logged":
                            dinner_rows = conn.execute(
                                """SELECT meal_id, occurred_at FROM meal_log
                                   WHERE user_id = ? AND meal_type = 'dinner' AND status = 'active'""",
                                (user_id,),
                            ).fetchall()
                            dinner_logged = any(
                                self._parse_day_in_timezone(dr["occurred_at"], tz_name) == item_day
                                for dr in dinner_rows
                            )
                            if dinner_logged:
                                eligible = False
                                suppression_reason = "dinner_already_logged"

                        elif trigger_condition == "workout_pending":
                            if safety_mode == "restricted":
                                eligible = False
                                suppression_reason = "safety_restricted_mode"
                            else:
                                wo_rows = conn.execute(
                                    """SELECT body_json FROM domain_record
                                       WHERE user_id = ? AND kind IN ('workout', 'workout_log') AND day = ? AND status = 'active'""",
                                    (user_id, item_day),
                                ).fetchall()
                                completed = False
                                for wr in wo_rows:
                                    try:
                                        wb = json.loads(wr["body_json"]) if wr["body_json"] else {}
                                        cr = wb.get("completion_rate")
                                        if cr is not None and float(cr) >= 1.0:
                                            completed = True
                                            break
                                    except Exception:
                                        pass

                                if completed:
                                    eligible = False
                                    suppression_reason = "workout_already_completed"
                                else:
                                    plan_row = conn.execute(
                                        """SELECT body_json FROM domain_record
                                           WHERE user_id = ? AND kind = 'plan' AND day = ? AND status NOT IN ('superseded', 'deleted')
                                           ORDER BY created_at DESC LIMIT 1""",
                                        (user_id, item_day),
                                    ).fetchone()
                                    if plan_row:
                                        try:
                                            p_body = json.loads(plan_row["body_json"])
                                            wp = p_body.get("workout_plan")
                                            if isinstance(wp, str) and ("休息" in wp or "休整" in wp or "rest" in wp.lower()):
                                                eligible = False
                                                suppression_reason = "scheduled_rest_day"
                                            elif isinstance(wp, dict) and (wp.get("is_rest_day") or wp.get("type") == "rest"):
                                                eligible = False
                                                suppression_reason = "scheduled_rest_day"
                                        except Exception:
                                            pass
                                    if eligible and (constraints.get("is_rest_day") or goals.get("is_rest_day")):
                                        eligible = False
                                        suppression_reason = "scheduled_rest_day"

                        elif trigger_condition == "review_pending":
                            rev_row = conn.execute(
                                """SELECT record_id FROM domain_record
                                   WHERE user_id = ? AND kind = 'daily_review' AND day = ? AND status = 'active'
                                   LIMIT 1""",
                                (user_id, item_day),
                            ).fetchone()
                            if rev_row:
                                eligible = False
                                suppression_reason = "daily_review_already_completed"

                        item["eligible"] = eligible
                        item["suppression_reason"] = suppression_reason

                    results.append(item)

                return results
            finally:
                conn.execute("COMMIT")

    # =========================================================================
    # Write Operations (Atomic Transactions, Strict Version & Idempotency)
    # =========================================================================

    def log_meal(
        self,
        *,
        user_id: str,
        occurred_at: str,
        meal_type: str,
        foods: list[dict[str, Any]] | None = None,
        kcal_low: int = 0,
        kcal_high: int = 0,
        idempotency_key: str,
        protein_low: int = 0,
        protein_high: int = 0,
        target_meal_id: str | None = None,
        expected_state_version: int | None = None,
        repeat_meal: str | None = None,
        source: str | None = None,
        confidence: str | None = None,
        correction_reason: str | None = None,
        user_confirmed: bool | None = None,
    ) -> dict[str, Any]:
        foods_list = foods if foods is not None else []
        try:
            validated = LogMealInput(
                user_id=user_id,
                occurred_at=occurred_at,
                meal_type=meal_type,
                foods=foods_list,  # type: ignore[arg-type]
                kcal_low=kcal_low,
                kcal_high=kcal_high,
                protein_low=protein_low,
                protein_high=protein_high,
                idempotency_key=idempotency_key,
                target_meal_id=target_meal_id,
                expected_state_version=expected_state_version,
                repeat_meal=repeat_meal,
                source=source,
                confidence=confidence,
                correction_reason=correction_reason,
                user_confirmed=user_confirmed,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        dumped_foods = [f.model_dump() for f in validated.foods]
        payload = {
            "action": "log_meal",
            "user_id": user_id,
            "occurred_at": occurred_at,
            "meal_type": meal_type,
            "foods": dumped_foods,
            "kcal_low": kcal_low,
            "kcal_high": kcal_high,
            "protein_low": protein_low,
            "protein_high": protein_high,
            "target_meal_id": target_meal_id,
            "expected_state_version": expected_state_version,
            "repeat_meal": repeat_meal,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "log_meal", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute(
                "SELECT timezone, state_version FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            before_version = profile["state_version"]
            tz_name = profile["timezone"]

            if expected_state_version is not None and expected_state_version != before_version:
                raise ConflictError(
                    f"Expected version {expected_state_version}, current version is {before_version}"
                )

            final_foods = dumped_foods
            final_kcal_low = kcal_low
            final_kcal_high = kcal_high
            final_protein_low = protein_low
            final_protein_high = protein_high

            # Local Yesterday Repeat Resolution
            if repeat_meal:
                meal_date = self._parse_day_in_timezone(occurred_at, tz_name)
                source_meal = None
                if repeat_meal.startswith("yesterday_") or repeat_meal == "yesterday":
                    target_sub = repeat_meal.replace("yesterday_", "") if repeat_meal != "yesterday" else meal_type
                    yesterday_date = (datetime.strptime(meal_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

                    candidates = conn.execute(
                        """SELECT meal_id, occurred_at, foods_json, kcal_low, kcal_high, protein_low, protein_high
                           FROM meal_log
                           WHERE user_id = ? AND meal_type = ? AND status = 'active'
                           ORDER BY occurred_at DESC""",
                        (user_id, target_sub),
                    ).fetchall()
                    for cand in candidates:
                        if self._parse_day_in_timezone(cand["occurred_at"], tz_name) == yesterday_date:
                            source_meal = cand
                            break
                    if not source_meal:
                        raise ValidationError(f"No active '{target_sub}' meal found for yesterday ({yesterday_date})")
                else:
                    source_meal = conn.execute(
                        """SELECT foods_json, kcal_low, kcal_high, protein_low, protein_high
                           FROM meal_log
                           WHERE meal_id = ? AND user_id = ? AND status = 'active'""",
                        (repeat_meal, user_id),
                    ).fetchone()

                if not source_meal:
                    raise ValidationError(f"Referenced repeat meal '{repeat_meal}' not found")

                final_foods = json.loads(source_meal["foods_json"])
                final_kcal_low = source_meal["kcal_low"]
                final_kcal_high = source_meal["kcal_high"]
                final_protein_low = source_meal["protein_low"]
                final_protein_high = source_meal["protein_high"]

            # Revision Handling
            if target_meal_id:
                original = conn.execute(
                    "SELECT meal_id, status FROM meal_log WHERE meal_id = ? AND user_id = ?",
                    (target_meal_id, user_id),
                ).fetchone()
                if not original or original["status"] != "active":
                    raise ValidationError("Target meal does not exist or is no longer active")
                conn.execute(
                    "UPDATE meal_log SET status = 'superseded' WHERE meal_id = ?",
                    (target_meal_id,),
                )

            after_version = before_version + 1
            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            meal_id = f"meal_{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO meal_log(
                    meal_id, user_id, occurred_at, meal_type, foods_json, kcal_low, kcal_high,
                    protein_low, protein_high, status, parent_meal_id, causation_id, state_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                (
                    meal_id,
                    user_id,
                    occurred_at,
                    meal_type,
                    self.store.json(final_foods),
                    final_kcal_low,
                    final_kcal_high,
                    final_protein_low,
                    final_protein_high,
                    target_meal_id,
                    operation_id,
                    after_version,
                    now,
                ),
            )

            day = self._parse_day_in_timezone(occurred_at, tz_name)
            totals = self._today_totals(conn, user_id, day, tz_name)
            response = self._response(
                operation_id,
                "success",
                {"meal_id": meal_id, "today_totals": totals},
                after_version,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="log_meal",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def delete_meal(
        self,
        *,
        user_id: str,
        meal_id: str,
        idempotency_key: str,
        reason: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            DeleteMealInput(
                user_id=user_id,
                meal_id=meal_id,
                idempotency_key=idempotency_key,
                reason=reason,
                expected_state_version=expected_state_version if expected_state_version is not None else 0,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {
            "action": "delete_meal",
            "user_id": user_id,
            "meal_id": meal_id,
            "reason": reason,
            "expected_state_version": expected_state_version,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "delete_meal", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute(
                "SELECT timezone, state_version FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            before_version = profile["state_version"]
            tz_name = profile["timezone"]

            if expected_state_version is not None and expected_state_version != before_version:
                raise ConflictError(
                    f"Expected version {expected_state_version}, current version is {before_version}"
                )

            target = conn.execute(
                "SELECT occurred_at, status FROM meal_log WHERE meal_id = ? AND user_id = ?",
                (meal_id, user_id),
            ).fetchone()
            if not target or target["status"] != "active":
                raise ValidationError(f"Meal {meal_id} does not exist or is already superseded/deleted")

            conn.execute(
                "UPDATE meal_log SET status = 'deleted' WHERE meal_id = ?",
                (meal_id,),
            )

            after_version = before_version + 1
            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            day = self._parse_day_in_timezone(target["occurred_at"], tz_name)
            totals = self._today_totals(conn, user_id, day, tz_name)

            response = self._response(
                operation_id,
                "success",
                {"deleted_meal_id": meal_id, "today_totals": totals},
                after_version,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="delete_meal",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def update_profile(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        goals: dict[str, Any] | None = None,
        constraints: dict[str, Any] | None = None,
        timezone: str | None = None,
        safety_flags: list[str] | None = None,
        clear_safety_flags: bool = False,
        clearance_reason: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            validated = UpdateProfileInput(
                user_id=user_id,
                idempotency_key=idempotency_key,
                goals=goals,
                constraints=constraints,
                timezone=timezone,
                safety_flags=safety_flags,
                clear_safety_flags=clear_safety_flags,
                clearance_reason=clearance_reason,
                expected_state_version=expected_state_version,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {
            "action": "update_profile",
            "user_id": user_id,
            "goals": validated.goals,
            "constraints": constraints,
            "timezone": timezone,
            "safety_flags": safety_flags,
            "clear_safety_flags": clear_safety_flags,
            "clearance_reason": clearance_reason,
            "expected_state_version": expected_state_version,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "update_profile", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            row = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = row["state_version"]

            if expected_state_version is not None and expected_state_version != before_version:
                raise ConflictError(
                    f"Expected version {expected_state_version}, current version is {before_version}"
                )

            new_tz = timezone or row["timezone"]
            new_goals = validated.goals if validated.goals is not None else json.loads(row["goals_json"])
            new_constraints = constraints if constraints is not None else json.loads(row["constraints_json"])
            new_safety_flags = list(safety_flags) if safety_flags is not None else json.loads(row["safety_flags_json"])
            safety_mode = row["safety_mode"]
            deload_until = row["deload_until"]
            warnings: list[str] = []

            # Check for red flag keywords in safety_flags, constraints, goals
            all_text = " ".join(new_safety_flags) + " " + json.dumps(new_constraints) + " " + json.dumps(new_goals)
            detected_red_flags = [kw for kw in RED_FLAG_KEYWORDS if kw in all_text]
            if detected_red_flags and not clear_safety_flags:
                safety_mode = "restricted"
                for rf in detected_red_flags:
                    if rf not in new_safety_flags:
                        new_safety_flags.append(rf)
                warnings.append(
                    f"TRAIN_SAFETY_01: Red flag symptom '{', '.join(detected_red_flags)}' detected. "
                    "System locked in Restricted Mode."
                )

            if clear_safety_flags:
                if not clearance_reason or not clearance_reason.strip():
                    raise ValidationError(
                        "A non-empty clearance_reason is required to clear safety flags and exit restricted mode."
                    )
                safety_mode = "normal"
                new_safety_flags = []
                deload_until = (datetime.now(UTC) + timedelta(days=7)).isoformat()
                warnings.append(
                    f"RECOVERY_FLAG_CLEAR_01: Safety cleared with reason '{clearance_reason}'. "
                    "Initiated 7-day Deload Period: load <= 50-60% baseline, RIR >= 3, no failure."
                )

            after_version = before_version + 1
            conn.execute(
                """UPDATE user_profile SET
                    timezone = ?, goals_json = ?, constraints_json = ?, safety_flags_json = ?,
                    safety_mode = ?, deload_until = ?, state_version = ?, updated_at = ?
                   WHERE user_id = ?""",
                (
                    new_tz,
                    self.store.json(new_goals),
                    self.store.json(new_constraints),
                    self.store.json(new_safety_flags),
                    safety_mode,
                    deload_until,
                    after_version,
                    now,
                    user_id,
                ),
            )

            data = {
                "user_id": user_id,
                "timezone": new_tz,
                "goals": new_goals,
                "constraints": new_constraints,
                "safety_flags": new_safety_flags,
                "safety_mode": safety_mode,
                "deload_until": deload_until,
            }
            data["onboarding"] = self.assess_onboarding(new_goals, new_constraints)
            data["daily_review_automation"] = self.daily_review_automation_spec(
                user_id, new_tz, new_constraints
            )
            response = self._response(
                operation_id,
                "success",
                data,
                after_version,
                warnings=warnings,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="update_profile",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def log_daily_metrics(
        self,
        *,
        user_id: str,
        date: str,
        metrics: dict[str, Any],
        idempotency_key: str,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            validated = LogDailyMetricsInput(
                user_id=user_id,
                date=date,
                metrics=DailyMetricsInput(**metrics),
                idempotency_key=idempotency_key,
                expected_state_version=expected_state_version,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {
            "action": "log_daily_metrics",
            "user_id": user_id,
            "date": date,
            "metrics": validated.metrics.model_dump(),
            "expected_state_version": expected_state_version,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "log_daily_metrics", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]

            if expected_state_version is not None and expected_state_version != before_version:
                raise ConflictError(
                    f"Expected version {expected_state_version}, current version is {before_version}"
                )

            # Check existing daily_state on the same date to merge metrics and maintain revision lineage
            existing_ds = conn.execute(
                """SELECT record_id, body_json FROM domain_record
                   WHERE user_id = ? AND kind = 'daily_state' AND day = ? AND status = 'active'
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, date),
            ).fetchone()
            parent_ds_id = None
            merged_metrics: dict[str, Any] = {}
            if existing_ds:
                parent_ds_id = existing_ds["record_id"]
                try:
                    prev_body = json.loads(existing_ds["body_json"])
                    merged_metrics = dict(prev_body.get("metrics", {}))
                except Exception:
                    pass
                conn.execute(
                    "UPDATE domain_record SET status = 'superseded' WHERE record_id = ?",
                    (parent_ds_id,),
                )

            for k, v in validated.metrics.model_dump().items():
                if v is not None:
                    merged_metrics[k] = v

            score = 100.0
            sleep_hours = merged_metrics.get("sleep_hours")
            fatigue_level = merged_metrics.get("fatigue_level")
            sleep_quality = merged_metrics.get("sleep_quality")

            if sleep_hours is not None:
                if sleep_hours < 7.0:
                    score -= (7.0 - sleep_hours) * 15.0
                elif sleep_hours >= 8.0:
                    score += min(5.0, (sleep_hours - 8.0) * 5.0)

            if fatigue_level is not None:
                score -= (fatigue_level - 1) * 6.0

            if sleep_quality == "poor":
                score -= 15.0
            elif sleep_quality == "good":
                score += 5.0

            recovery_score = max(0, min(100, int(round(score))))
            triggered_rules: list[str] = []
            warnings: list[str] = []
            coaching_alert: str | None = None

            if (sleep_hours is not None and sleep_hours < 6.0) or (fatigue_level is not None and fatigue_level >= 7) or recovery_score < 60:
                triggered_rules.append("TRAIN_RECOVERY_01")
                coaching_alert = (
                    f"检测到睡眠不足（{sleep_hours}小时）或自评疲劳较高（{fatigue_level}级），"
                    "触发 TRAIN_RECOVERY_01 降载规则。今日原定力量训练建议下调负荷20%-40%或切换为恢复性拉伸。"
                )
                warnings.append("TRAIN_RECOVERY_01: Fatigue/sleep threshold reached. Workload deload advised.")

            # Red flag check in metrics
            text_corpus = " ".join(merged_metrics.get("soreness_locations") or []) + " " + str(merged_metrics.get("notes") or "")
            detected_red_flags = [kw for kw in RED_FLAG_KEYWORDS if kw in text_corpus]
            if detected_red_flags:
                triggered_rules.append("TRAIN_SAFETY_01")
                flags = json.loads(profile["safety_flags_json"])
                for rf in detected_red_flags:
                    if rf not in flags:
                        flags.append(rf)
                conn.execute(
                    "UPDATE user_profile SET safety_mode = 'restricted', safety_flags_json = ? WHERE user_id = ?",
                    (self.store.json(flags), user_id),
                )
                warnings.append(
                    f"TRAIN_SAFETY_01: Red flag symptom '{', '.join(detected_red_flags)}' detected. "
                    "System locked in Restricted Mode. Training prescriptions blocked."
                )

            after_version = before_version + 1
            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            record_id = f"ds_{uuid.uuid4().hex}"
            domain_body = {
                "metrics": merged_metrics,
                "recovery_score": recovery_score,
                "triggered_rules": triggered_rules,
                "coaching_alert": coaching_alert,
            }
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, parent_id, status, causation_id, state_version, created_at
                ) VALUES (?, ?, 'daily_state', ?, ?, ?, 'active', ?, ?, ?)""",
                (record_id, user_id, date, self.store.json(domain_body), parent_ds_id, operation_id, after_version, now),
            )

            data = {
                "record_id": record_id,
                "date": date,
                "recovery_score": recovery_score,
                "triggered_rules": triggered_rules,
                "coaching_alert": coaching_alert,
                "recorded_metrics": validated.metrics.model_dump(),
            }
            response = self._response(
                operation_id,
                "success",
                data,
                after_version,
                warnings=warnings,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="log_daily_metrics",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def log_workout(
        self,
        *,
        user_id: str,
        date: str,
        idempotency_key: str,
        session_id: str | None = None,
        planned_exercises: list[str] | None = None,
        actual_sets: list[dict[str, Any]] | None = None,
        rpe_avg: float | None = None,
        discomfort_notes: str | None = None,
        completion_rate: float | None = None,
        activity_summary: dict[str, Any] | None = None,
        source_image: dict[str, Any] | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            validated = LogWorkoutInput(
                user_id=user_id,
                session_id=session_id,
                date=date,
                planned_exercises=planned_exercises or [],
                actual_sets=actual_sets or [],
                rpe_avg=rpe_avg,
                discomfort_notes=discomfort_notes,
                completion_rate=completion_rate,
                activity_summary=activity_summary,
                source_image=source_image,
                idempotency_key=idempotency_key,
                expected_state_version=expected_state_version,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        source_image_metadata = None
        if validated.source_image is not None:
            image_bytes = base64.b64decode(validated.source_image.data_base64, validate=True)
            source_image_metadata = {
                "media_type": validated.source_image.media_type,
                "filename": validated.source_image.filename,
                "byte_size": len(image_bytes),
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
            }

        payload = {
            "action": "log_workout",
            "user_id": user_id,
            "session_id": session_id,
            "date": date,
            "planned_exercises": planned_exercises or [],
            "actual_sets": actual_sets or [],
            "rpe_avg": rpe_avg,
            "discomfort_notes": discomfort_notes,
            "completion_rate": completion_rate,
            "activity_summary": validated.activity_summary.model_dump() if validated.activity_summary else None,
            # Keep the idempotency audit compact; image bytes live only with the workout fact.
            "source_image": source_image_metadata,
            "expected_state_version": expected_state_version,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "log_workout", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]

            if expected_state_version is not None and expected_state_version != before_version:
                raise ConflictError(
                    f"Expected version {expected_state_version}, current version is {before_version}"
                )

            # Check if ALREADY in restricted mode
            if profile["safety_mode"] == "restricted":
                raise SafetyRestrictedError(
                    "System is in Restricted Mode due to safety flags. Workout logging and prescription are blocked. "
                    "Please seek clinical clearance and submit update_profile with clear_safety_flags=true."
                )

            # Check for newly reported red flag symptoms during workout
            text_corpus = str(discomfort_notes or "") + " " + " ".join(planned_exercises or [])
            detected_red_flags = [kw for kw in RED_FLAG_KEYWORDS if kw in text_corpus]
            warnings: list[str] = []

            if detected_red_flags:
                flags = json.loads(profile["safety_flags_json"])
                for rf in detected_red_flags:
                    if rf not in flags:
                        flags.append(rf)
                conn.execute(
                    "UPDATE user_profile SET safety_mode = 'restricted', safety_flags_json = ? WHERE user_id = ?",
                    (self.store.json(flags), user_id),
                )
                warnings.append(
                    f"TRAIN_SAFETY_01: Red flag symptom '{', '.join(detected_red_flags)}' reported in workout. "
                    "System immediately transitioned to Restricted Mode."
                )

            deload_until = profile["deload_until"]
            if deload_until and date <= deload_until[:10]:
                warnings.append(
                    "User is currently under 7-day Deload Period (RECOVERY_FLAG_CLEAR_01). "
                    "Ensure weights remain <= 50-60% baseline and RIR >= 3."
                )

            after_version = before_version + 1
            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            record_id = session_id or f"wo_{uuid.uuid4().hex}"
            existing_session = conn.execute(
                """SELECT body_json FROM domain_record
                   WHERE record_id = ? AND user_id = ? AND kind = 'workout_log' AND status = 'active'""",
                (record_id, user_id),
            ).fetchone()
            prior_body: dict[str, Any] = {}
            if existing_session:
                try:
                    prior_body = json.loads(existing_session["body_json"]) if existing_session["body_json"] else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    prior_body = {}

            workout_body = {
                "planned_exercises": planned_exercises or [],
                "actual_sets": actual_sets or [],
                "rpe_avg": rpe_avg,
                "discomfort_notes": discomfort_notes,
                "completion_rate": completion_rate if completion_rate is not None else 1.0,
                "activity_summary": validated.activity_summary.model_dump() if validated.activity_summary else None,
            }
            # A stable session_id identifies a correction to the same workout, not a second workout.
            # Retain earlier fields when a later correction supplies only a screenshot or wearable summary.
            if prior_body:
                for key in ("planned_exercises", "actual_sets", "rpe_avg", "discomfort_notes", "completion_rate", "activity_summary", "source_image"):
                    if workout_body.get(key) is None or workout_body.get(key) == []:
                        workout_body[key] = prior_body.get(key)
            if validated.source_image is not None and source_image_metadata is not None:
                workout_body["source_image"] = {
                    **source_image_metadata,
                    "data_base64": validated.source_image.data_base64,
                }
            if existing_session:
                conn.execute(
                    """UPDATE domain_record
                       SET body_json = ?, causation_id = ?, state_version = ?, created_at = ?
                       WHERE record_id = ?""",
                    (self.store.json(workout_body), operation_id, after_version, now, record_id),
                )
            else:
                conn.execute(
                    """INSERT INTO domain_record(
                        record_id, user_id, kind, day, body_json, status, causation_id, state_version, created_at
                    ) VALUES (?, ?, 'workout_log', ?, ?, 'active', ?, ?, ?)""",
                    (record_id, user_id, date, self.store.json(workout_body), operation_id, after_version, now),
                )

            data = {
                "session_id": record_id,
                "date": date,
                "completion_rate": workout_body["completion_rate"],
                "rpe_avg": rpe_avg,
                "activity_summary": workout_body["activity_summary"],
                "source_image": source_image_metadata,
                "safety_mode": "restricted" if detected_red_flags else profile["safety_mode"],
            }
            response = self._response(
                operation_id,
                "success",
                data,
                after_version,
                warnings=warnings,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="log_workout",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def _resolve_exercise_baseline_weight(self, conn: Any, user_id: str, exercise_name: str) -> float | None:
        """Resolve confirmed or historical baseline load for an exercise without fabricating values."""
        target_norm = exercise_name.strip().lower()
        # 1. Check confirmed progression state
        prog_row = conn.execute(
            """SELECT body_json FROM domain_record
               WHERE user_id = ? AND kind = 'progression_state' AND status = 'active'
                 AND LOWER(json_extract(body_json, '$.exercise_name')) = ?
               ORDER BY created_at DESC LIMIT 1""",
            (user_id, target_norm),
        ).fetchone()
        if prog_row:
            try:
                p_body = json.loads(prog_row["body_json"])
                if p_body.get("confirmed_weight_kg") is not None:
                    return float(p_body["confirmed_weight_kg"])
            except Exception:
                pass

        # 2. Check latest workout log for weight
        wo_rows = conn.execute(
            """SELECT body_json, kind FROM domain_record
               WHERE user_id = ? AND kind IN ('workout', 'workout_log') AND status = 'active'
               ORDER BY day DESC, created_at DESC LIMIT 10""",
            (user_id,),
        ).fetchall()
        for row in wo_rows:
            try:
                b = json.loads(row["body_json"])
                if row["kind"] == "workout":
                    for ex in b.get("completed_exercises", []):
                        if ex.get("name", "").strip().lower() == target_norm and ex.get("weight_kg") is not None:
                            return float(ex["weight_kg"])
                elif row["kind"] == "workout_log":
                    for s in b.get("actual_sets", []):
                        ename = (s.get("exercise") or s.get("name") or "").strip().lower()
                        if ename == target_norm and s.get("weight_kg") is not None:
                            return float(s["weight_kg"])
            except Exception:
                continue

        return None

    def _evaluate_user_safety_and_recovery(
        self,
        conn: Any,
        user_id: str,
        target_date: str | None = None,
        evidence_window_days: int | None = None,
    ) -> SafetyRecoveryEvaluation:
        """Canonical, shared evaluation of safety mode, deload state, and daily recovery state.

        Unified schema extraction handles nested body.metrics while preserving 0 values.
        Unified thresholds:
          - sleep_hours < 6.0
          - fatigue_level >= 7.0
          - recovery_score < 60
          - 'TRAIN_RECOVERY_01' in triggered_rules
        Strictly filters daily_state by day <= target_date to prevent future entries from shadowing current fatigue.
        """
        profile = conn.execute(
            "SELECT safety_mode, deload_until, safety_flags_json, constraints_json, goals_json, timezone, state_version FROM user_profile WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        safety_mode = profile["safety_mode"] if profile else "normal"
        deload_until = profile["deload_until"] if profile else None
        safety_flags = json.loads(profile["safety_flags_json"]) if profile and profile["safety_flags_json"] else []
        goals = json.loads(profile["goals_json"]) if profile and profile["goals_json"] else {}
        tz_name = profile["timezone"] if profile and profile["timezone"] else "Asia/Shanghai"

        # Parse user health & movement constraints
        raw_constraints = json.loads(profile["constraints_json"]) if profile and profile["constraints_json"] else {}
        constraint_tokens: set[str] = set()
        if isinstance(raw_constraints, dict):
            for k, v in raw_constraints.items():
                constraint_tokens.add(str(k).lower())
                constraint_tokens.add(str(v).lower())
        elif isinstance(raw_constraints, list):
            for item in raw_constraints:
                constraint_tokens.add(str(item).lower())
        elif isinstance(raw_constraints, str):
            constraint_tokens.add(raw_constraints.lower())
        c_text = " ".join(constraint_tokens)

        active_constraints: set[str] = set()
        if any(w in c_text for w in ("knee", "膝", "膝盖", "patella", "meniscus", "acl", "deep_squat", "蹲")):
            active_constraints.add("knee")
        if any(w in c_text for w in ("shoulder", "肩", "impingement", "rotator_cuff", "bench_press", "overhead", "推胸")):
            active_constraints.add("shoulder")
        if any(w in c_text for w in ("lumbar", "腰", "disc", "herniation", "lower_back", "spine", "硬拉")):
            active_constraints.add("lumbar")

        # Resolve target_date in user's timezone
        if target_date is None:
            resolved_date = self._parse_day_in_timezone(self._now(), tz_name)
        elif "T" in target_date:
            resolved_date = self._parse_day_in_timezone(target_date, tz_name)
        else:
            resolved_date = target_date[:10]

        is_restricted = (safety_mode == "restricted" or bool(safety_flags))
        restricted_reason = (
            "System is in Restricted Mode. Training progression and prescriptions are blocked."
            if is_restricted
            else None
        )

        is_deload = bool(deload_until and resolved_date <= deload_until[:10])
        deload_reason = (
            f"User is currently under 7-day Deload period until {deload_until[:10]} (RECOVERY_FLAG_CLEAR_01)."
            if is_deload
            else None
        )

        win_days = evidence_window_days if evidence_window_days is not None else self.recovery_evidence_window_days
        latest_ds = conn.execute(
            """SELECT record_id, day, body_json, created_at FROM domain_record
               WHERE user_id = ? AND kind = 'daily_state' AND day <= ? AND status = 'active'
               ORDER BY day DESC, created_at DESC LIMIT 1""",
            (user_id, resolved_date),
        ).fetchone()

        has_daily_state = bool(latest_ds)
        daily_record_id = latest_ds["record_id"] if latest_ds else None
        daily_day = latest_ds["day"] if latest_ds else None
        is_fresh = False
        age_days = None
        sleep_hours = None
        fatigue_level = None
        recovery_score = None
        sleep_quality = None
        triggered_rules: list[str] = []
        coaching_alert = None
        is_fatigue_or_sleep_deficit = False
        deficit_reasons: list[str] = []

        if latest_ds:
            try:
                rec_d = datetime.strptime(latest_ds["day"][:10], "%Y-%m-%d").date()
                cur_d = datetime.strptime(resolved_date, "%Y-%m-%d").date()
                age_days = (cur_d - rec_d).days
                if 0 <= age_days <= win_days:
                    is_fresh = True
            except Exception:
                pass

            if is_fresh:
                try:
                    d_body = json.loads(latest_ds["body_json"])
                    if isinstance(d_body, dict):
                        metrics = d_body.get("metrics", {}) if isinstance(d_body.get("metrics"), dict) else {}

                        raw_sh = metrics.get("sleep_hours")
                        if raw_sh is None:
                            raw_sh = d_body.get("sleep_hours")
                        if raw_sh is not None:
                            try:
                                sleep_hours = float(raw_sh)
                            except (ValueError, TypeError):
                                sleep_hours = None

                        raw_fl = metrics.get("fatigue_level")
                        if raw_fl is None:
                            raw_fl = d_body.get("fatigue_level")
                        if raw_fl is None:
                            raw_fl = d_body.get("fatigue_score")
                        if raw_fl is not None:
                            try:
                                fatigue_level = float(raw_fl)
                            except (ValueError, TypeError):
                                fatigue_level = None

                        raw_rs = d_body.get("recovery_score")
                        if raw_rs is None:
                            raw_rs = metrics.get("recovery_score")
                        if raw_rs is not None:
                            try:
                                recovery_score = int(round(float(raw_rs)))
                            except (ValueError, TypeError):
                                recovery_score = None

                        sleep_quality = metrics.get("sleep_quality") or d_body.get("sleep_quality")

                        raw_tr = d_body.get("triggered_rules")
                        if isinstance(raw_tr, list):
                            triggered_rules = [str(r) for r in raw_tr]

                        coaching_alert = d_body.get("coaching_alert")
                except Exception:
                    pass

                # Unified Fatigue / Sleep Deficit / Recovery Score Check
                if sleep_hours is not None and sleep_hours < 6.0:
                    is_fatigue_or_sleep_deficit = True
                    deficit_reasons.append(f"sleep_hours ({sleep_hours}h) < 6.0h")
                if fatigue_level is not None and fatigue_level >= 7.0:
                    is_fatigue_or_sleep_deficit = True
                    deficit_reasons.append(f"fatigue_level ({fatigue_level}) >= 7")
                if recovery_score is not None and recovery_score < 60:
                    is_fatigue_or_sleep_deficit = True
                    deficit_reasons.append(f"recovery_score ({recovery_score}) < 60")
                if "TRAIN_RECOVERY_01" in triggered_rules:
                    is_fatigue_or_sleep_deficit = True
                    deficit_reasons.append("TRAIN_RECOVERY_01 in triggered_rules")

        if is_restricted:
            state_evidence = "acute_red_flag"
        elif is_deload:
            state_evidence = "deload_period"
        elif is_fatigue_or_sleep_deficit:
            state_evidence = "fatigue_detected"
        elif is_fresh:
            state_evidence = "verified_recent_state"
        else:
            state_evidence = "unrecorded_recent_state"

        return SafetyRecoveryEvaluation(
            user_id=user_id,
            target_date=resolved_date,
            timezone=tz_name,
            safety_mode=safety_mode,
            safety_flags=safety_flags,
            is_restricted=is_restricted,
            restricted_reason=restricted_reason,
            deload_until=deload_until,
            is_deload=is_deload,
            deload_reason=deload_reason,
            active_constraints=active_constraints,
            raw_constraints=raw_constraints,
            goals=goals,
            has_daily_state=has_daily_state,
            daily_record_id=daily_record_id,
            daily_day=daily_day,
            is_fresh=is_fresh,
            age_days=age_days,
            evidence_window_days=win_days,
            state_evidence=state_evidence,
            sleep_hours=sleep_hours,
            fatigue_level=fatigue_level,
            recovery_score=recovery_score,
            sleep_quality=sleep_quality,
            triggered_rules=triggered_rules,
            coaching_alert=coaching_alert,
            is_fatigue_or_sleep_deficit=is_fatigue_or_sleep_deficit,
            deficit_reasons=deficit_reasons,
        )

    def _evaluate_exercise_progression(
        self,
        conn: Any,
        user_id: str,
        exercise_name: str,
        target_reps_max: int,
        date: str | None = None,
    ) -> dict[str, Any] | None:
        """Evaluate Spec 4.2 double progression state machine for an exercise.

        Requires 2 consecutive completed sessions where target_reps_max was reached
        across all required sets with comparable load and RPE <= 8.0.
        Recent failed, incomplete, missing-set, or missing-RPE sessions break the streak.
        Same-day split records are consolidated into a single session.
        """
        target_norm = exercise_name.strip().lower()

        # Find catalog specification for required sets and rep range
        catalog_entry: dict[str, Any] | None = None
        for k, v in EXERCISE_CATALOG.items():
            if k.strip().lower() == target_norm:
                catalog_entry = v
                break

        # Resolve eval_date to user's local date
        row = conn.execute("SELECT timezone FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
        tz_name = row["timezone"] if row and row["timezone"] else "Asia/Shanghai"
        if date is None:
            eval_date = self._parse_day_in_timezone(self._now(), tz_name)
        elif "T" in date:
            eval_date = self._parse_day_in_timezone(date, tz_name)
        else:
            eval_date = date[:10]

        # Shared Safety & Recovery Gate: no progression suggestions under restricted, deload, or fatigue
        safety_eval = self._evaluate_user_safety_and_recovery(conn, user_id, target_date=eval_date)
        if safety_eval.is_restricted or safety_eval.is_deload or safety_eval.is_fatigue_or_sleep_deficit:
            return None

        if catalog_entry:
            c_set = set(catalog_entry.get("contraindications", []))
            if not c_set.isdisjoint(safety_eval.active_constraints):
                return None

        records = conn.execute(
            """SELECT record_id, day, kind, body_json, created_at FROM domain_record
               WHERE user_id = ? AND kind IN ('workout', 'workout_log') AND status = 'active'
                 AND day <= ?
               ORDER BY day DESC, created_at DESC
               LIMIT 100""",
            (user_id, eval_date),
        ).fetchall()

        required_sets = (catalog_entry.get("default_sets") or catalog_entry.get("sets", 3)) if catalog_entry else 3
        catalog_reps_max = catalog_entry.get("reps_max", target_reps_max) if catalog_entry else target_reps_max
        target_reps = target_reps_max or catalog_reps_max

        # Group records by session key to consolidate same-day split records
        sessions_by_key: dict[str, dict[str, Any]] = {}

        for row in records:
            try:
                body = json.loads(row["body_json"])
            except Exception:
                continue

            has_exercise = False
            matching_sets: list[dict[str, Any]] = []
            matching_exercises: list[dict[str, Any]] = []
            record_rpes: list[float] = []

            if row["kind"] == "workout_log":
                for s in body.get("actual_sets", []):
                    ename = (s.get("exercise") or s.get("name") or "").strip().lower()
                    if ename == target_norm:
                        has_exercise = True
                        matching_sets.append(s)
                        if s.get("rpe") is not None:
                            record_rpes.append(float(s["rpe"]))
                if not has_exercise:
                    for pe in body.get("planned_exercises", []):
                        if str(pe).strip().lower() == target_norm:
                            has_exercise = True
                            break
                if body.get("rpe_avg") is not None and not record_rpes:
                    record_rpes.append(float(body["rpe_avg"]))

            elif row["kind"] == "workout":
                for ex in body.get("completed_exercises", []):
                    ename = ex.get("name", "").strip().lower()
                    if ename == target_norm:
                        has_exercise = True
                        matching_exercises.append(ex)
                        if ex.get("rpe") is not None:
                            record_rpes.append(float(ex["rpe"]))
                if body.get("session_rpe") is not None and not record_rpes:
                    record_rpes.append(float(body["session_rpe"]))

            if not has_exercise:
                continue

            session_id = body.get("session_id")
            skey = f"{row['day']}_{session_id}" if session_id else f"{row['day']}"

            if skey not in sessions_by_key:
                sessions_by_key[skey] = {
                    "skey": skey,
                    "day": row["day"],
                    "record_ids": [],
                    "completion_rates": [],
                    "matching_sets": [],
                    "matching_exercises": [],
                    "rpes": [],
                }

            sess = sessions_by_key[skey]
            sess["record_ids"].append(row["record_id"])
            if body.get("completion_rate") is not None:
                sess["completion_rates"].append(float(body["completion_rate"]))
            sess["matching_sets"].extend(matching_sets)
            sess["matching_exercises"].extend(matching_exercises)
            sess["rpes"].extend(record_rpes)

        # Evaluate each session
        session_list: list[dict[str, Any]] = []
        for sess in sessions_by_key.values():
            # 1. Completion rate check
            if sess["completion_rates"] and min(sess["completion_rates"]) < 1.0:
                sess["success"] = False
                sess["fail_reason"] = "incomplete_session"
                session_list.append(sess)
                continue

            # 2. RPE check: missing or > 8.0 fails
            if not sess["rpes"]:
                sess["success"] = False
                sess["fail_reason"] = "missing_rpe"
                session_list.append(sess)
                continue

            avg_rpe = sum(sess["rpes"]) / len(sess["rpes"])
            sess["rpe"] = avg_rpe
            if avg_rpe > 8.0:
                sess["success"] = False
                sess["fail_reason"] = "rpe_too_high"
                session_list.append(sess)
                continue

            # 3. Sets, Reps, and Load check
            if sess["matching_sets"]:
                unique_sets: list[dict[str, Any]] = []
                seen_keys: set[Any] = set()
                for s in sess["matching_sets"]:
                    k = (s.get("set_num"), s.get("reps"), s.get("weight_kg"), s.get("rpe"))
                    if s.get("set_num") is not None:
                        if k in seen_keys:
                            continue
                        seen_keys.add(k)
                    unique_sets.append(s)

                if len(unique_sets) < required_sets:
                    sess["success"] = False
                    sess["fail_reason"] = "insufficient_sets"
                    session_list.append(sess)
                    continue

                set_reps = [int(s.get("reps", 0)) for s in unique_sets if s.get("reps") is not None]
                if not set_reps or any(r < target_reps for r in set_reps):
                    sess["success"] = False
                    sess["fail_reason"] = "reps_short_of_target"
                    session_list.append(sess)
                    continue

                weights = [float(s["weight_kg"]) for s in unique_sets if s.get("weight_kg") is not None]
                if weights:
                    if any(w != weights[0] for w in weights):
                        sess["success"] = False
                        sess["fail_reason"] = "inconsistent_set_weights"
                        session_list.append(sess)
                        continue
                    sess["weight_kg"] = weights[0]
                else:
                    sess["weight_kg"] = None

            elif sess["matching_exercises"]:
                ex = sess["matching_exercises"][0]
                ex_sets = int(ex.get("sets", 0)) if ex.get("sets") is not None else None
                if ex_sets is None or ex_sets < required_sets:
                    sess["success"] = False
                    sess["fail_reason"] = "missing_or_insufficient_sets"
                    session_list.append(sess)
                    continue

                ex_reps = int(ex.get("reps", 0)) if ex.get("reps") is not None else 0
                if ex_reps < target_reps:
                    sess["success"] = False
                    sess["fail_reason"] = "reps_short_of_target"
                    session_list.append(sess)
                    continue

                sess["weight_kg"] = float(ex["weight_kg"]) if ex.get("weight_kg") is not None else None

            else:
                sess["success"] = False
                sess["fail_reason"] = "no_exercise_data"
                session_list.append(sess)
                continue

            sess["success"] = True
            session_list.append(sess)

        # Must have at least 2 distinct sessions containing this exercise
        if len(session_list) < 2:
            return None

        # Take the most recent 2 sessions
        s1 = session_list[0]
        s2 = session_list[1]

        # Streak check: both must be successful!
        if not s1.get("success") or not s2.get("success"):
            return None

        # Load comparability check between s1 and s2
        w1 = s1.get("weight_kg")
        w2 = s2.get("weight_kg")

        if w1 is not None and w2 is not None:
            if abs(w1 - w2) > 0.01:
                return None
            increment = 2.5 if any(k in target_norm for k in ("squat", "thrust", "deadlift")) else 1.25
            suggested_weight = round(w1 + increment, 2)
            suggested_reps = target_reps
        elif w1 is None and w2 is None:
            increment = None
            suggested_weight = None
            suggested_reps = target_reps + 1
        else:
            return None

        evidence_ids = sorted(list(set(s1["record_ids"] + s2["record_ids"])))
        exercise_slug = re.sub(r"[^a-z0-9_]+", "_", target_norm).strip("_")
        sig_payload = f"{user_id}:{target_norm}:{','.join(evidence_ids)}:{suggested_weight}:{suggested_reps}"
        proposal_sig = hashlib.sha256(sig_payload.encode("utf-8")).hexdigest()[:16]
        proposal_id = f"prop_{exercise_slug}_{proposal_sig}"

        return {
            "proposal_id": proposal_id,
            "proposal_signature": proposal_sig,
            "rule_code": "TRAIN_PROGRESS_01",
            "exercise_name": exercise_name,
            "status": "pending_confirmation",
            "current_weight_kg": w1,
            "suggested_weight_kg": suggested_weight,
            "suggested_increment_kg": increment,
            "current_reps": target_reps,
            "suggested_reps": suggested_reps,
            "rationale": (
                f"动作 '{exercise_name}' 连续2次训练达到目标次数上限（{target_reps}次）且RPE<=8.0。"
                f"依据Double Progression双重渐进原则，提议微增 {increment}kg 负荷（需用户确认方生效）。"
                if increment else
                f"动作 '{exercise_name}' 连续2次训练达到目标次数上限（{target_reps}次）且RPE<=8.0。"
                f"依据Double Progression双重渐进原则，提议增加组次目标至 {suggested_reps} 次（需用户确认方生效）。"
            ),
            "evidence_source_record_ids": evidence_ids,
            "requires_user_confirmation": True,
            "thresholds_disclosed": {
                "required_consecutive_sessions": 2,
                "required_completion_rate": 1.0,
                "max_allowed_rpe": 8.0,
                "target_reps_per_set": target_reps,
                "required_sets": required_sets,
            },
        }

    def _evaluate_training_prescription(
        self,
        conn: Any,
        user_id: str,
        date: str,
        equipment: list[str] | None = None,
        target_duration_min: int = 45,
        evidence_window_days: int | None = None,
    ) -> tuple[dict[str, Any], str, str | None]:
        """Unified deterministic safety check and exercise prescription across all endpoints.

        Respects constraints_json, available equipment, and recent daily recovery evidence.
        Returns:
            (prescription_dict, summary_plan_text, safety_alert_text)
        """
        safety_eval = self._evaluate_user_safety_and_recovery(
            conn, user_id, target_date=date, evidence_window_days=evidence_window_days
        )

        has_knee_constraint = "knee" in safety_eval.active_constraints
        has_shoulder_constraint = "shoulder" in safety_eval.active_constraints
        has_lumbar_constraint = "lumbar" in safety_eval.active_constraints

        constraints_applied: list[str] = []
        if has_knee_constraint:
            constraints_applied.append("避开膝关节深屈曲动作（深蹲替换为臀桥/后链动作）")
        if has_shoulder_constraint:
            constraints_applied.append("避开肩部过头推举与大角度推胸动作（替换为中立位拉类/躯干支撑）")
        if has_lumbar_constraint:
            constraints_applied.append("避开脊柱轴向重载硬拉与深蹲（替换为臀桥与无负重体能）")

        # Parse training experience / background
        training_exp_val = safety_eval.goals.get("training_experience") or safety_eval.goals.get("experience_level")
        if not training_exp_val and isinstance(safety_eval.raw_constraints, dict):
            training_exp_val = safety_eval.raw_constraints.get("experience_level") or safety_eval.raw_constraints.get("training_experience")
        exp_level = str(training_exp_val).lower() if training_exp_val else "unconfigured"

        # Parse available equipment
        eq_list = [e.lower() for e in (equipment or [])]
        has_barbell = "barbell" in eq_list
        has_dumbbell = "dumbbell" in eq_list or "dumbbells" in eq_list
        equipment_mode = "barbell" if has_barbell else ("dumbbell" if has_dumbbell else "bodyweight")

        def _build_exercise_entry(ex_name: str, sets: int, rir: int, custom_reps: int | None = None) -> dict[str, Any]:
            info = EXERCISE_CATALOG.get(ex_name, {})
            patt = info.get("movement_pattern", "general")
            reps_min = info.get("reps_min", 8)
            reps_max = info.get("reps_max", 10)
            rep_target = custom_reps if custom_reps is not None else reps_max
            rest_sec = info.get("rest_seconds", 90)

            baseline_w = self._resolve_exercise_baseline_weight(conn, user_id, ex_name)
            if baseline_w is not None:
                weight_guidance = f"已知负荷基准：{baseline_w}kg。"
            else:
                weight_guidance = "负荷未知：首次执行请采用自测适宜重量，保证最后1-2次具有挑战性且动作不形变(RPE 7-8)，切勿盲目上大重量。"

            return {
                "name": ex_name,
                "movement_pattern": patt,
                "sets": sets,
                "reps": rep_target,
                "target_reps_min": reps_min,
                "target_reps_max": reps_max,
                "rir": rir,
                "rest_seconds": rest_sec,
                "suggested_weight_kg": baseline_w,
                "weight_guidance": weight_guidance,
            }

        # 1. Level 1: Restricted mode (acute red-flag lock)
        if safety_eval.is_restricted:
            prescription = {
                "rule_code": "SAFETY_RESTRICTED",
                "focus": "REST_AND_CLINICAL_EVALUATION",
                "intensity_baseline_pct": 0,
                "target_duration_min": 0,
                "min_rir": None,
                "prescribed_exercises": [],
                "guidance": "急性红旗症状锁定中，严禁进行任何力量训练或散步。请立即停止所有运动并前往急诊或医院专科排查。",
                "state_evidence": "acute_red_flag",
                "evidence_window_days": safety_eval.evidence_window_days,
                "evidence_age_days": safety_eval.age_days,
                "training_experience": exp_level,
                "equipment_mode": equipment_mode,
                "constraints_applied": constraints_applied,
                "disclaimer": "本受限阻断为安全防护规则，非临床诊断。",
            }
            return (
                prescription,
                "受限模式（Restricted Mode）：检测到严重红旗指征（如胸痛/呼吸困难），严禁进行任何力量训练、运动或散步。请立即停止一切活动，保持绝对静养并即刻就医诊治。",
                "TRAIN_SAFETY_01: 系统处于安全受限模式，已强制阻断所有运动与训练处方。",
            )

        # 2. Level 2: 7-day Deload protective period
        if safety_eval.is_deload:
            if has_knee_constraint or has_lumbar_constraint:
                lower_ex = _build_exercise_entry("Glute Bridge", sets=2, rir=4, custom_reps=12)
            else:
                lower_ex = _build_exercise_entry("Bodyweight Squat", sets=2, rir=4, custom_reps=10)

            if has_shoulder_constraint:
                upper_ex = _build_exercise_entry("Bird Dog", sets=2, rir=4, custom_reps=10)
            else:
                upper_ex = _build_exercise_entry("Incline Pushup", sets=2, rir=4, custom_reps=10)

            deload_guidance = "7天解除保护性减载期生效中，严格限制负荷<=50%-60%基线，RIR>=3，严禁力竭。"
            if constraints_applied:
                deload_guidance += " 注意：" + "；".join(constraints_applied) + "。"

            prescription = {
                "rule_code": "RECOVERY_FLAG_CLEAR_01",
                "focus": "DELOAD_PROTECTIVE_PERIOD",
                "intensity_baseline_pct": 50,
                "target_duration_min": min(25, target_duration_min),
                "min_rir": 3,
                "prescribed_exercises": [lower_ex, upper_ex],
                "guidance": deload_guidance,
                "state_evidence": "deload_period",
                "evidence_window_days": safety_eval.evidence_window_days,
                "evidence_age_days": safety_eval.age_days,
                "training_experience": exp_level,
                "equipment_mode": equipment_mode,
                "constraints_applied": constraints_applied,
                "disclaimer": "本减载恢复指导基于运动防护原则生成，非临床处方。",
            }
            return (
                prescription,
                "7天减载期（RECOVERY_FLAG_CLEAR_01）：负荷锁定≤50-60%基线，RIR≥3，严禁力竭",
                "RECOVERY_FLAG_CLEAR_01: 处于康复后7天减载期，负荷已限制。",
            )

        # 3. Level 3: Fatigue / Sleep deficit (TRAIN_RECOVERY_01)
        if safety_eval.is_fatigue_or_sleep_deficit:
            if has_dumbbell:
                if has_knee_constraint and has_lumbar_constraint:
                    r_lower = _build_exercise_entry("Dumbbell Hip Thrust", sets=3, rir=3, custom_reps=10)
                elif has_knee_constraint:
                    r_lower = _build_exercise_entry("Dumbbell Romanian Deadlift", sets=3, rir=3, custom_reps=8)
                elif has_lumbar_constraint:
                    r_lower = _build_exercise_entry("Dumbbell Goblet Squat", sets=3, rir=3, custom_reps=8)
                else:
                    r_lower = _build_exercise_entry("Dumbbell Goblet Squat", sets=3, rir=3, custom_reps=8)

                if has_shoulder_constraint:
                    r_upper = _build_exercise_entry("Dumbbell Chest Supported Row", sets=3, rir=3, custom_reps=8)
                else:
                    r_upper = _build_exercise_entry("Dumbbell Floor Press", sets=3, rir=3, custom_reps=8)
            elif has_barbell:
                if has_knee_constraint and has_lumbar_constraint:
                    r_lower = _build_exercise_entry("Glute Bridge", sets=3, rir=3, custom_reps=10)
                elif has_lumbar_constraint:
                    r_lower = _build_exercise_entry("Bodyweight Squat", sets=3, rir=3, custom_reps=8)
                elif has_knee_constraint:
                    r_lower = _build_exercise_entry("Romanian Deadlift", sets=3, rir=3, custom_reps=8)
                else:
                    r_lower = _build_exercise_entry("Romanian Deadlift", sets=3, rir=3, custom_reps=8)

                if has_shoulder_constraint:
                    r_upper = _build_exercise_entry("Bird Dog" if has_lumbar_constraint else "Barbell Row", sets=3, rir=3, custom_reps=8)
                else:
                    r_upper = _build_exercise_entry("Incline Pushup", sets=3, rir=3, custom_reps=8)
            else:
                r_lower = _build_exercise_entry("Glute Bridge" if (has_knee_constraint or has_lumbar_constraint) else "Bodyweight Squat", sets=3, rir=3, custom_reps=10)
                r_upper = _build_exercise_entry("Bird Dog" if has_shoulder_constraint else "Pushup", sets=3, rir=3, custom_reps=8)

            rec_guidance = "检测到睡眠不足（<6小时）或疲劳偏高，自动下调训练量20-30%，离心控制2秒。"
            if constraints_applied:
                rec_guidance += " 注意：" + "；".join(constraints_applied) + "。"

            prescription = {
                "rule_code": "TRAIN_RECOVERY_01",
                "focus": "FATIGUE_REDUCTION_LIGHT",
                "intensity_baseline_pct": 70,
                "target_duration_min": min(30, target_duration_min),
                "min_rir": 2,
                "prescribed_exercises": [r_lower, r_upper],
                "guidance": rec_guidance,
                "state_evidence": "fatigue_detected",
                "evidence_window_days": safety_eval.evidence_window_days,
                "evidence_age_days": safety_eval.age_days,
                "training_experience": exp_level,
                "equipment_mode": equipment_mode,
                "constraints_applied": constraints_applied,
                "disclaimer": "本疲劳自适应方案基于运动恢复学原则生成，非临床诊断。",
            }
            return (
                prescription,
                "疲劳自适应降载（TRAIN_RECOVERY_01）：负荷下调20%-40%或进行技术动作巩固与轻度拉伸",
                "TRAIN_RECOVERY_01: 检测到疲劳/睡眠不足，已自动生成降载计划。",
            )

        # 4. Level 4: Standard progressive overload respecting available equipment and constraints
        is_advanced = exp_level in ("advanced", "高阶", "资深", "athlete")
        std_sets = 4 if is_advanced else 3
        std_rir = 1 if is_advanced else 2

        if has_barbell:
            if has_knee_constraint or has_lumbar_constraint:
                ex1 = _build_exercise_entry("Barbell Hip Thrust", sets=std_sets, rir=std_rir, custom_reps=8)
            else:
                ex1 = _build_exercise_entry("Barbell Back Squat", sets=std_sets, rir=std_rir, custom_reps=8)

            if has_shoulder_constraint:
                ex2 = _build_exercise_entry("Bird Dog" if has_lumbar_constraint else "Barbell Row", sets=std_sets, rir=std_rir, custom_reps=8)
            else:
                ex2 = _build_exercise_entry("Barbell Bench Press", sets=std_sets, rir=std_rir, custom_reps=8)

            if has_lumbar_constraint or has_knee_constraint:
                ex3 = _build_exercise_entry("Glute Bridge", sets=std_sets, rir=std_rir, custom_reps=10)
            else:
                ex3 = _build_exercise_entry("Romanian Deadlift", sets=std_sets, rir=std_rir, custom_reps=10)
            exercises = [ex1, ex2, ex3]
        elif has_dumbbell:
            if has_knee_constraint or has_lumbar_constraint:
                ex1 = _build_exercise_entry("Dumbbell Hip Thrust", sets=std_sets, rir=std_rir, custom_reps=10)
            else:
                ex1 = _build_exercise_entry("Dumbbell Goblet Squat", sets=std_sets, rir=std_rir, custom_reps=10)

            if has_shoulder_constraint:
                ex2 = _build_exercise_entry("Dumbbell Chest Supported Row", sets=std_sets, rir=std_rir, custom_reps=10)
            else:
                ex2 = _build_exercise_entry("Dumbbell Floor Press", sets=std_sets, rir=std_rir, custom_reps=10)

            if has_lumbar_constraint or has_knee_constraint:
                ex3 = _build_exercise_entry("Glute Bridge", sets=std_sets, rir=std_rir, custom_reps=12)
            else:
                ex3 = _build_exercise_entry("Dumbbell Romanian Deadlift", sets=std_sets, rir=std_rir, custom_reps=12)
            exercises = [ex1, ex2, ex3]
        else:
            if has_knee_constraint or has_lumbar_constraint:
                ex1 = _build_exercise_entry("Glute Bridge", sets=std_sets, rir=std_rir, custom_reps=15)
            else:
                ex1 = _build_exercise_entry("Bodyweight Squat", sets=std_sets, rir=std_rir, custom_reps=15)

            if has_shoulder_constraint:
                ex2 = _build_exercise_entry("Bird Dog", sets=std_sets, rir=std_rir, custom_reps=12)
            else:
                ex2 = _build_exercise_entry("Pushup", sets=std_sets, rir=std_rir, custom_reps=12)

            if has_lumbar_constraint:
                ex3 = _build_exercise_entry("Bird Dog", sets=std_sets, rir=std_rir, custom_reps=15)
            else:
                ex3 = _build_exercise_entry("Glute Bridge", sets=std_sets, rir=std_rir, custom_reps=15)
            exercises = [ex1, ex2, ex3]

        # Strict post-filter check against all active constraints
        filtered_exercises: list[dict[str, Any]] = []
        for ex in exercises:
            c_info = EXERCISE_CATALOG.get(ex["name"], {})
            if c_info.get("contraindications", set()).isdisjoint(safety_eval.active_constraints):
                filtered_exercises.append(ex)

        if not filtered_exercises:
            prescription = {
                "rule_code": "TRAIN_CONSTRAINTS_SUSPENDED",
                "focus": "SUSPEND_SPECIFIC_MOVEMENTS_REFER_CLINICAL",
                "intensity_baseline_pct": 0,
                "target_duration_min": 0,
                "min_rir": None,
                "prescribed_exercises": [],
                "guidance": (
                    "检测到多处并发关节/脊柱限制（" + "、".join(constraints_applied) +
                    "），当前可用器械下缺乏满足全部安全过滤的抗阻候选动作。为防继发损伤，已暂停具体抗阻动作处方生成。"
                    "建议咨询持证物理治疗师或运动医学医师制定个性化康复性训练。"
                ),
                "state_evidence": "unrecorded_recent_state" if not safety_eval.is_fresh else "verified_recent_state",
                "evidence_window_days": safety_eval.evidence_window_days,
                "evidence_age_days": safety_eval.age_days,
                "training_experience": exp_level,
                "equipment_mode": equipment_mode,
                "constraints_applied": constraints_applied,
                "disclaimer": "本阻断基于运动安全与多重禁忌过滤原则生成，非临床诊断或医疗处方。",
            }
            return (
                prescription,
                "因多重运动限制暂停动作处方生成，建议寻求专业医疗或物理康复指导",
                "TRAIN_CONSTRAINTS_01: 检测到多重关节/脊柱禁忌冲突，已安全暂停抗阻动作处方。",
            )

        exercises = filtered_exercises

        # Check for Double Progression (TRAIN_PROGRESS_01)
        progression_suggestions: list[dict[str, Any]] = []
        for ex in exercises:
            sugg = self._evaluate_exercise_progression(
                conn, user_id, ex["name"], target_reps_max=ex["target_reps_max"], date=date
            )
            if sugg:
                progression_suggestions.append(sugg)

        # State evidence disclosure
        if safety_eval.is_fresh and safety_eval.has_daily_state:
            guidance = "近期体征显示恢复良好（睡眠与疲劳指数正常），可按计划执行周期渐进超负荷，保持动作规范与RIR余量，做好组间间歇（2-3分钟）。"
            summary_text = "标准自适应力量训练与有氧平衡"
            state_evidence = "verified_recent_state"
        else:
            guidance = f"未检测到近期体征记录（最近{safety_eval.evidence_window_days}天内无晨起打卡数据，历史记录已过期或未录入）。以下为基于可用器械的通用渐进式参考方案（非个性化处方），建议每日录入晨起体征（log_daily_metrics）以获得自适应负荷调整。"
            if exp_level == "unconfigured":
                guidance += "（注：训练经验未配置，采用保守基础容量基准）。"
            summary_text = "通用自适应力量训练（注：近期体征未记录，建议先记录日常指标）"
            state_evidence = "unrecorded_recent_state"

        if constraints_applied:
            guidance += " 注意：" + "；".join(constraints_applied) + "。"

        if progression_suggestions:
            guidance += f" 包含 {len(progression_suggestions)} 项待确认加重建议（TRAIN_PROGRESS_01）。"

        prescription = {
            "rule_code": "TRAIN_PROGRESSION_STANDARD",
            "focus": "PROGRESSIVE_RESISTANCE_OVERLOAD",
            "intensity_baseline_pct": 100,
            "target_duration_min": target_duration_min,
            "min_rir": std_rir,
            "prescribed_exercises": exercises,
            "progression_suggestions": progression_suggestions,
            "guidance": guidance,
            "state_evidence": state_evidence,
            "evidence_window_days": safety_eval.evidence_window_days,
            "evidence_age_days": safety_eval.age_days,
            "training_experience": exp_level,
            "equipment_mode": equipment_mode,
            "constraints_applied": constraints_applied,
            "disclaimer": "本方案基于运动训练学原则生成，未经持证医师或体能专家面诊，不构成医疗处方。训练中如感不适请即刻中止。",
        }
        return (
            prescription,
            summary_text,
            None,
        )

    def _determine_safe_workout_plan(
        self,
        conn: Any,
        user_id: str,
        date: str,
        profile: Any,
    ) -> tuple[str, str, str | None, dict[str, Any]]:
        constraints = json.loads(profile["constraints_json"]) if profile and profile["constraints_json"] else {}
        equipment = constraints.get("available_equipment") or constraints.get("equipment") or []
        if isinstance(equipment, str):
            equipment = [equipment]
        target_duration = constraints.get("session_duration_min", 45)
        try:
            target_duration = max(10, min(180, int(target_duration)))
        except (TypeError, ValueError):
            target_duration = 45
        prescription, summary, alert = self._evaluate_training_prescription(
            conn,
            user_id,
            date,
            equipment=equipment,
            target_duration_min=target_duration,
        )
        min_plan = "15分钟自重/弹力带应急核心激活"
        if prescription["rule_code"] == "SAFETY_RESTRICTED":
            min_plan = "绝对卧床静养、监测体征并立即就医"
        elif prescription["rule_code"] == "RECOVERY_FLAG_CLEAR_01":
            min_plan = "10分钟低强度动态拉伸"
        elif prescription["rule_code"] == "TRAIN_CONSTRAINTS_SUSPENDED":
            min_plan = "暂停抗阻训练，按专业医疗/康复建议静养或进行温和呼吸放松"
        return summary, min_plan, alert, prescription

    def daily_review(
        self,
        *,
        user_id: str,
        date: str,
        idempotency_key: str,
        user_notes: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            DailyReviewInput(
                user_id=user_id,
                date=date,
                idempotency_key=idempotency_key,
                user_notes=user_notes,
                expected_state_version=expected_state_version,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {
            "action": "daily_review",
            "user_id": user_id,
            "date": date,
            "user_notes": user_notes,
            "expected_state_version": expected_state_version,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "daily_review", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]
            tz_name = profile["timezone"]

            if expected_state_version is not None and expected_state_version != before_version:
                raise ConflictError(
                    f"Expected version {expected_state_version}, current version is {before_version}"
                )

            # Check user notes for red flags
            if user_notes:
                detected_rf = [kw for kw in RED_FLAG_KEYWORDS if kw in user_notes]
                if detected_rf:
                    flags = json.loads(profile["safety_flags_json"])
                    for rf in detected_rf:
                        if rf not in flags:
                            flags.append(rf)
                    conn.execute(
                        "UPDATE user_profile SET safety_mode = 'restricted', safety_flags_json = ? WHERE user_id = ?",
                        (self.store.json(flags), user_id),
                    )
                    profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()

            totals = self._today_totals(conn, user_id, date, tz_name, include_statistical=True)
            is_missing = totals["meal_count"] == 0
            fact_collection = self._daily_review_facts(conn, user_id, date, tz_name)

            if is_missing:
                summary = "今日未记录饮食数据。系统未假设断食，建议稍后补记或直接开启明日预案。"
                recording_status = "no_data"
            else:
                k_uncertainty = totals.get("kcal_uncertainty_range", [totals["kcal_low"], totals["kcal_high"]])
                p_uncertainty = totals.get("protein_uncertainty_range", [totals["protein_low"], totals["protein_high"]])
                k_m = totals.get("kcal_mid", round((totals["kcal_low"] + totals["kcal_high"]) / 2.0))
                p_m = totals.get("protein_mid", round((totals["protein_low"] + totals["protein_high"]) / 2.0))
                if totals["meal_count"] > 1 and (
                    k_uncertainty != [totals["kcal_low"], totals["kcal_high"]]
                    or p_uncertainty != [totals["protein_low"], totals["protein_high"]]
                ):
                    summary = (
                        f"今日已记录 {totals['meal_count']} 餐，累计摄入约 {k_m} kcal（合成不确定性范围 {k_uncertainty[0]}–{k_uncertainty[1]} kcal，原始估算范围 {totals['kcal_low']}–{totals['kcal_high']} kcal），"
                        f"蛋白质约 {p_m} g（合成不确定性范围 {p_uncertainty[0]}–{p_uncertainty[1]} g，原始估算范围 {totals['protein_low']}–{totals['protein_high']} g）。"
                    )
                else:
                    summary = (
                        f"今日已记录 {totals['meal_count']} 餐，累计摄入约 {k_m} kcal（估算范围 {totals['kcal_low']}–{totals['kcal_high']} kcal），"
                        f"蛋白质约 {p_m} g（估算范围 {totals['protein_low']}–{totals['protein_high']} g）。"
                    )
                recording_status = "active"

            tomorrow_date = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            workout_plan, min_plan, safety_alert, training_plan = self._determine_safe_workout_plan(
                conn, user_id, tomorrow_date, profile
            )

            goals = json.loads(profile["goals_json"])
            nutr_targets = self._resolve_nutrition_targets(goals)
            if nutr_targets:
                nutr_plan = {
                    "kcal_low": nutr_targets["kcal_low"],
                    "kcal_high": nutr_targets["kcal_high"],
                    "protein_target_g": nutr_targets["protein_target_g"],
                    "status": nutr_targets["status"],
                }
                kcal_gap = [
                    nutr_targets["kcal_low"] - totals["kcal_high"],
                    nutr_targets["kcal_high"] - totals["kcal_low"],
                ]
                if is_missing:
                    kcal_status = "insufficient_intake_data"
                elif kcal_gap[0] > 0:
                    kcal_status = "below_target"
                elif kcal_gap[1] < 0:
                    kcal_status = "above_target"
                else:
                    kcal_status = "within_or_overlapping_target"

                # The target is a policy interval, not a random measurement.  Its
                # interaction with intake estimates is therefore reported only as
                # an arithmetic interval; no target width is converted into sigma.
                t_kcal_mid = round((nutr_targets["kcal_low"] + nutr_targets["kcal_high"]) / 2.0)
                kcal_intake_mid = totals.get("kcal_mid", round((totals["kcal_low"] + totals["kcal_high"]) / 2.0))
                calorie_gap_mid = (t_kcal_mid - kcal_intake_mid) if not is_missing else None
                calorie_gap_uncertainty_range = kcal_gap if not is_missing else None
                calorie_gap_ci90 = None

                if is_missing:
                    kcal_status_precise = "insufficient_intake_data"
                elif kcal_intake_mid < nutr_targets["kcal_low"]:
                    kcal_status_precise = "below_target"
                elif kcal_intake_mid > nutr_targets["kcal_high"]:
                    kcal_status_precise = "above_target"
                else:
                    kcal_status_precise = "target_met"

                protein_gap: list[int] | None = None
                protein_status = "target_unconfigured"
                protein_gap_mid: int | None = None
                protein_gap_ci90: list[int] | None = None
                protein_status_precise = "target_unconfigured"

                if nutr_targets["protein_range"]:
                    protein_gap = [
                        nutr_targets["protein_range"][0] - totals["protein_high"],
                        nutr_targets["protein_range"][1] - totals["protein_low"],
                    ]
                    if is_missing:
                        protein_status = "insufficient_intake_data"
                    elif protein_gap[0] > 0:
                        protein_status = "below_target"
                    elif protein_gap[1] < 0:
                        protein_status = "above_target"
                    else:
                        protein_status = "within_or_overlapping_target"

                    t_prot_mid = round((nutr_targets["protein_range"][0] + nutr_targets["protein_range"][1]) / 2.0)
                    protein_intake_mid = totals.get("protein_mid", round((totals["protein_low"] + totals["protein_high"]) / 2.0))
                    protein_gap_mid = (t_prot_mid - protein_intake_mid) if not is_missing else None
                    protein_gap_uncertainty_range = protein_gap if not is_missing else None
                    protein_gap_ci90 = None

                    if is_missing:
                        protein_status_precise = "insufficient_intake_data"
                    elif protein_intake_mid < nutr_targets["protein_range"][0]:
                        protein_status_precise = "below_target"
                    elif protein_intake_mid > nutr_targets["protein_range"][1]:
                        protein_status_precise = "above_target"
                    else:
                        protein_status_precise = "target_met"

                nutrition_analysis = {
                    "status": "insufficient_intake_data" if is_missing else "available",
                    "intake_kcal_range": [totals["kcal_low"], totals["kcal_high"]],
                    "intake_kcal_mid": totals.get("kcal_mid", round((totals["kcal_low"] + totals["kcal_high"]) / 2.0)),
                    "intake_kcal_uncertainty_range": totals.get(
                        "kcal_uncertainty_range", [totals["kcal_low"], totals["kcal_high"]]
                    ),
                    "intake_kcal_ci90": None,
                    "target_kcal_range": [nutr_targets["kcal_low"], nutr_targets["kcal_high"]],
                    "calorie_target_gap_range": kcal_gap if not is_missing else None,
                    "calorie_gap_mid": calorie_gap_mid,
                    "calorie_gap_uncertainty_range": calorie_gap_uncertainty_range,
                    "calorie_gap_ci90": calorie_gap_ci90,
                    "calorie_target_gap_status": kcal_status,
                    "calorie_target_gap_status_precise": kcal_status_precise,
                    "intake_protein_g_range": [totals["protein_low"], totals["protein_high"]],
                    "intake_protein_mid": totals.get("protein_mid", round((totals["protein_low"] + totals["protein_high"]) / 2.0)),
                    "intake_protein_uncertainty_range": totals.get(
                        "protein_uncertainty_range", [totals["protein_low"], totals["protein_high"]]
                    ),
                    "intake_protein_ci90": None,
                    "target_protein_g_range": nutr_targets["protein_range"],
                    "protein_target_gap_range": protein_gap if not is_missing else None,
                    "protein_gap_mid": protein_gap_mid,
                    "protein_gap_uncertainty_range": protein_gap_uncertainty_range if nutr_targets["protein_range"] else None,
                    "protein_gap_ci90": protein_gap_ci90,
                    "protein_target_gap_status": protein_status,
                    "protein_target_gap_status_precise": protein_status_precise,
                    "gap_semantics": "正数表示距离当日目标尚有缺口，负数表示已超过目标；这是摄入目标差，不等同于能量消耗或脂肪变化。",
                    "uncertainty_semantics": {
                        "method": totals.get("uncertainty_method"),
                        "confidence_level": None,
                        "description": "餐食 low/high 是估算边界；合成范围是透明的 RSS 启发式，不是统计置信区间。目标范围是政策容差，不参与测量误差合成。",
                    },
                }
                action_item = "早起测量空腹体重与睡眠质量，锁定晨间执行计划。"
            else:
                nutr_plan = None
                nutrition_analysis = {
                    "status": "target_unconfigured",
                    "intake_kcal_range": [totals["kcal_low"], totals["kcal_high"]] if not is_missing else None,
                    "intake_kcal_mid": totals.get("kcal_mid", round((totals["kcal_low"] + totals["kcal_high"]) / 2.0)) if not is_missing else None,
                    "intake_kcal_uncertainty_range": totals.get("kcal_uncertainty_range") if not is_missing else None,
                    "intake_kcal_ci90": None,
                    "target_kcal_range": None,
                    "calorie_target_gap_range": None,
                    "calorie_gap_mid": None,
                    "calorie_gap_uncertainty_range": None,
                    "calorie_gap_ci90": None,
                    "calorie_target_gap_status": "target_unconfigured",
                    "calorie_target_gap_status_precise": "target_unconfigured",
                    "intake_protein_g_range": [totals["protein_low"], totals["protein_high"]] if not is_missing else None,
                    "intake_protein_mid": totals.get("protein_mid", round((totals["protein_low"] + totals["protein_high"]) / 2.0)) if not is_missing else None,
                    "intake_protein_uncertainty_range": totals.get("protein_uncertainty_range") if not is_missing else None,
                    "intake_protein_ci90": None,
                    "target_protein_g_range": None,
                    "protein_target_gap_range": None,
                    "protein_gap_mid": None,
                    "protein_gap_uncertainty_range": None,
                    "protein_gap_ci90": None,
                    "protein_target_gap_status": "target_unconfigured",
                    "protein_target_gap_status_precise": "target_unconfigured",
                    "gap_semantics": "未配置目标时禁止猜测热量或蛋白质缺口。",
                    "uncertainty_semantics": {
                        "method": totals.get("uncertainty_method") if not is_missing else "no_data",
                        "confidence_level": None,
                        "description": "餐食 low/high 是估算边界；合成范围是透明的 RSS 启发式，不是统计置信区间。",
                    },
                }
                action_item = "档案中营养目标未配置，建议先更新个人热量与蛋白质目标（update_profile）；早起测量空腹体重与睡眠质量。"

            tomorrow_draft = {
                "date": tomorrow_date,
                "status": "draft",
                "nutrition_targets": nutr_plan,
                "workout_plan": workout_plan,
                "training_plan": training_plan,
                "minimum_effective_plan": min_plan,
                "safety_alert": safety_alert,
                "action_item": action_item,
            }

            after_version = before_version + 1
            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            review_id = f"rev_{uuid.uuid4().hex}"
            review_body = {
                "summary": summary,
                "recording_status": recording_status,
                "today_totals": totals,
                "nutrition_analysis": nutrition_analysis,
                "workout_analysis": {
                    "status": fact_collection["workout_status"],
                    "session_count": len(fact_collection["workout_sessions"]),
                    "sessions": fact_collection["workout_sessions"],
                },
                "fact_collection": fact_collection,
                "tomorrow_draft_plan": tomorrow_draft,
                "user_notes": user_notes,
            }
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, status, causation_id, state_version, created_at
                ) VALUES (?, ?, 'daily_review', ?, ?, 'active', ?, ?, ?)""",
                (review_id, user_id, date, self.store.json(review_body), operation_id, after_version, now),
            )

            # Link previous draft plan to establish revision lineage
            prev_plan = conn.execute(
                """SELECT record_id FROM domain_record
                   WHERE user_id = ? AND kind = 'plan' AND day = ? AND status != 'deleted'
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, tomorrow_date),
            ).fetchone()
            parent_plan_id = prev_plan["record_id"] if prev_plan else None
            if parent_plan_id:
                conn.execute(
                    "UPDATE domain_record SET status = 'superseded' WHERE record_id = ?",
                    (parent_plan_id,),
                )

            plan_id = f"plan_{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, parent_id, status, causation_id, state_version, created_at
                ) VALUES (?, ?, 'plan', ?, ?, ?, 'draft', ?, ?, ?)""",
                (plan_id, user_id, tomorrow_date, self.store.json(tomorrow_draft), parent_plan_id, operation_id, after_version, now),
            )

            # Evaluate genuine maintenance due-work without spamming fake memory candidates
            now_dt = datetime.fromisoformat(now.replace("Z", "+00:00")) if isinstance(now, str) else datetime.now(UTC)
            due_info = self._calculate_maintenance_due(conn, user_id, now_dt, day=date)
            maint_rec = due_info["due"]
            maint_reason = due_info["reason"]
            maint_key = due_info["maintenance_key"]
            suggested_action = "cyber_health_maintain_memory" if maint_rec else None
            maintenance_data = {
                "recommended": maint_rec,
                "maintenance_recommended": maint_rec,
                "reason": maint_reason,
                "maintenance_reason": maint_reason,
                "reasons": due_info["reasons"],
                "suggested_action": suggested_action,
                "maintenance_key": maint_key,
                "work_generation": due_info["work_generation"],
                "pending_outbox_count": due_info["pending_outbox_count"],
                "expired_lease_count": due_info["expired_lease_count"],
                "ttl_meals_count": due_info["ttl_meals_count"],
                "ttl_prune_count": due_info["ttl_prune_count"],
                "due_work": due_info["due_work"],
                "retry_after_seconds": due_info["retry_after_seconds"],
            }

            data = {
                "review_id": review_id,
                "date": date,
                "summary": summary,
                "recording_status": recording_status,
                "nutrition_analysis": nutrition_analysis,
                "workout_analysis": review_body["workout_analysis"],
                "fact_collection": fact_collection,
                "tomorrow_draft_plan": tomorrow_draft,
                "action_item": tomorrow_draft["action_item"],
                "maintenance_recommended": maint_rec,
                "maintenance_reason": maint_reason,
                "suggested_action": suggested_action,
                "maintenance_key": maint_key,
                "maintenance": maintenance_data,
            }
            response = self._response(
                operation_id,
                "success",
                data,
                after_version,
            )
            response["maintenance_recommended"] = maint_rec
            response["maintenance_reason"] = maint_reason
            response["suggested_action"] = suggested_action
            response["maintenance_key"] = maint_key
            response["maintenance"] = maintenance_data
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="daily_review",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def plan_tomorrow(
        self,
        *,
        user_id: str,
        date: str,
        idempotency_key: str,
        commit: bool = False,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            PlanTomorrowInput(
                user_id=user_id,
                date=date,
                idempotency_key=idempotency_key,
                commit=commit,
                expected_state_version=expected_state_version,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {
            "action": "plan_tomorrow",
            "user_id": user_id,
            "date": date,
            "commit": commit,
            "expected_state_version": expected_state_version,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "plan_tomorrow", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]

            if expected_state_version is not None and expected_state_version != before_version:
                raise ConflictError(
                    f"Expected version {expected_state_version}, current version is {before_version}"
                )

            # Centralized safe plan generation
            workout_plan, min_plan, safety_alert, training_plan = self._determine_safe_workout_plan(
                conn, user_id, date, profile
            )

            goals = json.loads(profile["goals_json"])
            nutr_targets = self._resolve_nutrition_targets(goals)
            if nutr_targets:
                nutr_plan = {
                    "kcal_range": nutr_targets["kcal_range"],
                    "protein_target_g": nutr_targets["protein_target_g"],
                    "status": nutr_targets["status"],
                }
            else:
                nutr_plan = None

            warnings: list[str] = []
            if commit and nutr_plan is None:
                warnings.append(
                    "COMMITTED_WITHOUT_NUTRITION_TARGETS: Tomorrow's plan committed while user profile nutrition goals remain unconfigured. Prompting user to configure goals."
                )

            new_status = "committed" if commit else "draft"
            plan_data = {
                "date": date,
                "status": new_status,
                "nutrition_plan": nutr_plan,
                "workout_plan": workout_plan,
                "training_plan": training_plan,
                "minimum_effective_plan": min_plan,
                "safety_alert": safety_alert,
            }

            # Revision lineage linking
            prev_plan = conn.execute(
                """SELECT record_id FROM domain_record
                   WHERE user_id = ? AND kind = 'plan' AND day = ? AND status != 'deleted'
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, date),
            ).fetchone()
            parent_plan_id = prev_plan["record_id"] if prev_plan else None
            if parent_plan_id:
                conn.execute(
                    "UPDATE domain_record SET status = 'superseded' WHERE record_id = ?",
                    (parent_plan_id,),
                )

            after_version = before_version + 1
            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            plan_id = f"plan_{uuid.uuid4().hex}"
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, parent_id, status, causation_id, state_version, created_at
                ) VALUES (?, ?, 'plan', ?, ?, ?, ?, ?, ?, ?)""",
                (plan_id, user_id, date, self.store.json(plan_data), parent_plan_id, new_status, operation_id, after_version, now),
            )

            data = {
                "plan_id": plan_id,
                "date": date,
                "status": new_status,
                "plan": plan_data,
            }
            response = self._response(
                operation_id,
                "success",
                data,
                after_version,
                warnings=warnings,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="plan_tomorrow",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def schedule_daily_reminders(
        self,
        *,
        user_id: str,
        date: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Generate deterministic standard schedule events for a specific day."""
        try:
            ScheduleDailyRemindersInput(user_id=user_id, date=date, idempotency_key=idempotency_key)
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {"action": "schedule_daily_reminders", "user_id": user_id, "date": date}
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "schedule_daily_reminders", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            row = conn.execute("SELECT timezone, state_version FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = row["state_version"]
            tz_name = row["timezone"]

            try:
                user_tz = ZoneInfo(tz_name)
            except Exception:
                user_tz = ZoneInfo("Asia/Shanghai")

            standard_windows = [
                ("MORNING_PLAN", "07:30:00", "08:30:00", "晨间唤醒：记录体重与昨晚睡眠，锁定今日执行计划", "morning_plan_not_locked", f"sched_{user_id}_{date}_morning_plan"),
                ("MEAL_CHECK", "13:00:00", "14:00:00", "午餐核验：询问就餐与饥饿感，提示水分补充", "lunch_not_logged", f"sched_{user_id}_{date}_meal_check_lunch"),
                ("WORKOUT_REMINDER", "17:30:00", "18:30:00", "训练窗口临近：推送最低可完成版本或热身提示", "workout_pending", f"sched_{user_id}_{date}_workout_reminder"),
                ("MEAL_CHECK", "19:30:00", "20:30:00", "晚餐核验：询问就餐与饥饿感，提示摄入控制", "dinner_not_logged", f"sched_{user_id}_{date}_meal_check_dinner"),
                ("DAILY_REVIEW", "21:30:00", "22:30:00", "晚间对账：复盘全天摄入与运动，生成次日预案", "review_pending", f"sched_{user_id}_{date}_daily_review"),
            ]

            y, mo, d = map(int, date.split("-"))
            created_events: list[dict[str, Any]] = []

            for ev_type, start_time, end_time, hint, trigger_cond, ev_id in standard_windows:
                sh, sm, ss = map(int, start_time.split(":"))
                eh, em, es = map(int, end_time.split(":"))
                local_start = datetime(y, mo, d, sh, sm, ss, tzinfo=user_tz)
                local_end = datetime(y, mo, d, eh, em, es, tzinfo=user_tz)
                w_start = local_start.isoformat()
                w_end = local_end.isoformat()

                existing_event = conn.execute(
                    "SELECT revision, window_start, window_end, prompt_hint, status FROM schedule_event WHERE event_id = ?",
                    (ev_id,),
                ).fetchone()

                if existing_event:
                    rev = existing_event["revision"]
                    status = existing_event["status"]
                    if rev > 1 or status != "pending":
                        # Preserving modified schedule events (e.g. postponed or non-pending)
                        act_start = existing_event["window_start"]
                        act_end = existing_event["window_end"]
                    else:
                        act_start = w_start
                        act_end = w_end
                        if (
                            existing_event["window_start"] != w_start
                            or existing_event["window_end"] != w_end
                            or existing_event["prompt_hint"] != hint
                        ):
                            rev += 1
                            conn.execute(
                                """UPDATE schedule_event SET window_start = ?, window_end = ?, prompt_hint = ?, revision = ?, updated_at = ?
                                   WHERE event_id = ?""",
                                (w_start, w_end, hint, rev, now, ev_id),
                            )
                else:
                    rev = 1
                    status = "pending"
                    act_start = w_start
                    act_end = w_end
                    conn.execute(
                        """INSERT INTO schedule_event(
                            event_id, user_id, event_type, window_start, window_end, status,
                            revision, delivery_attempts, prompt_hint, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'pending', 1, 0, ?, ?, ?)""",
                        (ev_id, user_id, ev_type, w_start, w_end, hint, now, now),
                    )

                created_events.append({
                    "event_id": ev_id,
                    "event_type": ev_type,
                    "window_start": act_start,
                    "window_end": act_end,
                    "revision": rev,
                    "status": status,
                    "trigger_condition": trigger_cond,
                })

            after_version = before_version + 1
            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            data = {"date": date, "scheduled_events": created_events}
            response = self._response(
                operation_id,
                "success",
                data,
                after_version,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="schedule_daily_reminders",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def update_schedule_event(
        self,
        *,
        user_id: str,
        event_id: str,
        action: str = "acknowledged",
        idempotency_key: str,
        new_window_start: str | None = None,
        new_window_end: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        try:
            AcknowledgeScheduleInput(
                user_id=user_id,
                event_id=event_id,
                action=action,  # type: ignore[arg-type]
                idempotency_key=idempotency_key,
                new_window_start=new_window_start,
                new_window_end=new_window_end,
                note=note,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {
            "action": "update_schedule_event",
            "user_id": user_id,
            "event_id": event_id,
            "event_action": action,
            "new_window_start": new_window_start,
            "new_window_end": new_window_end,
            "note": note,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "update_schedule_event", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute("SELECT state_version FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]

            target = conn.execute(
                "SELECT event_type, window_start, window_end, status, revision, delivery_attempts FROM schedule_event WHERE event_id = ? AND user_id = ?",
                (event_id, user_id),
            ).fetchone()
            if not target:
                raise ValidationError(f"Schedule event '{event_id}' not found for user '{user_id}'")

            current_status = target["status"]
            revision = target["revision"]
            delivery_attempts = target["delivery_attempts"]
            compensation_action = None

            if action == "delivered":
                new_status = "delivered"
                delivery_attempts += 1
                conn.execute(
                    "UPDATE schedule_event SET status = ?, delivery_attempts = ?, updated_at = ? WHERE event_id = ?",
                    (new_status, delivery_attempts, now, event_id),
                )
            elif action == "acknowledged":
                new_status = "acknowledged"
                conn.execute(
                    "UPDATE schedule_event SET status = ?, updated_at = ? WHERE event_id = ?",
                    (new_status, now, event_id),
                )
            elif action == "skipped":
                new_status = "skipped"
                conn.execute(
                    "UPDATE schedule_event SET status = ?, updated_at = ? WHERE event_id = ?",
                    (new_status, now, event_id),
                )
            elif action == "cancelled":
                new_status = "cancelled"
                conn.execute(
                    "UPDATE schedule_event SET status = ?, updated_at = ? WHERE event_id = ?",
                    (new_status, now, event_id),
                )
            elif action == "postponed":
                new_status = "pending"
                revision += 1
                conn.execute(
                    """UPDATE schedule_event
                       SET status = 'pending', window_start = ?, window_end = ?, revision = ?, updated_at = ?
                       WHERE event_id = ?""",
                    (new_window_start, new_window_end, revision, now, event_id),
                )
            else:
                new_status = action
                conn.execute(
                    "UPDATE schedule_event SET status = ?, updated_at = ? WHERE event_id = ?",
                    (new_status, now, event_id),
                )

            if current_status == "overdue":
                compensation_action = {
                    "event_type": target["event_type"],
                    "original_window": [target["window_start"], target["window_end"]],
                    "status": "compensated",
                    "handling": f"Event {event_id} transitioned to {new_status}; overdue compensation fulfilled.",
                }

            after_version = before_version + 1
            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            data = {
                "event_id": event_id,
                "status": new_status,
                "revision": revision,
                "delivery_attempts": delivery_attempts,
                "compensation": compensation_action,
            }
            response = self._response(
                operation_id,
                "success",
                data,
                after_version,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="update_schedule_event",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def acknowledge_schedule_event(
        self,
        *,
        user_id: str,
        event_id: str,
        action: str = "acknowledged",
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.update_schedule_event(
            user_id=user_id,
            event_id=event_id,
            action=action,
            idempotency_key=idempotency_key,
        )

    def get_training_plan(
        self,
        *,
        user_id: str,
        date: str,
        equipment: list[str] | None = None,
        target_duration_min: int = 45,
        evidence_window_days: int | None = None,
    ) -> dict[str, Any]:
        """Evidence-based exercise prescription generator respecting safety rules and recovery state."""
        win_days = evidence_window_days if evidence_window_days is not None else self.recovery_evidence_window_days
        try:
            GetTrainingPlanInput(
                user_id=user_id,
                date=date,
                equipment=equipment or [],
                target_duration_min=target_duration_min,
                evidence_window_days=win_days,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        with self.store.connect() as conn:
            profile = conn.execute(
                "SELECT safety_mode, state_version FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            version = profile["state_version"] if profile else 0

            safety_eval = self._evaluate_user_safety_and_recovery(
                conn, user_id, target_date=date, evidence_window_days=win_days
            )
            safety_mode = safety_eval.safety_mode
            recovery_score = safety_eval.recovery_score if safety_eval.is_fresh else None

            prescription, _, _ = self._evaluate_training_prescription(
                conn, user_id, date, equipment=equipment, target_duration_min=target_duration_min, evidence_window_days=win_days
            )

            operation_id = f"op_read_{uuid.uuid4().hex[:12]}"
            data = {
                "user_id": user_id,
                "date": date,
                "safety_mode": safety_mode,
                "recovery_score": recovery_score,
                "plan": prescription,
            }
            return {
                "operation_id": operation_id,
                "status": "success",
                "data": data,
                "warnings": [],
                "error": None,
                "state_version": version,
                **data,
            }

    def confirm_training_progression(
        self,
        *,
        user_id: str,
        exercise_name: str,
        idempotency_key: str,
        confirmed_weight_kg: float | None = None,
        confirmed_reps: int | None = None,
        proposal_id: str | None = None,
        source_record_ids: list[str] | None = None,
        increment_kg: float | None = None,
        increment_reps: int | None = None,
        user_note: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        """Confirm a verified double progression proposal for an exercise, recording revision chain."""
        try:
            ConfirmProgressionInput(
                user_id=user_id,
                exercise_name=exercise_name,
                confirmed_weight_kg=confirmed_weight_kg,
                confirmed_reps=confirmed_reps,
                proposal_id=proposal_id,
                idempotency_key=idempotency_key,
                source_record_ids=source_record_ids or [],
                increment_kg=increment_kg,
                increment_reps=increment_reps,
                user_note=user_note,
                expected_state_version=expected_state_version,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {
            "action": "confirm_training_progression",
            "user_id": user_id,
            "exercise_name": exercise_name,
            "confirmed_weight_kg": confirmed_weight_kg,
            "confirmed_reps": confirmed_reps,
            "proposal_id": proposal_id,
            "idempotency_key": idempotency_key,
            "source_record_ids": source_record_ids or [],
            "increment_kg": increment_kg,
            "increment_reps": increment_reps,
            "user_note": user_note,
            "expected_state_version": expected_state_version,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "confirm_training_progression", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]

            if expected_state_version is not None and expected_state_version != before_version:
                raise ConflictError(
                    f"Expected version {expected_state_version}, current version is {before_version}"
                )

            # Resolve local calendar date in user's timezone
            tz_name = profile["timezone"] if profile and profile["timezone"] else "Asia/Shanghai"
            target_date = self._parse_day_in_timezone(now, tz_name)

            # 1. Canonical Safety & Recovery Evaluation
            # Blocks restricted mode, 7-day deload, acute fatigue / sleep deficit / recovery deficit, and contraindications
            safety_eval = self._evaluate_user_safety_and_recovery(conn, user_id, target_date=target_date)
            safety_eval.assert_progression_allowed(exercise_name=exercise_name)

            # 2. Check Evidence source records
            source_ids = source_record_ids or []
            if not source_ids:
                raise ValidationError("Training progression confirmation requires non-empty 'source_record_ids' citing qualifying workout evidence.")

            for s_id in source_ids:
                s_row = conn.execute(
                    "SELECT user_id, status, kind FROM domain_record WHERE record_id = ?",
                    (s_id,),
                ).fetchone()
                if not s_row:
                    raise ValidationError(f"Evidence source record '{s_id}' does not exist.")
                if s_row["user_id"] != user_id:
                    raise ValidationError(f"Evidence source record '{s_id}' belongs to another user (cross-user evidence rejected).")
                if s_row["status"] != "active":
                    raise ValidationError(f"Evidence source record '{s_id}' is not active (status='{s_row['status']}').")
                if s_row["kind"] not in ("workout", "workout_log"):
                    raise ValidationError(f"Evidence source record '{s_id}' is not a workout record.")

            # 3. Re-evaluate progression state machine in transaction
            target_norm = exercise_name.strip().lower()
            catalog_entry = None
            for k, v in EXERCISE_CATALOG.items():
                if k.strip().lower() == target_norm:
                    catalog_entry = v
                    break
            target_reps_max = catalog_entry.get("reps_max", 8) if catalog_entry else 8
            expected_proposal = self._evaluate_exercise_progression(conn, user_id, exercise_name, target_reps_max, date=target_date)
            if not expected_proposal:
                raise ValidationError(
                    f"No active qualifying progression proposal found for exercise '{exercise_name}'. "
                    "Confirmation requires 2 consecutive completed sessions meeting criteria."
                )

            if proposal_id and proposal_id != expected_proposal.get("proposal_id"):
                raise ValidationError(
                    f"Proposal ID mismatch: provided '{proposal_id}', active proposal is '{expected_proposal.get('proposal_id')}'."
                )

            expected_sources = set(expected_proposal.get("evidence_source_record_ids", []))
            if set(source_ids) != expected_sources:
                raise ValidationError(
                    f"Source record IDs do not match active proposal evidence. Expected {sorted(expected_sources)}, got {sorted(source_ids)}."
                )

            if expected_proposal.get("suggested_weight_kg") is not None:
                exp_wt = expected_proposal["suggested_weight_kg"]
                if confirmed_weight_kg is None or abs(confirmed_weight_kg - exp_wt) > 0.01:
                    raise ValidationError(
                        f"Confirmed weight {confirmed_weight_kg}kg does not match proposed weight {exp_wt}kg."
                    )
            elif expected_proposal.get("suggested_reps") is not None:
                exp_reps = expected_proposal["suggested_reps"]
                if confirmed_reps is None or confirmed_reps != exp_reps:
                    raise ValidationError(
                        f"Confirmed reps {confirmed_reps} do not match proposed reps {exp_reps}."
                    )

            # 7. Atomic update
            prev_row = conn.execute(
                """SELECT record_id FROM domain_record
                   WHERE user_id = ? AND kind = 'progression_state' AND status = 'active'
                     AND LOWER(json_extract(body_json, '$.exercise_name')) = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, target_norm),
            ).fetchone()

            parent_id = prev_row["record_id"] if prev_row else None
            if prev_row:
                conn.execute(
                    "UPDATE domain_record SET status = 'superseded' WHERE record_id = ?",
                    (prev_row["record_id"],),
                )

            after_version = before_version + 1
            record_id = f"prog_{uuid.uuid4().hex[:12]}"
            effective_increment_kg = increment_kg if increment_kg is not None else expected_proposal.get("suggested_increment_kg")
            effective_increment_reps = increment_reps if increment_reps is not None else (
                (expected_proposal.get("suggested_reps") - expected_proposal.get("current_reps"))
                if expected_proposal.get("suggested_reps") and expected_proposal.get("current_reps")
                else None
            )

            prog_body = {
                "exercise_name": exercise_name,
                "confirmed_weight_kg": confirmed_weight_kg,
                "confirmed_reps": confirmed_reps,
                "increment_kg": effective_increment_kg,
                "increment_reps": effective_increment_reps,
                "proposal_id": expected_proposal.get("proposal_id"),
                "proposal_signature": expected_proposal.get("proposal_signature"),
                "source_record_ids": source_ids,
                "verification_type": "proposal_confirmed",
                "user_note": user_note,
                "confirmed_at": now,
            }

            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, status, causation_id, parent_id, state_version, created_at
                ) VALUES (?, ?, 'progression_state', ?, ?, 'active', ?, ?, ?, ?)""",
                (record_id, user_id, now[:10], self.store.json(prog_body), operation_id, parent_id, after_version, now),
            )

            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            data = {
                "record_id": record_id,
                "exercise_name": exercise_name,
                "confirmed_weight_kg": confirmed_weight_kg,
                "confirmed_reps": confirmed_reps,
                "increment_kg": effective_increment_kg,
                "increment_reps": effective_increment_reps,
                "proposal_id": expected_proposal.get("proposal_id"),
                "source_record_ids": source_ids,
                "parent_id": parent_id,
                "status": "confirmed",
            }
            response = self._response(
                operation_id,
                "success",
                data,
                after_version,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="confirm_training_progression",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def record_exercise_baseline(
        self,
        *,
        user_id: str,
        exercise_name: str,
        idempotency_key: str,
        weight_kg: float | None = None,
        reps: int | None = None,
        user_note: str | None = None,
        expected_state_version: int | None = None,
    ) -> dict[str, Any]:
        """Manually record a baseline starting load/reps for an exercise without forging progression evidence."""
        if weight_kg is None and reps is None:
            raise ValidationError("At least one of weight_kg or reps must be provided.")
        payload = {
            "action": "record_exercise_baseline",
            "user_id": user_id,
            "exercise_name": exercise_name,
            "weight_kg": weight_kg,
            "reps": reps,
            "idempotency_key": idempotency_key,
            "user_note": user_note,
            "expected_state_version": expected_state_version,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"
        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "record_exercise_baseline", payload)
            if existing:
                return existing
            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]
            if expected_state_version is not None and expected_state_version != before_version:
                raise ConflictError(f"Expected version {expected_state_version}, current version is {before_version}")

            target_norm = exercise_name.strip().lower()
            prev_row = conn.execute(
                """SELECT record_id FROM domain_record
                   WHERE user_id = ? AND kind = 'progression_state' AND status = 'active'
                     AND LOWER(json_extract(body_json, '$.exercise_name')) = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id, target_norm),
            ).fetchone()
            parent_id = prev_row["record_id"] if prev_row else None
            if prev_row:
                conn.execute("UPDATE domain_record SET status = 'superseded' WHERE record_id = ?", (prev_row["record_id"],))

            after_version = before_version + 1
            record_id = f"prog_{uuid.uuid4().hex[:12]}"
            prog_body = {
                "exercise_name": exercise_name,
                "confirmed_weight_kg": weight_kg,
                "confirmed_reps": reps,
                "increment_kg": None,
                "increment_reps": None,
                "proposal_id": None,
                "proposal_signature": None,
                "source_record_ids": [],
                "verification_type": "manual_baseline",
                "user_note": user_note,
                "confirmed_at": now,
            }
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, status, causation_id, parent_id, state_version, created_at
                ) VALUES (?, ?, 'progression_state', ?, ?, 'active', ?, ?, ?, ?)""",
                (record_id, user_id, now[:10], self.store.json(prog_body), operation_id, parent_id, after_version, now),
            )
            conn.execute("UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?", (after_version, now, user_id))
            data = {
                "record_id": record_id,
                "exercise_name": exercise_name,
                "weight_kg": weight_kg,
                "confirmed_weight_kg": weight_kg,
                "reps": reps,
                "confirmed_reps": reps,
                "verification_type": "manual_baseline",
                "parent_id": parent_id,
                "status": "recorded",
            }
            response = self._response(operation_id, "success", data, after_version)
            self._record_operation(
                conn, operation_id=operation_id, user_id=user_id, idempotency_key=idempotency_key,
                payload=payload, action="record_exercise_baseline", before_version=before_version,
                response=response, now=now,
            )
            return response

    def substitute_exercise(
        self,
        *,
        user_id: str,
        original_exercise: str,
        equipment: list[str] | None = None,
        discomfort_joint: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Support on-the-fly exercise substitution preserving movement pattern and volume."""
        try:
            SubstituteExerciseInput(
                user_id=user_id,
                original_exercise=original_exercise,
                equipment=equipment or [],
                discomfort_joint=discomfort_joint,
                reason=reason,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        with self.store.connect() as conn:
            profile = conn.execute(
                "SELECT constraints_json, goals_json, state_version FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            version = profile["state_version"] if profile else 0

            target_norm = original_exercise.strip().lower()
            orig_def = None
            for name, defn in EXERCISE_CATALOG.items():
                if name.strip().lower() == target_norm or target_norm in name.lower():
                    orig_def = defn
                    break

            operation_id = f"op_sub_{uuid.uuid4().hex[:12]}"
            if not orig_def:
                return {
                    "operation_id": operation_id,
                    "status": "failed",
                    "data": {
                        "original_exercise": original_exercise,
                        "substitutes": [],
                        "reason": f"未在动作库中识别动作 '{original_exercise}'",
                    },
                    "warnings": [f"Unrecognized exercise: {original_exercise}"],
                    "error": None,
                    "state_version": version,
                }

            pattern = orig_def["movement_pattern"]

            raw_constraints = json.loads(profile["constraints_json"]) if profile and profile["constraints_json"] else {}
            constraint_tokens: set[str] = set()
            if isinstance(raw_constraints, dict):
                for k, v in raw_constraints.items():
                    constraint_tokens.add(str(k).lower())
                    constraint_tokens.add(str(v).lower())
            elif isinstance(raw_constraints, list):
                for item in raw_constraints:
                    constraint_tokens.add(str(item).lower())
            elif isinstance(raw_constraints, str):
                constraint_tokens.add(raw_constraints.lower())
            if discomfort_joint:
                constraint_tokens.add(discomfort_joint.lower())
            c_text = " ".join(constraint_tokens)

            active_constraints: set[str] = set()
            if any(w in c_text for w in ("knee", "膝", "膝盖", "patella", "meniscus", "acl", "deep_squat", "蹲")):
                active_constraints.add("knee")
            if any(w in c_text for w in ("shoulder", "肩", "impingement", "rotator_cuff", "bench_press", "overhead", "推胸")):
                active_constraints.add("shoulder")
            if any(w in c_text for w in ("lumbar", "腰", "disc", "herniation", "lower_back", "spine", "硬拉")):
                active_constraints.add("lumbar")

            avail_eq = [e.lower() for e in (equipment or [])]
            if not avail_eq:
                avail_eq = list(orig_def["equipment"]) + ["bodyweight"]

            candidates: list[dict[str, Any]] = []
            for name, defn in EXERCISE_CATALOG.items():
                if name == orig_def["name"]:
                    continue
                if defn["movement_pattern"] != pattern:
                    continue
                if not defn["equipment"].intersection(set(avail_eq)) and "bodyweight" not in defn["equipment"]:
                    continue
                if not defn["contraindications"].isdisjoint(active_constraints):
                    continue

                candidates.append({
                    "name": defn["name"],
                    "movement_pattern": defn["movement_pattern"],
                    "equipment": list(defn["equipment"])[0],
                    "default_sets": defn["default_sets"],
                    "reps_min": defn["reps_min"],
                    "reps_max": defn["reps_max"],
                    "rest_seconds": defn["rest_seconds"],
                    "rationale": f"同动模式（{pattern}）安全替换，避开受限关节，保持训练刺激与周总容量。",
                })

            status = "success" if candidates else "no_substitute_available"
            data = {
                "original_exercise": orig_def["name"],
                "movement_pattern": pattern,
                "discomfort_joint": discomfort_joint,
                "active_constraints": list(active_constraints),
                "substitutes": candidates,
                "guidance": "已成功筛选同动模式替代动作。" if candidates else "当前可用器械下无满足所有关节限制的同动模式替代动作，建议暂停该动作并咨询专业教练/医师。",
            }
            return {
                "operation_id": operation_id,
                "status": status,
                "data": data,
                "warnings": [] if candidates else ["No safe substitute found matching constraints."],
                "error": None,
                "state_version": version,
                **data,
            }

    def complete_workout(
        self,
        *,
        user_id: str,
        date: str,
        idempotency_key: str,
        completed_exercises: list[dict[str, Any]] | None = None,
        session_rpe: float | None = None,
        discomfort_notes: str | None = None,
        completion_rate: float | None = None,
    ) -> dict[str, Any]:
        """Workout completion check-in with red-flag detection and state transition."""
        try:
            CompleteWorkoutInput(
                user_id=user_id,
                date=date,
                idempotency_key=idempotency_key,
                completed_exercises=completed_exercises or [],
                session_rpe=session_rpe,
                discomfort_notes=discomfort_notes,
                completion_rate=completion_rate,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {
            "action": "complete_workout",
            "user_id": user_id,
            "date": date,
            "completed_exercises": completed_exercises or [],
            "session_rpe": session_rpe,
            "discomfort_notes": discomfort_notes,
            "completion_rate": completion_rate,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "complete_workout", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute(
                "SELECT safety_flags_json, safety_mode, state_version FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            before_version = profile["state_version"]
            safety_flags = json.loads(profile["safety_flags_json"])
            safety_mode = profile["safety_mode"]

            # Red-flag symptom check
            combined_text = (discomfort_notes or "") + " " + " ".join(str(e) for e in (completed_exercises or []))
            detected_flags = self._scan_for_red_flags(combined_text)
            warnings: list[str] = []

            if detected_flags:
                safety_mode = "restricted"
                for fl in detected_flags:
                    if fl not in safety_flags:
                        safety_flags.append(fl)
                conn.execute(
                    "UPDATE user_profile SET safety_mode = 'restricted', safety_flags_json = ? WHERE user_id = ?",
                    (self.store.json(safety_flags), user_id),
                )
                warnings.append(
                    f"SAFETY_RESTRICTED: Acute red-flag symptom detected ({', '.join(detected_flags)}). System locked into Restricted Mode."
                )

            record_id = f"workout_{uuid.uuid4().hex[:12]}"
            after_version = before_version + 1
            body_json = self.store.json({
                "date": date,
                "session_rpe": session_rpe,
                "completion_rate": completion_rate,
                "discomfort_notes": discomfort_notes,
                "completed_exercises": completed_exercises or [],
                "detected_safety_flags": detected_flags,
            })
            conn.execute(
                """INSERT INTO domain_record(
                    record_id, user_id, kind, day, body_json, status, causation_id, state_version, created_at
                ) VALUES (?, ?, 'workout', ?, ?, 'active', ?, ?, ?)""",
                (record_id, user_id, date, body_json, operation_id, after_version, now),
            )

            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            data = {
                "record_id": record_id,
                "date": date,
                "completion_rate": completion_rate,
                "session_rpe": session_rpe,
                "safety_mode": safety_mode,
                "detected_flags": detected_flags,
            }
            response = self._response(
                operation_id,
                "success",
                data,
                after_version,
                warnings=warnings,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="complete_workout",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def query_knowledge(
        self,
        *,
        query: str,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Verified primary evidence lookup with explicit source citation and category filtering."""
        try:
            QueryKnowledgeInput(query=query, category=category)
        except Exception as err:
            raise ValidationError(str(err)) from err

        q_lower = query.lower()
        verified_items = [
            {
                "topic": "protein_intake_for_exercising_individuals",
                "title": "ISSN Position Stand: Protein and Exercise (International Society of Sports Nutrition)",
                "source_type": "peer_reviewed_literature",
                "evidence_statement": "An overall daily protein intake in the range of 1.4-2.0 g protein/kg body weight/day for most exercising individuals is sufficient for building and maintaining muscle mass.",
                "doi_or_citation": "J Int Soc Sports Nutr. 2017;14:20. doi:10.1186/s12970-017-0177-8",
                "url": "https://doi.org/10.1186/s12970-017-0177-8",
                "category": "nutrition",
            },
            {
                "topic": "acute_cardiovascular_red_flags",
                "title": "AHA/ACC Scientific Statement: Exercise Standards for Testing and Training",
                "source_type": "clinical_guideline",
                "evidence_statement": "Exertional chest pain, unexplained syncope or pre-syncope, and disproportionate dyspnea warrant immediate exercise cessation and urgent clinical evaluation.",
                "doi_or_citation": "Circulation. 2013;128(8):873-934. doi:10.1161/CIR.0b013e31829b5b44",
                "url": "https://doi.org/10.1161/CIR.0b013e31829b5b44",
                "category": "safety",
            },
        ]

        matched = []
        for it in verified_items:
            if category and it["category"] != category:
                continue
            search_corpus = f"{it['topic']} {it['title'].lower()} {it['evidence_statement'].lower()}"
            if any(term in search_corpus for term in q_lower.split() if len(term) > 2):
                matched.append(it)

        operation_id = f"op_read_{uuid.uuid4().hex[:12]}"
        status = "success" if matched else "unavailable"
        data = {
            "query": query,
            "category": category,
            "status": status,
            "clinical_review_status": "evidence_rules_algorithmic_pending_licensed_physician_review",
            "external_dependency": "requires_credentialed_sports_dietitian_or_physician_for_individual_prescription",
            "evidence_items": matched,
            "note": "Verified primary literature only; unverified citations are withheld." if matched else "No verified primary literature matches query and category.",
        }
        return {
            "operation_id": operation_id,
            "status": status,
            "data": data,
            "warnings": ["NON_DIAGNOSTIC: Algorithmic health guidelines are informational only and do not constitute medical diagnosis."],
            "error": None,
            "state_version": 0,
            **data,
        }

    def export_data(self, *, user_id: str) -> dict[str, Any]:
        """Export all user health facts from SQLite into a portable schema."""
        try:
            ExportDataInput(user_id=user_id)
        except Exception as err:
            raise ValidationError(str(err)) from err

        with self.store.connect() as conn:
            profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            meals = [dict(r) for r in conn.execute("SELECT * FROM meal_log WHERE user_id = ?", (user_id,)).fetchall()]
            domain_records = [dict(r) for r in conn.execute("SELECT * FROM domain_record WHERE user_id = ?", (user_id,)).fetchall()]
            schedules = [dict(r) for r in conn.execute("SELECT * FROM schedule_event WHERE user_id = ?", (user_id,)).fetchall()]
            ops = [dict(r) for r in conn.execute("SELECT * FROM operation_log WHERE user_id = ? ORDER BY created_at ASC", (user_id,)).fetchall()]

        operation_id = f"op_read_{uuid.uuid4().hex[:12]}"
        facts = {
            "profile": dict(profile) if profile else None,
            "meal_count": len(meals),
            "meals": meals,
            "domain_records_count": len(domain_records),
            "domain_records": domain_records,
            "schedule_events": schedules,
            "operations_count": len(ops),
            "operations": ops,
        }
        data = {
            "schema_version": "0.1.0",
            "exported_at": self._now(),
            "user_id": user_id,
            "facts": facts,
        }
        return {
            "operation_id": operation_id,
            "status": "success",
            "data": data,
            "warnings": [],
            "error": None,
            "state_version": profile["state_version"] if profile else 0,
            **data,
        }

    def import_data(
        self,
        *,
        user_id: str,
        data: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Idempotently import user health facts into SQLite store."""
        try:
            ImportDataInput(user_id=user_id, data=data, idempotency_key=idempotency_key)
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {"action": "import_data", "user_id": user_id, "data": data}
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        SUPPORTED_SCHEMA_VERSIONS = {"0.1.0", "0.2.0", "0.2.1"}

        with self.store.transaction() as conn:
            # 1. Idempotency check FIRST to ensure key replay returns exact cached response
            # or IdempotencyMismatchError if payload differs
            existing = self._check_idempotency(conn, user_id, idempotency_key, "import_data", payload)
            if existing:
                return existing

            # 2. Schema and top-level structure validation
            schema_version = data.get("schema_version")
            if not schema_version or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
                raise ValidationError(
                    f"Unsupported schema version: '{schema_version}'. Supported versions: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
                )

            facts = data.get("facts")
            if facts is None or not isinstance(facts, dict):
                raise ValidationError("Import data must contain a valid 'facts' dictionary")

            if data.get("user_id") and data["user_id"] != user_id:
                raise ValidationError(f"Import data user_id '{data['user_id']}' does not match target user '{user_id}'")

            checksum = data.get("checksum")
            if checksum is not None:
                if not isinstance(checksum, str):
                    raise ValidationError("Checksum must be a string")
                facts_canonical = json.dumps(facts, sort_keys=True, separators=(",", ":"))
                computed_hash = hashlib.sha256(facts_canonical.encode("utf-8")).hexdigest()
                if computed_hash != checksum:
                    raise ValidationError("Checksum verification failed: facts integrity check failed")

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]
            current_sm = profile["safety_mode"]
            current_flags = json.loads(profile["safety_flags_json"] or "[]")

            # 3. Restore user_profile if present in facts
            imported_profile = facts.get("profile")
            target_version = before_version
            if imported_profile is not None:
                if not isinstance(imported_profile, dict):
                    raise ValidationError("facts.profile must be a dictionary")

                if imported_profile.get("user_id") and imported_profile["user_id"] != user_id:
                    raise ValidationError(
                        f"Profile user_id '{imported_profile['user_id']}' does not match target user '{user_id}'"
                    )

                # Timezone validation
                tz = imported_profile.get("timezone", profile["timezone"])
                if not isinstance(tz, str) or not tz.strip():
                    raise ValidationError("timezone must be a non-empty string")
                try:
                    ZoneInfo(tz)
                except Exception as err:
                    raise ValidationError(f"Invalid timezone '{tz}': {err}")

                # Safety mode validation
                VALID_SAFETY_MODES = {"normal", "restricted"}
                sm = imported_profile.get("safety_mode", "normal")
                if sm not in VALID_SAFETY_MODES:
                    raise ValidationError(f"Invalid safety_mode '{sm}'. Must be one of {sorted(VALID_SAFETY_MODES)}")

                # Goals validation
                gj = imported_profile.get("goals_json")
                if gj is not None:
                    if not isinstance(gj, str):
                        raise ValidationError("goals_json must be a JSON string")
                    try:
                        parsed_goals = json.loads(gj)
                        if not isinstance(parsed_goals, dict):
                            raise ValidationError("goals_json must decode to a dictionary")
                    except Exception as err:
                        raise ValidationError(f"Malformed goals_json: {err}")
                elif "goals" in imported_profile:
                    if not isinstance(imported_profile["goals"], dict):
                        raise ValidationError("goals must be a dictionary")
                    gj = json.dumps(imported_profile["goals"], ensure_ascii=False)
                else:
                    gj = "{}"

                # Constraints validation
                cj = imported_profile.get("constraints_json")
                if cj is not None:
                    if not isinstance(cj, str):
                        raise ValidationError("constraints_json must be a JSON string")
                    try:
                        parsed_constraints = json.loads(cj)
                        if not isinstance(parsed_constraints, dict):
                            raise ValidationError("constraints_json must decode to a dictionary")
                    except Exception as err:
                        raise ValidationError(f"Malformed constraints_json: {err}")
                elif "constraints" in imported_profile:
                    if not isinstance(imported_profile["constraints"], dict):
                        raise ValidationError("constraints must be a dictionary")
                    cj = json.dumps(imported_profile["constraints"], ensure_ascii=False)
                else:
                    cj = "{}"

                # Safety flags validation
                sfj = imported_profile.get("safety_flags_json")
                parsed_flags = []
                if sfj is not None:
                    if not isinstance(sfj, str):
                        raise ValidationError("safety_flags_json must be a JSON string")
                    try:
                        parsed_flags = json.loads(sfj)
                        if not isinstance(parsed_flags, list) or not all(isinstance(x, str) for x in parsed_flags):
                            raise ValidationError("safety_flags_json must decode to a list of strings")
                    except Exception as err:
                        raise ValidationError(f"Malformed safety_flags_json: {err}")
                elif "safety_flags" in imported_profile:
                    if not isinstance(imported_profile["safety_flags"], list) or not all(
                        isinstance(x, str) for x in imported_profile["safety_flags"]
                    ):
                        raise ValidationError("safety_flags must be a list of strings")
                    parsed_flags = imported_profile["safety_flags"]
                    sfj = json.dumps(parsed_flags, ensure_ascii=False)
                else:
                    sfj = "[]"

                if parsed_flags:
                    sm = "restricted"

                # Deload validation
                du = imported_profile.get("deload_until")
                if du is not None:
                    if not isinstance(du, str) or not du.strip():
                        du = None
                    else:
                        try:
                            _check_iso_instant(du)
                        except Exception as err:
                            raise ValidationError(f"Invalid deload_until format: {err}")

                # State version validation
                iv = imported_profile.get("state_version", before_version)
                if not isinstance(iv, int) or iv < 0:
                    raise ValidationError("state_version must be a non-negative integer")

                # SAFETY INTEGRITY RULE:
                # An active safety restriction (restricted mode or active red flags) CANNOT be cleared
                # by importing an older or normal backup without explicit medical clearance.
                if current_sm == "restricted" or current_flags:
                    if sm != "restricted" or not parsed_flags:
                        raise ConflictError(
                            f"Cannot clear active safety restriction via data import without explicit medical clearance. "
                            f"Current mode is '{current_sm}' with active flags: {current_flags}"
                        )
                    merged_flags = list(dict.fromkeys(current_flags + parsed_flags))
                    sfj = json.dumps(merged_flags, ensure_ascii=False)
                    sm = "restricted"

                # Protect deload_until: an active deload period cannot be shortened or cleared by an older backup
                curr_deload = profile["deload_until"]
                if curr_deload:
                    if not du or du < curr_deload:
                        du = curr_deload

                target_version = max(before_version, iv)
                conn.execute(
                    """UPDATE user_profile SET
                        timezone = ?, goals_json = ?, constraints_json = ?,
                        safety_flags_json = ?, safety_mode = ?, deload_until = ?,
                        state_version = ?, updated_at = ?
                       WHERE user_id = ?""",
                    (tz, gj, cj, sfj, sm, du, target_version, now, user_id),
                )

            imported_meals = 0
            imported_domain = 0
            imported_schedules = 0

            # 4. Import meals - validate fields and check for conflicting data on duplicate ID
            for m in facts.get("meals", []):
                if not isinstance(m, dict):
                    raise ValidationError("Meal entry must be a dictionary")
                mid = m.get("meal_id")
                if not mid or not isinstance(mid, str):
                    raise ValidationError("Meal records in facts must have a non-empty string meal_id")
                if m.get("user_id") and m["user_id"] != user_id:
                    raise ValidationError(f"Meal record '{mid}' user_id does not match import user '{user_id}'")

                occurred_at = m.get("occurred_at")
                if not occurred_at or not isinstance(occurred_at, str):
                    raise ValidationError(f"Meal record '{mid}' missing occurred_at timestamp")
                try:
                    _check_iso_instant(occurred_at)
                except Exception as err:
                    raise ValidationError(f"Meal record '{mid}' invalid occurred_at: {err}")

                meal_type = m.get("meal_type")
                if meal_type not in {"breakfast", "lunch", "dinner", "snack"}:
                    raise ValidationError(f"Invalid meal_type '{meal_type}' for meal '{mid}'")

                k_low = m.get("kcal_low", 0)
                k_high = m.get("kcal_high", 0)
                p_low = m.get("protein_low", 0)
                p_high = m.get("protein_high", 0)
                if not isinstance(k_low, (int, float)) or not isinstance(k_high, (int, float)) or k_low < 0 or k_high < k_low:
                    raise ValidationError(f"Invalid calorie range [{k_low}, {k_high}] for meal '{mid}'")
                if not isinstance(p_low, (int, float)) or not isinstance(p_high, (int, float)) or p_low < 0 or p_high < p_low:
                    raise ValidationError(f"Invalid protein range [{p_low}, {p_high}] for meal '{mid}'")

                m_foods = m.get("foods_json")
                if m_foods is None and "foods" in m:
                    if not isinstance(m["foods"], list):
                        raise ValidationError(f"foods in meal '{mid}' must be a list")
                    m_foods = json.dumps(m["foods"], ensure_ascii=False)
                elif m_foods is None:
                    m_foods = "[]"
                try:
                    parsed_foods = json.loads(m_foods)
                    if not isinstance(parsed_foods, list):
                        raise ValidationError(f"foods_json in meal '{mid}' must decode to a list")
                except Exception as err:
                    raise ValidationError(f"Malformed foods_json in meal '{mid}': {err}")

                m_status = m.get("status", "active")
                if m_status not in {"active", "deleted", "superseded"}:
                    raise ValidationError(f"Invalid status '{m_status}' for meal '{mid}'")

                ex = conn.execute("SELECT * FROM meal_log WHERE meal_id = ?", (mid,)).fetchone()
                if ex:
                    foods_match = True
                    try:
                        foods_match = json.loads(ex["foods_json"]) == parsed_foods
                    except Exception:
                        foods_match = (ex["foods_json"] == m_foods)

                    # All persisted fields checked including causation_id and state_version
                    if (
                        ex["user_id"] != user_id
                        or ex["occurred_at"] != occurred_at
                        or ex["meal_type"] != meal_type
                        or ex["kcal_low"] != k_low
                        or ex["kcal_high"] != k_high
                        or ex["protein_low"] != p_low
                        or ex["protein_high"] != p_high
                        or ex["status"] != m_status
                        or ex["parent_meal_id"] != m.get("parent_meal_id")
                        or (m.get("causation_id") is not None and ex["causation_id"] != m["causation_id"])
                        or (m.get("state_version") is not None and ex["state_version"] != m["state_version"])
                        or not foods_match
                    ):
                        raise ConflictError(f"Conflicting meal record for ID '{mid}'")
                    continue

                conn.execute(
                    """INSERT INTO meal_log(
                        meal_id, user_id, occurred_at, meal_type, foods_json,
                        kcal_low, kcal_high, protein_low, protein_high, status,
                        parent_meal_id, causation_id, state_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        mid,
                        user_id,
                        occurred_at,
                        meal_type,
                        m_foods,
                        k_low,
                        k_high,
                        p_low,
                        p_high,
                        m_status,
                        m.get("parent_meal_id"),
                        m.get("causation_id", operation_id),
                        m.get("state_version", 1),
                        m.get("created_at", now),
                    ),
                )
                imported_meals += 1

            # 5. Import domain records - validate fields and check for conflicting data on duplicate ID
            for d in facts.get("domain_records", []):
                if not isinstance(d, dict):
                    raise ValidationError("Domain record entry must be a dictionary")
                rid = d.get("record_id")
                if not rid or not isinstance(rid, str):
                    raise ValidationError("Domain records in facts must have a non-empty string record_id")
                if d.get("user_id") and d["user_id"] != user_id:
                    raise ValidationError(f"Domain record '{rid}' user_id does not match import user '{user_id}'")

                kind = d.get("kind")
                day = d.get("day")
                if not kind or not day or not isinstance(kind, str) or not isinstance(day, str):
                    raise ValidationError(f"Domain record '{rid}' must have non-empty string 'kind' and 'day'")
                try:
                    _check_real_date(day)
                except Exception as err:
                    raise ValidationError(f"Domain record '{rid}' invalid day: {err}")

                d_status = d.get("status", "active")
                if d_status not in {"active", "superseded", "deleted"}:
                    raise ValidationError(f"Invalid status '{d_status}' for domain record '{rid}'")

                d_body = d.get("body_json")
                if d_body is None and "body" in d:
                    if not isinstance(d["body"], dict):
                        raise ValidationError(f"body in domain record '{rid}' must be a dictionary")
                    d_body = json.dumps(d["body"], ensure_ascii=False)
                elif d_body is None:
                    d_body = "{}"
                try:
                    parsed_body = json.loads(d_body)
                    if not isinstance(parsed_body, dict):
                        raise ValidationError(f"body_json in domain record '{rid}' must decode to an object")
                except Exception as err:
                    raise ValidationError(f"Malformed body_json in domain record '{rid}': {err}")

                ex = conn.execute("SELECT * FROM domain_record WHERE record_id = ?", (rid,)).fetchone()
                if ex:
                    body_match = True
                    try:
                        body_match = json.loads(ex["body_json"]) == parsed_body
                    except Exception:
                        body_match = (ex["body_json"] == d_body)

                    if (
                        ex["user_id"] != user_id
                        or ex["kind"] != kind
                        or ex["day"] != day
                        or ex["status"] != d_status
                        or ex["parent_id"] != d.get("parent_id")
                        or (d.get("causation_id") is not None and ex["causation_id"] != d["causation_id"])
                        or (d.get("state_version") is not None and ex["state_version"] != d["state_version"])
                        or not body_match
                    ):
                        raise ConflictError(f"Conflicting domain record for ID '{rid}'")
                    continue

                conn.execute(
                    """INSERT INTO domain_record(
                        record_id, user_id, kind, day, body_json, parent_id,
                        status, causation_id, state_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        rid,
                        user_id,
                        kind,
                        day,
                        d_body,
                        d.get("parent_id"),
                        d_status,
                        d.get("causation_id", operation_id),
                        d.get("state_version", 1),
                        d.get("created_at", now),
                    ),
                )
                imported_domain += 1

            # 6. Import schedule events - validate fields and check for conflicting data on duplicate ID
            for s in facts.get("schedule_events", []):
                if not isinstance(s, dict):
                    raise ValidationError("Schedule event entry must be a dictionary")
                sid = s.get("event_id")
                if not sid or not isinstance(sid, str):
                    raise ValidationError("Schedule events in facts must have a non-empty string event_id")
                if s.get("user_id") and s["user_id"] != user_id:
                    raise ValidationError(f"Schedule event '{sid}' user_id does not match import user '{user_id}'")

                ev_type = s.get("event_type")
                w_start = s.get("window_start")
                w_end = s.get("window_end")
                if not ev_type or not w_start or not w_end:
                    raise ValidationError(f"Schedule event '{sid}' must have non-empty event_type, window_start, and window_end")
                try:
                    _check_iso_instant(w_start)
                    _check_iso_instant(w_end)
                except Exception as err:
                    raise ValidationError(f"Schedule event '{sid}' invalid window instant: {err}")

                s_status = s.get("status", "pending")
                if s_status not in {"pending", "delivered", "acknowledged", "skipped", "overdue", "cancelled"}:
                    raise ValidationError(f"Invalid status '{s_status}' for schedule event '{sid}'")

                s_rev = s.get("revision", 1)
                if not isinstance(s_rev, int) or s_rev < 1:
                    raise ValidationError(f"revision for schedule event '{sid}' must be a positive integer")

                ex = conn.execute("SELECT * FROM schedule_event WHERE event_id = ?", (sid,)).fetchone()
                if ex:
                    if (
                        ex["user_id"] != user_id
                        or ex["event_type"] != ev_type
                        or ex["window_start"] != w_start
                        or ex["window_end"] != w_end
                        or ex["status"] != s_status
                        or ex["revision"] != s_rev
                        or (s.get("prompt_hint") is not None and ex["prompt_hint"] != s.get("prompt_hint"))
                    ):
                        raise ConflictError(f"Conflicting schedule event for ID '{sid}'")
                    continue

                conn.execute(
                    """INSERT INTO schedule_event(
                        event_id, user_id, event_type, window_start, window_end, status,
                        revision, delivery_attempts, prompt_hint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sid,
                        user_id,
                        ev_type,
                        w_start,
                        w_end,
                        s_status,
                        s_rev,
                        s.get("delivery_attempts", 0),
                        s.get("prompt_hint", ""),
                        s.get("created_at", now),
                        s.get("updated_at", now),
                    ),
                )
                imported_schedules += 1

            # 7. Import operations if present - validate and check for conflicting data on duplicate ID
            for op in facts.get("operations", []):
                if not isinstance(op, dict):
                    raise ValidationError("Operation entry must be a dictionary")
                opid = op.get("operation_id")
                if not opid or not isinstance(opid, str):
                    raise ValidationError("Operation records in facts must have a non-empty string operation_id")
                if op.get("user_id") and op["user_id"] != user_id:
                    raise ValidationError(f"Operation record '{opid}' user_id does not match import user '{user_id}'")

                op_action = op.get("action", "unknown")
                op_resp = op.get("response_json", "{}")
                if op_resp:
                    try:
                        json.loads(op_resp)
                    except Exception as err:
                        raise ValidationError(f"Malformed response_json in operation '{opid}': {err}")

                ex = conn.execute("SELECT * FROM operation_log WHERE operation_id = ?", (opid,)).fetchone()
                if ex:
                    if (
                        ex["user_id"] != user_id
                        or ex["action"] != op_action
                        or (op.get("idempotency_key") is not None and ex["idempotency_key"] != op.get("idempotency_key"))
                        or (op.get("request_hash") is not None and ex["request_hash"] != op.get("request_hash"))
                        or (op.get("result_status") is not None and ex["result_status"] != op.get("result_status"))
                        or (op.get("before_version") is not None and ex["before_version"] != op.get("before_version"))
                        or (op.get("after_version") is not None and ex["after_version"] != op.get("after_version"))
                    ):
                        raise ConflictError(f"Conflicting operation log for ID '{opid}'")
                    continue

                conn.execute(
                    """INSERT OR IGNORE INTO operation_log(
                        operation_id, user_id, idempotency_key, request_hash, action,
                        result_status, before_version, after_version, response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        opid,
                        user_id,
                        op.get("idempotency_key", opid),
                        op.get("request_hash", ""),
                        op_action,
                        op.get("result_status", "success"),
                        op.get("before_version", 0),
                        op.get("after_version", 0),
                        op_resp,
                        op.get("created_at", now),
                    ),
                )

            after_version = target_version + 1
            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            res_data = {
                "user_id": user_id,
                "imported_profile": imported_profile is not None,
                "imported_meals": imported_meals,
                "imported_domain_records": imported_domain,
                "imported_schedules": imported_schedules,
            }
            response = self._response(
                operation_id,
                "success",
                res_data,
                after_version,
            )
            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="import_data",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def propose_memory_candidate(
        self,
        *,
        user_id: str,
        method: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Propose a memory candidate with durable intent committed before bounded external IO."""
        try:
            ProposeMemoryInput(
                user_id=user_id,
                method=method,
                payload=payload,
                idempotency_key=idempotency_key,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        op_payload = {
            "action": "propose_memory_candidate",
            "user_id": user_id,
            "method": method,
            "payload": payload,
        }
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        operation_id = f"op_{uuid.uuid4().hex}"
        intent_id = self._make_intent_id(user_id, idempotency_key)
        request_hash = self._request_hash(op_payload)

        call_payload = dict(payload)
        call_payload["user_id"] = user_id
        call_payload["intent_id"] = intent_id
        call_payload["idempotency_key"] = idempotency_key

        owner_token = f"worker_{uuid.uuid4().hex[:12]}"
        lease_until = (now_dt + timedelta(seconds=30)).isoformat()

        # Phase 1: Short transaction to reserve request & intent before any external IO
        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "propose_memory_candidate", op_payload)
            if existing:
                return existing

            row = conn.execute(
                "SELECT operation_id, request_hash, result_status FROM operation_log WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key),
            ).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise IdempotencyMismatchError(
                        f"Idempotency key '{idempotency_key}' was previously used with a different request or action."
                    )

            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute("SELECT state_version FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]

            if not row:
                conn.execute(
                    """INSERT INTO operation_log(
                        operation_id, user_id, idempotency_key, request_hash, action,
                        result_status, before_version, after_version, response_json, created_at
                    ) VALUES (?, ?, ?, ?, 'propose_memory_candidate', 'pending', ?, ?, '', ?)""",
                    (operation_id, user_id, idempotency_key, request_hash, before_version, before_version, now),
                )
            else:
                operation_id = row["operation_id"]

            outbox_row = conn.execute(
                "SELECT intent_id, status, owner_token, lease_until FROM memory_outbox WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if outbox_row:
                if (
                    outbox_row["status"] == "in_flight"
                    and outbox_row["lease_until"]
                    and outbox_row["lease_until"] > now
                ):
                    data = {
                        "intent_id": intent_id,
                        "status": "in_flight",
                        "warning": "MEMORY_IN_FLIGHT: Candidate proposition is currently executing under active lease.",
                    }
                    return self._response(
                        operation_id=operation_id,
                        status="partial",
                        data=data,
                        state_version=before_version,
                        warnings=["MEMORY_IN_FLIGHT: Candidate proposition is currently executing."],
                    )

                conn.execute(
                    """UPDATE memory_outbox
                       SET status = 'in_flight', owner_token = ?, lease_until = ?, updated_at = ?
                       WHERE intent_id = ?""",
                    (owner_token, lease_until, now, intent_id),
                )
            else:
                conn.execute(
                    """INSERT INTO memory_outbox(
                        intent_id, user_id, idempotency_key, request_hash, method, payload_json,
                        status, attempts, owner_token, lease_until, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'in_flight', 0, ?, ?, ?, ?)""",
                    (
                        intent_id,
                        user_id,
                        idempotency_key,
                        request_hash,
                        method,
                        self.store.json(call_payload),
                        owner_token,
                        lease_until,
                        now,
                        now,
                    ),
                )

        # Phase 2: External MemoryProvider IO executed strictly OUTSIDE SQLite lock
        provider_success = False
        provider_result: dict[str, Any] | None = None
        try:
            provider_result = self.memory_provider.call(method, call_payload)
            provider_success = True
        except (MemoryUnavailable, Exception):
            provider_success = False

        # Phase 3: Short transaction to update outbox status, state_version, and operation log
        with self.store.transaction() as conn:
            profile = conn.execute("SELECT state_version FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]
            after_version = before_version + 1

            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            if provider_success:
                conn.execute(
                    """UPDATE memory_outbox
                       SET status = 'sent', owner_token = NULL, lease_until = NULL, attempts = attempts + 1, result_json = ?, updated_at = ?
                       WHERE intent_id = ? AND owner_token = ?""",
                    (self.store.json(provider_result), now, intent_id, owner_token),
                )
                response = self._response(
                    operation_id,
                    "success",
                    {"intent_id": intent_id, "result": provider_result},
                    after_version,
                )
            else:
                conn.execute(
                    """UPDATE memory_outbox
                       SET status = 'pending', owner_token = NULL, lease_until = NULL, attempts = attempts + 1, updated_at = ?
                       WHERE intent_id = ? AND owner_token = ?""",
                    (now, intent_id, owner_token),
                )
                response = self._response(
                    operation_id,
                    "partial",
                    {"intent_id": intent_id},
                    after_version,
                    warnings=["MEMORY_DEFERRED: MemoryProvider unavailable. Intent queued in memory_outbox."],
                )

            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=op_payload,
                action="propose_memory_candidate",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def memory_action(
        self,
        *,
        user_id: str,
        action_type: str,
        idempotency_key: str,
        candidate_id: str | None = None,
        target_note_path: str | None = None,
        confirmed: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate and execute memory action (propose, confirm, reject, update, delete) via durable outbox."""
        p = dict(payload or {})
        if candidate_id is not None:
            p["candidate_id"] = candidate_id
        if target_note_path is not None:
            p["target_note_path"] = target_note_path
        if confirmed:
            p["confirmed"] = True

        # Validation happens FIRST: any invalid action or unconfirmed delete/confirm fails immediately before DB/IO
        try:
            validated = MemoryActionInput(
                user_id=user_id,
                action_type=action_type,
                idempotency_key=idempotency_key,
                candidate_id=candidate_id,
                target_note_path=target_note_path,
                confirmed=confirmed,
                payload=p,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        method = f"memory.{validated.action_type}"
        return self.propose_memory_candidate(
            user_id=user_id,
            method=method,
            payload=p,
            idempotency_key=idempotency_key,
        )

    def maintain_memory(
        self,
        *,
        user_id: str,
        prune_days: int = 30,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Drain pending outbox intents with bounded IO outside locks and calculate eligible prune counts."""
        try:
            MaintainMemoryInput(user_id=user_id, prune_days=prune_days, idempotency_key=idempotency_key)
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {"action": "maintain_memory", "user_id": user_id, "prune_days": prune_days}
        owner_token = f"maint_{uuid.uuid4().hex[:12]}"
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease_until = (now_dt + timedelta(seconds=30)).isoformat()
        operation_id = f"op_{uuid.uuid4().hex}"

        # 1. Atomic in-flight claim inside a short transaction with owner_token + lease (max 50)
        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, user_id, idempotency_key, "maintain_memory", payload)
            if existing:
                return existing

            eligible_rows = conn.execute(
                """SELECT intent_id, method, payload_json, attempts
                   FROM memory_outbox
                   WHERE user_id = ?
                     AND (status = 'pending' OR (status = 'in_flight' AND lease_until IS NOT NULL AND lease_until < ?))
                   ORDER BY created_at ASC
                   LIMIT 50""",
                (user_id, now),
            ).fetchall()

            claimed_ids = [r["intent_id"] for r in eligible_rows]
            if claimed_ids:
                placeholders = ",".join("?" for _ in claimed_ids)
                conn.execute(
                    f"""UPDATE memory_outbox
                        SET status = 'in_flight', owner_token = ?, lease_until = ?, updated_at = ?
                        WHERE intent_id IN ({placeholders})""",
                    [owner_token, lease_until, now, *claimed_ids],
                )

        # 2. Network / MemoryProvider IO executed strictly OUTSIDE SQLite lock
        results: list[tuple[str, bool, dict[str, Any] | None]] = []
        for r in eligible_rows:
            intent_id = r["intent_id"]
            method = r["method"]
            m_payload = json.loads(r["payload_json"])
            m_payload["user_id"] = user_id
            m_payload["intent_id"] = intent_id
            try:
                call_method = method if (method.startswith("memory.") or method == "ping") else f"memory.{method}"
                try:
                    res = self.memory_provider.call(call_method, m_payload)
                except Exception as call_err:
                    if not isinstance(call_err, MemoryUnavailable) and call_method != method:
                        res = self.memory_provider.call(method, m_payload)
                    else:
                        raise
                results.append((intent_id, True, res))
            except (MemoryUnavailable, Exception):
                results.append((intent_id, False, None))

        # 3. Short write transaction to update final outbox statuses and evaluate prune candidates
        with self.store.transaction() as conn:
            self._ensure_profile_in_tx(conn, user_id, now)
            profile = conn.execute("SELECT state_version FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
            before_version = profile["state_version"]

            sent_count = 0
            deferred_count = 0
            for intent_id, success, res in results:
                if success:
                    cur = conn.execute(
                        """UPDATE memory_outbox
                           SET status = 'sent', owner_token = NULL, lease_until = NULL, attempts = attempts + 1, result_json = ?, updated_at = ?
                           WHERE intent_id = ? AND owner_token = ?""",
                        (self.store.json(res), now, intent_id, owner_token),
                    )
                    if cur.rowcount > 0:
                        sent_count += 1
                else:
                    cur = conn.execute(
                        """UPDATE memory_outbox
                           SET status = 'pending', owner_token = NULL, lease_until = NULL, attempts = attempts + 1, updated_at = ?
                           WHERE intent_id = ? AND owner_token = ?""",
                        (now, intent_id, owner_token),
                    )
                    if cur.rowcount > 0:
                        deferred_count += 1

            remaining_pending = conn.execute(
                "SELECT COUNT(*) AS c FROM memory_outbox WHERE user_id = ? AND status = 'pending'",
                (user_id,),
            ).fetchone()["c"]
            deferred_count = max(deferred_count, remaining_pending)

            # REAL physical TTL cleanup of expired records (while preserving operation_log audit chain)
            purged_superseded_count = 0
            purged_outbox_count = 0
            cutoff_iso = (now_dt - timedelta(days=prune_days)).isoformat()

            # Protect parent_id revision lineage: only delete unreferenced superseded records
            cur_domain = conn.execute(
                """DELETE FROM domain_record
                   WHERE user_id = ?
                     AND status IN ('superseded', 'deleted')
                     AND created_at < ?
                     AND record_id NOT IN (
                         SELECT DISTINCT parent_id FROM domain_record WHERE parent_id IS NOT NULL
                     )""",
                (user_id, cutoff_iso),
            )
            purged_superseded_count += cur_domain.rowcount

            # For superseded records that are referenced as parent_id, preserve them for lineage reconstruction
            conn.execute(
                """UPDATE domain_record
                   SET body_json = json_set(body_json, '$._compacted', 1, '$._retention', 'lineage_preserved')
                   WHERE user_id = ?
                     AND status IN ('superseded', 'deleted')
                     AND created_at < ?
                     AND record_id IN (
                         SELECT DISTINCT parent_id FROM domain_record WHERE parent_id IS NOT NULL
                     )""",
                (user_id, cutoff_iso),
            )

            cur_outbox = conn.execute(
                "DELETE FROM memory_outbox WHERE user_id = ? AND status = 'sent' AND created_at < ?",
                (user_id, cutoff_iso),
            )
            purged_outbox_count += cur_outbox.rowcount

            cutoff_date = (now_dt - timedelta(days=prune_days)).strftime("%Y-%m-%d")
            eligible_count = conn.execute(
                "SELECT COUNT(*) AS c FROM meal_log WHERE user_id = ? AND substr(occurred_at, 1, 10) < ? AND status = 'superseded'",
                (user_id, cutoff_date),
            ).fetchone()["c"]

            after_version = before_version + 1

            # Local weekly trend consolidation for historical meals older than cutoff_date
            consolidated_trends_count = 0
            user_tz_name = profile["timezone"] if (profile and "timezone" in profile.keys()) else "Asia/Shanghai"
            try:
                user_tz = ZoneInfo(user_tz_name)
            except Exception:
                user_tz = ZoneInfo("UTC")

            historical_meals = conn.execute(
                """SELECT meal_id, occurred_at, foods_json, kcal_low, kcal_high, protein_low, protein_high
                   FROM meal_log
                   WHERE user_id = ? AND status = 'active' AND substr(occurred_at, 1, 10) < ?
                   ORDER BY occurred_at ASC""",
                (user_id, cutoff_date),
            ).fetchall()

            weeks_map: dict[str, dict[str, Any]] = {}
            if historical_meals:
                for m in historical_meals:
                    occ = m["occurred_at"]
                    try:
                        dt = datetime.fromisoformat(occ.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            local_dt = dt.replace(tzinfo=user_tz)
                        else:
                            local_dt = dt.astimezone(user_tz)
                    except Exception:
                        local_dt = datetime.strptime(occ[:10], "%Y-%m-%d").replace(tzinfo=user_tz)

                    year, week, _ = local_dt.isocalendar()
                    iso_week = f"{year}-W{week:02d}"
                    local_day = local_dt.strftime("%Y-%m-%d")

                    if iso_week not in weeks_map:
                        w_start = datetime.fromisocalendar(year, week, 1).strftime("%Y-%m-%d")
                        w_end = datetime.fromisocalendar(year, week, 7).strftime("%Y-%m-%d")
                        weeks_map[iso_week] = {
                            "iso_week": iso_week,
                            "window_start": w_start,
                            "window_end": w_end,
                            "days": {},
                        }

                    day_entry = weeks_map[iso_week]["days"].setdefault(local_day, {
                        "kcal_low": 0,
                        "kcal_high": 0,
                        "protein_low": 0,
                        "protein_high": 0,
                        "meals_count": 0,
                    })
                    day_entry["kcal_low"] += m["kcal_low"]
                    day_entry["kcal_high"] += m["kcal_high"]
                    day_entry["protein_low"] += m["protein_low"]
                    day_entry["protein_high"] += m["protein_high"]
                    day_entry["meals_count"] += 1

                for iso_week, winfo in weeks_map.items():
                    window_days = 7
                    days_dict = winfo["days"]
                    recorded_dates = sorted(list(days_dict.keys()))
                    recorded_days = len(recorded_dates)
                    if recorded_days == 0:
                        continue
                    missing_days = window_days - recorded_days
                    total_meals = sum(d["meals_count"] for d in days_dict.values())

                    # Aggregate per recorded day, then calculate true daily average
                    # Disclose recorded vs missing days without fabricating 0-calorie days
                    sum_day_kl = sum(d["kcal_low"] for d in days_dict.values())
                    sum_day_kh = sum(d["kcal_high"] for d in days_dict.values())
                    sum_day_pl = sum(d["protein_low"] for d in days_dict.values())
                    sum_day_ph = sum(d["protein_high"] for d in days_dict.values())

                    avg_daily_kl = round(sum_day_kl / recorded_days)
                    avg_daily_kh = round(sum_day_kh / recorded_days)
                    avg_daily_pl = round(sum_day_pl / recorded_days)
                    avg_daily_ph = round(sum_day_ph / recorded_days)

                    trend_data = {
                        "period_type": "weekly",
                        "iso_week": iso_week,
                        "window_start": winfo["window_start"],
                        "window_end": winfo["window_end"],
                        "window_days": window_days,
                        "recorded_days": recorded_days,
                        "missing_days": missing_days,
                        "recorded_day_dates": recorded_dates,
                        "total_meals": total_meals,
                        "avg_daily_kcal": [avg_daily_kl, avg_daily_kh],
                        "avg_daily_protein": [avg_daily_pl, avg_daily_ph],
                        # Backward compatibility aliases
                        "period_end": winfo["window_end"],
                        "aggregated_meals": total_meals,
                        "avg_kcal": [avg_daily_kl, avg_daily_kh],
                        "avg_protein": [avg_daily_pl, avg_daily_ph],
                    }
                    trend_body_json = self.store.json(trend_data)

                    # Revision chaining: check existing active weekly trend for this week
                    existing_trend = conn.execute(
                        """SELECT record_id, body_json, state_version
                           FROM domain_record
                           WHERE user_id = ? AND kind = 'weekly_nutrition_trend' AND day = ? AND status = 'active'""",
                        (user_id, iso_week),
                    ).fetchone()

                    if existing_trend:
                        try:
                            ex_body = json.loads(existing_trend["body_json"])
                        except Exception:
                            ex_body = {}
                        has_changed = (
                            ex_body.get("total_meals") != total_meals or
                            ex_body.get("recorded_days") != recorded_days or
                            ex_body.get("avg_daily_kcal") != [avg_daily_kl, avg_daily_kh] or
                            ex_body.get("avg_daily_protein") != [avg_daily_pl, avg_daily_ph]
                        )
                        if has_changed:
                            # Supersede old revision and chain via parent_id
                            conn.execute(
                                "UPDATE domain_record SET status = 'superseded' WHERE record_id = ?",
                                (existing_trend["record_id"],),
                            )
                            new_rec_id = f"trend_nutr_{user_id}_{iso_week}_{uuid.uuid4().hex[:8]}"
                            conn.execute(
                                """INSERT INTO domain_record(
                                    record_id, user_id, kind, day, body_json, parent_id, status, causation_id, state_version, created_at
                                ) VALUES (?, ?, 'weekly_nutrition_trend', ?, ?, ?, 'active', ?, ?, ?)""",
                                (new_rec_id, user_id, iso_week, trend_body_json, existing_trend["record_id"], operation_id, after_version, now),
                            )
                            consolidated_trends_count += 1
                    else:
                        prev_trend = conn.execute(
                            """SELECT record_id FROM domain_record
                               WHERE user_id = ? AND kind = 'weekly_nutrition_trend' AND day = ?
                               ORDER BY created_at DESC LIMIT 1""",
                            (user_id, iso_week),
                        ).fetchone()
                        parent_id = prev_trend["record_id"] if prev_trend else None
                        new_rec_id = f"trend_nutr_{user_id}_{iso_week}_{uuid.uuid4().hex[:8]}"
                        conn.execute(
                            """INSERT INTO domain_record(
                                record_id, user_id, kind, day, body_json, parent_id, status, causation_id, state_version, created_at
                            ) VALUES (?, ?, 'weekly_nutrition_trend', ?, ?, ?, 'active', ?, ?, ?)""",
                            (new_rec_id, user_id, iso_week, trend_body_json, parent_id, operation_id, after_version, now),
                        )
                        consolidated_trends_count += 1

                # Safely clear ingredients detail (foods_json = '[]') while preserving numerical macros and audit lineage
                conn.execute(
                    """UPDATE meal_log
                       SET foods_json = '[]'
                       WHERE user_id = ? AND status = 'active'
                         AND substr(occurred_at, 1, 10) < ?
                         AND foods_json != '[]'""",
                    (user_id, cutoff_date),
                )

            # Check for stale active weekly trends where all historical meals were deleted
            existing_active_trends = conn.execute(
                """SELECT record_id, day, body_json
                   FROM domain_record
                   WHERE user_id = ? AND kind = 'weekly_nutrition_trend' AND status = 'active'""",
                (user_id,),
            ).fetchall()

            for ex_trend in existing_active_trends:
                tr_week = ex_trend["day"]
                if tr_week not in weeks_map:
                    try:
                        b = json.loads(ex_trend["body_json"])
                        w_end = b.get("window_end", "")
                    except Exception:
                        w_end = ""
                    if not w_end or w_end < cutoff_date:
                        conn.execute(
                            "UPDATE domain_record SET status = 'superseded' WHERE record_id = ?",
                            (ex_trend["record_id"],),
                        )
                        retract_id = f"trend_nutr_{user_id}_{tr_week}_{uuid.uuid4().hex[:8]}"
                        retract_data = {
                            "period_type": "weekly",
                            "iso_week": tr_week,
                            "window_end": w_end or cutoff_date,
                            "total_meals": 0,
                            "recorded_days": 0,
                            "status": "retracted",
                            "retraction_reason": "all_historical_meals_deleted",
                        }
                        conn.execute(
                            """INSERT INTO domain_record(
                                record_id, user_id, kind, day, body_json, parent_id, status, causation_id, state_version, created_at
                            ) VALUES (?, ?, 'weekly_nutrition_trend', ?, ?, ?, 'superseded', ?, ?, ?)""",
                            (retract_id, user_id, tr_week, self.store.json(retract_data), ex_trend["record_id"], operation_id, after_version, now),
                        )
                        consolidated_trends_count += 1

            conn.execute(
                "UPDATE user_profile SET state_version = ?, updated_at = ? WHERE user_id = ?",
                (after_version, now, user_id),
            )

            # Calculate remaining due work at the end of transaction
            due_info = self._calculate_maintenance_due(conn, user_id, now_dt, prune_days=prune_days)
            has_more = due_info["due"]
            next_maintenance_key = due_info["maintenance_key"]
            continuation_token = next_maintenance_key
            retry_after_seconds = due_info["retry_after_seconds"]
            if deferred_count > 0 and retry_after_seconds is None:
                retry_after_seconds = 2

            warnings: list[str] = []
            if deferred_count > 0:
                warnings.append(
                    f"MEMORY_DEFERRED: {deferred_count} memory intents remain queued in memory_outbox."
                )

            status_str = "partial" if deferred_count > 0 else "success"
            pruned_records = purged_superseded_count + purged_outbox_count
            data = {
                "outbox_processed": len(results),
                "sent_count": sent_count,
                "deferred_count": deferred_count,
                "eligible_prune_count": eligible_count,
                "purged_superseded_count": purged_superseded_count,
                "purged_outbox_count": purged_outbox_count,
                "pruned_records": pruned_records,
                "consolidated_trends": consolidated_trends_count,
                "new_candidates": len(results),
                "audit_chain_preserved": True,
                # Round 14 continuation & state machine fields:
                "has_more": has_more,
                "continuation_token": continuation_token,
                "next_maintenance_key": next_maintenance_key,
                "retry_after_seconds": retry_after_seconds,
                "maintenance_recommended": has_more,
                "maintenance_reason": due_info["reason"],
                "due_work": due_info["due_work"],
            }
            response = self._response(
                operation_id,
                status_str,
                data,
                after_version,
                warnings=warnings,
            )
            # Expose top-level continuation attributes
            response["has_more"] = has_more
            response["next_maintenance_key"] = next_maintenance_key
            response["continuation_token"] = continuation_token
            if retry_after_seconds is not None:
                response["retry_after_seconds"] = retry_after_seconds

            self._record_operation(
                conn,
                operation_id=operation_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                payload=payload,
                action="maintain_memory",
                before_version=before_version,
                response=response,
                now=now,
            )
            return response

    def query_memory(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Dual-layer memory query: queries SQLite short-term facts + external MemoryProvider."""
        try:
            QueryMemoryInput(user_id=user_id, query=query, limit=limit)
        except Exception as err:
            raise ValidationError(str(err)) from err

        q_lower = query.lower()
        terms = [t for t in q_lower.split() if len(t) > 1]
        warnings: list[str] = []

        # Layer 1: Query local SQLite short-term facts (profile, domain records, meals)
        sqlite_facts: list[dict[str, Any]] = []
        with self.store.connect() as conn:
            profile = conn.execute(
                "SELECT goals_json, constraints_json, safety_flags_json, state_version FROM user_profile WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            version = profile["state_version"] if profile else 0

            # 1. Profile constraints & goals
            if profile:
                try:
                    c_dict = json.loads(profile["constraints_json"] or "{}")
                    for k, v in c_dict.items():
                        text = f"{k} {v}".lower()
                        if any(term in text for term in terms):
                            sqlite_facts.append({
                                "source_type": "sqlite_short_term_profile",
                                "record_id": f"constraint_{k}",
                                "content": f"Constraint {k}: {v}",
                                "occurred_at": None,
                                "confidence": 1.0,
                                "confirmation_status": "committed",
                            })
                except Exception:
                    pass

            # 2. Domain records (workouts, reviews, daily state, trends)
            records = conn.execute(
                """SELECT record_id, kind, day, body_json, created_at
                   FROM domain_record
                   WHERE user_id = ? AND status = 'active'
                   ORDER BY day DESC, created_at DESC LIMIT 50""",
                (user_id,),
            ).fetchall()
            for r in records:
                b_str = r["body_json"] or ""
                b_lower = b_str.lower()
                if any(term in b_lower for term in terms):
                    try:
                        b_dict = json.loads(b_str)
                    except Exception:
                        b_dict = {}
                    summary_text = (
                        b_dict.get("discomfort_notes")
                        or b_dict.get("summary_md")
                        or b_dict.get("action_item")
                        or b_str[:120]
                    )
                    sqlite_facts.append({
                        "source_type": f"sqlite_short_term_{r['kind']}",
                        "record_id": r["record_id"],
                        "content": str(summary_text),
                        "occurred_at": r["day"] or r["created_at"],
                        "confidence": 1.0,
                        "confirmation_status": "committed",
                    })

            # 3. Active meals
            meals = conn.execute(
                """SELECT meal_id, occurred_at, foods_json, kcal_low, kcal_high
                   FROM meal_log
                   WHERE user_id = ? AND status = 'active'
                   ORDER BY occurred_at DESC LIMIT 50""",
                (user_id,),
            ).fetchall()
            for m in meals:
                f_str = m["foods_json"] or ""
                if any(term in f_str.lower() for term in terms):
                    sqlite_facts.append({
                        "source_type": "sqlite_short_term_meal",
                        "record_id": m["meal_id"],
                        "content": f"Meal foods: {f_str} ({m['kcal_low']}-{m['kcal_high']} kcal)",
                        "occurred_at": m["occurred_at"],
                        "confidence": 1.0,
                        "confirmation_status": "committed",
                    })

        # Bounded to requested limit
        sqlite_facts = sqlite_facts[:limit]

        # Layer 2: Query external MemoryProvider (Obsidian Long-term memory)
        obsidian_memories: list[dict[str, Any]] = []
        provider_available = False
        try:
            prov_res = self.memory_provider.call("query", {"user_id": user_id, "query": query, "limit": limit})
            if not isinstance(prov_res, dict) or "items" not in prov_res or not isinstance(prov_res.get("items"), list):
                provider_available = False
                obsidian_memories = []
                warnings.append(
                    f"PROVIDER_MALFORMED_RESPONSE: MemoryProvider returned invalid structure (expected dict with 'items' list, got {type(prov_res).__name__})."
                )
            else:
                provider_available = True
                for it in prov_res["items"]:
                    if not isinstance(it, dict):
                        warnings.append("PROVIDER_ITEM_INVALID: Skipped non-dictionary memory item.")
                        continue

                    # Status must preserve actual source status; never fabricate 'confirmed_wiki' if absent
                    raw_status = it.get("confirmation_status")
                    conf_status = raw_status if raw_status else "unconfirmed"

                    # Confidence must reflect evidence; never invent fake default like 0.9
                    conf_val = it.get("confidence")
                    if conf_val is not None:
                        try:
                            conf_val = float(conf_val)
                            if math.isnan(conf_val) or math.isinf(conf_val):
                                conf_val = None
                        except (ValueError, TypeError):
                            conf_val = None

                    obsidian_memories.append({
                        "source_type": "obsidian_long_term_memory",
                        "record_id": it.get("candidate_id") or it.get("id") or f"obs_{uuid.uuid4().hex[:8]}",
                        "content": it.get("content") or it.get("statement") or str(it),
                        "occurred_at": it.get("occurred_at"),
                        "confidence": conf_val,
                        "confirmation_status": conf_status,
                    })
                # Bound returned memories strictly by caller's requested limit
                obsidian_memories = obsidian_memories[:limit]
        except (MemoryUnavailable, Exception) as exc:
            provider_available = False
            obsidian_memories = []
            warnings.append(
                f"MEMORY_DEFERRED: External MemoryProvider unavailable or unconfigured ({type(exc).__name__}). Returning SQLite short-term facts only."
            )

        # Deterministic red-flag safety rule priority
        red_flags = self._scan_for_red_flags(query)
        safety_advisory = None
        if red_flags:
            safety_advisory = f"SAFETY_RESTRICTED: Acute red-flag symptom queried ({', '.join(red_flags)}). Clinical safety rules supersede memory heuristics."
            warnings.append(safety_advisory)

        operation_id = f"op_read_mem_{user_id}_{version}"
        data = {
            "user_id": user_id,
            "query": query,
            "sqlite_facts_count": len(sqlite_facts),
            "sqlite_facts": sqlite_facts,
            "obsidian_provider_connected": provider_available,
            "obsidian_memories_count": len(obsidian_memories),
            "obsidian_memories": obsidian_memories,
            "safety_advisory": safety_advisory,
        }
        return {
            "operation_id": operation_id,
            "status": "success",
            "data": data,
            "warnings": warnings,
            "error": None,
            "state_version": version,
            **data,
        }

    @staticmethod
    def _memory_name_key(value: Any) -> str:
        """Normalize a user-provided food/exercise name for deterministic grouping."""
        text = str(value or "").strip().casefold()
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", text)

    @staticmethod
    def _memory_names_from_json(value: Any, *, keys: tuple[str, ...]) -> list[str]:
        """Extract bounded, human-readable names without treating estimates as evidence."""
        if not isinstance(value, list):
            return []
        result: dict[str, str] = {}
        for item in value:
            if isinstance(item, str):
                raw = item.strip()
            elif isinstance(item, dict):
                raw = ""
                for key in keys:
                    if item.get(key):
                        raw = str(item[key]).strip()
                        break
            else:
                raw = ""
            norm = CyberHealthService._memory_name_key(raw)
            if norm and raw:
                result.setdefault(norm, raw)
        return sorted(result.values(), key=lambda item: CyberHealthService._memory_name_key(item))

    @staticmethod
    def _memory_candidate_recently_seen(
        conn: Any,
        *,
        user_id: str,
        candidate_key: str,
        now: datetime,
    ) -> bool:
        """Apply a small anti-spam cooldown using only the local outbox.

        This is intentionally a read-only lookup. A provider or the host remains
        responsible for the actual candidate lifecycle and explicit confirmation.
        """
        rows = conn.execute(
            """SELECT method, payload_json, created_at
               FROM memory_outbox
               WHERE user_id = ? AND method IN ('memory.propose', 'memory.confirm', 'memory.reject')
               ORDER BY created_at DESC LIMIT 200""",
            (user_id,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("candidate_key") != candidate_key:
                continue
            try:
                created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                continue
            age = now - created_at.astimezone(UTC)
            cooldown = timedelta(days=30) if row["method"] == "memory.reject" else timedelta(days=7)
            if age >= timedelta(0) and age < cooldown:
                return True
        return False

    @staticmethod
    def _memory_daily_proposal_count(conn: Any, *, user_id: str, tz_name: str, now: datetime) -> int:
        """Count today's proposals without changing state."""
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        local_today = now.astimezone(tz).strftime("%Y-%m-%d")
        rows = conn.execute(
            """SELECT created_at
               FROM memory_outbox
               WHERE user_id = ? AND method = 'memory.propose'
               ORDER BY created_at DESC LIMIT 200""",
            (user_id,),
        ).fetchall()
        count = 0
        for row in rows:
            try:
                created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                if created_at.astimezone(tz).strftime("%Y-%m-%d") == local_today:
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    def get_memory_suggestions(
        self,
        *,
        user_id: str,
        date: str,
        window_days: int = 30,
        limit: int = 3,
    ) -> dict[str, Any]:
        """Find repeated, user-confirmed health patterns as non-mutating suggestions.

        The method deliberately reads only active SQLite facts and the local
        memory outbox. It never calls a provider, creates an Inbox candidate, or
        changes ``state_version``. The host must ask the user before proposing or
        confirming an inferred long-term memory.
        """
        try:
            validated = GetMemorySuggestionsInput(
                user_id=user_id,
                date=date,
                window_days=window_days,
                limit=limit,
            )
        except Exception as err:
            raise ValidationError(str(err)) from err

        end_day = datetime.strptime(validated.date, "%Y-%m-%d").date()
        start_day = end_day - timedelta(days=validated.window_days - 1)
        start_text = start_day.isoformat()
        end_text = end_day.isoformat()
        now = datetime.now(UTC)

        with self.store.connect() as conn:
            profile = conn.execute(
                "SELECT timezone, state_version FROM user_profile WHERE user_id = ?",
                (validated.user_id,),
            ).fetchone()
            tz_name = str(profile["timezone"] if profile and profile["timezone"] else "Asia/Shanghai")
            version = int(profile["state_version"] if profile else 0)
            daily_proposal_count = self._memory_daily_proposal_count(
                conn, user_id=validated.user_id, tz_name=tz_name, now=now
            )

            meal_groups: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
            meal_rows = conn.execute(
                """SELECT meal_id, occurred_at, meal_type, foods_json
                   FROM meal_log
                   WHERE user_id = ? AND status = 'active'
                   ORDER BY occurred_at ASC, meal_id ASC""",
                (validated.user_id,),
            ).fetchall()
            for row in meal_rows:
                try:
                    local_day = self._parse_day_in_timezone(row["occurred_at"], tz_name)
                except (TypeError, ValueError):
                    continue
                if not start_text <= local_day <= end_text:
                    continue
                try:
                    foods = json.loads(row["foods_json"] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                names = self._memory_names_from_json(foods, keys=("name",))
                if not names:
                    continue
                meal_type = str(row["meal_type"] or "meal").strip() or "meal"
                group_key = (self._memory_name_key(meal_type), tuple(self._memory_name_key(n) for n in names))
                group = meal_groups.setdefault(
                    group_key,
                    {
                        "meal_type": meal_type,
                        "names": names[:5],
                        "days": set(),
                        "records": [],
                    },
                )
                group["days"].add(local_day)
                group["records"].append((local_day, row["meal_id"]))

            workout_groups: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
            workout_rows = conn.execute(
                """SELECT record_id, day, kind, body_json
                   FROM domain_record
                   WHERE user_id = ? AND kind IN ('workout', 'workout_log') AND status = 'active'
                     AND day >= ? AND day <= ?
                   ORDER BY day ASC, record_id ASC""",
                (validated.user_id, start_text, end_text),
            ).fetchall()
            for row in workout_rows:
                day = str(row["day"] or "")[:10]
                try:
                    datetime.strptime(day, "%Y-%m-%d")
                except ValueError:
                    continue
                try:
                    body = json.loads(row["body_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue

                activity = body.get("activity_summary") if isinstance(body, dict) else None
                activity_type = (
                    activity.get("activity_type")
                    if isinstance(activity, dict) and activity.get("user_confirmed", True) is True
                    else None
                )
                if activity_type:
                    names = [str(activity_type).strip()]
                else:
                    names = self._memory_names_from_json(
                        body.get("actual_sets") if isinstance(body, dict) else [],
                        keys=("exercise", "name"),
                    )
                    if not names:
                        names = self._memory_names_from_json(
                            body.get("completed_exercises") if isinstance(body, dict) else [],
                            keys=("name", "exercise"),
                        )
                # A plan is intent, not completed activity. Empty check-ins and
                # plan-only records must not create a durable pattern.
                if not names:
                    continue
                group_key = ("workout", tuple(self._memory_name_key(n) for n in names))
                group = workout_groups.setdefault(
                    group_key,
                    {
                        "names": names[:5],
                        "days": set(),
                        "records": [],
                    },
                )
                group["days"].add(day)
                group["records"].append((day, row["record_id"]))

            candidates: list[dict[str, Any]] = []
            for group in meal_groups.values():
                days = sorted(group["days"])
                if len(days) < 3:
                    continue
                names = sorted(group["names"], key=self._memory_name_key)
                fingerprint = "|".join(self._memory_name_key(n) for n in names)
                candidate_key = f"meal-pattern:{self._memory_name_key(group['meal_type'])}:{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:12]}"
                if self._memory_candidate_recently_seen(
                    conn, user_id=validated.user_id, candidate_key=candidate_key, now=now
                ):
                    continue
                source_ids = [rid for _, rid in sorted(group["records"], key=lambda item: (item[0], item[1]))]
                statement = (
                    f"在最近 {validated.window_days} 天内，你在 {len(days)} 个不同日期的"
                    f"{group['meal_type']}记录中反复出现：{'、'.join(names)}。"
                )
                evidence = {
                    "window_start": start_text,
                    "window_end": end_text,
                    "distinct_days": days,
                    "evidence_count": len(source_ids),
                    "source_record_ids": source_ids,
                }
                candidates.append(
                    self._memory_suggestion_payload(
                        candidate_key=candidate_key,
                        candidate_type="repeated_meal_pattern",
                        title=f"重复饮食模式：{group['meal_type']} · {'、'.join(names)}",
                        statement=statement,
                        evidence=evidence,
                    )
                )

            for group in workout_groups.values():
                days = sorted(group["days"])
                if len(days) < 3:
                    continue
                names = sorted(group["names"], key=self._memory_name_key)
                fingerprint = "|".join(self._memory_name_key(n) for n in names)
                candidate_key = f"workout-pattern:{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:12]}"
                if self._memory_candidate_recently_seen(
                    conn, user_id=validated.user_id, candidate_key=candidate_key, now=now
                ):
                    continue
                source_ids = [rid for _, rid in sorted(group["records"], key=lambda item: (item[0], item[1]))]
                label = "、".join(names)
                statement = (
                    f"在最近 {validated.window_days} 天内，你在 {len(days)} 个不同日期的训练记录中"
                    f"反复进行：{label}。"
                )
                evidence = {
                    "window_start": start_text,
                    "window_end": end_text,
                    "distinct_days": days,
                    "evidence_count": len(source_ids),
                    "source_record_ids": source_ids,
                }
                candidates.append(
                    self._memory_suggestion_payload(
                        candidate_key=candidate_key,
                        candidate_type="repeated_workout_pattern",
                        title=f"重复训练模式：{label}",
                        statement=statement,
                        evidence=evidence,
                    )
                )

        candidates.sort(
            key=lambda item: (
                -int(item["evidence"]["evidence_count"]),
                item["candidate_type"],
                item["candidate_key"],
            )
        )
        if daily_proposal_count >= 3:
            candidates = []
        else:
            candidates = candidates[: min(validated.limit, 3 - daily_proposal_count)]
        data = {
            "user_id": validated.user_id,
            "date": validated.date,
            "window_days": validated.window_days,
            "suggestions": candidates,
            "daily_proposal_count": daily_proposal_count,
            "daily_proposal_limit": 3,
            "session_display_limit": 1,
            "safety_advisory": (
                "这些是基于已记录事实的长期记忆候选，不是医学结论。展示前必须让用户确认；"
                "用户拒绝或未确认时，不得调用 memory.confirm。"
            ),
        }
        return self._response(
            f"op_read_memory_suggestions_{validated.user_id}_{version}",
            "success",
            data,
            version,
        )

    @staticmethod
    def _memory_suggestion_payload(
        *,
        candidate_key: str,
        candidate_type: str,
        title: str,
        statement: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the stable payload the host can pass to memory_action.propose."""
        return {
            "suggestion_id": f"suggestion_{hashlib.sha256(candidate_key.encode('utf-8')).hexdigest()[:16]}",
            "candidate_key": candidate_key,
            "candidate_type": candidate_type,
            "title": title,
            "statement": statement,
            "content": statement,
            "evidence": evidence,
            "source_method": "cyber-health-active-pattern",
            "requires_user_confirmation": True,
            "propose_payload": {
                "candidate_key": candidate_key,
                "candidate_type": candidate_type,
                "title": title,
                "statement": statement,
                "content": statement,
                "evidence": evidence,
                "source_method": "cyber-health-active-pattern",
            },
        }


__all__ = [
    "CyberHealthService",
    "ConflictError",
    "IdempotencyMismatchError",
    "SafetyRestrictedError",
    "StoreBusyError",
    "ValidationError",
]
