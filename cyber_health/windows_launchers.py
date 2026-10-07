"""Let a package upgrade replace console-script launchers that Windows has locked.

Windows refuses to delete or overwrite a running ``.exe`` (``cyber-health.exe`` while it
runs the updater itself, ``cyber-health-mcp.exe`` while a host keeps the server alive) but
does allow renaming it. Before installing, our launchers are moved aside; on failure they
are moved back so the venv never loses its commands, and leftovers are deleted later once
nothing runs them anymore. Every function is a no-op on other platforms.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

LAUNCHER_GLOB = "cyber-health*.exe"
ASIDE_MARKER = ".old-"


def move_aside_launchers(scripts_dir: Path) -> list[tuple[Path, Path]]:
    """Rename our launchers to ``<name>.exe.old-<stamp>``; returns (original, aside) pairs."""
    if sys.platform != "win32" or not scripts_dir.is_dir():
        return []
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    moved: list[tuple[Path, Path]] = []
    for launcher in sorted(scripts_dir.glob(LAUNCHER_GLOB)):
        if not launcher.is_file():
            continue
        aside = launcher.with_name(f"{launcher.name}{ASIDE_MARKER}{stamp}")
        try:
            launcher.rename(aside)
        except OSError:
            restore_launchers(moved)
            raise
        moved.append((launcher, aside))
    return moved


def restore_launchers(moved: list[tuple[Path, Path]]) -> None:
    """Undo ``move_aside_launchers`` for every launcher the install did not recreate."""
    for original, aside in reversed(moved):
        if not original.exists() and aside.exists():
            aside.rename(original)


def remove_stale_launchers(scripts_dir: Path) -> list[str]:
    """Delete moved-aside launchers that are no longer running; locked ones wait for next time."""
    if sys.platform != "win32" or not scripts_dir.is_dir():
        return []
    removed: list[str] = []
    for stale in sorted(scripts_dir.glob(f"{LAUNCHER_GLOB}{ASIDE_MARKER}*")):
        try:
            stale.unlink()
        except OSError:
            continue
        removed.append(stale.name)
    return removed
