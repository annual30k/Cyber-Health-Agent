"""Profile, first-run onboarding and nightly automation declaration."""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from typing import Any

from ..errors import ConflictError, ValidationError
from ..models import UpdateProfileInput
from .base import ServiceCore
from .catalog import RED_FLAG_KEYWORDS


class ProfileMixin(ServiceCore):
    """Profile, first-run onboarding and nightly automation declaration."""

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
                deload_until = (self._utcnow() + timedelta(days=7)).isoformat()
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
