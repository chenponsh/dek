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
from ingestion.automation.core import Row, SafetyStop, insert_articles, normalize, note_urls, parse_table

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


class ArticleSourceTests(unittest.TestCase):
    URL = "https://mpa.shaanxi.gov.cn/ztzl/ypzcgl/ypsshbggl/"

    def test_li_listing_gives_url_title_date(self):
        items = sources.parse_li_list(fixture("shaanxi_list.html"), self.URL)
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0].title, "药品上市后变更管理常见问题简答（五）")
        self.assertEqual(items[0].date, "2025-10-30")
        self.assertTrue(items[0].url.startswith("https://mpa.shaanxi.gov.cn/ztzl/ypzcgl/ypsshbggl/202510/"))

    def test_hainan_listing_reads_the_em_date(self):
        items = sources.parse_li_list(fixture("hainan_list.html"), "https://amr.hainan.gov.cn/himpa/HICDME/fwsx/")
        self.assertGreater(len(items), 10)
        self.assertTrue(all(len(item.date) == 10 for item in items))

    def test_body_block_is_found_with_nested_divs(self):
        body = sources.extract_block(fixture("shaanxi_article.html"), sources.BODY_SELECTORS[1])
        self.assertIn("再注册", body)
        text = sources.html_to_text(body)
        self.assertNotIn("<", text)

    def test_qa_split_removes_numbering_and_answer_prefix(self):
        text = "问题1：变更能否同时申报？\n答：不能。\n应另行申报。\n问题2：需要备案吗？\n答：需要。"
        self.assertEqual(sources.split_qa(text), [("变更能否同时申报？", "不能。\n应另行申报。"), ("需要备案吗？", "需要。")])

    def test_text_without_qa_pattern_is_not_split(self):
        self.assertEqual(sources.split_qa("这是一篇通知。\n请遵照执行。"), [])

    def test_topic_filter(self):
        self.assertFalse(sources.on_topic("普通化妆品备案常见问题解答", "药品"))
        self.assertFalse(sources.on_topic("药问药答——中药申报资料", "药品"))
        self.assertTrue(sources.on_topic("药问药答|药品上市后备案类变更篇（七）——化学药品申报资料", ""))
        self.assertFalse(sources.on_topic("云课堂开课啦", "第二类创新医疗器械审批程序"))

    def fetch(self, known, since):
        listing, article = fixture("shaanxi_list.html"), fixture("shaanxi_article.html")
        calls = []

        def get(url):
            calls.append(url)
            return listing if url == self.URL else article
        rows, meta = sources.fetch_article_source({"url": self.URL}, known, since, get=get)
        return rows, meta, calls

    def test_known_articles_are_not_opened(self):
        items = sources.parse_li_list(fixture("shaanxi_list.html"), self.URL)
        rows, _, calls = self.fetch({item.url for item in items}, "2000-01-01")
        self.assertEqual((rows, calls), ([], [self.URL]))

    def test_new_article_gives_article_rows_with_title_and_url(self):
        items = sources.parse_li_list(fixture("shaanxi_list.html"), self.URL)
        rows, _, calls = self.fetch({item.url for item in items[1:]}, "2000-01-01")
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows[0].article_url, items[0].url)
        self.assertEqual(rows[0].article_title, items[0].title)
        self.assertEqual(rows[0].date, "2025-10-30")

    def test_insert_articles_puts_newest_section_first_and_records_the_link(self):
        note = "---\nlast_updated: 2025-01-01\n---\n\n## 整理规则\n\n规则\n\n## 内容\n\n### [旧文章](https://x/old.html)（2024-01-01）\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n| 旧问 | 旧答 | 2024-01-01 |\n"
        rows = [sources.ArticleRow("新问", "新答|带竖线", "2025-05-05", "新文章", "https://x/new.html")]
        result = insert_articles(note, rows)
        self.assertLess(result.index("新文章"), result.index("旧文章"))
        self.assertEqual(note_urls(result), {"https://x/new.html", "https://x/old.html"})
        self.assertIn(("新问", "2025-05-05"), {row.key for row in parse_table(result)})
        self.assertIn(r"新答\|带竖线", result)


class BeijingTests(unittest.TestCase):
    def test_list_text_is_parsed(self):
        items, pages = sources.parse_beijing_list(fixture("beijing_list.txt"))
        self.assertEqual(len(items), 20)
        self.assertGreater(pages, 1)
        self.assertRegex(items[0].url, r"^AH\d+")
        self.assertRegex(items[0].date, r"^\d{4}-\d{2}-\d{2}$")

    def test_detail_gives_letter_and_reply_without_signature(self):
        question, reply = sources.parse_beijing_detail(fixture("beijing_detail.html"))
        self.assertTrue(question.startswith("具有符合YY9706.1-2021"))
        self.assertTrue(reply.startswith("网民您好"))
        self.assertFalse(reply.endswith("北京市药品监督管理局"))

    def test_empty_endpoint_is_a_failure(self):
        with self.assertRaises(SafetyStop):
            sources.fetch_beijing({"url": "https://yjj.beijing.gov.cn/a/b.html"}, set(), "2026-01-01", get=lambda url: "{page: {}}")

    def test_only_drug_letters_with_a_real_reply_are_kept(self):
        def letter(question, answer):
            return (f'<div class="sino-text-format">{question}</div><div class="sino-text-format">{answer}</div>')
        details = {
            "AH1": letter("药品说明书变更需要备案吗", "网民您好！需要向北京市局备案，并提交相关资料。" * 12),
            "AH2": letter("医疗器械说明书变更需要备案吗", "网民您好！" * 30),
            "AH3": letter("药品零售企业经营范围问题", "网民您好！" * 30),
            "AH4": letter("药品说明书变更需要备案吗", "网民您好！我局工作人员已电话回复咨询人。"),
        }
        listing = "{page: {pageNo:'1', totalCount:'4', totalPages:'1', pageSize:'20'}, result: [" + ",".join(
            f"{{originalId:'{key}', letterTitle:'咨询{key}', finishDateReal:'2026-06-0{n}'}}" for n, key in enumerate(details, 1)) + "]}"

        def get(url):
            if "letterList" in url:
                return listing
            return details[url.rsplit("=", 1)[1]]
        rows, meta = sources.fetch_beijing({"url": "https://yjj.beijing.gov.cn/a/b.html"}, set(), "2026-01-01", get=get)
        self.assertEqual([row.question for row in rows], ["药品说明书变更需要备案吗"])
        self.assertEqual(meta["filtered_count"], 3)


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

    def test_article_layout_note_gets_a_section_and_one_draft_per_question(self):
        note = "---\nlast_updated: 2026-02-28\n---\n\n## 内容\n\n### [旧](https://x/old.html)（2026-01-01）\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n| 旧问 | 旧答 | 2026-01-01 |\n"
        (self.root / "source" / "a.md").write_text(note, encoding="utf-8")
        self.config["table_sources"][0]["layout"] = "articles"
        seen = {}

        def fetch(source, known, since):
            seen["known"] = known
            return [sources.ArticleRow("问一", "答一", "2026-06-01", "新文章", "https://x/new.html"),
                    sources.ArticleRow("问二", "答二", "2026-06-01", "新文章", "https://x/new.html")], {}
        result, writes = self.run_stage(fetch)
        self.assertIn("https://x/old.html", seen["known"])
        self.assertEqual(len(result["rough_created"]), 2)
        text = writes[self.root / "source" / "a.md"]
        self.assertEqual(text.count("### [新文章](https://x/new.html)（2026-06-01）"), 1)
        self.assertLess(text.index("新文章"), text.index("### [旧]"))

    def test_table_sources_are_on_the_automatic_write_allowlist(self):
        config = {"cde": {"sources": []}, **self.config}
        self.assertEqual(cli.automatic_write_allowlist(config), {"source/a.md"})


if __name__ == "__main__":
    unittest.main()
