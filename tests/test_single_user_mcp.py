"""The MCP boundary must never let a client choose a health-fact partition."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from cyber_health import CyberHealthService
from cyber_health_mcp.server import SINGLE_USER_ID, create_mcp_server


class SingleUserMCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Path(self.temp_dir.name) / "health.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def tool(server, name):
        return next(t for t in server._tool_manager.list_tools() if t.name == name)

    def test_all_tools_hide_user_id(self) -> None:
        server = create_mcp_server(self.db, allow_all_tools=True)
        tools = server._tool_manager.list_tools()
        self.assertEqual(len(tools), 27)
        for tool in tools:
            self.assertNotIn("user_id", tool.parameters.get("properties", {}), tool.name)

    def test_two_sessions_share_one_owner(self) -> None:
        first = create_mcp_server(self.db)
        result = self.tool(first, "cyber_health_log_meal").fn(
            occurred_at="2026-09-18T08:30:00+08:00",
            meal_type="breakfast",
            foods=[{"name": "soy milk"}],
            kcal_low=80,
            kcal_high=100,
            idempotency_key="breakfast-001",
        )
        self.assertEqual(result["status"], "success")

        second = create_mcp_server(self.db)
        today = self.tool(second, "cyber_health_get_today").fn(date="2026-09-18")
        self.assertEqual(today["state_version"], 1)
        self.assertEqual(today["nutrition"]["meal_count"], 1)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT user_id FROM meal_log").fetchone()[0], SINGLE_USER_ID)

    def test_legacy_partition_refused_before_service_start(self) -> None:
        CyberHealthService(self.db).log_meal(
            user_id="alex",
            occurred_at="2026-09-18T08:30:00+08:00",
            meal_type="breakfast",
            foods=[],
            kcal_low=80,
            kcal_high=100,
            idempotency_key="legacy-breakfast-001",
        )
        with self.assertRaisesRegex(RuntimeError, "migrate"):
            create_mcp_server(self.db)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM meal_log WHERE user_id='alex'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
