import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from urllib.parse import urlencode

from deploy.notify_entrypoint import (
    MAX_TITLE_CHARS, _bulleted, failure_reviewer_ids, new_pending_titles,
    main, new_publish_success_messages, new_workflow_failure_messages,
    notification_title, newly_stuck_approved_titles, reviewer_ids,
)
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

    def test_failure_reviewer_ids_is_independent_from_all_reviewers(self):
        with unittest.mock.patch.dict(os.environ, {
            "DEK_REVIEWER_IDS": "peng,other", "DEK_FAILURE_REVIEWER_IDS": "peng",
        }, clear=True):
            self.assertEqual(reviewer_ids(), ["peng", "other"])
            self.assertEqual(failure_reviewer_ids(), ["peng"])


class BulletedTests(unittest.TestCase):
    def test_bulleted_lists_up_to_ten_and_notes_the_remainder(self):
        titles = [f"问题{i}" for i in range(12)]
        message = _bulleted("标题", titles)
        self.assertIn("问题0", message)
        self.assertIn("问题9", message)
        self.assertNotIn("问题10", message)
        self.assertIn("还有 2 条", message)


class NotificationTitleTests(unittest.TestCase):
    class Item:
        def __init__(self, content, title="20260920_CDE_共性问题-受理共性问题_增量_1.md"):
            self.content, self.title = content, title

    @staticmethod
    def rough(question):
        return ROUGH.replace("| Q | A | 2026-09-14 |", f"| {question} | A | 2026-09-14 |")

    def test_shows_the_question_not_the_file_name(self):
        item = self.Item(self.rough("已上市的化学药品创新药如何申请药品试验数据保护？"))
        self.assertEqual(notification_title(item), "已上市的化学药品创新药如何申请药品试验数据保护？")

    def test_a_leading_question_label_is_dropped(self):
        self.assertEqual(notification_title(self.Item(self.rough("问：如何提交资料？"))), "如何提交资料？")

    def test_a_multi_line_question_becomes_one_line(self):
        self.assertEqual(notification_title(self.Item(self.rough("第一行<br>第二行"))), "第一行 第二行")

    def test_a_very_long_question_is_cut_with_an_ellipsis(self):
        title = notification_title(self.Item(self.rough("问" * 200)))
        self.assertEqual(len(title), MAX_TITLE_CHARS + 1)
        self.assertTrue(title.endswith("…"))

    def test_a_draft_without_a_question_falls_back_to_its_file_name(self):
        item = self.Item("---\nstatus: pending_review\n---\n\n没有表格\n")
        self.assertEqual(notification_title(item), "20260920_CDE_共性问题-受理共性问题_增量_1.md")

    def test_the_message_lists_questions(self):
        message = _bulleted("有 2 条新内容待审核", ["问题一？", "问题二？"])
        self.assertIn("· 问题一？", message)
        self.assertNotIn(".md", message)


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
        self.assertEqual(titles, ["Q"])
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
        self.assertEqual(newly_stuck_approved_titles(self.service, 1800, later, state), ["Q"])
        self.assertEqual(newly_stuck_approved_titles(self.service, 1800, later + 60, state), [])

    def test_stuck_item_that_publishes_can_be_reported_again_if_stuck_a_second_time(self):
        self.approve()
        state = self.root / "state/stuck-seen.json"
        later = self.clock_value + 1800
        self.assertEqual(newly_stuck_approved_titles(self.service, 1800, later, state), ["Q"])
        (self.root / "repo/ingestion/rough/a.md").unlink()  # simulates a real publish removing the rough file
        self.assertEqual(newly_stuck_approved_titles(self.service, 1800, later + 60, state), [])
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), [])

    def test_publish_success_is_baselined_then_each_new_publish_is_reported_once(self):
        request = self.root / "state/publish-requested-at"
        self.service.publish_state_path = request
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(str(self.clock_value), encoding="utf-8")
        refresh = request.with_name("publish-stage-refresh")
        finished = request.with_name(self.service.PUBLISH_FINISHED)
        refresh.touch(); finished.touch()
        os.utime(refresh, (self.clock_value + 75, self.clock_value + 75))
        os.utime(finished, (self.clock_value + 90, self.clock_value + 90))
        state = self.root / "state/publish-success-seen.json"
        self.assertEqual(new_publish_success_messages(self.service, state), [])

        self.clock_value += 200
        self.service.record_publish_request(self.clock_value)
        refresh.touch(); finished.touch()
        os.utime(refresh, (self.clock_value + 70, self.clock_value + 70))
        os.utime(finished, (self.clock_value + 80, self.clock_value + 80))
        messages = new_publish_success_messages(self.service, state)
        self.assertEqual(len(messages), 1)
        self.assertIn("发布成功", messages[0])
        self.assertIn("用时 1 分 20 秒", messages[0])
        self.assertEqual(new_publish_success_messages(self.service, state), [])

    def test_publish_and_ingest_failures_are_reported_once(self):
        ingest_request = self.root / "state/ingest-requested-at"
        publish_request = self.root / "state/publish-requested-at"
        ingest_request.parent.mkdir(parents=True, exist_ok=True)
        ingest_request.write_text(str(self.clock_value - 700), encoding="utf-8")
        publish_request.write_text(str(self.clock_value - 60), encoding="utf-8")
        self.service.ingest_state_path = ingest_request
        self.service.publish_state_path = publish_request
        finished = publish_request.with_name(self.service.PUBLISH_FINISHED)
        finished.touch(); os.utime(finished, (self.clock_value - 50, self.clock_value - 50))
        state = self.root / "state/workflow-failure-seen.json"
        missing_marker = self.root / "input/source-ingest-failed"
        messages = new_workflow_failure_messages(self.service, missing_marker, state, self.clock_value)
        self.assertEqual(len(messages), 2)
        self.assertTrue(any("数据更新失败" in value for value in messages))
        self.assertTrue(any("发布失败" in value for value in messages))
        self.assertEqual(new_workflow_failure_messages(self.service, missing_marker, state, self.clock_value), [])

    def test_success_and_pending_go_to_all_but_failures_only_go_to_designated_reviewer(self):
        key = self.root / "decision-key"
        key.write_bytes(b"queue-key-0123456789abcdef")
        sent = []

        class Client:
            def send_work_notification(self, ids, message):
                sent.append((ids, message))

        args = []
        for name in (
            "bundle", "clones", "queue", "pending-state", "stuck-state",
            "publish-success-state", "workflow-failure-state", "ingest-request",
            "ingest-summary", "publish-request", "source-failure-marker",
        ):
            args.extend((f"--{name}", str(self.root / name)))
        environment = {
            "DEK_REVIEWER_IDS": "peng,other",
            "DEK_FAILURE_REVIEWER_IDS": "peng",
            "DEK_REVIEW_DECISION_KEY_FILE": str(key),
            "DEK_REVIEW_AUDIT_KEY": "audit-key-0123456789abcdef",
        }
        with (unittest.mock.patch.dict(os.environ, environment, clear=True),
              unittest.mock.patch("deploy.notify_entrypoint.IsolatedReviewClone", return_value=object()),
              unittest.mock.patch("deploy.notify_entrypoint.ReviewService", return_value=object()),
              unittest.mock.patch("deploy.notify_entrypoint.new_pending_titles", return_value=["待审核问题"]),
              unittest.mock.patch("deploy.notify_entrypoint.newly_stuck_approved_titles", return_value=[]),
              unittest.mock.patch("deploy.notify_entrypoint.new_publish_success_messages", return_value=["发布成功"]),
              unittest.mock.patch("deploy.notify_entrypoint.new_workflow_failure_messages", return_value=["发布失败"]),
              unittest.mock.patch("deploy.notify_entrypoint.dingtalk_client", return_value=Client())):
            self.assertEqual(main(args), 0)

        self.assertEqual(sent[0][0], ["peng"])
        self.assertIn("发布失败", sent[0][1])
        self.assertEqual(sent[1][0], ["peng", "other"])
        self.assertIn("待审核问题", sent[1][1])
        self.assertIn("发布成功", sent[1][1])


if __name__ == "__main__":
    unittest.main()
