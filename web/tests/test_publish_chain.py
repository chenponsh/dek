"""Approvals are published on top of what is current, and several of them as one chain.

Before this, every approval was rebuilt from the review snapshot, so as soon as origin moved
(another approval, an ingest, a code push) the push was rejected as non-fast-forward: only the
first approval per snapshot could ever be published.
"""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deploy.activator_entrypoint import order_candidates_by_ancestry
from deploy.release_bundle import BundleError, ReleasePublisher

ROUGH = "---\nstatus: pending_review\nwiki_target:\n---\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n| {q} | A | 2026-01-01 |\n"


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@invalid", "-c", "protocol.file.allow=always", *args],
                          cwd=cwd, check=check, text=True, capture_output=True).stdout.strip()


class PublishChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        work = self.root / "work"
        (work / "ingestion/rough").mkdir(parents=True)
        (work / "wiki/01_A").mkdir(parents=True)
        for name in ("a", "b", "c"):
            (work / f"ingestion/rough/{name}.md").write_text(ROUGH.format(q=f"问题{name}"), encoding="utf-8")
        git(work, "init", "-q", "-b", "main")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "init")
        self.origin = self.root / "origin.git"
        git(self.root, "clone", "-q", "--bare", str(work), str(self.origin))
        self.work = work
        self.snapshot = git(work, "rev-parse", "HEAD")
        self.tree = git(work, "rev-parse", "HEAD^{tree}")
        self.publisher = ReleasePublisher(str(self.origin), Ed25519PrivateKey.generate(), self.root / "no-credential", test_only_local_origin=True)

    def tearDown(self):
        self.temp.cleanup()

    def decision(self, name, wiki_path=None, candidate=None, snapshot=None, tree=None):
        raw = (self.work / f"ingestion/rough/{name}.md").read_bytes()
        return {"decision_id": f"decision-{name}-0123456789", "action": "approve",
                "snapshot_commit": snapshot or self.snapshot, "snapshot_tree": tree or self.tree,
                "rough_path": f"ingestion/rough/{name}.md", "rough_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "wiki_path": wiki_path or f"wiki/01_A/01-000{'abc'.index(name) + 1}.md",
                "candidate_markdown": candidate or f"---\nno: 1\n---\n\n内容{name}\n"}

    def prepare(self, decision, chain_from=None):
        out = self.root / f"pkg-{decision['decision_id']}"
        kwargs = {"chain_from": chain_from} if chain_from is not None else {}
        approval = self.publisher.prepare_change(out, decision, **kwargs)
        return out, approval

    def push(self, package):
        approval = json.loads((package / "approval.json").read_text())
        clone = self.root / f"push-{package.name}"
        git(self.root, "clone", "--no-checkout", "-q", str(package / "repository.bundle"), str(clone))
        self.publisher._push_remote(clone, approval["commit"])
        return approval["commit"]

    def origin_head(self):
        return git(self.origin, "rev-parse", "main")

    def advance_origin(self, name="other.txt"):
        """Someone else pushes to origin after the review snapshot (an ingest, a code push, another approval)."""
        git(self.work, "checkout", "-q", "main")
        (self.work / name).write_text("later\n", encoding="utf-8")
        git(self.work, "add", name)
        git(self.work, "commit", "-qm", "later commit")
        git(self.work, "push", "-q", str(self.origin), "main")

    def chain(self, package, approval):
        return {"bundle": str(package / "repository.bundle"), "commit": approval["commit"]}

    def test_the_change_goes_on_top_of_the_current_tip_not_the_older_snapshot(self):
        self.advance_origin()
        package, approval = self.prepare(self.decision("a"))
        tip = self.origin_head()
        clone = self.root / "check"
        git(self.root, "clone", "--no-checkout", "-q", str(package / "repository.bundle"), str(clone))
        self.assertEqual(git(clone, "rev-parse", f"{approval['commit']}^"), tip)                  # its parent is what origin holds now
        self.assertEqual(git(clone, "show", f"{approval['commit']}:other.txt"), "later")         # the later commit is still there
        self.assertEqual(self.push(package), approval["commit"])                                 # so the push is a fast-forward
        self.assertEqual(self.origin_head(), approval["commit"])

    def test_an_approval_made_before_origin_moved_still_publishes(self):
        decision = self.decision("a")                                     # reviewed against the snapshot ...
        self.advance_origin("ingest-1.txt"); self.advance_origin("ingest-2.txt")   # ... then origin moves twice
        package, approval = self.prepare(decision)
        self.push(package)
        self.assertEqual(self.origin_head(), approval["commit"])
        self.assertIn("内容a", git(self.origin, "show", "main:wiki/01_A/01-0001.md"))

    def test_several_approvals_on_one_snapshot_publish_as_a_chain(self):
        d = [self.decision(n) for n in "abc"]
        first, a1 = self.prepare(d[0])
        second, a2 = self.prepare(d[1], self.chain(first, a1))
        third, a3 = self.prepare(d[2], self.chain(second, a2))
        self.assertEqual(a2["parent_commit"], a1["commit"])                # the activator's ordering rule: each one's parent is the one before
        self.assertEqual(a3["parent_commit"], a2["commit"])
        for package in (first, second, third):
            self.push(package)                                             # every push is a fast-forward
        self.assertEqual(self.origin_head(), a3["commit"])
        for n, path in zip("abc", ("01-0001", "01-0002", "01-0003")):
            self.assertIn(f"内容{n}", git(self.origin, "show", f"main:wiki/01_A/{path}.md"))
        for n in "abc":                                                    # each draft is marked promoted with its path
            text = git(self.origin, "show", f"main:ingestion/rough/{n}.md")
            self.assertIn("status: promoted", text)
            self.assertIn(f"wiki_target: wiki/01_A/01-000{'abc'.index(n) + 1}.md", text)

    def test_pushing_the_last_of_a_chain_first_publishes_all_of_it_and_the_rest_are_already_in(self):
        d = [self.decision(n) for n in "ab"]
        first, a1 = self.prepare(d[0])
        second, a2 = self.prepare(d[1], self.chain(first, a1))
        self.push(second)                                                  # order of processing does not matter ...
        self.assertEqual(self.origin_head(), a2["commit"])
        self.assertEqual(self.push(first), a1["commit"])                   # ... the earlier one is already an ancestor: benign
        self.assertEqual(self.origin_head(), a2["commit"])

    def test_the_activator_orders_a_chain_by_ancestry(self):
        earlier, earlier_approval = self.prepare(self.decision("c"))         # an already published decision the chain starts after
        self.push(earlier)
        first, a1 = self.prepare(self.decision("a"))
        second, a2 = self.prepare(self.decision("b"), self.chain(first, a1))
        self.assertEqual(a1["parent_commit"], earlier_approval["commit"])
        active = {"decision_id": "earlier", "commit": earlier_approval["commit"]}
        ordered = order_candidates_by_ancestry([(a2, "second"), (a1, "first")], active)        # deliberately shuffled
        self.assertEqual([name for _, name in ordered], ["first", "second"])

    def test_a_package_built_on_something_older_than_the_tip_is_not_used_as_a_base(self):
        stale, stale_approval = self.prepare(self.decision("a"))          # prepared on the snapshot
        self.advance_origin()                                             # origin then moves
        package, approval = self.prepare(self.decision("b"), self.chain(stale, stale_approval))
        clone = self.root / "check2"
        git(self.root, "clone", "--no-checkout", "-q", str(package / "repository.bundle"), str(clone))
        self.assertEqual(git(clone, "rev-parse", f"{approval['commit']}^"), self.origin_head())      # on the tip ...
        with self.assertRaises(subprocess.CalledProcessError):
            git(clone, "show", f"{approval['commit']}:wiki/01_A/01-0001.md")                        # ... without the stale package's change
        self.push(package)                                                # and it publishes

    def test_a_bad_chain_reference_is_ignored(self):
        bad = ({"bundle": str(self.root / "missing.bundle"), "commit": "0" * 40}, {"bundle": "", "commit": "nope"}, {})
        for name, chain in zip("abc", bad):
            with self.subTest(chain=chain):
                tip = self.origin_head()
                package, approval = self.prepare(self.decision(name), chain)
                clone = self.root / f"check-{name}"
                git(self.root, "clone", "--no-checkout", "-q", str(package / "repository.bundle"), str(clone))
                self.assertEqual(git(clone, "rev-parse", f"{approval['commit']}^"), tip)      # simply built on the tip
                self.push(package)
                self.assertEqual(self.origin_head(), approval["commit"])

    def test_two_approvals_naming_the_same_path_with_different_content_are_still_refused(self):
        first, a1 = self.prepare(self.decision("a", wiki_path="wiki/01_A/01-0001.md"))
        with self.assertRaisesRegex(BundleError, "wiki_path already published with different content"):
            self.prepare(self.decision("b", wiki_path="wiki/01_A/01-0001.md", candidate="---\nno: 1\n---\n\n不同的内容\n"), self.chain(first, a1))

    def test_a_draft_that_changed_since_it_was_reviewed_is_refused(self):
        decision = self.decision("a")
        (self.work / "ingestion/rough/a.md").write_text(ROUGH.format(q="被改过"), encoding="utf-8")
        git(self.work, "add", "-A"); git(self.work, "commit", "-qm", "edit draft"); git(self.work, "push", "-q", str(self.origin), "main")
        with self.assertRaisesRegex(BundleError, "rough binding changed"):
            self.prepare(decision)

    def test_a_snapshot_that_is_not_in_the_published_history_is_refused(self):
        other = self.root / "other"
        (other / "ingestion/rough").mkdir(parents=True)
        (other / "ingestion/rough/a.md").write_text(ROUGH.format(q="别的历史"), encoding="utf-8")
        git(other, "init", "-q", "-b", "main"); git(other, "add", "-A"); git(other, "commit", "-qm", "unrelated")
        # the identity check passes only for commits the origin really has; a rewritten history no longer contains the snapshot
        self.advance_origin()
        git(self.work, "checkout", "-q", "--orphan", "rewritten"); git(self.work, "commit", "-qm", "rewritten history", "--allow-empty")
        git(self.work, "push", "-q", "--force", str(self.origin), "rewritten:main")
        with self.assertRaises(BundleError):
            self.prepare(self.decision("a"))

    def test_process_decision_passes_the_chain_only_when_there_is_one(self):
        from deploy.publisher_entrypoint import process_decision
        seen = []

        class Recorder:
            def verify_review_snapshot(self, *args): pass
            def prepare_change(self, output, decision, **kwargs):
                seen.append(kwargs)
                output.mkdir()
                (output / "approval.json").write_text(json.dumps({"decision_id": decision["decision_id"], "nonce": decision["decision_id"]}))
        approved, builds, archive = self.root / "approved", self.root / "builds", self.root / "archive"
        for directory in (approved, builds, archive): directory.mkdir()
        for name in ("one", "two"):
            (archive / ("f" * 64 + ".bundle")).write_bytes(b"")
        decision = {"decision_id": "decision-one-0123456789", "snapshot_bundle_sha256": "f" * 64, "snapshot_commit": "x", "snapshot_tree": "y"}
        self.assertEqual(process_decision(Recorder(), decision, approved, builds, archive), "wait")
        chained = dict(decision, decision_id="decision-two-0123456789")
        process_decision(Recorder(), chained, approved, builds, archive, chain_from={"bundle": "b", "commit": "c"})
        self.assertEqual(seen, [{}, {"chain_from": {"bundle": "b", "commit": "c"}}])


if __name__ == "__main__":
    unittest.main()
