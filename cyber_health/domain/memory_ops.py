"""Long-term memory outbox, maintenance, recall and pattern suggestions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..errors import IdempotencyMismatchError, ValidationError
from ..memory import MemoryUnavailable
from ..models import GetMemorySuggestionsInput, MaintainMemoryInput, MemoryActionInput, ProposeMemoryInput, QueryMemoryInput
from .base import _INVALID_ZONE_ERRORS, _MALFORMED_RECORD_ERRORS, ServiceCore


class MemoryOpsMixin(ServiceCore):
    """Long-term memory outbox, maintenance, recall and pattern suggestions."""

    def _calculate_maintenance_due(
        self,
        conn: Any,
        user_id: str,
        now_dt: datetime | None = None,
        day: str | None = None,
        prune_days: int = 30,
    ) -> dict[str, Any]:
        """Strictly pure-read evaluation of maintenance due work across outbox, leases, and TTL facts."""
        now_dt = now_dt or self._utcnow()
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
                hasher.update(f"{it['intent_id']}:{it['attempts']}:{it['status']};".encode())
            hasher.update(f"ttl:{ttl_meals_count}:{ttl_records_count}:{ttl_sent_count}".encode())
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
        now_dt = self._utcnow()
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
            # Intent ids are derived from the key, so a memory key is never recycled.
            existing = self._check_idempotency(
                conn, user_id, idempotency_key, "propose_memory_candidate", op_payload, allow_key_reuse=False
            )
            if existing:
                return existing

            row = conn.execute(
                "SELECT operation_id, request_hash, result_status FROM operation_log WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key),
            ).fetchone()
            if row and row["request_hash"] != request_hash:
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
        except Exception:  # noqa: BLE001 - external memory adapter; any failure defers the intent
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

        # The validated top-level flag is authoritative, even if payload supplied a different value.
        p["confirmed"] = validated.confirmed
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
        now_dt = self._utcnow()
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
            except Exception:  # noqa: BLE001 - external memory adapter; any failure defers the intent
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
            user_tz_name = profile["timezone"] if (profile and "timezone" in profile) else "Asia/Shanghai"
            try:
                user_tz = ZoneInfo(user_tz_name)
            except _INVALID_ZONE_ERRORS:
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
                        dt = datetime.fromisoformat(occ)
                        if dt.tzinfo is None:
                            local_dt = dt.replace(tzinfo=user_tz)
                        else:
                            local_dt = dt.astimezone(user_tz)
                    except (TypeError, ValueError):
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
                    recorded_dates = sorted(days_dict.keys())
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
                        except _MALFORMED_RECORD_ERRORS:
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
                    except _MALFORMED_RECORD_ERRORS:
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
                except _MALFORMED_RECORD_ERRORS:
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
                    except _MALFORMED_RECORD_ERRORS:
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
                    conf_status = raw_status or "unconfirmed"

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
        except Exception as exc:  # noqa: BLE001 - external memory adapter; any failure defers the intent
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
            norm = MemoryOpsMixin._memory_name_key(raw)
            if norm and raw:
                result.setdefault(norm, raw)
        return sorted(result.values(), key=lambda item: MemoryOpsMixin._memory_name_key(item))

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
                created_at = datetime.fromisoformat(str(row["created_at"]))
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
        except _INVALID_ZONE_ERRORS:
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
                created_at = datetime.fromisoformat(str(row["created_at"]))
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
        now = self._utcnow()

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
            q_start = (start_day - timedelta(days=2)).isoformat()
            q_end = (end_day + timedelta(days=2)).isoformat()
            meal_rows = conn.execute(
                """SELECT meal_id, occurred_at, meal_type, foods_json
                   FROM meal_log
                   WHERE user_id = ? AND occurred_at >= ? AND occurred_at < ? AND status = 'active'
                   ORDER BY occurred_at ASC, meal_id ASC""",
                (validated.user_id, q_start, q_end),
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
