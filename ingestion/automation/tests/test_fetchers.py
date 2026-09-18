"""Regression test for a real production failure: under the sandboxed
dek-source-ingest-proof.service (ProtectHome=true, PrivateTmp=true),
Chromium's crashpad handler can't auto-locate a database directory and
refuses to launch at all with "chrome_crashpad_handler: --database is
required" -- reproduced on this session's first real CDE fetch attempt,
after Playwright itself and the vendored Chromium binary were already
wired up correctly. fetch_cde() must pass an explicit, already-created,
writable --crash-dumps-dir instead of relying on Chromium's defaults.
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from ingestion.automation import fetchers


class FetchCdeCrashDumpsDirTests(unittest.TestCase):
    def test_launch_receives_an_explicit_writable_crash_dumps_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile_dir = Path(temporary) / "cde-profile"
            captured = {}

            def fake_launch_persistent_context(user_data_dir, **kwargs):
                captured["args"] = kwargs.get("args", [])
                context = MagicMock()
                context.pages = []
                context.new_page.return_value = MagicMock(goto=MagicMock(side_effect=RuntimeError("stop after launch")))
                return context

            fake_module = types.ModuleType("playwright.sync_api")
            fake_module.Error = RuntimeError

            class _FakeSyncPlaywright:
                def __enter__(self_inner):
                    pw = MagicMock()
                    pw.chromium.launch_persistent_context.side_effect = fake_launch_persistent_context
                    return pw
                def __exit__(self_inner, *exc_info):
                    return False

            fake_module.sync_playwright = _FakeSyncPlaywright
            sys.modules["playwright"] = types.ModuleType("playwright")
            sys.modules["playwright.sync_api"] = fake_module
            self.addCleanup(sys.modules.pop, "playwright", None)
            self.addCleanup(sys.modules.pop, "playwright.sync_api", None)

            with self.assertRaises(RuntimeError):
                fetchers.fetch_cde("https://www.cde.org.cn/", [1], profile_dir)

            crash_flags = [arg for arg in captured["args"] if arg.startswith("--crash-dumps-dir=")]
            self.assertEqual(len(crash_flags), 1, captured["args"])
            crash_dir = Path(crash_flags[0].split("=", 1)[1])
            self.assertTrue(crash_dir.is_dir())
            self.assertTrue(crash_dir.is_relative_to(profile_dir))


if __name__ == "__main__":
    unittest.main()
