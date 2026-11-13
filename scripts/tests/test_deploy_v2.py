"""Exercise the deploy order without touching a real server."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy-v2.sh"


class DeployV2Test(unittest.TestCase):
    def run_deployment(self, configured_mode, override=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(SCRIPT, scripts / SCRIPT.name)
            (root / ".env").write_text(
                f"OPERATIONS_V2_WRITE_MODE={configured_mode}\n"
            )
            (scripts / "apply-migrations.sh").write_text('echo migrations >> "$TRACE"\n')
            (scripts / "stage13-status.sh").write_text('echo status >> "$TRACE"\n')
            fake = root / "docker"
            fake.write_text(
                '#!/bin/sh\n'
                'configured=$(sed -n '
                "'s/^OPERATIONS_V2_WRITE_MODE=//p' .env)\n"
                'mode=${OPERATIONS_V2_WRITE_MODE:-${configured:-disabled}}\n'
                'printf "%s %s\\n" "$mode" "$*" >> "$TRACE"\n'
            )
            fake.chmod(0o755)
            trace = root / "trace"
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                   "TRACE": str(trace)}
            env.pop("OPERATIONS_V2_WRITE_MODE", None)
            if override is not None:
                env["OPERATIONS_V2_WRITE_MODE"] = override
            result = subprocess.run(
                ["bash", str(scripts / SCRIPT.name)],
                env=env,
                check=True, capture_output=True, text=True,
            )
            lines = trace.read_text().splitlines()
            return result, lines

    def test_migrates_only_after_stopping_api_and_preserves_write_mode(self):
        for mode in ("disabled", "canary", "all"):
            with self.subTest(mode=mode):
                result, lines = self.run_deployment(mode)
                self.assert_deployment_order(result, lines, mode)

    def test_explicit_environment_override_keeps_compose_precedence(self):
        result, lines = self.run_deployment("all", override="canary")
        self.assert_deployment_order(result, lines, "canary")

    def assert_deployment_order(self, result, lines, mode):
        self.assertEqual(lines[:5], [
            f"{mode} compose build api",
            f"{mode} compose up -d --wait db",
            f"{mode} compose stop api", "migrations",
            f"{mode} compose up -d api caddy",
        ])
        self.assertIn("/ready", lines[5])
        self.assertEqual(lines[6:], ["status"])
        self.assertIn("V2 API ready", result.stdout)
