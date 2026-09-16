from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import textwrap
import unittest

from cyber_health.hermes_integration import (
    apply_hermes_registration,
    inspect_hermes_registration,
    plan_hermes_registration,
    remove_hermes_registration,
)


class HermesIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cyber-health-hermes-test-")
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "hermes-home"
        self.home.mkdir()
        self.target = self.root / ".cyber-health"
        self.db = self.target / "data" / "cyber-health.sqlite3"
        self.command = self.target / "venv" / "bin" / "cyber-health-mcp"
        self.command.parent.mkdir(parents=True)
        self.command.write_text("#!/bin/sh\nexit 0\n")
        self.command.chmod(0o755)
        self.hermes = self.root / "hermes"
        self.hermes.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, os, sys
                from pathlib import Path
                home = Path(os.environ['HERMES_HOME'])
                config = home / 'config.yaml'
                data = json.loads(config.read_text()) if config.exists() else {'unrelated': {'keep': True}}
                args = sys.argv[1:]
                servers = data.setdefault('mcp_servers', {})
                if args[:3] == ['mcp', 'add', 'cyber-health']:
                    command = args[args.index('--command') + 1]
                    index = args.index('--args')
                    servers['cyber-health'] = {'command': command, 'args': args[index + 1:], 'enabled': True}
                    config.write_text(json.dumps(data))
                elif args[:3] == ['mcp', 'test', 'cyber-health']:
                    if 'cyber-health' not in servers:
                        raise SystemExit(1)
                    print('Tools discovered: 27')
                    print('cyber_health_get_profile')
                    print('cyber_health_health_check')
                elif args[:3] == ['mcp', 'remove', 'cyber-health']:
                    servers.pop('cyber-health', None)
                    config.write_text(json.dumps(data))
                else:
                    raise SystemExit(2)
                """
            )
        )
        self.hermes.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def plan(self):
        return plan_hermes_registration(
            str(self.hermes),
            self.target,
            self.db,
            str(self.command),
            ["--db", str(self.db), "--allow-all"],
            hermes_home=self.home,
        )

    def test_register_probe_and_preserve_unrelated_config(self) -> None:
        status = self.plan()
        self.assertEqual(status.action, "register")
        apply_hermes_registration(
            str(self.hermes), status, self.target, self.db, hermes_home=self.home
        )
        self.assertTrue(status.probe_verified)
        self.assertEqual(status.tool_count, 27)
        data = json.loads((self.home / "config.yaml").read_text())
        self.assertTrue(data["unrelated"]["keep"])

    def test_owned_registration_can_be_removed_with_recheck(self) -> None:
        status = self.plan()
        apply_hermes_registration(
            str(self.hermes), status, self.target, self.db, hermes_home=self.home
        )
        inspected = inspect_hermes_registration(
            str(self.hermes), self.target, self.db, hermes_home=self.home
        )
        self.assertTrue(inspected.ownership_proven)
        inspected.action = "remove"
        remove_hermes_registration(
            str(self.hermes), inspected, self.target, self.db, hermes_home=self.home
        )
        data = json.loads((self.home / "config.yaml").read_text())
        self.assertNotIn("cyber-health", data["mcp_servers"])
        self.assertTrue(data["unrelated"]["keep"])

    def test_foreign_same_name_registration_is_refused(self) -> None:
        (self.home / "config.yaml").write_text(
            json.dumps(
                {
                    "mcp_servers": {
                        "cyber-health": {"command": "/tmp/foreign", "args": []}
                    }
                }
            )
        )
        status = self.plan()
        self.assertEqual(status.action, "refused")
        self.assertFalse(status.ownership_proven)

    def test_owned_but_disabled_registration_is_updated(self) -> None:
        (self.home / "config.yaml").write_text(
            json.dumps(
                {
                    "mcp_servers": {
                        "cyber-health": {
                            "command": str(self.command),
                            "args": ["--db", str(self.db), "--allow-all"],
                            "enabled": False,
                        }
                    }
                }
            )
        )
        status = self.plan()
        self.assertEqual(status.action, "update")
        apply_hermes_registration(
            str(self.hermes), status, self.target, self.db, hermes_home=self.home
        )
        self.assertTrue(status.probe_verified)
        data = json.loads((self.home / "config.yaml").read_text())
        self.assertTrue(data["mcp_servers"]["cyber-health"]["enabled"])

    def test_malformed_yaml_fails_closed(self) -> None:
        (self.home / "config.yaml").write_text("mcp_servers: [unterminated")
        status = inspect_hermes_registration(
            str(self.hermes), self.target, self.db, hermes_home=self.home
        )
        self.assertEqual(status.action, "error")

    def test_real_hermes_cli_round_trip_isolated_from_user_config(self) -> None:
        real_hermes = shutil.which("hermes")
        installed = Path.home() / ".cyber-health"
        command = installed / "venv" / "bin" / "cyber-health-mcp"
        db = installed / "data" / "cyber-health.sqlite3"
        if not real_hermes or not command.exists():
            self.skipTest("Hermes or installed Cyber Health runtime unavailable")
        isolated = self.root / "real-hermes-home"
        isolated.mkdir()
        status = plan_hermes_registration(
            real_hermes,
            installed,
            db,
            str(command),
            ["--db", str(db), "--allow-all"],
            hermes_home=isolated,
        )
        apply_hermes_registration(
            real_hermes, status, installed, db, hermes_home=isolated
        )
        self.assertTrue(status.probe_verified)
        inspected = inspect_hermes_registration(
            real_hermes, installed, db, hermes_home=isolated
        )
        self.assertTrue(inspected.ownership_proven)


if __name__ == "__main__":
    unittest.main()
