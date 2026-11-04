"""Stage 14 read-only audit; no PostgreSQL or Docker is contacted."""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
AUDIT = SCRIPTS / "stage14-retirement-audit.sh"
STATUS = SCRIPTS / "stage13-status.sh"

EMPTY = {
    "legacy_accounts": 0,
    "v2_accounts": 1,
    "legacy_notebooks": 0,
    "v2_documents": 2,
    "active_legacy_shares": 0,
    "dual_protocol_documents": 0,
    "invalid_actor_checkpoints": 0,
}


class Stage14RetirementAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.capture = root / "audit.sql"
        docker = root / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            "cat >\"$MOCK_SQL_CAPTURE\"\n"
            "if [[ \"$MOCK_DB_ERROR\" == 1 ]]; then exit 2; fi\n"
            "printf '%s\\n' \"$MOCK_DB_REPORT\"\n"
        )
        docker.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=str(root) + os.pathsep + os.environ["PATH"],
            MOCK_SQL_CAPTURE=str(self.capture),
            MOCK_DB_REPORT=json.dumps(EMPTY),
            MOCK_DB_ERROR="0",
        )

    def run_audit(self, report=None, **env_overrides):
        env = dict(self.env, **env_overrides)
        if report is not None:
            env["MOCK_DB_REPORT"] = (
                report if isinstance(report, str) else json.dumps(report)
            )
        return subprocess.run(
            ["bash", str(AUDIT)],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_clean_server_is_only_a_prerequisite(self):
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NOT approved", result.stdout)
        self.assertIn("v2_accounts: 1", result.stdout)

    def test_every_legacy_or_inconsistent_count_blocks(self):
        for name in (
            "legacy_accounts", "legacy_notebooks", "active_legacy_shares",
            "dual_protocol_documents", "invalid_actor_checkpoints",
        ):
            with self.subTest(name=name):
                report = dict(EMPTY, **{name: 1})
                result = self.run_audit(report)
                self.assertEqual(result.returncode, 75, result.stdout)
                self.assertIn("BLOCKED", result.stderr)

    def test_bad_response_or_missing_table_fails_closed(self):
        for response in ("", "BEGIN\n{}\nCOMMIT", "not-json",
                         json.dumps({"legacy_accounts": 0})):
            with self.subTest(response=response):
                self.assertEqual(self.run_audit(response).returncode, 75)
        self.assertNotEqual(
            self.run_audit(MOCK_DB_ERROR="1").returncode, 0
        )

    def test_negative_and_boolean_counts_are_rejected(self):
        for number in (-1, True, "0"):
            with self.subTest(number=number):
                result = self.run_audit(dict(EMPTY, v2_accounts=number))
                self.assertEqual(result.returncode, 75)

    def test_query_does_not_mutate_production_database(self):
        self.assertEqual(self.run_audit().returncode, 0)
        query = self.capture.read_text()
        self.assertIn(
            "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;",
            query,
        )
        self.assertIn("COMMIT;", query)
        self.assertIn("account_sync_protocol", query)
        self.assertNotRegex(
            query, r"(?im)^\s*(INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE)\b"
        )

    def test_stage13_monitor_sql_delimiters_are_valid(self):
        body = STATUS.read_text()
        self.assertGreaterEqual(len(re.findall(r"(?m)^DO \$\$$", body)), 2)
        self.assertGreaterEqual(len(re.findall(r"(?m)^END \$\$;", body)), 2)
        self.assertNotRegex(body, r"(?m)^DO \$$|^END \$;")
        self.assertEqual(
            subprocess.run(
                ["bash", "-n", str(STATUS)], check=False
            ).returncode, 0
        )


if __name__ == "__main__":
    unittest.main()
