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
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from .core import Row, SafetyStop, normalize
from .fetchers import fetch_shanghai, http_get

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


def fetch_shanghai_all(source: dict[str, Any], known: set[tuple[str, str]], since: str) -> tuple[list[Row], dict[str, Any]]:
    """Shanghai serves the whole Q&A list as JSON in one request."""
    return fetch_shanghai(source["url"])


FETCHERS: dict[str, Callable[..., tuple[list[Row], dict[str, Any]]]] = {
    "jiangsu": fetch_jiangsu,
    "shanghai": fetch_shanghai_all,
}
