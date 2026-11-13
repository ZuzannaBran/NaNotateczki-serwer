"""Regression checks for the destructive, per-account v2 reset procedure.

These tests use a fake Docker executable; they never contact PostgreSQL.
"""
import os
import re
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "stage14-reset-test-account-v2.sh"


class ResetTestAccountTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.sql_path = folder / "submitted.sql"
        docker = folder / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1 $2\" == 'compose ps' ]]; then\n"
            "  if [[ \"${MOCK_PS_ERROR:-0}\" == 1 ]]; then exit 2; fi\n"
            "  if [[ \"${MOCK_API_RUNNING:-0}\" == 1 ]]; then echo api; fi\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2\" == 'compose exec' ]]; then\n"
            "  cat > \"$MOCK_SQL_PATH\"\n"
            "  if [[ \"${MOCK_DB_ERROR:-0}\" == 1 ]]; then exit 1; fi\n"
            "  if [[ \"${MOCK_BAD_RECEIPT:-0}\" == 1 ]]; then\n"
            "    echo unexpected\n"
            "  else\n"
            "    echo \"MIGRATED:$MOCK_OWNER\"\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "exit 2\n"
        )
        docker.chmod(0o755)
        self.owner = str(uuid.uuid4())
        self.env = dict(
            os.environ,
            PATH=str(folder) + os.pathsep + os.environ["PATH"],
            MOCK_SQL_PATH=str(self.sql_path),
            MOCK_OWNER=self.owner,
            MOCK_PS_ERROR="0",
            MOCK_API_RUNNING="0",
            MOCK_DB_ERROR="0",
            MOCK_BAD_RECEIPT="0",
        )

    def run_reset(self, *args, **kwargs):
        if not args:
            args = (self.owner, "test_account", f"WIPE-{self.owner}")
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=dict(self.env, **kwargs),
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exact_confirmation_and_account_identity_required(self):
        for args in (
            (self.owner, "test_account", "WIPE-wrong"),
            (self.owner, "test_account", ""),
            ("anything", "test_account", "WIPE-anything"),
            (self.owner, "contains space", f"WIPE-{self.owner}"),
            (" ",),
        ):
            with self.subTest(args=args):
                result = self.run_reset(*args)
                self.assertEqual(result.returncode, 64)
        self.assertFalse(self.sql_path.exists())

    def test_running_api_and_failed_status_check_fail_closed(self):
        for variables, expected in (
            ({"MOCK_API_RUNNING": "1"}, 75),
            ({"MOCK_PS_ERROR": "1"}, 69),
        ):
            with self.subTest(variables=variables):
                result = self.run_reset(**variables)
                self.assertEqual(result.returncode, expected)
                self.assertFalse(self.sql_path.exists())

    def test_sql_failure_does_not_report_migration(self):
        result = self.run_reset(MOCK_DB_ERROR="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rolled back", result.stderr)

    def test_untrusted_or_missing_receipt_fails_closed(self):
        result = self.run_reset(MOCK_BAD_RECEIPT="1")
        self.assertEqual(result.returncode, 75)

    def test_success_reports_v2_write_mode(self):
        result = self.run_reset()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OPERATIONS_V2_WRITE_MODE=all", result.stdout)
        self.assertIn("all existing tokens invalidated", result.stdout)

    def test_sql_is_single_owner_scoped_and_transactional(self):
        self.assertEqual(self.run_reset().returncode, 0)
        sql = self.sql_path.read_text()
        self.assertIn("BEGIN;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertIn("FOR UPDATE", sql)
        self.assertIn("username does not match", sql)
        self.assertIn("account_sync_protocol", sql)
        self.assertIn("Unknown account tables", sql)
        self.assertIn("refresh_sessions", sql)
        self.assertIn(
            "UPDATE users SET auth_version = auth_version + 1 WHERE id = target;",
            sql,
        )
        deletes = re.findall(r"(?m)^\s*DELETE FROM [^;]+;", sql)
        self.assertGreaterEqual(len(deletes), 10)
        for stmt in deletes:
            self.assertIn("WHERE owner_id = target", stmt)
        self.assertNotRegex(sql, r"(?im)^\s*(DROP|TRUNCATE)\b")

    def test_script_is_valid_shell(self):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
