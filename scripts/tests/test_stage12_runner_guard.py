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

    def test_admin_fixture_is_separate_from_read_only_api_role(self):
        runner = RUNNER.read_text()
        fixture = (RUNNER.parent.parent / "api/tests/test_stage12_real_stack.py").read_text()
        self.assertIn('export DATABASE_URL="postgresql+asyncpg://nano_app:', runner)
        self.assertIn('export NN_STAGE12_FIXTURE_DATABASE_URL=', runner)
        self.assertIn("unset NN_STAGE12_FIXTURE_DATABASE_URL", runner)
        self.assertIn('fixture.username != "stage12_admin"', fixture)
        self.assertIn('url.username != "nano_app"', fixture)
        self.assertIn("fixture.port != url.port", fixture)
        self.assertIn('create_async_engine(os.environ["NN_STAGE12_FIXTURE_DATABASE_URL"])', fixture)
        self.assertNotIn("GRANT INSERT ON account_sync_protocol", runner)

    def test_fixture_requires_disposable_account_protocol_marker(self):
        source = RUNNER.read_text()
        self.assertIn("Fresh Stage 12 identity did not get the v2 marker", source)
        self.assertIn("nanotateczki_stage12_test", source)
        self.assertIn("NN_STAGE12_ACCESS_TOKEN", source)
        self.assertNotIn("export ALLOW_REGISTRATION=true", source)

    def test_flutter_peers_run_as_two_waited_processes(self):
        source = RUNNER.read_text()
        self.assertIn("run_peer a &", source)
        self.assertIn("run_peer b &", source)
        self.assertIn('wait "$peer_a" || failed=1', source)
        self.assertIn('wait "$peer_b" || failed=1', source)
        self.assertIn("test/stage12_two_process_live_test.dart", source)


if __name__ == "__main__":
    unittest.main()
