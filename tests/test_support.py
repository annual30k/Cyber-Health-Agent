"""Cross-platform helpers shared by the isolated host-CLI test fixtures."""

from __future__ import annotations

import os
from pathlib import Path
import sys


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
