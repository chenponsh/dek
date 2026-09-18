"""Regression test for a real production failure: source_ingest_entrypoint.py
is always invoked as `python3 -I` (isolated mode, for security -- see its own
sibling scripts' comments), which by design makes the interpreter ignore
PYTHONPATH entirely. The systemd unit still sets
Environment=PYTHONPATH=/var/lib/dek-source-ingest/vendor/python to make the
vendored Playwright install importable, but under -I that env var was never
applied, so "ALERT: CDE source failure: Playwright is not installed" kept
happening even after PYTHONPATH was added to the unit.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class IsolatedModeStillAppliesVendorPythonpathTests(unittest.TestCase):
    def test_dash_i_still_makes_the_vendored_pythonpath_importable(self):
        with tempfile.TemporaryDirectory() as temporary:
            vendor = Path(temporary) / "vendor"
            vendor.mkdir()
            (vendor / "dek_vendor_marker.py").write_text("VALUE = 'vendored'\n", encoding="utf-8")

            script = REPO / "deploy" / "source_ingest_entrypoint.py"
            result = subprocess.run(
                [sys.executable, "-I", "-c",
                 f"import runpy; runpy.run_path({str(script)!r}, run_name='not_main'); "
                 "import dek_vendor_marker; print(dek_vendor_marker.VALUE)"],
                env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(vendor)},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("vendored", result.stdout)


if __name__ == "__main__":
    unittest.main()
