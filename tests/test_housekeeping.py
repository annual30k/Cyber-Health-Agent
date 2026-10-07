"""Private permissions, backup retention and temp-file cleanup for an installation."""

from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from pathlib import Path

from cyber_health.housekeeping import run_housekeeping


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class HousekeepingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name).resolve() / ".cyber-health"
        self.data = self.target / "data"
        self.backups = self.data / "backups"
        self.backups.mkdir(parents=True)
        (self.target / "config").mkdir()
        self.db = self.data / "cyber-health.sqlite3"
        self.db.write_bytes(b"db")
        (self.target / "config" / "installation.json").write_text("{}", encoding="utf-8")

    def rolling(self, count: int) -> list[Path]:
        paths = []
        for i in range(count):
            path = self.backups / f"cyber-health-backup-202610{i + 1:02d}_120000.sqlite3"
            path.write_bytes(b"b")
            paths.append(path)
        return paths

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_installation_tree_becomes_owner_only(self) -> None:
        for path in (self.target, self.data, self.backups):
            path.chmod(0o755)
        self.db.chmod(0o644)
        status = run_housekeeping(self.target)
        self.assertEqual(status.errors, [])
        for path in (self.target, self.data, self.backups, self.target / "config"):
            self.assertEqual(mode(path), 0o700, path)
        self.assertEqual(mode(self.db), 0o600)
        self.assertEqual(mode(self.target / "config" / "installation.json"), 0o600)
        self.assertEqual(run_housekeeping(self.target).permissions_tightened, [])

    def test_keeps_newest_rolling_backups_and_every_other_file(self) -> None:
        backups = self.rolling(12)
        pre_migration = self.backups / "cyber-health-pre-owner-migration-20260918_145841.sqlite3"
        pre_migration.write_bytes(b"keep")
        notes = self.backups / "my-notes.txt"
        notes.write_text("keep", encoding="utf-8")

        status = run_housekeeping(self.target, keep_backups=10)

        self.assertEqual(sorted(status.backups_pruned), sorted(p.name for p in backups[:2]))
        self.assertTrue(all(p.exists() for p in backups[2:]))
        self.assertFalse(any(p.exists() for p in backups[:2]))
        self.assertTrue(pre_migration.exists())
        self.assertTrue(notes.exists())

    def test_dry_run_reports_without_changing_anything(self) -> None:
        backups = self.rolling(12)
        status = run_housekeeping(self.target, keep_backups=10, dry_run=True)
        self.assertEqual(len(status.backups_pruned), 2)
        self.assertTrue(all(p.exists() for p in backups))

    def test_only_orphaned_migration_sidecars_are_removed(self) -> None:
        orphan_wal = self.data / "cyber-health.tmp_migration-wal"
        orphan_shm = self.data / "cyber-health.tmp_migration-shm"
        for path in (orphan_wal, orphan_shm):
            path.write_bytes(b"")
        live = self.data / "other.tmp_migration"
        live_wal = self.data / "other.tmp_migration-wal"
        live.write_bytes(b"in progress")
        live_wal.write_bytes(b"")

        status = run_housekeeping(self.target)

        self.assertEqual(sorted(status.temp_files_removed), sorted([orphan_wal.name, orphan_shm.name]))
        self.assertFalse(orphan_wal.exists() or orphan_shm.exists())
        self.assertTrue(live.exists() and live_wal.exists())


if __name__ == "__main__":
    unittest.main()
