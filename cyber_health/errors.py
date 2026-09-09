class CyberHealthError(Exception):
    """Base error carrying a stable public error code."""

    code = "INTERNAL_ERROR"


class ConflictError(CyberHealthError):
    code = "CONFLICT_VERSION"


class StoreBusyError(CyberHealthError):
    code = "STORE_BUSY"


class ValidationError(CyberHealthError, ValueError):
    code = "VALIDATION_ERROR"


class IdempotencyMismatchError(CyberHealthError):
    code = "IDEMPOTENCY_MISMATCH"


class SafetyRestrictedError(CyberHealthError):
    code = "SAFETY_RESTRICTED"
