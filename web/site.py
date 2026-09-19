"""Build a read-only Obsidian-style site from reviewed wiki/source notes."""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import markdown
import yaml

WIKILINK = re.compile(r"(!?)\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
LEAF_CODE = re.compile(r"^\d+-\d+$")
# The vault's per-folder overview notes all embed this identical Obsidian
# Dataview block (rendered dynamically only inside Obsidian itself); the
# static site instead computes the equivalent table at build time so
# published pages don't show the raw, unrendered query text.
DATAVIEW_OVERVIEW = re.compile(
    r'```dataview\n'
    r'TABLE WITHOUT ID\n'
    r'  file\.link AS 项目,\n'
    r'  question AS 问题,\n'
    r'  source AS 来源,\n'
    r'  dateformat\(date, "yyyy-MM-dd"\) AS 日期\n'
    r'FROM "([^"]+)"\n'
    r'WHERE no != null\n'
    r'SORT file\.folder ASC, no ASC\n```'
)


def is_publishable_path(path: Path) -> bool:
    parts = path.as_posix().split("/")
    return path.suffix.lower() == ".md" and parts[0] in {"wiki", "source"} and not any("排除" in p for p in parts)


def _split_note(text: str) -> tuple[dict, str]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1)) or {}
    if False in meta and "no" not in meta:
        meta["no"] = meta.pop(False)
    return meta, text[match.end():]


def _iso_date(value: object) -> str | None:
    """Normalize a frontmatter date value to 'YYYY-MM-DD', or None if absent/invalid."""
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    match = re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", str(value).strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _target_key(raw: str) -> str:
    value = raw.strip().replace("\\", "/")
    return value[:-3] if value.endswith(".md") else value


def _resolve(target: str, by_path: dict[str, dict], by_stem: dict[str, list[dict]]) -> dict | None:
    key = _target_key(target).lstrip("/")
    if key in by_path:
        return by_path[key]
    matches = by_stem.get(PurePosixPath(key).name, [])
    return matches[0] if len(matches) == 1 else None


def _relative_href(source_output: PurePosixPath, target_output: PurePosixPath) -> str:
    import posixpath
    return quote(posixpath.relpath(str(target_output), str(source_output.parent)), safe="/.-_")


def _title(meta: dict, path: Path, body: str) -> str:
    return str(meta.get("question") or meta.get("source_name") or next((m.group(1).strip() for m in re.finditer(r"^#\s+(.+)$", body, re.M)), path.stem))


def _tree_html(docs: list[dict], current: str, source_output: PurePosixPath) -> str:
    groups = {"wiki": [], "source": []}
    for doc in docs:
        groups[doc["kind"]].append(doc)
    chunks = []
    for kind, label in (("wiki", "Wiki · 正式知识"), ("source", "Source · 来源材料")):
        links = []
        for doc in sorted(groups[kind], key=lambda d: d["path"]):
            cls = " active" if doc["path"] == current else ""
            href = _relative_href(source_output, doc["output"])
            links.append(f'<a class="tree-link{cls}" href="{href}" title="{html.escape(doc["path"])}">{html.escape(doc["title"])}</a>')
        chunks.append(f'<details open><summary>{label}<span>{len(links)}</span></summary>{"".join(links)}</details>')
    return "".join(chunks)


# --- Whitelist HTML sanitizer (stdlib only; no new dependencies) ---

_ALLOWED_TAGS = frozenset({
    "p", "br", "hr", "strong", "em", "b", "i", "u", "s", "del", "ins", "sub", "sup",
    "code", "pre", "blockquote", "ul", "ol", "li", "dl", "dt", "dd",
    "h1", "h2", "h3", "h4", "h5", "h6", "a", "span", "div",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "img",
})
_DROP_SUBTREE_TAGS = frozenset({
    "script", "style", "iframe", "object", "embed", "svg", "math", "frame", "frameset",
    "form", "input", "button", "select", "option", "textarea", "link", "meta", "base",
    "title", "template", "noscript", "applet", "audio", "video", "source", "track",
})
_ALLOWED_ATTRS = {
    "a": frozenset({"href", "title", "class", "rel", "target"}),
    "img": frozenset({"src", "alt", "title"}),
    "h1": frozenset({"id"}), "h2": frozenset({"id"}), "h3": frozenset({"id"}),
    "h4": frozenset({"id"}), "h5": frozenset({"id"}), "h6": frozenset({"id"}),
    "span": frozenset({"class"}), "div": frozenset({"class"}),
    "code": frozenset({"class"}), "pre": frozenset({"class"}),
    "th": frozenset({"align"}), "td": frozenset({"align"}),
}
_VOID_TAGS = frozenset({"br", "hr", "img"})
_ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto"})


def _safe_url(value: object, *, local_only: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    compact = re.sub(r"[\x00-\x20\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]", "", text)
    if compact.startswith("//") or compact.startswith("\\\\"):
        return None
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", compact)
    if match and match.group(1).lower() not in _ALLOWED_URL_SCHEMES:
        return None
    if local_only and match:
        return None
    return text


class _WhitelistSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._drop = 0

    def _emit_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        allowed = _ALLOWED_ATTRS.get(tag, ())
        cleaned: list[tuple[str, str | None]] = []
        for name, value in attrs:
            name = name.lower()
            if name.startswith("on") or name == "style":
                continue
            if name not in allowed:
                continue
            if name in ("href", "src"):
                value = _safe_url(value,local_only=(name=="src"))
                if value is None:
                    continue
            cleaned.append((name, value))
        attributes = "".join(
            f' {name}="{html.escape(value, quote=True)}"' if value is not None else f" {name}"
            for name, value in cleaned
        )
        self._parts.append(f"<{tag}{attributes}>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._drop:
            if tag in _DROP_SUBTREE_TAGS:
                self._drop += 1
            return
        if tag in _DROP_SUBTREE_TAGS:
            self._drop = 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        self._emit_start(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _DROP_SUBTREE_TAGS:
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._drop:
            if tag in _DROP_SUBTREE_TAGS:
                self._drop -= 1
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._drop:
            return
        self._parts.append(html.escape(data, quote=False))


def sanitize_html(fragment: str) -> str:
    parser = _WhitelistSanitizer()
    parser.feed(fragment)
    parser.close()
    return "".join(parser._parts)


def _dataview_sort_key(value: object) -> tuple[int, float | str]:
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, str(value if value is not None else ""))


def _dataview_overview_table(doc: dict, from_path: str, by_path: dict[str, dict], by_stem: dict[str, list[dict]], repl) -> str:
    prefix = from_path.rstrip("/") + "/"
    rows = [item for item in by_path.values() if item["path"].startswith(prefix) and item["meta"].get("no") is not None]
    rows.sort(key=lambda item: (PurePosixPath(item["path"]).parent.as_posix(), _dataview_sort_key(item["meta"].get("no"))))
    if not rows:
        return '<p class="muted">暂无内容</p>'
    body_rows = []
    for item in rows:
        href = _relative_href(doc["output"], item["output"])
        question = html.escape(str(item["meta"].get("question") or ""))
        raw_source = item["meta"].get("source")
        source_cell = WIKILINK.sub(repl, html.escape(str(raw_source))) if raw_source else ""
        raw_date = item["meta"].get("date")
        date_cell = raw_date.strftime("%Y-%m-%d") if isinstance(raw_date, (date, datetime)) else html.escape(str(raw_date or ""))
        body_rows.append(
            f'<tr><td><a href="{href}">{html.escape(item["title"])}</a></td>'
            f'<td>{question}</td><td>{source_cell}</td><td>{date_cell}</td></tr>'
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>项目</th><th>问题</th><th>来源</th><th>日期</th></tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
    )


def _render_body(doc: dict, by_path: dict[str, dict], by_stem: dict[str, list[dict]]) -> str:
    def repl(match: re.Match) -> str:
        target, label = match.group(2), match.group(3) or PurePosixPath(match.group(2)).name
        resolved = _resolve(target, by_path, by_stem)
        if not resolved:
            return f'<span class="broken-link" title="未找到：{html.escape(target)}">{html.escape(label)}</span>'
        href = _relative_href(doc["output"], resolved["output"])
        return f'<a class="wikilink" href="{href}">{html.escape(label)}</a>'
    source = DATAVIEW_OVERVIEW.sub(lambda match: _dataview_overview_table(doc, match.group(1), by_path, by_stem, repl), doc["body"])
    source = WIKILINK.sub(repl, source)
    rendered = markdown.markdown(source, extensions=["tables", "fenced_code", "toc", "sane_lists"], output_format="html")
    return sanitize_html(rendered)


def _toc(rendered: str) -> str:
    headings = re.findall(r'<h([2-4]) id="([^"]+)">(.*?)</h\1>', rendered)
    return "".join(f'<a class="toc-{level}" href="#{anchor}">{re.sub("<.*?>", "", text)}</a>' for level, anchor, text in headings)


def _manifest_tree(documents: list[dict]) -> list[dict]:
    roots = {kind: {"type": "directory", "name": kind, "path": kind, "children": {}} for kind in ("wiki", "source")}
    for doc in documents:
        parts = PurePosixPath(doc["path"]).parts
        node = roots[parts[0]]
        for index, part in enumerate(parts[1:-1], start=1):
            path = "/".join(parts[:index + 1])
            node = node["children"].setdefault(part, {"type": "directory", "name": part, "path": path, "children": {}})
        stem = PurePosixPath(parts[-1]).stem
        label = f"{stem} {doc['title']}" if LEAF_CODE.match(stem) else doc["title"]
        node["children"][parts[-1]] = {
            "type": "document", "name": label, "path": doc["path"], "url": doc["url"], "kind": doc["kind"],
        }

    def freeze(node: dict) -> dict:
        children = [freeze(child) if child["type"] == "directory" else child for child in node["children"].values()]
        children.sort(key=lambda child: (child["type"] != "directory", child["path"]))
        result = {key: value for key, value in node.items() if key != "children"}
        result["children"] = children
        result["count"] = sum(child["count"] if child["type"] == "directory" else 1 for child in children)
        return result

    return [freeze(roots["wiki"]), freeze(roots["source"])]


def _property_value(value: object, doc: dict, by_path: dict[str, dict], by_stem: dict[str, list[dict]]) -> str:
    if isinstance(value, list):
        return '<span class="property-list">' + "".join(f"<span>{_property_value(item, doc, by_path, by_stem)}</span>" for item in value) + "</span>"
    text = str(value)
    chunks, cursor = [], 0
    for match in WIKILINK.finditer(text):
        chunks.append(html.escape(text[cursor:match.start()]).replace("\n", "<br>"))
        target = _resolve(match.group(2), by_path, by_stem)
        label = match.group(3) or PurePosixPath(match.group(2)).name
        if target:
            chunks.append(f'<a class="wikilink" href="{_relative_href(doc["output"], target["output"])}">{html.escape(label)}</a>')
        else:
            chunks.append(f'<span class="broken-link">{html.escape(label)}</span>')
        cursor = match.end()
    chunks.append(html.escape(text[cursor:]).replace("\n", "<br>"))
    return "".join(chunks)


def _note_properties(doc: dict, by_path: dict[str, dict], by_stem: dict[str, list[dict]]) -> str:
    # "question" duplicates the <h1> title directly above this table, and "tags"
    # duplicates "tag_pages" (and the badge chips under the title); both are
    # dropped here so the table doesn't push the article below the fold.
    labels = (("no", "编号"), ("date", "日期"), ("source", "来源"), ("tag_pages", "标签页面"))
    rows = []
    for key, label in labels:
        value = doc["meta"].get(key)
        if value is None or value == "" or value == []:
            continue
        rows.append(f'<div class="property-row"><dt>{label}</dt><dd>{_property_value(value, doc, by_path, by_stem)}</dd></div>')
    rows.append(f'<div class="property-row"><dt>笔记路径</dt><dd><code>{html.escape(doc["path"])}</code></dd></div>')
    return f'<details class="note-properties" open><summary>笔记信息</summary><dl>{"".join(rows)}</dl></details>'


def _breadcrumb_html(doc: dict, by_path: dict[str, dict]) -> str:
    """Link every breadcrumb segment except the current (leaf) page: the first
    segment ("wiki"/"source") goes home, and each folder segment goes to its
    same-named overview note when one exists, else stays plain text."""
    parts = PurePosixPath(doc["path"]).with_suffix("").parts
    segments = []
    for index, part in enumerate(parts):
        text = html.escape(part)
        href = None
        if index == len(parts) - 1:
            pass
        elif index == 0:
            href = _relative_href(doc["output"], PurePosixPath("index.html"))
        else:
            target = by_path.get("/".join(parts[:index + 1] + (part,)))
            if target: href = _relative_href(doc["output"], target["output"])
        segments.append(f'<a href="{href}">{text}</a>' if href else text)
    return " / ".join(segments)


def _page(doc: dict, docs: list[dict], rendered: str, backlinks: list[dict], by_path: dict[str, dict], by_stem: dict[str, list[dict]], source_refs: list[dict] | None = None) -> str:
    assets = _relative_href(doc["output"], PurePosixPath("assets/style.css"))
    script = _relative_href(doc["output"], PurePosixPath("assets/app.js"))
    search = _relative_href(doc["output"], PurePosixPath("assets/search-index.json"))
    manifest = _relative_href(doc["output"], PurePosixPath("manifest.json"))
    crumbs = _breadcrumb_html(doc, by_path)
    tags = doc["meta"].get("tags") or []
    if isinstance(tags, str): tags = [tags]
    badges = "".join(f'<span class="badge">#{html.escape(str(tag))}</span>' for tag in tags)
    links = "".join(f'<a href="{_relative_href(doc["output"], x["output"])}">{html.escape(x["title"])}</a>' for x in backlinks) or '<p class="muted">暂无反向链接</p>'
    raw_urls = doc["meta"].get("source_urls") or doc["meta"].get("source_url") or doc["meta"].get("url") or []
    if isinstance(raw_urls, str): raw_urls = [raw_urls]
    safe_urls = [safe for url in raw_urls if (safe := _safe_url(str(url))) is not None]
    external_links = "".join(f'<a class="external" href="{html.escape(url)}" rel="noreferrer" target="_blank">打开来源链接 ↗</a>' for url in safe_urls)
    source_refs = source_refs or []
    source_note_links = "".join(f'<a href="{_relative_href(doc["output"], item["output"])}">{html.escape(item["title"])}</a>' for item in source_refs)
    source_wiki_links = "".join(f'<a href="{_relative_href(doc["output"], item["output"])}">{html.escape(item["title"])}</a>' for item in backlinks if item["kind"] == "wiki")
    source_card = ""
    if source_note_links: source_card += f'<section><h3>来源笔记</h3>{source_note_links}</section>'
    if external_links: source_card += f'<section><h3>来源链接</h3>{external_links}</section>'
    if doc["kind"] == "source" and source_wiki_links: source_card += f'<section><h3>引用此来源的 Wiki</h3>{source_wiki_links}</section>'
    # The home page has no meaningful frontmatter of its own ("笔记信息" would
    # just show a synthetic "首页.md" path), so it skips the properties panel
    # entirely rather than rendering an empty/misleading one.
    properties = "" if doc["kind"] == "home" else _note_properties(doc, by_path, by_stem)
    auth_me = _relative_href(doc["output"], PurePosixPath("auth/me"))
    auth_logout = _relative_href(doc["output"], PurePosixPath("auth/logout"))
    search_script = _relative_href(doc["output"], PurePosixPath("assets/search.js"))
    toc = _toc(rendered)
    # Real Q&A-style notes rarely have markdown headings, so "本页目录" was
    # showing an always-empty "本页没有小节" placeholder on every such page;
    # only render the aside (and its heading) when there is something in it.
    toc_section = f'<h3>本页目录</h3>{toc}' if toc else ""
    aside = f'<aside class="toc">{toc_section}{source_card}</aside>' if (toc_section or source_card) else ""
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{html.escape(doc['title'])} · DEK</title><link rel="stylesheet" href="{assets}"></head><body><header><button id="menu-toggle" aria-label="打开目录">☰</button><strong>DEK 知识库</strong><div class="search-wrap"><div class="search-box"><input type="search" id="global-search" data-index="{search}" placeholder="输入关键词…" autocomplete="off"><button type="button" id="search-button" disabled>加载中…</button></div><span id="search-status" aria-live="polite"></span><div id="search-results"></div></div><div class="user-menu" data-auth-me="{auth_me}"><a class="review-entry" href="/review/">知识审核</a><span id="user-name">正在读取…</span><a href="{auth_logout}">退出</a></div><button id="theme-toggle" aria-label="切换主题">◐</button></header><aside class="sidebar"><nav id="nav-tree" data-manifest="{manifest}" data-current="{html.escape(doc['path'])}"></nav><div class="sidebar-resize-handle" aria-hidden="true"></div></aside><main class="document{' home-page' if doc['kind']=='home' else ''}"><div class="breadcrumbs">{crumbs}</div><span class="kind">{doc['kind'].upper()}</span><h1>{html.escape(doc['title'])}</h1><div class="badges">{badges}</div>{properties}<article>{rendered}</article><section class="backlinks"><h2>反向链接</h2>{links}</section></main>{aside}<script src="{search_script}" defer></script><script src="{script}" defer></script></body></html>'''


def build_site(vault: Path, output: Path) -> dict:
    vault, output = Path(vault), Path(output)
    docs = []
    for root in (vault / "wiki", vault / "source"):
        if not root.exists(): continue
        for path in root.rglob("*.md"):
            rel = path.relative_to(vault)
            if not is_publishable_path(rel): continue
            meta, body = _split_note(path.read_text(encoding="utf-8"))
            key = rel.with_suffix("").as_posix()
            docs.append({"path": rel.as_posix(), "key": key, "kind": rel.parts[0], "meta": meta, "body": body, "title": _title(meta, path, body), "output": PurePosixPath(rel.with_suffix(".html").as_posix())})
    by_path = {d["key"]: d for d in docs}
    by_stem: dict[str, list[dict]] = {}
    for d in docs: by_stem.setdefault(PurePosixPath(d["key"]).name, []).append(d)
    backlinks = {d["key"]: [] for d in docs}
    for doc in docs:
        for match in WIKILINK.finditer(doc["body"] + "\n" + json.dumps(doc["meta"], ensure_ascii=False, default=str)):
            target = _resolve(match.group(2), by_path, by_stem)
            if target and doc not in backlinks[target["key"]]: backlinks[target["key"]].append(doc)
    if output.exists(): shutil.rmtree(output)
    (output / "assets").mkdir(parents=True)
    asset_dir = Path(__file__).with_name("assets")
    shutil.copy2(asset_dir / "style.css", output / "assets" / "style.css")
    shutil.copy2(asset_dir / "app.js", output / "assets" / "app.js")
    shutil.copy2(asset_dir / "search.js", output / "assets" / "search.js")
    for doc in docs:
        rendered = _render_body(doc, by_path, by_stem)
        refs = []
        for match in WIKILINK.finditer(str(doc["meta"].get("source") or "") + "\n" + doc["body"]):
            target = _resolve(match.group(2), by_path, by_stem)
            if target and target["kind"] == "source" and target not in refs: refs.append(target)
        dest = output / Path(str(doc["output"])); dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_page(doc, docs, rendered, backlinks[doc["key"]], by_path, by_stem, refs), encoding="utf-8")
    public_docs = [{"path": d["path"], "title": d["title"], "kind": d["kind"], "url": str(d["output"]), "date": _iso_date(d["meta"].get("date"))} for d in docs]
    tree = _manifest_tree(public_docs)
    (output / "manifest.json").write_text(json.dumps({"documents": public_docs, "tree": tree}, ensure_ascii=False, indent=2), encoding="utf-8")
    search_docs = [{**x, "tags": docs[index]["meta"].get("tags") or [], "text": re.sub(r"\s+", " ", docs[index]["body"])[:5000]} for index, x in enumerate(public_docs)]
    (output / "assets" / "search-index.json").write_text(json.dumps(search_docs, ensure_ascii=False), encoding="utf-8")
    home_doc = {
        "path": "首页.md", "kind": "home", "meta": {}, "title": "DEK 知识库",
        "output": PurePosixPath("index.html"),
    }
    home_sections = []
    for root_node, label in zip(tree, ("Wiki · 正式知识", "Source · 来源材料")):
        cards = []
        for child in root_node["children"]:
            target = child
            while target["type"] == "directory" and target["children"]:
                target = target["children"][0]
            href = quote(str(target.get("url", "#")), safe="/.-_")
            cards.append(f'<a class="folder-card" href="{href}"><strong>{html.escape(child["name"])}</strong><span>{child.get("count", 1)} 篇</span></a>')
        home_sections.append(f'<section class="home-section"><h2>{label}<span>{root_node["count"]}</span></h2><div class="folder-grid">{"".join(cards)}</div></section>')
    # The filter controls stay near the top (so reviewers don't have to scroll
    # past every folder card to find them again), but the actual result list
    # moves below the Wiki/Source cards -- see recent_results_html below.
    recent_filters_html = (
        '<section class="recent-filters"><h2>最近信息</h2>'
        '<div class="recent-tabs" role="group" aria-label="按天数快速筛选">'
        '<button type="button" class="recent-tab active" data-days="7">7天</button>'
        '<button type="button" class="recent-tab" data-days="30">30天</button>'
        '<button type="button" class="recent-tab" data-days="90">90天</button>'
        '<button type="button" class="recent-tab" data-days="0">全部</button>'
        '</div>'
        '<div class="recent-range">'
        '<label>开始日期 <input type="date" id="recent-start"></label>'
        '<label>结束日期 <input type="date" id="recent-end"></label>'
        '</div>'
        '</section>'
    )
    recent_results_html = '<section class="recent-section"><div id="recent-list" class="recent-list" data-index="assets/search-index.json">正在加载最近信息…</div></section>'
    home_body = recent_filters_html + "".join(home_sections) + recent_results_html
    (output / "index.html").write_text(_page(home_doc, docs, home_body, [], by_path, by_stem), encoding="utf-8")
    return {"documents": len(docs), "wiki": sum(d["kind"] == "wiki" for d in docs), "source": sum(d["kind"] == "source" for d in docs)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_site(args.vault, args.output), ensure_ascii=False))


if __name__ == "__main__": main()
