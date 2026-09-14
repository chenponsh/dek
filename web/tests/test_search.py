import json
import subprocess
import unittest
from pathlib import Path


SEARCH_JS = Path(__file__).parents[1] / "assets" / "search.js"


class FuzzySearchTests(unittest.TestCase):
    def run_javascript(self, expression):
        script = f"const s=require({json.dumps(str(SEARCH_JS))}); process.stdout.write(JSON.stringify({expression}));"
        result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
        return json.loads(result.stdout)

    def run_search(self, query):
        documents = [
            {"title": "药品上市许可持有人变更", "text": "生产场地发生变化时提交补充申请", "path": "wiki/16_注册变更/持有人变更.md", "kind": "wiki", "url": "a.html", "tags": ["注册变更", "上市许可持有人"]},
            {"title": "临床试验样品要求", "text": "临床样品包装与标签要求", "path": "wiki/04_临床试验/样品.md", "kind": "wiki", "url": "b.html", "tags": ["临床试验"]},
            {"title": "CDE 来源资料", "text": "药品注册申请受理审查指南", "path": "source/CDE/指南.md", "kind": "source", "url": "c.html", "tags": []},
        ]
        script = f"const s=require({json.dumps(str(SEARCH_JS))}); process.stdout.write(JSON.stringify(s.searchDocuments({json.dumps(documents, ensure_ascii=False)}, {json.dumps(query, ensure_ascii=False)})));"
        result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
        return json.loads(result.stdout)

    def test_result_url_resolves_from_site_root_not_assets_directory(self):
        url = self.run_javascript("s.resultUrl({url:'wiki/中文.html'}, 'https://regkb.chenponai.com/kb/assets/search-index.json')")
        self.assertEqual(url, "https://regkb.chenponai.com/kb/wiki/%E4%B8%AD%E6%96%87.html")

    def test_ignores_spaces_punctuation_case_and_width(self):
        self.assertEqual(self.run_search("ＣＤＥ，来源")[0]["url"], "c.html")

    def test_multiple_terms_can_match_different_fields(self):
        self.assertEqual(self.run_search("持有人 生产场地")[0]["url"], "a.html")

    def test_missing_character_still_matches(self):
        self.assertEqual(self.run_search("上市许可持有变更")[0]["url"], "a.html")

    def test_typo_still_matches(self):
        self.assertEqual(self.run_search("上市许可持有人便更")[0]["url"], "a.html")

    def test_extra_character_still_matches(self):
        self.assertEqual(self.run_search("上市许可持有人信息变更")[0]["url"], "a.html")

    def test_term_order_does_not_change_top_result(self):
        self.assertEqual(self.run_search("生产场地 持有人")[0]["url"], "a.html")

    def test_title_and_tags_rank_before_body_only_match(self):
        hits = self.run_search("注册变更")
        self.assertEqual(hits[0]["url"], "a.html")

    def run_recent(self, documents, days, as_of):
        script = f"const s=require({json.dumps(str(SEARCH_JS))}); process.stdout.write(JSON.stringify(s.recentDocuments({json.dumps(documents, ensure_ascii=False)}, {days}, {json.dumps(as_of)})));"
        result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
        return json.loads(result.stdout)

    def test_recent_documents_filters_by_publication_date_window(self):
        documents = [
            {"path": "a.md", "date": "2026-09-13"},
            {"path": "b.md", "date": "2026-09-01"},
            {"path": "c.md", "date": None},
            {"path": "d.md", "date": "2026-08-01"},
            {"path": "e.md", "date": "2026-09-10"},
        ]
        recent = self.run_recent(documents, 7, "2026-09-14")
        self.assertEqual([d["path"] for d in recent], ["a.md", "e.md"])

    def test_recent_documents_are_sorted_by_date_descending(self):
        documents = [
            {"path": "x.md", "date": "2026-09-01"},
            {"path": "y.md", "date": "2026-09-13"},
        ]
        recent = self.run_recent(documents, 30, "2026-09-14")
        self.assertEqual([d["path"] for d in recent], ["y.md", "x.md"])

    def test_recent_documents_accepts_date_objects_and_full_timestamps(self):
        documents = [
            {"path": "a.md", "date": "2026-09-13 00:00:00"},
            {"path": "b.md", "date": "2026-09-13"},
        ]
        recent = self.run_recent(documents, 7, "2026-09-14")
        self.assertEqual(len(recent), 2)



if __name__ == "__main__":
    unittest.main()
