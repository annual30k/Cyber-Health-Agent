"""Safety, recovery, progression and prescription rules behind training plans."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from .base import _MALFORMED_RECORD_ERRORS, ServiceCore
from .catalog import EXERCISE_CATALOG
from .safety import SafetyRecoveryEvaluation


class TrainingRulesMixin(ServiceCore):
    """Safety, recovery, progression and prescription rules behind training plans."""

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
            except _MALFORMED_RECORD_ERRORS:
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
            except _MALFORMED_RECORD_ERRORS:
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
            except (TypeError, ValueError):
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
                                recovery_score = round(float(raw_rs))
                            except (ValueError, TypeError):
                                recovery_score = None

                        sleep_quality = metrics.get("sleep_quality") or d_body.get("sleep_quality")

                        raw_tr = d_body.get("triggered_rules")
                        if isinstance(raw_tr, list):
                            triggered_rules = [str(r) for r in raw_tr]

                        coaching_alert = d_body.get("coaching_alert")
                except _MALFORMED_RECORD_ERRORS:
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
            except _MALFORMED_RECORD_ERRORS:
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

        evidence_ids = sorted(set(s1["record_ids"] + s2["record_ids"]))
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
