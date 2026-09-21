"""Source material dated from 2026-03-01 on is public only once a reviewer approved it."""
import json
import tempfile
import unittest
from pathlib import Path

from web.site import SOURCE_APPROVAL_FLOOR, build_site

APPROVED_Q = "药品包装中盒属于外标签吗，按二十四号令第十八条应当注明哪些内容"
PENDING_Q = "微生态活菌制品申请注册医疗机构制剂行政路径通吗"
OLD_Q = "二零二五年之前已经收录的老问题不受这条规则影响"


class SourceApprovalGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.out = Path(self.tmp.name) / "site"
        (self.vault / "wiki" / "01_注册").mkdir(parents=True)
        (self.vault / "source" / "北京").mkdir(parents=True)
        (self.vault / "source" / "CPC" / "专栏").mkdir(parents=True)
        self.write("source/北京/咨询.md",
                   "---\nentity: 北京局\nlast_updated: 2026-09-21\n---\n\n## 内容\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n"
                   f"| {APPROVED_Q} | 已批准的解答 | 2026-05-08 |\n"
                   f"| {PENDING_Q} | 待审的解答 | 2026-08-03 |\n"
                   f"| {OLD_Q} | 老解答 | 2025-12-01 |\n")
        self.write("wiki/01_注册/0101-0001.md",
                   f"---\nno: 1\ndate: 2026-05-08\nquestion: \"{APPROVED_Q}\"\nsource: \"[[source/北京/咨询]]\"\ntags:\n  - 注册\n---\n\n已批准的解答\n")
        self.write("source/CPC/专栏/2026-06-17_已批准文章.md",
                   "---\ndate: 2026-06-17\n---\n\n## 内容\n\n已批准文章正文。\n")
        self.write("source/CPC/专栏/2026-06-18_待审文章.md",
                   "---\ndate: 2026-06-18\n---\n\n## 内容\n\n待审文章正文。\n")
        self.write("source/CPC/专栏/2025-06-18_旧文章.md",
                   "---\ndate: 2025-06-18\n---\n\n## 内容\n\n旧文章正文。\n")
        self.write("source/CPC/专栏.md",
                   "---\nlast_updated: 2026-09-21\n---\n\n| 标题 | 发布日期 |\n| --- | --- |\n"
                   "| [[source/CPC/专栏/2026-06-18_待审文章]] | 2026-06-18 |\n")
        self.write("wiki/01_注册/0101-0002.md",
                   "---\nno: 2\ndate: 2026-06-17\nquestion: 已批准文章的标题\nsource: \"[[source/CPC/专栏/2026-06-17_已批准文章]]\"\ntags:\n  - 注册\n---\n\n正文\n")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def build(self):
        build_site(self.vault, self.out)
        docs = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))["documents"]
        return {d["path"] for d in docs}, (self.out / "assets" / "search-index.json").read_text(encoding="utf-8")

    def test_the_floor_is_the_ingest_floor(self):
        config = json.loads((Path(__file__).parents[2] / "ingestion" / "automation" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(SOURCE_APPROVAL_FLOOR, config["earliest_date"])

    def test_a_dated_source_note_nobody_approved_is_not_built_listed_or_searchable(self):
        paths, search = self.build()
        self.assertNotIn("source/CPC/专栏/2026-06-18_待审文章.md", paths)
        self.assertFalse((self.out / "source" / "CPC" / "专栏" / "2026-06-18_待审文章.html").exists())
        self.assertNotIn("待审文章正文", search)

    def test_a_dated_source_note_that_an_approved_wiki_entry_points_to_stays(self):
        paths, _ = self.build()
        self.assertIn("source/CPC/专栏/2026-06-17_已批准文章.md", paths)

    def test_notes_before_the_floor_are_untouched(self):
        paths, search = self.build()
        self.assertIn("source/CPC/专栏/2025-06-18_旧文章.md", paths)
        self.assertIn("旧文章正文", search)

    def test_table_rows_from_the_floor_on_show_only_when_approved(self):
        self.build()
        page = (self.out / "source" / "北京" / "咨询.html").read_text(encoding="utf-8")
        self.assertIn(APPROVED_Q, page)
        self.assertIn("已批准的解答", page)
        self.assertIn(OLD_Q, page)
        self.assertNotIn(PENDING_Q, page)
        self.assertNotIn("待审的解答", page)

    def test_held_back_rows_do_not_leave_the_raw_text_in_the_search_index(self):
        _, search = self.build()
        self.assertNotIn(PENDING_Q, search)
        self.assertNotIn("待审的解答", search)
        self.assertIn(APPROVED_Q, search)

    def test_a_list_row_pointing_at_a_held_back_note_is_gone_not_a_broken_link(self):
        self.build()
        page = (self.out / "source" / "CPC" / "专栏.html").read_text(encoding="utf-8")
        self.assertNotIn("待审文章", page)
        self.assertNotIn("broken-link", page)

    def test_approving_the_entry_releases_the_row_on_the_next_build(self):
        self.write("wiki/01_注册/0101-0003.md",
                   f"---\nno: 3\ndate: 2026-08-03\nquestion: \"{PENDING_Q}\"\nsource: \"[[source/北京/咨询]]\"\ntags:\n  - 注册\n---\n\n待审的解答\n")
        self.build()
        page = (self.out / "source" / "北京" / "咨询.html").read_text(encoding="utf-8")
        self.assertIn(PENDING_Q, page)

    def test_a_wiki_entry_pointing_at_another_note_does_not_release_the_row(self):
        self.write("wiki/01_注册/0101-0004.md",
                   f"---\nno: 4\ndate: 2026-08-03\nquestion: \"{PENDING_Q}\"\nsource: \"[[source/CPC/专栏]]\"\ntags:\n  - 注册\n---\n\n别的来源\n")
        self.build()
        page = (self.out / "source" / "北京" / "咨询.html").read_text(encoding="utf-8")
        self.assertNotIn("待审的解答", page)

    def test_an_entry_naming_two_source_notes_releases_the_row_in_both(self):
        row = "| {q} | 两处都有的解答 | 2026-08-04 |\n"
        question = "同一个问题被列在两个栏目里的时候两处都要放行"
        for name in ("甲", "乙"):
            self.write(f"source/北京/{name}.md", "---\nlast_updated: 2026-09-21\n---\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n" + row.format(q=question))
        self.write("wiki/01_注册/0101-0006.md",
                   f"---\nno: 6\ndate: 2026-08-04\nquestion: \"{question}\"\nsource: \"[[source/北京/甲]] [[source/北京/乙]]\"\ntags:\n  - 注册\n---\n\n两处都有的解答\n")
        self.build()
        for name in ("甲", "乙"):
            self.assertIn(question, (self.out / "source" / "北京" / f"{name}.html").read_text(encoding="utf-8"))

    def test_a_short_question_never_releases_a_row_by_accident(self):
        self.write("source/北京/短.md",
                   "---\nlast_updated: 2026-09-21\n---\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n| 是 | 短问题的解答 | 2026-08-01 |\n")
        self.write("wiki/01_注册/0101-0005.md",
                   "---\nno: 5\ndate: 2026-08-01\nquestion: \"是\"\nsource: \"[[source/北京/短]]\"\ntags:\n  - 注册\n---\n\n短\n")
        self.build()
        page = (self.out / "source" / "北京" / "短.html").read_text(encoding="utf-8")
        self.assertNotIn("短问题的解答", page)


if __name__ == "__main__":
    unittest.main()
