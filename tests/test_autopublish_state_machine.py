import hashlib
import json
import os
import tempfile
import unittest
import subprocess
from urllib.parse import urlencode
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class PushBoundaryTests(unittest.TestCase):
    def test_push_authorization_revalidates_latest_decision_while_locked(self):
        from deploy.publisher_entrypoint import authorized_queue_snapshot

        approval = {"decision_id": "approve-12345678", "rough_path": "ingestion/rough/a.md", "action": "approve"}
        rejection = {"decision_id": "reject-12345678", "rough_path": approval["rough_path"], "action": "reject"}
        state = {"locked": False}

        class Review:
            @staticmethod
            @contextmanager
            def queue_lock(path, *, read_only=False):
                state["locked"] = True
                try:
                    yield
                finally:
                    state["locked"] = False

            @staticmethod
            def iter_valid_decisions(*args, **kwargs):
                self.assertTrue(state["locked"])
                yield approval
                yield rejection

            @staticmethod
            def validate_decision(record, key):
                return record

        with tempfile.TemporaryDirectory() as temporary:
            queue = Path(temporary) / "decisions.jsonl"
            queue.write_bytes(b"signed queue bytes\n")
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                authorized_queue_snapshot(Review, queue, b"k" * 32, approval, Path(temporary) / "bad", lambda _: None)
            self.assertFalse(state["locked"])

    def test_push_authorization_releases_lock_before_returning_snapshot(self):
        from deploy.publisher_entrypoint import authorized_queue_snapshot

        approval = {"decision_id": "approve-12345678", "rough_path": "ingestion/rough/a.md", "action": "approve"}
        state = {"locked": False}

        class Review:
            @staticmethod
            @contextmanager
            def queue_lock(path, *, read_only=False):
                state["locked"] = True
                try:
                    yield
                finally:
                    state["locked"] = False

            @staticmethod
            def iter_valid_decisions(*args, **kwargs):
                self.assertTrue(state["locked"])
                yield approval

            @staticmethod
            def validate_decision(record, key):
                return record

        with tempfile.TemporaryDirectory() as temporary:
            queue = Path(temporary) / "decisions.jsonl"
            queue.write_bytes(b"signed queue bytes\n")
            snapshot = authorized_queue_snapshot(Review, queue, b"k" * 32, approval, Path(temporary) / "bad", lambda _: None)
            # The lock must already be released by the time the caller has the
            # snapshot in hand -- it must not still be held while the caller
            # goes on to do the slow network push.
            self.assertFalse(state["locked"])
            self.assertEqual(snapshot["decision_queue_size"], len(b"signed queue bytes\n"))

    def test_process_decision_does_not_hold_authorizer_lock_during_publish(self):
        """The queue lock a real push_authorizer takes internally must already
        be released by the time process_decision calls publish() -- publish()
        does a blocking network push and must not be serialized behind
        reviewer decision submissions or activator polling."""
        from deploy.publisher_entrypoint import process_decision
        import threading

        lock = threading.Lock()
        events = []
        testcase = self

        class FakePublisher:
            def verify_review_snapshot(self, *args, **kwargs):
                pass

            def prepare_change(self, preparing, decision):
                preparing.mkdir(parents=True)
                (preparing / "approval.json").write_text(json.dumps({"decision_id": decision["decision_id"], "nonce": "n"}))

            def finalize(self, build):
                pass

            def publish(self, build, *, queue_snapshot=None):
                events.append("publish")
                held = not lock.acquire(blocking=False)
                if not held:
                    lock.release()
                testcase.assertFalse(held, "authorizer's lock is still held during publish")

        def push_authorizer(decision):
            with lock:
                events.append("authorize")
                return {"decision_queue_sha256": "x" * 64, "decision_queue_size": 1}

        decision = {"decision_id": "approve-12345678", "snapshot_bundle_sha256": "b" * 64,
                    "snapshot_commit": "c", "snapshot_tree": "t"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive"; archive.mkdir()
            (archive / (decision["snapshot_bundle_sha256"] + ".bundle")).write_bytes(b"")
            approved = root / "approved"; approved.mkdir()
            builds = root / "builds"; builds.mkdir()
            build = builds / "approve-12345678-n"; build.mkdir()
            (build / "release.json").write_text("{}"); (build / "release.sig").write_bytes(b"sig")
            self.assertEqual(process_decision(FakePublisher(), decision, approved, builds, archive, push_authorizer), "pushed")
        self.assertEqual(events, ["authorize", "publish"])


class QueueGateTests(unittest.TestCase):
    def test_late_append_invalidates_signed_gate_snapshot_without_mac_key(self):
        from deploy.publisher_entrypoint import queue_snapshot
        from deploy.activator_entrypoint import verify_gate_queue_snapshot

        with tempfile.TemporaryDirectory() as temporary:
            queue = Path(temporary) / "decisions.jsonl"
            queue.write_bytes(b"approval\n")
            gate = {**queue_snapshot(queue), "status": "pushed"}
            verify_gate_queue_snapshot(gate, queue)
            with queue.open("ab") as handle:
                handle.write(b"late rejection\n")
                handle.flush()
                os.fsync(handle.fileno())
            with self.assertRaisesRegex(Exception, "queue snapshot"):
                verify_gate_queue_snapshot(gate, queue)


class PublicationOrderTests(unittest.TestCase):
    def test_candidates_follow_parent_commit_chain_not_random_decision_id(self):
        from deploy.activator_entrypoint import order_candidates_by_ancestry

        active = {"commit": "a" * 40}
        newer = ({"decision_id": "aaa", "parent_commit": "b" * 40, "commit": "c" * 40}, Path("newer"))
        next_one = ({"decision_id": "zzz", "parent_commit": "a" * 40, "commit": "b" * 40}, Path("next"))
        ordered = order_candidates_by_ancestry([newer, next_one], active)
        self.assertEqual([path.name for _, path in ordered], ["next", "newer"])

    def test_bootstrap_rejects_ambiguous_multiple_candidates(self):
        from deploy.activator import FatalActivationError
        from deploy.activator_entrypoint import order_candidates_by_ancestry

        first = ({"parent_commit": "a" * 40, "commit": "b" * 40}, Path("first"))
        second = ({"parent_commit": "c" * 40, "commit": "d" * 40}, Path("second"))
        with self.assertRaisesRegex(FatalActivationError, "bootstrap"):
            order_candidates_by_ancestry([first, second], None)


class FatalActivationTests(unittest.TestCase):
    def test_fatal_inconsistency_stops_later_candidates_but_ordinary_failure_does_not(self):
        from deploy.activator import ActivationError, FatalActivationError
        from deploy.activator_entrypoint import activate_candidates

        calls = []

        class OrdinaryThenGood:
            def activate(self, path):
                calls.append(path.name)
                if path.name == "bad":
                    raise ActivationError("bad candidate")

        isolated = []
        activate_candidates(OrdinaryThenGood(), [(1, Path("bad")), (2, Path("good"))],
                            on_failure=lambda path, exc: isolated.append((path.name, type(exc).__name__)))
        self.assertEqual(calls, ["bad", "good"])
        self.assertEqual(isolated, [("bad", "ActivationError")])
        calls.clear()

        class FatalThenLater:
            def activate(self, path):
                calls.append(path.name)
                if path.name == "fatal":
                    raise FatalActivationError("committed activation state inconsistent")

        with self.assertRaises(FatalActivationError):
            activate_candidates(FatalThenLater(), [(1, Path("fatal")), (2, Path("later"))])
        self.assertEqual(calls, ["fatal"])


class WikiPathSafetyTests(unittest.TestCase):
    def test_candidate_write_rejects_symlink_component_and_verifies_regular_bytes(self):
        from deploy.release_bundle import BundleError, write_candidate_regular

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "wiki").mkdir()
            outside = root / "outside"
            outside.mkdir()
            (root / "wiki" / "linked").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(BundleError, "symlink"):
                write_candidate_regular(root, "wiki/linked/note.md", b"candidate")
            target = write_candidate_regular(root, "wiki/safe/note.md", b"candidate")
            self.assertEqual(target.read_bytes(), b"candidate")
            details = target.lstat()
            self.assertTrue(target.is_file())
            self.assertEqual(details.st_nlink, 1)

    def test_prepare_change_rejects_symlinked_rough_source(self):
        from deploy.release_bundle import BundleError, ReleasePublisher

        git_env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository = root / "repository"; remote = root / "remote.git"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, env=git_env, check=True, stdout=subprocess.DEVNULL)
            rough_root = repository / "ingestion/rough"; rough_root.mkdir(parents=True)
            target = rough_root / "target.md"
            target.write_text("---\nstatus: pending_review\n---\n\nAnswer\n", encoding="utf-8")
            (rough_root / "item.md").symlink_to("target.md")
            subprocess.run(["git", "add", "."], cwd=repository, env=git_env, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-m", "seed"], cwd=repository, env=git_env, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "init", "--bare", str(remote)], env=git_env, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "push", str(remote), "main:main"], cwd=repository, env=git_env, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], env=git_env, check=True)
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, env=git_env, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
            tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repository, env=git_env, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
            raw = target.read_bytes()
            decision = {"action": "approve", "decision_id": "decision-symlink-01", "snapshot_commit": commit,
                        "snapshot_tree": tree, "rough_path": "ingestion/rough/item.md",
                        "rough_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "wiki_path": "wiki/safe/note.md", "candidate_markdown": "published\n"}
            publisher = ReleasePublisher(str(remote), Ed25519PrivateKey.generate(), root / "unused", test_only_local_origin=True)
            with self.assertRaisesRegex(BundleError, "rough.*regular|rough.*symlink"):
                publisher.prepare_change(root / "approved", decision)


class BuilderIsolationTests(unittest.TestCase):
    def test_malformed_package_is_durably_isolated_and_later_package_builds(self):
        from deploy.builder_entrypoint import process_packages

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approved, builds, failures = root / "approved", root / "builds", root / "failures"
            approved.mkdir(); builds.mkdir()
            (approved / "00-bad").mkdir()
            (approved / "00-bad" / "approval.json").write_text("{bad json", encoding="utf-8")
            (approved / "01-good").mkdir()
            (approved / "01-good" / "approval.json").write_text(json.dumps({"decision_id": "decision-good-01", "nonce": "nonce-good-0001"}), encoding="utf-8")

            class Builder:
                def build(self, package, target):
                    if package.name != "01-good":
                        raise AssertionError("malformed package reached builder")
                    target.mkdir()
                    (target / "release.json").write_text("{}")

            process_packages(Builder(), approved, builds, failures)
            self.assertTrue((builds / "decision-good-01-nonce-good-0001").is_dir())
            records = list(failures.glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text())["status"], "isolated")


class OutcomeIsolationTests(unittest.TestCase):
    def test_malformed_outcome_is_isolated_and_valid_later_outcome_is_applied(self):
        from deploy.publisher_entrypoint import ingest_activation_outcomes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outcomes, states, quarantine = root / "outcomes", root / "states", root / "quarantine"
            outcomes.mkdir(); states.mkdir()
            published = {
                "status": "published", "decision_id": "decision-good-01", "nonce": "nonce-good-0001",
                "generation": "decision-good-01-nonce-good-0001", "commit": "1" * 40,
                "tree": "2" * 40, "bundle_sha256": "3" * 64, "decision_sha256": "6" * 64,
                "origin": "https://github.com/chenponsh/dek.git",
                "artifacts": {"dek-kb.json": "4" * 64, "site/index.html": "5" * 64},
            }
            (states / "decision-good-01.json").write_text(json.dumps(published))
            (outcomes / "00-malformed.json").write_text("{bad json")
            good = {
                "status": "succeeded", "schema_version": 2, "sequence": 1,
                "nonce": published["nonce"], "generation": published["generation"],
                "previous_generation": None, "commit": published["commit"], "tree": published["tree"],
                "bundle_sha256": published["bundle_sha256"], "artifacts": {"dek-kb.json": "4" * 64, "site/index.html": "5" * 64},
                "decision_id": published["decision_id"], "decision_sha256": "6" * 64,
                "origin": "https://github.com/chenponsh/dek.git", "proved_at": 1,
            }
            (outcomes / (published["nonce"] + ".json")).write_text(json.dumps(good))
            writes = {}
            ingest_activation_outcomes(outcomes, states, quarantine, lambda key, value: writes.__setitem__(key, value))
            self.assertEqual(writes[published["decision_id"]]["status"], "activated")
            self.assertEqual(len(list(quarantine.glob("*.json"))), 1)

    def test_success_outcome_with_artifact_mismatch_is_isolated(self):
        from deploy.publisher_entrypoint import ingest_activation_outcomes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); outcomes = root / "outcomes"; states = root / "states"; quarantine = root / "quarantine"
            outcomes.mkdir(); states.mkdir(); nonce = "nonce-good-0001"; decision_id = "decision-good-01"
            identity = {"decision_id": decision_id, "decision_sha256": "6" * 64, "nonce": nonce,
                        "generation": "decision-good-01-nonce-good-0001", "origin": "https://github.com/chenponsh/dek.git",
                        "commit": "1" * 40, "tree": "2" * 40, "bundle_sha256": "3" * 64,
                        "artifacts": {"dek-kb.json": "4" * 64, "site/index.html": "5" * 64}}
            (states / (decision_id + ".json")).write_text(json.dumps({"status": "published", **identity}))
            outcome = {"status": "succeeded", "schema_version": 2, "sequence": 1,
                       "previous_generation": None, "proved_at": 1, **identity,
                       "artifacts": {**identity["artifacts"], "site/index.html": "9" * 64}}
            (outcomes / (nonce + ".json")).write_text(json.dumps(outcome))
            writes = {}
            ingest_activation_outcomes(outcomes, states, quarantine, lambda key, value: writes.__setitem__(key, value))
            self.assertEqual(writes, {})
            self.assertEqual(len(list(quarantine.glob("*.json"))), 1)

    def test_outcome_symlink_and_filename_nonce_mismatch_are_isolated(self):
        from deploy.publisher_entrypoint import ingest_activation_outcomes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); outcomes = root / "outcomes"; states = root / "states"; quarantine = root / "quarantine"
            outcomes.mkdir(); states.mkdir()
            target = root / "outside.json"; target.write_text("{}")
            (outcomes / "linked.json").symlink_to(target)
            mismatch = {"status": "succeeded", "nonce": "different-nonce"}
            (outcomes / "claimed-nonce.json").write_text(json.dumps(mismatch))
            ingest_activation_outcomes(outcomes, states, quarantine, lambda *_: None)
            self.assertEqual(len(list(quarantine.glob("*.json"))), 2)


class ManualPublishTriggerTests(unittest.TestCase):
    """Nothing publishes automatically once approved: a reviewer must click
    "发布" in the review UI, which writes a marker that a systemd .path unit
    turns into a build -> publish -> activate chain (see deploy/systemd/
    dek-review-publish-manual.{path,service} and web/review_app.py's
    /publish route)."""

    def test_no_periodic_timer_exists_for_builder_publisher_or_activator(self):
        for name in ("dek-builder.timer", "dek-review-publish.timer", "dek-activator.timer"):
            self.assertFalse((Path("deploy/systemd") / name).exists(), name)

    def test_manual_publish_path_unit_watches_the_reviewer_trigger_marker(self):
        path_unit = Path("deploy/systemd/dek-review-publish-manual.path").read_text(encoding="utf-8")
        self.assertIn("PathExists=/var/lib/dek-review/state/publish-trigger-requested", path_unit)
        self.assertIn("Unit=dek-review-publish-manual.service", path_unit)

    def test_manual_publish_service_chains_builder_then_publisher_then_activator(self):
        text = Path("deploy/systemd/dek-review-publish-manual.service").read_text(encoding="utf-8")
        build = text.index("systemctl start --wait dek-builder.service")
        publish = text.index("systemctl start --wait dek-review-publish.service")
        activate = text.index("systemctl start --wait dek-activator.service")
        self.assertLess(build, publish)
        self.assertLess(publish, activate)

    def test_runbook_enables_the_manual_path_units_not_removed_timers(self):
        runbook = Path("deploy/PRODUCTION_ROLLOUT.md").read_text(encoding="utf-8")
        self.assertIn("dek-review-publish-manual.path", runbook)
        self.assertNotIn("dek-builder.timer", runbook)
        self.assertNotIn("dek-review-publish.timer", runbook)
        self.assertNotIn("dek-activator.timer", runbook)


class RealEntrypointPipelineTests(unittest.TestCase):
    def test_local_bare_remote_late_rejection_blocks_pushed_gate_activation(self):
        from deploy.activator import ActivationError, Activator, ActivatorConfig, atomic_json
        from deploy.activator_entrypoint import QueueBoundActivator
        from deploy.builder_entrypoint import process_packages
        from deploy.publisher_entrypoint import authorized_queue_snapshot, process_decision
        from deploy.release_bundle import BundleBuilder, ReleasePublisher
        from web.review import (MemoryFormNonceStore, ReviewService, append_record,
                                decision_mac, iter_valid_decisions)

        git_env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository = root / "repository"; remote = root / "remote.git"
            repository.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repository, env=git_env, check=True, stdout=subprocess.DEVNULL)
            rough_relative = "ingestion/rough/item.md"
            rough = repository / rough_relative; rough.parent.mkdir(parents=True)
            rough.write_text("---\nstatus: pending_review\nsource_item_key: sha256:item-1\nquestion: Test\n---\n\nAnswer\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repository, env=git_env, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-m", "seed"], cwd=repository, env=git_env, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "init", "--bare", str(remote)], env=git_env, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "push", str(remote), "main:main"], cwd=repository, env=git_env, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], env=git_env, check=True)
            bundle = root / "snapshot.bundle"
            subprocess.run(["git", "bundle", "create", str(bundle), "refs/heads/main"], cwd=repository, env=git_env, check=True)
            bundle_digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            archive = root / "archive"; archive.mkdir(); (archive / (bundle_digest + ".bundle")).write_bytes(bundle.read_bytes())

            class Snapshot:
                _digest = bundle_digest
                def current(self): return repository

            queue = root / "decisions" / "decisions.jsonl"; queue_key = b"queue-key-0123456789abcdef-0000"
            nonces = MemoryFormNonceStore(clock=lambda: 100)
            service = ReviewService(Snapshot(), queue, audit_key=b"audit-key-0123456789", queue_key=queue_key,
                                    nonces=nonces, clock=lambda: 100)
            binding = service._pending(repository)[0]
            form_nonce = nonces.issue("session", binding.path, 1000, str(repository))
            body = urlencode({"form_nonce": form_nonce, "rough_path": binding.path,
                              "rough_sha256": binding.sha256, "rough_version": binding.version,
                              "action": "approve", "wiki_path": "wiki/01_Test/01-0001.md",
                              "candidate_markdown": "---\nno: 1\n---\n\nPublished\n", "comment": ""}).encode()
            decision_id = service.submit_form(body, session_id="session", user_id="reviewer")
            decision = list(iter_valid_decisions(queue, queue_key))[0]
            self.assertEqual(decision["decision_id"], decision_id)

            signing_key = Ed25519PrivateKey.generate()
            publisher = ReleasePublisher(str(remote), signing_key, root / "unused-credential", test_only_local_origin=True)
            approved = root / "approved"; builds = root / "builds"; approved.mkdir(); builds.mkdir()
            self.assertEqual(process_decision(publisher, decision, approved, builds, archive), "wait")

            def fixed_runner(command, **kwargs):
                command = list(command)
                if "web.site" in command:
                    output = Path(command[command.index("--output") + 1]); output.mkdir(parents=True); (output / "index.html").write_text("ok")
                elif "qa.dek_qa.build_index" in command:
                    output = Path(command[command.index("--output") + 1]); output.write_text('{"version":4,"documents":[]}')
                return b""

            process_packages(BundleBuilder(signing_key.public_key(), runner=fixed_runner), approved, builds, root / "builder-failures")
            authorize = lambda current: authorized_queue_snapshot(__import__("web.review", fromlist=["x"]), queue, queue_key,
                                                                   current, root / "queue-bad", lambda _: None)
            self.assertEqual(process_decision(publisher, decision, approved, builds, archive, authorize), "pushed")
            build = next(path for path in builds.iterdir() if path.is_dir())
            gate = json.loads((build / "activation-ready.json").read_text())
            self.assertEqual(gate["decision_queue_sha256"], hashlib.sha256(queue.read_bytes()).hexdigest())

            rejection = dict(decision)
            rejection.update({"decision_id": "late-reject-12345678", "action": "reject", "wiki_path": "",
                              "candidate_markdown": "", "comment": "late rejection"})
            rejection["decision_mac"] = decision_mac(rejection, queue_key)
            append_record(queue, rejection)

            config = ActivatorConfig.under(root / "activate"); config = ActivatorConfig(builds, config.releases, config.control,
                    config.journal, config.outcomes, config.spent, config.active); config.prepare()
            parent = decision["snapshot_commit"]
            seed = {"schema_version": 2, "sequence": 1, "nonce": "seed-nonce-12345678", "generation": "seed-generation-0001",
                    "previous_generation": None, "commit": parent, "tree": "1" * 40, "bundle_sha256": "2" * 64,
                    "artifacts": {"dek-kb.json": "3" * 64, "site/index.html": "4" * 64}}
            atomic_json(config.active, seed)
            activator = Activator(config, proof_reader=lambda kind, expected: dict(expected), approval_key=signing_key.public_key())
            with self.assertRaisesRegex(ActivationError, "queue snapshot"):
                QueueBoundActivator(activator, queue).activate(build)
            self.assertEqual(json.loads(config.active.read_text())["generation"], seed["generation"])


if __name__ == "__main__":
    unittest.main()
