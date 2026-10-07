"""Audit trail, health check, export and verified import of facts."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from ..errors import ConflictError, ValidationError
from ..models import ExportDataInput, ImportDataInput, _check_iso_instant, _check_real_date
from .base import _INVALID_ZONE_ERRORS, _MALFORMED_RECORD_ERRORS, ServiceCore


class RecordsMixin(ServiceCore):
    """Audit trail, health check, export and verified import of facts."""

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
            except Exception:  # noqa: BLE001 - external memory adapter; any failure defers the intent
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

            incoming_user = data.get("user_id")
            legacy_remap = (user_id == "owner" and (incoming_user in ("u_default", "default") or not incoming_user))
            if incoming_user and incoming_user != user_id and not legacy_remap:
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

                p_user = imported_profile.get("user_id")
                if p_user and p_user != user_id and not (user_id == "owner" and p_user in ("u_default", "default")):
                    raise ValidationError(
                        f"Profile user_id '{imported_profile['user_id']}' does not match target user '{user_id}'"
                    )

                # Timezone validation
                tz = imported_profile.get("timezone", profile["timezone"])
                if not isinstance(tz, str) or not tz.strip():
                    raise ValidationError("timezone must be a non-empty string")
                try:
                    ZoneInfo(tz)
                except _INVALID_ZONE_ERRORS as err:
                    raise ValidationError(f"Invalid timezone '{tz}': {err}") from err

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
                    except (TypeError, ValueError) as err:
                        raise ValidationError(f"Malformed goals_json: {err}") from err
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
                    except (TypeError, ValueError) as err:
                        raise ValidationError(f"Malformed constraints_json: {err}") from err
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
                    except (TypeError, ValueError) as err:
                        raise ValidationError(f"Malformed safety_flags_json: {err}") from err
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
                        except (TypeError, ValueError) as err:
                            raise ValidationError(f"Invalid deload_until format: {err}") from err

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
                if curr_deload and (not du or du < curr_deload):
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
                m_user = m.get("user_id")
                if m_user and m_user != user_id and not (user_id == "owner" and m_user in ("u_default", "default")):
                    raise ValidationError(f"Meal record '{mid}' user_id does not match import user '{user_id}'")

                occurred_at = m.get("occurred_at")
                if not occurred_at or not isinstance(occurred_at, str):
                    raise ValidationError(f"Meal record '{mid}' missing occurred_at timestamp")
                try:
                    _check_iso_instant(occurred_at)
                except (TypeError, ValueError) as err:
                    raise ValidationError(f"Meal record '{mid}' invalid occurred_at: {err}") from err

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
                except (TypeError, ValueError) as err:
                    raise ValidationError(f"Malformed foods_json in meal '{mid}': {err}") from err

                m_status = m.get("status", "active")
                if m_status not in {"active", "deleted", "superseded"}:
                    raise ValidationError(f"Invalid status '{m_status}' for meal '{mid}'")

                ex = conn.execute("SELECT * FROM meal_log WHERE meal_id = ?", (mid,)).fetchone()
                if ex:
                    foods_match = True
                    try:
                        foods_match = json.loads(ex["foods_json"]) == parsed_foods
                    except _MALFORMED_RECORD_ERRORS:
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
                d_user = d.get("user_id")
                if d_user and d_user != user_id and not (user_id == "owner" and d_user in ("u_default", "default")):
                    raise ValidationError(f"Domain record '{rid}' user_id does not match import user '{user_id}'")

                kind = d.get("kind")
                day = d.get("day")
                if not kind or not day or not isinstance(kind, str) or not isinstance(day, str):
                    raise ValidationError(f"Domain record '{rid}' must have non-empty string 'kind' and 'day'")
                try:
                    _check_real_date(day)
                except (TypeError, ValueError) as err:
                    raise ValidationError(f"Domain record '{rid}' invalid day: {err}") from err

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
                except (TypeError, ValueError) as err:
                    raise ValidationError(f"Malformed body_json in domain record '{rid}': {err}") from err

                ex = conn.execute("SELECT * FROM domain_record WHERE record_id = ?", (rid,)).fetchone()
                if ex:
                    body_match = True
                    try:
                        body_match = json.loads(ex["body_json"]) == parsed_body
                    except _MALFORMED_RECORD_ERRORS:
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
                s_user = s.get("user_id")
                if s_user and s_user != user_id and not (user_id == "owner" and s_user in ("u_default", "default")):
                    raise ValidationError(f"Schedule event '{sid}' user_id does not match import user '{user_id}'")

                ev_type = s.get("event_type")
                w_start = s.get("window_start")
                w_end = s.get("window_end")
                if not ev_type or not w_start or not w_end:
                    raise ValidationError(f"Schedule event '{sid}' must have non-empty event_type, window_start, and window_end")
                try:
                    _check_iso_instant(w_start)
                    _check_iso_instant(w_end)
                except (TypeError, ValueError) as err:
                    raise ValidationError(f"Schedule event '{sid}' invalid window instant: {err}") from err

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
                    except (TypeError, ValueError) as err:
                        raise ValidationError(f"Malformed response_json in operation '{opid}': {err}") from err

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
