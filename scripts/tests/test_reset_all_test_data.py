"""Run the reset against an isolated fake Docker installation only."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "reset-all-test-data.sh"


class TestResetAllTestData(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / "data" / "backups").mkdir(parents=True)
        (self.root / "data" / "backups" / "old.json").write_text("old")
        (self.root / "compose.yaml").write_text("name: nanotateczki\n")
        (self.root / "scripts" / "reset-all-test-data.sh").write_bytes(SCRIPT.read_bytes())
        (self.root / "scripts" / "apply-migrations.sh").write_text(
            '#!/bin/bash\nprintf "migrate\\n" >> "$COMMAND_LOG"\n'
        )
        bindir = self.root / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(
            '#!/bin/bash\nprintf "docker %s\\n" "$*" >> "$COMMAND_LOG"\n'
            'if [[ "$FAIL_DOCKER_DOWN" == "1" && "$*" == "compose down --volumes" ]]; then exit 1; fi\n'
        )
        docker.chmod(0o755)
        self.log = self.root / "commands.log"
        self.env = dict(os.environ, PATH=str(bindir) + ":" + os.environ["PATH"],
                        COMMAND_LOG=str(self.log), FAIL_DOCKER_DOWN="0")

    def run_script(self, *args, **updates):
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "reset-all-test-data.sh"), *args],
            env=dict(self.env, **updates), capture_output=True, text=True,
            check=False,
        )

    def test_requires_wipe_flag(self):
        self.assertEqual(self.run_script().returncode, 64)
        self.assertTrue((self.root / "data/backups/old.json").exists())

    def test_wipe_and_reinitialization_order(self):
        result = self.run_script("--delete-all-test-data")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.root / "data").iterdir()), [])
        log = self.log.read_text()
        self.assertIn("docker compose down --volumes", log)
        self.assertIn("migrate", log)
        self.assertLess(log.index("down --volumes"), log.index("migrate"))
        self.assertIn("docker compose up -d --build", log)

    def test_docker_failure_never_removes_files(self):
        self.assertNotEqual(
            self.run_script("--delete-all-test-data", FAIL_DOCKER_DOWN="1").returncode, 0
        )
        self.assertTrue((self.root / "data/backups/old.json").exists())

    def test_wrong_checkout_refuses_wipe(self):
        (self.root / "compose.yaml").write_text("name: other\n")
        self.assertEqual(self.run_script("--delete-all-test-data").returncode, 75)
        self.assertTrue((self.root / "data/backups/old.json").exists())

    def test_symlink_refuses_wipe(self):
        target = self.root / "outside"
        target.mkdir()
        (target / "important").write_text("keep")
        (self.root / "data/backups/old.json").unlink()
        (self.root / "data/backups").rmdir()
        (self.root / "data").rmdir()
        (self.root / "data").symlink_to(target, target_is_directory=True)
        self.assertEqual(self.run_script("--delete-all-test-data").returncode, 75)
        self.assertEqual((target / "important").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
