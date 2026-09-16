"""Safety and idempotency tests for explicit one-Vault memory bootstrap."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import textwrap
import unittest

from cyber_health.memory_bootstrap import MemoryBootstrapError, MemoryBootstrapper


class MemoryBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cyber-health-memory-bootstrap-")
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "Vault"
        self.vault.mkdir()
        self.obsidian_app = self.root / "Obsidian.app"
        self.obsidian_app.mkdir()
        self.plugin_archive = self.root / "obsidian-memory-plugin-0.3.2.tgz"
        self.plugin_archive.write_bytes(b"fixture archive")
        self.state = self.root / "openclaw-state.json"
        self.state.write_text(json.dumps({"installed": False, "entry": None}), encoding="utf-8")
        self.openclaw = self.root / "openclaw"
        self.openclaw.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json, sys
                from pathlib import Path
                state_path = Path({str(self.state)!r})
                state = json.loads(state_path.read_text())
                args = sys.argv[1:]
                if args[:2] == ['plugins', 'inspect']:
                    if not state['installed']:
                        print('Plugin not installed', file=sys.stderr)
                        raise SystemExit(1)
                    print(json.dumps({{'plugin': {{'id': 'obsidian-memory-plugin', 'status': 'loaded'}}}}))
                elif args[:2] == ['plugins', 'install']:
                    state['installed'] = True
                    state_path.write_text(json.dumps(state))
                elif args[:2] == ['config', 'get']:
                    if state['entry'] is None:
                        print('Config path is valid but unset', file=sys.stderr)
                        raise SystemExit(1)
                    print(json.dumps(state['entry']))
                elif args[:2] == ['config', 'set']:
                    value = json.loads(args[3])
                    entry = state['entry'] or {{}}
                    path = args[2].split('.')[3:]
                    cursor = entry
                    for key in path[:-1]:
                        cursor = cursor.setdefault(key, {{}})
                    cursor[path[-1]] = value
                    state['entry'] = entry
                    state_path.write_text(json.dumps(state))
                else:
                    print('unexpected args: ' + repr(args), file=sys.stderr)
                    raise SystemExit(2)
                """
            ),
            encoding="utf-8",
        )
        self.openclaw.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def bootstrap(self, *, project_id: str | None = None, dry_run: bool = False) -> MemoryBootstrapper:
        return MemoryBootstrapper(
            vault_path=self.vault,
            project_root=self.root,
            openclaw_bin=str(self.openclaw),
            openclaw_env={},
            plugin_archive=self.plugin_archive,
            project_id=project_id,
            dry_run=dry_run,
            obsidian_application_finder=lambda: self.obsidian_app,
        )

    def test_bootstrap_creates_only_managed_project_and_merges_agent_config(self) -> None:
        bootstrap = self.bootstrap()
        plan = bootstrap.plan()
        self.assertEqual(plan.plugin_action, "install")
        self.assertEqual(plan.obsidian_action, "verify")
        self.assertEqual(plan.vault_action, "create")
        self.assertEqual(plan.config_action, "configure")
        memory = bootstrap.apply(plan)
        self.assertTrue(memory.connected)
        self.assertTrue(plan.executed)
        assert plan.project_id
        project = self.vault / "20-Projects" / plan.project_id
        self.assertTrue(plan.project_id.startswith("Cyber-Health-Agent-"))
        self.assertTrue((self.vault / "AGENTS.md").is_file())
        self.assertTrue((self.vault / "00-System" / "projects.yaml").is_file())
        for relative in ("AGENTS.md", "rules.md", "index.md", "log.md", "inbox/assets", "raw/assets", "wiki/knowledge"):
            self.assertTrue((project / relative).exists(), relative)
        registry = (self.vault / "00-System" / "projects.yaml").read_text(encoding="utf-8")
        self.assertIn(plan.project_id, registry)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertTrue(state["installed"])
        config = state["entry"]["config"]["agentConfigs"]["health-manager"]
        self.assertEqual(config["vaultPath"], str(self.vault))
        self.assertEqual(config["projectId"], plan.project_id)

    def test_existing_other_agent_config_is_preserved_and_second_run_is_noop(self) -> None:
        self.state.write_text(
            json.dumps(
                {
                    "installed": True,
                    "entry": {
                        "enabled": True,
                        "hooks": {"allowPromptInjection": True},
                        "config": {"agentConfigs": {"main": {"vaultPath": "/other", "projectId": "Other"}}},
                    },
                }
            ),
            encoding="utf-8",
        )
        first = self.bootstrap()
        plan = first.plan()
        first.apply(plan)
        state_before = self.state.read_text(encoding="utf-8")
        second = self.bootstrap()
        second_plan = second.plan()
        self.assertEqual(second_plan.plugin_action, "verify")
        self.assertEqual(second_plan.vault_action, "verify")
        self.assertEqual(second_plan.config_action, "verify")
        second.apply(second_plan)
        self.assertEqual(state_before, self.state.read_text(encoding="utf-8"))

    def test_different_existing_health_manager_binding_is_refused_without_writes(self) -> None:
        self.state.write_text(
            json.dumps(
                {
                    "installed": True,
                    "entry": {
                        "enabled": True,
                        "config": {"agentConfigs": {"health-manager": {"vaultPath": "/foreign", "projectId": "Foreign"}}},
                    },
                }
            ),
            encoding="utf-8",
        )
        before = self.state.read_text(encoding="utf-8")
        bootstrap = self.bootstrap()
        plan = bootstrap.plan()
        self.assertEqual(plan.config_action, "error")
        self.assertIn("refusing to overwrite", plan.reason)
        with self.assertRaises(MemoryBootstrapError):
            bootstrap.apply(plan)
        self.assertEqual(before, self.state.read_text(encoding="utf-8"))
        self.assertFalse((self.vault / "20-Projects").exists())

    def test_dry_run_is_zero_mutation(self) -> None:
        bootstrap = self.bootstrap(dry_run=True)
        plan = bootstrap.plan()
        memory = bootstrap.apply(plan)
        self.assertEqual(memory.state, "planned")
        self.assertFalse((self.vault / "00-System").exists())
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["installed"], False)

    def test_missing_obsidian_requires_installation_before_any_mutation(self) -> None:
        bootstrap = MemoryBootstrapper(
            vault_path=self.vault,
            project_root=self.root,
            openclaw_bin=str(self.openclaw),
            openclaw_env={},
            plugin_archive=self.plugin_archive,
            obsidian_application_finder=lambda: None,
        )
        plan = bootstrap.plan()
        self.assertEqual(plan.obsidian_action, "install-required")
        self.assertIn("Obsidian is not installed", plan.reason)
        with self.assertRaises(MemoryBootstrapError):
            bootstrap.apply(plan)
        self.assertFalse((self.vault / "00-System").exists())
        self.assertFalse(json.loads(self.state.read_text(encoding="utf-8"))["installed"])

    def test_hermes_or_codex_only_bootstrap_does_not_require_openclaw(self) -> None:
        bootstrap = MemoryBootstrapper(
            vault_path=self.vault,
            project_root=self.root,
            openclaw_bin=None,
            openclaw_env={},
            plugin_archive=self.plugin_archive,
            obsidian_application_finder=lambda: self.obsidian_app,
        )
        plan = bootstrap.plan()
        self.assertEqual(plan.obsidian_action, "verify")
        self.assertEqual(plan.plugin_action, "skip")
        self.assertEqual(plan.config_action, "skip")
        self.assertEqual(plan.vault_action, "create")
        self.assertIn("without OpenClaw", plan.reason)

        memory = bootstrap.apply(plan)
        self.assertTrue(memory.connected)
        self.assertFalse(memory.plugin_loaded)
        self.assertTrue((self.vault / "00-System" / "projects.yaml").is_file())
        self.assertTrue((self.vault / "20-Projects" / (plan.project_id or "")).is_dir())
        self.assertFalse(json.loads(self.state.read_text(encoding="utf-8"))["installed"])


if __name__ == "__main__":
    unittest.main()
