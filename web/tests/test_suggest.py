import tempfile
import unittest
from pathlib import Path

from web.suggest import MIN_SIMILARITY, folder_index, suggest_folders


def note(no: int, question: str, body: str) -> str:
    return f"---\nno: {no}\ndate: 2026-01-01\nquestion: \"{question}\"\nsource:\ntag_pages:\ntags:\n---\n\n{body}\n"


class SuggestFoldersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        wiki = self.root / "wiki"
        for folder, entries in {
            "05_药学研究/0507_溶出曲线": [
                ("溶出曲线相似性因子f2如何计算？", "比较参比制剂与自制制剂的溶出曲线时，应采用相似性因子f2进行评价，取样点不少于三个。"),
                ("溶出曲线研究应选择哪些溶出介质？", "溶出曲线研究通常选择不同pH的溶出介质，并说明转速和取样时间点。"),
                ("溶出度方法学验证有哪些要求？", "溶出度检查方法应进行专属性、线性、准确度验证，溶出介质需考察稳定性。"),
            ],
            "01_注册申报/0106_受理审查": [
                ("申报资料受理审查需要提交哪些光盘？", "受理审查阶段申请人应按要求提交申报资料光盘和档案盒，并核对资料目录。"),
                ("受理审查不通过如何处理？", "受理审查未通过的，申请人可补充资料后重新提交受理审查。"),
                ("受理审查的时限是多久？", "药审中心在收到申报资料后对资料进行受理审查，并在规定时限内作出决定。"),
            ],
        }.items():
            directory = wiki / folder
            directory.mkdir(parents=True)
            prefix = folder.split("/")[-1][:4]
            for number, (question, body) in enumerate(entries, 1):
                (directory / f"{prefix}-{number:04d}.md").write_text(note(number, question, body), encoding="utf-8")
            (directory / f"{folder.split('/')[-1]}.md").write_text("---\naliases:\n  - 概览\n---\n\n目录页\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_ranks_the_folder_holding_similar_entries_first(self):
        ranked = suggest_folders(self.root, "溶出曲线f2相似性因子的计算方法", "两条溶出曲线比较时应采用相似性因子f2，选择不同pH溶出介质。")
        self.assertEqual(ranked[0], "wiki/05_药学研究/0507_溶出曲线")
        other = suggest_folders(self.root, "受理审查时需要提交光盘吗", "受理审查阶段应提交申报资料光盘和档案盒。")
        self.assertEqual(other[0], "wiki/01_注册申报/0106_受理审查")

    def test_returns_at_most_the_requested_number_of_distinct_folders(self):
        ranked = suggest_folders(self.root, "溶出曲线受理审查光盘", "溶出介质和受理审查资料", limit=3)
        self.assertLessEqual(len(ranked), 3)
        self.assertEqual(len(ranked), len(set(ranked)))

    def test_gives_no_suggestion_when_nothing_is_similar(self):
        self.assertEqual(suggest_folders(self.root, "zzz qqq", "完全无关的内容XYZ"), [])
        self.assertEqual(suggest_folders(self.root, "", ""), [])

    def test_index_pages_without_a_number_and_question_are_not_evidence(self):
        index = folder_index(str(self.root))
        self.assertEqual(index.size, 6)  # the two overview pages are skipped

    def test_missing_wiki_folder_is_harmless(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(suggest_folders(Path(empty), "溶出曲线", "溶出介质"), [])

    def test_threshold_is_a_named_constant(self):
        self.assertGreater(MIN_SIMILARITY, 0)


if __name__ == "__main__":
    unittest.main()
