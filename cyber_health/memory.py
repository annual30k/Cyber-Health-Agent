"""Replaceable memory boundary. Never accesses an Obsidian Vault directly."""

from typing import Any, Protocol


class MemoryUnavailable(Exception):
    pass


class MemoryProvider(Protocol):
    """Providers must bound their own IO and deduplicate writes by intent_id.

    user_id must be mapped to an explicitly configured private memory scope.
    A provider, not this core, owns candidate/Raw/Wiki lifecycle and consent.
    """

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class UnavailableMemoryProvider:
    def __init__(self, reason: str = "No MemoryProvider is connected"):
        self.reason = reason

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise MemoryUnavailable(self.reason)
