"""Nightly fact collection, daily review and tomorrow's plan."""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from ..errors import ConflictError, ValidationError
from ..models import DailyReviewInput, PlanTomorrowInput
from .base import _MALFORMED_RECORD_ERRORS, ServiceCore
from .catalog import RED_FLAG_KEYWORDS


class ReviewMixin(ServiceCore):
    """Nightly fact collection, daily review and tomorrow's plan."""

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
            except _MALFORMED_RECORD_ERRORS:
                body = {}
            completion = body.get("completion_rate")
            if completion is not None:
                with contextlib.suppress(TypeError, ValueError):
                    completion_rates.append(float(completion))
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
            existing = self._check_idempotency(
                conn, user_id, idempotency_key, "daily_review", payload, recompute_when_stale=True
            )
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
            now_dt = datetime.fromisoformat(now) if isinstance(now, str) else self._utcnow()
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
            existing = self._check_idempotency(
                conn, user_id, idempotency_key, "plan_tomorrow", payload, recompute_when_stale=True
            )
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
