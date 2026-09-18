import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from urllib.parse import urlencode

from deploy.notify_entrypoint import _bulleted, new_pending_titles, newly_stuck_approved_titles, reviewer_ids
from web.review import MemoryFormNonceStore, ReviewService, rough_binding


ROUGH = """---
date: 2026-09-14
source: "[[source/example]]"
status: pending_review
source_item_key: sha256:item-version
recommended_tags:
wiki_target:
reviewed_at:
---

## 新增问答

| 问题 | 解答 | 发布日期 |
| --- | --- | --- |
| Q | A | 2026-09-14 |
"""

CANDIDATE = """---
no: 1
date: 2026-09-14
question: Q
source: "[[source/example]]"
tag_pages:
  - "[[wiki/01_Test/01_Test]]"
tags:
  - "01_Test"
---

A
"""


class ReviewerIdsTests(unittest.TestCase):
    def test_reviewer_ids_splits_and_trims_the_comma_joined_env_var(self):
        with unittest.mock.patch.dict(os.environ, {"DEK_REVIEWER_IDS": " a, b ,,c"}, clear=False):
            self.assertEqual(reviewer_ids(), ["a", "b", "c"])

    def test_reviewer_ids_is_empty_when_unset(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(reviewer_ids(), [])


class BulletedTests(unittest.TestCase):
    def test_bulleted_lists_up_to_ten_and_notes_the_remainder(self):
        titles = [f"问题{i}" for i in range(12)]
        message = _bulleted("标题", titles)
        self.assertIn("问题0", message)
        self.assertIn("问题9", message)
        self.assertNotIn("问题10", message)
        self.assertIn("还有 2 条", message)


class NotifyEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "repo/ingestion/rough").mkdir(parents=True)
        (self.root / "repo/ingestion/rough/a.md").write_text(ROUGH, encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root / "repo", check=True)
        subprocess.run(["git", "add", "."], cwd=self.root / "repo", check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-qm", "init"], cwd=self.root / "repo", check=True)
        self.queue = self.root / "state/decisions.jsonl"
        self.clock_value = 1_900_000_000
        self.nonces = MemoryFormNonceStore(clock=lambda: self.clock_value)
        self.service = ReviewService(
            self.root / "repo", self.queue,
            audit_key=b"audit-key-0123456789abcdef",
            queue_key=b"queue-key-0123456789abcdef",
            nonces=self.nonces,
            clock=lambda: self.clock_value,
        )

    def tearDown(self):
        self.temp.cleanup()

    def approve(self):
        path = "ingestion/rough/a.md"
        binding = rough_binding(self.root / "repo" / path, relative=path)
        values = {
            "form_nonce": self.nonces.issue("s", path, self.clock_value + 900, str(self.root / "repo")),
            "rough_path": path, "rough_sha256": binding.sha256, "rough_version": binding.version,
            "action": "approve", "wiki_path": "wiki/01_Test/01-0001.md", "candidate_markdown": CANDIDATE, "comment": "",
        }
        self.service.submit_form(urlencode(values).encode(), session_id="s", user_id="reviewer-1")

    def test_first_run_reports_the_pending_item_and_persists_state(self):
        state = self.root / "state/pending-seen.json"
        titles = new_pending_titles(self.service, state)
        self.assertEqual(titles, ["a.md"])
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), ["ingestion/rough/a.md"])
        self.assertEqual(new_pending_titles(self.service, state), [])

    def test_recently_approved_item_is_not_yet_stuck(self):
        self.approve()
        state = self.root / "state/stuck-seen.json"
        stuck = newly_stuck_approved_titles(self.service, 1800, self.clock_value + 60, state)
        self.assertEqual(stuck, [])

    def test_approved_item_past_the_threshold_is_reported_once(self):
        self.approve()
        state = self.root / "state/stuck-seen.json"
        later = self.clock_value + 1800
        self.assertEqual(newly_stuck_approved_titles(self.service, 1800, later, state), ["a.md"])
        self.assertEqual(newly_stuck_approved_titles(self.service, 1800, later + 60, state), [])

    def test_stuck_item_that_publishes_can_be_reported_again_if_stuck_a_second_time(self):
        self.approve()
        state = self.root / "state/stuck-seen.json"
        later = self.clock_value + 1800
        self.assertEqual(newly_stuck_approved_titles(self.service, 1800, later, state), ["a.md"])
        (self.root / "repo/ingestion/rough/a.md").unlink()  # simulates a real publish removing the rough file
        self.assertEqual(newly_stuck_approved_titles(self.service, 1800, later + 60, state), [])
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
