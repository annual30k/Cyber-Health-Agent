"""Pydantic v2 schemas and models for Cyber Health Agent.

Enforces strict field types, range constraints, real calendar dates,
valid timezone identifiers, and extra forbidden checks.
"""

from __future__ import annotations

import math
import base64
import binascii
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _check_real_date(v: str) -> str:
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except Exception as err:
        raise ValueError(f"'{v}' is not a valid calendar date (expected YYYY-MM-DD)") from err
    return v


def _check_iso_instant(v: str) -> str:
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception as err:
        raise ValueError(f"'{v}' is not a valid ISO 8601 timestamp") from err
    return v


class AmountRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: float = Field(..., ge=0)
    high: float = Field(..., ge=0)

    @field_validator("low", "high")
    @classmethod
    def check_finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("Amount values must be finite")
        return v

    @model_validator(mode="after")
    def validate_range(self) -> AmountRange:
        if self.high < self.low:
            raise ValueError("AmountRange high cannot be less than low")
        return self


class FoodItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    amount_g: AmountRange | None = None
    notes: str | None = None


class LogMealInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    occurred_at: str = Field(...)
    meal_type: str = Field(..., min_length=1)
    foods: list[FoodItem] = Field(default_factory=list)
    kcal_low: int = Field(ge=0)
    kcal_high: int = Field(ge=0)
    protein_low: int = Field(default=0, ge=0)
    protein_high: int = Field(default=0, ge=0)
    idempotency_key: str = Field(..., min_length=1)
    target_meal_id: str | None = None
    expected_state_version: int | None = None
    repeat_meal: str | None = None
    source: str | None = None
    confidence: str | None = None
    correction_reason: str | None = None
    user_confirmed: bool | None = None

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, v: str) -> str:
        return _check_iso_instant(v)

    @model_validator(mode="after")
    def validate_ranges_and_correction(self) -> LogMealInput:
        if self.kcal_high < self.kcal_low:
            raise ValueError("kcal_high cannot be less than kcal_low")
        if self.protein_high < self.protein_low:
            raise ValueError("protein_high cannot be less than protein_low")
        if self.target_meal_id and self.expected_state_version is None:
            raise ValueError("expected_state_version is required when target_meal_id is specified")
        return self


class DeleteMealInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    meal_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    reason: str | None = None
    expected_state_version: int = Field(..., ge=0)


class DailyMetricsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight_kg: float | None = Field(default=None, ge=20.0, le=500.0)
    sleep_hours: float | None = Field(default=None, ge=0.0, le=24.0)
    sleep_quality: str | None = None
    fatigue_level: int | None = Field(default=None, ge=1, le=10)
    soreness_locations: list[str] = Field(default_factory=list)
    steps: int | None = Field(default=None, ge=0)
    notes: str | None = None


class LogDailyMetricsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    date: str = Field(...)
    metrics: DailyMetricsInput
    idempotency_key: str = Field(..., min_length=1)
    expected_state_version: int | None = None

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return _check_real_date(v)


class ProfileGoals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_kcal_low: int | None = Field(default=None, ge=500, le=10000)
    target_kcal_high: int | None = Field(default=None, ge=500, le=10000)
    target_protein_low: int | None = Field(default=None, ge=0, le=500)
    target_protein_high: int | None = Field(default=None, ge=0, le=500)
    target_weight_kg: float | None = Field(default=None, ge=20.0, le=500.0)
    target_timeframe_weeks: int | None = Field(default=None, ge=1, le=520)
    goal: str | None = None
    goal_type: str | None = None
    activity_level: str | None = None
    experience_level: str | None = None
    training_experience: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def check_ranges(self) -> ProfileGoals:
        if self.target_kcal_low is not None and self.target_kcal_high is not None:
            if self.target_kcal_high < self.target_kcal_low:
                raise ValueError("target_kcal_high cannot be less than target_kcal_low")
        if self.target_protein_low is not None and self.target_protein_high is not None:
            if self.target_protein_high < self.target_protein_low:
                raise ValueError("target_protein_high cannot be less than target_protein_low")
        return self


class UpdateProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    goals: ProfileGoals | dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None
    timezone: str | None = None
    safety_flags: list[str] | None = None
    clear_safety_flags: bool = False
    clearance_reason: str | None = None
    expected_state_version: int | None = None

    @field_validator("goals")
    @classmethod
    def validate_goals(cls, v: Any) -> Any:
        if v is not None:
            if isinstance(v, dict):
                return ProfileGoals(**v).model_dump()
            elif isinstance(v, ProfileGoals):
                return v.model_dump()
        return v

    @field_validator("timezone")
    @classmethod
    def validate_tz(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ZoneInfo(v)
            except Exception as err:
                raise ValueError(f"Invalid timezone '{v}'") from err
        return v


class ActivitySummaryInput(BaseModel):
    """Facts extracted from a wearable, fitness screenshot, or user report."""

    model_config = ConfigDict(extra="forbid")

    activity_type: str = Field(..., min_length=1, max_length=120)
    duration_min: float | None = Field(default=None, ge=0.0, le=1440.0)
    distance_km: float | None = Field(default=None, ge=0.0, le=2000.0)
    active_kcal: int | None = Field(default=None, ge=0, le=20000)
    total_kcal: int | None = Field(default=None, ge=0, le=20000)
    avg_heart_rate_bpm: int | None = Field(default=None, ge=20, le=300)
    avg_pace_seconds_per_km: int | None = Field(default=None, ge=0, le=14400)
    exertion: str | None = Field(default=None, max_length=80)
    source: str = Field(default="user_report", min_length=1, max_length=80)
    user_confirmed: bool = True

    @model_validator(mode="after")
    def validate_energy_range(self) -> ActivitySummaryInput:
        if self.active_kcal is not None and self.total_kcal is not None and self.total_kcal < self.active_kcal:
            raise ValueError("total_kcal cannot be less than active_kcal")
        return self


class SourceImageInput(BaseModel):
    """Original image supplied by the user, retained inside the local fact store."""

    model_config = ConfigDict(extra="forbid")

    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    data_base64: str = Field(..., min_length=1, max_length=16_777_216)
    filename: str | None = Field(default=None, max_length=255)

    @field_validator("data_base64")
    @classmethod
    def validate_base64(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as err:
            raise ValueError("source_image.data_base64 must be valid base64") from err
        if not raw:
            raise ValueError("source_image.data_base64 cannot be empty")
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError("source_image must not exceed 12 MiB")
        return value


class LogWorkoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    session_id: str | None = None
    date: str = Field(...)
    planned_exercises: list[str] = Field(default_factory=list)
    actual_sets: list[dict[str, Any]] = Field(default_factory=list)
    rpe_avg: float | None = Field(default=None, ge=1.0, le=10.0)
    discomfort_notes: str | None = None
    completion_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    activity_summary: ActivitySummaryInput | None = None
    source_image: SourceImageInput | None = None
    expected_state_version: int | None = None

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return _check_real_date(v)


class DailyReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    date: str = Field(...)
    idempotency_key: str = Field(..., min_length=1)
    user_notes: str | None = None
    expected_state_version: int | None = None

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return _check_real_date(v)


class PlanTomorrowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    date: str = Field(...)
    idempotency_key: str = Field(..., min_length=1)
    commit: bool = False
    expected_state_version: int | None = None

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return _check_real_date(v)


class AcknowledgeScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    event_id: str = Field(..., min_length=1)
    action: Literal["delivered", "acknowledged", "skipped", "cancelled", "postponed"] = "acknowledged"
    idempotency_key: str = Field(..., min_length=1)
    new_window_start: str | None = None
    new_window_end: str | None = None
    note: str | None = None

    @field_validator("new_window_start", "new_window_end")
    @classmethod
    def validate_optional_instant(cls, v: str | None) -> str | None:
        if v is not None:
            return _check_iso_instant(v)
        return v

    @model_validator(mode="after")
    def validate_postpone(self) -> AcknowledgeScheduleInput:
        if self.action == "postponed" and (not self.new_window_start or not self.new_window_end):
            raise ValueError("new_window_start and new_window_end are required when action is postponed")
        return self


class GetTrainingPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    date: str = Field(...)
    equipment: list[str] = Field(default_factory=list)
    target_duration_min: int = Field(default=45, ge=10, le=180)
    evidence_window_days: int = Field(default=1, ge=0, le=14)

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return _check_real_date(v)


class ConfirmProgressionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    exercise_name: str = Field(..., min_length=1)
    confirmed_weight_kg: float | None = Field(default=None, ge=0.0, le=1000.0)
    confirmed_reps: int | None = Field(default=None, ge=1, le=500)
    proposal_id: str | None = None
    idempotency_key: str = Field(..., min_length=1)
    source_record_ids: list[str] = Field(default_factory=list)
    increment_kg: float | None = Field(default=None, ge=0.0, le=50.0)
    increment_reps: int | None = Field(default=None, ge=1, le=50)
    user_note: str | None = None
    expected_state_version: int | None = None

    @model_validator(mode="after")
    def validate_load_or_reps(self) -> ConfirmProgressionInput:
        if self.confirmed_weight_kg is None and self.confirmed_reps is None:
            raise ValueError("At least one of confirmed_weight_kg or confirmed_reps must be provided")
        return self



class SubstituteExerciseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    original_exercise: str = Field(..., min_length=1)
    equipment: list[str] = Field(default_factory=list)
    discomfort_joint: str | None = None
    reason: str | None = None


class CompleteWorkoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    date: str = Field(...)
    idempotency_key: str = Field(..., min_length=1)
    completed_exercises: list[dict[str, Any]] = Field(default_factory=list)
    session_rpe: float | None = Field(default=None, ge=1.0, le=10.0)
    discomfort_notes: str | None = None
    completion_rate: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return _check_real_date(v)


class QueryKnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1)
    category: str | None = None


class ExportDataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)


class ImportDataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    data: dict[str, Any] = Field(...)
    idempotency_key: str = Field(..., min_length=1)


class MaintainMemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    prune_days: int = Field(default=30, ge=1)
    idempotency_key: str = Field(..., min_length=1)


VALID_MEMORY_ACTIONS = {"propose", "confirm", "reject", "update", "delete", "action"}


class ProposeMemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    method: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(..., min_length=1)

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        norm = v.removeprefix("memory.")
        if norm not in VALID_MEMORY_ACTIONS:
            raise ValueError(f"Invalid memory method '{v}'. Allowed actions: {sorted(VALID_MEMORY_ACTIONS)}")
        return v

    @model_validator(mode="after")
    def validate_payload(self) -> ProposeMemoryInput:
        norm = self.method.removeprefix("memory.")
        p = self.payload or {}
        cid = p.get("candidate_id")
        is_confirmed = p.get("confirmed") is True

        if norm == "confirm":
            if not cid:
                raise ValueError("Memory action 'confirm' requires non-empty 'candidate_id'")
            if not is_confirmed:
                raise ValueError("Memory action 'confirm' requires explicit confirmation ('confirmed=True')")
        elif norm == "delete":
            target = cid or p.get("target_note_path")
            if not target:
                raise ValueError("Memory action 'delete' requires non-empty 'candidate_id' or 'target_note_path'")
            if not is_confirmed:
                raise ValueError("Memory action 'delete' requires explicit confirmation ('confirmed=True')")
        elif norm == "reject":
            if not cid:
                raise ValueError("Memory action 'reject' requires non-empty 'candidate_id'")
        elif norm == "propose":
            if not p and not cid:
                raise ValueError("Memory action 'propose' requires non-empty payload containing rule or content")
        elif norm == "action":
            target = cid or p.get("target_note_path")
            if not target and not p:
                raise ValueError("Memory action 'action' requires non-empty payload or target identifier")
        return self


class MemoryActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    action_type: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    candidate_id: str | None = None
    target_note_path: str | None = None
    confirmed: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action_type")
    @classmethod
    def validate_action(cls, v: str) -> str:
        norm = v.removeprefix("memory.")
        if norm not in VALID_MEMORY_ACTIONS:
            raise ValueError(f"Invalid memory action '{v}'. Allowed actions: {sorted(VALID_MEMORY_ACTIONS)}")
        return norm

    @model_validator(mode="after")
    def validate_action_payload(self) -> MemoryActionInput:
        act = self.action_type
        p = self.payload or {}
        cid = self.candidate_id or p.get("candidate_id")
        target = self.target_note_path or p.get("target_note_path")
        is_confirmed = self.confirmed or p.get("confirmed") is True

        if act == "confirm":
            if not cid:
                raise ValueError("Memory action 'confirm' requires non-empty 'candidate_id'")
            if not is_confirmed:
                raise ValueError("Memory action 'confirm' requires explicit confirmation ('confirmed=True')")
        elif act == "delete":
            del_target = cid or target
            if not del_target:
                raise ValueError("Memory action 'delete' requires non-empty 'candidate_id' or 'target_note_path'")
            if not is_confirmed:
                raise ValueError("Memory action 'delete' requires explicit confirmation ('confirmed=True')")
        elif act == "reject":
            if not cid:
                raise ValueError("Memory action 'reject' requires non-empty 'candidate_id'")
        elif act == "propose":
            if not p and not cid:
                raise ValueError("Memory action 'propose' requires non-empty payload containing rule or content")
        elif act == "action":
            if not cid and not target and not p:
                raise ValueError("Memory action 'action' requires non-empty payload or target identifier")
        return self


class GetRemainingCaloriesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    date: str = Field(...)

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return _check_real_date(v)


class ScheduleDailyRemindersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    date: str = Field(...)
    idempotency_key: str = Field(..., min_length=1)

    @field_validator("date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        return _check_real_date(v)


class QueryMemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1)
    limit: int = Field(default=10, ge=1, le=50)


class UnifiedResponse(BaseModel):
    operation_id: str
    status: Literal["success", "partial", "failed"]
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    state_version: int
