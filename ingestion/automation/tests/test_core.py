import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ingestion.automation import cli, core
from ingestion.automation.core import Row, SafetyStop, atomic_write_batch, compare_rows, ingestion_lock, insert_rows, last_updated, markdown_cell, parse_table, replace_last_updated, write_json
from ingestion.automation.fetchers import (
    CDEBrowserUnavailable, CPCArticle, _safe_public_url,
    assert_cpc_baseline_workspace_safe, fetch_cpc_content_hash,
    split_cpc_local_excerpt, validate_cpc_local_excerpt,
)


NOTE = """---
last_updated: 2026-01-01
---

## 内容

| 问题 | 解答 | 发布日期 |
| --- | --- | --- |
| 已有问题 | 已有解答 | 2026-01-01 |
"""


class CoreTests(unittest.TestCase):
    def scheduled_repo(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        remote = base / "remote.git"
        root = base / "repo"
        subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
        subprocess.run(["git", "init", "-q", root], check=True)
        subprocess.run(["git", "-C", root, "branch", "-M", "main"], check=True)
        subprocess.run(["git", "-C", root, "config", "user.email", "test@example.test"], check=True)
        subprocess.run(["git", "-C", root, "config", "user.name", "Test"], check=True)
        (root / ".gitignore").write_text("_/\n", encoding="utf-8")
        source = root / "source" / "CDE" / "auto.md"
        source.parent.mkdir(parents=True)
        source.write_text(NOTE, encoding="utf-8")
        (root / "ingestion" / "rough").mkdir(parents=True)
        (root / "ingestion" / "logs").mkdir(parents=True)
        (root / "ingestion" / "rough" / ".gitkeep").write_text("", encoding="utf-8")
        (root / "ingestion" / "logs" / ".gitkeep").write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", root, "add", "."], check=True)
        subprocess.run(["git", "-C", root, "commit", "-qm", "initial"], check=True)
        subprocess.run(["git", "-C", root, "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(["git", "-C", root, "push", "-qu", "origin", "main"], check=True)
        return root, remote, source

    def scheduled_plan(self, root, source, *, writes=True):
        rough = root / "ingestion" / "rough" / "rough.md"
        planned = [str(source.relative_to(root)), str(rough.relative_to(root))] if writes else []
        report = {
            "blocking": False, "alerts": [], "planned_writes": planned,
            "auto_write_paths": planned,
            "report": {str(source.relative_to(root)): {"status": "updated_with_new"}} if writes else {},
            "rough_created": planned[1:],
        }
        contents = {source: NOTE + "new\n", rough: "rough\n"} if writes else {}
        config = {"cde": {"sources": [{
            "path": str(source.relative_to(root)), "auto_classified": True, "auto_ingest": True,
        }]}}
        return config, report, contents

    def cpc_hash(self, content, *, nested=True, attachments=None, external_url=None):
        article = CPCArticle("id", "药品标准 123", "2026-01-02", "article.md")
        detail = {"newsContent": content, "annexFileList": attachments or []}
        if external_url is not None:
            detail["toLink"] = external_url
        payload = {"result": {"news": detail}} if nested else {"result": detail}
        with patch("ingestion.automation.fetchers.get_json", return_value=payload):
            return fetch_cpc_content_hash("https://example.test/{news_id}", article)

    def test_cpc_hash_reads_nested_news_content(self):
        self.assertRegex(self.cpc_hash("<p>正文内容</p>"), r"^sha256:[0-9a-f]{64}$")

    def test_cpc_hash_supports_legacy_result_content(self):
        self.assertEqual(self.cpc_hash("<p>正文内容</p>"), self.cpc_hash("正文内容", nested=False))

    def test_cpc_hash_nested_news_has_priority_over_legacy_fields(self):
        article = CPCArticle("id", "标题", "2026-01-02", "article.md")
        payload = {"result": {"newsContent": "旧正文", "news": {"newsContent": "嵌套正文"}}}
        with patch("ingestion.automation.fetchers.get_json", return_value=payload):
            actual = fetch_cpc_content_hash("https://example.test/{news_id}", article)
        with patch("ingestion.automation.fetchers.get_json", return_value={"result": {"newsContent": "嵌套正文"}}):
            expected = fetch_cpc_content_hash("https://example.test/{news_id}", article)
        self.assertEqual(actual, expected)

    def test_cpc_hash_rejects_missing_body(self):
        with self.assertRaisesRegex(SafetyStop, "no non-empty normalized body"):
            self.cpc_hash("<div> \u200b </div>")

    def test_cpc_hash_is_repeatable_and_ignores_formatting(self):
        first = self.cpc_hash("<p>药品标准&nbsp;123</p><br>2026-01-02")
        second = self.cpc_hash("  药品标准 123\n2026-01-02  ")
        self.assertEqual(first, second)
        self.assertEqual(first, self.cpc_hash("<p>药品标准&nbsp;123</p><br>2026-01-02"))

    def test_cpc_hash_changes_for_substantive_values(self):
        original = self.cpc_hash("药品阿司匹林 标准编号 123 日期 2026-01-02")
        for changed in (
            "药品阿司匹林 标准编号 124 日期 2026-01-02",
            "药品阿司匹林 标准编号 123 日期 2026-01-03",
            "药品布洛芬 标准编号 123 日期 2026-01-02",
            "药品阿司匹林 标准编号 123 日期 2026-01-02 修订",
        ):
            self.assertNotEqual(original, self.cpc_hash(changed))

    def test_markdown_url_numbers_do_not_affect_body_validation(self):
        payload = {"result": {"news": {"newsContent": "正文 2020年", "annexFileList": [{"id": "stable", "name": "勘误表.docx"}]}}}
        local = "正文 2020年<br>附件：<br>- [勘误表.docx](https://example.test/123?token=456)"
        validate_cpc_local_excerpt(local, payload, "article.md")
        body, names = split_cpc_local_excerpt(local)
        self.assertEqual(body, "正文 2020年")
        self.assertEqual(names, ["勘误表.docx"])

    def test_matching_attachment_name_passes(self):
        payload = {"result": {"news": {"newsContent": "正文", "annexFileList": [{"id": "stable", "name": "附件.pdf"}]}}}
        validate_cpc_local_excerpt("正文\n附件：[附件.pdf](https://host/path?token=x)", payload, "article.md")

    def test_attachment_name_change_is_blocking(self):
        payload = {"result": {"news": {"newsContent": "正文", "annexFileList": [{"id": "stable", "name": "修订附件.pdf"}]}}}
        with self.assertRaisesRegex(SafetyStop, "attachment names differ"):
            validate_cpc_local_excerpt("正文\n附件：[原附件.pdf](https://host/path)", payload, "article.md")
        self.assertNotEqual(
            self.cpc_hash("正文", attachments=[{"id": "stable", "name": "原附件.pdf"}]),
            self.cpc_hash("正文", attachments=[{"id": "stable", "name": "修订附件.pdf"}]),
        )

    def test_stable_attachment_id_changes_hash(self):
        self.assertNotEqual(
            self.cpc_hash("正文", attachments=[{"id": "stable-a", "name": "附件.pdf"}]),
            self.cpc_hash("正文", attachments=[{"id": "stable-b", "name": "附件.pdf"}]),
        )

    def test_url_query_or_signature_does_not_change_hash(self):
        first = self.cpc_hash("正文", external_url="https://one.test/path?token=one", attachments=[{"id": "stable", "name": "附件.pdf", "url": "https://one.test/a?sign=one"}])
        second = self.cpc_hash("正文", external_url="https://two.test/other?token=two", attachments=[{"id": "stable", "name": "附件.pdf", "url": "https://two.test/b?sign=two"}])
        self.assertEqual(first, second)

    def test_2020_correction_notice_is_not_a_numeric_false_positive(self):
        payload = {"result": {"news": {"newsContent": "通知正文 2020年9月27日", "annexFileList": [{"id": "FFE10B5B6C2B0E87E0539701A8C01EEE", "name": "2020年版《中国药典》勘误/修订表.docx"}]}}}
        local = "通知正文 2020年9月27日<br>附件：<br>- [2020年版《中国药典》勘误/修订表.docx](https://www.chp.org.cn/download/123?token=456)"
        validate_cpc_local_excerpt(local, payload, "2020-09-30.md")

    def test_attachment_embedded_text_is_not_parsed_as_attachment_names(self):
        local = (
            "通知正文<br>附件：<br>"
            "- [勘误表.PDF](https://host/download/123?token=temporary)<br>"
            "附件《勘误表.PDF》文本：<br>-2-<br>序号 页码 标准名称<br>1 91 艾叶"
        )
        body, names = split_cpc_local_excerpt(local)
        self.assertEqual(body, "通知正文")
        self.assertEqual(names, ["勘误表.pdf"])
        self.assertNotIn("-2-", names)
        self.assertNotIn("序号 页码 标准名称", names)

    def test_attachment_multiset_detects_missing_added_and_duplicate(self):
        payload = {"result": {"news": {"newsContent": "正文", "annexFileList": [
            {"id": "stable-a", "name": "附件A.pdf"},
            {"id": "stable-b", "name": "附件B.pdf"},
        ]}}}
        validate_cpc_local_excerpt(
            "正文<br>附件：<br>- [附件B.PDF](https://host/b)<br>- [附件A.pdf](https://host/a)",
            payload, "article.md",
        )
        for local in (
            "正文<br>附件：<br>- [附件A.pdf](https://host/a)",
            "正文<br>附件：<br>- [附件A.pdf](https://host/a)<br>- [附件B.pdf](https://host/b)<br>- [附件C.pdf](https://host/c)",
            "正文<br>附件：<br>- [附件A.pdf](https://host/a)<br>- [附件A.pdf](https://host/a2)",
        ):
            with self.assertRaisesRegex(SafetyStop, "attachment names differ"):
                validate_cpc_local_excerpt(local, payload, "article.md")

    def test_2021_correction_notice_embedded_pdf_text_is_not_an_attachment(self):
        payload = {"result": {"news": {"newsContent": "通知正文 2021年7月8日", "annexFileList": [{
            "id": "FFE10B5B6DDF0E87E0539701A8C01EEE",
            "name": "2020年版《中国药典》勘误表.pdf",
        }]}}}
        local = (
            "通知正文 2021年7月8日<br>附件：<br>"
            "- [2020年版《中国药典》勘误表.PDF](https://host/download?id=ignored&token=ignored)<br>"
            "附件《2020年版《中国药典》勘误表.pdf》文本：<br>-2-<br>1 一部91页 艾叶"
        )
        validate_cpc_local_excerpt(local, payload, "2021-07-09.md")

    def test_cpc_hash_change_blocks_inspection(self):
        report = self.inspect_cpc(remote_hash="sha256:" + "b" * 64)
        self.assertTrue(report["blocking"])
        self.assertEqual(report["report"]["cpc.md"]["status"], "failed")

    def test_cpc_baseline_workspace_whitelist_rejects_other_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "deploy").mkdir(); (root / "deploy" / "unit.service").write_text("x")
            (root / "requirements-ingestion.txt").write_text("x")
            assert_cpc_baseline_workspace_safe(root)
            (root / "unexpected.txt").write_text("x")
            with self.assertRaisesRegex(SafetyStop, "unexpected baseline workspace"):
                assert_cpc_baseline_workspace_safe(root)

    def inspect_cpc(self, article_exists=True, remote_hash=None, hash_error=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        note = "---\nlast_updated: 2025-01-01\n---\n\n## 内容\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n"
        (root / "shanghai.md").write_text(note, encoding="utf-8")
        (root / "included").mkdir(); (root / "excluded").mkdir()
        article = CPCArticle("id", "标题", "2026-01-01", "2026-01-01_标题.md")
        baseline = "sha256:" + "a" * 64
        if article_exists:
            (root / "included" / article.filename).write_text(f'---\nsource_content_hash: "{baseline}"\n---\n', encoding="utf-8")
        config = {"no_fetch_rule": [], "known_unautomated": [], "shanghai": {"url": "x", "path": "shanghai.md"}, "cpc": {"list_url": "x", "detail_url": "x/{news_id}", "path": "cpc.md", "included_dir": "included", "excluded_dir": "excluded"}, "cde": {"url": "x", "sources": []}}
        detail_effect = SafetyStop("unavailable") if hash_error else None
        with patch.object(cli, "ROOT", root), patch.object(cli, "repo_fingerprint", return_value="x"), patch.object(cli, "fetch_shanghai", return_value=([], {"remote_count": 0})), patch.object(cli, "fetch_cpc", return_value=([article], {"remote_count": 1})), patch.object(cli, "fetch_cpc_content_hash", return_value=remote_hash or baseline, side_effect=detail_effect), patch.object(cli, "fetch_cde", return_value=({}, {})):
            return cli.inspect(config, datetime.now())[0]

    def test_parse_and_add(self):
        local = parse_table(NOTE)
        new = Row("新问题", "第一行\n第二行 | x", "2026-02-03")
        additions, revisions = compare_rows(local, [local[0], new])
        self.assertEqual(additions, [new])
        self.assertEqual(revisions, [])
        rendered = insert_rows(NOTE, additions)
        self.assertIn("第一行<br>第二行 \\| x", rendered)

    def test_revision_is_reported(self):
        additions, revisions = compare_rows(parse_table(NOTE), [Row("已有问题", "修订解答", "2026-01-01")])
        self.assertFalse(additions)
        self.assertEqual(revisions[0]["question"], "已有问题")

    def test_last_updated_preserves_frontmatter_gap(self):
        self.assertEqual(last_updated(NOTE), "2026-01-01")
        changed = replace_last_updated(NOTE, "2026-03-04")
        self.assertIn("last_updated: 2026-03-04", changed)
        self.assertIn("---\n\n## 内容", changed)

    def test_noncanonical_table_stops(self):
        with self.assertRaises(SafetyStop):
            insert_rows("## 内容\n", [Row("q", "a", "2026-01-01")])

    def test_diagnostic_url_removes_query_and_fragment(self):
        self.assertEqual(_safe_public_url("https://example.test/path?token=secret#x"), "https://example.test/path")

    def test_cde_unavailable_carries_only_explicit_diagnostics(self):
        diagnostics = {"navigation_statuses": [{"status": 202}], "myAjax_is_function": False}
        error = CDEBrowserUnavailable("blocked", diagnostics)
        self.assertEqual(error.diagnostics, diagnostics)

    def test_html_link_is_preserved(self):
        value = markdown_cell('请看<a href="https://example.test/a">原文</a>。')
        self.assertEqual(value, "请看[原文](https://example.test/a)。")

    def test_process_lock_rejects_concurrent_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ingestion_lock(root):
                with self.assertRaises(SafetyStop):
                    with ingestion_lock(root):
                        pass

    def test_atomic_batch_rolls_back_on_final_check_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            target = root / "source.md"
            target.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "source.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            with self.assertRaisesRegex(RuntimeError, "final failure"):
                with atomic_write_batch(root, {target: "after\n", root / "rough.md": "rough\n"}):
                    raise RuntimeError("final failure")
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
            self.assertFalse((root / "rough.md").exists())

    def test_cde_browser_unavailable_is_source_level_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = "---\nlast_updated: 2026-01-01\n---\n\n## 内容\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n"
            shanghai = root / "shanghai.md"; shanghai.write_text(note, encoding="utf-8")
            cpc = root / "cpc.md"; cpc.write_text(note, encoding="utf-8")
            (root / "included").mkdir(); (root / "excluded").mkdir()
            config = {"no_fetch_rule": [], "known_unautomated": [], "shanghai": {"url": "x", "path": "shanghai.md"}, "cpc": {"list_url": "x", "path": "cpc.md", "included_dir": "included", "excluded_dir": "excluded"}, "cde": {"url": "x", "sources": [{"type": 4, "path": "cde.md", "auto_classified": True}]}}
            error = CDEBrowserUnavailable("unavailable", {"myAjax_is_function": False})
            with patch.object(cli, "ROOT", root), patch.object(cli, "repo_fingerprint", return_value="x"), patch.object(cli, "fetch_shanghai", return_value=([], {"remote_count": 0})), patch.object(cli, "fetch_cpc", return_value=(set(), {"remote_count": 0})), patch.object(cli, "fetch_cde", side_effect=error):
                report, _ = cli.inspect(config, datetime.now())
            self.assertFalse(report["blocking"])
            self.assertEqual(report["report"]["cde.md"]["status"], "skipped_browser_unavailable")
            with patch.object(cli, "ROOT", root), patch.object(cli, "repo_fingerprint", return_value="x"), patch.object(cli, "fetch_shanghai", return_value=([], {"remote_count": 0})), patch.object(cli, "fetch_cpc", return_value=(set(), {"remote_count": 0})), patch.object(cli, "fetch_cde", side_effect=RuntimeError("schema failure")):
                failed_report, _ = cli.inspect(config, datetime.now())
            self.assertTrue(failed_report["blocking"])
            self.assertEqual(failed_report["report"]["cde.md"]["status"], "failed")

    def test_cde_unclassified_addition_blocks_scheduled_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = "---\nlast_updated: 2026-01-01\n---\n\n## 内容\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n"
            (root / "shanghai.md").write_text(note, encoding="utf-8")
            (root / "cde.md").write_text(note, encoding="utf-8")
            (root / "included").mkdir(); (root / "excluded").mkdir()
            config = {"no_fetch_rule": [], "known_unautomated": [], "shanghai": {"url": "x", "path": "shanghai.md"}, "cpc": {"list_url": "x", "path": "cpc.md", "included_dir": "included", "excluded_dir": "excluded"}, "cde": {"enabled": True, "url": "x", "sources": [{"type": 1, "path": "cde.md", "auto_classified": False, "auto_ingest": False}]}}
            remote = {1: ([Row("新问题", "新解答", "2026-02-01")], {"remote_count": 1})}
            with patch.object(cli, "ROOT", root), patch.object(cli, "repo_fingerprint", return_value="x"), patch.object(cli, "fetch_shanghai", return_value=([], {"remote_count": 0})), patch.object(cli, "fetch_cpc", return_value=([], {"remote_count": 0})), patch.object(cli, "fetch_cde", return_value=(remote, {})):
                report, writes = cli.inspect(config, datetime.now())
            self.assertTrue(report["blocking"])
            self.assertEqual(report["report"]["cde.md"]["status"], "failed")
            self.assertEqual(writes, {})

    def test_expired_approval_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "_" / "ingestion"; report_dir.mkdir(parents=True)
            report_file = report_dir / "dry-run.json"
            report_file.write_text(json.dumps({"mode": "dry-run", "blocking": False, "baseline": "x", "generated_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()}), encoding="utf-8")
            with patch.object(cli, "ROOT", root), patch.object(cli, "CONFIG_PATH", root / "config.json"), patch.object(cli, "APPROVAL_PATH", report_dir / "approval.json"), patch.object(cli, "repo_fingerprint", return_value="x"):
                with self.assertRaisesRegex(SafetyStop, "expired"):
                    cli.approve(report_file)

    def test_dry_run_report_is_created_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {
                "mode": "dry-run", "blocking": False, "alerts": [],
                "planned_writes": [], "report": {}, "rough_created": [],
            }
            with patch.object(cli, "ROOT", root), patch.object(cli, "assert_git_safe"), patch.object(cli, "load_config", return_value={}), patch.object(cli, "inspect", return_value=(report, {})):
                self.assertEqual(cli.execute(real=False), 0)
            report_file = next((root / "_" / "ingestion").glob("dry-run-*.json"))
            self.assertEqual(report_file.stat().st_mode & 0o777, 0o600)

    def test_approval_file_is_created_mode_0600_and_binds_report_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_dir = root / "_" / "ingestion"
            report_dir.mkdir(parents=True)
            report_file = report_dir / "dry-run.json"
            report = {
                "mode": "dry-run", "blocking": False, "baseline": "baseline",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "report": {}, "rough_created": [], "planned_writes": [],
            }
            write_json(report_file, report)
            raw = report_file.read_bytes()
            approval_file = report_dir / "approval.json"
            with patch.object(cli, "ROOT", root), patch.object(cli, "CONFIG_PATH", root / "config.json"), patch.object(cli, "APPROVAL_PATH", approval_file), patch.object(cli, "repo_fingerprint", return_value="baseline"), patch.object(cli, "assert_git_safe"), patch.object(cli, "workspace_snapshot", return_value="workspace"):
                self.assertEqual(cli.approve(report_file), 0)
            approval = json.loads(approval_file.read_text(encoding="utf-8"))
            self.assertEqual(approval_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(approval["report_sha256"], hashlib.sha256(raw).hexdigest())

    def test_secure_json_overwrite_remains_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text("old\n", encoding="utf-8")
            os.chmod(path, 0o644)
            write_json(path, {"value": "new"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "value": "new"\n}\n')

    def test_approval_scope_changes_with_planned_result(self):
        report = {"report": {"a": {"status": "checked_no_new"}}, "rough_created": [], "planned_writes": ["a"], "blocking": False}
        changed = {**report, "planned_writes": ["a", "b"]}
        self.assertNotEqual(cli.approval_scope_hash(report), cli.approval_scope_hash(changed))

    def test_rough_content_has_frontmatter_gap_and_source_link(self):
        content = cli.rough_content("source/CDE/example.md", [Row("问题", "解答", "2026-01-02")], "2026-01-03")
        self.assertIn('source: "[[source/CDE/example]]"', content)
        self.assertIn("---\n\n## 新增问答", content)
        self.assertIn("| 问题 | 解答 | 2026-01-02 |", content)

    def test_only_last_updated_change_is_no_change(self):
        report = self.inspect_cpc(remote_hash="sha256:" + "a" * 64)
        self.assertEqual(report["report"]["shanghai.md"]["status"], "no_change")
        self.assertEqual(report["report"]["cpc.md"]["status"], "no_change")
        self.assertEqual(report["planned_writes"], [])

    def test_no_change_run_does_not_write_commit_or_push(self):
        report = {"blocking": False, "alerts": [], "planned_writes": [], "report": {}, "rough_created": []}
        with patch.object(cli, "assert_git_safe"), patch.object(cli, "load_config", return_value={}), patch.object(cli, "inspect", return_value=(report, {})), patch.object(cli, "git") as git_mock:
            self.assertEqual(cli.execute(real=True), 0)
        git_mock.assert_not_called()

    def test_scheduled_low_risk_addition_commits_and_pushes_to_local_remote(self):
        root, remote, source = self.scheduled_repo()
        config, report, writes = self.scheduled_plan(root, source)
        old_head = core.git(root, "rev-parse", "HEAD")
        with patch.object(cli, "ROOT", root), patch.object(cli, "load_config", return_value=config), patch.object(cli, "inspect", return_value=(report, writes)):
            self.assertEqual(cli.execute_scheduled(), 0)
        self.assertNotEqual(core.git(root, "rev-parse", "HEAD"), old_head)
        self.assertEqual(core.git(root, "rev-parse", "HEAD"), core.git(root, "rev-parse", "origin/main"))
        self.assertEqual(core.git(root, "status", "--porcelain"), "")

    def test_scheduled_no_change_ignores_approval_and_does_not_commit(self):
        root, remote, source = self.scheduled_repo()
        config, report, writes = self.scheduled_plan(root, source, writes=False)
        approval = root / "_" / "ingestion" / "approval.json"
        approval.parent.mkdir(parents=True)
        approval.write_text("invalid", encoding="utf-8")
        head = core.git(root, "rev-parse", "HEAD")
        with patch.object(cli, "ROOT", root), patch.object(cli, "APPROVAL_PATH", approval), patch.object(cli, "load_config", return_value=config), patch.object(cli, "inspect", return_value=(report, writes)):
            self.assertEqual(cli.execute_scheduled(), 0)
        self.assertEqual(core.git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(approval.read_text(encoding="utf-8"), "invalid")

    def test_scheduled_rejects_non_allowlisted_path(self):
        root, remote, source = self.scheduled_repo()
        other = root / "wiki" / "unexpected.md"
        report = {"planned_writes": ["wiki/unexpected.md"], "auto_write_paths": ["wiki/unexpected.md"]}
        config = {"cde": {"sources": []}}
        with patch.object(cli, "ROOT", root):
            with self.assertRaisesRegex(SafetyStop, "non-allowlisted"):
                cli.validate_automatic_plan(config, report, {other: "x"})

    def test_scheduled_nonblocking_without_automatic_decision_is_rejected(self):
        root, remote, source = self.scheduled_repo()
        config, report, writes = self.scheduled_plan(root, source)
        report["auto_write_paths"] = []
        with patch.object(cli, "ROOT", root):
            with self.assertRaisesRegex(SafetyStop, "without an explicit automatic decision"):
                cli.validate_automatic_plan(config, report, writes)

    def test_scheduled_head_change_before_write_blocks(self):
        root, remote, source = self.scheduled_repo()
        config, report, writes = self.scheduled_plan(root, source)
        checks = [None, SafetyStop("HEAD changed before scheduled write")]
        with patch.object(cli, "ROOT", root), patch.object(cli, "load_config", return_value=config), patch.object(cli, "inspect", return_value=(report, writes)), patch.object(cli, "assert_git_safe", side_effect=checks):
            with self.assertRaisesRegex(SafetyStop, "HEAD changed"):
                cli.execute_scheduled()
        self.assertEqual(source.read_text(encoding="utf-8"), NOTE)

    def test_scheduled_git_add_and_commit_failures_restore_files(self):
        for failing_command in ("add", "commit"):
            with self.subTest(command=failing_command):
                root, remote, source = self.scheduled_repo()
                config, report, writes = self.scheduled_plan(root, source)
                original_git = cli.git
                def fail_selected(repo, *args):
                    if args and args[0] == failing_command:
                        raise SafetyStop(f"simulated {failing_command} failure")
                    return original_git(repo, *args)
                with patch.object(cli, "ROOT", root), patch.object(cli, "load_config", return_value=config), patch.object(cli, "inspect", return_value=(report, writes)), patch.object(cli, "git", side_effect=fail_selected):
                    with self.assertRaisesRegex(SafetyStop, f"simulated {failing_command}"):
                        cli.execute_scheduled()
                self.assertEqual(source.read_text(encoding="utf-8"), NOTE)
                self.assertEqual(core.git(root, "status", "--porcelain"), "")

    def test_scheduled_remote_change_before_commit_restores_files(self):
        root, remote, source = self.scheduled_repo()
        config, report, writes = self.scheduled_plan(root, source)
        original_git = cli.git
        def changed_remote(repo, *args):
            if args[:2] == ("rev-parse", "origin/main"):
                return "0" * 40
            return original_git(repo, *args)
        with patch.object(cli, "ROOT", root), patch.object(cli, "load_config", return_value=config), patch.object(cli, "inspect", return_value=(report, writes)), patch.object(cli, "git", side_effect=changed_remote):
            with self.assertRaisesRegex(SafetyStop, "origin/main changed before"):
                cli.execute_scheduled()
        self.assertEqual(source.read_text(encoding="utf-8"), NOTE)
        self.assertEqual(core.git(root, "status", "--porcelain"), "")

    def test_scheduled_push_failure_retains_commit_and_blocks_next_run(self):
        root, remote, source = self.scheduled_repo()
        config, report, writes = self.scheduled_plan(root, source)
        old_head = core.git(root, "rev-parse", "HEAD")
        original_git = cli.git
        def fail_push(repo, *args):
            if args and args[0] == "push":
                raise SafetyStop("simulated push failure")
            return original_git(repo, *args)
        with patch.object(cli, "ROOT", root), patch.object(cli, "load_config", return_value=config), patch.object(cli, "inspect", return_value=(report, writes)), patch.object(cli, "git", side_effect=fail_push):
            with self.assertRaisesRegex(SafetyStop, "simulated push failure"):
                cli.execute_scheduled()
        self.assertNotEqual(core.git(root, "rev-parse", "HEAD"), old_head)
        self.assertEqual(core.git(root, "rev-parse", "origin/main"), old_head)
        with self.assertRaisesRegex(SafetyStop, "main differs"):
            core.assert_git_safe(root)

    def test_cpc_new_article_is_blocking_candidate(self):
        report = self.inspect_cpc(article_exists=False)
        self.assertTrue(report["blocking"])
        self.assertEqual(report["report"]["cpc.md"]["status"], "candidate_new")

    def test_cpc_existing_content_unchanged(self):
        report = self.inspect_cpc(remote_hash="sha256:" + "a" * 64)
        self.assertFalse(report["blocking"])
        self.assertEqual(report["report"]["cpc.md"]["verified_existing_count"], 1)

    def test_cpc_revision_blocks_everything(self):
        report = self.inspect_cpc(remote_hash="sha256:" + "b" * 64)
        self.assertTrue(report["blocking"])
        self.assertEqual(report["report"]["cpc.md"]["status"], "failed")
        self.assertEqual(report["report"]["cpc.md"]["revision_count"], 1)

    def test_cpc_revision_check_unavailable_is_safe_skip(self):
        report = self.inspect_cpc(hash_error=True)
        self.assertFalse(report["blocking"])
        self.assertEqual(report["report"]["cpc.md"]["status"], "skipped_revision_check_unavailable")
        self.assertNotIn("cpc.md", report["planned_writes"])


if __name__ == "__main__":
    unittest.main()
