"""Cyber Health domain core."""

__version__ = "0.2.6"

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
    if name == "CyberHealthInstaller":
        from .install import CyberHealthInstaller

        return CyberHealthInstaller
    if name == "CyberHealthUpdater":
        from .update import CyberHealthUpdater

        return CyberHealthUpdater
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CyberHealthError",
    "CyberHealthService",
    "CyberHealthInstaller",
    "CyberHealthUpdater",
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
