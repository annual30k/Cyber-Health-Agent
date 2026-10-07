"""Cross-platform helpers shared by the isolated host-CLI test fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

# Fixed instant for tests that hard-code calendar dates; 12:00 in Asia/Shanghai.
FIXED_NOW = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)


def fixed_clock(now: datetime = FIXED_NOW) -> Callable[[], datetime]:
    """Return a service clock frozen at ``now`` so date-sensitive rules never expire."""
    return lambda: now


def make_python_command(directory: Path, name: str, script: str) -> Path:
    """Write an executable fake CLI, using a cmd shim on Windows."""
    if not script.startswith("#!"):
        script = f"#!/usr/bin/env python3\n{script}"
    script_path = directory / f"{name}.py"
    script_path.write_text(script, encoding="utf-8")

    if os.name == "nt":
        command = directory / f"{name}.cmd"
        command.write_text(
            f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n',
            encoding="utf-8",
        )
        return command

    command = directory / name
    command.write_text(script, encoding="utf-8")
    command.chmod(0o755)
    return command


def isolate_host_clis(test: unittest.TestCase, home: Path, *modules: str) -> None:
    """Hide the developer's real Codex/Hermes CLIs and config homes from a test.

    Auto-discovery in the given ``cyber_health`` modules returns ``None`` and
    ``CODEX_HOME``/``HERMES_HOME`` point into ``home``, so a test that does not pass
    explicit fake binaries can never inspect or mutate the real host registrations.
    """
    env = mock.patch.dict(
        os.environ,
        {"CODEX_HOME": str(home / "codex-home"), "HERMES_HOME": str(home / "hermes-home")},
    )
    env.start()
    test.addCleanup(env.stop)
    for module in modules:
        for finder in ("find_codex_cli", "find_hermes_cli"):
            patcher = mock.patch(f"cyber_health.{module}.{finder}", return_value=None)
            patcher.start()
            test.addCleanup(patcher.stop)


def runnable_cli(name: str) -> str | None:
    """Return the path of a host CLI on PATH only if ``<cli> --version`` actually runs.

    Opt-in live tests skip on a missing or broken installation (for example an npm
    wrapper whose vendored binary is absent) instead of reporting a product failure.
    """
    path = shutil.which(name)
    if not path:
        return None
    try:
        probe = subprocess.run([path, "--version"], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if probe.returncode == 0 else None


# The single identity every fact is stored under (see cyber_health.store.SINGLE_USER_ID).
OWNER = "owner"
