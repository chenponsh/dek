"""The reviewers' snapshot is refreshed after every publish (refresh-bundle mode of the ingest
entrypoint, run by dek-source-refresh.service at the end of the manual publish chain). Without it
the review page keeps showing a published item as 'approved, not yet published' and, after 30
minutes, the DingTalk notifier reports the publish as stuck."""
import hashlib
import json
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

    def test_active_release_bundle_is_reused_without_git_or_credentials(self):
        active_root=Path(self.tmp.name)/"active"; releases=Path(self.tmp.name)/"releases"
        review_input=Path(self.tmp.name)/"active-review"; review_input.mkdir()
        generation="batch-12345678-batch-12345678"; release=releases/generation; release.mkdir(parents=True)
        raw=b"proved active repository bundle\n"; digest=hashlib.sha256(raw).hexdigest()
        active={"generation":generation,"commit":"1"*40,"tree":"2"*40,"bundle_sha256":digest}
        (active_root/"control").mkdir(parents=True)
        active_path=active_root/"control/active.json"; active_path.write_text(json.dumps(active))
        (release/"release.json").write_text(json.dumps(active))
        (release/"repository.bundle").write_bytes(raw)

        listed=mock.Mock(returncode=0,stdout=f'{active["commit"]} refs/heads/main\n'.encode(),stderr=b'')
        with mock.patch.object(entrypoint.subprocess,"run",return_value=listed):
            entrypoint.refresh_review_bundle_from_active(active_path,releases,review_input)
        self.assertEqual((review_input/"repository.bundle").read_bytes(),raw)
        self.assertEqual((review_input/"repository.bundle").stat().st_mode & 0o777,0o640)

    def test_active_release_bundle_digest_mismatch_is_rejected_without_replacing_snapshot(self):
        releases=Path(self.tmp.name)/"bad-releases"; review_input=Path(self.tmp.name)/"bad-review"
        generation="batch-12345678-batch-12345678"; release=releases/generation
        release.mkdir(parents=True); review_input.mkdir(); (review_input/"repository.bundle").write_bytes(b"old")
        active={"generation":generation,"commit":"1"*40,"tree":"2"*40,"bundle_sha256":"3"*64}
        active_path=Path(self.tmp.name)/"bad-active.json"; active_path.write_text(json.dumps(active))
        (release/"release.json").write_text(json.dumps(active)); (release/"repository.bundle").write_bytes(b"tampered")
        listed=mock.Mock(returncode=0,stdout=f'{active["commit"]} refs/heads/main\n'.encode(),stderr=b'')
        with mock.patch.object(entrypoint.subprocess,"run",return_value=listed), self.assertRaisesRegex(SystemExit,"digest mismatch"):
            entrypoint.refresh_review_bundle_from_active(active_path,releases,review_input)
        self.assertEqual((review_input/"repository.bundle").read_bytes(),b"old")


class UnitFileTests(unittest.TestCase):
    units = REPO / "deploy" / "systemd"

    def test_refresh_uses_only_the_proved_active_release_without_network_credentials(self):
        refresh = (self.units / "dek-source-refresh.service").read_text(encoding="utf-8")
        start = next(l for l in refresh.splitlines() if l.startswith("ExecStart="))
        self.assertTrue(start.endswith(" refresh-active-bundle"))
        self.assertIn("SupplementaryGroups=dek-release-read",refresh)
        self.assertIn("BindReadOnlyPaths=/var/lib/dek-activate/control:/run/dek-active-control",refresh)
        self.assertIn("ReadWritePaths=/var/lib/dek-review/input",refresh)
        self.assertIn("IPAddressDeny=any",refresh)
        self.assertNotIn("ExecCondition=",refresh)
        self.assertNotIn("git-credentials",refresh)
        self.assertNotIn("HTTP_PROXY",refresh)

    def test_the_manual_publish_chain_ends_with_a_refresh_that_cannot_fail_the_publish(self):
        starts = [l for l in (self.units / "dek-review-publish-manual.service").read_text(encoding="utf-8").splitlines() if l.startswith("ExecStart")]
        self.assertEqual(starts[-1], "ExecStart=-/usr/bin/systemctl start --wait dek-source-refresh.service")
        self.assertEqual(starts[-2], "ExecStart=/usr/bin/touch /var/lib/dek-review/state/publish-stage-refresh")
        self.assertEqual(starts[-3], "ExecStart=/usr/bin/systemctl start --wait dek-activator.service")


if __name__ == "__main__":
    unittest.main()
