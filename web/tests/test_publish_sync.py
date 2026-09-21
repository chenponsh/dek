"""A sync release publishes removals from wiki/ without a reviewer decision, and nothing else.

It exists so that deleting content (a reset, a retraction) reaches the live site right away.
The rule that keeps it safe: compared with the last published commit, wiki/ may only lose files.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deploy.activator_entrypoint import order_candidates_by_ancestry
from deploy.release_bundle import BundleError, ReleasePublisher

ROUGH = "---\nstatus: pending_review\nwiki_target:\n---\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n| 问 | A | 2026-01-01 |\n"


def git(cwd, *args, author=("t", "t@invalid")):
    return subprocess.run(["git", "-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}", "-c", "protocol.file.allow=always", *args],
                          cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


class PublishSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        (self.work / "wiki/01_A").mkdir(parents=True)
        (self.work / "ingestion/rough").mkdir(parents=True)
        (self.work / "source").mkdir()
        for n in range(1, 4):
            (self.work / f"wiki/01_A/01-000{n}.md").write_text(f"---\nno: {n}\n---\n\n条目{n}\n", encoding="utf-8")
        (self.work / "ingestion/rough/a.md").write_text(ROUGH, encoding="utf-8")
        git(self.work, "init", "-q", "-b", "main"); git(self.work, "add", "-A"); git(self.work, "commit", "-qm", "init")
        # the last published decision: what the live site was built from
        (self.work / "wiki/01_A/01-0004.md").write_text("---\nno: 4\n---\n\n条目4\n", encoding="utf-8")
        git(self.work, "add", "-A"); git(self.work, "commit", "-qm", "publish: decision-old-0123456789", author=("DEK Publisher", "publisher@invalid"))
        self.published = git(self.work, "rev-parse", "HEAD")
        self.origin = self.root / "origin.git"
        git(self.root, "clone", "-q", "--bare", str(self.work), str(self.origin))
        self.publisher = ReleasePublisher(str(self.origin), Ed25519PrivateKey.generate(), self.root / "no-credential", test_only_local_origin=True)

    def tearDown(self):
        self.temp.cleanup()

    def push_from_work(self, message="change"):
        git(self.work, "add", "-A"); git(self.work, "commit", "-qm", message); git(self.work, "push", "-q", str(self.origin), "main")

    def prepare(self):
        out = self.root / f"sync-{len(list(self.root.glob('sync-*')))}"
        approval = self.publisher.prepare_sync(out)
        return out, approval

    def push(self, package):
        approval = json.loads((package / "approval.json").read_text())
        clone = self.root / f"push-{package.name}"
        git(self.root, "clone", "--no-checkout", "-q", str(package / "repository.bundle"), str(clone))
        self.publisher._push_remote(clone, approval["commit"])
        return approval["commit"]

    def head(self):
        return git(self.origin, "rev-parse", "main")

    def test_removals_are_published_as_a_release_that_needs_no_decision(self):
        (self.work / "wiki/01_A/01-0002.md").unlink(); (self.work / "wiki/01_A/01-0003.md").unlink()
        self.push_from_work("delete two entries")
        tip_tree = git(self.origin, "rev-parse", "main^{tree}")
        package, approval = self.prepare()
        self.assertTrue(approval["decision_id"].startswith("sync-"))
        self.assertEqual(approval["parent_commit"], self.published)          # the activator chains it after the live generation
        self.push(package)
        self.assertEqual(self.head(), approval["commit"])
        left = git(self.origin, "ls-tree", "-r", "--name-only", "main", "wiki").split("\n")
        self.assertEqual(left, ["wiki/01_A/01-0001.md", "wiki/01_A/01-0004.md"])
        # the release commit is one the publisher wrote (it counts as a published decision from now on)
        self.assertEqual(git(self.origin, "log", "-1", "--format=%ae|%s", "main"), f"publisher@invalid|publish: {approval['decision_id']}")
        self.assertEqual(git(self.origin, "rev-parse", "main^{tree}"), tip_tree)   # the sync commit adds nothing: same tree as the tip it sits on

    def test_the_release_tree_is_exactly_the_tip_and_the_bundle_matches_the_signed_approval(self):
        (self.work / "wiki/01_A/01-0001.md").unlink()
        self.push_from_work("delete")
        tip = self.head()
        package, approval = self.prepare()
        self.assertEqual(approval["tree"], git(self.origin, "rev-parse", f"{tip}^{{tree}}"))     # nothing is added to what is on origin
        self.assertEqual(json.loads((package / "approval.json").read_text()), approval)
        self.assertTrue((package / "approval.sig").is_file())
        self.assertEqual(len(approval["decision_sha256"]), 64)

    def test_the_same_state_always_gives_the_same_release_id(self):
        (self.work / "wiki/01_A/01-0002.md").unlink(); self.push_from_work("delete")
        first, a = self.prepare(); second, b = self.prepare()
        self.assertEqual(a["decision_id"], b["decision_id"])
        self.assertEqual(a["decision_sha256"], b["decision_sha256"])

    def test_an_entry_that_is_added_or_changed_is_refused(self):
        (self.work / "wiki/01_A/01-0002.md").unlink()
        (self.work / "wiki/01_A/01-0005.md").write_text("---\nno: 5\n---\n\n没审核过的新条目\n", encoding="utf-8")     # a removal AND an unreviewed addition
        self.push_from_work("mixed")
        with self.assertRaisesRegex(BundleError, "need a reviewed decision"):
            self.prepare()

    def test_a_changed_entry_is_refused_even_alone(self):
        (self.work / "wiki/01_A/01-0001.md").write_text("---\nno: 1\n---\n\n被改过的内容\n", encoding="utf-8")
        self.push_from_work("edit")
        with self.assertRaisesRegex(BundleError, "need a reviewed decision"):
            self.prepare()
        self.assertFalse(list(self.root.glob("sync-*")))                     # and no package was left behind

    def test_a_removal_hidden_among_other_changes_is_still_a_removal_and_the_others_do_not_matter(self):
        (self.work / "wiki/01_A/01-0003.md").unlink()
        (self.work / "source/note.md").write_text("ingested\n", encoding="utf-8")                # sources / code / drafts move with every release anyway
        (self.work / "ingestion/rough/b.md").write_text(ROUGH, encoding="utf-8")
        self.push_from_work("delete + ingest")
        package, approval = self.prepare()
        self.push(package)
        self.assertIn("ingested", git(self.origin, "show", "main:source/note.md"))

    def test_deleting_and_re_adding_the_same_content_is_no_change(self):
        (self.work / "wiki/01_A/01-0002.md").unlink(); self.push_from_work("delete")
        (self.work / "wiki/01_A/01-0002.md").write_text("---\nno: 2\n---\n\n条目2\n", encoding="utf-8"); self.push_from_work("put back")
        self.assertIsNone(self.publisher.prepare_sync(self.root / "none"))
        self.assertFalse((self.root / "none").exists())

    def test_nothing_to_remove_gives_none(self):
        self.assertIsNone(self.publisher.prepare_sync(self.root / "none"))
        (self.work / "source/x.md").write_text("x\n", encoding="utf-8"); self.push_from_work("source only")
        self.assertIsNone(self.publisher.prepare_sync(self.root / "none2"))

    def test_once_published_there_is_nothing_left_to_sync(self):
        (self.work / "wiki/01_A/01-0002.md").unlink(); self.push_from_work("delete")
        package, _ = self.prepare(); self.push(package)
        self.assertIsNone(self.publisher.prepare_sync(self.root / "again"))

    def test_a_later_decision_chains_after_the_sync_release(self):
        (self.work / "wiki/01_A/01-0002.md").unlink(); self.push_from_work("delete")
        package, sync = self.prepare(); self.push(package)
        git(self.work, "pull", "-q", str(self.origin), "main")
        raw = (self.work / "ingestion/rough/a.md").read_bytes()
        import hashlib
        snapshot = git(self.work, "rev-parse", "HEAD"); tree = git(self.work, "rev-parse", "HEAD^{tree}")
        decision = {"decision_id": "decision-a-0123456789", "action": "approve", "snapshot_commit": snapshot, "snapshot_tree": tree,
                    "rough_path": "ingestion/rough/a.md", "rough_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    "wiki_path": "wiki/01_A/01-0005.md", "candidate_markdown": "---\nno: 5\n---\n\n新\n"}
        approval = self.publisher.prepare_change(self.root / "decision-pkg", decision)
        self.assertEqual(approval["parent_commit"], sync["commit"])            # the activator's rule: parent == the live (sync) generation
        active = {"decision_id": sync["decision_id"], "commit": sync["commit"]}
        self.assertEqual([x for x, _ in order_candidates_by_ancestry([(approval, "d")], active)], [approval])

    def test_too_many_removals_at_once_are_refused(self):
        many = self.work / "wiki/02_B"; many.mkdir()
        for n in range(self.publisher.SYNC_MAX_REMOVALS + 1):
            (many / f"02-{n:04d}.md").write_text("x\n", encoding="utf-8")
        git(self.work, "add", "-A"); git(self.work, "commit", "-qm", "publish: decision-many-0123456789", author=("DEK Publisher", "publisher@invalid"))
        git(self.work, "push", "-q", str(self.origin), "main")
        # the last published commit now contains them; remove them all
        for f in many.glob("*.md"): f.unlink()
        self.push_from_work("delete all")
        with self.assertRaisesRegex(BundleError, "too many removals"):
            self.prepare()


class SyncEntrypointTests(unittest.TestCase):
    """How the publisher service drives a sync release: once per state of origin, decisions first, failures contained."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.approved, self.builds, self.state = self.root / "approved", self.root / "builds", self.root / "state"
        for directory in (self.approved, self.builds, self.state):
            directory.mkdir()
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def publisher(self, approval="sync-0123456789abcdef", *, fail=None):
        outer = self

        class Fake:
            def prepare_sync(self, output):
                outer.calls.append("prepare")
                if fail: raise fail
                if approval is None: return None
                output.mkdir()
                meta = {"decision_id": approval, "nonce": approval, "commit": "c" * 40}
                (output / "approval.json").write_text(json.dumps(meta))
                (output / "repository.bundle").write_bytes(b"")
                return meta
            def finalize(self, build): outer.calls.append("finalize"); (build / "release.sig").write_bytes(b"s")
            def publish(self, build, **kwargs): outer.calls.append(("publish", sorted(kwargs)))
        return Fake()

    def build_done(self, release_id="sync-0123456789abcdef", *, signed=False):
        build = self.builds / f"{release_id}-{release_id}"
        build.mkdir()
        (build / "release.json").write_text(json.dumps({"decision_sha256": "d" * 64, "nonce": release_id, "generation": f"{release_id}-{release_id}", "origin": "o",
                                                       "commit": "c" * 40, "tree": "t" * 40, "bundle_sha256": "b" * 64, "artifacts": {"site/index.html": "a" * 64}, "parent_commit": "p" * 40}))
        if signed: (build / "release.sig").write_bytes(b"s")

    def test_nothing_to_remove_does_nothing(self):
        from deploy.publisher_entrypoint import process_sync
        self.assertIsNone(process_sync(self.publisher(None), self.approved, self.builds))
        self.assertEqual(list(self.approved.iterdir()), [])

    def test_a_release_is_prepared_once_and_waits_for_the_builder(self):
        from deploy.publisher_entrypoint import process_sync
        self.assertEqual(process_sync(self.publisher(), self.approved, self.builds), ("sync-0123456789abcdef", "wait"))
        self.assertEqual([p.name for p in self.approved.iterdir()], ["sync-0123456789abcdef"])
        self.assertEqual(process_sync(self.publisher(), self.approved, self.builds), ("sync-0123456789abcdef", "wait"))   # the same state: no second package
        self.assertEqual([p.name for p in self.approved.iterdir()], ["sync-0123456789abcdef"])

    def test_a_built_release_is_signed_and_pushed_with_the_queue_snapshot(self):
        from deploy.publisher_entrypoint import process_sync
        process_sync(self.publisher(), self.approved, self.builds); self.build_done()
        self.calls.clear()
        result = process_sync(self.publisher(), self.approved, self.builds, lambda: {"decision_queue_sha256": "q" * 64, "decision_queue_size": 0})
        self.assertEqual(result, ("sync-0123456789abcdef", "pushed"))
        self.assertEqual(self.calls, ["prepare", "finalize", ("publish", ["queue_snapshot"])])

    def test_an_already_signed_build_is_not_signed_again(self):
        from deploy.publisher_entrypoint import process_sync
        process_sync(self.publisher(), self.approved, self.builds); self.build_done(signed=True); self.calls.clear()
        process_sync(self.publisher(), self.approved, self.builds)
        self.assertNotIn("finalize", self.calls)

    def test_the_state_record_carries_what_the_activator_checks(self):
        from deploy.publisher_entrypoint import published_result
        (self.approved / "sync-0123456789abcdef").mkdir(); (self.approved / "sync-0123456789abcdef/approval.json").write_text(json.dumps({"decision_id": "sync-0123456789abcdef", "nonce": "sync-0123456789abcdef"}))
        self.build_done()
        result = published_result(self.approved, self.builds, "sync-0123456789abcdef")
        self.assertEqual(result["status"], "published")
        self.assertEqual({k for k in result} - {"parent_commit"}, {"status", "decision_id", "decision_sha256", "nonce", "generation", "origin", "commit", "tree", "bundle_sha256", "artifacts"})
        self.assertEqual(result["parent_commit"], "p" * 40)

    def run_sync(self, decisions, publisher, states=None):
        from deploy.publisher_entrypoint import run_sync_release
        for name, status in (states or {}).items():
            (self.state / f"{name}.json").write_text(json.dumps(status if isinstance(status, dict) else {"status": status}))
        written = {}
        class Review:
            @staticmethod
            def queue_lock(queue, read_only=False):
                import contextlib
                return contextlib.nullcontext()
        queue = self.root / "decisions.jsonl"; queue.write_text("")
        run_sync_release({"web.review": Review}, publisher, decisions, self.approved, self.builds, self.state, queue, lambda i, v: written.update({i: v}))
        return written

    def test_a_decision_that_is_not_yet_published_goes_first(self):
        written = self.run_sync([{"decision_id": "decision-1-0123456789"}], self.publisher(), states={})
        self.assertEqual(self.calls, [])                                   # sync did not even look
        self.assertEqual(written, {})
        written = self.run_sync([{"decision_id": "decision-1-0123456789"}], self.publisher(), states={"decision-1-0123456789": "failed"})
        self.assertEqual(self.calls, [])

    def test_once_every_decision_is_published_the_sync_runs_and_records_its_state(self):
        decisions = [{"decision_id": "decision-1-0123456789"}, {"decision_id": "decision-2-0123456789"}]
        self.build_done(signed=True)
        (self.approved / "sync-0123456789abcdef").mkdir()
        (self.approved / "sync-0123456789abcdef/approval.json").write_text(json.dumps({"decision_id": "sync-0123456789abcdef", "nonce": "sync-0123456789abcdef"}))
        written = self.run_sync(decisions, self.publisher(), states={"decision-1-0123456789": "activated", "decision-2-0123456789": "published"})
        self.assertIn("prepare", self.calls)
        self.assertEqual(list(written), ["sync-0123456789abcdef"])
        self.assertEqual(written["sync-0123456789abcdef"]["status"], "published")

    def test_a_decision_that_can_never_be_published_does_not_hold_the_sync_back(self):
        dead = {"status": "failed", "retryable": False, "last_error": "rough source is unreadable"}
        self.build_done(signed=True)
        (self.approved / "sync-0123456789abcdef").mkdir()
        (self.approved / "sync-0123456789abcdef/approval.json").write_text(json.dumps({"decision_id": "sync-0123456789abcdef", "nonce": "sync-0123456789abcdef"}))
        written = self.run_sync([{"decision_id": "decision-1-0123456789"}, {"decision_id": "decision-2-0123456789"}], self.publisher(),
                                states={"decision-1-0123456789": dead, "decision-2-0123456789": "published"})
        self.assertIn("prepare", self.calls)
        self.assertEqual(list(written), ["sync-0123456789abcdef"])

    def test_a_retryable_failure_or_a_pending_decision_still_holds_the_sync_back(self):
        for held in ({"status": "failed", "retryable": True}, {"status": "failed"}, {"status": "pending"}):
            with self.subTest(held=held):
                self.calls.clear()
                self.run_sync([{"decision_id": "decision-1-0123456789"}, {"decision_id": "decision-2-0123456789"}], self.publisher(),
                              states={"decision-1-0123456789": {"status": "failed", "retryable": False}, "decision-2-0123456789": held})
                self.assertEqual(self.calls, [])

    def test_a_failing_sync_never_raises_into_the_decisions_run(self):
        from deploy.release_bundle import BundleError
        for error in (BundleError("wiki changes other than removals need a reviewed decision"), RuntimeError("boom"), SystemExit("stop")):
            with self.subTest(error=type(error).__name__):
                self.calls.clear()
                self.assertEqual(self.run_sync([], self.publisher(fail=error)), {})
                self.assertEqual(self.calls, ["prepare"])


if __name__ == "__main__":
    unittest.main()
