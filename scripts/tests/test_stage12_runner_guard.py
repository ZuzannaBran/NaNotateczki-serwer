"""Static guard against invalid usernames in the isolated E2E fixture."""
import re
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "run-stage12-real.sh"


class Stage12RunnerUsernameTest(unittest.TestCase):
    def test_disposable_username_respects_database_constraint(self):
        source = RUNNER.read_text()
        match = re.search(
            r"'s12_'\s*\|\|\s*substr\(replace\(:'owner_id', '-', ''\),\s*1,\s*(\d+)\)",
            source,
        )
        self.assertIsNotNone(match, "Runner must bound generated usernames")
        maximum_suffix = int(match.group(1))
        self.assertGreater(maximum_suffix, 0)
        self.assertLessEqual(4 + maximum_suffix, 32)
        username = "s12_" + "a" * maximum_suffix
        self.assertRegex(username, r"^[a-z0-9_]{3,32}$")

    def test_fixture_requires_disposable_account_protocol_marker(self):
        source = RUNNER.read_text()
        self.assertIn("INSERT INTO account_sync_protocol", source)
        self.assertIn("nanotateczki_stage12_test", source)
        self.assertIn("NN_STAGE12_ACCESS_TOKEN", source)
        self.assertNotIn("export ALLOW_REGISTRATION=true", source)


if __name__ == "__main__":
    unittest.main()
