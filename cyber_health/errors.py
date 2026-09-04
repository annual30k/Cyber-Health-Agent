class CyberHealthError(Exception):
    """Base error carrying a stable public error code."""

    code = "INTERNAL_ERROR"


class ConflictError(CyberHealthError):
    code = "CONFLICT_VERSION"


class StoreBusyError(CyberHealthError):
    code = "STORE_BUSY"

