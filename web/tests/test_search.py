import json
import subprocess
import unittest
from pathlib import Path


SEARCH_JS = Path(__file__).parents[1] / "assets" / "search.js"
PAGE_JS = Path(__file__).parents[1] / "assets" / "page.js"


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

    def test_highlight_marks_every_query_word_and_escapes_the_text(self):
        html = self.run_javascript("s.highlight('溶出曲线 <b>f2</b> 相似性因子', '溶出 F2')")
        self.assertEqual(html, "<mark>溶出</mark>曲线 &lt;b&gt;<mark>f2</mark>&lt;/b&gt; 相似性因子")

    def test_highlight_of_text_without_a_hit_is_just_the_escaped_text(self):
        self.assertEqual(self.run_javascript("s.highlight('a & b', 'zzz')"), "a &amp; b")

    def test_highlight_merges_overlapping_hits(self):
        self.assertEqual(self.run_javascript("s.highlight('溶出曲线', '溶出 出曲')"), "<mark>溶出曲</mark>线")

    def test_snippet_is_centred_on_the_literal_hit(self):
        snippet = self.run_javascript("s.resultSnippet({text: '无关的话。'.repeat(30) + '这里讲溶出曲线怎么算'}, '溶出曲线')")
        self.assertIn("溶出曲线", snippet)
        self.assertTrue(snippet.startswith("…"))

    def test_count_in_range_counts_a_folder_and_ignores_similarly_named_ones(self):
        documents = json.dumps([
            {"path": "wiki/16_a/1.md", "date": "2026-09-01"}, {"path": "wiki/16_a/2.md", "date": "2026-01-01"},
            {"path": "wiki/16_a/3.md", "date": None}, {"path": "wiki/16_ab/1.md", "date": "2026-09-01"},
            {"path": "source/x.md", "date": "2026-09-01"}])
        self.assertEqual(self.run_javascript(f"s.countInRange({documents}, 'wiki/16_a', '', '')"), 3)          # no range: the total, undated included
        self.assertEqual(self.run_javascript(f"s.countInRange({documents}, 'wiki/16_a', '2026-08-01', '')"), 1)  # a range: dated and inside
        self.assertEqual(self.run_javascript(f"s.countInRange({documents}, 'wiki', '2026-08-01', '2026-09-30')"), 2)

    def test_undated_documents_are_listed_by_path_and_counted_per_folder(self):
        documents = json.dumps([
            {"path": "wiki/a/2.md", "date": None}, {"path": "wiki/a/1.md", "date": ""}, {"path": "wiki/a/3.md", "date": "2026-01-01"},
            {"path": "wiki/ab/1.md"}, {"path": "source/x.md", "date": None}])
        self.assertEqual([d["path"] for d in self.run_javascript(f"s.undatedDocuments({documents})")],
                         ["source/x.md", "wiki/a/1.md", "wiki/a/2.md", "wiki/ab/1.md"])
        self.assertEqual(self.run_javascript(f"s.countUndated({documents}, 'wiki/a')"), 2)     # not wiki/ab
        self.assertEqual(self.run_javascript(f"s.countUndated({documents}, 'wiki')"), 3)
        self.assertEqual(self.run_javascript(f"s.countUndated({documents}, 'source')"), 1)

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

    def run_range(self, documents, start, end):
        script = f"const s=require({json.dumps(str(SEARCH_JS))}); process.stdout.write(JSON.stringify(s.recentDocumentsInRange({json.dumps(documents, ensure_ascii=False)}, {json.dumps(start)}, {json.dumps(end)})));"
        result = subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True)
        return json.loads(result.stdout)

    def test_recent_documents_in_range_filters_by_start_and_end_inclusive(self):
        documents = [
            {"path": "a.md", "date": "2026-09-13"},
            {"path": "b.md", "date": "2026-09-01"},
            {"path": "c.md", "date": "2026-08-31"},
            {"path": "d.md", "date": "2026-09-14"},
        ]
        result = self.run_range(documents, "2026-09-01", "2026-09-13")
        self.assertEqual([d["path"] for d in result], ["a.md", "b.md"])

    def test_recent_documents_in_range_sorts_descending(self):
        documents = [
            {"path": "x.md", "date": "2026-09-01"},
            {"path": "y.md", "date": "2026-09-13"},
        ]
        result = self.run_range(documents, "2026-01-01", "2026-12-31")
        self.assertEqual([d["path"] for d in result], ["y.md", "x.md"])

    def test_recent_documents_in_range_with_blank_start_has_no_lower_bound(self):
        documents = [
            {"path": "a.md", "date": "2026-01-01"},
            {"path": "b.md", "date": "2026-09-13"},
        ]
        result = self.run_range(documents, "", "2026-12-31")
        self.assertEqual([d["path"] for d in result], ["b.md", "a.md"])

    def test_recent_documents_in_range_with_blank_end_has_no_upper_bound(self):
        documents = [
            {"path": "a.md", "date": "2026-01-01"},
            {"path": "b.md", "date": "2026-09-13"},
        ]
        result = self.run_range(documents, "2026-01-01", "")
        self.assertEqual([d["path"] for d in result], ["b.md", "a.md"])

    def test_recent_documents_in_range_excludes_undated_documents(self):
        documents = [{"path": "a.md", "date": None}, {"path": "b.md", "date": "2026-09-13"}]
        result = self.run_range(documents, "", "")
        self.assertEqual([d["path"] for d in result], ["b.md"])


if __name__ == "__main__":
    unittest.main()


class PageScriptTests(unittest.TestCase):
    def render(self, data, body=""):
        script = f"const p=require({json.dumps(str(PAGE_JS))}); process.stdout.write(p.pageHtml({json.dumps(data, ensure_ascii=False)}, {json.dumps(body, ensure_ascii=False)}));"
        return subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True).stdout

    BASE = {"kind": "wiki", "title": "标题", "path": "wiki/a.md", "root": "../", "tags": [], "crumbs": [], "props": [], "backlinks": [], "toc": [], "sourceNotes": [], "externalLinks": [], "sourceWiki": []}

    def test_unsafe_links_in_the_data_are_dropped(self):
        html = self.render({**self.BASE,
                            "crumbs": [{"text": "坏", "href": "javascript:alert(1)"}, {"text": "好", "href": "../index.html"}],
                            "externalLinks": ["javascript:alert(2)", "data:text/html,x", "https://example.com/a"],
                            "backlinks": [{"title": "x", "href": "//evil.example/"}]})
        self.assertNotIn("javascript:", html)
        self.assertNotIn("data:text", html)
        self.assertNotIn("//evil.example", html)
        self.assertIn('href="../index.html"', html)
        self.assertEqual(html.count("打开来源链接"), 1)
        self.assertIn('href="https://example.com/a" rel="noreferrer" target="_blank"', html)

    def test_page_urls_are_built_from_the_relative_root(self):
        html = self.render(self.BASE)
        self.assertIn('data-index="../assets/search-index.json"', html)
        self.assertIn('data-manifest="../manifest.json"', html)
        self.assertIn('data-auth-me="../auth/me"', html)
        self.assertIn('data-current="wiki/a.md"', html)

    def test_contents_list_and_source_panel_appear_only_when_there_is_something_in_them(self):
        self.assertNotIn('class="toc"', self.render(self.BASE))
        html = self.render({**self.BASE, "toc": [{"level": "2", "anchor": "s", "text": "小节"}]})
        self.assertIn('<aside class="toc"><h3>本页目录</h3><a class="toc-2" href="#s">小节</a></aside>', html)

    def test_the_home_kind_gets_the_wide_layout_and_no_properties(self):
        html = self.render({**self.BASE, "kind": "home", "title": "DEK 知识库"}, '<div id="home-app"></div>')
        self.assertIn('<main class="document home-page">', html)
        self.assertIn('<article><div id="home-app"></div></article>', html)
        self.assertNotIn("note-properties", html)

    def test_only_the_home_page_goes_without_a_kind_chip(self):
        home = self.render({**self.BASE, "kind": "home", "title": "DEK 知识库", "crumbs": [{"text": "首页"}]})
        self.assertNotIn("HOME", home)
        self.assertNotIn('class="kind"', home)
        self.assertIn('<div class="breadcrumbs">首页</div><h1>DEK 知识库</h1>', home)
        for kind in ("wiki", "source"):
            with self.subTest(kind=kind):
                self.assertIn(f'<span class="kind">{kind.upper()}</span>', self.render({**self.BASE, "kind": kind}))
