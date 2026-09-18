"""Production invokes these scripts as `python3 -I .../deploy/<script>.py`
(see deploy/PRODUCTION_ROLLOUT.md and deploy/systemd/*.service). -I suppresses
Python's normal auto-add of the script's own directory to sys.path, which
silently breaks any `import fsutil` / `from deploy.X import Y` sibling-module
import that relies on that default (a real regression caught once already,
in deploy/readiness.py, by test_readiness_marker_lifetime.py's subprocess CLI
test). This locks the same guarantee in for every deploy/*.py script that is
directly executable and imports a sibling deploy module."""
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCRIPTS = ("deploy/a6_cutover.py", "deploy/install_components.py",
          "deploy/qa_profile.py", "deploy/readiness.py", "deploy/publisher_entrypoint.py",
          "deploy/credential_gate.py", "deploy/builder_entrypoint.py", "deploy/rollback.py")


class IsolatedInvocationImportTests(unittest.TestCase):
    def test_help_does_not_fail_to_import_under_isolated_mode(self):
        for script in SCRIPTS:
            with self.subTest(script=script):
                result = subprocess.run(
                    [sys.executable, "-I", script, "--help"],
                    cwd=REPO_ROOT, capture_output=True, text=True, check=False,
                )
                self.assertNotIn("ModuleNotFoundError", result.stderr, result.stderr)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
