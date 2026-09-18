import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deploy.activator import ActivationError, Activator, ActivatorConfig
from deploy.release_bundle import BundleError, ReleasePublisher, _canonical
from deploy.readiness import ReadinessError, validate_marker, write_marker
from web.site import sanitize_html


READINESS_CONFIG = {
    "review_origin": "https://review.example",
    "oauth_callback": "https://review.example/auth/callback",
    "reviewer_ids": ["guessable-enterprise-id"],
    "expected_addresses": ["192.0.2.10"],
    "readiness_url": "https://review.example/__ready",
}


class GitSubprocessProxyEnvironmentTests(unittest.TestCase):
    """The host reaches github.com only through a local proxy (the deployed
    dek-source-ingest.service sets HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:7890).
    `_run`'s env sanitization must not silently drop that for real network
    git operations, or every clone/push to GitHub fails to connect."""

    def test_run_passes_through_proxy_variables_present_in_caller_env(self):
        from deploy.release_bundle import _run
        captured = {}

        def fake_run(arguments, *, cwd, env, stdin, stdout, stderr, timeout, check):
            captured.update(env)
            return subprocess.CompletedProcess(arguments, 0, b"ok", b"")

        with patch("deploy.release_bundle.subprocess.run", fake_run):
            _run(("true",), env={
                "HTTP_PROXY": "http://127.0.0.1:7890", "HTTPS_PROXY": "http://127.0.0.1:7890",
                "NO_PROXY": "example.internal", "GIT_CONFIG_COUNT": "0",
            })
        self.assertEqual(captured.get("HTTP_PROXY"), "http://127.0.0.1:7890")
        self.assertEqual(captured.get("HTTPS_PROXY"), "http://127.0.0.1:7890")
        self.assertEqual(captured.get("NO_PROXY"), "example.internal")

    def test_run_does_not_leak_arbitrary_caller_env_keys(self):
        # The sanitizer stays an allowlist: only GIT_CONFIG_*/proxy keys pass.
        from deploy.release_bundle import _run
        captured = {}

        def fake_run(arguments, *, cwd, env, stdin, stdout, stderr, timeout, check):
            captured.update(env)
            return subprocess.CompletedProcess(arguments, 0, b"ok", b"")

        with patch("deploy.release_bundle.subprocess.run", fake_run):
            _run(("true",), env={"LD_PRELOAD": "/tmp/evil.so", "PATH": "/tmp/evil"})
        self.assertNotIn("LD_PRELOAD", captured)
        self.assertEqual(captured.get("PATH"), "/usr/bin:/bin")

    def test_clone_and_push_remote_carry_the_process_proxy_settings(self):
        from deploy.release_bundle import ReleasePublisher
        key = Ed25519PrivateKey.generate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root / "credential"
            credential.write_text("https://x:y@github.example/org/dek.git", encoding="utf-8")
            publisher = ReleasePublisher("https://github.example/org/dek.git", key, credential)
            captured_envs = []

            def fake_run(arguments, *, cwd=None, env=None, timeout=120):
                captured_envs.append(env or {})
                return b""

            with patch("deploy.release_bundle._run", fake_run), \
                 patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:7890", "HTTPS_PROXY": "http://127.0.0.1:7890"}):
                publisher._clone_remote(root / "clone")
                publisher._push_remote(root / "clone", "a" * 40)
            for env in captured_envs:
                self.assertEqual(env.get("HTTP_PROXY"), "http://127.0.0.1:7890")
                self.assertEqual(env.get("HTTPS_PROXY"), "http://127.0.0.1:7890")


class ReviewCredentialGateFieldsTests(unittest.TestCase):
    """The review environment gate's required-field set must match what
    web/review_app.py actually reads at startup (DEK_REVIEW_AUDIT_KEY,
    DEK_WEB_CLAIM_SECRET, DEK_REVIEWER_IDS) — not a stale field set from a
    since-removed dedicated review DingTalk OAuth client."""

    def _write(self, tmp: Path, raw: bytes) -> Path:
        path = tmp / "environment"
        path.write_bytes(raw)
        return path

    AUDIT_KEY = b"7f3a9c2e18b6d40592af71c3e8b0d925"
    CLAIM_SECRET = b"4b1e8d6f2a9c3705b8e41f0a6c2d9713"
    DINGTALK_SECRET = b"9d2f4a6e8b1c0537f9a2d6e4b8c1f053"

    def test_the_environment_review_app_actually_requires_passes_the_gate(self):
        from deploy.credential_gate import _environment
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(Path(temporary), (
                b"DEK_REVIEW_AUDIT_KEY=" + self.AUDIT_KEY + b"\n"
                b"DEK_WEB_CLAIM_SECRET=" + self.CLAIM_SECRET + b"\n"
                b"DEK_REVIEWER_IDS=016523605824315385,12283255131204392\n"
            ))
            values = _environment(path, "review")
        self.assertEqual(set(values), {"DEK_REVIEW_AUDIT_KEY", "DEK_WEB_CLAIM_SECRET", "DEK_REVIEWER_IDS"})

    def test_the_environment_review_app_cannot_start_with_is_rejected(self):
        # No DEK_WEB_CLAIM_SECRET (what web/review_app.py:main() actually
        # requires), only the unused legacy DEK_REVIEW_DINGTALK_* fields.
        from deploy.credential_gate import CredentialError, _environment
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write(Path(temporary), (
                b"DEK_REVIEW_DINGTALK_CLIENT_ID=ding-client-918273\n"
                b"DEK_REVIEW_DINGTALK_CLIENT_SECRET=" + self.DINGTALK_SECRET + b"\n"
                b"DEK_REVIEW_DINGTALK_AGENT_ID=123456\n"
                b"DEK_REVIEW_AUDIT_KEY=" + self.AUDIT_KEY + b"\n"
                b"DEK_REVIEWER_IDS=016523605824315385\n"
            ))
            with self.assertRaises(CredentialError):
                _environment(path, "review")


class FinalizeCrashRecoveryTests(unittest.TestCase):
    """finalize() overwrites release.json's bytes in place (it must never
    replace the inode: the file is builder-owned, and the publisher has no
    CAP_FOWNER to re-chmod a freshly created one — see DAC_MATRIX.tsv's
    publisher-signs-build row). A process kill mid-write must not corrupt
    the file to the point that a retry can no longer even read it."""

    def _prepared_package(self, root: Path):
        key = Ed25519PrivateKey.generate()
        package = root / "build"
        (package / "site").mkdir(parents=True)
        (package / "site/index.html").write_text("ok", encoding="utf-8")
        (package / "dek-kb.json").write_text('{"version":4,"documents":[]}', encoding="utf-8")
        approval = {
            "schema_version": 2, "decision_id": "decision-12345678", "decision_sha256": "1" * 64,
            "nonce": "decision-12345678", "origin": "https://github.com/chenponsh/dek.git",
            "commit": "2" * 40, "tree": "3" * 40, "bundle_sha256": hashlib.sha256(b"bundle").hexdigest(),
        }
        (package / "approval.json").write_text(json.dumps(approval), encoding="utf-8")
        (package / "approval.sig").write_bytes(key.sign(_canonical(approval)))
        artifacts = {
            "dek-kb.json": hashlib.sha256((package / "dek-kb.json").read_bytes()).hexdigest(),
            "site/index.html": hashlib.sha256((package / "site/index.html").read_bytes()).hexdigest(),
        }
        original_release = {**approval, "generation": "decision-12345678-decision-12345678", "artifacts": artifacts}
        (package / "release.json").write_text(json.dumps(original_release), encoding="utf-8")
        credential = root / "credential"
        credential.write_text("https://user:token@github.com/chenponsh/dek.git\n", encoding="utf-8")
        os.chmod(credential, 0o400)
        publisher = ReleasePublisher(approval["origin"], key, credential)
        return publisher, package, original_release

    def test_a_crash_mid_write_leaves_the_original_release_json_intact_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            publisher, package, original_release = self._prepared_package(Path(temporary))
            before = (package / "release.json").read_bytes()

            with patch("deploy.release_bundle.os.write", side_effect=OSError("simulated crash mid-write")):
                with self.assertRaises(OSError):
                    publisher.finalize(package)

            # Not truncated, not partially written: identical to the
            # builder's original content, still valid JSON.
            after = (package / "release.json").read_bytes()
            self.assertEqual(after, before)
            self.assertEqual(json.loads(after), original_release)
            self.assertFalse((package / "release.sig").exists())

            # A retry (the real recovery path: process_decision() calls
            # finalize() again when release.sig is still missing) now works.
            final = publisher.finalize(package)
            self.assertEqual(final["artifacts"], original_release["artifacts"])
            self.assertTrue((package / "release.sig").exists())

    def test_overwrite_in_place_never_creates_chmods_or_renames_the_path(self):
        from deploy.release_bundle import _overwrite_in_place
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release.json"
            path.write_bytes(b'{"a": 1}')
            os.chmod(path, 0o640)
            before_inode = path.stat().st_ino
            _overwrite_in_place(path, b'{"a": 22}')
            self.assertEqual(path.stat().st_ino, before_inode)
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o640")
            self.assertEqual(path.read_bytes(), b'{"a": 22}')

    def test_overwrite_in_place_truncates_a_shorter_replacement(self):
        from deploy.release_bundle import _overwrite_in_place
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "release.json"
            path.write_bytes(b'{"a": "a much longer original value"}')
            _overwrite_in_place(path, b'{"a":1}')
            self.assertEqual(path.read_bytes(), b'{"a":1}')


class PublisherActivationGateTests(unittest.TestCase):
    def _package(self, root: Path):
        key = Ed25519PrivateKey.generate()
        package = root / "build"
        (package / "site").mkdir(parents=True)
        (package / "site/index.html").write_text("ok", encoding="utf-8")
        (package / "dek-kb.json").write_text('{"version":4,"documents":[]}', encoding="utf-8")
        (package / "repository.bundle").write_bytes(b"bundle")
        approval = {
            "schema_version": 2,
            "decision_id": "decision-12345678",
            "decision_sha256": "1" * 64,
            "nonce": "decision-12345678",
            "origin": "https://github.com/chenponsh/dek.git",
            "commit": "2" * 40,
            "tree": "3" * 40,
            "bundle_sha256": hashlib.sha256(b"bundle").hexdigest(),
        }
        (package / "approval.json").write_text(json.dumps(approval), encoding="utf-8")
        (package / "approval.sig").write_bytes(key.sign(_canonical(approval)))
        artifacts = {
            "dek-kb.json": hashlib.sha256((package / "dek-kb.json").read_bytes()).hexdigest(),
            "site/index.html": hashlib.sha256((package / "site/index.html").read_bytes()).hexdigest(),
        }
        (package / "release.json").write_text(json.dumps({
            **approval,
            "generation": "decision-12345678-decision-12345678",
            "artifacts": artifacts,
        }), encoding="utf-8")
        credential = root / "credential"
        credential.write_text("https://user:token@github.com/chenponsh/dek.git\n", encoding="utf-8")
        os.chmod(credential, 0o400)
        publisher = ReleasePublisher(approval["origin"], key, credential)
        publisher.finalize(package)
        return key, package, publisher, approval

    def test_only_successful_push_creates_exact_signed_activation_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key, package, publisher, approval = self._package(root)
            self.assertFalse((package / "activation-ready.json").exists())
            self.assertFalse((package / "activation-ready.sig").exists())

            def failed_run(arguments, **kwargs):
                if "rev-parse" in arguments:
                    return (approval["commit"] if "commit" in arguments[-1] else approval["tree"]).encode() + b"\n"
                if "push" in arguments:
                    raise BundleError("push failed")
                return b""

            with patch("deploy.release_bundle._run", side_effect=failed_run):
                with self.assertRaisesRegex(BundleError, "push failed"):
                    publisher.publish(package)
            self.assertFalse((package / "activation-ready.json").exists())
            self.assertFalse((package / "activation-ready.sig").exists())

            def successful_run(arguments, **kwargs):
                if "rev-parse" in arguments:
                    return (approval["commit"] if "commit" in arguments[-1] else approval["tree"]).encode() + b"\n"
                return b""

            with patch("deploy.release_bundle._run", side_effect=successful_run):
                publisher.publish(package)
            gate = json.loads((package / "activation-ready.json").read_text(encoding="utf-8"))
            self.assertEqual(gate["status"], "pushed")
            self.assertEqual(gate["generation"], "decision-12345678-decision-12345678")
            self.assertEqual(gate["release_sha256"], hashlib.sha256((package / "release.json").read_bytes()).hexdigest())

            inbox = root / "activate"
            config = ActivatorConfig.under(inbox)
            config.prepare()
            candidate = config.build_inbox / package.name
            os.replace(package, candidate)
            activator = Activator(config, proof_reader=lambda kind, expected: dict(expected), approval_key=key.public_key())
            activator._validate_release(candidate)
            (candidate / "activation-ready.json").write_text(json.dumps({**gate, "release_sha256": "0" * 64}), encoding="utf-8")
            with self.assertRaisesRegex(ActivationError, "activation gate"):
                activator._validate_release(candidate)


class StrictReadinessMarkerTests(unittest.TestCase):
    def test_secret_bound_markers_reject_empty_corrupt_stale_and_unsafe_files(self):
        secret = b"root-only-readiness-secret-32bytes"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            automation = root / "automation-ready"
            other = root / "other-marker"

            def write():
                write_marker(automation, READINESS_CONFIG, secret,
                            confirmed_authorized_login=True, confirmed_unauthorized_login=True)

            write()
            public = automation.read_text(encoding="utf-8")
            self.assertNotIn("guessable-enterprise-id", public)
            self.assertNotIn(hashlib.sha256(json.dumps(READINESS_CONFIG, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), public)
            validate_marker(automation, READINESS_CONFIG, secret)

            with self.assertRaisesRegex(ReadinessError, "invalid readiness marker"):
                validate_marker(automation, {**READINESS_CONFIG, "reviewer_ids": ["changed"]}, secret)
            automation.write_bytes(b"")
            with self.assertRaises(ReadinessError):
                validate_marker(automation, READINESS_CONFIG, secret)

            write()
            os.chmod(automation, 0o644)
            with self.assertRaisesRegex(ReadinessError, "unsafe"):
                validate_marker(automation, READINESS_CONFIG, secret)
            os.chmod(automation, 0o444)
            hardlink = root / "automation-hardlink"
            os.link(automation, hardlink)
            with self.assertRaisesRegex(ReadinessError, "unsafe"):
                validate_marker(automation, READINESS_CONFIG, secret)
            hardlink.unlink()
            automation.unlink()
            write_marker(other, READINESS_CONFIG, secret,
                        confirmed_authorized_login=True, confirmed_unauthorized_login=True)
            automation.symlink_to(other)
            with self.assertRaisesRegex(ReadinessError, "unsafe"):
                validate_marker(automation, READINESS_CONFIG, secret)

    def test_units_use_strict_exec_condition_not_path_existence(self):
        for name in ("dek-activator.service", "dek-builder.service", "dek-review-publish.service", "dek-source-ingest.service"):
            unit = (Path("deploy/systemd") / name).read_text(encoding="utf-8")
            self.assertNotIn("ConditionPathExists", unit)
            self.assertIn("ExecCondition=+", unit)
            self.assertIn("--validate-marker", unit)


class FirstActivationFailureTests(unittest.TestCase):
    def test_first_failed_candidate_is_removed_from_active_and_quarantined(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = ActivatorConfig.under(Path(temporary))
            config.prepare()
            activator = Activator(config, proof_reader=lambda kind, expected: (_ for _ in ()).throw(ActivationError("proof failed")))
            candidate = activator.test_release("first", sequence=1)
            with self.assertRaisesRegex(ActivationError, "no rollback"):
                activator.activate(candidate)
            self.assertFalse(config.active.exists())
            quarantine = config.control / "failed" / "nonce-first-12345678.json"
            self.assertEqual(json.loads(quarantine.read_text(encoding="utf-8"))["generation"], "first")
            with self.assertRaisesRegex(ActivationError, "quarantined"):
                activator.activate(candidate)

    def test_reconcile_removes_only_matching_first_candidate_and_quarantines_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = ActivatorConfig.under(Path(temporary))
            config.prepare()
            seed = Activator(config, proof_reader=lambda kind, expected: dict(expected))
            candidate = seed.test_release("first", sequence=1)
            expected = seed._validate_release(candidate)
            from deploy.activator import atomic_json
            atomic_json(config.active, expected)
            journal = config.journal / "prepared.json"
            atomic_json(journal, {"status": "prepared", "requested": expected, "previous": None, "recorded_at": 1})
            broken = Activator(config, proof_reader=lambda kind, value: (_ for _ in ()).throw(ActivationError("proof failed")))
            broken.reconcile()
            self.assertFalse(config.active.exists())
            self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["status"], "failed_no_previous")
            self.assertTrue((config.control / "failed" / (expected["nonce"] + ".json")).is_file())

    def test_reconcile_treats_a_same_nonce_but_otherwise_diverged_active_as_fatal(self):
        # activate()'s live-path equivalent (line ~299) compares the whole
        # expected dict, not just the nonce. reconcile()'s crash-recovery
        # path for the identical situation must not be laxer: a same-nonce
        # active.json that disagrees on any other field (disk corruption, a
        # racing writer) must halt, not silently roll back to `previous` as
        # if the observed state were the exact requested candidate.
        # (With previous=None a different, already-correct check inside
        # _remove_active_if_candidate happens to also catch this, so the
        # test must exercise the previous-is-not-None branch, which is the
        # one that unconditionally overwrote active.json with no comparison
        # at all before this fix.)
        from deploy.activator import FatalActivationError, atomic_json
        with tempfile.TemporaryDirectory() as temporary:
            config = ActivatorConfig.under(Path(temporary))
            config.prepare()
            seed = Activator(config, proof_reader=lambda kind, expected: dict(expected))
            zero_candidate = seed.test_release("zero", sequence=1)
            zero = seed.activate(zero_candidate)

            first_candidate = seed.test_release("first", sequence=2)
            expected = seed._validate_release(first_candidate)
            atomic_json(config.active, expected)
            journal = config.journal / "prepared.json"
            atomic_json(journal, {"status": "prepared", "requested": expected, "previous": zero, "recorded_at": 1})

            corrupted = {**expected, "commit": "9" * 40}  # same nonce, diverged field

            def corrupt_then_fail(kind, value):
                atomic_json(config.active, corrupted)
                raise ActivationError("proof failed")

            broken = Activator(config, proof_reader=corrupt_then_fail)
            with self.assertRaisesRegex(FatalActivationError, "unknown third generation"):
                broken.reconcile()
            self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["status"], "stopped_unknown_generation")
            # The corrupted active.json must be left in place for a human to
            # inspect, not silently overwritten by rollback-to-previous.
            self.assertEqual(json.loads(config.active.read_text(encoding="utf-8")), corrupted)


class RunbookSecretBindingTests(unittest.TestCase):
    def test_web_and_activator_generation_proof_secrets_are_raw_byte_compared(self):
        runbook = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        self.assertIn(
            "cmp -s /var/lib/dek-web/secrets/generation-proof-secret /var/lib/dek-activator/secrets/web-generation-proof",
            runbook,
        )


class InstallManifestParsingTests(unittest.TestCase):
    def test_blank_lines_are_skipped_not_a_crash(self):
        from deploy.install_components import _manifest
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest"
            digest = "a" * 64
            path.write_text(f"\n{digest}  web/app.py\n\n", encoding="utf-8")
            self.assertEqual(_manifest(path), {"web/app.py": digest})

    def test_a_line_with_no_relative_path_field_raises_the_labeled_error_not_a_bare_valueerror(self):
        from deploy.install_components import _manifest
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest"
            path.write_text("not-a-valid-manifest-line\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "invalid install manifest"):
                _manifest(path)


class RestoreLegacyLinkCrashRecoveryTests(unittest.TestCase):
    """A crash between switching current/previous and the app->legacy rename
    leaves root/app exactly as it always was: a real, non-symlink directory,
    with legacy-app.before-versioned not created yet. Recovery replaying
    kind="legacy" for that state must recognize it as already satisfied,
    not treat the untouched original as an unsafe surprise and refuse
    forever (blocking every future install for that service)."""

    def test_restore_link_is_a_no_op_when_app_is_still_the_untouched_original(self):
        from deploy.install_components import _restore_link
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app").mkdir()
            (root / "app" / "marker.txt").write_text("original", encoding="utf-8")
            _restore_link(root, "app", ["legacy", None])
            self.assertTrue((root / "app").is_dir())
            self.assertFalse((root / "app").is_symlink())
            self.assertEqual((root / "app" / "marker.txt").read_text(encoding="utf-8"), "original")
            self.assertFalse((root / "legacy-app.before-versioned").exists())

    def test_restore_link_still_refuses_a_real_directory_when_a_legacy_backup_already_exists(self):
        # If legacy-app.before-versioned already exists, `app` being a real
        # (non-symlink) directory again is not "still untouched" -- it is
        # unexplained, and must stay a hard refusal.
        from deploy.install_components import _restore_link
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app").mkdir()
            (root / "legacy-app.before-versioned").mkdir()
            with self.assertRaisesRegex(RuntimeError, "refusing unsafe rollback overwrite"):
                _restore_link(root, "app", ["legacy", None])

    def test_restore_link_still_restores_from_legacy_when_app_is_the_versioned_symlink(self):
        from deploy.install_components import _restore_link
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "versions/new").mkdir(parents=True)
            (root / "app").symlink_to("versions/new")
            legacy = root / "legacy-app.before-versioned"
            legacy.mkdir()
            (legacy / "marker.txt").write_text("original", encoding="utf-8")
            _restore_link(root, "app", ["legacy", None])
            self.assertTrue((root / "app").is_dir())
            self.assertFalse((root / "app").is_symlink())
            self.assertEqual((root / "app" / "marker.txt").read_text(encoding="utf-8"), "original")


class BuilderAtomicWriteDurabilityTests(unittest.TestCase):
    """build_atomically() previously fsynced only the staging directory's own
    fd (directory-entry metadata) before the atomic rename that hands a
    build to the activator, never the release file contents BundleBuilder
    actually wrote -- unlike fsync_tree()/seed_release.py's
    install_generation(), which explicitly fsync every file bottom-up
    first. A crash/power-loss right after a build "completes" could leave
    the activator picking up truncated or stale file contents."""

    class _FakeBuilder:
        def build(self, package, output):
            output.mkdir(parents=True)
            (output / "release.json").write_text('{"ok": true}', encoding="utf-8")
            (output / "site").mkdir()
            (output / "site" / "index.html").write_text("hi", encoding="utf-8")

    def test_every_file_is_fsynced_before_the_atomic_rename(self):
        from deploy.builder_entrypoint import build_atomically
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "builds" / "generation-1"
            target.parent.mkdir(parents=True)
            synced_fds = []
            real_fstat = os.fstat
            real_fsync = os.fsync

            def tracking_fsync(fd):
                try:
                    path = os.readlink(f"/proc/self/fd/{fd}")
                except OSError:
                    path = f"fd:{fd}"
                synced_fds.append(path)
                return real_fsync(fd)

            with patch("deploy.activator.os.fsync", side_effect=tracking_fsync):
                build_atomically(self._FakeBuilder(), root / "package", target)

            self.assertTrue(target.is_dir())
            self.assertEqual((target / "release.json").read_text(encoding="utf-8"), '{"ok": true}')
            self.assertTrue(any(p.endswith("release.json") for p in synced_fds))
            self.assertTrue(any(p.endswith("index.html") for p in synced_fds))
            self.assertTrue(any(p.endswith("generation-1") or "staging" in p for p in synced_fds))


class AtomicSevenComponentInstallTests(unittest.TestCase):
    def test_all_components_prepare_before_switch_and_failure_restores_all_links(self):
        from deploy.install_components import SERVICES, install_versioned_components

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            package.mkdir()
            manifest_lines = []
            for top in ("deploy", "web", "qa", "ingestion/automation"):
                path = package / top / "payload.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(top, encoding="utf-8")
                manifest_lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(package).as_posix()}\n")
            manifest = package / "PACKAGE.sha256"
            manifest.write_text("".join(manifest_lines), encoding="utf-8")
            roots = {}
            original = {}
            for service in SERVICES:
                service_root = root / service
                (service_root / "versions/old").mkdir(parents=True)
                (service_root / "current").symlink_to("versions/old")
                (service_root / "app").symlink_to("current")
                (service_root / "previous").symlink_to("versions/older")
                roots[service] = service_root
                original[service] = tuple(os.readlink(service_root / name) for name in ("current", "app", "previous"))

            observed = []
            def fail_on_fourth(service, index):
                observed.append((service, all((roots[name] / "versions" / ("a" * 64)).is_dir() for name in SERVICES)))
                if index == 3:
                    raise RuntimeError("injected switch failure")

            with self.assertRaisesRegex(RuntimeError, "injected"):
                install_versioned_components(
                    package, manifest, roots, "a" * 64, before_switch=fail_on_fourth,
                    test_only_allow_unsafe_ancestors={package.parent.parent},
                )
            self.assertTrue(observed)
            self.assertTrue(all(prepared for _, prepared in observed))
            for service, service_root in roots.items():
                self.assertEqual(tuple(os.readlink(service_root / name) for name in ("current", "app", "previous")), original[service])


class PublisherQueueLockTests(unittest.TestCase):
    def test_scan_holds_writer_queue_lock_through_tail_validation(self):
        from contextlib import contextmanager
        from deploy.publisher_entrypoint import load_approved_decisions

        state = {"locked": False, "iterated": False}
        class ReviewModule:
            @staticmethod
            @contextmanager
            def queue_lock(path, *, read_only=False):
                self.assertTrue(read_only)
                state["locked"] = True
                try: yield
                finally: state["locked"] = False

            @staticmethod
            def iter_valid_decisions(path, key, **kwargs):
                self.assertTrue(state["locked"])
                state["iterated"] = True
                yield {"action": "approve", "decision_id": "decision-12345678", "rough_path": "ingestion/rough/a.md"}

            @staticmethod
            def validate_decision(record, key):
                self.assertTrue(state["locked"])
                return record

        records = load_approved_decisions(ReviewModule, Path("decisions.jsonl"), b"k" * 32,
                                          Path("quarantine"), lambda value: None)
        self.assertTrue(state["iterated"])
        self.assertFalse(state["locked"])
        self.assertEqual(len(records), 1)

    def test_only_latest_valid_decision_per_rough_path_can_publish(self):
        from contextlib import contextmanager
        from deploy.publisher_entrypoint import load_approved_decisions

        queued = [
            {"action": "approve", "decision_id": "decision-old-0001", "rough_path": "ingestion/rough/a.md"},
            {"action": "approve", "decision_id": "decision-other-01", "rough_path": "ingestion/rough/b.md"},
            {"action": "reject", "decision_id": "decision-new-0001", "rough_path": "ingestion/rough/a.md"},
            {"action": "approve", "decision_id": "decision-old-0002", "rough_path": "ingestion/rough/c.md"},
            {"action": "approve", "decision_id": "decision-new-0002", "rough_path": "ingestion/rough/c.md"},
        ]

        class ReviewModule:
            @staticmethod
            @contextmanager
            def queue_lock(path, *, read_only=False):
                yield

            @staticmethod
            def iter_valid_decisions(path, key, **kwargs):
                yield from queued

            @staticmethod
            def validate_decision(record, key):
                return dict(record)

        records = load_approved_decisions(
            ReviewModule, Path("decisions.jsonl"), b"k" * 32,
            Path("quarantine"), lambda value: None,
        )
        self.assertEqual(
            [record["decision_id"] for record in records],
            ["decision-other-01", "decision-new-0002"],
        )


class SiteUrlPolicyTests(unittest.TestCase):
    def test_protocol_relative_links_and_remote_images_are_rejected(self):
        rendered = sanitize_html(
            '<a href="//evil.example/path">bad</a>'
            '<img src="//evil.example/a.png">'
            '<img src="https://evil.example/a.png">'
            '<img src="data:image/png;base64,AAAA">'
            '<img src="/assets/local.png"><img src="images/local.png">'
        )
        self.assertNotIn("evil.example", rendered)
        self.assertNotIn("data:image", rendered)
        self.assertIn('src="/assets/local.png"', rendered)
        self.assertIn('src="images/local.png"', rendered)

    def test_web_csp_allows_only_same_origin_images(self):
        source = Path("web/app.py").read_text(encoding="utf-8")
        self.assertIn("img-src 'self';", source)
        self.assertNotIn("img-src 'self' data: https:", source)


if __name__ == "__main__":
    unittest.main()
