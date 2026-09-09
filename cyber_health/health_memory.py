"""OpenClaw health-manager memory configuration discovery.

This module only inspects the host configuration.  It does not read or write
the Vault contents; the configured provider owns that boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


HEALTH_MANAGER_AGENT_ID = "health-manager"
OBSIDIAN_MEMORY_PLUGIN_ID = "obsidian-memory-plugin"
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass
class HealthManagerMemoryStatus:
    """Host-side readiness for the health-manager long-term memory bridge."""

    state: str = "unavailable"  # connected, unconfigured, invalid, unavailable
    plugin_id: str = OBSIDIAN_MEMORY_PLUGIN_ID
    plugin_loaded: bool = False
    agent_id: str = HEALTH_MANAGER_AGENT_ID
    vault_path: str | None = None
    project_id: str | None = None
    project_path: str | None = None
    provider_args: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"connected": self.connected}


def _run_json(openclaw_bin: str, args: list[str], env: dict[str, str]) -> tuple[dict[str, Any] | None, str]:
    try:
        result = subprocess.run(
            [openclaw_bin, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
            check=False,
        )
    except Exception as exc:
        return None, f"OpenClaw command failed: {exc}"
    if result.returncode != 0:
        return None, (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
    try:
        value = json.loads(result.stdout)
    except Exception as exc:
        return None, f"OpenClaw returned invalid JSON: {exc}"
    if not isinstance(value, dict):
        return None, "OpenClaw returned a non-object JSON value"
    return value, ""


def _path_has_symlink(path: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _inside(path: Path, parent: Path) -> bool:
    try:
        return parent.resolve() in path.resolve().parents
    except OSError:
        return False


def _project_declared(vault: Path, project_id: str) -> bool:
    projects_file = vault / "00-System" / "projects.yaml"
    if not projects_file.is_file():
        return False
    try:
        text = projects_file.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(rf"(?m)^\s*- id:\s*{re.escape(project_id)}\s*$", text))


def inspect_health_manager_memory(
    openclaw_bin: str | None,
    env: dict[str, str] | None = None,
    *,
    agent_id: str = HEALTH_MANAGER_AGENT_ID,
) -> HealthManagerMemoryStatus:
    """Inspect the configured OpenClaw health-manager memory connection.

    A missing or invalid connection is reported as a warning state so core
    health facts can still be installed.  The installer must never claim that
    long-term memory is connected unless every boundary is verified.
    """

    status = HealthManagerMemoryStatus(agent_id=agent_id)
    if not openclaw_bin:
        status.reason = "OpenClaw executable was not found; long-term memory was not inspected."
        status.warnings.append(status.reason)
        return status

    command_env = dict(env or os.environ)
    config, error = _run_json(
        openclaw_bin,
        ["config", "get", "plugins.entries.obsidian-memory-plugin", "--json"],
        command_env,
    )
    if config is None:
        status.state = "unconfigured"
        status.reason = "obsidian-memory-plugin configuration could not be read."
        status.warnings.append(f"{status.reason} {error}")
        return status

    plugin_config = config.get("config") if isinstance(config.get("config"), dict) else {}
    agent_configs = plugin_config.get("agentConfigs") if isinstance(plugin_config.get("agentConfigs"), dict) else {}
    health_config = agent_configs.get(agent_id) if isinstance(agent_configs.get(agent_id), dict) else None

    enabled = config.get("enabled") is True
    if not enabled:
        status.state = "unconfigured"
        status.reason = "obsidian-memory-plugin exists but is not enabled."
        status.warnings.append(status.reason)
        return status
    if health_config is None:
        status.state = "unconfigured"
        status.reason = f"obsidian-memory-plugin has no configuration for agent '{agent_id}'."
        status.warnings.append(status.reason)
        return status

    status.vault_path = health_config.get("vaultPath")
    status.project_id = health_config.get("projectId")
    if not isinstance(status.vault_path, str) or not Path(status.vault_path).is_absolute():
        status.state = "invalid"
        status.reason = "health-manager vaultPath is missing or not absolute."
        status.warnings.append(status.reason)
        return status
    if not isinstance(status.project_id, str) or not PROJECT_ID_RE.fullmatch(status.project_id):
        status.state = "invalid"
        status.reason = "health-manager projectId is missing or invalid."
        status.warnings.append(status.reason)
        return status

    vault = Path(status.vault_path)
    project = vault / "20-Projects" / status.project_id
    status.project_path = str(project)
    if _path_has_symlink(vault) or _path_has_symlink(project):
        status.state = "invalid"
        status.reason = "Configured health-manager Vault/project path contains a symlink."
        status.warnings.append(status.reason)
        return status
    if not vault.is_dir() or not os.access(vault, os.R_OK):
        status.state = "invalid"
        status.reason = f"Configured Vault is missing or unreadable: {vault}"
        status.warnings.append(status.reason)
        return status
    if not _inside(project, vault) or not project.is_dir() or not os.access(project, os.R_OK):
        status.state = "invalid"
        status.reason = f"Configured health-manager project is missing or outside the Vault: {project}"
        status.warnings.append(status.reason)
        return status
    if not _project_declared(vault, status.project_id):
        status.state = "invalid"
        status.reason = f"Project '{status.project_id}' is not declared in {vault / '00-System/projects.yaml'}"
        status.warnings.append(status.reason)
        return status

    plugin_runtime, plugin_error = _run_json(
        openclaw_bin,
        ["plugins", "inspect", OBSIDIAN_MEMORY_PLUGIN_ID, "--runtime", "--json"],
        command_env,
    )
    runtime_plugin = plugin_runtime.get("plugin") if isinstance(plugin_runtime, dict) else None
    runtime_status = runtime_plugin.get("status") if isinstance(runtime_plugin, dict) else plugin_runtime.get("status") if isinstance(plugin_runtime, dict) else None
    if plugin_runtime is None or runtime_status not in {"loaded", "active"}:
        status.state = "unconfigured"
        status.reason = "health-manager memory paths are valid, but obsidian-memory-plugin is not loaded."
        status.warnings.append(f"{status.reason} {plugin_error}".strip())
        return status

    status.plugin_loaded = True
    status.state = "connected"
    status.reason = "health-manager Obsidian Memory configuration verified."
    status.provider_args = [
        "--memory-provider",
        "obsidian",
        "--memory-vault",
        str(vault),
        "--memory-project-id",
        status.project_id,
    ]
    return status
