"""Adversarial tests for the stdlib whitelist HTML sanitizer in web.site.

These tests pin the security boundary around markdown-rendered note bodies:
dangerous tags/attributes/URLs are stripped while markdown's safe tags and the
AGENTS.md-mandated <br> (table-cell line breaks) survive.
"""
import tempfile
import unittest
from pathlib import Path

from web.site import build_site, sanitize_html


class SanitizeHtmlUnitTests(unittest.TestCase):
    def test_strips_script_tag_and_its_content(self):
        out = sanitize_html('<p>ok</p><script>alert("xss")</script>')
        self.assertIn("<p>ok</p>", out)
        self.assertNotIn("script", out.lower())
        self.assertNotIn("xss", out)
        self.assertNotIn("alert", out)

    def test_strips_style_tag(self):
        out = sanitize_html("<style>body{display:none}</style><p>x</p>")
        self.assertIn("<p>x</p>", out)
        self.assertNotIn("style", out.lower())

    def test_strips_iframe_object_embed(self):
        for tag in ("iframe", "object", "embed"):
            out = sanitize_html(f'<p>a</p><{tag} src="https://evil.example/x"></{tag}>')
            self.assertIn("<p>a</p>", out)
            self.assertNotIn(f"<{tag}", out.lower())

    def test_strips_svg_and_math_subtrees(self):
        out = sanitize_html(
            '<p>a</p><svg onload="alert(1)"><circle r="1"/></svg><math><mi>x</mi></math>'
        )
        self.assertIn("<p>a</p>", out)
        self.assertNotIn("svg", out.lower())
        self.assertNotIn("math", out.lower())
        self.assertNotIn("onload", out.lower())
        self.assertNotIn("alert", out)

    def test_strips_event_handler_attributes(self):
        out = sanitize_html('<img src="/x.png" onerror="alert(1)" onclick="x()">')
        self.assertNotIn("onerror", out.lower())
        self.assertNotIn("onclick", out.lower())
        self.assertNotIn("alert", out)

    def test_strips_javascript_vbscript_and_data_urls(self):
        for url in (
            "javascript:alert(1)",
            "vbscript:msgbox(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
        ):
            out = sanitize_html(f'<a href="{url}">x</a>')
            self.assertNotIn("javascript:", out.lower())
            self.assertNotIn("vbscript:", out.lower())
            self.assertNotIn("data:", out.lower())
            self.assertNotIn("alert", out)

    def test_preserves_safe_links(self):
        out = sanitize_html('<a href="https://example.com" class="wikilink">ok</a>')
        self.assertIn('href="https://example.com"', out)
        self.assertIn('class="wikilink"', out)
        self.assertIn(">ok</a>", out)

    def test_preserves_markdown_safe_tags_and_br(self):
        out = sanitize_html(
            '<h1 id="t">T</h1><h2 id="s">S</h2>'
            "<p><strong>b</strong><em>i</em><code class=\"language-python\">c</code></p>"
            "<pre><code>x</code></pre><blockquote><p>q</p></blockquote>"
            "<ul><li>a</li></ul><ol><li>b</li></ol>"
            "<table><thead><tr><th>h</th></tr></thead><tbody><tr><td>x<br>y</td></tr></tbody></table>"
        )
        for token in (
            "<h1", "<h2", "<strong>", "<em>", "<code", "<pre>", "<blockquote>",
            "<ul>", "<ol>", "<li>", "<table>", "<thead>", "<tbody>", "<tr>",
            "<th>", "<td>", "<br>",
        ):
            self.assertIn(token, out)
        self.assertIn('id="t"', out)
        self.assertIn('id="s"', out)
        self.assertIn('class="language-python"', out)

    def test_escaped_entities_are_not_reintroduced_as_tags(self):
        out = sanitize_html("<p>a &lt;script&gt;alert(1)&lt;/script&gt; b</p>")
        self.assertNotIn("<script", out.lower())
        self.assertIn("&lt;script&gt;", out)

    def test_self_closing_drop_tag_does_not_swallow_following_content(self):
        # A self-closing <svg/>/<script/>/<style/> must be dropped without entering
        # the subtree-drop state, otherwise everything after it is silently lost.
        out = sanitize_html("<svg/><p>safe</p>")
        self.assertNotIn("svg", out.lower())
        self.assertIn("<p>safe</p>", out)
        out = sanitize_html('<script/>after<script x="1"></script><p>ok</p>')
        self.assertNotIn("script", out.lower())
        self.assertIn("<p>ok</p>", out)

    def test_unicode_whitespace_obfuscated_url_is_rejected(self):
        for url in (
            "java\u00a0script:alert(1)",
            "java\u3000script:alert(1)",
            "java\u2007script:alert(1)",
        ):
            out = sanitize_html(f'<a href="{url}">x</a>')
            self.assertNotIn("javascript:", out.lower())
            self.assertNotIn("alert", out)


class SanitizeSiteIntegrationTests(unittest.TestCase):
    def test_build_site_filters_unsafe_frontmatter_source_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            out = Path(tmp) / "out"
            (vault / "wiki").mkdir(parents=True)
            (vault / "wiki" / "note.md").write_text(
                "---\nsource_urls:\n  - javascript:alert(1)\n  - https://example.com/source\n---\n\nSafe body.\n",
                encoding="utf-8",
            )
            build_site(vault, out)
            page = (out / "wiki" / "note.html").read_text(encoding="utf-8")
            self.assertNotIn("javascript:", page.lower())
            self.assertIn('href="https://example.com/source"', page)

    def test_build_site_emits_sanitized_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            out = Path(tmp) / "site"
            (vault / "wiki").mkdir(parents=True)
            (vault / "wiki" / "note.md").write_text(
                "---\nquestion: q\n---\n\n"
                "# Title\n\n"
                "<script>alert(1)</script>\n\n"
                '<img src="x" onerror="alert(2)">\n\n'
                "[bad](javascript:alert(3))\n\n"
                "| A | B |\n|---|---|\n|x<br>y | z |\n",
                encoding="utf-8",
            )
            build_site(vault, out)
            page = (out / "wiki" / "note.html").read_text(encoding="utf-8")
            article = page.split("<article>", 1)[1].split("</article>", 1)[0]
            self.assertNotIn("<script", article)
            self.assertNotIn("onerror", page)
            self.assertNotIn("javascript:", page)
            self.assertIn("<br>", article)
            self.assertIn("<table>", article)
            self.assertIn("<h1", article)
            self.assertIn("Title", article)


if __name__ == "__main__":
    unittest.main()
