"""Daily recovery metrics, workout facts and training plan operations."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from typing import Any

from ..errors import ConflictError, SafetyRestrictedError, ValidationError
from ..models import (
    CompleteWorkoutInput,
    ConfirmProgressionInput,
    DailyMetricsInput,
    GetTrainingPlanInput,
    LogDailyMetricsInput,
    LogWorkoutInput,
    SubstituteExerciseInput,
)
from .base import _MALFORMED_RECORD_ERRORS, ServiceCore
from .catalog import EXERCISE_CATALOG, RED_FLAG_KEYWORDS


class TrainingMixin(ServiceCore):
    """Daily recovery metrics, workout facts and training plan operations."""

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
                except _MALFORMED_RECORD_ERRORS:
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

            recovery_score = max(0, min(100, round(score)))
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
                avail_eq = [*list(orig_def["equipment"]), "bodyweight"]

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
                    "equipment": next(iter(defn["equipment"])),
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
