"""Tests for the health-manager Obsidian Memory bridge and installer discovery."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cyber_health import CyberHealthService, ValidationError
from cyber_health.health_memory import inspect_health_manager_memory
from cyber_health.memory import MemoryUnavailable
from cyber_health.obsidian_memory_provider import ObsidianMemoryProvider


class MemoryBridgeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".cyber-health-memory-", dir=Path.cwd())
        self.vault = Path(self.temp_dir.name) / "Vault"
        self.project_id = "Health-Manager-test"
        self.project = self.vault / "20-Projects" / self.project_id
        (self.vault / "00-System").mkdir(parents=True)
        (self.project / "inbox").mkdir(parents=True)
        (self.project / "raw").mkdir()
        (self.project / "wiki" / "knowledge").mkdir(parents=True)
        (self.project / "checkpoints").mkdir()
        (self.project / "index.md").write_text(
            "# Health Manager Index\n\n## Knowledge\n\n_No knowledge pages yet._\n",
            encoding="utf-8",
        )
        (self.project / "log.md").write_text(
            "# Health Manager Log\n",
            encoding="utf-8",
        )
        (self.vault / "00-System" / "projects.yaml").write_text(
            f"projects:\n  - id: {self.project_id}\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_provider_promotes_candidate_idempotently(self) -> None:
        provider = ObsidianMemoryProvider(self.vault, self.project_id)
        self.assertEqual(provider.call("ping", {})["status"], "ok")

        proposed = provider.call(
            "memory.propose",
            {
                "intent_id": "intent-health-1",
                "user_id": "qiuqiquan",
                "title": "长期早餐规律",
                "evidence": {"statement": "早餐通常在训练前完成"},
            },
        )
        self.assertEqual(proposed["status"], "pending")
        replay = provider.call(
            "memory.propose",
            {"intent_id": "intent-health-1", "user_id": "qiuqiquan"},
        )
        self.assertTrue(replay["idempotent_replay"])

        promoted = provider.call(
            "memory.confirm",
            {"candidate_id": proposed["candidate_id"], "confirmed": True},
        )
        self.assertEqual(promoted["status"], "confirmed_wiki")
        self.assertTrue((self.project / promoted["raw_path"]).is_file())
        self.assertTrue((self.project / promoted["wiki_path"]).is_file())
        self.assertIn("wiki/knowledge", (self.project / "index.md").read_text(encoding="utf-8"))
        self.assertIn("ingest | 长期早餐规律", (self.project / "log.md").read_text(encoding="utf-8"))

        results = provider.call("memory.query", {"query": "早餐规律", "limit": 10})
        self.assertEqual(results["items"][0]["confirmation_status"], "confirmed_wiki")

    def test_provider_requires_confirmation_for_every_promotion_route(self) -> None:
        provider = ObsidianMemoryProvider(self.vault, self.project_id)
        proposed = provider.call("memory.propose", {
            "intent_id": "intent-unconfirmed",
            "user_id": "qiuqiquan",
            "evidence": {"statement": "待确认"},
        })
        candidate_id = proposed["candidate_id"]
        for method, payload in (
            ("memory.confirm", {"candidate_id": candidate_id}),
            ("memory.action", {"candidate_id": candidate_id, "action_type": "confirm"}),
            ("memory.update", {"candidate_id": candidate_id}),
            ("memory.reject", {"candidate_id": candidate_id, "action_type": "confirm"}),
        ):
            with self.subTest(method=method, payload=payload):
                with self.assertRaises(MemoryUnavailable):
                    provider.call(method, payload)
        self.assertFalse((self.project / "raw" / f"raw-{candidate_id}.md").exists())
        self.assertFalse((self.project / "wiki" / "knowledge" / f"{candidate_id}.md").exists())

        promoted = provider.call("memory.action", {
            "candidate_id": candidate_id,
            "action_type": "confirm",
            "confirmed": True,
        })
        self.assertEqual(promoted["status"], "confirmed_wiki")

    def test_service_rejects_unconfirmed_generic_action_before_real_provider_io(self) -> None:
        provider = ObsidianMemoryProvider(self.vault, self.project_id)
        proposed = provider.call("memory.propose", {
            "intent_id": "intent-service-boundary", "user_id": "qiuqiquan",
            "evidence": {"statement": "待用户确认"},
        })
        candidate_id = proposed["candidate_id"]
        service = CyberHealthService(Path(self.temp_dir.name) / "health.sqlite3", memory_provider=provider)
        with self.assertRaises(ValidationError):
            service.memory_action(
                user_id="qiuqiquan", action_type="action", confirmed=False,
                payload={"candidate_id": candidate_id, "action_type": "confirm"},
                idempotency_key="no-user-confirmation",
            )
        self.assertFalse((self.project / "raw" / f"raw-{candidate_id}.md").exists())
        self.assertFalse((self.project / "wiki" / "knowledge" / f"{candidate_id}.md").exists())

    def test_inspector_requires_plugin_and_project_scope(self) -> None:
        config = {
            "enabled": True,
            "config": {
                "agentConfigs": {
                    "health-manager": {
                        "vaultPath": str(self.vault),
                        "projectId": self.project_id,
                    }
                }
            },
        }
        runtime = {"plugin": {"status": "loaded"}}
        with (
            mock.patch("cyber_health.health_memory._path_has_symlink", return_value=False),
            mock.patch(
                "cyber_health.health_memory._run_json",
                side_effect=[(config, ""), (runtime, "")],
            ),
        ):
            status = inspect_health_manager_memory("openclaw", env={})
        self.assertTrue(status.connected)
        self.assertEqual(status.project_id, self.project_id)
        self.assertIn("--memory-provider", status.provider_args)

    def test_inspector_warns_when_agent_configuration_is_missing(self) -> None:
        with mock.patch(
            "cyber_health.health_memory._run_json",
            return_value=({"enabled": True, "config": {"agentConfigs": {}}}, ""),
        ):
            status = inspect_health_manager_memory("openclaw", env={})
        self.assertEqual(status.state, "unconfigured")
        self.assertFalse(status.connected)
        self.assertTrue(status.warnings)


if __name__ == "__main__":
    unittest.main()
