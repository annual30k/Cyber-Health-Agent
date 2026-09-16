"""Explicit, fail-closed first-run bootstrap for Cyber Health long-term memory.

The user opts in by supplying one physical Obsidian Vault path.  This module
only creates Cyber Health's missing project unit and merges its one OpenClaw
agent connection.  It never discovers a Vault, rewrites another project's
binding, or reads notes outside the managed paths.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable

import yaml

from .health_memory import (
    HEALTH_MANAGER_AGENT_ID,
    OBSIDIAN_MEMORY_PLUGIN_ID,
    PROJECT_ID_RE,
    HealthManagerMemoryStatus,
    inspect_health_manager_memory,
)


class MemoryBootstrapError(RuntimeError):
    """Raised when an opted-in memory bootstrap cannot safely continue."""


def _has_symlink_in_path(path: Path) -> bool:
    current = path
    while True:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        if current.parent == current:
            return False
        current = current.parent


def _inside(path: Path, parent: Path) -> bool:
    try:
        return parent.resolve() == path.resolve() or parent.resolve() in path.resolve().parents
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _project_id(vault: Path) -> str:
    suffix = hashlib.sha256(str(vault.resolve()).encode("utf-8")).hexdigest()[:8]
    # This non-code project has no filesystem root. Its stable, product-specific
    # ID is the boundary that keeps Cyber Health memory separate from every
    # other agent's project.
    return f"Cyber-Health-Agent-{suffix}"


def find_obsidian_application() -> Path | None:
    """Find a locally installed Obsidian application without invoking it.

    The memory provider uses the Vault filesystem, but guided onboarding promises
    a user-visible Obsidian Vault. The caller can therefore ask the user to
    install Obsidian before this bootstrap changes a Vault or a shared plugin.
    """
    override = os.environ.get("CYBER_HEALTH_OBSIDIAN_APP")
    if override:
        candidate = Path(override).expanduser()
        if candidate.exists() and not candidate.is_symlink():
            return candidate.resolve()
        return None
    if sys.platform == "darwin":
        for candidate in (Path("/Applications/Obsidian.app"), Path.home() / "Applications" / "Obsidian.app"):
            if candidate.is_dir() and not candidate.is_symlink():
                return candidate.resolve()
        return None
    if sys.platform == "win32":
        roots = (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"))
        for root in filter(None, roots):
            candidate = Path(root) / "Obsidian" / "Obsidian.exe"
            if candidate.is_file() and not candidate.is_symlink():
                return candidate.resolve()
        return None
    executable = shutil.which("obsidian")
    if executable:
        candidate = Path(executable)
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve()
    return None


ROOT_AGENTS = """# Obsidian Memory Workspace\n\nThis Vault stores private, source-backed memory. Inbox is pending evidence; Raw is immutable evidence; Wiki is maintained synthesis.\n\nProject work is restricted to a selected project under `20-Projects/`. Do not scan unrelated projects, overwrite existing project bindings, or ingest pending evidence without an explicit user request.\n"""

PROJECT_AGENTS = """---\ntype: project-schema\nproject_id: {{project_id}}\nproject_root: null\nschema_state: pending-first-ingest\n---\n\n# {{project_name}} memory contract\n\nInherit the Vault root `AGENTS.md`. Keep this project's evidence private. Local rules cannot weaken capture, ingest, privacy, or source boundaries.\n\n## Starter categories\n\n| Path | Purpose | Page type |\n| --- | --- | --- |\n| wiki/decisions/ | Durable evidence-backed choices | decision |\n| wiki/pitfalls/ | Verified causes and prevention | pitfall |\n| wiki/knowledge/ | Reusable synthesis | knowledge |\n| checkpoints/ | Meaningful unfinished work | checkpoint |\n\nOnly a user-triggered ingest may promote Inbox evidence into Raw and Wiki.\n\n[[20-Projects/{{project_id}}/index|Index]] · [[20-Projects/{{project_id}}/log|Log]]\n"""

PROJECT_RULES = """---\ntype: project-rules\nproject_id: {{project_id}}\n---\n\n# {{project_name}} rules\n\n## User-confirmed scope\n\nHealth-related long-term memory for the configured Cyber Health agent.\n\n## Stable conventions\n\n## Project-specific exclusions\n\nDo not retain credentials or automatically ingest routine execution.\n"""

PROJECT_INDEX = """---\ntype: memory-index\nproject_id: {{project_id}}\n---\n\n# {{project_name}} memory index\n\n[[20-Projects/{{project_id}}/AGENTS|Project schema]] · [[20-Projects/{{project_id}}/log|Log]]\n\n## Decisions\n\n## Pitfalls\n\n## Knowledge\n\n## Checkpoints\n"""

PROJECT_LOG = """---\ntype: memory-log\nproject_id: {{project_id}}\n---\n\n# {{project_name}} memory log\n\nAppend-only. A user-triggered ingest records dated evidence and verified destinations here.\n"""


@dataclass
class MemoryBootstrapStatus:
    requested: bool = False
    vault_path: str | None = None
    project_id: str | None = None
    project_path: str | None = None
    plugin_action: str = "none"  # verify, install, error
    obsidian_action: str = "none"  # verify, install-required, error
    obsidian_app_path: str | None = None
    vault_action: str = "none"  # verify, create, append, error
    config_action: str = "none"  # verify, configure, error
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    executed: bool = False
    state_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryBootstrapper:
    """Plan and apply the single-Vault, single-agent bootstrap transaction."""

    def __init__(
        self,
        *,
        vault_path: str | Path | None,
        project_root: Path,
        openclaw_bin: str | None,
        openclaw_env: dict[str, str],
        plugin_archive: Path,
        project_id: str | None = None,
        dry_run: bool = False,
        obsidian_application_finder: Callable[[], Path | None] = find_obsidian_application,
    ) -> None:
        self.requested = vault_path is not None
        self.vault_input = Path(vault_path).expanduser() if vault_path is not None else None
        self.project_root = project_root.resolve()
        self.openclaw_bin = openclaw_bin
        self.openclaw_env = dict(openclaw_env)
        self.plugin_archive = plugin_archive
        self.requested_project_id = project_id
        self.dry_run = dry_run
        self.obsidian_application_finder = obsidian_application_finder
        self.vault: Path | None = None
        self.projects_file: Path | None = None
        self.project: Path | None = None
        self._projects_fingerprint = ""
        self._entry_before: dict[str, Any] | None = None
        self._entry_before_fingerprint = ""
        self._entry_after: dict[str, Any] | None = None

    def _run(self, args: list[str], *, input_text: str | None = None) -> tuple[int, str, str]:
        if not self.openclaw_bin:
            return 127, "", "OpenClaw CLI executable not found"
        try:
            result = subprocess.run(
                [self.openclaw_bin, *args],
                env=self.openclaw_env,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=45,
                shell=False,
            )
        except Exception as exc:
            return 127, "", f"OpenClaw command failed: {exc.__class__.__name__}"
        return result.returncode, result.stdout, result.stderr

    def _read_plugin_entry(self) -> tuple[dict[str, Any] | None, bool, str]:
        code, stdout, stderr = self._run(
            ["config", "get", f"plugins.entries.{OBSIDIAN_MEMORY_PLUGIN_ID}", "--json"]
        )
        if code != 0:
            if "unset" in f"{stdout} {stderr}".lower():
                return None, True, ""
            return None, False, "OpenClaw plugin configuration could not be read"
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError:
            return None, False, "OpenClaw plugin configuration returned invalid JSON"
        if not isinstance(value, dict):
            return None, False, "OpenClaw plugin configuration is not an object"
        return value, False, ""

    def _plugin_installed(self) -> tuple[bool, str]:
        code, stdout, stderr = self._run(
            ["plugins", "inspect", OBSIDIAN_MEMORY_PLUGIN_ID, "--runtime", "--json"]
        )
        if code == 0:
            try:
                value = json.loads(stdout)
            except json.JSONDecodeError:
                return False, "OpenClaw plugin inspection returned invalid JSON"
            plugin = value.get("plugin") if isinstance(value, dict) else None
            if isinstance(plugin, dict) and plugin.get("id") == OBSIDIAN_MEMORY_PLUGIN_ID:
                return True, ""
            return False, "OpenClaw plugin inspection did not identify obsidian-memory-plugin"
        output = f"{stdout} {stderr}".lower()
        if "not found" in output or "unknown plugin" in output or "not installed" in output:
            return False, ""
        return False, "OpenClaw plugin inspection failed"

    def _validate_vault(self, status: MemoryBootstrapStatus) -> bool:
        if not self.vault_input or not self.vault_input.is_absolute():
            status.reason = "--memory-vault must be an absolute, user-selected Vault path"
            return False
        if _has_symlink_in_path(self.vault_input):
            status.reason = "Selected Vault path contains a symlink"
            return False
        if not self.vault_input.is_dir() or not os.access(self.vault_input, os.R_OK | os.X_OK):
            status.reason = "Selected Vault is missing or unreadable"
            return False
        self.vault = self.vault_input.resolve()
        self.projects_file = self.vault / "00-System" / "projects.yaml"
        status.vault_path = str(self.vault)
        return True

    def _validate_obsidian_application(self, status: MemoryBootstrapStatus) -> bool:
        try:
            application = self.obsidian_application_finder()
        except Exception:
            application = None
        if application is None:
            status.obsidian_action = "install-required"
            status.reason = (
                "Obsidian is not installed. Install Obsidian, create or open the selected Vault, "
                "then rerun the opted-in long-term memory setup."
            )
            return False
        status.obsidian_action = "verify"
        status.obsidian_app_path = str(application)
        return True

    def _load_projects(self, status: MemoryBootstrapStatus) -> tuple[dict[str, Any], bool] | None:
        assert self.projects_file is not None
        if not self.projects_file.exists():
            return {"projects": []}, True
        if not self.projects_file.is_file() or _has_symlink_in_path(self.projects_file):
            status.reason = "Vault projects.yaml is not a safe regular file"
            return None
        try:
            text = self.projects_file.read_text(encoding="utf-8")
            raw = yaml.safe_load(text) or {}
        except Exception:
            status.reason = "Vault projects.yaml contains invalid YAML"
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("projects"), list):
            status.reason = "Vault projects.yaml must contain a projects list"
            return None
        if any(not isinstance(item, dict) for item in raw["projects"]):
            status.reason = "Vault projects.yaml contains an invalid project entry"
            return None
        self._projects_fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return raw, False

    def _existing_agent_project_id(self, entry: dict[str, Any] | None) -> str | None:
        if not entry or not self.vault:
            return None
        config = entry.get("config")
        if not isinstance(config, dict):
            return None
        agents = config.get("agentConfigs")
        candidate = agents.get(HEALTH_MANAGER_AGENT_ID) if isinstance(agents, dict) else None
        if not isinstance(candidate, dict):
            return None
        vault_path = candidate.get("vaultPath")
        project_id = candidate.get("projectId")
        if not isinstance(vault_path, str) or not isinstance(project_id, str):
            return None
        try:
            if Path(vault_path).expanduser().resolve() == self.vault and PROJECT_ID_RE.fullmatch(project_id):
                return project_id
        except OSError:
            return None
        return None

    def _plan_project(self, status: MemoryBootstrapStatus, entry: dict[str, Any] | None) -> bool:
        loaded = self._load_projects(status)
        if loaded is None:
            return False
        projects_doc, registry_missing = loaded
        preferred = self.requested_project_id or self._existing_agent_project_id(entry) or _project_id(self.vault or Path("/"))
        if not PROJECT_ID_RE.fullmatch(preferred):
            status.reason = "--memory-project-id is invalid"
            return False
        status.project_id = preferred
        assert self.vault is not None
        self.project = self.vault / "20-Projects" / preferred
        status.project_path = str(self.project)
        if not _inside(self.project, self.vault) or _has_symlink_in_path(self.project):
            status.reason = "Planned health-manager project path is unsafe"
            return False
        matches = [item for item in projects_doc["projects"] if item.get("id") == preferred]
        if len(matches) > 1:
            status.reason = "Vault projects.yaml contains duplicate health-manager project IDs"
            return False
        if matches:
            roots = matches[0].get("roots", [])
            if roots not in (None, []):
                status.reason = "Existing health-manager project ID belongs to a different project root"
                return False
            status.vault_action = "verify" if self.project.exists() else "create"
        else:
            status.vault_action = "create" if registry_missing else "append"
        if self.project.exists() and not self.project.is_dir():
            status.reason = "Existing health-manager project path is not a directory"
            return False
        return True

    def _merged_plugin_entry(self, entry: dict[str, Any] | None, status: MemoryBootstrapStatus) -> dict[str, Any] | None:
        base = json.loads(json.dumps(entry or {}))
        config = base.get("config", {})
        if not isinstance(config, dict):
            status.reason = "Existing obsidian-memory-plugin config is not an object"
            return None
        if "agentConfigs" in config:
            agents = config.get("agentConfigs")
            if not isinstance(agents, dict):
                status.reason = "Existing obsidian-memory-plugin agentConfigs is not an object"
                return None
        elif config:
            status.reason = "Existing single-agent obsidian-memory-plugin config cannot be safely converted without replacing it"
            return None
        else:
            agents = {}
        expected = {"vaultPath": status.vault_path, "projectId": status.project_id}
        current = agents.get(HEALTH_MANAGER_AGENT_ID)
        if current is not None and (
            not isinstance(current, dict)
            or current.get("vaultPath") != expected["vaultPath"]
            or current.get("projectId") != expected["projectId"]
        ):
            status.reason = "health-manager already has a different Obsidian Memory binding; refusing to overwrite"
            return None
        if current is None:
            agents[HEALTH_MANAGER_AGENT_ID] = expected
        base["enabled"] = True
        hooks = base.get("hooks")
        if hooks is None:
            hooks = {}
        if not isinstance(hooks, dict):
            status.reason = "Existing obsidian-memory-plugin hooks are not an object"
            return None
        hooks = {**hooks, "allowPromptInjection": True, "allowConversationAccess": True}
        base["hooks"] = hooks
        base["config"] = {"agentConfigs": agents}
        return base

    def plan(self) -> MemoryBootstrapStatus:
        status = MemoryBootstrapStatus(requested=self.requested)
        if not self.requested:
            status.reason = "Memory bootstrap was not requested"
            return status
        if not self.openclaw_bin:
            status.reason = "OpenClaw is required to configure the opted-in Obsidian Memory connection"
            status.plugin_action = status.config_action = "error"
            return status
        if not self._validate_vault(status):
            status.vault_action = "error"
            return status
        if not self._validate_obsidian_application(status):
            return status
        installed, plugin_error = self._plugin_installed()
        if plugin_error:
            status.reason = plugin_error
            status.plugin_action = "error"
            return status
        status.plugin_action = "verify" if installed else "install"
        if not installed and (not self.plugin_archive.is_file() or self.plugin_archive.is_symlink()):
            status.reason = "Bundled obsidian-memory-plugin archive is unavailable"
            status.plugin_action = "error"
            return status
        entry, absent, entry_error = self._read_plugin_entry()
        if entry_error:
            status.reason = entry_error
            status.config_action = "error"
            return status
        self._entry_before = entry
        self._entry_before_fingerprint = hashlib.sha256(
            json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest() if entry is not None else ""
        if not self._plan_project(status, entry):
            status.vault_action = "error"
            return status
        merged = self._merged_plugin_entry(entry, status)
        if merged is None:
            status.config_action = "error"
            return status
        self._entry_after = merged
        status.config_action = "configure" if absent or merged != entry else "verify"
        status.state_fingerprint = self._projects_fingerprint
        status.reason = "Memory bootstrap plan validated"
        return status

    def _write_project_registry(self, status: MemoryBootstrapStatus) -> None:
        assert self.projects_file is not None and self.project is not None and status.project_id is not None
        if status.vault_action not in ("create", "append"):
            return
        if self.projects_file.exists():
            text = self.projects_file.read_text(encoding="utf-8")
            fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if fingerprint != self._projects_fingerprint:
                raise MemoryBootstrapError("Vault projects.yaml changed after planning")
            parsed = yaml.safe_load(text) or {}
            if any(item.get("id") == status.project_id for item in parsed.get("projects", [])):
                raise MemoryBootstrapError("health-manager project ID appeared after planning")
            # Appending preserves user comments and all prior project mappings.  We
            # deliberately only support the standard single-key registry shape.
            top_level_after_projects = re.search(r"(?m)^projects:\s*$.*^\S[^#\n]*$", text, re.DOTALL)
            if top_level_after_projects or not re.search(r"(?m)^projects:\s*$", text):
                raise MemoryBootstrapError("Existing projects.yaml cannot be safely appended without rewriting it")
            addition = f"\n  - id: {status.project_id}\n    roots: []\n    scope: private\n"
            _atomic_write(self.projects_file, text.rstrip("\n") + addition)
        else:
            _atomic_write(
                self.projects_file,
                "projects:\n"
                f"  - id: {status.project_id}\n"
                "    roots: []\n"
                "    scope: private\n",
            )

    def _write_project_unit(self, status: MemoryBootstrapStatus) -> None:
        assert self.vault is not None and self.project is not None and status.project_id is not None
        # These are all under the explicitly selected Vault and current project.
        (self.vault / "00-System").mkdir(parents=True, exist_ok=True)
        root_agents = self.vault / "AGENTS.md"
        if not root_agents.exists():
            _atomic_write(root_agents, ROOT_AGENTS)
        for relative in ("inbox/assets", "raw/assets", "wiki/decisions", "wiki/pitfalls", "wiki/knowledge", "checkpoints"):
            (self.project / relative).mkdir(parents=True, exist_ok=True)
        replacements = {"{{project_id}}": status.project_id, "{{project_name}}": "Cyber Health Agent"}
        notes = {
            "AGENTS.md": PROJECT_AGENTS,
            "rules.md": PROJECT_RULES,
            "index.md": PROJECT_INDEX,
            "log.md": PROJECT_LOG,
        }
        for name, template in notes.items():
            target = self.project / name
            if target.exists():
                continue
            value = template
            for old, new in replacements.items():
                value = value.replace(old, new)
            _atomic_write(target, value)

    def _configure_plugin(self, status: MemoryBootstrapStatus) -> None:
        assert self._entry_after is not None
        if status.plugin_action == "install":
            code, _stdout, _stderr = self._run(
                [
                    "plugins",
                    "install",
                    str(self.plugin_archive),
                    "--force",
                    "--accept-capabilities",
                    "--acknowledge-install-policy-warning",
                ]
            )
            if code != 0:
                raise MemoryBootstrapError("OpenClaw could not install bundled obsidian-memory-plugin")
        current, absent, error = self._read_plugin_entry()
        if error:
            raise MemoryBootstrapError(error)
        if status.plugin_action == "install":
            # The native installer may create its own disabled/default entry.
            # Re-read it after installation and merge only our agent binding.
            merged = self._merged_plugin_entry(current, status)
            if merged is None:
                raise MemoryBootstrapError(status.reason)
            self._entry_after = merged
        merged = self._merged_plugin_entry(current, status)
        if merged is None:
            raise MemoryBootstrapError(status.reason)
        config = current.get("config") if isinstance(current, dict) else None
        agents = config.get("agentConfigs") if isinstance(config, dict) else None
        existing_agent = agents.get(HEALTH_MANAGER_AGENT_ID) if isinstance(agents, dict) else None
        expected_agent = {"vaultPath": status.vault_path, "projectId": status.project_id}
        if existing_agent is None:
            command = [
                "config", "set",
                f"plugins.entries.{OBSIDIAN_MEMORY_PLUGIN_ID}.config.agentConfigs.{HEALTH_MANAGER_AGENT_ID}",
                json.dumps(expected_agent, ensure_ascii=False), "--strict-json", "--expect-current-absent",
            ]
            code, _stdout, _stderr = self._run(command)
            if code != 0:
                raise MemoryBootstrapError("OpenClaw rejected the guarded health-manager memory binding")
        for field in ("allowPromptInjection", "allowConversationAccess"):
            current_value = current.get("hooks", {}).get(field) if isinstance(current, dict) and isinstance(current.get("hooks"), dict) else None
            if current_value is True:
                continue
            command = [
                "config", "set", f"plugins.entries.{OBSIDIAN_MEMORY_PLUGIN_ID}.hooks.{field}",
                "true", "--strict-json",
            ]
            command.extend(
                ["--expect-current-json", json.dumps(current_value)]
                if current_value is not None else ["--expect-current-absent"]
            )
            code, _stdout, _stderr = self._run(command)
            if code != 0:
                raise MemoryBootstrapError(f"OpenClaw rejected the guarded {field} permission update")
        enabled = current.get("enabled") if isinstance(current, dict) else None
        if enabled is not True:
            command = ["config", "set", f"plugins.entries.{OBSIDIAN_MEMORY_PLUGIN_ID}.enabled", "true", "--strict-json"]
            command.extend(
                ["--expect-current-json", json.dumps(enabled)]
                if enabled is not None else ["--expect-current-absent"]
            )
            code, _stdout, _stderr = self._run(command)
            if code != 0:
                raise MemoryBootstrapError("OpenClaw rejected the guarded plugin enablement update")

    def apply(self, status: MemoryBootstrapStatus) -> HealthManagerMemoryStatus:
        if not status.requested:
            return HealthManagerMemoryStatus(reason=status.reason)
        if status.obsidian_action == "install-required" or any(
            action == "error" for action in (status.plugin_action, status.vault_action, status.config_action)
        ):
            raise MemoryBootstrapError(status.reason)
        if self.dry_run:
            return HealthManagerMemoryStatus(
                state="planned",
                plugin_loaded=False,
                vault_path=status.vault_path,
                project_id=status.project_id,
                project_path=status.project_path,
                provider_args=[
                    "--memory-provider", "obsidian", "--memory-vault", status.vault_path or "",
                    "--memory-project-id", status.project_id or "",
                ],
                reason="Memory bootstrap planned; dry run made zero changes.",
            )
        self._write_project_registry(status)
        self._write_project_unit(status)
        self._configure_plugin(status)
        verified = inspect_health_manager_memory(self.openclaw_bin, self.openclaw_env)
        if not verified.connected:
            warning = "OpenClaw accepted the configuration but has not loaded the Memory plugin; restart or reload OpenClaw, then rerun cyber-health update."
            verified.warnings.append(warning)
            raise MemoryBootstrapError(f"Memory bootstrap could not be verified: {verified.reason}")
        status.executed = True
        status.reason = "Memory bootstrap completed and health-manager connection verified"
        return verified
