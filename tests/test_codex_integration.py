from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import textwrap
import unittest

from cyber_health.codex_integration import (
    apply_codex_registration,
    inspect_codex_registration,
    plan_codex_registration,
    remove_codex_registration,
)


class CodexIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cyber-health-codex-test-")
        self.root = Path(self.temp.name)
        self.target = (self.root / ".cyber-health").resolve()
        self.db = self.target / "data" / "cyber-health.sqlite3"
        self.command = self.target / "venv" / "bin" / "cyber-health-mcp"
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"servers": {}, "unrelated": {"keep": True}}))
        script = self.root / "codex"
        script.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json, sys
                from pathlib import Path
                state_path = Path({str(self.state)!r})
                state = json.loads(state_path.read_text())
                args = sys.argv[1:]
                name = 'cyber-health'
                if args[:3] == ['mcp', 'get', name]:
                    server = state['servers'].get(name)
                    if server is None:
                        print("Error: No MCP server named 'cyber-health' found.", file=sys.stderr)
                        raise SystemExit(1)
                    print(json.dumps({{'name': name, 'enabled': True, 'transport': server}}))
                elif args[:3] == ['mcp', 'add', name]:
                    sep = args.index('--')
                    state['servers'][name] = {{'type': 'stdio', 'command': args[sep + 1], 'args': args[sep + 2:], 'env': None, 'env_vars': [], 'cwd': None}}
                    state_path.write_text(json.dumps(state))
                elif args[:3] == ['mcp', 'remove', name]:
                    state['servers'].pop(name, None)
                    state_path.write_text(json.dumps(state))
                else:
                    raise SystemExit(2)
                """
            )
        )
        script.chmod(0o755)
        self.codex = str(script)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def desired(self):
        return plan_codex_registration(
            self.codex,
            self.target,
            self.db,
            str(self.command),
            ["--db", str(self.db), "--allow-all"],
            "",
        )

    def test_absent_entry_is_registered_without_touching_unrelated_state(self) -> None:
        status = self.desired()
        self.assertEqual(status.action, "register")
        apply_codex_registration(self.codex, status)
        state = json.loads(self.state.read_text())
        self.assertTrue(state["unrelated"]["keep"])
        self.assertEqual(state["servers"]["cyber-health"]["command"], str(self.command))

    def test_owned_entry_is_detected_and_can_be_removed(self) -> None:
        status = self.desired()
        apply_codex_registration(self.codex, status)
        inspected = inspect_codex_registration(self.codex, self.target, self.db)
        self.assertTrue(inspected.ownership_proven)
        inspected.action = "remove"
        remove_codex_registration(self.codex, inspected, self.target, self.db)
        state = json.loads(self.state.read_text())
        self.assertNotIn("cyber-health", state["servers"])
        self.assertTrue(state["unrelated"]["keep"])

    def test_foreign_same_name_entry_is_refused(self) -> None:
        state = json.loads(self.state.read_text())
        state["servers"]["cyber-health"] = {
            "type": "stdio",
            "command": "/tmp/foreign-server",
            "args": [],
            "cwd": None,
        }
        self.state.write_text(json.dumps(state))
        status = self.desired()
        self.assertEqual(status.action, "refused")
        self.assertFalse(status.ownership_proven)

    def test_real_codex_cli_round_trip_isolated_from_user_config(self) -> None:
        real_codex = shutil.which("codex")
        if not real_codex:
            self.skipTest("Codex CLI not installed")
        isolated_home = self.root / "codex-home"
        isolated_home.mkdir()
        status = plan_codex_registration(
            real_codex,
            self.target,
            self.db,
            str(self.command),
            ["--db", str(self.db), "--allow-all"],
            "",
            codex_home=isolated_home,
        )
        self.assertEqual(status.action, "register")
        apply_codex_registration(real_codex, status, codex_home=isolated_home)
        inspected = inspect_codex_registration(
            real_codex, self.target, self.db, codex_home=isolated_home
        )
        self.assertTrue(inspected.ownership_proven)


if __name__ == "__main__":
    unittest.main()
