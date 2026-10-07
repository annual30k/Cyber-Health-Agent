"""stdio MCP server: tool surface, single-owner boundary and error envelopes."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cyber_health import CyberHealthService
from cyber_health_mcp.server import SINGLE_USER_ID, create_mcp_server


class TestMCPStdio(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "mcp_stdio_test.sqlite3")

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_p0_tool_discovery_and_invocations(self) -> None:
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "cyber_health_mcp", "--db", self.db_path],
            env={"PATH": os.environ.get("PATH", "")},
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                init_result = await session.initialize()
                self.assertIn("cyber_health_get_profile", init_result.instructions or "")
                self.assertIn("cyber_health_update_profile", init_result.instructions or "")
                self.assertIn("status=success", init_result.instructions or "")
                self.assertIn("session search/history", init_result.instructions or "")
                self.assertIn("explicit user messages", init_result.instructions or "")

                # 1. Verify exactly 7 P0 tools discovered
                tools_res = await session.list_tools()
                tool_names = [t.name for t in tools_res.tools]
                expected_p0_tools = [
                    "cyber_health_get_profile",
                    "cyber_health_update_profile",
                    "cyber_health_get_today",
                    "cyber_health_log_meal",
                    "cyber_health_get_audit_trail",
                    "cyber_health_health_check",
                    "cyber_health_get_schedule",
                ]
                self.assertEqual(len(tool_names), 7)
                self.assertEqual(tool_names, expected_p0_tools)

                # 2. Call cyber_health_get_profile (read-only purity)
                prof_call = await session.call_tool("cyber_health_get_profile", {})
                prof_data = json.loads(prof_call.content[0].text)
                self.assertEqual(prof_data["user_id"], "owner")
                self.assertEqual(prof_data["state_version"], 0)
                self.assertFalse(prof_data["exists"])
                self.assertEqual(prof_data["onboarding"]["status"], "required")
                self.assertFalse(prof_data["onboarding"]["training_plan_ready"])
                self.assertIn("constraints.height_cm", prof_data["onboarding"]["missing_fields"])

                # 3. Call cyber_health_log_meal
                meal_payload = {
                    "occurred_at": "2026-09-04T12:30:00+08:00",
                    "meal_type": "lunch",
                    "foods": [{"name": "Salmon and rice", "amount_g": {"low": 200, "high": 250}}],
                    "kcal_low": 480,
                    "kcal_high": 550,
                    "protein_low": 35,
                    "protein_high": 42,
                    "idempotency_key": "stdio-meal-001",
                }
                log_call = await session.call_tool("cyber_health_log_meal", meal_payload)
                log_data = json.loads(log_call.content[0].text)
                self.assertEqual(log_data["status"], "success")
                self.assertEqual(log_data["state_version"], 1)
                self.assertEqual(log_data["data"]["today_totals"]["meal_count"], 1)
                self.assertEqual(log_data["data"]["today_totals"]["kcal_low"], 480)

                # 4. Call cyber_health_get_today
                today_call = await session.call_tool("cyber_health_get_today", {"date": "2026-09-04"})
                today_data = json.loads(today_call.content[0].text)
                self.assertEqual(today_data["state_version"], 1)
                self.assertEqual(today_data["nutrition"]["meal_count"], 1)
                self.assertEqual(today_data["nutrition"]["kcal_low"], 480)
                self.assertFalse(today_data["plan_status"]["missing_data"])

                # 5. Exact idempotency replay over stdio
                replay_call = await session.call_tool("cyber_health_log_meal", meal_payload)
                replay_data = json.loads(replay_call.content[0].text)
                self.assertEqual(replay_data, log_data)

                # 6. Idempotency mismatch rejection over stdio
                mismatch_payload = dict(meal_payload)
                mismatch_payload["kcal_low"] = 800
                mismatch_payload["kcal_high"] = 900
                mismatch_call = await session.call_tool("cyber_health_log_meal", mismatch_payload)
                mismatch_data = json.loads(mismatch_call.content[0].text)
                self.assertEqual(mismatch_data["status"], "failed")
                self.assertEqual(mismatch_data["error"]["code"], "IDEMPOTENCY_MISMATCH")

                # 7. Call cyber_health_health_check
                hc_call = await session.call_tool("cyber_health_health_check", {})
                hc_data = json.loads(hc_call.content[0].text)
                self.assertEqual(hc_data["overall_status"], "ok")
                self.assertEqual(hc_data["components"]["sqlite"], "ok")

                # 8. Call cyber_health_get_schedule
                sched_call = await session.call_tool("cyber_health_get_schedule", {})
                sched_data = json.loads(sched_call.content[0].text)
                self.assertIn("events", sched_data)
                self.assertIsInstance(sched_data["events"], list)

                # 9. Call cyber_health_get_audit_trail
                audit_call = await session.call_tool("cyber_health_get_audit_trail", {})
                audit_data = json.loads(audit_call.content[0].text)
                self.assertIn("operations", audit_data)
                self.assertEqual(len(audit_data["operations"]), 1)
                self.assertEqual(audit_data["operations"][0]["action"], "log_meal")

                # 10. Verify safety annotations on P0 tools
                tools_by_name = {t.name: t for t in tools_res.tools}
                prof_ann = tools_by_name["cyber_health_get_profile"].annotations
                self.assertIsNotNone(prof_ann)
                self.assertTrue(prof_ann.readOnlyHint)
                self.assertFalse(prof_ann.destructiveHint)
                self.assertTrue(prof_ann.idempotentHint)
                self.assertFalse(prof_ann.openWorldHint)

                log_meal_ann = tools_by_name["cyber_health_log_meal"].annotations
                self.assertIsNotNone(log_meal_ann)
                self.assertFalse(log_meal_ann.readOnlyHint)
                self.assertFalse(log_meal_ann.destructiveHint)
                self.assertTrue(log_meal_ann.idempotentHint)
                self.assertFalse(log_meal_ann.openWorldHint)

    async def test_allow_all_tools_discovery(self) -> None:
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "cyber_health_mcp", "--db", self.db_path, "--allow-all"],
            env={"PATH": os.environ.get("PATH", "")},
        )

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_res = await session.list_tools()
                tool_names = [t.name for t in tools_res.tools]
                tools_by_name = {t.name: t for t in tools_res.tools}

                # Exactly 27 tools (7 P0 + 20 extended domain capabilities)
                self.assertEqual(len(tool_names), 27)
                self.assertIn("cyber_health_log_daily_metrics", tool_names)
                self.assertIn("cyber_health_delete_meal", tool_names)
                self.assertIn("cyber_health_log_workout", tool_names)
                self.assertIn("cyber_health_daily_review", tool_names)
                self.assertIn("cyber_health_plan_tomorrow", tool_names)
                self.assertIn("cyber_health_acknowledge_schedule_event", tool_names)
                self.assertIn("cyber_health_maintain_memory", tool_names)
                self.assertIn("cyber_health_get_remaining_calories", tool_names)
                self.assertIn("cyber_health_get_training_plan", tool_names)
                self.assertIn("cyber_health_complete_workout", tool_names)
                self.assertIn("cyber_health_confirm_training_progression", tool_names)
                self.assertIn("cyber_health_substitute_exercise", tool_names)
                self.assertIn("cyber_health_query_knowledge", tool_names)
                self.assertIn("cyber_health_export_data", tool_names)
                self.assertIn("cyber_health_import_data", tool_names)
                self.assertIn("cyber_health_memory_action", tool_names)
                self.assertIn("cyber_health_schedule_daily_reminders", tool_names)
                self.assertIn("cyber_health_update_schedule_event", tool_names)
                self.assertIn("cyber_health_query_memory", tool_names)
                self.assertIn("cyber_health_get_memory_suggestions", tool_names)

                # Verify granular safety annotations
                del_meal_ann = tools_by_name["cyber_health_delete_meal"].annotations
                self.assertTrue(del_meal_ann.destructiveHint)
                self.assertFalse(del_meal_ann.readOnlyHint)

                maint_ann = tools_by_name["cyber_health_maintain_memory"].annotations
                self.assertTrue(maint_ann.destructiveHint)
                self.assertTrue(maint_ann.openWorldHint)

                mem_act_ann = tools_by_name["cyber_health_memory_action"].annotations
                self.assertTrue(mem_act_ann.destructiveHint)
                self.assertTrue(mem_act_ann.openWorldHint)

                train_ann = tools_by_name["cyber_health_get_training_plan"].annotations
                self.assertTrue(train_ann.readOnlyHint)
                self.assertFalse(train_ann.destructiveHint)

                import_ann = tools_by_name["cyber_health_import_data"].annotations
                self.assertTrue(import_ann.destructiveHint)
                self.assertFalse(import_ann.readOnlyHint)

                qm_ann = tools_by_name["cyber_health_query_memory"].annotations
                self.assertTrue(qm_ann.readOnlyHint)
                self.assertTrue(qm_ann.openWorldHint)

                suggestion_ann = tools_by_name["cyber_health_get_memory_suggestions"].annotations
                self.assertTrue(suggestion_ann.readOnlyHint)
                self.assertFalse(suggestion_ann.destructiveHint)
                self.assertFalse(suggestion_ann.openWorldHint)

                # Test stdio invocation of newly connected extended tools
                # 1. get_training_plan
                tp_res = await session.call_tool(
                    "cyber_health_get_training_plan",
                    {"date": "2026-09-04", "target_duration_min": 45},
                )
                tp_data = json.loads(tp_res.content[0].text)
                self.assertEqual(tp_data["status"], "partial")
                self.assertTrue(tp_data["onboarding_required"])
                self.assertIsNone(tp_data["plan"])

                # 2. complete_workout
                cw_res = await session.call_tool(
                    "cyber_health_complete_workout",
                    {
                        "date": "2026-09-04",
                        "idempotency_key": "cw-key-001",
                        "session_rpe": 7.5,
                        "completion_rate": 1.0,
                    },
                )
                cw_data = json.loads(cw_res.content[0].text)
                self.assertEqual(cw_data["status"], "success")

                # 3. query_knowledge
                qk_res = await session.call_tool(
                    "cyber_health_query_knowledge",
                    {"query": "protein intake"},
                )
                qk_data = json.loads(qk_res.content[0].text)
                self.assertEqual(qk_data["status"], "success")
                self.assertIn("evidence_items", qk_data)

                # 4. export_data
                exp_res = await session.call_tool(
                    "cyber_health_export_data",
                    {},
                )
                exp_data = json.loads(exp_res.content[0].text)
                self.assertEqual(exp_data["status"], "success")
                self.assertIn("facts", exp_data)

                # 5. get_remaining_calories
                rem_res = await session.call_tool(
                    "cyber_health_get_remaining_calories",
                    {"date": "2026-09-04"},
                )
                rem_data = json.loads(rem_res.content[0].text)
                self.assertEqual(rem_data["status"], "success")
                self.assertIn("suggestion", rem_data)

                # 6. schedule_daily_reminders
                sdr_res = await session.call_tool(
                    "cyber_health_schedule_daily_reminders",
                    {"date": "2026-09-04", "idempotency_key": "sdr-key-001"},
                )
                sdr_data = json.loads(sdr_res.content[0].text)
                self.assertEqual(sdr_data["status"], "success")
                self.assertEqual(len(sdr_data["data"]["scheduled_events"]), 5)

                # 7. memory_action
                mem_res = await session.call_tool(
                    "cyber_health_memory_action",
                    {
                        "idempotency_key": "mem-key-001",
                        "action_type": "propose",
                        "payload": {"rule": "High protein breakfast improves satiety"},
                    },
                )
                mem_data = json.loads(mem_res.content[0].text)
                # Without external provider connected, outbox gracefully buffers candidate
                self.assertEqual(mem_data["status"], "partial")

                # 8. query_memory (dual-layer)
                qm_res = await session.call_tool(
                    "cyber_health_query_memory",
                    {"query": "protein"},
                )
                qm_data = json.loads(qm_res.content[0].text)
                self.assertEqual(qm_data["status"], "success")
                self.assertIn("sqlite_facts", qm_data)
                self.assertIn("obsidian_memories", qm_data)


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
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT user_id FROM meal_log").fetchone()[0], SINGLE_USER_ID)

    def test_legacy_partition_refused_before_service_start(self) -> None:
        CyberHealthService(self.db)  # create the schema
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                """INSERT INTO meal_log(meal_id, user_id, occurred_at, meal_type, foods_json, kcal_low, kcal_high,
                       status, causation_id, state_version, created_at)
                   VALUES ('legacy-meal', 'alex', '2026-09-18T08:30:00+08:00', 'breakfast', '[]', 80, 100,
                       'active', 'op_legacy', 1, '2026-09-18T00:30:00+00:00')"""
            )
            conn.commit()
        with self.assertRaisesRegex(RuntimeError, "migrate"):
            create_mcp_server(self.db)
        with self.assertRaisesRegex(RuntimeError, "migrate"):
            CyberHealthService(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM meal_log WHERE user_id='alex'").fetchone()[0], 1)


class ConsoleEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CyberHealthService(Path(self.tmp.name) / "test.sqlite3")

    def test_console_entrypoint_is_callable(self):
        import cyber_health_mcp
        self.assertTrue(callable(getattr(cyber_health_mcp, "main", None)))


class ErrorEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_remaining.sqlite3"
        self.service = CyberHealthService(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_mcp_err_envelope_sanitization(self) -> None:
        """Verify error envelope strips raw input values and does not leak raw sensitive text."""
        server = create_mcp_server(self.db_path)

        # Access the wrapped function or tool directly
        log_tool = None
        for tool in server._tool_manager.list_tools():
            if tool.name == "cyber_health_log_meal":
                log_tool = tool.fn
                break
        
        self.assertIsNotNone(log_tool)
        res = log_tool(
            occurred_at="2026-09-04T12:00:00+08:00",
            meal_type="lunch",
            foods=[{"name": "bread", "amount_g": {"low": 300, "high": 100}}],
            kcal_low=100,
            kcal_high=200,
            idempotency_key="key_err_01",
        )
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["error"]["code"], "VALIDATION_ERROR")
        self.assertNotIn("input_value=", res["error"]["message"])


if __name__ == "__main__":
    unittest.main()
