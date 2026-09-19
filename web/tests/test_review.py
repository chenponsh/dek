import json
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import patch
import os

from web.review import (
    MemoryFormNonceStore,
    ReviewerLabelStore,
    ReviewError,
    ReviewService,
    candidate_draft,
    default_wiki_path,
    rough_binding,
    sanitize_nickname,
    source_urls,
    validate_decision,
    decision_mac,
)


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


class ReviewServiceTests(unittest.TestCase):

    def test_decision_id_boundary_matches_generation_component_contract(self):
        key = b"queue-key-0123456789abcdef"
        def record(decision_id):
            value={"schema_version":2,"record_type":"decision","decision_id":decision_id,"created_at":"2026-09-15T00:00:00+00:00","reviewer_digest":"hmac-sha256:"+"1"*64,"action":"approve","rough_path":"ingestion/rough/a.md","rough_sha256":"sha256:"+"2"*64,"rough_version":"sha256:item","wiki_path":"wiki/a.md","candidate_markdown":"x","comment":"","snapshot_commit":"3"*40,"snapshot_tree":"4"*40,"snapshot_bundle_sha256":"5"*64}
            value["decision_mac"]=decision_mac(value,key)
            return value
        self.assertEqual(validate_decision(record("d" * 79), key)["decision_id"], "d" * 79)
        with self.assertRaisesRegex(ReviewError, "schema"):
            validate_decision(record("d" * 80), key)

    def test_validate_decision_rejects_tampering_and_requires_snapshot_binding(self):
        key=b"queue-key-0123456789abcdef"
        value={"schema_version":2,"record_type":"decision","decision_id":"decision-12345678","created_at":"2026-09-15T00:00:00+00:00","reviewer_digest":"hmac-sha256:"+"1"*64,"action":"approve","rough_path":"ingestion/rough/a.md","rough_sha256":"sha256:"+"2"*64,"rough_version":"sha256:item","wiki_path":"wiki/a.md","candidate_markdown":"x","comment":"","snapshot_commit":"3"*40,"snapshot_tree":"4"*40,"snapshot_bundle_sha256":"5"*64}
        value["decision_mac"]=decision_mac(value,key)
        self.assertEqual(validate_decision(value,key),value)
        with self.assertRaisesRegex(ReviewError,"MAC"):
            validate_decision({**value,"candidate_markdown":"evil"},key)
        missing=dict(value); missing.pop("snapshot_tree"); missing["decision_mac"]=decision_mac(missing,key)
        with self.assertRaisesRegex(ReviewError,"schema"):
            validate_decision(missing,key)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "repo/ingestion/rough").mkdir(parents=True)
        self.rough = self.root / "repo/ingestion/rough/pending.md"
        self.rough.write_text(ROUGH, encoding="utf-8")
        os.system(f"git -C {self.root/'repo'} init -q -b main && git -C {self.root/'repo'} add . && git -C {self.root/'repo'} -c user.name=t -c user.email=t@invalid commit -qm init")
        self.queue = self.root / "state/decisions.jsonl"
        self.nonces = MemoryFormNonceStore(clock=lambda: 1_900_000_000)
        self.service = ReviewService(
            self.root / "repo", self.queue,
            audit_key=b"audit-key-0123456789abcdef",
            queue_key=b"queue-key-0123456789abcdef",
            nonces=self.nonces,
            clock=lambda: 1_900_000_000,
        )

    def tearDown(self):
        self.temp.cleanup()

    def form(self, *, session_id="opaque-session", nonce=None, **changes):
        binding = rough_binding(self.rough)
        values = {
            "form_nonce": nonce or self.nonces.issue(session_id, "ingestion/rough/pending.md", 1_900_000_900),
            "rough_path": "ingestion/rough/pending.md",
            "rough_sha256": binding.sha256,
            "rough_version": binding.version,
            "action": "approve",
            "wiki_path": "wiki/01_Test/01-0001.md",
            "candidate_markdown": CANDIDATE,
        }
        values.update(changes)
        return urlencode(values).encode()

    def test_render_issues_distinct_single_use_nonce_for_each_form(self):
        first = self.service.render("opaque-session").decode()
        second = self.service.render("opaque-session").decode()
        self.assertIn('name="form_nonce"', first)
        self.assertNotEqual(first.split('name="form_nonce" value="', 1)[1].split('"', 1)[0], second.split('name="form_nonce" value="', 1)[1].split('"', 1)[0])
        self.assertNotIn("csrf", first.lower())

    def test_nonce_is_bound_to_session_and_consumed_once(self):
        nonce = self.nonces.issue("opaque-session", "ingestion/rough/pending.md", 1_900_000_900)
        with self.assertRaisesRegex(ReviewError, "nonce"):
            self.service.submit_form(self.form(session_id="other", nonce=nonce), session_id="other", user_id="enterprise-user")
        nonce = self.nonces.issue("opaque-session", "ingestion/rough/pending.md", 1_900_000_900)
        self.service.submit_form(self.form(nonce=nonce), session_id="opaque-session", user_id="enterprise-user")
        with self.assertRaisesRegex(ReviewError, "nonce"):
            self.service.submit_form(self.form(nonce=nonce), session_id="opaque-session", user_id="enterprise-user")

    def test_a_downstream_validation_failure_does_not_burn_the_nonce(self):
        # Live symptom: a reviewer hit "invalid path" (400), then retried
        # without reloading and got "invalid form nonce" (403) -- the first
        # failed attempt had already consumed their only nonce, so they had
        # no way to retry short of a full page reload.
        nonce = self.nonces.issue("opaque-session", "ingestion/rough/pending.md", 1_900_000_900)
        with self.assertRaises(ReviewError):
            self.service.submit_form(self.form(nonce=nonce, action="approve", wiki_path=""),
                                     session_id="opaque-session", user_id="enterprise-user")
        # Retry with the same nonce and a corrected field -- must succeed.
        self.service.submit_form(self.form(nonce=nonce, action="approve"),
                                 session_id="opaque-session", user_id="enterprise-user")
        record = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "approve")

    def test_nonce_accepts_a_matching_non_ascii_rough_path(self):
        path = "ingestion/rough/20260917_回退审核_0102-0001.md"
        nonce = self.nonces.issue("opaque-session", path, 1_900_000_900, "/snapshot")
        self.assertEqual(self.nonces.consume(nonce, "opaque-session", path), "/snapshot")

    def test_decision_schema_remains_worker_compatible_and_hides_identity(self):
        self.service.submit_form(self.form(), session_id="opaque-session", user_id="enterprise-user")
        record = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(self.queue.stat().st_mode), 0o660)
        self.assertEqual(set(record), {
            "schema_version", "record_type", "decision_id", "created_at", "reviewer_digest",
            "action", "rough_path", "rough_sha256", "rough_version", "wiki_path",
            "candidate_markdown", "comment", "snapshot_commit", "snapshot_tree", "snapshot_bundle_sha256", "decision_mac",
        })
        self.assertNotIn("enterprise-user", self.queue.read_text(encoding="utf-8"))
        self.assertRegex(record["reviewer_digest"], r"^hmac-sha256:[0-9a-f]{64}$")
        self.assertRegex(record["decision_mac"], r"^hmac-sha256:[0-9a-f]{64}$")

    def test_first_queue_creation_fsyncs_queue_parent(self):
        synced = []
        real_fsync = os.fsync
        def observe(descriptor):
            try: synced.append(Path(f"/proc/self/fd/{descriptor}").resolve())
            except OSError: pass
            return real_fsync(descriptor)
        with patch("web.review.os.fsync", side_effect=observe):
            self.service.submit_form(self.form(), session_id="opaque-session", user_id="enterprise-user")
        self.assertIn(self.queue.parent.resolve(), synced)

    def test_submission_rejects_path_traversal_and_stale_rough_binding(self):
        bad_path = self.form(
            rough_path="ingestion/rough/../../AGENTS.md",
            nonce=self.nonces.issue("opaque-session", "ingestion/rough/../../AGENTS.md", 1_900_000_900),
        )
        with self.assertRaises(ReviewError):
            self.service.submit_form(bad_path, session_id="opaque-session", user_id="enterprise-user")

        stale_hash = self.form(
            rough_sha256="sha256:" + "0" * 64,
            nonce=self.nonces.issue("opaque-session", "ingestion/rough/pending.md", 1_900_000_900),
        )
        with self.assertRaisesRegex(ReviewError, "rough changed"):
            self.service.submit_form(stale_hash, session_id="opaque-session", user_id="enterprise-user")
        self.assertFalse(self.queue.exists())

    def test_approve_normalizes_crlf_in_candidate_markdown_to_lf(self):
        # Browsers submit <textarea> form fields with CRLF line endings per the
        # HTML spec regardless of OS, but .gitattributes normalizes *.md to LF
        # on `git add`. Without normalizing here first, release_bundle.py's
        # prepare_change() byte-compares the queued candidate against what git
        # actually committed and always finds a CRLF/LF mismatch -- every real
        # browser-submitted approval fails to publish.
        crlf_candidate = CANDIDATE.replace("\n", "\r\n")
        self.service.submit_form(
            self.form(candidate_markdown=crlf_candidate), session_id="opaque-session", user_id="enterprise-user",
        )
        record = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertNotIn("\r", record["candidate_markdown"])
        self.assertEqual(record["candidate_markdown"], CANDIDATE)

    def test_approve_with_malformed_candidate_yaml_is_rejected(self):
        bad_yaml = self.form(candidate_markdown="---\nno: 1\n  bad: [unterminated\n---\n\nA\n")
        with self.assertRaisesRegex(ReviewError, "YAML"):
            self.service.submit_form(bad_yaml, session_id="opaque-session", user_id="enterprise-user")
        self.assertFalse(self.queue.exists())


VERIFY_ROUGH = """---
date: 2026-09-17
published_date: 2017-10-10
ingested_at: 2026-09-17
source: "[[CDE_共性问题-常见一般性技术问题]]"
status: pending_review
source_item_key: sha256:verify-only-0102-0001
recommended_tags:
  - "[[01_注册申报]]"
  - "[[0102_注册分类]]"
wiki_target: "[[wiki/01_注册申报/0102_注册分类/0102-0001]]"
reviewed_at:
---

## 新增问答

| 问题 | 解答 | 发布日期 |
| --- | --- | --- |
| 《决定》第三条定义是什么？<br>第二行问题 | 解答正文：含管道符 \\| 与换行<br>第二段 | 2017-10-10 |
"""


class ReviewerLabelStoreTests(unittest.TestCase):
    def test_nickname_is_sanitized_and_bounded(self):
        self.assertEqual(sanitize_nickname("彭文艳"), "彭文艳")
        self.assertEqual(sanitize_nickname("  彭文艳  "), "彭文艳")
        self.assertEqual(sanitize_nickname("彭\x00文\x1b艳\n"), "彭文艳")
        self.assertEqual(sanitize_nickname(""), "同事")
        self.assertEqual(sanitize_nickname("　"), "同事")
        self.assertLessEqual(len(sanitize_nickname("名" * 200)), 40)

    def test_labels_append_and_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "reviewer-labels.jsonl"
            store = ReviewerLabelStore(path)
            store.append("decision-1", "彭文艳")
            store.append("decision-2", "张三")
            reloaded = ReviewerLabelStore(path).load()
            self.assertEqual(reloaded, {"decision-1": "彭文艳", "decision-2": "张三"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "repo/ingestion/rough").mkdir(parents=True)
        self.rough = self.root / "repo/ingestion/rough/pending.md"
        self.rough.write_text(ROUGH, encoding="utf-8")
        self.other = self.root / "repo/ingestion/rough/other.md"
        self.other.write_text(ROUGH.replace("2026-09-14", "2026-08-01"), encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root / "repo", check=True)
        subprocess.run(["git", "add", "."], cwd=self.root / "repo", check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-qm", "init"], cwd=self.root / "repo", check=True)
        self.queue = self.root / "state/decisions.jsonl"
        self.labels = ReviewerLabelStore(self.root / "state/reviewer-labels.jsonl")
        self.clock_value = 1_900_000_000
        self.nonces = MemoryFormNonceStore(clock=lambda: self.clock_value)
        self.service = ReviewService(
            self.root / "repo", self.queue,
            audit_key=b"audit-key-0123456789abcdef",
            queue_key=b"queue-key-0123456789abcdef",
            nonces=self.nonces,
            clock=lambda: self.clock_value,
            labels=self.labels,
        )

    def tearDown(self):
        self.temp.cleanup()

    def decide(self, *, session_id="opaque-session", path="ingestion/rough/pending.md", action="reject", nickname="彭文艳", **changes):
        binding = rough_binding(self.root / "repo" / path, relative=path)
        values = {
            "form_nonce": self.nonces.issue(session_id, path, self.clock_value + 900, str(self.root / "repo")),
            "rough_path": path,
            "rough_sha256": binding.sha256,
            "rough_version": binding.version,
            "action": action,
            "wiki_path": "wiki/01_Test/01-0001.md" if action == "approve" else "",
            "candidate_markdown": CANDIDATE if action == "approve" else "",
        }
        values.update(changes)
        return self.service.submit_form(urlencode(values).encode(), session_id=session_id, user_id="enterprise-user", reviewer_label=nickname)

    def test_new_item_is_pending_with_stable_identity_and_metadata(self):
        items = self.service.list_items()
        self.assertEqual(len(items), 2)
        pending = next(item for item in items if item.path == "ingestion/rough/pending.md")
        self.assertRegex(pending.identity, r"^[0-9a-f]{16}$")
        self.assertEqual(pending.status, "pending")
        self.assertEqual(pending.status_label, "待审核")
        self.assertEqual(pending.published_date, "2026-09-14")
        self.assertEqual(pending.reviewer, "")
        self.assertEqual(pending.decided_at, "")

    def test_decision_sets_status_and_records_reviewer_nickname(self):
        self.decide(action="approve")
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        self.assertEqual(item.status, "approved")
        self.assertEqual(item.status_label, "已批准待发布")
        self.assertEqual(item.reviewer, "彭文艳")
        self.assertEqual(item.decided_at, "2030-03-17T17:46:40+00:00")

    def test_reject_ignores_leftover_wiki_path_and_candidate(self):
        # The two decision buttons (批准/拒绝) share one <form>; a
        # reviewer who typed a candidate then clicked 拒绝 without
        # clearing those fields used to get a 400 ("non-approve decision
        # cannot include a candidate") instead of the decision going through.
        self.decide(action="reject", wiki_path="wiki/01_Test/01-0001.md", candidate_markdown=CANDIDATE)
        record = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "reject")
        self.assertEqual(record["wiki_path"], "")
        self.assertEqual(record["candidate_markdown"], "")

    def test_decision_time_displays_in_beijing_time_not_stored_utc(self):
        # Stored/audited value stays UTC (decision_mac, queue records); only
        # the reviewer-facing table and history switch to +08:00 for display.
        self.decide(action="approve")
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        self.assertEqual(item.decided_at, "2030-03-17T17:46:40+00:00")
        page = self.service.render_list("opaque-session").decode("utf-8")
        self.assertIn("2030-03-18 01:46:40", page)
        self.assertNotIn("2030-03-17T17:46:40+00:00", page)
        detail = self.service.render_item("opaque-session", item.identity).decode("utf-8")
        self.assertIn("2030-03-18 01:46:40", detail)
        self.assertNotIn("2030-03-17T17:46:40+00:00", detail)

    def test_redecision_keeps_history_and_list_shows_latest(self):
        self.decide(action="reject", nickname="彭文艳")
        self.clock_value += 60
        self.decide(action="approve", nickname="张三")
        lines = [line for line in self.queue.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        self.assertEqual(item.status, "approved")
        self.assertEqual(item.reviewer, "张三")

    def test_reject_needs_no_comment_and_records_none(self):
        self.decide(action="reject")
        first = json.loads(self.queue.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first["action"], "reject")
        # The signed record schema still carries the field (publisher and MAC
        # validation expect it), but it is always empty now.
        self.assertEqual(first["comment"], "")

    def test_a_stale_form_that_still_sends_a_comment_is_accepted_but_the_text_is_dropped(self):
        self.decide(action="reject", comment="旧页面里填的意见")
        record = json.loads(self.queue.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["comment"], "")
        self.assertNotIn("旧页面里填的意见", self.queue.read_text(encoding="utf-8"))

    def test_review_form_has_no_comment_field_and_history_no_comment_column(self):
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        page = self.service.render_item("opaque-session", item.identity).decode("utf-8")
        self.assertNotIn('name="comment"', page)
        self.assertNotIn("审核意见", page)
        self.assertIn('name="action" value="approve">批准</button>', page)
        self.assertIn('name="action" value="reject" class="action-reject">拒绝</button>', page)
        self.decide(action="reject")
        history = self.service.render_item("opaque-session", item.identity).decode("utf-8")
        self.assertIn("<th>决定</th><th>审核人</th><th>时间</th></tr>", history)
        self.assertNotIn("<th>意见</th>", history)

    def test_return_is_no_longer_an_accepted_action(self):
        with self.assertRaisesRegex(ReviewError, "invalid action"):
            self.decide(action="return")
        self.assertFalse(self.queue.exists())

    def test_a_legacy_return_record_leaves_the_item_pending_and_stays_in_its_history(self):
        self.decide(action="reject")
        record = json.loads(self.queue.read_text(encoding="utf-8"))
        record["action"] = "return"
        record["decision_mac"] = decision_mac({k: v for k, v in record.items() if k != "decision_mac"}, b"queue-key-0123456789abcdef")
        self.queue.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        self.assertEqual(item.status, "pending")
        page = self.service.render_item("opaque-session", item.identity).decode("utf-8")
        self.assertIn("退回澄清", page)

    def test_approve_of_a_vanished_rough_reports_published(self):
        self.decide(action="approve")
        self.rough.unlink()
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        self.assertEqual(item.status, "published")
        self.assertEqual(item.status_label, "已发布")

    def test_list_renders_rows_links_and_supports_search_and_status_filters(self):
        self.decide(action="reject")
        page = self.service.render_list("opaque-session").decode("utf-8")
        self.assertIn("待审核", page)
        self.assertIn("已拒绝", page)
        self.assertIn("/item/", page)
        self.assertNotIn("name=\"q\"", page)
        self.assertIn("ingestion/rough/other.md", page)

        filtered = self.service.render_list("opaque-session", query="other").decode("utf-8")
        self.assertIn("other.md", filtered)
        self.assertNotIn("pending.md", filtered)

        only_pending = self.service.render_list("opaque-session", status="pending").decode("utf-8")
        self.assertIn("other.md", only_pending)
        self.assertNotIn("pending.md", only_pending)
        self.assertIn('<div class="summary">共 1 条</div>', only_pending)

    def test_list_filter_links_show_record_counts(self):
        self.decide(action="approve")
        page = self.service.render_list("opaque-session").decode("utf-8")
        self.assertIn('>全部<sup class="filter-count">2</sup></a>', page)
        self.assertIn('>待审核<sup class="filter-count">1</sup></a>', page)
        self.assertIn('>已批准待发布<sup class="filter-count">1</sup></a>', page)
        self.assertIn('>已发布</a>', page)
        self.assertNotIn('已退回', page)
        self.assertNotIn('status=returned', page)
        self.assertIn('>已拒绝</a>', page)
        self.assertNotIn('<sup class="filter-count">0</sup>', page)

    def test_list_reuses_the_knowledge_base_chrome_with_a_resizable_sidebar(self):
        page = self.service.render_list("opaque-session", status="pending").decode("utf-8")
        self.assertIn('<link rel="stylesheet" href="/assets/style.css">', page)
        self.assertNotIn('data:image/', page)
        self.assertNotIn('class="brand-logo"', page)
        self.assertIn('<header><button id="menu-toggle" aria-label="打开目录">☰</button>', page)
        self.assertIn('<strong role="heading" aria-level="1">知识审核</strong>', page)
        # The sidebar's 首页 entry already leads back to the knowledge base.
        self.assertNotIn("kb-return-link", page)
        self.assertNotIn("返回知识库", page)
        self.assertIn('<strong role="heading" aria-level="1">知识审核</strong></header>', page)
        self.assertNotIn('<form class="logout-form"', page)
        self.assertNotIn('>退出审核<', page)
        self.assertIn('<aside class="sidebar">', page)
        self.assertIn('<nav id="nav-tree" data-manifest="/manifest.json" data-current=""></nav>', page)
        # The drag handle is created by app.js outside the scrolling sidebar.
        self.assertNotIn("sidebar-resize-handle", page)
        self.assertIn('<script src="/assets/app.js" defer></script>', page)
        self.assertIn('class="status-tab active" aria-current="page" href="/?status=pending"', page)
        self.assertNotIn('class="search-box"', page)
        self.assertNotIn('id="review-search"', page)
        self.assertNotIn('>搜索</button>', page)
        self.assertIn('<td class="status status-pending"><span class="status-dot" aria-hidden="true"></span>待审核</td>', page)
        # Chrome (fonts, colors, sidebar) is the literal knowledge-base stylesheet,
        # referenced via var(...) rather than a second hardcoded palette.
        self.assertIn('.content{margin-left:var(--sidebar-w)}', page)
        self.assertIn('background:var(--accent);color:#fff', page)
        self.assertIn('<colgroup><col class="col-index"><col class="col-task"><col class="col-status"><col class="col-reviewer"><col class="col-time"></colgroup>', page)
        self.assertIn('<thead><tr><th class="index">序号</th><th>内容</th><th>状态</th><th>审核人</th><th>处理时间</th></tr></thead>', page)
        self.assertNotIn("<th>待办</th>", page)
        self.assertIn(".col-index{width:60px}", page)
        # Index, status, reviewer and time columns fit their content on one line;
        # the 内容 column takes whatever is left.
        self.assertIn(".col-status,.col-reviewer,.col-time{width:1%}", page)
        self.assertIn(".table-wrap th,.table-wrap td.status,.table-wrap td.reviewer,.table-wrap td.time{white-space:nowrap}", page)
        self.assertNotIn(".col-task{", page)
        # Below its minimum width the table scrolls instead of squeezing 内容 to nothing.
        self.assertIn(".table-wrap td.content{max-width:0;min-width:240px}", page)
        self.assertIn("th.index,td.index{width:60px;min-width:60px;text-align:center}", page)
        self.assertNotIn('<th>操作</th>', page)
        self.assertNotIn('>去审核</a>', page)
        self.assertNotIn('>查看</a>', page)
        empty = self.service.render_list("opaque-session", status="approved").decode("utf-8")
        self.assertIn('<td colspan="5">没有符合条件的条目。</td>', empty)

    def test_review_sidebar_has_no_browse_title_above_the_tree(self):
        # The sidebar is a single group, so a title only cost a line of height.
        item = self.service.list_items()[0]
        for page in (self.service.render_list("opaque-session").decode("utf-8"),
                     self.service.render_item("opaque-session", item.identity).decode("utf-8")):
            self.assertNotIn("side-title", page)
            self.assertNotIn(">浏览<", page)
            self.assertIn('<aside class="sidebar"><nav id="nav-tree"', page)
            self.assertNotIn("sidebar-resize-handle", page)

    def test_content_cell_second_line_is_one_short_truncated_line_with_the_full_text_on_hover(self):
        long_source = "[[source/CDE/CDE_共性问题-常见一般性技术问题]]"
        rough = ROUGH.replace('source: "[[source/example]]"', f'source: "{long_source}"')
        (self.root / "repo/ingestion/rough/pending.md").write_text(rough, encoding="utf-8")
        page = self.service.render_list("opaque-session").decode("utf-8")
        cell = page[page.index("pending.md") - 300: page.index("pending.md") + 500]
        self.assertIn('title="ingestion/rough/pending.md · 来源：[[source/CDE/CDE_共性问题-常见一般性技术问题]]', cell)
        self.assertIn('>发布日期：2026-09-14 · pending.md · 来源：CDE_共性问题-常见一般性技术问题', cell)
        self.assertNotIn(">ingestion/rough/pending.md", cell)
        self.assertNotIn(">[[", cell)
        self.assertIn(".content-meta{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}", page)

    def test_browser_title_is_review_without_the_todo_wording(self):
        page = self.service.render_list("opaque-session").decode("utf-8")
        self.assertIn("<title>知识审核 · DEK</title>", page)
        self.assertNotIn("待办</title>", page)

    def make_pending_items(self, count):
        rough_dir = self.root / "repo" / "ingestion" / "rough"
        for number in range(count):
            (rough_dir / f"extra-{number:03d}.md").write_text(ROUGH.replace("2026-09-14", f"2026-07-{1 + number % 28:02d}"), encoding="utf-8")

    def row_numbers(self, page):
        return [int(n) for n in re.findall(r'<td class="meta index">(\d+)</td>', page)]

    def test_rows_are_numbered_and_the_list_is_paginated_fifteen_at_a_time(self):
        self.make_pending_items(45)  # 47 items in all
        first = self.service.render_list("opaque-session").decode("utf-8")
        self.assertEqual(self.row_numbers(first), list(range(1, 16)))
        self.assertIn('<div class="summary">共 47 条</div>', first)
        self.assertIn("第 1/4 页", first)
        second = self.service.render_list("opaque-session", page=2).decode("utf-8")
        self.assertEqual(self.row_numbers(second), list(range(16, 31)))
        third = self.service.render_list("opaque-session", page=3).decode("utf-8")
        self.assertEqual(self.row_numbers(third), list(range(31, 46)))
        fourth = self.service.render_list("opaque-session", page=4).decode("utf-8")
        self.assertEqual(self.row_numbers(fourth), [46, 47])
        self.assertIn("第 4/4 页", fourth)

    def test_pager_links_keep_the_filter_and_disable_the_ends(self):
        self.make_pending_items(45)
        page = self.service.render_list("opaque-session", status="pending", page=2).decode("utf-8")
        self.assertIn('<a href="/?status=pending">上一页</a>', page)
        self.assertIn('<a href="/?status=pending&amp;page=3">下一页</a>', page)
        self.assertIn('<span class="current" aria-current="page">2</span>', page)
        first = self.service.render_list("opaque-session").decode("utf-8")
        self.assertIn('<span class="disabled">上一页</span>', first)
        last = self.service.render_list("opaque-session", page=4).decode("utf-8")
        self.assertIn('<span class="disabled">下一页</span>', last)

    def test_page_size_can_be_overridden_within_bounds_and_numbering_follows_it(self):
        self.make_pending_items(45)  # 47 items
        second = self.service.render_list("opaque-session", page=2, page_size=5).decode("utf-8")
        self.assertEqual(self.row_numbers(second), [6, 7, 8, 9, 10])
        self.assertIn("第 2/10 页", second)
        self.assertIn('<a href="/?page_size=5">上一页</a>', second)
        self.assertIn('<a href="/?page=3&amp;page_size=5">下一页</a>', second)
        # Bounds: never below 5 or above 100 rows a page.
        self.assertEqual(len(self.row_numbers(self.service.render_list("opaque-session", page_size=1).decode("utf-8"))), 5)
        self.assertEqual(len(self.row_numbers(self.service.render_list("opaque-session", page_size=1000).decode("utf-8"))), 47)

    def test_page_size_travels_with_the_list_position_to_the_item_page(self):
        self.make_pending_items(45)
        page = self.service.render_list("opaque-session", page=2, page_size=5).decode("utf-8")
        link = re.search(r'<tr data-href="/item/([0-9a-f]{16})([^"]*)"', page)
        self.assertEqual(link.group(2), "?page=2&amp;page_size=5")
        detail = self.service.render_item("opaque-session", link.group(1), list_page=2, list_size=5).decode("utf-8")
        self.assertIn('<a href="/?page=2&amp;page_size=5">← 返回列表</a>', detail)
        self.assertIn('action="/decision?page=2&amp;page_size=5"', detail)

    def test_page_length_is_a_number_input_in_a_get_form_that_keeps_the_filter(self):
        self.make_pending_items(45)  # 47 items
        page = self.service.render_list("opaque-session", status="pending", page=2, page_size=30).decode("utf-8")
        form = page.split('<form method="get" action="/" class="page-size">', 1)[1].split("</form>", 1)[0]
        self.assertIn("每页", form)
        self.assertIn('<input type="number" name="page_size" min="5" max="100" step="1" value="30" inputmode="numeric" aria-label="每页条数">', form)
        self.assertIn('<input type="hidden" name="status" value="pending">', form)
        # Submitting starts again from page 1, so the page is never carried.
        self.assertNotIn('name="page"', form)
        self.assertNotIn("<a ", form)
        self.assertTrue(form.rstrip().endswith("条"))

    def test_page_length_input_defaults_to_15_and_omits_an_empty_filter(self):
        page = self.service.render_list("opaque-session").decode("utf-8")
        form = page.split('<form method="get" action="/" class="page-size">', 1)[1].split("</form>", 1)[0]
        self.assertIn('value="15"', form)
        self.assertNotIn('name="status"', form)

    def test_page_length_input_stays_when_everything_fits_on_one_page(self):
        page = self.service.render_list("opaque-session").decode("utf-8")
        self.assertEqual(self.row_numbers(page), [1, 2])
        self.assertIn('<form method="get" action="/" class="page-size">', page)
        self.assertIn("第 1/1 页", page)

    def test_status_tabs_carry_a_non_default_page_length_and_reset_the_page(self):
        self.make_pending_items(45)
        page = self.service.render_list("opaque-session", page=2, page_size=30).decode("utf-8")
        tabs = page.split('<nav class="status-tabs"', 1)[1].split("</nav>", 1)[0]
        self.assertIn('href="/?status=pending&amp;page_size=30">待审核', tabs)
        self.assertIn('href="/?status=&amp;page_size=30">全部', tabs)
        self.assertNotIn("page=2", tabs)
        default_tabs = self.service.render_list("opaque-session", page=2).decode("utf-8").split('<nav class="status-tabs"', 1)[1].split("</nav>", 1)[0]
        self.assertIn('href="/?status=pending">待审核', default_tabs)
        self.assertNotIn("page_size", default_tabs)

    def test_page_length_input_is_styled_and_wired_up(self):
        page = self.service.render_list("opaque-session").decode("utf-8")
        self.assertIn(".page-size input[type=number]{width:52px;box-sizing:border-box;padding:.3rem .5rem;text-align:center;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--muted);font:inherit;line-height:inherit;appearance:textfield;-moz-appearance:textfield}", page)
        self.assertIn(".page-size input[type=number]:focus{outline:none;border-color:var(--accent);background:var(--panel);color:var(--text)}", page)
        self.assertIn(".page-size input[type=number]::-webkit-inner-spin-button,.page-size input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}", page)
        self.assertNotIn(".page-size a", page)

    def test_paging_display_is_shown_for_an_empty_result_too(self):
        page = self.service.render_list("opaque-session", status="published").decode("utf-8")
        self.assertIn("没有符合条件的条目。", page)
        self.assertIn("第 1/1 页", page)

    def test_out_of_range_page_is_clamped(self):
        self.make_pending_items(45)
        self.assertEqual(self.row_numbers(self.service.render_list("opaque-session", page=99).decode("utf-8")), [46, 47])
        self.assertEqual(self.row_numbers(self.service.render_list("opaque-session", page=0).decode("utf-8")), list(range(1, 16)))

    def test_status_tabs_reset_to_the_first_page_and_keep_total_counts(self):
        self.make_pending_items(45)
        page = self.service.render_list("opaque-session", page=2).decode("utf-8")
        self.assertIn('href="/?status=pending">待审核<sup class="filter-count">47</sup>', page)
        self.assertNotIn('status=pending&amp;page', page.split('<nav class="status-tabs"', 1)[1].split("</nav>", 1)[0])

    def test_pagination_ordering_is_stable_and_pending_first_by_oldest_publish_date(self):
        self.make_pending_items(45)
        self.decide(action="reject")
        ids = re.findall(r'<tr data-href="/item/([0-9a-f]{16})', self.service.render_list("opaque-session").decode("utf-8"))
        again = re.findall(r'<tr data-href="/item/([0-9a-f]{16})', self.service.render_list("opaque-session").decode("utf-8"))
        self.assertEqual(ids, again)
        last = self.service.render_list("opaque-session", page=4).decode("utf-8")
        self.assertIn("已拒绝", last)  # decided items come after every pending one

    def test_rows_and_the_item_page_carry_the_list_position_so_back_returns_to_it(self):
        self.make_pending_items(45)
        page = self.service.render_list("opaque-session", status="pending", page=2).decode("utf-8")
        identity = re.search(r'<tr data-href="/item/([0-9a-f]{16})([^"]*)"', page)
        self.assertEqual(identity.group(2), "?status=pending&amp;page=2")
        detail = self.service.render_item("opaque-session", identity.group(1), list_status="pending", list_page=2).decode("utf-8")
        self.assertIn('<a href="/?status=pending&amp;page=2">← 返回列表</a>', detail)
        self.assertIn('action="/decision?status=pending&amp;page=2"', detail)
        plain = self.service.render_item("opaque-session", identity.group(1)).decode("utf-8")
        self.assertIn('<a href="/">← 返回列表</a>', plain)
        self.assertIn('action="/decision"', plain)

    def test_list_rows_carry_a_data_href_for_whole_row_navigation(self):
        item = self.service.list_items()[0]
        page = self.service.render_list("opaque-session").decode("utf-8")
        self.assertIn(f'<tr data-href="/item/{item.identity}">', page)

    def test_list_offers_a_manual_ingest_trigger_button(self):
        page = self.service.render_list("opaque-session").decode("utf-8")
        self.assertIn('<form method="post" action="/trigger-ingest" class="ingest-trigger-form">', page)
        self.assertIn('<button type="submit">立即拉取最新源</button>', page)

    def test_list_action_buttons_sit_together_on_the_right_ingest_then_publish(self):
        page = self.service.render_list("opaque-session").decode("utf-8")
        row = page.split('<div class="summary-row">', 1)[1].split('</div></div>', 1)[0]
        self.assertTrue(row.startswith('<div class="summary">共 '))
        self.assertIn('<div class="summary-actions">', row)
        actions = row.split('<div class="summary-actions">', 1)[1]
        self.assertLess(actions.index('class="ingest-trigger-form"'), actions.index('class="publish-trigger-form"'))
        self.assertIn(".summary-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-left:auto}", page)

    def test_list_action_buttons_do_not_change_colour_on_hover(self):
        page = self.service.render_list("opaque-session").decode("utf-8")
        # The ingest button's own rule was fully shadowed by button[type=submit]
        # except its hover, which turned the solid blue button pale grey.
        self.assertNotIn(".ingest-trigger-form button", page)
        self.assertIn(".summary-actions button[type=submit]:hover{filter:none;background:var(--accent)}", page)
        # The site-wide and form-wide hover rules stay untouched.
        self.assertIn("button[type=submit]:hover{filter:brightness(.94)}", page)

    def test_list_offers_a_manual_publish_button_with_approved_count(self):
        page = self.service.render_list("opaque-session").decode("utf-8")
        self.assertIn('<form method="post" action="/publish" class="publish-trigger-form">', page)
        self.assertIn('<button type="submit">发布已批准内容（0）</button>', page)

        self.decide(action="approve")
        page = self.service.render_list("opaque-session").decode("utf-8")
        self.assertIn('<button type="submit">发布已批准内容（1）</button>', page)

    def test_detail_page_shows_content_and_issues_its_own_nonce(self):
        identity = self.service.list_items()[0].identity
        page = self.service.render_item("opaque-session", identity).decode("utf-8")
        self.assertIn('name="form_nonce"', page)
        self.assertIn("ingestion/rough/", page)
        self.assertIn('<button type="submit" name="action" value="approve">批准</button>', page)
        self.assertIn('<button type="submit" name="action" value="reject" class="action-reject">拒绝</button>', page)
        self.assertNotIn('value="return"', page)
        self.assertNotIn("退回</button>", page)
        self.assertLess(page.index('value="approve"'), page.index('value="reject"'))
        self.assertIsNone(self.service.render_item("opaque-session", "0" * 16))
        self.assertIsNone(self.service.render_item("opaque-session", "not-an-identity"))

    def test_detail_page_shows_the_file_name_once_and_labels_the_raw_text(self):
        # The h1 already carries the name; the raw-text block used to repeat it.
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        for state in ("pending", "approve", "reject"):
            if state != "pending":
                self.decide(action=state)
                self.clock_value += 60
            for unlocked in (False, True):
                with self.subTest(state=state, unlocked=unlocked):
                    page = self.service.render_item("opaque-session", item.identity, unlocked=unlocked).decode("utf-8")
                    self.assertEqual(page.count("pending.md</h1>"), 1)
                    self.assertNotIn("<h2>pending.md</h2>", page)
                    self.assertNotIn("<article><h2>pending.md", page)
                    if state == "pending" or unlocked:
                        self.assertIn("<article><h2>原文</h2><pre>", page)
                    self.assertIn("← 返回列表</a>", page)
                    self.assertNotIn("返回待办列表", page)

    def test_detail_of_a_decided_item_is_locked_by_default(self):
        self.decide(action="reject")
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        page = self.service.render_item("opaque-session", item.identity).decode("utf-8")
        self.assertIn("已拒绝", page)
        self.assertNotIn('name="form_nonce"', page)
        self.assertIn(f'href="/item/{item.identity}?edit=1"', page)
        self.assertIn("已有处理决定", page)

    def test_detail_of_a_decided_item_unlocks_for_redecision_when_requested(self):
        self.decide(action="reject")
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        page = self.service.render_item("opaque-session", item.identity, unlocked=True).decode("utf-8")
        self.assertIn("已拒绝", page)
        self.assertIn('name="form_nonce"', page)
        self.assertIn("提交将新增一条决定", page)

    def test_detail_of_a_pending_item_is_never_locked(self):
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        page = self.service.render_item("opaque-session", item.identity).decode("utf-8")
        self.assertIn('name="form_nonce"', page)
        self.assertNotIn("已有处理决定", page)

    def test_detail_of_a_decided_item_flags_the_stale_frontmatter_status(self):
        # The rough file's own `status:` line is written once at ingest time and
        # never updated; the true status is the "状态：" line derived from the
        # decision queue above it. Without a note, a reviewer skimming the raw
        # preview could mistake the frozen "pending_review" for the real state.
        self.decide(action="reject")
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        page = self.service.render_item("opaque-session", item.identity, unlocked=True).decode("utf-8")
        self.assertIn("status: pending_review", page)
        self.assertIn("不随审核结果更新", page)

    def test_detail_of_a_pending_item_has_no_stale_status_notice(self):
        item = next(item for item in self.service.list_items() if item.path == "ingestion/rough/pending.md")
        page = self.service.render_item("opaque-session", item.identity).decode("utf-8")
        self.assertNotIn("不随审核结果更新", page)

    def test_detail_wiki_path_is_a_searchable_combobox_not_free_text(self):
        # Native <datalist> suggestion width/style isn't controllable via
        # CSS, so it renders inconsistently (narrower than the input) across
        # browsers; a JS-driven dropdown sized off the same wrapper matches
        # the input's width reliably like every other dropdown on the site.
        wiki_folder = self.root / "repo" / "wiki" / "01_Test"
        wiki_folder.mkdir(parents=True)
        (wiki_folder / "01-0001.md").write_text("x", encoding="utf-8")
        item = self.service.list_items()[0]
        page = self.service.render_item("opaque-session", item.identity).decode("utf-8")
        self.assertNotIn("<datalist", page)
        self.assertIn('<div class="combo">', page)
        self.assertIn('class="wiki-path-input"', page)
        self.assertIn('<div class="combo-list" role="listbox"></div>', page)
        self.assertIn('data-options="[[&quot;01_Test&quot;, &quot;wiki/01_Test/01-0002.md&quot;]]"', page)

    def test_detail_prefills_wiki_path_and_candidate_draft(self):
        path = "ingestion/rough/verify-0102-0001.md"
        (self.root / "repo/ingestion/rough/verify-0102-0001.md").write_text(VERIFY_ROUGH, encoding="utf-8")
        item = next(entry for entry in self.service.list_items() if entry.path == path)
        page = self.service.render_item("opaque-session", item.identity).decode("utf-8")
        self.assertIn('name="wiki_path" class="wiki-path-input" autocomplete="off" value="wiki/01_注册申报/0102_注册分类/0102-0001.md"', page)
        self.assertIn("no: 1", page)
        self.assertIn("date: 2017-10-10", page)
        self.assertIn("question:", page)
        self.assertIn("tag_pages:", page)
        self.assertIn("01_注册申报/0102_注册分类", page)
        self.assertIn("解答正文：含管道符 | 与换行", page)

    def test_candidate_draft_rebuilds_frontmatter_and_body(self):
        draft = candidate_draft(VERIFY_ROUGH, "wiki/01_注册申报/0102_注册分类/0102-0001.md")
        self.assertTrue(draft.startswith("---\nno: 1\n"))
        self.assertIn('date: 2017-10-10', draft)
        self.assertIn('source: "[[CDE_共性问题-常见一般性技术问题]]"', draft)
        self.assertIn('  - "[[01_注册申报]]"', draft)
        self.assertIn('  - "01_注册申报/0102_注册分类"', draft)
        self.assertIn("解答正文：含管道符 | 与换行\n第二段", draft)
        self.assertNotIn("<br>", draft)

    def test_default_wiki_path_rejects_absent_or_unsafe_targets(self):
        self.assertEqual(default_wiki_path('"[[wiki/01_Test/01-0001]]"'), "wiki/01_Test/01-0001.md")
        self.assertEqual(default_wiki_path(""), "")
        self.assertEqual(default_wiki_path('"[[ingestion/rough/x]]"'), "")
        self.assertEqual(default_wiki_path('"[[wiki/../../etc/passwd]]"'), "")

    def test_wiki_folder_candidates_suggests_next_number_per_folder(self):
        from web.review import wiki_folder_candidates
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "wiki" / "05_制剂药学研究" / "0517_其他"
            folder.mkdir(parents=True)
            (folder / "0517-0001.md").write_text("x", encoding="utf-8")
            (folder / "0517-0006.md").write_text("x", encoding="utf-8")
            (folder / "0517_其他.md").write_text("x", encoding="utf-8")  # folder overview note, no dash-number suffix to bump
            flat = root / "wiki" / "06_关联审评"
            flat.mkdir(parents=True)
            (flat / "06-0001.md").write_text("x", encoding="utf-8")
            candidates = wiki_folder_candidates(root)
            self.assertIn(("05_制剂药学研究/0517_其他", "wiki/05_制剂药学研究/0517_其他/0517-0007.md"), candidates)
            self.assertIn(("06_关联审评", "wiki/06_关联审评/06-0002.md"), candidates)

    def test_detail_shows_source_urls_for_review(self):
        root = self.root / "repo"
        (root / "source/CDE").mkdir(parents=True, exist_ok=True)
        (root / "source/CDE/CDE_共性问题-常见一般性技术问题.md").write_text(
            '---\nentity: 国家药品监督管理局药品审评中心\nentity_alias: CDE\n'
            'url: https://www.cde.org.cn/main/xxgk/listpage/07edef25f1e7354bfd8490baa0ce056b\n'
            'path: 信息公开 > 共性问题 > 常见一般性技术问题\nlast_updated: 2026-09-14\n---\n\n正文\n',
            encoding="utf-8",
        )
        path = "ingestion/rough/cde-item.md"
        (root / path).write_text(
            ROUGH.replace('"[[source/example]]"', '"[[CDE_共性问题-常见一般性技术问题]]"'),
            encoding="utf-8",
        )
        item = next(entry for entry in self.service.list_items() if entry.path == path)
        page = self.service.render_item("opaque-session", item.identity).decode("utf-8")
        self.assertIn("来源网址", page)
        self.assertIn('href="https://www.cde.org.cn/main/xxgk/listpage/07edef25f1e7354bfd8490baa0ce056b"', page)
        self.assertIn("国家药品监督管理局药品审评中心", page)

    def test_source_urls_follow_direct_wikilinks_and_inline_links(self):
        content = (
            '---\nsource: "[[source/国家药监局/2017-10-10_解读]]"\n---\n\n'
            "正文见 [解读](https://www.nmpa.gov.cn/directory/web/nmpa/xxgk/zhcjd/20171010214301421.html)\n"
        )
        links = source_urls(content, self.root / "repo")
        self.assertEqual(links, [{"label": "source/国家药监局/2017-10-10_解读", "url": "https://www.nmpa.gov.cn/directory/web/nmpa/xxgk/zhcjd/20171010214301421.html"}])

    def test_source_urls_ignore_non_http_values_and_unknown_sources(self):
        content = '---\nsource: "[[missing-note]]"\nsource_url: ftp://example.test/x\n---\n\n无链接\n'
        self.assertEqual(source_urls(content, self.root / "repo"), [])

    def test_failed_submission_does_not_write_a_reviewer_label(self):
        with self.assertRaises(ReviewError):
            self.decide(action="approve", rough_sha256="sha256:" + "0" * 64)
        self.assertEqual(self.labels.load(), {})


if __name__ == "__main__":
    unittest.main()
