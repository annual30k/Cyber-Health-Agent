"""Daily reminder schedule events and their lifecycle."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..errors import ValidationError
from ..models import AcknowledgeScheduleInput, ScheduleDailyRemindersInput
from .base import _INVALID_ZONE_ERRORS, _MALFORMED_RECORD_ERRORS, OWNER_ID, ServiceCore


class ScheduleMixin(ServiceCore):
    """Daily reminder schedule events and their lifecycle."""

    def get_schedule(
        self,
        date: str | None = None,
        now: datetime | None = None,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        now_dt = now or self._utcnow()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        now_utc = now_dt.astimezone(UTC)

        with self.store.connect() as conn:
            conn.execute("BEGIN")
            try:
                profile_row = conn.execute(
                    """SELECT timezone, goals_json, constraints_json, safety_mode, state_version
                       FROM user_profile WHERE user_id = ?""",
                    (OWNER_ID,),
                ).fetchone()
                tz_name = profile_row["timezone"] if profile_row else "Asia/Shanghai"
                try:
                    user_tz = ZoneInfo(tz_name)
                except _INVALID_ZONE_ERRORS:
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
                        (OWNER_ID,),
                    ).fetchall()
                else:
                    all_rows = conn.execute(
                        """SELECT event_id, event_type, window_start, window_end, status,
                                  revision, delivery_attempts, prompt_hint, created_at, updated_at
                           FROM schedule_event
                           WHERE user_id = ? AND status IN ('pending', 'overdue')
                           ORDER BY window_start ASC""",
                        (OWNER_ID,),
                    ).fetchall()

                results: list[dict[str, Any]] = []
                for r in all_rows:
                    item = dict(r)
                    end_dt = datetime.fromisoformat(item["window_end"])
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
                    if date and effective_status != "overdue" and item_day != date:
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
                            start_dt = datetime.fromisoformat(item["window_start"])
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
                                (OWNER_ID, item_day),
                            ).fetchone()
                            if plan_row:
                                p_status = plan_row["status"]
                                try:
                                    p_body = json.loads(plan_row["body_json"])
                                    if p_body.get("status") == "committed":
                                        p_status = "committed"
                                except _MALFORMED_RECORD_ERRORS:
                                    pass
                                if p_status == "committed":
                                    eligible = False
                                    suppression_reason = "morning_plan_already_committed"

                        elif trigger_condition == "lunch_not_logged":
                            lunch_rows = conn.execute(
                                """SELECT meal_id, occurred_at FROM meal_log
                                   WHERE user_id = ? AND meal_type = 'lunch' AND status = 'active'""",
                                (OWNER_ID,),
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
                                (OWNER_ID,),
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
                                    (OWNER_ID, item_day),
                                ).fetchall()
                                completed = False
                                for wr in wo_rows:
                                    try:
                                        wb = json.loads(wr["body_json"]) if wr["body_json"] else {}
                                        cr = wb.get("completion_rate")
                                        if cr is not None and float(cr) >= 1.0:
                                            completed = True
                                            break
                                    except _MALFORMED_RECORD_ERRORS:
                                        pass

                                if completed:
                                    eligible = False
                                    suppression_reason = "workout_already_completed"
                                else:
                                    plan_row = conn.execute(
                                        """SELECT body_json FROM domain_record
                                           WHERE user_id = ? AND kind = 'plan' AND day = ? AND status NOT IN ('superseded', 'deleted')
                                           ORDER BY created_at DESC LIMIT 1""",
                                        (OWNER_ID, item_day),
                                    ).fetchone()
                                    if plan_row:
                                        try:
                                            p_body = json.loads(plan_row["body_json"])
                                            wp = p_body.get("workout_plan")
                                            if (isinstance(wp, str) and ("休息" in wp or "休整" in wp or "rest" in wp.lower())) or (isinstance(wp, dict) and (wp.get("is_rest_day") or wp.get("type") == "rest")):
                                                eligible = False
                                                suppression_reason = "scheduled_rest_day"
                                        except _MALFORMED_RECORD_ERRORS:
                                            pass
                                    if eligible and (constraints.get("is_rest_day") or goals.get("is_rest_day")):
                                        eligible = False
                                        suppression_reason = "scheduled_rest_day"

                        elif trigger_condition == "review_pending":
                            rev_row = conn.execute(
                                """SELECT record_id FROM domain_record
                                   WHERE user_id = ? AND kind = 'daily_review' AND day = ? AND status = 'active'
                                   LIMIT 1""",
                                (OWNER_ID, item_day),
                            ).fetchone()
                            if rev_row:
                                eligible = False
                                suppression_reason = "daily_review_already_completed"

                        item["eligible"] = eligible
                        item["suppression_reason"] = suppression_reason

                    results.append(item)

                conn.execute("COMMIT")
                return results
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def schedule_daily_reminders(
        self,
        *,
        date: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Generate deterministic standard schedule events for a specific day."""
        try:
            ScheduleDailyRemindersInput(date=date, idempotency_key=idempotency_key)
        except Exception as err:
            raise ValidationError(str(err)) from err

        payload = {"action": "schedule_daily_reminders", "user_id": OWNER_ID, "date": date}
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, idempotency_key, "schedule_daily_reminders", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, now)
            row = conn.execute("SELECT timezone, state_version FROM user_profile WHERE user_id = ?", (OWNER_ID,)).fetchone()
            before_version = row["state_version"]
            tz_name = row["timezone"]

            try:
                user_tz = ZoneInfo(tz_name)
            except _INVALID_ZONE_ERRORS:
                user_tz = ZoneInfo("Asia/Shanghai")

            standard_windows = [
                ("MORNING_PLAN", "07:30:00", "08:30:00", "晨间唤醒：记录体重与昨晚睡眠，锁定今日执行计划", "morning_plan_not_locked", f"sched_{OWNER_ID}_{date}_morning_plan"),
                ("MEAL_CHECK", "13:00:00", "14:00:00", "午餐核验：询问就餐与饥饿感，提示水分补充", "lunch_not_logged", f"sched_{OWNER_ID}_{date}_meal_check_lunch"),
                ("WORKOUT_REMINDER", "17:30:00", "18:30:00", "训练窗口临近：推送最低可完成版本或热身提示", "workout_pending", f"sched_{OWNER_ID}_{date}_workout_reminder"),
                ("MEAL_CHECK", "19:30:00", "20:30:00", "晚餐核验：询问就餐与饥饿感，提示摄入控制", "dinner_not_logged", f"sched_{OWNER_ID}_{date}_meal_check_dinner"),
                ("DAILY_REVIEW", "21:30:00", "22:30:00", "晚间对账：复盘全天摄入与运动，生成次日预案", "review_pending", f"sched_{OWNER_ID}_{date}_daily_review"),
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
                        (ev_id, OWNER_ID, ev_type, w_start, w_end, hint, now, now),
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
                (after_version, now, OWNER_ID),
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
        event_id: str,
        action: str = "acknowledged",
        idempotency_key: str,
        new_window_start: str | None = None,
        new_window_end: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        try:
            AcknowledgeScheduleInput(
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
            "user_id": OWNER_ID,
            "event_id": event_id,
            "event_action": action,
            "new_window_start": new_window_start,
            "new_window_end": new_window_end,
            "note": note,
        }
        now, operation_id = self._now(), f"op_{uuid.uuid4().hex}"

        with self.store.transaction() as conn:
            existing = self._check_idempotency(conn, idempotency_key, "update_schedule_event", payload)
            if existing:
                return existing

            self._ensure_profile_in_tx(conn, now)
            profile = conn.execute("SELECT state_version FROM user_profile WHERE user_id = ?", (OWNER_ID,)).fetchone()
            before_version = profile["state_version"]

            target = conn.execute(
                "SELECT event_type, window_start, window_end, status, revision, delivery_attempts FROM schedule_event WHERE event_id = ? AND user_id = ?",
                (event_id, OWNER_ID),
            ).fetchone()
            if not target:
                raise ValidationError(f"Schedule event '{event_id}' not found for user '{OWNER_ID}'")

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
                (after_version, now, OWNER_ID),
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
        event_id: str,
        action: str = "acknowledged",
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.update_schedule_event(
            event_id=event_id,
            action=action,
            idempotency_key=idempotency_key,
        )
