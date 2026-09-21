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


class CpcNewNotesTests(unittest.TestCase):
    CPC = {"detail_url": "https://x/detail?newsId={news_id}"}

    def test_attachment_only_article_lists_the_files(self):
        from ingestion.automation.fetchers import CPCArticle
        payload = {"result": {"news": {"newsContent": None, "toLink": None, "annexFileList": [{"id": "1", "name": "解读.pdf"}]}}}
        notes, meta = sources.fetch_cpc_notes(self.CPC, [CPCArticle("n1", "药典执行解读", "2026-06-01", "2026-06-01_药典执行解读.md")], lambda url: payload, fetch_bytes=lambda url: b"not a pdf")
        self.assertEqual(notes[0].body, "附件：\n- [解读.pdf](https://www.chp.org.cn/three/anon/user/download?id=1&token=)")
        self.assertEqual(meta["attachments_without_text"][0]["attachment"], "解读.pdf")
        self.assertEqual(notes[0].source_url, "https://www.chp.org.cn/#/newsDetail?id=n1")

    def test_external_link_without_body_gets_the_standard_placeholder(self):
        from ingestion.automation.fetchers import CPCArticle
        payload = {"result": {"news": {"newsContent": None, "toLink": "1", "toLinkIp": "https://nmpa.example/a.html"}}}
        notes, _ = sources.fetch_cpc_notes(self.CPC, [CPCArticle("n2", "公告", "2026-06-01", "f.md")], lambda url: payload)
        self.assertEqual(notes[0].body, sources.CPC_NO_BODY)
        self.assertEqual(notes[0].external_url, "https://nmpa.example/a.html")

    def test_training_notices_and_failed_details_are_reported_not_drafted(self):
        from ingestion.automation.fetchers import CPCArticle

        def broken(url):
            raise SafetyStop("HTTP 500")
        notes, meta = sources.fetch_cpc_notes(self.CPC, [
            CPCArticle("n3", "药典培训班通知", "2026-06-01", "a.md"),
            CPCArticle("n4", "药典解读", "2026-06-01", "b.md")], broken)
        self.assertEqual(notes, [])
        self.assertEqual(len(meta["filtered_out"]), 1)
        self.assertEqual(len(meta["skipped_items"]), 1)

    def test_note_text_has_frontmatter_and_one_row_table(self):
        note = sources.NewNote("f.md", "标题|含竖线", "2026-06-01", "https://u", "", "第一段\n第二段")
        text = sources.note_text(note, "CPC_专栏")
        self.assertTrue(text.startswith('---\nsource_note: "[[CPC_专栏]]"\n'))
        self.assertIn("| 标题\\|含竖线 | 第一段<br>第二段 | 2026-06-01 |", text)
        self.assertNotIn("external_url", text)


class AnhuiTests(unittest.TestCase):
    SOURCE = {"url": "https://mpa.ah.gov.cn/ztgz/yssypbgba/yp/index.html",
              "list_url": "https://mpa.ah.gov.cn/content/column/31415151?pageIndex={page}",
              "cpc_detail_url": "https://x/detail?newsId={news_id}"}

    def get(self, calls=None):
        listing, article = fixture("anhui_list.html"), fixture("anhui_article.html")

        def get(url):
            if calls is not None:
                calls.append(url)
            return listing if "pageIndex" in url else article
        return get

    def test_file_name_follows_the_library_rule(self):
        self.assertEqual(sources.safe_filename("2026-07-02", "a/b:c\n d"), "2026-07-02_a、b、c d.md")

    def test_new_qa_article_becomes_a_note_with_one_pair_per_question(self):
        notes, meta = sources.fetch_anhui_notes(self.SOURCE, set(), "2026-06-01", get=self.get(), fetch_json=lambda url: {})
        titles = [n.title for n in notes]
        self.assertIn("药品上市后变更备案共性问题解答（五）", titles)
        note = next(n for n in notes if n.title.endswith("（五）"))
        self.assertGreaterEqual(len(note.pairs), 2)
        self.assertEqual(note.date, "2026-07-02")

    def test_articles_about_traditional_medicine_or_vaccines_are_filtered_out(self):
        notes, meta = sources.fetch_anhui_notes(self.SOURCE, set(), "2000-01-01", get=self.get(), fetch_json=lambda url: {})
        self.assertFalse(any("配方颗粒" in n.title or "疫苗" in n.title for n in notes))
        self.assertTrue(any("配方颗粒" in item["title"] for item in meta["filtered_out"]))

    def test_known_files_and_old_items_are_not_opened(self):
        calls = []
        known = {sources.safe_filename("2026-07-02", "药品上市后变更备案共性问题解答（五）")}
        notes, _ = sources.fetch_anhui_notes(self.SOURCE, known, "2026-07-14", get=self.get(calls), fetch_json=lambda url: {})
        self.assertEqual(notes, [])
        self.assertEqual(len(calls), 1)  # only the list page

    def test_external_site_without_access_gets_a_placeholder_and_the_link(self):
        listing = ('<li class="odd"><a href="https://www.nmpa.gov.cn/x/1.html" title="药品上市后变更管理公告" class="left"><span>t</span></a>'
                   '<span class="right date">2026-08-01</span></li> pageCount:1,')
        notes, _ = sources.fetch_anhui_notes(self.SOURCE, set(), "2026-01-01", get=lambda url: listing, fetch_json=lambda url: {})
        self.assertEqual(notes[0].body, sources.AH_NO_BODY)
        self.assertEqual(notes[0].external_url, "https://www.nmpa.gov.cn/x/1.html")

    def test_empty_list_is_a_failure(self):
        with self.assertRaises(SafetyStop):
            sources.fetch_anhui_notes(self.SOURCE, set(), "2026-01-01", get=lambda url: "<html></html>", fetch_json=lambda url: {})


class FileSourceStagingTests(unittest.TestCase):
    def test_qa_note_gives_one_draft_per_question_and_failures_do_not_raise(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source" / "x" / "dir").mkdir(parents=True)
            (root / "source" / "x" / "dir_排除").mkdir()
            (root / "ingestion" / "rough").mkdir(parents=True)
            (root / "source" / "x" / "n.md").write_text("---\nlast_updated: 2026-01-01\n---\n\n## 内容\n", encoding="utf-8")
            config = {"file_sources": [{"note": "source/x/n.md", "dir": "source/x/dir", "known_dirs": ["source/x/dir_排除"], "fetcher": "fake"}]}
            note = sources.NewNote("2026-06-01_a.md", "a", "2026-06-01", "https://u", "", "正文", (("问1", "答1"), ("问2", "答2")))
            with patch.object(cli, "ROOT", root), patch.dict(sources.FILE_FETCHERS, {"fake": lambda s, k, since: ([note], {})}):
                result = {"report": {}, "auto_write_paths": [], "rough_created": [], "rough_sources": {}, "alerts": []}
                writes = {}
                cli.stage_file_sources(config, result, writes, datetime(2026, 9, 20, tzinfo=timezone.utc))
                self.assertEqual(len(result["rough_created"]), 2)
                self.assertEqual(result["report"]["source/x/n.md"]["status"], "new_articles_staged")
                text = writes[root / "source/x/dir/2026-06-01_a.md"]
                self.assertIn("| 问2 | 答2 | 2026-06-01 |", text)
            with patch.object(cli, "ROOT", root), patch.dict(sources.FILE_FETCHERS, {"fake": lambda s, k, since: (_ for _ in ()).throw(SafetyStop("403"))}):
                result = {"report": {}, "auto_write_paths": [], "rough_created": [], "rough_sources": {}, "alerts": []}
                cli.stage_file_sources(config, result, {}, datetime(2026, 9, 20, tzinfo=timezone.utc))
                self.assertEqual(result["report"]["source/x/n.md"]["status"], "failed")


class JspccTests(unittest.TestCase):
    SOURCE = {"url": "https://www.jspcc.org.cn/spzx/web/column/nwwd/1.html",
              "list_url": "https://www.jspcc.org.cn/spzx/web/column/nwwd/{page}.html"}

    def test_list_decodes_every_object_despite_the_trailing_comma(self):
        items = sources.parse_jspcc_list(fixture("jspcc_list.html"), self.SOURCE["url"])
        self.assertEqual(len(items), 10)
        self.assertEqual(items[0].date, "2026-02-03")
        self.assertTrue(items[0].url.startswith("https://www.jspcc.org.cn/spzx/web/article/"))

    def test_article_without_wen_da_markers_uses_the_title_as_question(self):
        title, question, answer, date = sources.parse_jspcc_article(fixture("jspcc_article.html"))
        self.assertEqual(title, question)
        self.assertFalse(title[0].isdigit())
        self.assertTrue(answer.startswith("在进行稳定性研究时"))
        self.assertEqual(date, "2026-02-03")

    def test_wen_da_body_is_split_into_question_and_answer(self):
        article = {"title": "12、变更备案怎么办？", "releaseDate": 1770071160000, "content": "<p>问：药品变更备案怎么办？</p><p>答：按规定备案。</p>"}
        page = "var article = " + json.dumps(article, ensure_ascii=False) + ";"
        _, question, answer, _ = sources.parse_jspcc_article(page)
        self.assertEqual((question, answer), ("药品变更备案怎么办？", "按规定备案。"))

    def test_device_and_cosmetic_questions_are_left_out(self):
        listing = fixture("jspcc_list.html")
        article = fixture("jspcc_article.html")
        rows, meta = sources.fetch_jspcc(self.SOURCE, set(), "2026-01-01", get=lambda url: listing if url.endswith("/1.html") else article)
        self.assertEqual(rows, [])
        self.assertGreaterEqual(meta["filtered_count"], 1)

    def test_empty_list_page_is_a_failure(self):
        with self.assertRaises(SafetyStop):
            sources.fetch_jspcc(self.SOURCE, set(), "2026-01-01", get=lambda url: "var articleData = [];")


class NifdcTests(unittest.TestCase):
    URL = "https://www.nifdc.org.cn/nifdc/ywzx/jyywzx/cjgxwtjd/index.html"

    def test_list_ignores_navigation_and_reads_bracketed_dates(self):
        items = sources.parse_li_list(fixture("nifdc_list.html"), self.URL)
        self.assertEqual([i.date for i in items], ["2026-02-25", "2025-08-04", "2024-09-18"])

    def test_article_body_is_found_and_split_into_questions(self):
        page = fixture("nifdc_article.html")
        body = sources.html_to_text(sources.extract_block(page, sources.BODY_SELECTORS[-1]))
        self.assertGreater(len(sources.split_qa(body)), 3)

    def test_stem_cell_articles_are_off_topic(self):
        self.assertFalse(sources.on_topic("中检院干细胞合同检验常见问题(专题第一期)", "药品"))

    def test_insecure_tls_is_used_only_when_the_source_asks_for_it(self):
        seen = []

        def fake(url, timeout=45, verify=True):
            seen.append(verify)
            return fixture("nifdc_list.html")
        with patch.object(sources, "http_get", fake):
            sources.fetch_article_source({"url": self.URL, "insecure_tls": True}, {i.url for i in sources.parse_li_list(fixture("nifdc_list.html"), self.URL)}, "2000-01-01", get=sources.http_get)
        self.assertEqual(seen, [False])


class ShandongTests(unittest.TestCase):
    SOURCE = {"url": "http://mpa.shandong.gov.cn/col/col101798/index.html",
              "api_url": "http://mpa.shandong.gov.cn/api/unit", "api_params": {"pageId": "x"}}

    @staticmethod
    def payload(*items):
        li = "".join(f'<li><a title="{t}" href="{u}" target="_blank">{t}</a><span>{d}</span></li>' for t, u, d in items)
        return json.dumps({"success": True, "data": {"html": f'<div class="page-content">{li}</div>'}}, ensure_ascii=False)

    def test_numbered_qa_is_split(self):
        text = "标题\n发布日期：2025-08-20\n一\n问题一？\n答案一。\n补充。\n二\n问题二？\n答案二。"
        self.assertEqual(sources.split_numbered(text), [("问题一？", "答案一。\n补充。"), ("问题二？", "答案二。")])

    def test_real_page_is_read_and_cosmetics_and_wechat_items_are_not_drafted(self):
        pages = iter([fixture("shandong_list_page2.json"), self.payload()])
        rows, meta = sources.fetch_shandong(self.SOURCE, set(), "2000-01-01", get=lambda url: next(pages), max_pages=2)
        self.assertEqual(rows, [])
        self.assertTrue(any("化妆品" in f["title"] for f in meta["filtered_out"]))
        self.assertTrue(meta["skipped_items"])

    def test_on_site_article_becomes_one_row_per_numbered_question(self):
        page_url = "/col/col101798/art/2025/art_x.html"
        listing = self.payload(("“检”问百“答” | 药品检验问题解答", page_url, "2025-08-20"), ("对话某分局", "https://sdxw.iqilu.com/a.html", "2025-08-19"))
        article = fixture("shandong_article.html")
        calls = []

        def get(url):
            calls.append(url)
            return article if url.endswith("art_x.html") else listing
        rows, _ = sources.fetch_shandong(self.SOURCE, set(), "2025-01-01", get=get, max_pages=1)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0].date, "2025-08-20")
        self.assertEqual(rows[0].article_url, "http://mpa.shandong.gov.cn/col/col101798/art/2025/art_x.html")
        self.assertFalse(any("iqilu" in url for url in calls))

    def test_wechat_articles_are_reported_not_drafted(self):
        listing = self.payload(("“检”问百“答” | 药品微生物检验问题解答", "https://mp.weixin.qq.com/s/abc", "2025-12-24"))
        rows, meta = sources.fetch_shandong(self.SOURCE, set(), "2025-01-01", get=lambda url: listing, max_pages=1)
        self.assertEqual(rows, [])
        self.assertIn("微信", meta["skipped_items"][0]["reason"])

    def test_known_and_old_items_are_not_opened(self):
        listing = self.payload(("“检”问百“答” | 药品检验问题解答", "/col/x/art_y.html", "2025-08-20"))
        calls = []

        def get(url):
            calls.append(url)
            return listing
        sources.fetch_shandong(self.SOURCE, {"http://mpa.shandong.gov.cn/col/x/art_y.html"}, "2000-01-01", get=get, max_pages=1)
        sources.fetch_shandong(self.SOURCE, set(), "2026-01-01", get=get, max_pages=1)
        self.assertEqual(len(calls), 2)  # list pages only

    def test_empty_interface_is_a_failure(self):
        with self.assertRaises(SafetyStop):
            sources.fetch_shandong(self.SOURCE, set(), "2000-01-01", get=lambda url: self.payload())


class JiangsuArticlesTests(unittest.TestCase):
    URL = "https://da.jiangsu.gov.cn/col/col84698/index.html"

    def listing(self, *items):
        rows = "".join(f'<tr><td><a class="bt_link" href="{u}"><b>·</b>{t}</a></td><td><font>{d}</font></td></tr>' for t, u, d in items)
        return f"<table>{rows}</table>"

    def test_only_drug_registration_and_change_titles_pass_and_the_rest_are_listed_as_dropped(self):
        listing = self.listing(
            ("《药品上市后变更管理办法》解读（一）", "/art/2026/9/1/art_84698_1.html", "2026-09-01"),
            ("《医疗器械经营质量管理规范》系列解读（一）", "/art/2026/9/2/art_84698_2.html", "2026-09-02"),
            ("药品经营和使用质量监督管理办法解读", "/art/2026/9/3/art_84698_3.html", "2026-09-03"),
            ("【宪法宣传周】国家根本大法", "/art/2026/9/4/art_84698_4.html", "2026-09-04"))
        article = fixture("jiangsu_article.html")
        calls = []

        def get(url):
            calls.append(url)
            return listing if url == self.URL else article
        rows, meta = sources.fetch_jiangsu_articles({"url": self.URL}, set(), "2026-01-01", get=get)
        self.assertEqual(len(calls), 2)  # list + the one on-topic article
        self.assertEqual({r.article_title for r in rows}, {"《药品上市后变更管理办法》解读（一）"})
        self.assertEqual(len(meta["filtered_out"]), 3)

    def test_insert_articles_removes_the_placeholder_line(self):
        note = "---\nlast_updated: 2026-01-01\n---\n\n## 内容\n\n_待整理。_\n"
        result = insert_articles(note, [sources.ArticleRow("问", "答", "2026-09-01", "标题", "https://x/1.html")])
        self.assertNotIn("待整理", result)
        self.assertIn("### [标题](https://x/1.html)（2026-09-01）", result)


class PdfTextTests(unittest.TestCase):
    def pages(self):
        return json.loads(fixture("cpc_pdf_pages.json"))

    def test_glyph_substitutes_and_full_width_forms_are_fixed(self):
        text = sources.clean_pdf_text(["２０２５\n年版《中国药典》\n９２１１\n中图分类号：Ｒ９２１\ue010２\nＥ\ue011ｍａｉｌ：ａ＠ｂ\ue010ｃｏｍ\ue012"])
        self.assertIn("2025年版《中国药典》9211", text)
        self.assertIn("R921.2", text)
        self.assertIn("E-mail：a@b.com", text)
        self.assertFalse(any(0xE000 <= ord(c) <= 0xF8FF for c in text))

    def test_spaces_inside_chinese_text_are_removed_but_english_spacing_kept(self):
        text = sources.clean_pdf_text(["在药品 质 量 控 制 领 域，微 生 物 污 染 是 风 险。\nUSP 922 and EP 2.9.39 apply。"])
        self.assertIn("在药品质量控制领域，微生物污染是风险。", text)
        self.assertIn("USP 922", text)

    def test_reference_list_and_english_only_paragraphs_are_dropped(self):
        english = "Interpretationofguidelines" * 6 + "。"
        text = sources.clean_pdf_text(["摘要：这是正文。\n" + english + "\n结论：这是结论。\n参考文献：\n[1]某某.某文[J].2020。"])
        self.assertIn("这是正文。", text)
        self.assertIn("这是结论。", text)
        self.assertNotIn("Interpretationofguidelines", text)
        self.assertNotIn("某某", text)

    def test_real_article_pages_read_as_chinese_paragraphs(self):
        text = sources.clean_pdf_text(self.pages())
        self.assertGreater(text.count("水分活度"), 20)
        self.assertIn("2025年版《中国药典》四部新增", text)
        self.assertNotIn("\ue010", text)
        self.assertNotIn("参考文献", text.split("总结与展望")[-1])

    def test_unreadable_bytes_give_empty_text_never_an_error(self):
        self.assertEqual(sources.pdf_text(b"not a pdf at all"), "")
        self.assertEqual(sources.pdf_text(b""), "")

    def test_attachment_text_follows_the_name_like_existing_notes(self):
        from ingestion.automation.fetchers import CPCArticle
        payload = {"result": {"news": {"newsContent": None, "annexFileList": [{"id": "AB12", "name": "解读.pdf"}]}}}
        with patch.object(sources, "pdf_text", return_value="第一段正文。\n第二段正文。"):
            notes, meta = sources.fetch_cpc_notes(
                {"detail_url": "https://x/detail?newsId={news_id}"},
                [CPCArticle("n1", "药典执行解读", "2026-06-01", "f.md")], lambda url: payload, fetch_bytes=lambda url: b"%PDF-")
        self.assertEqual(
            notes[0].body,
            "附件：\n- [解读.pdf](https://www.chp.org.cn/three/anon/user/download?id=AB12&token=)\n附件《解读》文本：\n第一段正文。\n第二段正文。")
        self.assertNotIn("attachments_without_text", meta)

    def test_failed_download_keeps_the_note_and_reports_it(self):
        from ingestion.automation.fetchers import CPCArticle
        payload = {"result": {"news": {"newsContent": None, "annexFileList": [{"id": "AB12", "name": "解读.pdf"}]}}}

        def down(url):
            raise SafetyStop("HTTP 500")
        notes, meta = sources.fetch_cpc_notes(
            {"detail_url": "https://x/detail?newsId={news_id}"},
            [CPCArticle("n1", "药典执行解读", "2026-06-01", "f.md")], lambda url: payload, fetch_bytes=down)
        self.assertIn("[解读.pdf]", notes[0].body)
        self.assertIn("下载失败", meta["attachments_without_text"][0]["reason"])

    @unittest.skipUnless(__import__("importlib").util.find_spec("pypdf"), "pypdf is not installed here")
    def test_pypdf_is_used_when_installed(self):
        # a one-page PDF with no text layer must come back empty, not raise
        import pypdf
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buffer = __import__("io").BytesIO()
        writer.write(buffer)
        self.assertEqual(sources.pdf_text(buffer.getvalue()), "")


class SourceUrlTests(unittest.TestCase):
    """Every draft records the official page of its own question when the source has one."""

    def test_jiangsu_rows_carry_the_article_address(self):
        listing, article = fixture("jiangsu_list_91813.html"), fixture("jiangsu_article.html")
        rows, _ = sources.fetch_jiangsu({"url": LIST_URL}, set(), "2026-02-28", get=lambda url: listing if url == LIST_URL else article)
        self.assertTrue(rows)
        self.assertTrue(all(row.url.startswith("https://da.jiangsu.gov.cn/art/") for row in rows))

    def test_beijing_rows_carry_the_letter_page_address(self):
        def letter(question, answer):
            return f'<div class="sino-text-format">{question}</div><div class="sino-text-format">{answer}</div>'
        listing = "{page: {pageNo:'1', totalCount:'1', totalPages:'1', pageSize:'20'}, result: [{originalId:'AH42', letterTitle:'咨询', finishDateReal:'2026-06-01'}]}"
        page = letter("药品说明书变更需要备案吗", "网民您好！需要向北京市局备案，并提交相关资料。" * 12)
        rows, _ = sources.fetch_beijing({"url": "https://yjj.beijing.gov.cn/a/b.html"}, set(), "2026-01-01",
                                        get=lambda url: listing if "letterList" in url else page)
        self.assertEqual(rows[0].url, "https://yjj.beijing.gov.cn/a/bjah-index-dept!detail.action?originalId=AH42")

    def test_a_row_url_is_not_part_of_its_identity(self):
        self.assertEqual(Row("q", "a", "2026-03-01", url="https://x/1"), Row("q", "a", "2026-03-01"))
        self.assertEqual(Row("q", "a", "2026-03-01", url="https://x/1").key, Row("q", "a", "2026-03-01").key)

    def test_article_rows_still_take_title_and_url_positionally(self):
        row = sources.ArticleRow("问", "答", "2026-03-01", "文章标题", "https://x/article")
        self.assertEqual((row.article_title, row.article_url, row.url), ("文章标题", "https://x/article", ""))

    def test_the_draft_gets_a_source_url_line_only_when_there_is_one(self):
        with_url = cli.rough_content("source/a", [Row("问", "答", "2026-03-01")], "2026-09-21", "https://x/1")
        without = cli.rough_content("source/a", [Row("问", "答", "2026-03-01")], "2026-09-21")
        self.assertIn('source_url: "https://x/1"\n', with_url)
        self.assertNotIn("source_url", without)
        self.assertLess(with_url.index("source_url"), with_url.index("status: pending_review"))

    def test_staged_drafts_carry_the_row_or_article_address(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").mkdir()
            (root / "ingestion" / "rough").mkdir(parents=True)
            (root / "source" / "a.md").write_text(TableSourceStagingTests.NOTE, encoding="utf-8")
            (root / "source" / "b.md").write_text(TableSourceStagingTests.NOTE.replace("## 内容\n", "## 内容\n"), encoding="utf-8")
            with patch.object(cli, "ROOT", root), patch.object(cli, "repo_fingerprint", return_value={}):
                for name, layout, row in (("a", "table", Row("行问题", "答", "2026-05-29", url="https://x/row")),
                                          ("b", "articles", sources.ArticleRow("文章问题", "答", "2026-05-29", "文章", "https://x/art"))):
                    config = {"table_sources": [{"path": f"source/{name}.md", "fetcher": "fake", "layout": layout, "auto_classified": True, "auto_ingest": True}]}
                    with patch.dict(cli.FETCHERS, {"fake": lambda s_, k, since, row=row: ([row], {})}):
                        result = cli.base_report(datetime(2026, 9, 21, tzinfo=timezone.utc), "dry-run")
                        writes = {}
                        cli.stage_table_sources(config, result, writes, datetime(2026, 9, 21, tzinfo=timezone.utc))
                    draft = writes[root / result["rough_created"][0]]
                    self.assertIn(f'source_url: "{"https://x/row" if name == "a" else "https://x/art"}"', draft)

    def test_per_article_notes_pass_their_page_address_to_the_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ingestion" / "rough").mkdir(parents=True)
            with patch.object(cli, "ROOT", root):
                result = {"report": {}, "auto_write_paths": [], "rough_created": [], "rough_sources": {}}
                writes = {}
                note = sources.NewNote("2026-06-01_a.md", "a", "2026-06-01", "https://www.chp.org.cn/#/newsDetail?id=1", "", "正文")
                cli.stage_new_notes(result, writes, [note], "source/CPC/专栏", "source/CPC/专栏.md", datetime(2026, 9, 21, tzinfo=timezone.utc))
                self.assertIn('source_url: "https://www.chp.org.cn/#/newsDetail?id=1"', writes[root / result["rough_created"][0]])


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

    def test_rows_before_the_earliest_date_are_ignored_even_when_the_watermark_is_older(self):
        (self.root / "source" / "a.md").write_text(self.NOTE.replace("2026-02-28", "2026-01-01"), encoding="utf-8")
        self.config["earliest_date"] = "2026-03-01"
        seen = {}

        def fetch(source, known, since):
            seen["since"] = since
            return [Row("二月的问题", "答", "2026-02-10"), Row("三月的问题", "答", "2026-03-05")], {}
        result, writes = self.run_stage(fetch)
        self.assertEqual(seen["since"], "2026-02-28")            # never earlier than the day before the floor
        note = writes[self.root / "source" / "a.md"]
        self.assertIn("三月的问题", note)
        self.assertNotIn("二月的问题", note)
        self.assertEqual(len(result["rough_created"]), 1)

    def test_a_later_watermark_still_wins_over_the_floor(self):
        (self.root / "source" / "a.md").write_text(self.NOTE.replace("2026-02-28", "2026-07-14"), encoding="utf-8")
        self.config["earliest_date"] = "2026-03-01"
        seen = {}

        def fetch(source, known, since):
            seen["since"] = since
            return [], {}
        self.run_stage(fetch)
        self.assertEqual(seen["since"], "2026-07-14")

    def test_a_changed_old_answer_does_not_block_or_get_reported_once_the_floor_is_set(self):
        self.config["earliest_date"] = "2026-03-01"
        for blocks in (False, True):
            result = cli.base_report(self.now, "dry-run")
            writes = {}
            cli.stage_source_rows(result, writes, self.config["table_sources"][0],
                                  [Row("旧问题", "改过的答案", "2026-01-05")], {}, self.now,
                                  revisions_block=blocks, earliest="2026-03-01")
            self.assertFalse(result["blocking"])
            self.assertEqual(result["report"]["source/a.md"]["status"], "no_change")
            self.assertEqual(writes, {})

    def test_without_a_floor_a_changed_old_answer_still_blocks_when_asked_to(self):
        result = cli.base_report(self.now, "dry-run")
        cli.stage_source_rows(result, {}, self.config["table_sources"][0],
                              [Row("旧问题", "改过的答案", "2026-01-05")], {}, self.now, revisions_block=True)
        self.assertTrue(result["blocking"])

    def test_the_shipped_config_sets_the_floor(self):
        self.assertEqual(cli.earliest_date(cli.load_config()), "2026-03-01")

    def test_table_sources_are_on_the_automatic_write_allowlist(self):
        config = {"cde": {"sources": []}, **self.config}
        self.assertEqual(cli.automatic_write_allowlist(config), {"source/a.md"})


class DuplicateAcrossColumnsTests(unittest.TestCase):
    """The same item listed under two columns of one site becomes one draft that names both."""
    NOTE = "---\nentity: x\nlast_updated: 2026-02-28\n---\n\n## 内容\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "source").mkdir()
        (self.root / "ingestion" / "rough").mkdir(parents=True)
        for name in ("a", "b"):
            (self.root / "source" / f"{name}.md").write_text(self.NOTE, encoding="utf-8")
        self.enterContext(patch.object(cli, "ROOT", self.root))
        self.enterContext(patch.object(cli, "repo_fingerprint", return_value={}))
        self.now = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
        self.config = {"cde": {"sources": []}, "table_sources": [
            {"path": f"source/{name}.md", "fetcher": f"fake_{name}", "auto_classified": True, "auto_ingest": True} for name in ("a", "b")]}

    def stage(self, rows_a, rows_b):
        fetchers = {"fake_a": lambda s, k, since: (rows_a, {}), "fake_b": lambda s, k, since: (rows_b, {})}
        self.enterContext(patch.dict(cli.FETCHERS, fetchers))
        result = cli.base_report(self.now, "dry-run")
        writes: dict = {}
        cli.stage_table_sources(self.config, result, writes, self.now)
        result["planned_writes"] = sorted(str(p.relative_to(self.root)) for p in writes)
        return result, writes

    def test_the_same_question_and_answer_in_two_notes_makes_one_draft_naming_both(self):
        row = Row("同一个问题会出现两次吗", "同一个解答", "2026-06-01")
        result, writes = self.stage([row], [row])
        self.assertEqual(len(result["rough_created"]), 1)
        draft = writes[self.root / result["rough_created"][0]]
        self.assertIn('source: "[[source/a]] [[source/b]]"', draft)
        self.assertEqual(result["rough_also_sources"], {result["rough_created"][0]: ["source/b.md"]})
        for name in ("a", "b"):                       # both notes still get the row and are marked updated
            self.assertIn("同一个问题会出现两次吗", writes[self.root / "source" / f"{name}.md"])
            self.assertEqual(result["report"][f"source/{name}.md"]["status"], "updated_with_new")
        cli.validate_report_invariants(result)
        self.assertEqual(len(writes), 3)

    def test_the_automatic_plan_accepts_the_merged_pair(self):
        row = Row("同一个问题会出现两次吗", "同一个解答", "2026-06-01")
        result, writes = self.stage([row], [row])
        result["auto_write_paths"] = sorted(result["planned_writes"])
        self.assertEqual(cli.validate_automatic_plan(self.config, result, writes), set(result["planned_writes"]))

    def test_the_automatic_plan_still_refuses_an_unpaired_source(self):
        row = Row("同一个问题会出现两次吗", "同一个解答", "2026-06-01")
        result, writes = self.stage([row], [row])
        result["auto_write_paths"] = sorted(result["planned_writes"])
        result["rough_also_sources"] = {}
        with self.assertRaises(SafetyStop):
            cli.validate_automatic_plan(self.config, result, writes)
        with self.assertRaises(SafetyStop):
            cli.validate_report_invariants(result)

    def test_a_different_answer_or_question_keeps_two_drafts(self):
        result, _ = self.stage([Row("同一个问题会出现两次吗", "解答甲", "2026-06-01")], [Row("同一个问题会出现两次吗", "解答乙", "2026-06-01")])
        self.assertEqual(len(result["rough_created"]), 2)
        self.assertEqual(result["rough_also_sources"], {})

    def test_the_same_item_twice_in_one_column_is_not_merged(self):
        row = Row("同一个问题会出现两次吗", "同一个解答", "2026-06-01")
        result, _ = self.stage([row, row], [])
        self.assertEqual(len(result["rough_created"]), 2)

    def test_only_the_repeated_row_is_merged_when_a_column_has_other_new_rows(self):
        shared = Row("同一个问题会出现两次吗", "同一个解答", "2026-06-01")
        result, writes = self.stage([shared], [Row("只在第二栏目出现的问题", "另一个解答", "2026-06-02"), shared])
        self.assertEqual(len(result["rough_created"]), 2)
        names = sorted(path.name for path in writes if path.parent.name == "rough")
        self.assertEqual(names, ["20260920_a_增量_1.md", "20260920_b_增量_1.md"])
        self.assertEqual(sum(1 for text in writes.values() if "[[source/a]] [[source/b]]" in text), 1)

    def test_a_draft_name_used_by_an_earlier_run_is_not_reused_after_the_draft_was_deleted(self):
        logs = self.root / "ingestion" / "logs"; logs.mkdir(parents=True)
        used = ["ingestion/rough/20260920_a_增量_1.md", "ingestion/rough/20260920_a_增量_4.md", "ingestion/rough/20260920_b_增量_9.md"]
        (logs / "source_ingest_20260920_1000_report.json").write_text(json.dumps({"rough_created": used}), encoding="utf-8")
        (logs / "source_ingest_broken_report.json").write_text("{not json", encoding="utf-8")
        result, writes = self.stage([Row("第一个新问题会从五号开始编号吗", "答一", "2026-06-01")], [Row("另一个来源的新问题", "答二", "2026-06-02")])
        self.assertEqual(sorted(Path(p).name for p in result["rough_created"]), ["20260920_a_增量_5.md", "20260920_b_增量_10.md"])

    def test_the_audit_counts_a_second_source_only_when_the_draft_names_it(self):
        from ingestion.automation.audit import audit_history
        row = Row("同一个问题会出现两次吗", "同一个解答", "2026-06-01")
        result, writes = self.stage([row], [row])
        logs = self.root / "ingestion" / "logs"; logs.mkdir(parents=True)
        rough = result["rough_created"][0]
        (self.root / rough).write_text(writes[self.root / rough], encoding="utf-8")
        payload = {"date": "2026-09-20", "report": result["report"], "rough_created": result["rough_created"],
                   "rough_sources": result["rough_sources"], "rough_also_sources": result["rough_also_sources"]}
        (logs / "source_ingest_2026-09-20_report.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(audit_history(self.root)["missing_rough_events"], 0)
        text = (self.root / rough).read_text(encoding="utf-8").replace(' [[source/b]]', "")
        (self.root / rough).write_text(text, encoding="utf-8")
        audit = audit_history(self.root)
        self.assertEqual(audit["missing_rough_events"], 1)
        self.assertEqual(audit["backlog"][0]["source"], "source/b.md")

    def test_the_audit_rejects_a_malformed_also_sources_entry(self):
        from ingestion.automation.audit import audit_history
        logs = self.root / "ingestion" / "logs"; logs.mkdir(parents=True)
        (logs / "source_ingest_2026-09-20_report.json").write_text(
            json.dumps({"date": "2026-09-20", "report": {}, "rough_also_sources": {"x": "source/b.md"}}), encoding="utf-8")
        self.assertEqual(len(audit_history(self.root)["report_errors"]), 1)


if __name__ == "__main__":
    unittest.main()


class CpcStagingTests(unittest.TestCase):
    def test_new_articles_become_notes_in_the_included_dir_with_one_draft_each(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source" / "CPC").mkdir(parents=True)
            (root / "ingestion" / "rough").mkdir(parents=True)
            with patch.object(cli, "ROOT", root):
                result = {"report": {}, "auto_write_paths": [], "rough_created": [], "rough_sources": {}}
                writes = {}
                notes = [sources.NewNote("2026-06-01_解读.md", "解读", "2026-06-01", "https://u", "", "正文")]
                cli.stage_new_notes(result, writes, notes, "source/CPC/专栏", "source/CPC/专栏.md", datetime(2026, 9, 20, tzinfo=timezone.utc))
                self.assertEqual(result["report"]["source/CPC/专栏/2026-06-01_解读.md"]["status"], "updated_with_new")
                self.assertEqual(len(result["rough_created"]), 1)
                self.assertEqual(set(result["rough_sources"].values()), {"source/CPC/专栏/2026-06-01_解读.md"})
                self.assertIn('source: "[[source/CPC/专栏/2026-06-01_解读]]"', writes[root / result["rough_created"][0]])
                cli.validate_report_invariants(result)
                config = {"cpc": {"included_dir": "source/CPC/专栏"}, "cde": {"sources": []}}
                result.update(planned_writes=sorted(str(p.relative_to(root)) for p in writes))
                cli.validate_automatic_plan(config, result, writes)

    def test_a_new_note_outside_the_included_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").mkdir()
            with patch.object(cli, "ROOT", root):
                config = {"cpc": {"included_dir": "source/CPC/专栏"}, "cde": {"sources": []}}
                writes = {root / "source" / "elsewhere.md": "x"}
                report = {"planned_writes": ["source/elsewhere.md"], "auto_write_paths": ["source/elsewhere.md"], "rough_created": [], "rough_sources": {}, "report": {"source/elsewhere.md": {"status": "updated_with_new"}}}
                with self.assertRaises(SafetyStop):
                    cli.validate_automatic_plan(config, report, writes)
