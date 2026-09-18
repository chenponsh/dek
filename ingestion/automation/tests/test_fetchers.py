"""Regression test for a real production failure: under the sandboxed
dek-source-ingest-proof.service (ProtectHome=true, PrivateTmp=true),
Chromium's crashpad handler refuses to launch at all with
"chrome_crashpad_handler: --database is required" -- reproduced on this
session's first real CDE fetch attempt, after Playwright itself and the
vendored Chromium binary were already wired up correctly. An explicit
--crash-dumps-dir was tried first and did NOT fix it (confirmed by direct
reproduction against the real vendored binary); --disable-crash-reporter
does. fetch_cde() must pass that flag.
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from ingestion.automation import fetchers
from ingestion.automation.fetchers import CDEBrowserUnavailable


class FetchCdeDisablesCrashReporterTests(unittest.TestCase):
    def test_launch_disables_the_crash_reporter(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile_dir = Path(temporary) / "cde-profile"
            captured = {}
            # _full_chromium() globs Path.home()/.cache/ms-playwright for a real
            # binary; under a sandboxed build gate (HOME=/var/empty, no cached
            # Chromium) it raises SafetyStop before launch_persistent_context is
            # ever called, so without patching it the assertion below silently
            # never exercised the mock at all -- it only "passed" in a dev shell
            # that happened to have a real cached Chromium under $HOME.
            self.enterContext(patch.object(fetchers, "_full_chromium", return_value=Path("/fake/chromium")))

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

            self.assertIn("--disable-crash-reporter", captured["args"])


class FetchCdeLaunchFailureIsASafeSkipTests(unittest.TestCase):
    def test_launch_failure_raises_cde_browser_unavailable_not_a_bare_error(self):
        """cli.py's caller only treats CDEBrowserUnavailable as a safe,
        non-blocking skip; any other exception sets blocking=True and
        refuses ALL writes, including from unrelated sources that already
        succeeded. A launch failure (crashpad, missing display, etc.) is a
        browser-unavailable condition exactly like the post-launch checks
        already handled this way -- it must not propagate as a bare error."""
        with tempfile.TemporaryDirectory() as temporary:
            profile_dir = Path(temporary) / "cde-profile"
            self.enterContext(patch.object(fetchers, "_full_chromium", return_value=Path("/fake/chromium")))

            fake_module = types.ModuleType("playwright.sync_api")
            fake_module.Error = RuntimeError

            class _FakeSyncPlaywright:
                def __enter__(self_inner):
                    pw = MagicMock()
                    pw.chromium.launch_persistent_context.side_effect = RuntimeError("Target page, context or browser has been closed")
                    return pw
                def __exit__(self_inner, *exc_info):
                    return False

            fake_module.sync_playwright = _FakeSyncPlaywright
            sys.modules["playwright"] = types.ModuleType("playwright")
            sys.modules["playwright.sync_api"] = fake_module
            self.addCleanup(sys.modules.pop, "playwright", None)
            self.addCleanup(sys.modules.pop, "playwright.sync_api", None)

            with self.assertRaises(CDEBrowserUnavailable):
                fetchers.fetch_cde("https://www.cde.org.cn/", [1], profile_dir)


if __name__ == "__main__":
    unittest.main()
