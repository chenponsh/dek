"""Adapters for sources that publish one article per question.

An adapter turns a source's listing page into `Row`s for the items that are not
in the source note yet. It only looks at items newer than the note's
`last_updated` and only opens the articles it needs, so a run costs one listing
request plus one request per new item.

Every adapter has the signature
    fetch(source, known, since) -> (rows, meta)
where `known` is the set of (normalize(question), date) keys already in the
note and `since` is the note's `last_updated` (YYYY-MM-DD).
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .core import Row, SafetyStop, markdown_cell, normalize
from .fetchers import fetch_shanghai, get_json, http_get

def html_to_text(fragment: str) -> str:
    """Article HTML -> plain text with one line per paragraph."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", fragment)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ").replace("　", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


@dataclass(frozen=True)
class ListItem:
    url: str
    title: str
    date: str


# --------------------------------------------------------------- 江苏省药监局

_JIANGSU_ITEM = re.compile(
    r'<a\b[^>]*?href="(?P<href>/art/[^"]+)"[^>]*>(?P<title>.*?)</a>(?P<tail>.{0,240}?)(?P<date>\d{4}-\d{2}-\d{2})',
    re.S,
)


def parse_jiangsu_list(page: str, base: str) -> list[ListItem]:
    """The column page carries the listing twice (PC table and mobile list);
    keep one item per article URL, in page order."""
    items: dict[str, ListItem] = {}
    for match in _JIANGSU_ITEM.finditer(page):
        title = re.sub(r"^\s*[·•]\s*", "", html_to_text(match.group("title")))
        url = urllib.parse.urljoin(base, match.group("href"))
        if title and url not in items:
            items[url] = ListItem(url, title, match.group("date"))
    return list(items.values())


def parse_jiangsu_article(page: str) -> tuple[str, str, str]:
    """(title, answer text, date) of one article page."""
    title = re.search(r'<meta name="ArticleTitle" content="([^"]*)"', page)
    pub = re.search(r'<meta name="PubDate" content="(\d{4}-\d{2}-\d{2})', page)
    begin = page.find("ZJEG_RSS.content.begin")
    end = page.find("ZJEG_RSS.content.end", begin)
    if not title or not pub or begin < 0 or end < 0:
        raise SafetyStop("article page has no title, date or body markers")
    body = re.sub(r"<meta name=\"Content(Start|End)\"[^>]*>", "", page[begin + len("ZJEG_RSS.content.begin"):end])
    body = body.removeprefix("-->").rstrip().removesuffix("<!--")
    return html.unescape(title.group(1)).strip(), html_to_text(body), pub.group(1)


def fetch_jiangsu(
    source: dict[str, Any], known: set[tuple[str, str]], since: str,
    get: Callable[[str], str] = http_get,
) -> tuple[list[Row], dict[str, Any]]:
    listing = parse_jiangsu_list(get(source["url"]), source["url"])
    if not listing:
        raise SafetyStop("Jiangsu column page lists no articles; page structure may have changed")
    rows: list[Row] = []
    skipped: list[dict[str, str]] = []
    for item in listing:
        if item.date <= since or (normalize(item.title), item.date) in known:
            continue
        try:
            title, answer, date = parse_jiangsu_article(get(item.url))
        except SafetyStop as exc:
            skipped.append({"url": item.url, "reason": str(exc)})
            continue
        if not answer:
            skipped.append({"url": item.url, "reason": "article body is empty (image or attachment only)"})
            continue
        rows.append(Row(title, answer, date))
    meta: dict[str, Any] = {"remote_count": len(listing), "latest_date": max((i.date for i in listing), default=None)}
    if skipped:
        meta["skipped_items"] = skipped
    return rows, meta


# ------------------------------------------------ article sources (li > a + date)

@dataclass(frozen=True)
class ArticleRow(Row):
    """A question/answer row that remembers the web article it came from."""
    article_title: str = ""
    article_url: str = ""


_LI_ITEM = re.compile(
    r'<li[^>]*>\s*<a\b[^>]*?href="(?P<href>[^"]+)"[^>]*>(?P<title>(?:(?!<li\b|</li>).)*?)</a>\s*<(?:em|span)[^>]*>\s*[（(]?\s*(?P<date>\d{4}-\d{2}-\d{2})',
    re.S,
)


def parse_li_list(page: str, base: str) -> list[ListItem]:
    """`<li><a href>title</a><em>date</em></li>` listings (Hainan, Shaanxi)."""
    items: dict[str, ListItem] = {}
    for match in _LI_ITEM.finditer(page):
        title = re.sub(r"^\s*[·•]\s*", "", html_to_text(match.group("title"))).replace("\n", " ")
        title = re.sub(r"\s+", " ", title).strip()
        url = urllib.parse.urljoin(base, match.group("href"))
        if title and url not in items:
            items[url] = ListItem(url, title, match.group("date"))
    return list(items.values())


class _BlockFinder(HTMLParser):
    """Collects the inner HTML of the first element for which `wanted(tag, attrs)` holds."""

    def __init__(self, wanted: Callable[[str, dict[str, str]], bool]):
        super().__init__(convert_charrefs=False)
        self.wanted, self.depth, self.parts, self.done = wanted, 0, [], False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self.depth:
            if tag not in _VOID:
                self.depth += 1
            self.parts.append(self.get_starttag_text())
        elif self.wanted(tag, {k: v or "" for k, v in attrs}):
            self.depth = 1

    def handle_startendtag(self, tag, attrs):
        if self.depth and not self.done:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if not self.depth or self.done:
            return
        if tag in _VOID:
            return
        self.depth -= 1
        if self.depth == 0:
            self.done = True
        else:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.depth and not self.done:
            self.parts.append(data)

    handle_entityref = lambda self, name: self.parts.append(f"&{name};") if self.depth and not self.done else None
    handle_charref = lambda self, name: self.parts.append(f"&#{name};") if self.depth and not self.done else None


_VOID = {"br", "img", "hr", "meta", "link", "input"}


def extract_block(page: str, wanted: Callable[[str, dict[str, str]], bool]) -> str:
    finder = _BlockFinder(wanted)
    finder.feed(page)
    return "".join(finder.parts) if finder.done else ""


def _has_class(attrs: dict[str, str], *names: str) -> bool:
    classes = attrs.get("class", "").split()
    return all(name in classes for name in names)


# Where each site keeps the article body. Tried in order.
BODY_SELECTORS: list[Callable[[str, dict[str, str]], bool]] = [
    lambda tag, a: tag == "div" and _has_class(a, "TRS_UEDITOR"),
    lambda tag, a: tag == "div" and _has_class(a, "news-content"),
    lambda tag, a: tag == "div" and a.get("id", "").startswith("vsb_content"),
    lambda tag, a: tag == "div" and _has_class(a, "wzcon"),
    lambda tag, a: tag == "div" and _has_class(a, "text"),  # nifdc.org.cn
]

_QUESTION = re.compile(r"^\s*(?:问题\s*[0-9一二三四五六七八九十]*|问|\d+[.、．]|[一二三四五六七八九十]+[、.．])\s*[:：]?\s*(?P<q>.+)$")
_ANSWER = re.compile(r"^\s*(?:答复|答|回答)\s*[:：]\s*(?P<a>.*)$")
_QNUM = re.compile(r"^\s*(?:问题\s*[0-9一二三四五六七八九十]*\s*[:：]|问\s*[:：]|\d+[.、．]\s*|[一二三四五六七八九十]+[、.．]\s*)")


def split_qa(text: str) -> list[tuple[str, str]]:
    """Q&A collections written as `问题N：… 答：…` paragraphs -> (question, answer)
    pairs, numbering and the leading 答： removed. Empty when the text does not
    follow that pattern."""
    pairs: list[tuple[str, list[str]]] = []
    question: list[str] | None = None
    answer: list[str] | None = None
    current: list[str] | None = None
    for line in text.split("\n"):
        answered = _ANSWER.match(line)
        if answered and question is not None and answer is None:
            answer = [answered.group("a")] if answered.group("a") else []
            current = answer
            continue
        starts = _QNUM.match(line)
        if starts and (answer is not None or question is None):
            if question is not None and answer is not None:
                pairs.append((question, answer))
            question, answer, current = [line[starts.end():].strip()], None, None
            current = question
            continue
        if current is not None:
            current.append(line)
    if question is not None and answer is not None:
        pairs.append((question, answer))
    return [("\n".join(q).strip(), "\n".join(a).strip()) for q, a in pairs if "".join(q).strip() and "".join(a).strip()]


_NOT_DRUG = re.compile(r"化妆品|医疗器械|器械|数字疗法|体外诊断|保健食品|特殊食品|疫苗|生物制品|配方颗粒|饮片|干细胞")
_DRUG = re.compile(r"化学药|药品|制剂|药学|说明书|再注册|上市后|变更|仿制药")


def on_topic(title: str, body: str) -> bool:
    """The library keeps 化学药品制剂 material. Cosmetics/device/food items are
    dropped, as are articles that never mention drugs."""
    if _NOT_DRUG.search(title):
        return False
    if "中药" in title and "化学" not in title:
        return False
    return bool(_DRUG.search(title) or _DRUG.search(body))


def fetch_article_source(
    source: dict[str, Any], known: set[Any], since: str, get: Callable[[str], str] = http_get,
) -> tuple[list[Row], dict[str, Any]]:
    """Article listing -> ArticleRows for articles that are new. `known` holds
    both row keys and the article URLs already in the note."""
    if source.get("insecure_tls") and get is http_get:
        get = lambda url: http_get(url, verify=False)  # noqa: E731
    listing = parse_li_list(get(source["url"]), source["url"])
    if not listing:
        raise SafetyStop("column page lists no articles; page structure may have changed")
    rows: list[Row] = []
    skipped: list[dict[str, str]] = []
    filtered: list[dict[str, str]] = []
    for item in listing:
        if item.date <= since or item.url in known:
            continue
        try:
            page = get(item.url)
        except SafetyStop as exc:
            skipped.append({"url": item.url, "reason": str(exc)})
            continue
        body = ""
        for selector in BODY_SELECTORS:
            body = html_to_text(extract_block(page, selector))
            if body:
                break
        if not body:
            skipped.append({"url": item.url, "reason": "article body not found or empty"})
            continue
        if not on_topic(item.title, body):
            filtered.append({"url": item.url, "title": item.title, "reason": "不属于化学药品制剂主题"})
            continue
        pairs = split_qa(body) or [(item.title, body)]
        for question, answer in pairs:
            rows.append(ArticleRow(question, answer, item.date, item.title, item.url))
    meta: dict[str, Any] = {"remote_count": len(listing), "latest_date": max((i.date for i in listing), default=None)}
    if skipped:
        meta["skipped_items"] = skipped
    if filtered:
        meta["filtered_out"] = filtered
    return rows, meta


# ------------------------------------------------------------- 北京市药监局 咨询建议

_BJ_ITEM = re.compile(r"\{originalId:'(?P<id>[^']+)'.*?letterTitle:'(?P<title>[^']*)'.*?finishDateReal:'(?P<date>\d{4}-\d{2}-\d{2})'", re.S)
_BJ_NOT_DRUG = re.compile(r"器械|化妆品|护肤|牙膏|保健|食品|中药|中成药|饮片|体外诊断|试剂|植入|炮制")
# Retail, wholesale, internet sales, patient purchase questions are outside the library's scope.
_BJ_TRADE = re.compile(r"零售|批发|经营|网络销售|执业药师|医保|处方|平台|购买|患者|药店")
_BJ_DRUG = re.compile(r"药品|药物|药学|制剂|GMP|仿制|化学药|原料药|辅料|药包材|安慰剂")
_BJ_PHONE = re.compile(r"电话(?:已)?(?:回复|沟通)")
_BJ_TRAILING = re.compile(r"\s*北京市药品监督管理局\s*(?:\d{4}年\d{1,2}月\d{1,2}日)?\s*$")
_BJ_GREETING = re.compile(r"^网民您好！(?:关于您[^：:]*[：:])?")
_BJ_SIGNATURE = re.compile(r"^(北京市药品监督管理局.*|\d{4}年\d{1,2}月\d{1,2}日)$")


def parse_beijing_list(text: str) -> tuple[list[ListItem], int]:
    """(letters, total pages). The list endpoint returns JavaScript-object text."""
    pages = re.search(r"totalPages:'(\d+)'", text)
    items = [ListItem(m.group("id"), m.group("title"), m.group("date")) for m in _BJ_ITEM.finditer(text)]
    return items, int(pages.group(1)) if pages else 0


def parse_beijing_detail(page: str) -> tuple[str, str]:
    """(letter text, reply text without the closing signature and date)."""
    blocks = [html_to_text(m.group(1)) for m in re.finditer(
        r'<div[^>]*sino-text-format[^>]*>(.*?)</div>', page, re.S)]
    blocks = [b for b in blocks if b]
    if len(blocks) < 2:
        raise SafetyStop("letter page has no question/reply blocks")
    reply = [line for line in blocks[-1].split("\n") if not _BJ_SIGNATURE.match(line.strip())]
    text = "\n".join(reply).strip()
    return blocks[0], _BJ_TRAILING.sub("", text).strip()


def fetch_beijing(
    source: dict[str, Any], known: set[Any], since: str, get: Callable[[str], str] = http_get,
    max_pages: int = 40,
) -> tuple[list[Row], dict[str, Any]]:
    base = source["url"].rsplit("/", 1)[0] + "/"
    api = base + "bjah-index-dept!letterList.action?keyword=&page.pageNo={page}&page.pageSize=20"
    detail = base + "bjah-index-dept!detail.action?originalId={id}"
    rows: list[Row] = []
    skipped: list[dict[str, str]] = []
    seen = filtered = 0
    total_pages = 1
    page_no = 1
    while page_no <= min(total_pages, max_pages):
        items, total_pages = parse_beijing_list(get(api.format(page=page_no)))
        if not items:
            if page_no == 1:
                raise SafetyStop("Beijing letter list is empty; endpoint may have changed")
            break
        for item in items:
            seen += 1
            if item.date <= since:
                continue
            if _BJ_NOT_DRUG.search(item.title):
                filtered += 1
                continue
            try:
                question, answer = parse_beijing_detail(get(detail.format(id=item.url)))
            except SafetyStop as exc:
                skipped.append({"id": item.url, "reason": str(exc)})
                continue
            text = (item.title + question).replace("药品监督管理", "")
            on_topic_letter = _BJ_DRUG.search(text) and not _BJ_TRADE.search(text) and not _BJ_NOT_DRUG.search(text)
            # A reply that only says the office phoned the sender has no content to keep.
            phone_only = _BJ_PHONE.search(answer) and len(answer) < 300
            # "请致电…详询" and similar replies carry no content either.
            empty_reply = len(_BJ_GREETING.sub("", answer)) < 40
            if not on_topic_letter or phone_only or empty_reply:
                filtered += 1
                continue
            if (normalize(question), item.date) not in known:
                rows.append(Row(question, answer, item.date))
        if items[-1].date <= since:
            break
        page_no += 1
    meta: dict[str, Any] = {"remote_count": seen, "latest_date": max((r.date for r in rows), default=None), "filtered_count": filtered}
    if skipped:
        meta["skipped_items"] = skipped
    return rows, meta


# ------------------------------------------------------------ 国家药典委员会 (one note per article)

@dataclass(frozen=True)
class NewNote:
    """An article that becomes its own excerpt note under a source folder."""
    filename: str
    title: str
    date: str
    source_url: str
    external_url: str
    body: str
    # Question/answer pairs when the article is a Q&A collection; otherwise the
    # note has one row: title -> body.
    pairs: tuple[tuple[str, str], ...] = ()

    @property
    def rows(self) -> list[tuple[str, str]]:
        return list(self.pairs) or [(self.title, self.body)]


_CPC_SKIP = re.compile(r"培训|会议|预算|经销|出版|招标|采购")
CPC_NO_BODY = "原文为外部链接，当前国家药典委员会接口未提供正文；NMPA 等外部页面请点击外部链接查看原文。"


def fetch_cpc_notes(
    cpc: dict[str, Any], articles: list[Any], fetch_json: Callable[[str], Any] = get_json,
) -> tuple[list[NewNote], dict[str, Any]]:
    """Detail pages of the new CPC articles -> excerpt notes."""
    notes: list[NewNote] = []
    skipped: list[dict[str, str]] = []
    filtered: list[dict[str, str]] = []
    for article in articles:
        if _CPC_SKIP.search(article.title) or _NOT_DRUG.search(article.title):
            filtered.append({"title": article.title, "reason": "培训/会议/非药品主题"})
            continue
        try:
            payload = fetch_json(cpc["detail_url"].format(news_id=article.news_id))
        except Exception as exc:
            skipped.append({"title": article.title, "reason": str(exc)})
            continue
        container = payload.get("result") or payload.get("data") or payload
        data = container.get("news", container) if isinstance(container, dict) else {}
        html_body = str(data.get("newsContent") or "")
        body = html_to_text(html_body) if html_body.strip() not in ("", "None") else str(data.get("newsContentText") or "")
        body = "" if body.strip() == "None" else body
        external = str(data.get("toLinkIp") or "") if str(data.get("toLink")) == "1" else ""
        external = "" if external == "None" else external
        attachments = []
        for key in ("annexFileList", "annexPicList", "annexMediaList"):
            for item in data.get(key) or []:
                name = item.get("name") if isinstance(item, dict) else str(item).replace("\\", "/").rsplit("/", 1)[-1]
                if name:
                    attachments.append(str(name))
        if attachments:
            body = (body + "\n" if body else "") + "附件：\n" + "\n".join(f"- {name}" for name in attachments)
        if not body:
            body = CPC_NO_BODY
        notes.append(NewNote(
            article.filename, article.title, article.date,
            f"https://www.chp.org.cn/#/newsDetail?id={article.news_id}", external, body,
        ))
    meta: dict[str, Any] = {}
    if skipped:
        meta["skipped_items"] = skipped
    if filtered:
        meta["filtered_out"] = filtered
    return notes, meta


def note_text(note: NewNote, source_note_stem: str) -> str:
    """The excerpt note as the library stores it: frontmatter + one-row table."""
    q = lambda value: '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    lines = ["---", f'source_note: "[[{source_note_stem}]]"', f"source_url: {q(note.source_url)}"]
    if note.external_url:
        lines.append(f"external_url: {q(note.external_url)}")
    lines += [f"article_title: {q(note.title)}", f"date: {note.date}", "---", ""]
    table = "".join(f"| {markdown_cell(q_)} | {markdown_cell(a_)} | {note.date} |\n" for q_, a_ in note.rows)
    return "\n".join(lines) + "\n" + f"| 问题 | 解答 | 发布日期 |\n|---|---|---|\n{table}"


# ------------------------------------------------------------------ 安徽省药监局

_AH_ITEM = re.compile(
    r'<li[^>]*>\s*<a href="(?P<href>[^"]+)"[^>]*title="(?P<title>[^"]*)"[^>]*>.*?</a>\s*<span class="right date">(?P<date>\d{4}-\d{2}-\d{2})</span>',
    re.S,
)
_AH_PAGES = re.compile(r"pageCount:\s*(\d+)")
AH_NO_BODY = "正文未能自动提取（外部站点需访问校验）；请通过 source_url 查看原文。"


def safe_filename(date: str, title: str) -> str:
    title = re.sub(r"\s+", " ", html.unescape(title)).strip()
    return date + "_" + "".join("、" if c in '<>:"/\\|?*' else c for c in title) + ".md"


def fetch_anhui_notes(
    source: dict[str, Any], known: set[str], since: str,
    get: Callable[[str], str] = http_get, fetch_json: Callable[[str], Any] = get_json,
    max_pages: int = 10,
) -> tuple[list[NewNote], dict[str, Any]]:
    """Column list -> one NewNote per new, on-topic article. `known` holds the
    file names already in the note folders (kept and excluded)."""
    notes: list[NewNote] = []
    skipped: list[dict[str, str]] = []
    filtered: list[dict[str, str]] = []
    seen = 0
    pages = 1
    page_no = 1
    while page_no <= min(pages, max_pages):
        page = get(source["list_url"].format(page=page_no))
        if page_no == 1:
            found = _AH_PAGES.search(page)
            pages = int(found.group(1)) if found else 1
        items = [(m.group("href"), re.sub(r"\s+", " ", html.unescape(m.group("title"))).strip(), m.group("date"))
                 for m in _AH_ITEM.finditer(page)]
        if not items:
            if page_no == 1:
                raise SafetyStop("Anhui column page lists no articles; page structure may have changed")
            break
        seen += len(items)
        for href, title, date in items:
            if date <= since or safe_filename(date, title) in known:
                continue
            url = urllib.parse.urljoin(source["url"], href)
            try:
                body, external = _anhui_body(url, get, fetch_json, source)
            except SafetyStop as exc:
                skipped.append({"title": title, "reason": str(exc)})
                continue
            if not on_topic(title, body):
                filtered.append({"title": title, "reason": "不属于化学药品制剂主题"})
                continue
            pairs = tuple(split_qa(body))
            notes.append(NewNote(safe_filename(date, title), title, date, url, external, body or AH_NO_BODY, pairs))
        if items[-1][2] <= since:
            break
        page_no += 1
    meta: dict[str, Any] = {"remote_count": seen, "latest_date": max((n.date for n in notes), default=None)}
    if skipped:
        meta["skipped_items"] = skipped
    if filtered:
        meta["filtered_out"] = filtered
    return notes, meta


def _anhui_body(url: str, get, fetch_json, source: dict[str, Any]) -> tuple[str, str]:
    """(body text, external url) for a listed article, wherever it lives."""
    host = urllib.parse.urlsplit(url).netloc
    if host.endswith("chp.org.cn"):
        news_id = re.search(r"[?&]id=([^&#]+)", url)
        if not news_id:
            raise SafetyStop("CPC link has no id")
        payload = fetch_json(source["cpc_detail_url"].format(news_id=news_id.group(1)))
        container = payload.get("result") or payload.get("data") or payload
        data = container.get("news", container) if isinstance(container, dict) else {}
        raw = str(data.get("newsContent") or "")
        return (html_to_text(raw) if raw.strip() not in ("", "None") else ""), ""
    if host.endswith("ah.gov.cn"):
        page = get(url)
        for selector in BODY_SELECTORS:
            body = html_to_text(extract_block(page, selector))
            if body:
                return body, ""
        raise SafetyStop("article body not found")
    return "", url


# ------------------------------------------------ 江苏省药品审评中心 你问我答 (jspcc.org.cn)

_CST = timezone(timedelta(hours=8))


def _js_json(page: str, name: str, opener: str) -> Any:
    """The JSON literal assigned to `var <name> = ` in a page script."""
    start = page.find(f"var {name} =")
    if start < 0:
        raise SafetyStop(f"page has no {name} data")
    begin = page.index(opener, start)
    value, _ = json.JSONDecoder().raw_decode(page[begin:])
    return value


def _ms_date(value: Any) -> str:
    return datetime.fromtimestamp(int(value) / 1000, _CST).strftime("%Y-%m-%d")


_JSPCC_DEVICE = re.compile(r"有源|无源|内窥镜|软件|检测样本|产品技术要求|型号规格|许可事项变更|临床试验中")


def parse_jspcc_list(page: str, base: str) -> list[ListItem]:
    """`var articleData = [ {...}, {...}, ]` (with a trailing comma, so the
    objects are decoded one by one)."""
    start = page.find("var articleData")
    if start < 0:
        raise SafetyStop("page has no articleData")
    decoder = json.JSONDecoder()
    items: list[ListItem] = []
    position = page.index("[", start) + 1
    while True:
        begin = page.find("{", position)
        if begin < 0 or page.find("]", position) not in (-1,) and page.find("]", position) < begin:
            break
        item, position = decoder.raw_decode(page, begin)
        items.append(ListItem(urllib.parse.urljoin(base, f"/spzx/web/article/{item['id']}.html"), str(item["title"]), _ms_date(item["releaseDate"])))
    return items


def parse_jspcc_article(page: str) -> tuple[str, str, str, str]:
    """(title, question, answer, date). The body reads `问：… 答：…`; when it
    does not, the numbered title is the question and the whole text the answer."""
    article = _js_json(page, "article", "{")
    title = re.sub(r"^\s*\d+\s*[、.．]\s*", "", str(article["title"])).strip()
    text = html_to_text(re.sub(r"(?is)<style\b.*?</style>", "", str(article.get("content") or "")))
    match = re.search(r"问\s*[:：]\s*(?P<q>.*?)\n?\s*答\s*[:：]\s*(?P<a>.*)$", text, re.S)
    if match and match.group("q").strip() and match.group("a").strip():
        return title, match.group("q").strip(), match.group("a").strip(), _ms_date(article["releaseDate"])
    return title, title, text, _ms_date(article["releaseDate"])


def fetch_jspcc(
    source: dict[str, Any], known: set[Any], since: str, get: Callable[[str], str] = http_get, max_pages: int = 40,
) -> tuple[list[Row], dict[str, Any]]:
    rows: list[Row] = []
    skipped: list[dict[str, str]] = []
    seen = filtered = 0
    page_no = 1
    while page_no <= max_pages:
        try:
            items = parse_jspcc_list(get(source["list_url"].format(page=page_no)), source["url"])
        except SafetyStop:
            if page_no == 1:
                raise
            break  # past the last page the site answers 404
        if not items:
            if page_no == 1:
                raise SafetyStop("jspcc list is empty; page structure may have changed")
            break
        seen += len(items)
        for item in items:
            if item.date <= since:
                continue
            try:
                title, question, answer, date = parse_jspcc_article(get(item.url))
            except (SafetyStop, KeyError, ValueError) as exc:
                skipped.append({"url": item.url, "reason": str(exc)})
                continue
            if not answer or not on_topic(title, question) or _NOT_DRUG.search(question) or _JSPCC_DEVICE.search(title + question):
                filtered += 1
                continue
            if (normalize(question), date) not in known:
                rows.append(Row(question, answer, date))
        if items[-1].date <= since:
            break
        page_no += 1
    meta: dict[str, Any] = {"remote_count": seen, "latest_date": max((r.date for r in rows), default=None), "filtered_count": filtered}
    if skipped:
        meta["skipped_items"] = skipped
    return rows, meta


# ------------------------------------------------ 山东省药监局 “检”问百“答” (in 在线访谈)

_SD_ITEM = re.compile(r'<li><a title="(?P<title>[^"]*)" href="(?P<href>[^"]*)"[^>]*>.*?<span>(?P<date>\d{4}-\d{2}-\d{2})</span>', re.S)
_NUMBERED = re.compile(r"^(?:[一二三四五六七八九十百]+|\d+)$")


def split_numbered(text: str) -> list[tuple[str, str]]:
    """`一 / 问题 / 答案… / 二 / 问题 / 答案…` (a numeral alone on a line starts
    an item; its first line is the question)."""
    items: list[list[str]] = []
    for line in text.split("\n"):
        if _NUMBERED.match(line.strip()):
            items.append([])
        elif items:
            items[-1].append(line)
    return [(block[0].strip(), "\n".join(block[1:]).strip()) for block in items if len(block) > 1 and "".join(block[1:]).strip()]


def _shandong_page(source: dict[str, Any], page_no: int, get: Callable[[str], str]) -> list[ListItem]:
    query = urllib.parse.urlencode({**source["api_params"], "paramJson": json.dumps({"pageNo": page_no, "pageSize": 15})})
    payload = json.loads(get(f"{source['api_url']}?{query}"))
    fragment = str(((payload.get("data") or {}).get("html")) or "")
    return [ListItem(urllib.parse.urljoin(source["url"], m.group("href")), html.unescape(m.group("title")).strip(), m.group("date"))
            for m in _SD_ITEM.finditer(fragment)]


def fetch_shandong(
    source: dict[str, Any], known: set[Any], since: str, get: Callable[[str], str] = http_get, max_pages: int = 10,
) -> tuple[list[Row], dict[str, Any]]:
    """Only the “检”问百“答” series of the 在线访谈 column. Articles on the
    bureau's own site are read; WeChat links are behind a verification page and
    are reported as skipped."""
    rows: list[Row] = []
    skipped: list[dict[str, str]] = []
    filtered: list[dict[str, str]] = []
    seen = 0
    for page_no in range(1, max_pages + 1):
        items = _shandong_page(source, page_no, get)
        if not items:
            if page_no == 1:
                raise SafetyStop("Shandong column list is empty; interface may have changed")
            break
        seen += len(items)
        for item in items:
            if item.date <= since or item.url in known or "问百" not in item.title:
                continue
            if _NOT_DRUG.search(item.title):
                filtered.append({"title": item.title, "reason": "不属于化学药品制剂主题"})
                continue
            if "mp.weixin.qq.com" in item.url:
                skipped.append({"title": item.title, "url": item.url, "reason": "微信文章需要验证，无法自动提取，请人工查看"})
                continue
            try:
                page = get(item.url)
            except SafetyStop as exc:
                skipped.append({"title": item.title, "url": item.url, "reason": str(exc)})
                continue
            body = html_to_text(extract_block(page, lambda tag, a: tag == "div" and _has_class(a, "wen")))
            if not body:
                skipped.append({"title": item.title, "url": item.url, "reason": "article body not found or empty"})
                continue
            if not on_topic(item.title, body):
                filtered.append({"title": item.title, "reason": "不属于化学药品制剂主题"})
                continue
            for question, answer in split_numbered(body) or [(item.title, body)]:
                rows.append(ArticleRow(question, answer, item.date, item.title, item.url))
        if items[-1].date <= since:
            break
    meta: dict[str, Any] = {"remote_count": seen, "latest_date": max((r.date for r in rows), default=None)}
    if skipped:
        meta["skipped_items"] = skipped
    if filtered:
        meta["filtered_out"] = filtered
    return rows, meta


def fetch_jiangsu_articles(
    source: dict[str, Any], known: set[Any], since: str, get: Callable[[str], str] = http_get,
) -> tuple[list[Row], dict[str, Any]]:
    """Jiangsu column of explanatory articles (药小问普法讲堂): not Q&A rows but
    articles, so they go in as `### [标题](链接)（日期）` sections. Only articles
    whose title is about drugs' registration/change/label matters are kept."""
    listing = parse_jiangsu_list(get(source["url"]), source["url"])
    if not listing:
        raise SafetyStop("Jiangsu column page lists no articles; page structure may have changed")
    rows: list[Row] = []
    skipped: list[dict[str, str]] = []
    filtered: list[dict[str, str]] = []
    for item in listing:
        if item.date <= since or item.url in known:
            continue
        if _NOT_DRUG.search(item.title) or _BJ_TRADE.search(item.title) or not _DRUG.search(item.title):
            filtered.append({"title": item.title, "reason": "标题不属于化学药品制剂注册/生产/质量/变更主题"})
            continue
        try:
            title, answer, _ = parse_jiangsu_article(get(item.url))
        except SafetyStop as exc:
            skipped.append({"url": item.url, "reason": str(exc)})
            continue
        if not answer or _NOT_DRUG.search(answer[:200]):
            filtered.append({"title": item.title, "reason": "正文为空或不属于化学药品制剂主题"})
            continue
        for question, reply in split_qa(answer) or [(item.title, answer)]:
            rows.append(ArticleRow(question, reply, item.date, item.title, item.url))
    meta: dict[str, Any] = {"remote_count": len(listing), "latest_date": max((i.date for i in listing), default=None)}
    if skipped:
        meta["skipped_items"] = skipped
    if filtered:
        meta["filtered_out"] = filtered
    return rows, meta


def fetch_shanghai_all(source: dict[str, Any], known: set[tuple[str, str]], since: str) -> tuple[list[Row], dict[str, Any]]:
    """Shanghai serves the whole Q&A list as JSON in one request."""
    return fetch_shanghai(source["url"])


FILE_FETCHERS: dict[str, Callable[..., tuple[list[NewNote], dict[str, Any]]]] = {
    "anhui": fetch_anhui_notes,
}


FETCHERS: dict[str, Callable[..., tuple[list[Row], dict[str, Any]]]] = {
    "jiangsu": fetch_jiangsu,
    "shanghai": fetch_shanghai_all,
    "articles": fetch_article_source,
    "beijing": fetch_beijing,
    "jspcc": fetch_jspcc,
    "shandong": fetch_shandong,
    "jiangsu_articles": fetch_jiangsu_articles,
}
