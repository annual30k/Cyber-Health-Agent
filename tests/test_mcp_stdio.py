"""Real stdio MCP cross-process client tests for Cyber Health Agent.

Verifies:
1. Tool discovery over stdio (exactly 7 P0 tools by default, including onboarding writes)
2. Extended tool discovery with --allow-all flag
3. End-to-end tool execution over stdio:
   - cyber_health_get_profile
   - cyber_health_log_meal
   - cyber_health_get_today
   - cyber_health_log_meal idempotency replay
   - cyber_health_log_meal idempotency mismatch
   - cyber_health_get_schedule
   - cyber_health_health_check
   - cyber_health_get_audit_trail
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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
                prof_call = await session.call_tool("cyber_health_get_profile", {"user_id": "u_stdio_user"})
                prof_data = json.loads(prof_call.content[0].text)
                self.assertEqual(prof_data["user_id"], "u_stdio_user")
                self.assertEqual(prof_data["state_version"], 0)
                self.assertFalse(prof_data["exists"])
                self.assertEqual(prof_data["onboarding"]["status"], "required")
                self.assertFalse(prof_data["onboarding"]["training_plan_ready"])
                self.assertIn("constraints.height_cm", prof_data["onboarding"]["missing_fields"])

                # 3. Call cyber_health_log_meal
                meal_payload = {
                    "user_id": "u_stdio_user",
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
                today_call = await session.call_tool("cyber_health_get_today", {"user_id": "u_stdio_user", "date": "2026-09-04"})
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
                sched_call = await session.call_tool("cyber_health_get_schedule", {"user_id": "u_stdio_user"})
                sched_data = json.loads(sched_call.content[0].text)
                self.assertIn("events", sched_data)
                self.assertIsInstance(sched_data["events"], list)

                # 9. Call cyber_health_get_audit_trail
                audit_call = await session.call_tool("cyber_health_get_audit_trail", {"user_id": "u_stdio_user"})
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
                    {"user_id": "u_ext_user", "date": "2026-09-04", "target_duration_min": 45},
                )
                tp_data = json.loads(tp_res.content[0].text)
                self.assertEqual(tp_data["status"], "partial")
                self.assertTrue(tp_data["onboarding_required"])
                self.assertIsNone(tp_data["plan"])

                # 2. complete_workout
                cw_res = await session.call_tool(
                    "cyber_health_complete_workout",
                    {
                        "user_id": "u_ext_user",
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
                    {"user_id": "u_ext_user"},
                )
                exp_data = json.loads(exp_res.content[0].text)
                self.assertEqual(exp_data["status"], "success")
                self.assertIn("facts", exp_data)

                # 5. get_remaining_calories
                rem_res = await session.call_tool(
                    "cyber_health_get_remaining_calories",
                    {"user_id": "u_ext_user", "date": "2026-09-04"},
                )
                rem_data = json.loads(rem_res.content[0].text)
                self.assertEqual(rem_data["status"], "success")
                self.assertIn("suggestion", rem_data)

                # 6. schedule_daily_reminders
                sdr_res = await session.call_tool(
                    "cyber_health_schedule_daily_reminders",
                    {"user_id": "u_ext_user", "date": "2026-09-04", "idempotency_key": "sdr-key-001"},
                )
                sdr_data = json.loads(sdr_res.content[0].text)
                self.assertEqual(sdr_data["status"], "success")
                self.assertEqual(len(sdr_data["data"]["scheduled_events"]), 5)

                # 7. memory_action
                mem_res = await session.call_tool(
                    "cyber_health_memory_action",
                    {
                        "user_id": "u_ext_user",
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
                    {"user_id": "u_ext_user", "query": "protein"},
                )
                qm_data = json.loads(qm_res.content[0].text)
                self.assertEqual(qm_data["status"], "success")
                self.assertIn("sqlite_facts", qm_data)
                self.assertIn("obsidian_memories", qm_data)


if __name__ == "__main__":
    unittest.main()
