"""Meal facts, daily intake totals and calorie/protein target gaps."""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta
from typing import Any

from ..errors import ConflictError, ValidationError
from ..models import DeleteMealInput, GetRemainingCaloriesInput, LogMealInput
from .base import ServiceCore


class NutritionMixin(ServiceCore):
    """Meal facts, daily intake totals and calorie/protein target gaps."""

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
        try:
            day_dt = datetime.strptime(day, "%Y-%m-%d")
            w_start = (day_dt - timedelta(days=2)).strftime("%Y-%m-%d")
            w_end = (day_dt + timedelta(days=2)).strftime("%Y-%m-%d")
            rows = conn.execute(
                """SELECT meal_id, occurred_at, kcal_low, kcal_high, protein_low, protein_high
                   FROM meal_log
                   WHERE user_id = ? AND occurred_at >= ? AND occurred_at < ? AND status = 'active'""",
                (user_id, w_start, w_end),
            ).fetchall()
        except ValueError:
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

    def get_today(self, user_id: str, day: str, now: datetime | None = None) -> dict[str, Any]:
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except Exception as err:
            raise ValidationError(f"Invalid date format: {day}") from err

        now_dt = now or self._utcnow()

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
                res = {
                    "operation_id": f"op_read_{uuid.uuid4().hex[:12]}",
                    "status": "success",
                    "data": data,
                    "warnings": [],
                    "error": None,
                    "state_version": version,
                    **data,
                }
                conn.execute("COMMIT")
                return res
            except Exception:
                conn.execute("ROLLBACK")
                raise

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
            "source": source,
            "confidence": confidence,
            "correction_reason": correction_reason,
            "user_confirmed": user_confirmed,
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
