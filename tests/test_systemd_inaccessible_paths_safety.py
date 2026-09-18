"""Regression test for a real production failure: dek-activator.service and
dek-review-publish.service both listed /run/systemd in InaccessiblePaths.
systemd's own namespace setup uses /run/systemd/inaccessible/{dir,file,...}
as the SOURCE for every other InaccessiblePaths/read-only bind-mount it
constructs; hiding /run/systemd itself partway through that setup makes
later mounts in the same unit fail with no specific error message, and the
unit exits status=226/NAMESPACE before its ExecStart ever runs. This was
never caught by `systemd-analyze verify` (a syntax check only) and never
exercised for real: dek-activator.service was only ever invoked in this
session by calling the Activator class directly in Python, never through
systemd itself, and dek-review-publish.service had never been triggered
at all before the first real click of the review UI's publish button.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO / "deploy" / "systemd"


def _inaccessible_entries(unit_text: str) -> list[str]:
    match = re.search(r"(?m)^InaccessiblePaths=(.*)$", unit_text)
    return match.group(1).split() if match else []


class InaccessiblePathsNeverHideRunSystemdTests(unittest.TestCase):
    def test_no_unit_marks_run_systemd_itself_inaccessible(self):
        offenders = {}
        for unit in sorted(SYSTEMD_DIR.glob("*.service")):
            entries = _inaccessible_entries(unit.read_text(encoding="utf-8"))
            if "/run/systemd" in entries:
                offenders[unit.name] = entries
        self.assertEqual({}, offenders,
                         "/run/systemd must never be an InaccessiblePaths entry -- "
                         "systemd needs it to construct every other inaccessible/read-only mount in the same unit")


@unittest.skipUnless(shutil.which("systemd-run"), "requires systemd-run")
class RealSandboxSetupSucceedsTests(unittest.TestCase):
    def test_dek_review_publish_sandbox_directives_actually_start(self):
        """Reproduces the real failure end-to-end: the exact ReadOnlyPaths/
        ReadWritePaths/InaccessiblePaths from dek-review-publish.service,
        run for real via systemd-run, must exit 0 -- not 226/NAMESPACE."""
        unit = (SYSTEMD_DIR / "dek-review-publish.service").read_text(encoding="utf-8")

        def line(name):
            match = re.search(rf"(?m)^{name}=(.*)$", unit)
            return match.group(1) if match else None

        result = subprocess.run([
            "systemd-run", "--pipe", "--wait", "--collect",
            "-p", "User=dek-publisher", "-p", "Group=dek-publisher",
            "-p", f"SupplementaryGroups={line('SupplementaryGroups')}",
            "-p", "NoNewPrivileges=true", "-p", "PrivateTmp=true", "-p", "PrivateDevices=true",
            "-p", "ProtectSystem=strict", "-p", "ProtectHome=true",
            "-p", "CapabilityBoundingSet=", "-p", "AmbientCapabilities=",
            "-p", f"ReadOnlyPaths={line('ReadOnlyPaths')}",
            "-p", f"ReadWritePaths={line('ReadWritePaths')}",
            "-p", f"InaccessiblePaths={line('InaccessiblePaths')}",
            "/bin/true",
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
