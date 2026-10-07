"""Canonical safety/recovery evaluation shared by plans, prescriptions and progression."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import SafetyRestrictedError
from .catalog import EXERCISE_CATALOG


@dataclass
class SafetyRecoveryEvaluation:
    """Canonical assessment of safety restrictions, deload state, and recovery evidence."""
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
