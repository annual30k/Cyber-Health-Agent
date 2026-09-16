"""Safe Codex MCP registration support for Cyber Health Agent.

Cyber Health remains an independent Python Core + stdio MCP server.  This
module only manages its single, fixed Codex MCP registration and never edits
unrelated Codex configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
from typing import Any

FIXED_CODEX_SERVER_NAME = "cyber-health"
CODEX_ABSENT_MARKER = "No MCP server named 'cyber-health' found."


def find_codex_cli() -> str | None:
    """Return a plausibly named Codex executable for automatic discovery."""
    candidate = shutil.which("codex")
    if candidate and Path(candidate).name.lower() in ("codex", "codex.exe"):
        return candidate
    return None


@dataclass
class CodexRegistrationStatus:
    name: str = FIXED_CODEX_SERVER_NAME
    detected: bool = False
    ownership_proven: bool = False
    action: str = "none"  # none, register, update, remove, skip, refused, error
    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    reason: str = ""
    executed: bool = False
    state_fingerprint: str = ""


def _has_symlink_in_path(path: Path) -> bool:
    current = path
    while True:
        try:
            if current.is_symlink():
                return True
        except (OSError, ValueError):
            pass
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _verify_command_signature(command: str, args: list[str]) -> bool:
    if not command:
        return False
    name = Path(command).name.lower()
    if name in ("cyber-health-mcp", "cyber-health-mcp.exe", "cyber-health-mcp.cmd"):
        return True
    if name.startswith("python"):
        return any(
            value == "-m" and index + 1 < len(args) and args[index + 1] == "cyber_health_mcp"
            for index, value in enumerate(args)
        )
    return False


def codex_env(codex_home: Path | str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return env


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _transport(raw_data: dict[str, Any]) -> tuple[str, list[str], str]:
    transport = raw_data.get("transport")
    if not isinstance(transport, dict) or transport.get("type") != "stdio":
        return "", [], ""
    command = str(transport.get("command") or "")
    raw_args = transport.get("args")
    args = [str(value) for value in raw_args] if isinstance(raw_args, list) else []
    cwd = str(transport.get("cwd") or "")
    return command, args, cwd


def _is_bound_to_installation(
    command: str,
    args: list[str],
    cwd: str,
    target_dir: Path,
    db_path: Path,
) -> bool:
    target = target_dir.resolve()
    database = db_path.resolve()

    def under_target(value: str) -> bool:
        if not value:
            return False
        raw = Path(value)
        if _has_symlink_in_path(raw):
            return False
        resolved = raw.resolve()
        return resolved == target or target in resolved.parents

    if under_target(command) or under_target(cwd):
        return True
    for index, value in enumerate(args):
        if value == "--db" and index + 1 < len(args):
            raw_db = Path(args[index + 1])
            if _has_symlink_in_path(raw_db):
                return False
            resolved_db = raw_db.resolve()
            return resolved_db == database or resolved_db == target or target in resolved_db.parents
    return False


def inspect_codex_registration(
    codex_bin: str | None,
    target_dir: Path,
    db_path: Path,
    *,
    codex_home: Path | str | None = None,
) -> CodexRegistrationStatus:
    """Inspect only the fixed Cyber Health Codex entry with sanitized output."""
    status = CodexRegistrationStatus()
    if not codex_bin:
        status.action = "skip"
        status.reason = "Codex CLI executable not found; registration skipped"
        return status

    try:
        result = subprocess.run(
            [codex_bin, "mcp", "get", FIXED_CODEX_SERVER_NAME, "--json"],
            env=codex_env(codex_home),
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
    except Exception:
        status.action = "error"
        status.reason = "Failed to invoke Codex CLI inspection executable"
        return status

    combined = (result.stderr + " " + result.stdout).strip()
    if result.returncode != 0:
        if CODEX_ABSENT_MARKER in combined:
            status.action = "none"
            status.reason = "Not registered in Codex (clean state / no-op)"
            return status
        status.detected = True
        status.action = "error"
        status.reason = f"Codex CLI inspection failed with exit code {result.returncode}"
        return status

    status.detected = True
    try:
        raw_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        status.action = "error"
        status.reason = "Codex CLI inspection returned malformed JSON"
        return status
    if not isinstance(raw_data, dict):
        status.action = "error"
        status.reason = "Codex CLI inspection returned invalid response structure"
        return status

    command, args, cwd = _transport(raw_data)
    status.command = command
    status.args = args
    status.cwd = cwd
    status.state_fingerprint = _fingerprint(raw_data)
    status.ownership_proven = _verify_command_signature(command, args) and _is_bound_to_installation(
        command, args, cwd, target_dir, db_path
    )
    if status.ownership_proven:
        status.action = "update"
        status.reason = "Verified Cyber Health Codex MCP registration ownership"
    else:
        status.action = "refused"
        status.reason = (
            "Foreign or ambiguous Codex MCP entry named 'cyber-health'; "
            "refusing to overwrite"
        )
    return status


def plan_codex_registration(
    codex_bin: str | None,
    target_dir: Path,
    db_path: Path,
    command: str,
    args: list[str],
    cwd: str,
    *,
    codex_home: Path | str | None = None,
    skip: bool = False,
) -> CodexRegistrationStatus:
    if skip:
        return CodexRegistrationStatus(action="skip", reason="Codex registration skipped by request")
    status = inspect_codex_registration(
        codex_bin, target_dir, db_path, codex_home=codex_home
    )
    desired = (command, args, cwd)
    if status.action == "none":
        status.action = "register"
        status.reason = "Codex MCP server not registered; planned new registration"
    elif status.action == "update" and (status.command, status.args, status.cwd) == desired:
        status.reason = "Codex MCP server already matches the target registration"
    if status.action in ("register", "update"):
        status.command = command
        status.args = list(args)
        status.cwd = cwd
    return status


def apply_codex_registration(
    codex_bin: str | None,
    status: CodexRegistrationStatus,
    *,
    codex_home: Path | str | None = None,
    dry_run: bool = False,
) -> None:
    if status.action not in ("register", "update") or dry_run or not codex_bin:
        return
    command = [
        codex_bin,
        "mcp",
        "add",
        FIXED_CODEX_SERVER_NAME,
        "--",
        status.command,
        *status.args,
    ]
    result = subprocess.run(
        command,
        env=codex_env(codex_home),
        capture_output=True,
        text=True,
        timeout=20,
        shell=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to register Codex MCP server {FIXED_CODEX_SERVER_NAME} "
            f"(CLI code {result.returncode})"
        )
    status.executed = True
    status.reason = "Successfully registered Cyber Health MCP server in Codex"


def remove_codex_registration(
    codex_bin: str | None,
    status: CodexRegistrationStatus,
    target_dir: Path,
    db_path: Path,
    *,
    codex_home: Path | str | None = None,
    dry_run: bool = False,
) -> None:
    if status.action != "remove" or dry_run or not codex_bin:
        return
    fresh = inspect_codex_registration(
        codex_bin, target_dir, db_path, codex_home=codex_home
    )
    if (
        fresh.action != "update"
        or not fresh.ownership_proven
        or fresh.state_fingerprint != status.state_fingerprint
    ):
        raise RuntimeError(
            "Codex registration changed or ownership was no longer proven before removal"
        )
    result = subprocess.run(
        [codex_bin, "mcp", "remove", FIXED_CODEX_SERVER_NAME],
        env=codex_env(codex_home),
        capture_output=True,
        text=True,
        timeout=20,
        shell=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to remove Codex MCP server {FIXED_CODEX_SERVER_NAME} "
            f"(CLI code {result.returncode})"
        )
    status.executed = True
    status.reason = "Successfully removed Cyber Health MCP server from Codex"
