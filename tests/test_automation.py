"""The host nightly-review job is reported and reconciled against the release's spec."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cyber_health import CyberHealthService
from cyber_health.automation import (
    REVISION,
    SESSION_TARGET,
    automation_status,
    render_nightly_message,
    sync_automation,
)
from test_support import isolate_host_clis, make_python_command

FAKE_OPENCLAW = r'''
import json, sys, uuid
from pathlib import Path
state_file = Path(__STATE__)
state = json.loads(state_file.read_text())
state["calls"].append(sys.argv[1:])
args = sys.argv[2:]
action = args[0] if args else ""
def save():
    state_file.write_text(json.dumps(state))
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
def apply(job):
    if "--name" in args: job["name"] = opt("--name")
    if "--agent" in args: job["agentId"] = opt("--agent")
    if "--session" in args: job["sessionTarget"] = opt("--session")
    if "--message" in args: job["payload"] = {"kind": "agentTurn", "message": opt("--message")}
    if "--cron" in args or "--tz" in args:
        job["schedule"] = {"kind": "cron", "expr": opt("--cron", job.get("schedule", {}).get("expr")),
                           "tz": opt("--tz", job.get("schedule", {}).get("tz"))}
    if "--disabled" in args or "--disable" in args: job["enabled"] = False
    if "--enable" in args: job["enabled"] = True
if sys.argv[1] != "cron":
    sys.exit(2)
if action == "list":
    print(json.dumps({"jobs": state["jobs"]}))
elif action == "get":
    print(json.dumps({"job": next(j for j in state["jobs"] if j["id"] == args[1])}))
elif action == "runs":
    if int(opt("--limit", "50")) < 1:
        print("Invalid --limit (must be a positive integer).", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"entries": state["runs"].get(args[1], [])[: int(opt("--limit", "50"))]}))
elif action == "add":
    job = {"id": str(uuid.uuid4()), "enabled": True, "declarationKey": opt("--declaration-key"), "state": {}}
    apply(job)
    state["jobs"].append(job)
    save()
    print(json.dumps({"ok": True, "job": job}))
elif action == "edit":
    job = next(j for j in state["jobs"] if j["id"] == args[1])
    apply(job)
    save()
    print(json.dumps({"ok": True}))
else:
    sys.exit(2)
save()
'''


class AutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        isolate_host_clis(self, self.root)
        self.target = self.root / ".cyber-health"
        (self.target / "config").mkdir(parents=True)
        (self.target / "config" / "installation.json").write_text("{}", encoding="utf-8")
        self.db = self.target / "data" / "cyber-health.sqlite3"
        self.state_file = self.root / "openclaw-state.json"
        self.write_state(jobs=[], runs={})
        self.openclaw = str(make_python_command(
            self.root, "openclaw", FAKE_OPENCLAW.replace("__STATE__", repr(str(self.state_file)))
        ))

    def write_state(self, *, jobs: list, runs: dict) -> None:
        self.state_file.write_text(json.dumps({"jobs": jobs, "runs": runs, "calls": []}), encoding="utf-8")

    def state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def mutating_calls(self) -> list:
        return [c for c in self.state()["calls"] if len(c) > 1 and c[1] in ("add", "edit")]

    def profile(self, **constraints) -> None:
        CyberHealthService(self.db).update_profile(constraints=constraints, timezone=constraints.pop("tz", None),
                                                   idempotency_key="profile")

    def legacy_job(self, **overrides) -> dict:
        job = {
            "id": "legacy-1", "name": "cyber-health-nightly-review-qiuqiquan", "agentId": "health-manager",
            "declarationKey": "cyber-health:daily-review:qiuqiquan", "enabled": True,
            "schedule": {"kind": "cron", "expr": "30 21 * * *", "tz": "Asia/Shanghai"},
            "sessionTarget": "session:4d15a3b4", "payload": {"kind": "agentTurn", "message": "执行晚间复盘（user_id=qiuqiquan）"},
            "state": {"consecutiveErrors": 0, "nextRunAtMs": 1791466200000},
        }
        job.update(overrides)
        return job

    def test_message_carries_the_declaration_key_and_revision(self) -> None:
        message = render_nightly_message("cyber-health:daily-review:owner")
        self.assertIn("cyber-health:daily-review:owner", message)
        self.assertIn(REVISION, message)
        self.assertIn("不要输出 NO_REPLY", message)
        self.assertIn("最多读 3 个", message)
        self.assertNotIn("user_id=", message)

    def test_missing_job_is_planned_then_created_in_the_main_conversation(self) -> None:
        self.assertFalse(automation_status(self.target, self.openclaw).found)
        planned = sync_automation(self.target, self.openclaw, dry_run=True)
        self.assertEqual(planned.action, "planned-create")
        self.assertEqual(self.mutating_calls(), [])

        created = sync_automation(self.target, self.openclaw)
        self.assertEqual(created.action, "created")
        job = self.state()["jobs"][0]
        self.assertEqual(job["agentId"], "health-manager")
        self.assertEqual(job["sessionTarget"], SESSION_TARGET)
        self.assertEqual(job["schedule"], {"kind": "cron", "expr": "30 21 * * *", "tz": "Asia/Shanghai"})
        self.assertEqual(job["declarationKey"], "cyber-health:daily-review:owner")
        self.assertTrue(created.status.in_sync)

        again = sync_automation(self.target, self.openclaw)
        self.assertEqual(again.action, "unchanged")
        self.assertEqual(len(self.mutating_calls()), 1)

    def test_legacy_job_is_adopted_backed_up_and_corrected(self) -> None:
        self.write_state(jobs=[self.legacy_job()], runs={"legacy-1": [
            {"runAtMs": 1791379800036, "status": "ok", "sessionKey": "agent:health-manager:main",
             "durationMs": 150048, "usage": {"total_tokens": 623297}, "summary": "NO_REPLY"},
        ]})
        status = automation_status(self.target, self.openclaw)
        self.assertTrue(status.found)
        self.assertFalse(status.in_sync)
        self.assertTrue(any("session" in d for d in status.drift))
        self.assertTrue(any("instructions" in d for d in status.drift))
        self.assertEqual(status.recent_runs[0]["delivered_to"], "agent:health-manager:main")
        self.assertEqual(status.recent_runs[0]["total_tokens"], 623297)

        self.assertEqual(sync_automation(self.target, self.openclaw, dry_run=True).action, "planned-update")
        self.assertEqual(self.mutating_calls(), [])

        result = sync_automation(self.target, self.openclaw)
        self.assertEqual(result.action, "updated", result.reason)
        backup = json.loads(Path(result.backup_file).read_text(encoding="utf-8"))
        self.assertIn("user_id=qiuqiquan", backup["payload"]["message"])
        if sys.platform != "win32":
            self.assertEqual(os.stat(result.backup_file).st_mode & 0o777, 0o600)
        job = self.state()["jobs"][0]
        self.assertEqual(job["id"], "legacy-1")
        self.assertEqual(job["sessionTarget"], SESSION_TARGET)
        self.assertEqual(job["payload"]["message"], render_nightly_message("cyber-health:daily-review:owner"))
        self.assertTrue(automation_status(self.target, self.openclaw).in_sync)

    def test_schedule_and_timezone_follow_the_profile(self) -> None:
        self.profile(daily_review_time="22:15", tz="America/New_York")
        self.write_state(jobs=[self.legacy_job()], runs={})
        drift = automation_status(self.target, self.openclaw).drift
        self.assertTrue(any("'15 22 * * *'" in d and "America/New_York" in d for d in drift))
        sync_automation(self.target, self.openclaw)
        self.assertEqual(self.state()["jobs"][0]["schedule"], {"kind": "cron", "expr": "15 22 * * *", "tz": "America/New_York"})

    def test_disabled_reminders_disable_the_job(self) -> None:
        self.profile(daily_review_enabled=False)
        self.write_state(jobs=[self.legacy_job()], runs={})
        sync_automation(self.target, self.openclaw)
        self.assertFalse(self.state()["jobs"][0]["enabled"])

    def test_duplicates_are_refused(self) -> None:
        self.write_state(jobs=[self.legacy_job(), self.legacy_job(id="legacy-2")], runs={})
        result = sync_automation(self.target, self.openclaw)
        self.assertEqual(result.action, "refused")
        self.assertEqual(self.mutating_calls(), [])

    def test_never_creates_a_second_job_when_another_host_runs_it(self) -> None:
        hermes = Path(os.environ["HERMES_HOME"]) / "cron"
        hermes.mkdir(parents=True)
        (hermes / "jobs.json").write_text(json.dumps({"jobs": [{"name": "cyber-health daily-review"}]}), encoding="utf-8")
        result = sync_automation(self.target, self.openclaw)
        self.assertEqual(result.action, "refused")
        self.assertIn("hermes", result.reason)
        self.assertEqual(self.mutating_calls(), [])

    def test_without_openclaw_nothing_is_attempted(self) -> None:
        status = automation_status(self.target, None)
        self.assertFalse(status.available)
        self.assertEqual(sync_automation(self.target, None).action, "refused")

    def test_cli_exit_codes_reflect_sync_state(self) -> None:
        from cyber_health.cli import main

        self.write_state(jobs=[self.legacy_job()], runs={})
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["automation", "status", "--target-dir", str(self.target), "--openclaw-bin", self.openclaw]), 1)
            self.assertEqual(main(["automation", "sync", "--target-dir", str(self.target), "--openclaw-bin", self.openclaw]), 0)
            self.assertEqual(main(["automation", "status", "--target-dir", str(self.target), "--openclaw-bin", self.openclaw]), 0)

    def test_update_reports_drift_but_never_edits_the_host(self) -> None:
        from cyber_health.update import BackupStatus, CyberHealthUpdater, UpdateReport

        self.write_state(jobs=[self.legacy_job()], runs={})
        updater = CyberHealthUpdater(target_dir=self.target, openclaw_bin=self.openclaw, codex_bin=None, hermes_bin=None,
                                     dry_run=False)
        report = UpdateReport(dry_run=False, success=True, target_dir=str(self.target), old_version="1", new_version="2",
                              backup=BackupStatus(), message="Update completed successfully")
        updater.post_update(report)
        self.assertIn("cyber-health automation sync", report.message)
        self.assertEqual(self.mutating_calls(), [])


if __name__ == "__main__":
    unittest.main()
