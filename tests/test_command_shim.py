"""The `cyber-health` command entry point placed in the user's bin directory."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cyber_health.command_shim import (
    _shim_path,
    _target_path,
    command_shim_note,
    ensure_command_shim,
    remove_command_shim,
)

POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "symlink ownership semantics are POSIX-specific")


class CommandShimTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.install_dir = root / ".cyber-health"
        self.venv_bin = self.install_dir / "venv" / ("Scripts" if sys.platform == "win32" else "bin")
        self.venv_bin.mkdir(parents=True)
        self.target = _target_path(self.venv_bin)
        self.target.write_text("#!/bin/sh\n", encoding="utf-8")
        self.bin_dir = root / "home" / ".local" / "bin"
        self.shim = _shim_path(self.bin_dir)
        path_env = mock.patch.dict(os.environ, {"PATH": os.pathsep.join([str(self.bin_dir), "/usr/bin"])})
        path_env.start()
        self.addCleanup(path_env.stop)

    def ensure(self, **kwargs):
        return ensure_command_shim(self.install_dir, self.venv_bin, bin_dir=self.bin_dir, **kwargs)

    def test_creates_bin_dir_and_link_when_missing(self) -> None:
        status = self.ensure()
        self.assertEqual(status.action, "linked")
        self.assertTrue(status.executed)
        self.assertTrue(status.on_path)
        self.assertEqual(status.path_hint, "")
        self.assertEqual(command_shim_note(status), "")
        if sys.platform == "win32":
            self.assertIn(str(self.target), self.shim.read_text(encoding="utf-8"))
        else:
            self.assertEqual(os.readlink(self.shim), str(self.target))

    def test_second_run_is_unchanged(self) -> None:
        self.ensure()
        status = self.ensure()
        self.assertEqual(status.action, "unchanged")
        self.assertFalse(status.executed)

    @POSIX_ONLY
    def test_repairs_a_stale_link_into_this_installation(self) -> None:
        self.bin_dir.mkdir(parents=True)
        self.shim.symlink_to(self.install_dir / "old-venv" / "bin" / "cyber-health")
        status = self.ensure()
        self.assertEqual(status.action, "linked")
        self.assertEqual(os.readlink(self.shim), str(self.target))

    def test_foreign_file_is_never_replaced(self) -> None:
        self.bin_dir.mkdir(parents=True)
        self.shim.write_text("someone else's cyber-health\n", encoding="utf-8")
        status = self.ensure()
        self.assertEqual(status.action, "refused")
        self.assertIn("left untouched", command_shim_note(status))
        self.assertEqual(self.shim.read_text(encoding="utf-8"), "someone else's cyber-health\n")

    @POSIX_ONLY
    def test_foreign_symlink_is_never_replaced(self) -> None:
        self.bin_dir.mkdir(parents=True)
        self.shim.symlink_to("/opt/other/cyber-health")
        self.assertEqual(self.ensure().action, "refused")
        self.assertEqual(os.readlink(self.shim), "/opt/other/cyber-health")

    def test_dry_run_plans_without_writing(self) -> None:
        status = self.ensure(dry_run=True)
        self.assertEqual(status.action, "planned")
        self.assertFalse(self.bin_dir.exists())

    def test_missing_installed_command_is_reported(self) -> None:
        self.target.unlink()
        status = self.ensure()
        self.assertEqual(status.action, "error")
        self.assertFalse(self.shim.exists())

    def test_bin_dir_off_path_yields_a_hint_instead_of_editing_shell_files(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            status = self.ensure()
        self.assertEqual(status.action, "linked")
        self.assertFalse(status.on_path)
        self.assertIn(str(self.bin_dir), status.path_hint)
        self.assertIn(status.path_hint, command_shim_note(status))

    def test_remove_deletes_only_this_installations_link(self) -> None:
        self.ensure()
        self.assertEqual(remove_command_shim(self.install_dir, bin_dir=self.bin_dir, dry_run=True).action, "planned")
        self.assertTrue(self.shim.exists())
        status = remove_command_shim(self.install_dir, bin_dir=self.bin_dir)
        self.assertEqual(status.action, "removed")
        self.assertFalse(self.shim.exists() or self.shim.is_symlink())
        self.assertEqual(remove_command_shim(self.install_dir, bin_dir=self.bin_dir).action, "none")

    def test_remove_refuses_a_foreign_file(self) -> None:
        self.bin_dir.mkdir(parents=True)
        self.shim.write_text("foreign\n", encoding="utf-8")
        status = remove_command_shim(self.install_dir, bin_dir=self.bin_dir)
        self.assertEqual(status.action, "refused")
        self.assertTrue(self.shim.exists())


if __name__ == "__main__":
    unittest.main()
