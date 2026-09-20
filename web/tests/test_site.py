import re
import subprocess
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from web.site import build_site, is_publishable_path
from web.tests.pagehelper import rendered_page


class SiteBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.out = Path(self.tmp.name) / "site"
        (self.vault / "wiki" / "01_注册").mkdir(parents=True)
        (self.vault / "source" / "CDE").mkdir(parents=True)
        (self.vault / "_raw").mkdir()
        (self.vault / "wiki" / "01_注册" / "条目.md").write_text(
            "---\nno: 1\ndate: 2026-09-10\nquestion: 申报要求\ntags:\n  - 注册/受理\ntag_pages:\n  - '[[wiki/01_注册/条目]]'\nsource: '[[source/CDE/来源]]'\n---\n\n# 申报要求\n\n参见 [[source/CDE/来源|来源资料]]。",
            encoding="utf-8",
        )
        (self.vault / "source" / "CDE" / "来源.md").write_text(
            "---\nsource_name: CDE培训\nsource_type: 培训资料\nsource_url: https://example.com/source\n---\n\n## 内容\n\n来源正文。",
            encoding="utf-8",
        )
        (self.vault / "wiki" / "排除资料.md").write_text("secret", encoding="utf-8")
        (self.vault / "_raw" / "secret.md").write_text("secret", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_wiki_and_source_without_excluded_paths_are_published(self):
        build_site(self.vault, self.out)
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({d["path"] for d in manifest["documents"]}, {"wiki/01_注册/条目.md", "source/CDE/来源.md"})
        self.assertFalse(is_publishable_path(Path("_raw/secret.md")))
        self.assertFalse(is_publishable_path(Path("wiki/排除资料.md")))

    def test_wikilinks_and_backlinks_are_rendered(self):
        build_site(self.vault, self.out)
        wiki = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))
        source = rendered_page((self.out / "source" / "CDE" / "来源.html"))
        self.assertIn('../../source/CDE/' + quote('来源.html'), wiki)
        self.assertIn('来源资料', wiki)
        self.assertIn('反向链接', source)
        self.assertIn('../../wiki/' + quote('01_注册/条目.html', safe='/'), source)

    def test_dataview_overview_block_renders_as_a_real_table_not_raw_query_text(self):
        (self.vault / "wiki" / "01_注册" / "0102_分类").mkdir(parents=True)
        (self.vault / "wiki" / "01_注册" / "0102_分类" / "条目2.md").write_text(
            "---\nno: 2\ndate: 2018-06-14\nquestion: 另一个问题\nsource: 培训材料\n---\n\n正文2。",
            encoding="utf-8",
        )
        (self.vault / "wiki" / "01_注册" / "无编号.md").write_text(
            "---\nquestion: 没有编号不应出现\n---\n\n正文3。", encoding="utf-8",
        )
        (self.vault / "wiki" / "01_注册" / "01_注册.md").write_text(
            "---\naliases:\n  - 概览\n---\n\n"
            '```dataview\n'
            'TABLE WITHOUT ID\n'
            '  file.link AS 项目,\n'
            '  question AS 问题,\n'
            '  source AS 来源,\n'
            '  dateformat(date, "yyyy-MM-dd") AS 日期\n'
            'FROM "wiki/01_注册"\n'
            'WHERE no != null\n'
            'SORT file.folder ASC, no ASC\n'
            '```\n',
            encoding="utf-8",
        )
        build_site(self.vault, self.out)
        page = (self.out / "wiki" / "01_注册" / "01_注册.html").read_text(encoding="utf-8")
        self.assertNotIn("TABLE WITHOUT ID", page)
        self.assertNotIn("```dataview", page)
        self.assertIn('<div class="table-wrap"><table>', page)
        self.assertIn("申报要求", page)
        self.assertIn("另一个问题", page)
        self.assertIn("培训材料", page)
        self.assertIn("2018-06-14", page)
        self.assertNotIn("没有编号不应出现", page)

    def test_dataview_block_with_a_different_query_shape_is_left_untouched(self):
        (self.vault / "source" / "Index.md").write_text(
            "```dataview\nTABLE WITHOUT ID\n  file.link AS \"题目\"\nFROM \"source\"\nWHERE entity != null\n```\n",
            encoding="utf-8",
        )
        build_site(self.vault, self.out)
        page = (self.out / "source" / "Index.html").read_text(encoding="utf-8")
        self.assertIn("TABLE WITHOUT ID", page)

    def test_search_index_contains_both_collections_and_no_frontmatter(self):
        build_site(self.vault, self.out)
        search = json.loads((self.out / "assets" / "search-index.json").read_text(encoding="utf-8"))
        self.assertEqual({d["kind"] for d in search}, {"wiki", "source"})
        self.assertNotIn("source_url:", json.dumps(search, ensure_ascii=False))

    def test_layout_has_tree_content_toc_theme_and_search(self):
        build_site(self.vault, self.out)
        page = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))
        for marker in ('class="sidebar"', 'class="document"', 'class="toc"', 'id="theme-toggle"', 'id="global-search"'):
            self.assertIn(marker, page)
        self.assertTrue((self.out / "assets" / "style.css").is_file())
        self.assertTrue((self.out / "assets" / "app.js").is_file())
        self.assertTrue((self.out / "assets" / "search.js").is_file())

    def test_every_page_exposes_the_main_origin_review_entry(self):
        build_site(self.vault, self.out)
        pages = (
            self.out / "index.html",
            self.out / "wiki" / "01_注册" / "条目.html",
            self.out / "source" / "CDE" / "来源.html",
        )
        for page in pages:
            with self.subTest(page=page):
                rendered = rendered_page(page)
                self.assertIn('href="/review/"', rendered)
                self.assertIn("知识审核", rendered)

    def test_search_links_are_relative_to_site_assets_for_subpath_deployment(self):
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("location.origin+'/'", script)
        self.assertIn("new URL(input.dataset.index, location.href)", script)

    def test_search_has_submit_button_enter_support_and_result_summaries(self):
        build_site(self.vault, self.out)

        page = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="search-button"', page)
        self.assertIn('type="search"', page)
        self.assertIn('event.key === "Enter"', script)
        self.assertIn("DEKSearch.resultSnippet", script)
        self.assertIn('class="result-snippet"', script)

    def test_search_reports_index_loading_failure_and_retry_states(self):
        build_site(self.vault, self.out)

        page = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="search-status"', page)
        self.assertIn("正在加载搜索索引", script)
        self.assertIn("搜索索引加载失败", script)
        self.assertIn("重试", script)
        self.assertIn("DEKSearch.resultUrl", script)

    def home_markup(self):
        """The home page body as the installed scripts build it from this release's manifest.json."""
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        script = (f"const s=require({json.dumps(str(self.out / 'assets' / 'search.js'))});"
                  f"process.stdout.write(s.homeHtml({json.dumps(manifest['tree'], ensure_ascii=False)}, {{}}));")
        return subprocess.run(["node", "-e", script], check=True, text=True, capture_output=True).stdout

    def test_a_published_page_is_only_content_plus_data_the_scripts_draw_around(self):
        build_site(self.vault, self.out)
        raw = (self.out / "wiki" / "01_注册" / "条目.html").read_text(encoding="utf-8")
        for drawn_by_script in ("<header>", 'class="sidebar"', 'class="breadcrumbs"', 'class="backlinks"', 'id="theme-toggle"'):
            self.assertNotIn(drawn_by_script, raw)
        self.assertIn('id="page-data"', raw)
        self.assertIn('<template id="page-body">', raw)
        head = raw.split("</head>", 1)[0]
        self.assertLess(head.index("assets/search.js"), head.index("assets/page.js"))   # page.js draws the frame ...
        self.assertLess(head.index("assets/page.js"), head.index("assets/app.js"))      # ... before app.js wires it up
        seen = rendered_page(self.out / "wiki" / "01_注册" / "条目.html")
        for drawn in ("<header>", 'class="sidebar"', 'class="breadcrumbs"', 'class="backlinks"', 'id="theme-toggle"'):
            self.assertIn(drawn, seen)
        self.assertTrue((self.out / "assets" / "page.js").is_file())

    def test_metadata_cannot_break_out_of_the_page_data_block(self):
        (self.vault / "wiki" / "01_注册" / "恶意.md").write_text(
            '---\nno: 9\ndate: 2026-01-01\nquestion: "</script><script>alert(1)</script>"\n'
            'source: "<img src=x onerror=alert(2)>"\ntags:\n  - "</script><b>x"\n---\n\n正文。', encoding="utf-8")
        build_site(self.vault, self.out)
        raw = (self.out / "wiki" / "01_注册" / "恶意.html").read_text(encoding="utf-8")
        data_block = raw.split('id="page-data">', 1)[1].split("</script>", 1)[0]
        self.assertNotIn("<", data_block)                       # only one </script> in the whole page, the block's own
        self.assertEqual(raw.count("</script>"), 4)             # three scripts in <head> + the data block
        seen = rendered_page(self.out / "wiki" / "01_注册" / "恶意.html")
        self.assertNotIn("<script>alert", seen)
        self.assertNotIn("<img src=x", seen)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", seen)

    def test_homepage_is_a_stable_library_landing_page_not_a_document_redirect(self):
        build_site(self.vault, self.out)

        homepage = (self.out / "index.html").read_text(encoding="utf-8")

        self.assertNotIn("http-equiv=\"refresh\"", homepage)
        self.assertIn("DEK 知识库", homepage)
        markup = self.home_markup()
        self.assertIn("Wiki · 正式知识", markup)
        self.assertIn("Source · 来源材料", markup)

    def test_the_built_home_page_is_only_a_shell_the_scripts_fill_in(self):
        # Filters and category cards come from installed scripts + manifest.json, so a
        # change to them is a deploy, not a content release.
        build_site(self.vault, self.out)
        homepage = (self.out / "index.html").read_text(encoding="utf-8")
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('<div id="home-app" data-manifest="manifest.json" data-index="assets/search-index.json">', homepage)
        for built_by_script in ("folder-card", "recent-tab", "recent-filters", "recent-list", "section-empty"):
            self.assertNotIn(f'class="{built_by_script}', homepage)
        self.assertIn("DEKSearch.homeHtml(tree", script)
        self.assertIn("首页加载失败", script)   # a failed manifest fetch offers a retry, never a blank page

    def test_manifest_contains_nested_directory_tree(self):
        build_site(self.vault, self.out)

        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        wiki = next(node for node in manifest["tree"] if node["path"] == "wiki")
        registration = next(node for node in wiki["children"] if node["path"] == "wiki/01_注册")

        self.assertEqual(registration["type"], "directory")
        self.assertEqual(registration["count"], 1)
        self.assertEqual(registration["children"][0]["path"], "wiki/01_注册/条目.md")
        self.assertEqual(registration["children"][0]["type"], "document")

    def test_tree_labels_numbered_leaf_notes_with_their_code_but_not_named_notes(self):
        (self.vault / "wiki" / "07_非临床研究").mkdir(parents=True)
        (self.vault / "wiki" / "07_非临床研究" / "07-0001.md").write_text(
            "---\nno: 1\nquestion: 非临床样品的要求？\n---\n\n正文。", encoding="utf-8",
        )
        build_site(self.vault, self.out)
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        wiki = next(node for node in manifest["tree"] if node["path"] == "wiki")
        nonclinical = next(node for node in wiki["children"] if node["path"] == "wiki/07_非临床研究")
        leaf = next(child for child in nonclinical["children"] if child["path"] == "wiki/07_非临床研究/07-0001.md")
        self.assertEqual(leaf["name"], "07-0001 非临床样品的要求？")
        # A non-numbered filename (like the existing 条目.md fixture) keeps its
        # plain title, since it has no code worth surfacing.
        registration = next(node for node in wiki["children"] if node["path"] == "wiki/01_注册")
        self.assertEqual(registration["children"][0]["name"], "申报要求")

    def test_homepage_lists_top_level_directories(self):
        build_site(self.vault, self.out)

        homepage = self.home_markup()

        self.assertIn("01_注册", homepage)
        self.assertIn("CDE", homepage)

    def test_navigation_script_renders_recursive_accessible_tree(self):
        build_site(self.vault, self.out)

        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn('role="tree"', script)
        self.assertIn('role="group"', script)
        self.assertIn("renderNode", script)
        self.assertIn("dek-tree-open", script)

    def test_wiki_page_renders_obsidian_note_properties(self):
        build_site(self.vault, self.out)

        wiki = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))

        self.assertIn("笔记信息", wiki)
        self.assertIn("编号", wiki)
        self.assertIn(">1<", wiki)
        self.assertIn("日期", wiki)
        self.assertIn("2026-09-10", wiki)
        self.assertIn("标签页面", wiki)
        # "问题" duplicates the <h1> title directly above the table, and "标签"
        # duplicates "标签页面" (and the badge chips under the title); dropping
        # both keeps the property table from pushing the article below the fold.
        self.assertNotIn("<dt>问题</dt>", wiki)
        self.assertNotIn("<dt>标签</dt>", wiki)

    def test_breadcrumb_segments_link_to_their_folder_overview_note(self):
        (self.vault / "wiki" / "01_注册" / "01_注册.md").write_text(
            "---\nsource_name: 分类总览\n---\n\n# 01_注册", encoding="utf-8",
        )
        build_site(self.vault, self.out)
        wiki = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))
        self.assertIn('<div class="breadcrumbs">', wiki)
        self.assertIn('<a href="../../index.html">wiki</a>', wiki)
        self.assertIn('<a href="01_%E6%B3%A8%E5%86%8C.html">01_注册</a>', wiki)
        # The leaf segment is the current page itself and stays plain text.
        self.assertNotIn('<a href="%E6%9D%A1%E7%9B%AE.html">条目</a>', wiki)

    def test_breadcrumb_segment_without_an_overview_note_stays_plain_text(self):
        build_site(self.vault, self.out)
        wiki = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))
        self.assertIn('<div class="breadcrumbs">', wiki)
        self.assertIn("01_注册", wiki)
        self.assertNotIn('<a href="01_注册.html">01_注册</a>', wiki)

    def test_tree_node_title_is_the_full_label_not_the_file_path(self):
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("title=\"${escapeHtml(node.name)}\"", script)
        self.assertNotIn("title=\"${escapeHtml(node.path)}\"", script)

    def test_recent_and_search_share_a_single_index_fetch_with_retry(self):
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("loadSharedIndex", script)
        self.assertEqual(script.count("fetch(url,"), 1)
        self.assertEqual(script.count("fetch(indexUrl"), 0)
        self.assertIn("recent-retry", script)

    def test_explicit_source_wikilink_is_clickable_and_source_lists_referring_wiki(self):
        build_site(self.vault, self.out)

        wiki = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))
        source = rendered_page((self.out / "source" / "CDE" / "来源.html"))

        self.assertIn("来源笔记", wiki)
        self.assertIn('../../source/CDE/' + quote('来源.html'), wiki)
        self.assertIn("引用此来源的 Wiki", source)
        self.assertIn('../../wiki/' + quote('01_注册/条目.html', safe='/'), source)

    def test_page_includes_authenticated_name_and_logout_controls(self):
        build_site(self.vault, self.out)

        wiki = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="user-name"', wiki)
        self.assertIn('id="user-name">正在读取…', wiki)
        self.assertNotIn('id="user-name">同事', wiki)
        self.assertIn("auth/me", wiki)
        self.assertIn("auth/logout", wiki)
        self.assertIn("dataset.authMe", script)
        self.assertNotIn('data.display_name || "同事"', script)

    def test_search_index_includes_publication_date(self):
        build_site(self.vault, self.out)
        search = json.loads((self.out / "assets" / "search-index.json").read_text(encoding="utf-8"))
        wiki = next(d for d in search if d["path"] == "wiki/01_注册/条目.md")
        self.assertEqual(wiki["date"], "2026-09-10")

    def test_search_index_marks_missing_date_as_none(self):
        (self.vault / "wiki" / "01_注册" / "无日期.md").write_text(
            "---\nno: 2\nquestion: 无日期\n---\n\n正文。", encoding="utf-8"
        )
        build_site(self.vault, self.out)
        search = json.loads((self.out / "assets" / "search-index.json").read_text(encoding="utf-8"))
        missing = next(d for d in search if d["path"].endswith("无日期.md"))
        source = next(d for d in search if d["path"] == "source/CDE/来源.md")
        self.assertIsNone(missing["date"])
        self.assertIsNone(source["date"])

    def test_homepage_has_recent_section_with_day_filters(self):
        build_site(self.vault, self.out)
        homepage = self.home_markup()
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        for marker in ("信息速览", "7天", "30天", "90天", "全部"):
            self.assertIn(marker, homepage)
        self.assertIn("recentDocuments", script)

    def test_homepage_recent_filters_offer_start_and_end_date_inputs(self):
        build_site(self.vault, self.out)
        homepage = self.home_markup()
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="recent-start"', homepage)
        self.assertIn('id="recent-end"', homepage)
        self.assertIn('type="date"', homepage)
        self.assertIn("recentDocumentsInRange", script)

    def add_big_category(self):
        """A category holding most of the wiki, with sub-folders of unequal size."""
        for folder, count in (("1601_持有人变更", 1), ("1619_生产场地变更", 4), ("1620_变更资料要求", 3), ("1621_过渡期", 2), ("1622_其他", 1)):
            directory = self.vault / "wiki" / "16_注册变更" / folder
            directory.mkdir(parents=True)
            for number in range(count):
                (directory / f"{folder[:4]}-{number + 1:04d}.md").write_text(
                    f"---\nno: {number + 1}\ndate: 2026-0{number + 1}-01\nquestion: 问题{folder}{number}\n---\n\n正文。", encoding="utf-8")

    def test_folder_cards_carry_totals_the_page_script_can_recompute_for_a_date_range(self):
        build_site(self.vault, self.out)
        homepage = self.home_markup()
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertRegex(homepage, r'<span class="card-count" data-count-path="wiki/01_注册" data-total="\d+">共 \d+ 篇</span>')
        self.assertRegex(homepage, r'<span class="section-count" data-count-path="wiki" data-total="\d+">\d+</span>')
        self.assertIn("countInRange", script)
        self.assertIn('id="recent-summary"', homepage)

    def test_a_large_category_is_still_a_single_plain_card(self):
        self.add_big_category()
        build_site(self.vault, self.out)
        homepage = self.home_markup()
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        for gone in ("has-subs", "card-sub", "card-subs"):
            self.assertNotIn(gone, homepage)
        self.assertNotIn("card-sub-", script)
        cards = re.findall(r'<div class="folder-card">(.*?)</div>', homepage, re.S)
        big = next(card for card in cards if "16_注册变更" in card)
        # An older release's page still carries the nested lists while the scripts are already new.
        self.assertIn('document.querySelectorAll(".card-subs").forEach(list => list.remove())', script)
        self.assertEqual(big.count("<a "), 1)              # one link, no nested sub-folder links
        self.assertIn('data-total="11"', big)             # its count is the whole category

    def test_every_card_link_has_the_full_name_as_a_hover_title(self):
        build_site(self.vault, self.out)
        homepage = self.home_markup()
        self.assertRegex(homepage, r'<a class="folder-card-link" href="[^"]+" title="01_注册">')

    def test_custom_dates_are_collapsed_behind_a_button_and_the_presets_stay(self):
        build_site(self.vault, self.out)
        homepage = self.home_markup()
        self.assertRegex(homepage, r'<div class="recent-range" id="recent-range" hidden>')
        self.assertIn('id="recent-custom"', homepage)
        self.assertEqual(re.findall(r'data-days="(\w+)"', homepage), ["7", "30", "90", "undated", "0"])   # 无日期 sits with the periods

    def test_the_home_page_lands_on_all_with_every_card_showing_its_total(self):
        build_site(self.vault, self.out)
        homepage = self.home_markup()
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertRegex(homepage, r'<button type="button" class="recent-tab active" data-days="0">全部</button>')
        self.assertEqual(homepage.count("recent-tab active"), 1)
        self.assertIn("applyDays(0)", script)
        self.assertNotIn("applyDays(7)", script)
        self.assertNotIn("is-empty", script)

    def test_a_period_hides_empty_categories_and_each_section_has_an_empty_message(self):
        build_site(self.vault, self.out)
        homepage = self.home_markup()
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        style = (self.out / "assets" / "style.css").read_text(encoding="utf-8")
        self.assertEqual(homepage.count('<p class="section-empty" hidden>该时段内暂无新增内容</p>'), 2)   # Wiki and Source
        self.assertIn("box.hidden = filtered && shown === 0", script)
        self.assertIn("`${shown} 篇`", script)                    # a period shows its own count only
        self.assertNotIn("· 共", script)                          # ... never the total next to it
        self.assertNotIn("opacity:.45", style)                    # empty cards are hidden, not dimmed
        self.assertIn(".folder-card[hidden]", style)              # display:flex would otherwise defeat `hidden`

    def test_全部_lists_everything_and_无日期_lists_only_what_has_no_date(self):
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('...DEKSearch.undatedDocuments(recentDocs)]', script)        # 全部 = dated newest first, then undated
        self.assertIn("undatedOnly ? DEKSearch.undatedDocuments(recentDocs)", script)
        self.assertIn('days === "undated"', script)
        self.assertIn("countUndated", script)                                      # cards count only the undated ones there
        self.assertIn("没有无日期的内容", script)
        self.assertIn("篇无日期，排在列表最后", script)   # the note under the filters says how many, without guessing what they are
        self.assertNotIn("如目录页", script)

    def test_the_home_section_is_called_信息速览(self):
        build_site(self.vault, self.out)
        homepage = self.home_markup()
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("<h2>信息速览</h2>", homepage)
        self.assertNotIn("最近信息", homepage + script + (self.out / "assets" / "search.js").read_text(encoding="utf-8"))
        self.assertIn("正在加载信息速览…", homepage)
        self.assertIn("信息速览加载失败", script)

    def test_dates_are_labelled_as_publication_dates(self):
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("发布于 ${doc.date}", script)
        self.assertIn('"无日期"', script)
        self.assertNotIn("日期待确认", script)

    def test_homepage_recent_results_are_the_last_section_but_filters_stay_up_top(self):
        build_site(self.vault, self.out)
        homepage = self.home_markup()
        filters_at = homepage.index('id="recent-start"')
        first_folder_section_at = homepage.index('class="home-section"')
        results_list_at = homepage.index('id="recent-list"')
        last_folder_section_at = homepage.rindex('class="home-section"')
        self.assertLess(filters_at, first_folder_section_at)
        self.assertGreater(results_list_at, last_folder_section_at)

    def test_homepage_has_no_intro_paragraph_or_note_properties(self):
        build_site(self.vault, self.out)
        homepage = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("home-intro", homepage)
        self.assertNotIn("笔记信息", homepage)
        self.assertNotIn("笔记路径", homepage)
        self.assertIn("DEK 知识库", homepage)

    def test_wiki_page_still_has_note_properties(self):
        # Only the homepage drops the properties panel; regular notes keep it.
        build_site(self.vault, self.out)
        wiki = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))
        self.assertIn("笔记信息", wiki)

    def test_page_without_headings_omits_the_empty_table_of_contents(self):
        (self.vault / "wiki" / "01_注册" / "无小节.md").write_text(
            "---\nno: 3\nquestion: 无小节问答\n---\n\n没有二级标题的问答正文。", encoding="utf-8",
        )
        build_site(self.vault, self.out)
        page = (self.out / "wiki" / "01_注册" / "无小节.html").read_text(encoding="utf-8")
        self.assertNotIn("本页目录", page)

    def test_page_with_headings_still_shows_the_table_of_contents(self):
        # _toc() only surfaces h2-h4 (the document's own h1 title is excluded),
        # so this needs a real subsection heading, unlike the plain-body fixture.
        (self.vault / "wiki" / "01_注册" / "带小节.md").write_text(
            "---\nno: 4\nquestion: 带小节问答\n---\n\n## 第一节\n\n正文。", encoding="utf-8",
        )
        build_site(self.vault, self.out)
        page = rendered_page((self.out / "wiki" / "01_注册" / "带小节.html"))
        self.assertIn('class="toc"', page)
        self.assertIn("本页目录", page)
        self.assertIn("第一节", page)

    def test_sidebar_has_a_drag_resize_handle(self):
        build_site(self.vault, self.out)
        for page_path in (self.out / "index.html", self.out / "wiki" / "01_注册" / "条目.html"):
            with self.subTest(page=page_path):
                page = page_path.read_text(encoding="utf-8")
                # app.js creates the handle outside the scrolling sidebar.
                self.assertNotIn('class="sidebar-resize-handle"', page)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        style = (self.out / "assets" / "style.css").read_text(encoding="utf-8")
        self.assertIn('document.body.appendChild(handle)', script)
        self.assertIn('".sidebar .sidebar-resize-handle"', script)  # legacy in-sidebar handles are removed
        self.assertIn('"dek-sidebar-width"', script)
        self.assertIn("innerWidth * 0.5", script)
        self.assertIn('addEventListener("dblclick"', script)
        self.assertIn("body>.sidebar-resize-handle{position:fixed", style)
        self.assertIn(".sidebar .sidebar-resize-handle{display:none}", style)
        sidebar_rule = style.split(".sidebar{position:fixed", 1)[1].split("}", 1)[0]
        self.assertIn("overflow-x:hidden", sidebar_rule)
        self.assertNotIn("overflow-x:visible", sidebar_rule)

    def test_home_page_main_area_is_widened_but_articles_keep_a_readable_measure(self):
        build_site(self.vault, self.out)
        home = rendered_page((self.out / "index.html"))
        article = rendered_page((self.out / "wiki" / "01_注册" / "条目.html"))
        self.assertIn('<main class="document home-page">', home)
        self.assertNotIn("home-page", article)
        style = (self.out / "assets" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".document.home-page{max-width:1440px;margin-right:0}", style)

    def test_script_marks_the_home_page_so_widening_works_on_already_built_html(self):
        # Frontend assets are served from installed code and update on deploy,
        # but already-published HTML only changes with the next content release.
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('document.querySelector(".recent-filters, #home-app")', script)
        self.assertIn('classList.add("home-page")', script)

    def test_sidebar_has_no_browse_title(self):
        build_site(self.vault, self.out)
        for page_path in (self.out / "index.html", self.out / "wiki" / "01_注册" / "条目.html"):
            with self.subTest(page=page_path):
                page = rendered_page(page_path)
                self.assertNotIn("side-title", page)
                self.assertIn('<aside class="sidebar"><nav id="nav-tree"', page)
        # Pages published before this change still carry the element until the
        # next release rebuilds them, so the shared stylesheet hides it.
        style = (self.out / "assets" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".side-title{display:none}", style)
        self.assertNotIn("letter-spacing:.08em}summary", style)

    def test_scrollbars_are_thin_theme_aware_and_stateful(self):
        build_site(self.vault, self.out)
        css = (self.out / "assets" / "style.css").read_text(encoding="utf-8")
        # color-scheme lives on :root (the dark theme is html[data-theme=dark], also the root).
        self.assertIn(":root{color-scheme:light}", css)
        self.assertIn("html[data-theme=dark]{color-scheme:dark}", css)
        # 10px hit area, 6px visible thumb (2px transparent border), 8px on hover, 40px minimum.
        self.assertIn("::-webkit-scrollbar{width:10px;height:10px}", css)
        self.assertIn("::-webkit-scrollbar-track,::-webkit-scrollbar-corner{background:transparent}", css)
        self.assertIn("background-color:color-mix(in srgb,var(--muted) 38%,transparent);background-clip:padding-box;border:2px solid transparent;border-radius:999px", css)
        self.assertIn("::-webkit-scrollbar-thumb:vertical{min-height:40px}", css)
        self.assertIn("::-webkit-scrollbar-thumb:horizontal{min-width:40px}", css)
        self.assertIn("::-webkit-scrollbar-thumb:hover{background-color:color-mix(in srgb,var(--muted) 65%,transparent);border-width:1px}", css)
        self.assertIn("::-webkit-scrollbar-thumb:active{background-color:var(--accent)}", css)
        # Firefox only: the standard properties would override the WebKit styling in Chrome/Edge.
        self.assertIn("@supports not selector(::-webkit-scrollbar){*{scrollbar-width:thin;scrollbar-color:color-mix(in srgb,var(--muted) 38%,transparent) transparent}}", css)
        outside = css.replace(css[css.index("@supports not selector(::-webkit-scrollbar)"):].split("}}", 1)[0] + "}}", "")
        self.assertNotIn("scrollbar-width", outside)
        self.assertNotIn("scrollbar-color", outside)

    def test_page_length_input_submits_on_change_and_is_clamped(self):
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('document.querySelectorAll("form.page-size")', script)
        self.assertIn('addEventListener("change"', script)
        self.assertIn("Math.min(100, Math.max(5,", script)
        self.assertIn("form.requestSubmit()", script)

    def test_inputs_are_regular_weight_and_share_one_focus_style(self):
        build_site(self.vault, self.out)
        css = (self.out / "assets" / "style.css").read_text(encoding="utf-8")
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("input,textarea{font-weight:400}", css)
        self.assertIn("::placeholder{color:var(--muted);opacity:.7;font-weight:400}", css)
        focus = "{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 20%,transparent)}"
        self.assertIn("input:not([type=hidden]):focus-visible,textarea:focus-visible" + focus, css)
        # The search box's own copy of that rule is gone.
        self.assertNotIn(".search-wrap input:focus{", css)
        self.assertEqual(css.count("box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 20%,transparent)"), 1)

    def test_wiki_path_dropdown_supports_the_keyboard_and_shows_one_line_rows(self):
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        for token in ('"ArrowDown"', '"ArrowUp"', '"Enter"', '"Escape"', 'classList.toggle("active"', "scrollIntoView", "aria-activedescendant"):
            self.assertIn(token, script)
        # Each option is one line with the full path, left aligned.
        self.assertIn('<span class="combo-path">${escapeHtml(path)}</span>', script)
        self.assertNotIn("combo-folder", script)
        self.assertNotIn("combo-file", script)
        # Enter must never fall through to the form's default button (批准).
        self.assertIn('event.key === "Enter"', script)
        self.assertIn("event.preventDefault()", script)

    def test_choosing_a_path_keeps_the_candidate_number_and_tags_in_step(self):
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        css = (self.out / "assets" / "style.css").read_text(encoding="utf-8")
        for token in ("syncCandidateToPath", '".suggestion-chip"', "dataset.path", "candidate_markdown"):
            self.assertIn(token, script)
        # Both the dropdown and the suggestion buttons go through the same step.
        self.assertEqual(script.count("syncCandidateToPath("), 3)

    def test_sidebar_tree_has_a_home_entry(self):
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("home-link", script)

    def test_approve_button_is_guarded_against_an_empty_wiki_path(self):
        # A rough with no wiki_target suggestion of its own (common for
        # genuinely new content ingestion hasn't categorized yet) prefills
        # the wiki-path combobox blank. Clicking 批准 without first picking
        # a folder from the dropdown used to silently 400 server-side
        # ("invalid path", never shown to the reviewer -- the response body
        # deliberately never carries the real reason, to avoid leaking
        # form/nonce internals). Catching this client-side, before the
        # round trip, gives the reviewer an actual actionable message.
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("event.submitter", script)
        self.assertIn('!== "approve"', script)


if __name__ == "__main__":
    unittest.main()
