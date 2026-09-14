import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from web.site import build_site, is_publishable_path


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
        wiki = (self.out / "wiki" / "01_注册" / "条目.html").read_text(encoding="utf-8")
        source = (self.out / "source" / "CDE" / "来源.html").read_text(encoding="utf-8")
        self.assertIn('../../source/CDE/' + quote('来源.html'), wiki)
        self.assertIn('来源资料', wiki)
        self.assertIn('反向链接', source)
        self.assertIn('../../wiki/' + quote('01_注册/条目.html', safe='/'), source)

    def test_search_index_contains_both_collections_and_no_frontmatter(self):
        build_site(self.vault, self.out)
        search = json.loads((self.out / "assets" / "search-index.json").read_text(encoding="utf-8"))
        self.assertEqual({d["kind"] for d in search}, {"wiki", "source"})
        self.assertNotIn("source_url:", json.dumps(search, ensure_ascii=False))

    def test_layout_has_tree_content_toc_theme_and_search(self):
        build_site(self.vault, self.out)
        page = (self.out / "wiki" / "01_注册" / "条目.html").read_text(encoding="utf-8")
        for marker in ('class="sidebar"', 'class="document"', 'class="toc"', 'id="theme-toggle"', 'id="global-search"'):
            self.assertIn(marker, page)
        self.assertTrue((self.out / "assets" / "style.css").is_file())
        self.assertTrue((self.out / "assets" / "app.js").is_file())
        self.assertTrue((self.out / "assets" / "search.js").is_file())

    def test_search_links_are_relative_to_site_assets_for_subpath_deployment(self):
        build_site(self.vault, self.out)
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("location.origin+'/'", script)
        self.assertIn("new URL(input.dataset.index, location.href)", script)

    def test_search_has_submit_button_enter_support_and_result_summaries(self):
        build_site(self.vault, self.out)

        page = (self.out / "wiki" / "01_注册" / "条目.html").read_text(encoding="utf-8")
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="search-button"', page)
        self.assertIn('type="search"', page)
        self.assertIn('event.key === "Enter"', script)
        self.assertIn("DEKSearch.resultSnippet", script)
        self.assertIn('class="result-snippet"', script)

    def test_search_reports_index_loading_failure_and_retry_states(self):
        build_site(self.vault, self.out)

        page = (self.out / "wiki" / "01_注册" / "条目.html").read_text(encoding="utf-8")
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="search-status"', page)
        self.assertIn("正在加载搜索索引", script)
        self.assertIn("搜索索引加载失败", script)
        self.assertIn("重试", script)
        self.assertIn("DEKSearch.resultUrl", script)

    def test_homepage_is_a_stable_library_landing_page_not_a_document_redirect(self):
        build_site(self.vault, self.out)

        homepage = (self.out / "index.html").read_text(encoding="utf-8")

        self.assertNotIn("http-equiv=\"refresh\"", homepage)
        self.assertIn("DEK 知识库", homepage)
        self.assertIn("Wiki · 正式知识", homepage)
        self.assertIn("Source · 来源材料", homepage)

    def test_manifest_contains_nested_directory_tree(self):
        build_site(self.vault, self.out)

        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        wiki = next(node for node in manifest["tree"] if node["path"] == "wiki")
        registration = next(node for node in wiki["children"] if node["path"] == "wiki/01_注册")

        self.assertEqual(registration["type"], "directory")
        self.assertEqual(registration["count"], 1)
        self.assertEqual(registration["children"][0]["path"], "wiki/01_注册/条目.md")
        self.assertEqual(registration["children"][0]["type"], "document")

    def test_homepage_lists_top_level_directories(self):
        build_site(self.vault, self.out)

        homepage = (self.out / "index.html").read_text(encoding="utf-8")

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

        wiki = (self.out / "wiki" / "01_注册" / "条目.html").read_text(encoding="utf-8")

        self.assertIn("笔记信息", wiki)
        self.assertIn("编号", wiki)
        self.assertIn(">1<", wiki)
        self.assertIn("日期", wiki)
        self.assertIn("2026-09-10", wiki)
        self.assertIn("问题", wiki)
        self.assertIn("申报要求", wiki)
        self.assertIn("标签页面", wiki)
        self.assertIn("注册/受理", wiki)

    def test_explicit_source_wikilink_is_clickable_and_source_lists_referring_wiki(self):
        build_site(self.vault, self.out)

        wiki = (self.out / "wiki" / "01_注册" / "条目.html").read_text(encoding="utf-8")
        source = (self.out / "source" / "CDE" / "来源.html").read_text(encoding="utf-8")

        self.assertIn("来源笔记", wiki)
        self.assertIn('../../source/CDE/' + quote('来源.html'), wiki)
        self.assertIn("引用此来源的 Wiki", source)
        self.assertIn('../../wiki/' + quote('01_注册/条目.html', safe='/'), source)

    def test_page_includes_authenticated_name_and_logout_controls(self):
        build_site(self.vault, self.out)

        wiki = (self.out / "wiki" / "01_注册" / "条目.html").read_text(encoding="utf-8")
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
        homepage = (self.out / "index.html").read_text(encoding="utf-8")
        script = (self.out / "assets" / "app.js").read_text(encoding="utf-8")
        for marker in ("最近信息", "7天", "30天", "90天", "全部"):
            self.assertIn(marker, homepage)
        self.assertIn("recentDocuments", script)



if __name__ == "__main__":
    unittest.main()
