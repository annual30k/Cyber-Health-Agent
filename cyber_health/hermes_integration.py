"""Safe native Hermes Agent MCP registration for Cyber Health."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import yaml


FIXED_HERMES_SERVER_NAME = "cyber-health"


@dataclass
class HermesRegistrationStatus:
    name: str = FIXED_HERMES_SERVER_NAME
    detected: bool = False
    ownership_proven: bool = False
    action: str = "none"  # none, register, update, verify, remove, skip, refused, error
    command: str = ""
    args: list[str] = field(default_factory=list)
    enabled: bool = True
    reason: str = ""
    executed: bool = False
    probe_verified: bool = False
    tool_count: int | None = None
    state_fingerprint: str = ""


def find_hermes_cli() -> str | None:
    candidate = shutil.which("hermes")
    if candidate and Path(candidate).name.lower() in ("hermes", "hermes.exe"):
        return candidate
    return None


def hermes_env(hermes_home: Path | str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    return env


def resolve_hermes_home(hermes_home: Path | str | None = None) -> Path:
    if hermes_home is not None:
        return Path(hermes_home).expanduser().resolve()
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".hermes").resolve()


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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


def _recognized_command(command: str, args: list[str]) -> bool:
    if not command:
        return False
    name = Path(command).name.lower()
    if name in ("cyber-health-mcp", "cyber-health-mcp.exe", "cyber-health-mcp.cmd"):
        return True
    return name.startswith("python") and any(
        value == "-m" and index + 1 < len(args) and args[index + 1] == "cyber_health_mcp"
        for index, value in enumerate(args)
    )


def _bound_to_installation(
    command: str, args: list[str], target_dir: Path, db_path: Path
) -> bool:
    target = target_dir.resolve()
    database = db_path.resolve()
    raw_command = Path(command)
    if not _has_symlink_in_path(raw_command):
        resolved_command = raw_command.resolve()
        if target in resolved_command.parents:
            return True
    for index, value in enumerate(args):
        if value == "--db" and index + 1 < len(args):
            raw_db = Path(args[index + 1])
            if _has_symlink_in_path(raw_db):
                return False
            resolved_db = raw_db.resolve()
            return resolved_db == database or target in resolved_db.parents
    return False


def inspect_hermes_registration(
    hermes_bin: str | None,
    target_dir: Path,
    db_path: Path,
    *,
    hermes_home: Path | str | None = None,
) -> HermesRegistrationStatus:
    """Read only the fixed Hermes entry; never expose unrelated config or env."""
    status = HermesRegistrationStatus()
    if not hermes_bin:
        status.action = "skip"
        status.reason = "Hermes CLI executable not found; registration skipped"
        return status

    config_file = resolve_hermes_home(hermes_home) / "config.yaml"
    if not config_file.exists():
        status.action = "none"
        status.reason = "Hermes config does not exist; registration is absent"
        return status
    if not config_file.is_file() or _has_symlink_in_path(config_file):
        status.action = "error"
        status.reason = "Hermes config is not a safe regular file"
        return status
    try:
        raw_config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    except Exception:
        status.action = "error"
        status.reason = "Hermes config contains invalid YAML"
        return status
    if not isinstance(raw_config, dict):
        status.action = "error"
        status.reason = "Hermes config has an invalid root structure"
        return status
    servers = raw_config.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        status.action = "error"
        status.reason = "Hermes mcp_servers configuration is not a mapping"
        return status
    raw_entry = servers.get(FIXED_HERMES_SERVER_NAME)
    if raw_entry is None:
        status.action = "none"
        status.reason = "Not registered in Hermes (clean state / no-op)"
        return status
    status.detected = True
    if not isinstance(raw_entry, dict):
        status.action = "error"
        status.reason = "Hermes cyber-health MCP entry has an invalid structure"
        return status
    command = str(raw_entry.get("command") or "")
    raw_args = raw_entry.get("args")
    args = [str(item) for item in raw_args] if isinstance(raw_args, list) else []
    status.command = command
    status.args = args
    status.enabled = raw_entry.get("enabled", True) is not False
    status.state_fingerprint = _fingerprint(raw_entry)
    status.ownership_proven = _recognized_command(command, args) and _bound_to_installation(
        command, args, target_dir, db_path
    )
    if status.ownership_proven:
        status.action = "verify"
        status.reason = "Verified Cyber Health Hermes MCP registration ownership"
    else:
        status.action = "refused"
        status.reason = (
            "Foreign or ambiguous Hermes MCP entry named 'cyber-health'; refusing to overwrite"
        )
    return status


def plan_hermes_registration(
    hermes_bin: str | None,
    target_dir: Path,
    db_path: Path,
    command: str,
    args: list[str],
    *,
    hermes_home: Path | str | None = None,
    skip: bool = False,
) -> HermesRegistrationStatus:
    if skip:
        return HermesRegistrationStatus(
            action="skip", reason="Hermes registration skipped by request"
        )
    status = inspect_hermes_registration(
        hermes_bin, target_dir, db_path, hermes_home=hermes_home
    )
    if status.action == "none":
        status.action = "register"
        status.reason = "Hermes MCP server not registered; planned native registration"
    elif status.action == "verify" and (
        (status.command, status.args) != (command, args) or not status.enabled
    ):
        status.action = "update"
        status.reason = "Owned Hermes registration differs or is disabled; planned update"
    if status.action in ("register", "update", "verify"):
        status.command = command
        status.args = list(args)
    return status


def _probe_hermes(
    hermes_bin: str, *, hermes_home: Path | str | None = None
) -> tuple[bool, int | None]:
    try:
        result = subprocess.run(
            [hermes_bin, "mcp", "test", FIXED_HERMES_SERVER_NAME],
            env=hermes_env(hermes_home),
            capture_output=True,
            text=True,
            timeout=45,
            shell=False,
        )
    except Exception:
        return False, None
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"Tools discovered:\s*(\d+)", output)
    count = int(match.group(1)) if match else None
    required = "cyber_health_get_profile" in output and "cyber_health_health_check" in output
    return result.returncode == 0 and required and bool(count and count >= 7), count


def apply_hermes_registration(
    hermes_bin: str | None,
    status: HermesRegistrationStatus,
    target_dir: Path,
    db_path: Path,
    *,
    hermes_home: Path | str | None = None,
    dry_run: bool = False,
) -> None:
    if status.action not in ("register", "update", "verify") or dry_run or not hermes_bin:
        return
    if status.action in ("register", "update"):
        answers = "y\ny\n" if status.action == "update" else "y\n"
        result = subprocess.run(
            [
                hermes_bin,
                "mcp",
                "add",
                FIXED_HERMES_SERVER_NAME,
                "--command",
                status.command,
                "--args",
                *status.args,
            ],
            env=hermes_env(hermes_home),
            input=answers,
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to register Hermes MCP server {FIXED_HERMES_SERVER_NAME} "
                f"(CLI code {result.returncode})"
            )
        fresh = inspect_hermes_registration(
            hermes_bin, target_dir, db_path, hermes_home=hermes_home
        )
        if (
            not fresh.ownership_proven
            or fresh.command != status.command
            or fresh.args != status.args
            or not fresh.enabled
        ):
            raise RuntimeError("Hermes MCP registration did not persist the expected owned state")
    verified, count = _probe_hermes(hermes_bin, hermes_home=hermes_home)
    status.tool_count = count
    if not verified:
        raise RuntimeError("Hermes MCP connection probe did not discover required Cyber Health tools")
    status.executed = True
    status.probe_verified = True
    status.reason = f"Hermes connected and discovered {count} Cyber Health tools"


def remove_hermes_registration(
    hermes_bin: str | None,
    status: HermesRegistrationStatus,
    target_dir: Path,
    db_path: Path,
    *,
    hermes_home: Path | str | None = None,
    dry_run: bool = False,
) -> None:
    if status.action != "remove" or dry_run or not hermes_bin:
        return
    fresh = inspect_hermes_registration(
        hermes_bin, target_dir, db_path, hermes_home=hermes_home
    )
    if (
        fresh.action != "verify"
        or not fresh.ownership_proven
        or fresh.state_fingerprint != status.state_fingerprint
    ):
        raise RuntimeError("Hermes registration changed or ownership was lost before removal")
    result = subprocess.run(
        [hermes_bin, "mcp", "remove", FIXED_HERMES_SERVER_NAME],
        env=hermes_env(hermes_home),
        input="y\n",
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to remove Hermes MCP server {FIXED_HERMES_SERVER_NAME} "
            f"(CLI code {result.returncode})"
        )
    after = inspect_hermes_registration(
        hermes_bin, target_dir, db_path, hermes_home=hermes_home
    )
    if after.action != "none":
        raise RuntimeError("Hermes MCP registration still exists after removal")
    status.executed = True
    status.reason = "Successfully removed Cyber Health MCP server from Hermes"
