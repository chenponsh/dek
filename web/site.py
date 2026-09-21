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
            # 项目 is the entry's file name (0101-0001), as Obsidian's file.link shows it: the
            # entry's title is its question, which the 问题 column already carries.
            f'<tr><td><a href="{href}">{html.escape(PurePosixPath(item["path"]).stem)}</a></td>'
            f'<td>{question}</td><td>{source_cell}</td><td>{date_cell}</td></tr>'
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>项目</th><th>问题</th><th>来源</th><th>发布日期</th></tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
    )


_CODE_SPAN = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]+`)", re.S)


def _outside_code(text: str, transform) -> str:
    """`transform` applied to the text outside fenced blocks and inline code, which stay exactly as written."""
    parts = _CODE_SPAN.split(text)
    return "".join(part if index % 2 else transform(part) for index, part in enumerate(parts))


def _render_body(doc: dict, by_path: dict[str, dict], by_stem: dict[str, list[dict]], excluded_stems: frozenset[str] = frozenset()) -> str:
    def repl(match: re.Match) -> str:
        target, label = match.group(2), match.group(3) or PurePosixPath(match.group(2)).name
        resolved = _resolve(target, by_path, by_stem)
        if not resolved and PurePosixPath(_target_key(target)).name in excluded_stems:
            # A note that exists but was moved to an 排除 folder on purpose is not published: say so, it is not an error.
            return f'<span class="excluded-link">{html.escape(label)}</span>'
        if not resolved:
            return f'<span class="broken-link" title="未找到：{html.escape(target)}">{html.escape(label)}</span>'
        href = _relative_href(doc["output"], resolved["output"])
        return f'<a class="wikilink" href="{href}">{html.escape(label)}</a>'
    source = DATAVIEW_OVERVIEW.sub(lambda match: _dataview_overview_table(doc, match.group(1), by_path, by_stem, repl), doc["body"])
    source = _outside_code(source, lambda text: WIKILINK.sub(repl, text))
    rendered = markdown.markdown(source, extensions=["tables", "fenced_code", "toc", "sane_lists"], output_format="html")
    return sanitize_html(rendered)


def _toc_data(rendered: str) -> list[dict]:
    return [{"level": level, "anchor": anchor, "text": re.sub("<.*?>", "", text)}
            for level, anchor, text in re.findall(r'<h([2-4]) id="([^"]+)">(.*?)</h\1>', rendered)]


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


def _property_data(value: object, doc: dict, by_path: dict[str, dict], by_stem: dict[str, list[dict]]) -> dict:
    """A frontmatter value as data: {"list": [...]} or {"parts": [text | wikilink | broken link, ...]}."""
    if isinstance(value, list):
        return {"list": [_property_data(item, doc, by_path, by_stem) for item in value]}
    text = str(value)
    parts, cursor = [], 0
    for match in WIKILINK.finditer(text):
        if match.start() > cursor: parts.append({"t": text[cursor:match.start()]})
        target = _resolve(match.group(2), by_path, by_stem)
        label = match.group(3) or PurePosixPath(match.group(2)).name
        parts.append({"t": label, "href": _relative_href(doc["output"], target["output"])} if target else {"t": label, "broken": True})
        cursor = match.end()
    if cursor < len(text) or not parts: parts.append({"t": text[cursor:]})
    return {"parts": parts}


def _properties_data(doc: dict, by_path: dict[str, dict], by_stem: dict[str, list[dict]]) -> list[dict]:
    # "question" duplicates the title and "tags" duplicates "tag_pages" (and the
    # tag chips under the title); both are left out so the panel stays short.
    labels = (("no", "编号"), ("date", "发布日期"), ("source", "来源"), ("tag_pages", "标签页面"))
    rows = []
    for key, label in labels:
        value = doc["meta"].get(key)
        if value is None or value == "" or value == []:
            continue
        rows.append({"label": label, "value": _property_data(value, doc, by_path, by_stem)})
    rows.append({"label": "笔记路径", "code": doc["path"]})
    return rows


def _breadcrumb_data(doc: dict, by_path: dict[str, dict]) -> list[dict]:
    """Every segment except the current (leaf) page links: the first ("wiki"/"source")
    goes home, and each folder goes to its same-named overview note when one exists."""
    parts = PurePosixPath(doc["path"]).with_suffix("").parts
    segments = []
    for index, part in enumerate(parts):
        href = None
        if index == len(parts) - 1:
            pass
        elif index == 0:
            href = _relative_href(doc["output"], PurePosixPath("index.html"))
        else:
            target = by_path.get("/".join(parts[:index + 1] + (part,)))
            if target: href = _relative_href(doc["output"], target["output"])
        segments.append({"text": part, "href": href} if href else {"text": part})
    return segments


def _site_root(output: PurePosixPath) -> str:
    """Relative prefix from a page to the site root ("" for a top-level page)."""
    return _relative_href(output, PurePosixPath("index.html"))[:-len("index.html")]


def _page_data(doc: dict, rendered: str, backlinks: list[dict], by_path: dict[str, dict], by_stem: dict[str, list[dict]], source_refs: list[dict] | None = None) -> dict:
    tags = doc["meta"].get("tags") or []
    if isinstance(tags, str): tags = [tags]
    raw_urls = doc["meta"].get("source_urls") or doc["meta"].get("source_url") or doc["meta"].get("url") or []
    if isinstance(raw_urls, str): raw_urls = [raw_urls]
    link = lambda item: {"title": item["title"], "href": _relative_href(doc["output"], item["output"])}
    return {
        "v": 1, "kind": doc["kind"], "title": doc["title"], "path": doc["path"], "root": _site_root(doc["output"]),
        "tags": [str(tag) for tag in tags],
        "crumbs": _breadcrumb_data(doc, by_path),
        # The home page has no meaningful frontmatter of its own, so no properties panel.
        "props": [] if doc["kind"] == "home" else _properties_data(doc, by_path, by_stem),
        "backlinks": [link(item) for item in backlinks],
        "toc": _toc_data(rendered),
        "sourceNotes": [link(item) for item in (source_refs or [])],
        "externalLinks": [safe for url in raw_urls if (safe := _safe_url(str(url))) is not None],
        "sourceWiki": [link(item) for item in backlinks if item["kind"] == "wiki"] if doc["kind"] == "source" else [],
    }


def _json_for_html(value: object) -> str:
    """JSON safe to embed in a <script type="application/json"> element."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _page(doc: dict, docs: list[dict], rendered: str, backlinks: list[dict], by_path: dict[str, dict], by_stem: dict[str, list[dict]], source_refs: list[dict] | None = None) -> str:
    """A published page: the reviewed content plus a description of it. The header,
    sidebar, headings and panels are drawn in the browser by assets/page.js."""
    root = _site_root(doc["output"])
    data = _page_data(doc, rendered, backlinks, by_path, by_stem, source_refs)
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f'<title>{html.escape(doc["title"])} · DEK</title><link rel="stylesheet" href="{root}assets/style.css">'
        f'<script src="{root}assets/search.js" defer></script><script src="{root}assets/page.js" defer></script><script src="{root}assets/app.js" defer></script></head>'
        '<body><div id="page-loading" class="muted">正在加载页面…</div><noscript>此页面需要启用 JavaScript。</noscript>'
        f'<script type="application/json" id="page-data">{_json_for_html(data)}</script>'
        f'<template id="page-body">{rendered}</template></body></html>'
    )


_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _copy_note_files(vault: Path, output: Path) -> None:
    """Pictures and PDF attachments used by notes.

    They live in `wiki/_images/` and `wiki/_attachments/` and are referenced with a
    relative path (`../_images/x.png`, `../_attachments/x.pdf`), which is also what
    Obsidian resolves. The site keeps the same layout. Only plain files with a safe
    ASCII name are copied (no links, no other types); a `.pdf` must really start
    with the PDF signature and stay under a size limit."""
    for folder, suffixes in (("_images", _IMAGE_SUFFIXES), ("_attachments", frozenset({".pdf"}))):
        source = vault / "wiki" / folder
        if not source.is_dir() or source.is_symlink():
            continue
        target = output / "wiki" / folder
        for path in sorted(source.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            if not re.fullmatch(r"[A-Za-z0-9._-]+", path.name):
                continue
            if path.suffix.lower() == ".pdf":
                if path.stat().st_size > MAX_ATTACHMENT_BYTES:
                    continue
                with path.open("rb") as handle:
                    if handle.read(5) != b"%PDF-":
                        continue
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target / path.name)


# Material dated on or after this day is only public once a reviewer approved it. The ingest
# pushes every pulled source note and table row straight into the repository, but a reviewer
# only approves the wiki entry made from it, so the site holds the raw source back until then.
# Must equal `earliest_date` in ingestion/automation/config.json (a test keeps them together).
SOURCE_APPROVAL_FLOOR = "2026-03-01"
_TABLE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_MIN_QUESTION_CHARS = 8


def _question_key(text: object) -> str:
    text = re.sub(r"<br\s*/?>", "", str(text or ""))
    return re.sub(r"^问[:：]", "", re.sub(r"\s+", "", text)).replace("：", "").replace(":", "")


def _same_question(row_key: str, wiki_key: str) -> bool:
    if min(len(row_key), len(wiki_key)) < _MIN_QUESTION_CHARS:
        return False
    return row_key == wiki_key or row_key.startswith(wiki_key) or wiki_key.startswith(row_key)


def _row_date(line: str) -> tuple[list[str], str]:
    cells = [cell.strip() for cell in _UNESCAPED_PIPE.split(line.strip()[1:-1])]
    dates = [cell for cell in cells if _TABLE_DATE.fullmatch(cell)]
    return cells, max(dates) if dates else ""


def _source_note_date(doc: dict) -> str:
    """A source note's own date: its `date`, else the YYYY-MM-DD its file name starts with."""
    named = re.match(r"(\d{4}-\d{2}-\d{2})_", PurePosixPath(doc["key"]).name)
    return _iso_date(doc["meta"].get("date")) or (named.group(1) if named else "")


def _hold_back_unapproved_source(docs: list[dict]) -> tuple[list[dict], frozenset[str]]:
    """Drop source material dated from SOURCE_APPROVAL_FLOOR on that no approved wiki entry
    stands on. Wiki entries are in the tree only once approved, so a wiki entry dated from
    the floor on is the approval. Returns the remaining docs (source table rows edited in
    place on copies) and the names of the whole notes that were held back."""
    by_path = {d["key"]: d for d in docs}
    by_stem: dict[str, list[dict]] = {}
    for d in docs: by_stem.setdefault(PurePosixPath(d["key"]).name, []).append(d)
    approved: dict[str, list[str]] = {}
    for doc in docs:
        if doc["kind"] != "wiki" or (_iso_date(doc["meta"].get("date")) or "") < SOURCE_APPROVAL_FLOOR:
            continue
        question = _question_key(doc["meta"].get("question") or doc["title"])
        for match in WIKILINK.finditer(str(doc["meta"].get("source") or "")):
            target = _resolve(match.group(2), by_path, by_stem)
            if target and target["kind"] == "source":
                approved.setdefault(target["key"], []).append(question)
    kept, held = [], set()
    for doc in docs:
        if doc["kind"] != "source":
            kept.append(doc); continue
        if _source_note_date(doc) >= SOURCE_APPROVAL_FLOOR:
            if doc["key"] in approved:
                kept.append(doc)
            else:
                held.add(PurePosixPath(doc["key"]).name)
            continue
        questions = approved.get(doc["key"], [])
        lines = []
        for line in doc["body"].split("\n"):
            if line.startswith("|"):
                cells, day = _row_date(line)
                if day >= SOURCE_APPROVAL_FLOOR and not any(_same_question(_question_key(cells[0]), q) for q in questions):
                    continue
            lines.append(line)
        kept.append({**doc, "body": "\n".join(lines)})
    return kept, frozenset(held)


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
    docs, held_stems = _hold_back_unapproved_source(docs)
    excluded_stems = held_stems | frozenset(
        path.stem for area in (vault / "wiki", vault / "source") if area.exists() for path in area.rglob("*.md")
        if any("排除" in part for part in path.relative_to(vault).parts))
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
    shutil.copy2(asset_dir / "page.js", output / "assets" / "page.js")
    _copy_note_files(vault, output)
    for doc in docs:
        rendered = _render_body(doc, by_path, by_stem, excluded_stems)
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
    # The page itself (filters, category cards) is built in the browser by the
    # installed scripts from manifest.json, so changing it takes a deploy, not a
    # release; only the directory data below comes from reviewed content.
    home_body = (
        '<div id="home-app" data-manifest="manifest.json" data-index="assets/search-index.json">'
        '<noscript>首页需要启用 JavaScript。</noscript>正在加载首页…</div>'
    )
    (output / "index.html").write_text(_page(home_doc, docs, home_body, [], by_path, by_stem), encoding="utf-8")
    return {"documents": len(docs), "wiki": sum(d["kind"] == "wiki" for d in docs), "source": sum(d["kind"] == "source" for d in docs)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_site(args.vault, args.output), ensure_ascii=False))


if __name__ == "__main__": main()
