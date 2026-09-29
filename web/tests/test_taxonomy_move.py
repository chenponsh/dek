import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from deploy.release_bundle import ReleasePublisher
from web.taxonomy import inspect_wiki_target, rewrite_note_for_target


HOME = """---
aliases:
  - "#{name}"
tags:
  - "{name}"
master:
cssclasses:
  - dv-compact-table
  - dv-overview-table
---

```dataview
TABLE WITHOUT ID
  file.link AS 项目,
  question AS 问题,
  source AS 来源,
  dateformat(date, "yyyy-MM-dd") AS 日期
FROM "wiki/{name}"
WHERE no != null
SORT file.folder ASC, no ASC
```
"""


NOTE = """---
no: 1
date: 2026-09-29
question: "如何分类？"
source: "测试"
tag_pages:
  - "[[01_原分类]]"
tags:
  - "01_原分类"
---

正文。![图](../_images/example.png)
"""

ROUGH = """---
status: pending_review
wiki_target:
---

待发布正文。
"""


class TaxonomyPublisherTests(unittest.TestCase):
    def _repo(self, root: Path) -> tuple[Path, str, str]:
        repo = root / "repo"
        (repo / "wiki/01_原分类").mkdir(parents=True)
        (repo / "wiki/02_新分类").mkdir(parents=True)
        (repo / "wiki/01_原分类/01_原分类.md").write_text(HOME.format(name="01_原分类"), encoding="utf-8")
        (repo / "wiki/02_新分类/02_新分类.md").write_text(HOME.format(name="02_新分类"), encoding="utf-8")
        (repo / "wiki/01_原分类/01-0001.md").write_text(NOTE, encoding="utf-8")
        (repo / "ingestion/rough").mkdir(parents=True)
        (repo / "ingestion/rough/a.md").write_text(ROUGH, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@i", "commit", "-qm", "init"], cwd=repo, check=True)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
        return repo, commit, tree

    def test_new_child_category_has_a_deterministic_home_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo, _, _ = self._repo(Path(temporary))
            plan = inspect_wiki_target(repo, "wiki/01_原分类/0102_新增分类/0102-0001.md")
            self.assertIsNotNone(plan)
            self.assertEqual(plan.page_path, "wiki/01_原分类/0102_新增分类/0102_新增分类.md")
            self.assertIn('master: "[[wiki/01_原分类/01_原分类]]"', plan.page_markdown)
            self.assertIn('FROM "wiki/01_原分类/0102_新增分类"', plan.page_markdown)

    def test_approval_creates_category_home_and_first_note_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit, tree = self._repo(root)
            target = "wiki/01_原分类/0102_新增分类/0102-0001.md"
            plan = inspect_wiki_target(repo, target)
            candidate = rewrite_note_for_target(NOTE, "wiki/01_原分类/01-0001.md", target)
            decision = {
                "schema_version": 3, "decision_id": "a" * 20, "action": "approve",
                "snapshot_commit": commit, "snapshot_tree": tree,
                "rough_path": "ingestion/rough/a.md",
                "rough_sha256": "sha256:" + hashlib.sha256(ROUGH.encode()).hexdigest(),
                "wiki_path": target, "candidate_markdown": candidate,
                "category_page_path": plan.page_path,
                "category_page_markdown": plan.page_markdown,
            }
            publisher = ReleasePublisher(str(repo), Ed25519PrivateKey.generate(), root / "missing", test_only_local_origin=True)
            output = root / "prepared"
            publisher.prepare_change(output, decision)
            check = root / "check"
            subprocess.run(["git", "clone", "-q", "--no-checkout", str(output / "repository.bundle"), str(check)], check=True)
            home = subprocess.check_output(["git", "show", f"origin/dek-approved:{plan.page_path}"], cwd=check, text=True)
            note = subprocess.check_output(["git", "show", f"origin/dek-approved:{target}"], cwd=check, text=True)
            self.assertEqual(home, plan.page_markdown)
            self.assertEqual(note, candidate)

    def test_move_rewrites_taxonomy_assets_and_publishes_redirect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, commit, tree = self._repo(root)
            source = "wiki/01_原分类/01-0001.md"
            target = "wiki/02_新分类/02-0002.md"
            candidate = rewrite_note_for_target(NOTE, source, target)
            decision = {
                "schema_version": 3, "decision_id": "m" * 20, "action": "move",
                "snapshot_commit": commit, "snapshot_tree": tree,
                "source_wiki_path": source,
                "source_wiki_sha256": "sha256:" + hashlib.sha256(NOTE.encode()).hexdigest(),
                "wiki_path": target, "candidate_markdown": candidate,
                "category_page_path": "", "category_page_markdown": "",
            }
            publisher = ReleasePublisher(str(repo), Ed25519PrivateKey.generate(), root / "missing", test_only_local_origin=True)
            output = root / "prepared"
            publisher.prepare_change(output, decision)
            check = root / "check"
            subprocess.run(["git", "clone", "-q", "--no-checkout", str(output / "repository.bundle"), str(check)], check=True)
            moved = subprocess.check_output(["git", "show", f"origin/dek-approved:{target}"], cwd=check, text=True)
            self.assertEqual(moved, candidate)
            self.assertIn("no: 2", moved)
            self.assertIn('  - "02_新分类"', moved)
            self.assertIn("../_images/example.png", moved)
            missing = subprocess.run(
                ["git", "cat-file", "-e", f"origin/dek-approved:{source}"], cwd=check,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.assertNotEqual(missing.returncode, 0)
            redirects = json.loads(subprocess.check_output(
                ["git", "show", "origin/dek-approved:wiki/_redirects.json"], cwd=check, text=True,
            ))
            self.assertEqual(redirects[source], target)


if __name__ == "__main__":
    unittest.main()
