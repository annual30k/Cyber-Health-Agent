"""Cyber Health domain core."""

__version__ = "0.2.4"

from .errors import (
    ConflictError,
    CyberHealthError,
    IdempotencyMismatchError,
    SafetyRestrictedError,
    StoreBusyError,
    ValidationError,
)
from .memory import MemoryProvider, MemoryUnavailable, UnavailableMemoryProvider
from .service import CyberHealthService


def __getattr__(name: str):
    if name == "CyberHealthUninstaller":
        from .uninstall import CyberHealthUninstaller

        return CyberHealthUninstaller
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CyberHealthError",
    "CyberHealthService",
    "CyberHealthUninstaller",
    "ConflictError",
    "IdempotencyMismatchError",
    "SafetyRestrictedError",
    "StoreBusyError",
    "ValidationError",
    "MemoryProvider",
    "MemoryUnavailable",
    "UnavailableMemoryProvider",
]
