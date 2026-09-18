"""Regression test for a real production failure: source_ingest_entrypoint.py's
initial `git clone` builds its subprocess environment from an explicit,
fully-enumerated dict (subprocess.run(env=...) replaces the whole
environment rather than inheriting it), which never included the systemd
unit's HTTP(S)_PROXY/http(s)_proxy. This server cannot reach github.com
without that proxy, so dek-source-ingest-proof.service's first real run
hung for ~90s and then failed with a TLS reset trying to clone directly.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from deploy.source_ingest_entrypoint import main as entrypoint_main


class SourceIngestCloneForwardsProxyEnvTests(unittest.TestCase):
    def test_initial_clone_environment_includes_the_configured_proxy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proof_root = root / "proofs"; proof_root.mkdir(mode=0o700)
            os.chmod(proof_root, 0o700)  # mkdir's mode is subject to umask
            isolated_clone = root / "clones"; isolated_clone.mkdir()
            credential = root / "git-credentials"
            credential.write_text("https://alice:s3cret@github.com/chenponsh/dek.git", encoding="utf-8")
            os.chmod(credential, 0o440)  # matches real systemd LoadCredential delivery

            captured = {}

            def fake_run(command, **kwargs):
                if "clone" in command:
                    captured["env"] = kwargs.get("env")
                return mock.Mock(returncode=1)  # stop right after the clone; never touch the real network

            with mock.patch.dict(os.environ, {
                        "DEK_FIXED_ORIGIN": "https://github.com/chenponsh/dek.git",
                        "DEK_GIT_CREDENTIAL_FILE": str(credential),
                        "HTTP_PROXY": "http://127.0.0.1:7890",
                        "HTTPS_PROXY": "http://127.0.0.1:7890",
                        "http_proxy": "http://127.0.0.1:7890",
                        "https_proxy": "http://127.0.0.1:7890",
                    }), \
                    mock.patch("deploy.source_ingest_entrypoint.PROOF_ROOT", proof_root), \
                    mock.patch("deploy.source_ingest_entrypoint.subprocess.run", side_effect=fake_run):
                with self.assertRaises(SystemExit):
                    entrypoint_main([
                        "--isolated-clone", str(isolated_clone),
                        "--origin", "https://github.com/chenponsh/dek.git",
                        "--proof-output", str(proof_root / "staged-proof-report.json"),
                        "--expected-output", str(proof_root / "staged-proof-report.expected.json"),
                        "--package-root", str(REPO),
                        "--pre-cutover-proof",
                        "scheduled-run",
                    ])

            self.assertIn("env", captured, "the clone subprocess.run call was never made")
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                self.assertEqual(captured["env"].get(key), "http://127.0.0.1:7890", key)


class SourceIngestHomeIsWritableTests(unittest.TestCase):
    def test_home_is_a_real_writable_per_run_directory_not_var_empty(self):
        """HOME was hardcoded to /var/empty/dek-source-ingest -- deliberately
        unwritable -- for the initial git clone, and (since it was applied
        process-wide via os.environ.update(), not just that one subprocess
        call) stayed that way for everything running later in the same
        process, including the eventual Chromium launch inside fetch_cde().
        Chromium's startup needs a writable $HOME even with
        --disable-crash-reporter passed and refused to launch at all with
        "chrome_crashpad_handler: --database is required" -- confirmed by
        direct reproduction, reverting to reach the real
        /var/empty/dek-source-ingest, which reliably reproduced it."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proof_root = root / "proofs"; proof_root.mkdir(mode=0o700)
            os.chmod(proof_root, 0o700)
            isolated_clone = root / "clones"; isolated_clone.mkdir()
            credential = root / "git-credentials"
            credential.write_text("https://alice:s3cret@github.com/chenponsh/dek.git", encoding="utf-8")
            os.chmod(credential, 0o440)

            captured = {}

            def fake_run(command, **kwargs):
                if "clone" in command:
                    captured["env"] = kwargs.get("env")
                return mock.Mock(returncode=1)

            with mock.patch.dict(os.environ, {
                        "DEK_FIXED_ORIGIN": "https://github.com/chenponsh/dek.git",
                        "DEK_GIT_CREDENTIAL_FILE": str(credential),
                    }), \
                    mock.patch("deploy.source_ingest_entrypoint.PROOF_ROOT", proof_root), \
                    mock.patch("deploy.source_ingest_entrypoint.subprocess.run", side_effect=fake_run):
                with self.assertRaises(SystemExit):
                    entrypoint_main([
                        "--isolated-clone", str(isolated_clone),
                        "--origin", "https://github.com/chenponsh/dek.git",
                        "--proof-output", str(proof_root / "staged-proof-report.json"),
                        "--expected-output", str(proof_root / "staged-proof-report.expected.json"),
                        "--package-root", str(REPO),
                        "--pre-cutover-proof",
                        "scheduled-run",
                    ])

            self.assertIn("env", captured)
            home = captured["env"].get("HOME")
            self.assertNotEqual(home, "/var/empty/dek-source-ingest")
            self.assertTrue(home and Path(home).is_dir(), home)
            self.assertTrue(os.access(home, os.W_OK), f"{home} is not writable")
            self.assertTrue(Path(home).is_relative_to(isolated_clone), home)


if __name__ == "__main__":
    unittest.main()
