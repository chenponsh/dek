"""Build a read-only Obsidian-style site from reviewed wiki/source notes."""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import markdown
import yaml

WIKILINK = re.compile(r"(!?)\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


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


def _render_body(doc: dict, by_path: dict[str, dict], by_stem: dict[str, list[dict]]) -> str:
    def repl(match: re.Match) -> str:
        target, label = match.group(2), match.group(3) or PurePosixPath(match.group(2)).name
        resolved = _resolve(target, by_path, by_stem)
        if not resolved:
            return f'<span class="broken-link" title="未找到：{html.escape(target)}">{html.escape(label)}</span>'
        href = _relative_href(doc["output"], resolved["output"])
        return f'<a class="wikilink" href="{href}">{html.escape(label)}</a>'
    source = WIKILINK.sub(repl, doc["body"])
    return markdown.markdown(source, extensions=["tables", "fenced_code", "toc", "sane_lists"], output_format="html")


def _toc(rendered: str) -> str:
    headings = re.findall(r'<h([2-4]) id="([^"]+)">(.*?)</h\1>', rendered)
    if not headings:
        return '<p class="muted">本页没有小节</p>'
    return "".join(f'<a class="toc-{level}" href="#{anchor}">{re.sub("<.*?>", "", text)}</a>' for level, anchor, text in headings)


def _manifest_tree(documents: list[dict]) -> list[dict]:
    roots = {kind: {"type": "directory", "name": kind, "path": kind, "children": {}} for kind in ("wiki", "source")}
    for doc in documents:
        parts = PurePosixPath(doc["path"]).parts
        node = roots[parts[0]]
        for index, part in enumerate(parts[1:-1], start=1):
            path = "/".join(parts[:index + 1])
            node = node["children"].setdefault(part, {"type": "directory", "name": part, "path": path, "children": {}})
        node["children"][parts[-1]] = {
            "type": "document", "name": doc["title"], "path": doc["path"], "url": doc["url"], "kind": doc["kind"],
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
    labels = (("no", "编号"), ("date", "日期"), ("question", "问题"), ("source", "来源"), ("tag_pages", "标签页面"), ("tags", "标签"))
    rows = []
    for key, label in labels:
        value = doc["meta"].get(key)
        if value is None or value == "" or value == []:
            continue
        rows.append(f'<div class="property-row"><dt>{label}</dt><dd>{_property_value(value, doc, by_path, by_stem)}</dd></div>')
    rows.append(f'<div class="property-row"><dt>笔记路径</dt><dd><code>{html.escape(doc["path"])}</code></dd></div>')
    return f'<details class="note-properties" open><summary>笔记信息</summary><dl>{"".join(rows)}</dl></details>'


def _page(doc: dict, docs: list[dict], rendered: str, backlinks: list[dict], by_path: dict[str, dict], by_stem: dict[str, list[dict]], source_refs: list[dict] | None = None) -> str:
    assets = _relative_href(doc["output"], PurePosixPath("assets/style.css"))
    script = _relative_href(doc["output"], PurePosixPath("assets/app.js"))
    search = _relative_href(doc["output"], PurePosixPath("assets/search-index.json"))
    manifest = _relative_href(doc["output"], PurePosixPath("manifest.json"))
    crumbs = " / ".join(html.escape(p) for p in PurePosixPath(doc["path"]).with_suffix("").parts)
    tags = doc["meta"].get("tags") or []
    if isinstance(tags, str): tags = [tags]
    badges = "".join(f'<span class="badge">#{html.escape(str(tag))}</span>' for tag in tags)
    links = "".join(f'<a href="{_relative_href(doc["output"], x["output"])}">{html.escape(x["title"])}</a>' for x in backlinks) or '<p class="muted">暂无反向链接</p>'
    raw_urls = doc["meta"].get("source_urls") or doc["meta"].get("source_url") or doc["meta"].get("url") or []
    if isinstance(raw_urls, str): raw_urls = [raw_urls]
    external_links = "".join(f'<a class="external" href="{html.escape(str(url))}" rel="noreferrer" target="_blank">打开来源链接 ↗</a>' for url in raw_urls)
    source_refs = source_refs or []
    source_note_links = "".join(f'<a href="{_relative_href(doc["output"], item["output"])}">{html.escape(item["title"])}</a>' for item in source_refs)
    source_wiki_links = "".join(f'<a href="{_relative_href(doc["output"], item["output"])}">{html.escape(item["title"])}</a>' for item in backlinks if item["kind"] == "wiki")
    source_card = ""
    if source_note_links: source_card += f'<section><h3>来源笔记</h3>{source_note_links}</section>'
    if external_links: source_card += f'<section><h3>来源链接</h3>{external_links}</section>'
    if doc["kind"] == "source" and source_wiki_links: source_card += f'<section><h3>引用此来源的 Wiki</h3>{source_wiki_links}</section>'
    properties = _note_properties(doc, by_path, by_stem)
    auth_me = _relative_href(doc["output"], PurePosixPath("auth/me"))
    auth_logout = _relative_href(doc["output"], PurePosixPath("auth/logout"))
    search_script = _relative_href(doc["output"], PurePosixPath("assets/search.js"))
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{html.escape(doc['title'])} · DEK</title><link rel="stylesheet" href="{assets}"></head><body><header><button id="menu-toggle" aria-label="打开目录">☰</button><strong>DEK 知识库</strong><div class="search-wrap"><div class="search-box"><input type="search" id="global-search" data-index="{search}" placeholder="输入关键词…" autocomplete="off"><button type="button" id="search-button" disabled>加载中…</button></div><span id="search-status" aria-live="polite"></span><div id="search-results"></div></div><div class="user-menu" data-auth-me="{auth_me}"><span id="user-name">正在读取…</span><a href="{auth_logout}">退出</a></div><button id="theme-toggle" aria-label="切换主题">◐</button></header><aside class="sidebar"><div class="side-title">浏览</div><nav id="nav-tree" data-manifest="{manifest}" data-current="{html.escape(doc['path'])}"></nav></aside><main class="document"><div class="breadcrumbs">{crumbs}</div><span class="kind">{doc['kind'].upper()}</span><h1>{html.escape(doc['title'])}</h1><div class="badges">{badges}</div>{properties}<article>{rendered}</article><section class="backlinks"><h2>反向链接</h2>{links}</section></main><aside class="toc"><h3>本页目录</h3>{_toc(rendered)}{source_card}</aside><script src="{search_script}" defer></script><script src="{script}" defer></script></body></html>'''


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
    public_docs = [{"path": d["path"], "title": d["title"], "kind": d["kind"], "url": str(d["output"])} for d in docs]
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
    home_body = '<p class="home-intro">面向具备 Kbot 使用权限同事的内部只读知识库。按目录浏览，或使用顶部搜索。</p>' + "".join(home_sections)
    (output / "index.html").write_text(_page(home_doc, docs, home_body, [], by_path, by_stem), encoding="utf-8")
    return {"documents": len(docs), "wiki": sum(d["kind"] == "wiki" for d in docs), "source": sum(d["kind"] == "source" for d in docs)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_site(args.vault, args.output), ensure_ascii=False))


if __name__ == "__main__": main()
