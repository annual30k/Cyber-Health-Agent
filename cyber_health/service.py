"""Host-neutral domain operations for Cyber Health Core.

Enforces strict Pydantic v2 input validation, optimistic concurrency version checks,
canonical SHA-256 idempotency hashing, timezone-aware calendar aggregation,
pure read snapshots, safety red flag invariants, and out-of-lock memory drainage.

The operations are grouped by domain in ``cyber_health.domain``; this module composes
them into the single public ``CyberHealthService``.
"""

from __future__ import annotations

from .domain.base import DEFAULT_IDEMPOTENCY_REPLAY_WINDOW
from .domain.catalog import EXERCISE_CATALOG, RED_FLAG_KEYWORDS
from .domain.knowledge import KnowledgeMixin
from .domain.memory_ops import MemoryOpsMixin
from .domain.nutrition import NutritionMixin
from .domain.profile import ProfileMixin
from .domain.progress import ProgressMixin
from .domain.records import RecordsMixin
from .domain.review import ReviewMixin
from .domain.safety import SafetyRecoveryEvaluation
from .domain.schedule import ScheduleMixin
from .domain.training import TrainingMixin
from .domain.training_rules import TrainingRulesMixin
from .errors import (
    ConflictError,
    IdempotencyMismatchError,
    SafetyRestrictedError,
    StoreBusyError,
    ValidationError,
)


class CyberHealthService(
    ProfileMixin,
    NutritionMixin,
    TrainingMixin,
    TrainingRulesMixin,
    ScheduleMixin,
    ReviewMixin,
    ProgressMixin,
    RecordsMixin,
    KnowledgeMixin,
    MemoryOpsMixin,
):
    """Host-neutral health facts service used by the MCP adapter, CLI and installers."""


__all__ = [
    "DEFAULT_IDEMPOTENCY_REPLAY_WINDOW",
    "EXERCISE_CATALOG",
    "RED_FLAG_KEYWORDS",
    "ConflictError",
    "CyberHealthService",
    "IdempotencyMismatchError",
    "SafetyRecoveryEvaluation",
    "SafetyRestrictedError",
    "StoreBusyError",
    "ValidationError",
]
