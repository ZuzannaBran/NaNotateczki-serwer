"""Offline activation guard tests. No Docker or database is contacted."""
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "stage13-activate-empty-canary.sh"


class CanaryActivationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        fake = root / "docker"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1 $2\" == \"compose ps\" ]]; then\n"
            "  if [[ \"${MOCK_API_RUNNING:-0}\" == 1 ]]; then echo api; fi\n"
            "  if [[ \"${MOCK_PS_EXIT:-0}\" != 0 ]]; then exit 2; fi\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"$1 $2\" == \"compose exec\" ]]; then\n"
            "  cat >/dev/null\n"
            "  if [[ \"${MOCK_DB_EXIT:-0}\" != 0 ]]; then exit 2; fi\n"
            "  printf '%s\\n' \"${MOCK_DB_RETURN:-}\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 2\n"
        )
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"])
        self.owner = str(uuid.uuid4())

    def run_script(self, owner=None, **overrides):
        env = dict(self.env, **overrides)
        return subprocess.run(
            ["bash", str(SCRIPT), owner or self.owner],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_invalid_identity_is_rejected(self):
        result = self.run_script("not-uuid")
        self.assertEqual(result.returncode, 64)

    def test_api_must_be_stopped(self):
        result = self.run_script(MOCK_API_RUNNING="1")
        self.assertEqual(result.returncode, 75)
        self.assertIn("API is running", result.stderr)

    def test_compose_status_failure_blocks_activation(self):
        result = self.run_script(MOCK_PS_EXIT="1", MOCK_DB_RETURN=self.owner)
        self.assertEqual(result.returncode, 69)
        self.assertIn("Cannot verify stopped API", result.stderr)

    def test_only_single_verified_insert_counts_as_success(self):
        result = self.run_script(MOCK_DB_RETURN=self.owner)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Activated EMPTY test account", result.stdout)

    def test_no_account_activated_fails_closed(self):
        result = self.run_script(MOCK_DB_RETURN="")
        self.assertEqual(result.returncode, 75)
        self.assertIn("No activation", result.stderr)

    def test_database_error_is_not_silenced(self):
        result = self.run_script(MOCK_DB_EXIT="1")
        self.assertNotEqual(result.returncode, 0)

    def test_sql_filters_all_legacy_material_and_settings(self):
        body = SCRIPT.read_text()
        for table in (
            "document_states", "document_operations",
            "device_backups", "account_settings",
            "planner_entries", "planner_entry_mutations",
            "actor_checkpoints", "object_tombstones",
            "study_shares", "verified_assets",
        ):
            self.assertIn("FROM " + table, body)
        self.assertIn("Unknown account tables", body)


if __name__ == "__main__":
    unittest.main()
