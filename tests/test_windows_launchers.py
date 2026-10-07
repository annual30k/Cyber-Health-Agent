"""Locked Windows launchers are moved aside for an upgrade and never lost on failure."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cyber_health.update import CyberHealthUpdater, UpdaterError
from cyber_health.windows_launchers import move_aside_launchers, remove_stale_launchers, restore_launchers
from test_support import isolate_host_clis


def windows():
    return mock.patch.object(sys, "platform", "win32")


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.scripts = Path(tmp.name) / "Scripts"
        self.scripts.mkdir()
        for name in ("cyber-health.exe", "cyber-health-mcp.exe", "python.exe"):
            (self.scripts / name).write_text(name, encoding="utf-8")

    def names(self) -> set[str]:
        return {p.name for p in self.scripts.iterdir()}

    def test_only_our_launchers_are_moved_and_failure_restores_them(self) -> None:
        with windows():
            moved = move_aside_launchers(self.scripts)
            self.assertEqual({orig.name for orig, _ in moved}, {"cyber-health.exe", "cyber-health-mcp.exe"})
            self.assertIn("python.exe", self.names())
            self.assertNotIn("cyber-health.exe", self.names())
            restore_launchers(moved)
        self.assertEqual(self.names(), {"cyber-health.exe", "cyber-health-mcp.exe", "python.exe"})
        self.assertEqual((self.scripts / "cyber-health.exe").read_text(encoding="utf-8"), "cyber-health.exe")

    def test_successful_install_keeps_new_launchers_and_cleans_leftovers(self) -> None:
        with windows():
            moved = move_aside_launchers(self.scripts)
            for original, _ in moved:  # the installer writes fresh launchers
                original.write_text("new", encoding="utf-8")
            restore_launchers(moved)  # no-op: originals were recreated
            removed = remove_stale_launchers(self.scripts)
        self.assertEqual(len(removed), 2)
        self.assertEqual(self.names(), {"cyber-health.exe", "cyber-health-mcp.exe", "python.exe"})
        self.assertEqual((self.scripts / "cyber-health.exe").read_text(encoding="utf-8"), "new")

    def test_other_platforms_are_untouched(self) -> None:
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(move_aside_launchers(self.scripts), [])
            self.assertEqual(remove_stale_launchers(self.scripts), [])
        self.assertIn("cyber-health.exe", self.names())


class UpdaterLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        isolate_host_clis(self, root, "update")
        self.source = root / "src"
        self.source.mkdir()
        (self.source / "pyproject.toml").write_text('[project]\nname = "cyber-health-agent"\nversion = "9.9.9"\n', encoding="utf-8")
        self.target = root / ".cyber-health"
        self.scripts = self.target / "venv" / "Scripts"
        self.scripts.mkdir(parents=True)
        for name in ("python.exe", "cyber-health.exe", "cyber-health-mcp.exe"):
            (self.scripts / name).write_text("old", encoding="utf-8")

    def updater(self) -> CyberHealthUpdater:
        return CyberHealthUpdater(project_root=self.source, target_dir=self.target, openclaw_bin=None, codex_bin=None,
                                  hermes_bin=None, dry_run=False, use_uv=False)

    def test_failed_upgrade_restores_the_commands(self) -> None:
        failed = subprocess.CompletedProcess([], 1, "", "boom")
        with windows(), mock.patch("cyber_health.update.subprocess.run", return_value=failed):
            updater = self.updater()
            with self.assertRaises(UpdaterError):
                updater.upgrade_package()
        self.assertEqual({p.name for p in self.scripts.iterdir()}, {"python.exe", "cyber-health.exe", "cyber-health-mcp.exe"})

    def test_successful_upgrade_replaces_locked_launchers(self) -> None:
        def install(*_args, **_kwargs):
            for name in ("cyber-health.exe", "cyber-health-mcp.exe"):
                self.assertFalse((self.scripts / name).exists(), "launcher must be moved aside before install")
                (self.scripts / name).write_text("new", encoding="utf-8")
            return subprocess.CompletedProcess([], 0, "", "")

        with windows(), mock.patch("cyber_health.update.subprocess.run", side_effect=install):
            updater = self.updater()
            self.assertTrue(updater.upgrade_package())
        self.assertEqual((self.scripts / "cyber-health.exe").read_text(encoding="utf-8"), "new")
        self.assertEqual({p.name for p in self.scripts.iterdir()}, {"python.exe", "cyber-health.exe", "cyber-health-mcp.exe"})


if __name__ == "__main__":
    unittest.main()
