"""Exercise the deploy order without touching a real server."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy-v2.sh"


class DeployV2Test(unittest.TestCase):
    def test_migrates_only_after_stopping_api_and_uses_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
            (root / ".env").write_text("OPERATIONS_V2_WRITE_MODE=disabled\n")
            (scripts / "apply-migrations.sh").write_text('echo migrations >> "$TRACE"\n')
            (scripts / "stage13-status.sh").write_text('echo status >> "$TRACE"\n')
            fake = root / "docker"
            fake.write_text('#!/bin/sh\nprintf "%s %s\\n" "$OPERATIONS_V2_WRITE_MODE" "$*" >> "$TRACE"\n')
            fake.chmod(0o755)
            trace = root / "trace"
            result = subprocess.run(
                ["bash", str(scripts / SCRIPT.name)],
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "TRACE": str(trace)},
                check=True, capture_output=True, text=True,
            )
            lines = trace.read_text().splitlines()
            self.assertEqual(lines[:5], [
                "all compose build api", "all compose up -d --wait db",
                "all compose stop api", "migrations", "all compose up -d api caddy",
            ])
            self.assertIn("/ready", lines[5])
            self.assertEqual(lines[6:], ["status"])
            self.assertIn("V2 API ready", result.stdout)
