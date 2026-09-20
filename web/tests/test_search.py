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

    def test_page_window_shows_2_pages_each_side_of_the_current_one_with_first_and_last(self):
        window = lambda page, pages: self.run_javascript(f"s.pageWindow({page},{pages})")
        self.assertEqual(window(1, 78), [1, 2, 3, None, 78])
        self.assertEqual(window(40, 78), [1, None, 38, 39, 40, 41, 42, None, 78])
        self.assertEqual(window(78, 78), [1, None, 76, 77, 78])
        self.assertEqual(window(3, 78), [1, 2, 3, 4, 5, None, 78])              # the gap closes when the run reaches page 1
        self.assertEqual(window(4, 78), [1, 2, 3, 4, 5, 6, None, 78])
        self.assertEqual(window(1, 1), [1])
        self.assertEqual(window(3, 5), [1, 2, 3, 4, 5])
        self.assertEqual(window(2, 9), [1, 2, 3, 4, None, 9])

    def test_page_window_rules_hold_for_every_page_of_every_list_up_to_120_pages(self):
        script = ("(()=>{const out=[];for(let pages=1;pages<=120;pages++)for(let page=1;page<=pages;page++)out.push([pages,page,s.pageWindow(page,pages)]);return out})()")
        for pages, page, window in self.run_javascript(script):
            numbers = [n for n in window if n is not None]
            self.assertEqual(numbers, sorted(set(numbers)), (pages, page))                       # ascending, no repeats
            self.assertTrue({1, pages, page} <= set(numbers), (pages, page))                    # first, last and current always there
            self.assertTrue(set(range(max(1, page - 2), min(pages, page + 2) + 1)) <= set(numbers), (pages, page))
            self.assertLessEqual(len(numbers), 7, (pages, page))                                # 5 in the run + first + last
            for left, right in zip(window, window[1:]):
                self.assertFalse(left is None and right is None, (pages, page))                 # no doubled gaps
            for index, item in enumerate(window):
                if item is None:
                    self.assertGreater(window[index + 1] - window[index - 1], 1, (pages, page))  # a gap only where pages are skipped
                elif index and window[index - 1] is not None:
                    self.assertEqual(item - window[index - 1], 1, (pages, page))                # no skipped page without a gap mark

    def test_pager_has_first_and_last_buttons_and_the_window(self):
        html = self.run_javascript("s.pagerHtml(40, 78)")
        self.assertTrue(html.startswith('<nav class="pager" aria-label="分页"><a href="#" data-page="1">首页</a><a href="#" data-page="39">上一页</a>'), html)
        self.assertIn('<span class="gap">…</span><a href="#" data-page="38">38</a>', html)
        self.assertIn('<span class="current" aria-current="page">40</span>', html)
        self.assertIn('<a href="#" data-page="42">42</a><span class="gap">…</span><a href="#" data-page="78">78</a><a href="#" data-page="41">下一页</a><a href="#" data-page="78">末页</a>', html)
        self.assertTrue(html.endswith('<span class="page-info">第 40/78 页</span></nav>'))
        first = self.run_javascript("s.pagerHtml(1, 78)")
        self.assertIn('<span class="disabled">首页</span><span class="disabled">上一页</span>', first)
        last = self.run_javascript("s.pagerHtml(78, 78)")
        self.assertIn('<span class="disabled">下一页</span><span class="disabled">末页</span>', last)
        self.assertEqual(self.run_javascript("s.pagerHtml(1, 1)").count("disabled"), 4)

    def test_jump_form_takes_a_page_number_up_to_the_last_page(self):
        html = self.run_javascript("s.pageJumpHtml(78)")
        self.assertEqual(html, '<form class="page-jump" novalidate>跳转到第<input type="number" name="page_jump" min="1" max="78" step="1" inputmode="numeric" aria-label="跳转到页码">页<button type="submit">跳转</button></form>')

    def test_list_state_round_trips_through_the_address_bar_and_leaves_out_defaults(self):
        to = lambda state: self.run_javascript(f"s.listStateToQuery({json.dumps(state)})")
        self.assertEqual(to({"page": 1, "range": "0", "size": 15}), "")
        self.assertEqual(to({"page": 40, "range": "0", "size": 15}), "?page=40")
        self.assertEqual(to({"page": 3, "range": "90", "size": 40}), "?page=3&range=90&size=40")
        self.assertEqual(to({"page": 2, "range": "undated", "size": 15}), "?page=2&range=undated")
        self.assertEqual(to({"page": 1, "range": "custom", "start": "2025-01-01", "end": "2025-12-31", "size": 15}), "?range=custom&start=2025-01-01&end=2025-12-31")
        self.assertEqual(to({"page": 1, "range": "90", "start": "2025-01-01", "end": "2025-12-31", "size": 15}), "?range=90")   # dates only matter for custom
        for state in ({"page": 40, "range": "0", "size": 15}, {"page": 3, "range": "90", "size": 40}, {"page": 2, "range": "undated", "size": 15},
                      {"page": 5, "range": "custom", "start": "2025-01-01", "end": "", "size": 25}):
            with self.subTest(state=state):
                back = self.run_javascript(f"s.listStateFromQuery(s.listStateToQuery({json.dumps(state)}))")
                self.assertEqual((back["page"], back["range"], back["size"]), (state["page"], state["range"], state["size"]))
                self.assertEqual((back["start"], back["end"]), (state.get("start", ""), state.get("end", "")))

    def test_the_chosen_category_travels_in_the_address_bar_and_only_a_wiki_or_source_path_is_believed(self):
        to = lambda state: self.run_javascript(f"s.listStateToQuery({json.dumps(state)})")
        read = lambda query: self.run_javascript(f"s.listStateFromQuery({json.dumps(query)})")
        query = to({"page": 2, "range": "90", "category": "wiki/03_药品核查", "size": 15})
        self.assertEqual(query, "?page=2&range=90&category=wiki%2F03_%E8%8D%AF%E5%93%81%E6%A0%B8%E6%9F%A5")
        self.assertEqual(read(query)["category"], "wiki/03_药品核查")
        self.assertEqual(read("?category=source%2FCPC")["category"], "source/CPC")
        self.assertEqual(read("?category=wiki")["category"], "wiki")
        for bad in ("evil", "wikipedia", "../wiki/x", "javascript:alert(1)", ""):
            with self.subTest(bad=bad):
                self.assertEqual(read("?category=" + bad)["category"], "")
        self.assertEqual(to({"page": 1, "range": "0", "category": "", "size": 15}), "")

    def test_a_document_is_in_a_category_when_its_path_is_or_is_under_it(self):
        inside = lambda path, category: self.run_javascript(f"s.inCategory({{path:{json.dumps(path)}}}, {json.dumps(category)})")
        self.assertTrue(inside("wiki/03_药品核查/03-0001.md", "wiki/03_药品核查"))
        self.assertTrue(inside("wiki/03_药品核查", "wiki/03_药品核查"))
        self.assertTrue(inside("source/CPC/a/b.md", "source/CPC"))
        self.assertFalse(inside("wiki/03_药品核查x/a.md", "wiki/03_药品核查"))     # a sibling with the same start is not inside
        self.assertFalse(inside("source/CPC/a.md", "wiki/03_药品核查"))
        self.assertTrue(inside("wiki/a.md", ""))                                    # no category chosen: everything

    def test_a_damaged_address_bar_falls_back_to_the_defaults(self):
        read = lambda query: self.run_javascript(f"s.listStateFromQuery({json.dumps(query)})")
        self.assertEqual(read(""), {"category": "", "range": "0", "start": "", "end": "", "page": 1, "size": 15})
        self.assertEqual(read("?page=-3&range=zzz&size=abc&start=x&end=y"), {"category": "", "range": "0", "start": "", "end": "", "page": 1, "size": 15})
        self.assertEqual(read("?page=abc"), {"category": "", "range": "0", "start": "", "end": "", "page": 1, "size": 15})
        self.assertEqual(read("?range=custom&start=2025-1-1&end=2025-12-31")["start"], "")       # not a date
        self.assertEqual(read("?range=custom&end=2025-12-31")["end"], "2025-12-31")
        self.assertEqual(read("?range=90&start=2025-01-01")["start"], "")                        # dates are only read for custom
        self.assertEqual(read("?size=999")["size"], 100)
        self.assertEqual(read("?size=2")["size"], 5)
        self.assertEqual(read("?page=7&extra=1")["page"], 7)

    def test_page_size_defaults_to_15_and_is_held_to_5_through_100(self):
        self.assertEqual(self.run_javascript("s.PAGE_SIZE"), 15)
        for value, expected in (("", 15), ("abc", 15), ("3", 5), ("5", 5), ("40", 40), ("100", 100), ("999", 100), (" 20 ", 20)):
            with self.subTest(value=value):
                self.assertEqual(self.run_javascript(f"s.clampPageSize({json.dumps(value)})"), expected)
        box = self.run_javascript("s.pageSizeHtml(15)")
        self.assertIn('<form class="page-size">每页<input type="number" name="page_size" min="5" max="100" step="1" value="15"', box)
        self.assertTrue(box.endswith("条</form>"))

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

    PROPS = [{"label": "编号", "value": {"parts": [{"t": "1"}]}}, {"label": "笔记路径", "code": "wiki/a.md"}]

    def test_unsafe_links_in_the_data_are_dropped(self):
        html = self.render({**self.BASE, "props": self.PROPS,
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

    def test_there_is_no_right_hand_panel_whatever_the_data_holds(self):
        full = {**self.BASE, "props": self.PROPS, "toc": [{"level": "2", "anchor": "s", "text": "小节"}],
                "sourceNotes": [{"title": "n", "href": "n.html"}], "sourceWiki": [{"title": "w", "href": "w.html"}],
                "externalLinks": ["https://example.com/a"], "kind": "source"}
        html = self.render(full)
        for gone in ('class="toc"', "<aside class=\"toc", "本页目录", "来源笔记", "引用此来源的 Wiki"):
            self.assertNotIn(gone, html)
        self.assertTrue(html.rstrip().endswith("</main>"))               # the page ends with the article and 反向链接

    def test_the_source_link_is_a_row_of_the_note_information_before_the_path(self):
        html = self.render({**self.BASE, "props": self.PROPS, "externalLinks": ["https://example.com/a", "https://example.com/b"]})
        panel = html.split('<details class="note-properties"', 1)[1].split("</details>", 1)[0]
        self.assertEqual(panel.count("打开来源链接"), 2)
        self.assertLess(panel.index("来源链接</dt>"), panel.index("笔记路径</dt>"))
        self.assertNotIn("来源链接</dt>", self.render({**self.BASE, "props": self.PROPS}))     # no link, no row

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
