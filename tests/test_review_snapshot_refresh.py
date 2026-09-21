"""The reviewers' snapshot is refreshed after every publish (refresh-bundle mode of the ingest
entrypoint, run by dek-source-refresh.service at the end of the manual publish chain). Without it
the review page keeps showing a published item as 'approved, not yet published' and, after 30
minutes, the DingTalk notifier reports the publish as stuck."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from deploy import source_ingest_entrypoint as entrypoint


class RefreshBundleModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.proof_root = root / "proofs"; self.proof_root.mkdir(mode=0o700); os.chmod(self.proof_root, 0o700)
        self.clones = root / "clones"; self.clones.mkdir()
        self.review_input = root / "review-input"; self.review_input.mkdir()
        (self.review_input / "repository.bundle").write_bytes(b"old snapshot")
        credential = root / "git-credentials"
        credential.write_text("https://alice:s3cret@github.com/chenponsh/dek.git", encoding="utf-8")
        os.chmod(credential, 0o440)
        self.credential = credential
        self.commands = []

    def fake_run(self, command, **kwargs):
        self.commands.append(list(command))
        if "clone" in command:
            Path(command[-1]).mkdir(parents=True)
        if "bundle" in command:                                  # git bundle create <path> refs/heads/main
            Path(command[command.index("create") + 1]).write_bytes(b"new snapshot")
        return mock.Mock(returncode=0)

    def run_main(self, mode):
        with mock.patch.dict(os.environ, {"DEK_FIXED_ORIGIN": "https://github.com/chenponsh/dek.git",
                                          "DEK_GIT_CREDENTIAL_FILE": str(self.credential)}), \
                mock.patch.object(entrypoint, "PROOF_ROOT", self.proof_root), \
                mock.patch.object(entrypoint, "REVIEW_INPUT", self.review_input), \
                mock.patch.object(entrypoint, "load_ingestion_modules", side_effect=AssertionError("ingestion code must not load")), \
                mock.patch.object(entrypoint.subprocess, "run", side_effect=self.fake_run):
            return entrypoint.main([
                "--isolated-clone", str(self.clones), "--origin", "https://github.com/chenponsh/dek.git",
                "--proof-output", str(self.proof_root / "latest-report.json"),
                "--expected-output", str(self.proof_root / "latest-report.expected.json"),
                "--package-root", str(REPO), mode])

    def test_refresh_replaces_the_bundle_and_only_clones_and_bundles(self):
        self.assertEqual(self.run_main("refresh-bundle"), 0)
        self.assertEqual((self.review_input / "repository.bundle").read_bytes(), b"new snapshot")
        verbs = [next(part for part in command if part in {"clone", "bundle", "push", "commit", "add", "fetch", "checkout"}) for command in self.commands]
        self.assertEqual(verbs, ["clone", "bundle"])              # no push, no commit, nothing fetched
        self.assertEqual(sorted(p.name for p in self.review_input.iterdir()), ["repository.bundle"])   # no staging file left
        self.assertEqual(list(self.clones.iterdir()), [])          # the per-run clone is removed

    def test_refresh_leaves_the_last_ingest_proof_record_alone(self):
        (self.proof_root / "latest-report.expected.json").write_text('{"kept": true}', encoding="utf-8")
        self.run_main("refresh-bundle")
        self.assertEqual((self.proof_root / "latest-report.expected.json").read_text(encoding="utf-8"), '{"kept": true}')

    def test_a_real_ingest_run_still_writes_its_expected_proof_record(self):
        with self.assertRaises(AssertionError):                    # stops when it reaches the (forbidden here) ingestion code
            self.run_main("scheduled-run")
        self.assertTrue((self.proof_root / "latest-report.expected.json").exists())


class UnitFileTests(unittest.TestCase):
    units = REPO / "deploy" / "systemd"

    def test_the_refresh_unit_is_the_ingest_unit_with_only_the_mode_changed(self):
        ingest = (self.units / "dek-source-ingest.service").read_text(encoding="utf-8").splitlines()
        refresh = (self.units / "dek-source-refresh.service").read_text(encoding="utf-8").splitlines()
        drop = lambda lines: [l for l in lines if not l.startswith(("Description=", "OnFailure=", "ExecStart="))]
        self.assertEqual(drop(ingest), drop(refresh))              # same sandbox, same credentials, same paths
        start = next(l for l in refresh if l.startswith("ExecStart="))
        self.assertTrue(start.endswith(" refresh-bundle"))
        self.assertNotIn("scheduled-run", start)
        self.assertNotIn("xvfb", start)

    def test_the_manual_publish_chain_ends_with_a_refresh_that_cannot_fail_the_publish(self):
        starts = [l for l in (self.units / "dek-review-publish-manual.service").read_text(encoding="utf-8").splitlines() if l.startswith("ExecStart")]
        self.assertEqual(starts[-1], "ExecStart=-/usr/bin/systemctl start --wait dek-source-refresh.service")
        self.assertEqual(starts[-2], "ExecStart=/usr/bin/systemctl start --wait dek-activator.service")


if __name__ == "__main__":
    unittest.main()
