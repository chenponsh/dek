import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from ingestion.automation import cli, sources
from ingestion.automation.core import Row, SafetyStop, normalize

FIXTURES = Path(__file__).with_name("fixtures")
LIST_URL = "https://da.jiangsu.gov.cn/col/col91813/index.html"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class JiangsuParsingTests(unittest.TestCase):
    def test_list_is_deduplicated_by_url_and_keeps_page_order(self):
        items = sources.parse_jiangsu_list(fixture("jiangsu_list_91813.html"), LIST_URL)
        self.assertEqual(len(items), 45)
        self.assertEqual(len({item.url for item in items}), 45)
        self.assertTrue(all(item.url.startswith("https://da.jiangsu.gov.cn/art/") for item in items))
        self.assertEqual(max(item.date for item in items), "2026-07-19")
        self.assertFalse(any(item.title.startswith("·") for item in items))

    def test_article_gives_title_date_and_paragraph_lines(self):
        title, answer, date = sources.parse_jiangsu_article(fixture("jiangsu_article.html"))
        self.assertEqual((title, date), ("沟通交流案例7-变更注册标准", "2025-08-12"))
        lines = answer.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("沟通交流事项："))
        self.assertTrue(lines[2].startswith("沟通交流结果："))
        self.assertNotIn("<", answer)

    def test_article_without_markers_is_refused(self):
        with self.assertRaises(SafetyStop):
            sources.parse_jiangsu_article("<html><body>加速乐</body></html>")

    def test_second_column_layout_is_read_too(self):
        items = sources.parse_jiangsu_list(fixture("jiangsu_list_91925.html"), "https://da.jiangsu.gov.cn/col/col91925/index.html")
        self.assertGreater(len(items), 10)
        self.assertIn("境外生产药品的前置服务流程是怎么样的？", [item.title for item in items])


class JiangsuFetchTests(unittest.TestCase):
    def pages(self, calls):
        listing = fixture("jiangsu_list_91813.html")
        article = fixture("jiangsu_article.html")

        def get(url):
            calls.append(url)
            return listing if url == LIST_URL else article
        return get

    def test_only_new_articles_after_since_are_opened(self):
        calls: list[str] = []
        items = sources.parse_jiangsu_list(fixture("jiangsu_list_91813.html"), LIST_URL)
        known = {(normalize(i.title), i.date) for i in items if i.date < "2026-05-01"}
        rows, meta = sources.fetch_jiangsu({"url": LIST_URL}, known, "2026-02-28", get=self.pages(calls))
        self.assertEqual(len(calls), 3)  # listing + the two items dated 2026-05-29 and 2026-07-19
        self.assertEqual(len(rows), 2)
        self.assertEqual(meta["remote_count"], 45)

    def test_known_items_are_not_fetched_again(self):
        calls: list[str] = []
        items = sources.parse_jiangsu_list(fixture("jiangsu_list_91813.html"), LIST_URL)
        known = {(normalize(i.title), i.date) for i in items}
        rows, _ = sources.fetch_jiangsu({"url": LIST_URL}, known, "2000-01-01", get=self.pages(calls))
        self.assertEqual(rows, [])
        self.assertEqual(calls, [LIST_URL])

    def test_empty_listing_is_a_failure_not_zero_news(self):
        with self.assertRaises(SafetyStop):
            sources.fetch_jiangsu({"url": LIST_URL}, set(), "2026-01-01", get=lambda url: "<html></html>")

    def test_unreadable_article_is_reported_and_skipped(self):
        listing = fixture("jiangsu_list_91813.html")
        rows, meta = sources.fetch_jiangsu(
            {"url": LIST_URL}, set(), "2026-06-01",
            get=lambda url: listing if url == LIST_URL else "<html>blocked</html>")
        self.assertEqual(rows, [])
        self.assertEqual(len(meta["skipped_items"]), 1)


class TableSourceStagingTests(unittest.TestCase):
    NOTE = (
        "---\nentity: x\nlast_updated: 2026-02-28\n---\n\n## 内容\n\n"
        "| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n| 旧问题 | 旧答案 | 2026-01-05 |\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "source").mkdir()
        (self.root / "ingestion" / "rough").mkdir(parents=True)
        (self.root / "source" / "a.md").write_text(self.NOTE, encoding="utf-8")
        self.enterContext(patch.object(cli, "ROOT", self.root))
        self.enterContext(patch.object(cli, "repo_fingerprint", return_value={}))
        self.now = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
        self.config = {"table_sources": [{"path": "source/a.md", "fetcher": "fake", "auto_classified": True, "auto_ingest": True}]}

    def run_stage(self, fetch):
        self.enterContext(patch.dict(cli.FETCHERS, {"fake": fetch}))
        result = cli.base_report(self.now, "dry-run")
        writes: dict = {}
        cli.stage_table_sources(self.config, result, writes, self.now)
        return result, writes

    def test_new_rows_become_note_rows_and_one_draft_each(self):
        result, writes = self.run_stage(lambda s, k, since: ([Row("新问题一", "答一", "2026-05-29"), Row("新问题二", "答二", "2026-07-19")], {}))
        self.assertEqual(result["report"]["source/a.md"]["status"], "updated_with_new")
        self.assertEqual(len(result["rough_created"]), 2)
        self.assertEqual(len(writes), 3)
        note = writes[self.root / "source" / "a.md"]
        self.assertIn("| 新问题二 | 答二 | 2026-07-19 |", note)
        self.assertIn("last_updated: 2026-09-20", note)
        self.assertFalse(result["blocking"])

    def test_fetch_failure_is_reported_without_blocking_or_writing(self):
        def broken(source, known, since):
            raise SafetyStop("HTTP 403")
        result, writes = self.run_stage(broken)
        self.assertEqual(result["report"]["source/a.md"]["status"], "failed")
        self.assertFalse(result["blocking"])
        self.assertEqual(writes, {})

    def test_remote_revision_is_reported_not_written_and_does_not_block(self):
        result, writes = self.run_stage(lambda s, k, since: ([Row("旧问题", "改过的答案", "2026-01-05")], {}))
        self.assertEqual(result["report"]["source/a.md"]["status"], "failed")
        self.assertFalse(result["blocking"])
        self.assertEqual(writes, {})

    def test_implausible_number_of_new_rows_is_refused(self):
        rows = [Row(f"问题{n}", "答", "2026-06-01") for n in range(cli.MAX_NEW_PER_SOURCE + 1)]
        result, writes = self.run_stage(lambda s, k, since: (rows, {}))
        self.assertEqual(result["report"]["source/a.md"]["status"], "failed")
        self.assertEqual(writes, {})

    def test_table_sources_are_on_the_automatic_write_allowlist(self):
        config = {"cde": {"sources": []}, **self.config}
        self.assertEqual(cli.automatic_write_allowlist(config), {"source/a.md"})


if __name__ == "__main__":
    unittest.main()
