"""Tests for legacy user partition migration to single-owner architecture."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cyber_health.migrate import migrate_database_to_owner
from cyber_health.store import SINGLE_USER_ID, SQLiteStore
from cyber_health_mcp.server import assert_single_user_database


class TestMigrateOwner(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cyber-health.sqlite3"
        self.store = SQLiteStore(self.db_path)

        # Seed legacy partition with user_id = "u_default"
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO user_profile (user_id, timezone, goals_json, updated_at)
                   VALUES ('u_default', 'Asia/Shanghai', '{"target_kcal_low": 2000, "target_kcal_high": 2500}', '2026-09-01T00:00:00Z')"""
            )
            conn.execute(
                """INSERT INTO meal_log (
                    meal_id, user_id, occurred_at, meal_type, foods_json, kcal_low, kcal_high,
                    protein_low, protein_high, status, causation_id, state_version, created_at
                ) VALUES (
                    'meal_test_1', 'u_default', '2026-09-01T12:00:00Z', 'lunch', '[]', 500, 600,
                    25, 30, 'active', 'op_init', 1, '2026-09-01T12:00:00Z'
                )"""
            )
            conn.execute(
                """INSERT INTO operation_log (
                    operation_id, user_id, idempotency_key, request_hash, action,
                    result_status, before_version, after_version, response_json, created_at
                ) VALUES (
                    'op_init', 'u_default', 'key_1', 'hash_1', 'log_meal',
                    'success', 0, 1, '{}', '2026-09-01T12:00:00Z'
                )"""
            )
            conn.execute(
                """INSERT INTO domain_record (
                    record_id, user_id, kind, day, body_json, status, causation_id, state_version, created_at
                ) VALUES (
                    'rec_1', 'u_default', 'metrics', '2026-09-01', '{}', 'active', 'op_init', 1, '2026-09-01T12:00:00Z'
                )"""
            )
            conn.execute(
                """INSERT INTO schedule_event (
                    event_id, user_id, event_type, window_start, window_end, status, created_at, updated_at
                ) VALUES (
                    'evt_1', 'u_default', 'daily_review', '2026-09-01T20:00:00Z', '2026-09-01T21:00:00Z',
                    'pending', '2026-09-01T12:00:00Z', '2026-09-01T12:00:00Z'
                )"""
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_server_assertion_fails_before_migration(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            assert_single_user_database(self.db_path)
        self.assertIn("Legacy user_id partitions found", str(ctx.exception))

    def test_dry_run_migration(self) -> None:
        report = migrate_database_to_owner(self.db_path, dry_run=True)
        self.assertTrue(report.success)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.from_user, "u_default")
        self.assertEqual(report.to_user, SINGLE_USER_ID)
        self.assertIsNone(report.backup_path)
        self.assertEqual(report.migrated_counts["meal_log"], 1)

        # Still fails assertion because dry_run made no changes
        with self.assertRaises(RuntimeError):
            assert_single_user_database(self.db_path)

    def test_actual_migration_succeeds_and_creates_backup(self) -> None:
        report = migrate_database_to_owner(self.db_path, dry_run=False)
        self.assertTrue(report.success)
        self.assertFalse(report.dry_run)
        self.assertEqual(report.from_user, "u_default")
        self.assertEqual(report.to_user, SINGLE_USER_ID)
        self.assertIsNotNone(report.backup_path)
        self.assertTrue(Path(report.backup_path).is_file())

        # Server assertion must now pass with zero errors
        assert_single_user_database(self.db_path)

        # Verify data now belongs to owner
        with self.store.connect() as conn:
            profile = conn.execute("SELECT * FROM user_profile WHERE user_id = ?", (SINGLE_USER_ID,)).fetchone()
            self.assertIsNotNone(profile)
            self.assertEqual(profile["timezone"], "Asia/Shanghai")

            meal = conn.execute("SELECT * FROM meal_log WHERE user_id = ?", (SINGLE_USER_ID,)).fetchone()
            self.assertIsNotNone(meal)
            self.assertEqual(meal["meal_id"], "meal_test_1")

            old_records = conn.execute("SELECT COUNT(*) FROM meal_log WHERE user_id = 'u_default'").fetchone()[0]
            self.assertEqual(old_records, 0)

    def test_cli_migrate_owner(self) -> None:
        from cyber_health.cli import main
        # Run with dry run first
        code = main(["migrate-owner", "--db", str(self.db_path), "--dry-run"])
        self.assertEqual(code, 0)

        # Run actual migration
        code = main(["migrate-owner", "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        assert_single_user_database(self.db_path)


if __name__ == "__main__":
    unittest.main()
