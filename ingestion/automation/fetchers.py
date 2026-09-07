from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import Row, SafetyStop


class CDEBrowserUnavailable(SafetyStop):
    def __init__(self, message: str, diagnostics: dict[str, Any]):
        super().__init__(message)
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class CPCArticle:
    news_id: str
    title: str
    date: str
    filename: str


def get_json(url: str, timeout: int = 45) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "dek-source-ingest/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise SafetyStop(f"HTTP {response.status} from configured endpoint")
        return json.load(response)


def fetch_shanghai(url: str) -> tuple[list[Row], dict[str, Any]]:
    payload = get_json(url)
    data = payload.get("data", payload)
    records = data.get("records")
    if not isinstance(records, list):
        raise SafetyStop("Shanghai response has no records list")
    total = int(data.get("total", len(records)))
    if total > len(records):
        raise SafetyStop(f"Shanghai response truncated: {len(records)}/{total}")
    rows = [Row(str(x["question"]), str(x["answer"]), str(x["createDate"])[:10]) for x in records]
    return rows, {"remote_count": total, "latest_date": max((r.date for r in rows), default=None)}


def fetch_cpc(url: str) -> tuple[list[CPCArticle], dict[str, Any]]:
    payload = get_json(url)
    data = payload.get("result") or payload.get("data") or payload
    page = data.get("pagelist", data)
    records = page.get("records") or page.get("list") or page.get("content")
    if not isinstance(records, list):
        raise SafetyStop("CPC response has no recognized records list")
    articles: list[CPCArticle] = []
    dates: list[str] = []
    for item in records:
        title = str(item.get("title") or item.get("newsTitle") or "").strip()
        news_id = str(item.get("id") or item.get("newsId") or "").strip()
        day = str(item.get("newsTime") or item.get("publishTime") or item.get("releaseTime") or item.get("publishDate") or "")[:10]
        if not news_id or not title or not day:
            raise SafetyStop("CPC record lacks id/title/date")
        safe = "".join("、" if c in '<>:"/\\|?*' else c for c in title)
        safe = "".join(safe.split())
        articles.append(CPCArticle(news_id, title, day, f"{day}_{safe}.md"))
        dates.append(day)
    if len({article.filename for article in articles}) != len(articles):
        raise SafetyStop("CPC list contains duplicate normalized filenames")
    return articles, {"remote_count": len(records), "latest_date": max(dates, default=None)}


CPC_CONTENT_HASH_VERSION = "cpc-source-content-v2"


def normalize_cpc_content(value: str) -> str:
    """Normalize non-semantic HTML formatting while preserving substantive text."""
    import html

    value = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ", value or "")
    value = re.sub(r"(?i)<(?:br|hr)\s*/?>", " ", value)
    value = re.sub(r"(?i)</?(?:p|div|li|tr|td|th|h[1-6]|section|article)\b[^>]*>", " ", value)
    value = html.unescape(re.sub(r"<[^>]+>", "", value))
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", value).strip()


def normalize_cpc_attachment_name(value: str) -> str:
    value = normalize_cpc_content(value).strip()
    value = value.translate(str.maketrans({"／": "/", "。": ".", "．": "."}))
    return re.sub(r"(?i)(\.[a-z0-9]+)$", lambda match: match.group(1).lower(), value)


def _cpc_detail_object(payload: Any, filename: str) -> dict[str, Any]:
    container = payload.get("result") or payload.get("data") or payload
    if not isinstance(container, dict):
        raise SafetyStop(f"CPC detail is not an object: {filename}")
    if "news" in container:
        if not isinstance(container["news"], dict):
            raise SafetyStop(f"CPC nested news is not an object: {filename}")
        return container["news"]
    return container


def _cpc_detail_components(payload: Any, filename: str) -> tuple[str, list[dict[str, str]]]:
    data = _cpc_detail_object(payload, filename)
    body = normalize_cpc_content(str(data.get("newsContentText") or data.get("newsContent") or ""))
    if not body:
        raise SafetyStop(f"CPC detail has no non-empty normalized body: {filename}")
    attachments = []
    for key in ("annexFileList", "annexPicList", "annexMediaList"):
        for item in data.get(key) or []:
            name = normalize_cpc_attachment_name(str(item.get("name") or ""))
            stable_id = str(item.get("id") or "").strip()
            if not name:
                raise SafetyStop(f"CPC attachment has no name: {filename}")
            attachment = {"name": name}
            if stable_id:
                attachment["stable_id"] = stable_id
            attachments.append(attachment)
    attachments.sort(key=lambda item: (item.get("stable_id", ""), item["name"]))
    return body, attachments


def strip_markdown_link_targets(value: str) -> str:
    """Keep Markdown link labels while excluding destination URL noise."""
    return re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", value or "")


def split_cpc_local_excerpt(value: str) -> tuple[str, list[str]]:
    """Separate a local excerpt body from its explicit attachment section."""
    parts = re.split(r"附件\s*[:：]", value or "", maxsplit=1)
    body = normalize_cpc_content(strip_markdown_link_targets(parts[0]))
    if len(parts) == 1:
        return body, []
    attachment_text = re.split(r"附件\s*[《<].*?[》>]\s*文本\s*[:：]", parts[1], maxsplit=1)[0]
    link_names = re.findall(r"\[([^]]+)\]\([^)]*\)", attachment_text)
    if link_names:
        return body, sorted(normalize_cpc_attachment_name(name) for name in link_names)
    lines = re.sub(r"(?i)<br\s*/?>", "\n", attachment_text).splitlines()
    names = []
    started = False
    for line in lines:
        match = re.match(r"^\s*[-*+]\s+(.+?)\s*$", line)
        if match:
            started = True
            names.append(normalize_cpc_attachment_name(match.group(1)))
        elif started:
            break
        elif line.strip():
            break
    return body, sorted(name for name in names if name)


def validate_cpc_local_excerpt(local_excerpt: str, payload: Any, filename: str) -> None:
    """Validate body and attachment names separately; URL targets are irrelevant."""
    from collections import Counter

    local_body, local_names = split_cpc_local_excerpt(local_excerpt)
    remote_body, remote_attachments = _cpc_detail_components(payload, filename)
    compact = lambda value: re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value).lower()
    local_compact, remote_compact = compact(local_body), compact(remote_body)
    if not local_compact or local_compact not in remote_compact:
        raise SafetyStop(f"CPC local body is not a verified remote-body subset: {filename}")
    numbers = lambda value: Counter(re.findall(r"\d+(?:[.-]\d+)*", value))
    local_numbers, remote_numbers = numbers(local_body), numbers(remote_body)
    if any(remote_numbers[token] < count for token, count in local_numbers.items()):
        raise SafetyStop(f"CPC body numeric/date/standard identifier mismatch: {filename}")
    remote_names = [item["name"] for item in remote_attachments]
    if Counter(local_names) != Counter(remote_names):
        raise SafetyStop(f"CPC attachment names differ: {filename}")


def fetch_cpc_content_hash(detail_url: str, article: CPCArticle) -> str:
    import hashlib

    payload = get_json(detail_url.format(news_id=article.news_id))
    content, attachments = _cpc_detail_components(payload, article.filename)
    canonical = {
        "algorithm": CPC_CONTENT_HASH_VERSION,
        "title": normalize_cpc_content(article.title),
        "date": article.date[:10],
        "body": content,
        "attachments": attachments,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def assert_cpc_baseline_workspace_safe(root: Path, expected_cpc_changes: set[str] | None = None) -> None:
    """Allow only known commissioning files plus an explicit CPC baseline file set."""
    expected = expected_cpc_changes or set()
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root, text=True, capture_output=True,
    )
    if proc.returncode:
        raise SafetyStop(proc.stderr.strip() or "cannot inspect baseline workspace")
    unexpected = []
    for line in proc.stdout.splitlines():
        status, path = line[:2], line[3:]
        commissioning = status == "??" and (
            path == "requirements-ingestion.txt"
            or path.startswith("deploy/")
            or path.startswith("ingestion/automation/")
        )
        approved_cpc = path in expected and path.startswith("source/CPC/CPC_《中国药典》执行专栏/")
        if not commissioning and not approved_cpc:
            unexpected.append(line)
    if unexpected:
        raise SafetyStop(f"unexpected baseline workspace changes: {unexpected}")


def _full_chromium() -> Path:
    configured = os.environ.get("DEK_CHROMIUM_EXECUTABLE")
    candidates = [Path(configured)] if configured else []
    candidates.extend(sorted((Path.home() / ".cache" / "ms-playwright").glob("chromium-*/chrome-linux/chrome"), reverse=True))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise SafetyStop("full Playwright Chromium executable was not found")


def _safe_public_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def fetch_cde(url: str, types: list[int], profile_dir: Path) -> tuple[dict[int, tuple[list[Row], dict[str, Any]]], dict[str, Any]]:
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as exc:
        raise SafetyStop("Playwright is not installed") from exc
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_dir.chmod(0o700)
    result: dict[int, tuple[list[Row], dict[str, Any]]] = {}
    diagnostics: dict[str, Any] = {"navigation_statuses": [], "challenge_resources": [], "final_url": None, "myAjax_is_function": False}
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir), executable_path=str(_full_chromium()), headless=False,
            locale="zh-CN", timezone_id="Asia/Shanghai", viewport={"width": 1365, "height": 768},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            def observe(response: Any) -> None:
                item = {"method": response.request.method, "url": _safe_public_url(response.url), "status": response.status, "resource_type": response.request.resource_type}
                if response.request.resource_type == "document":
                    diagnostics["navigation_statuses"].append(item)
                elif response.request.resource_type == "script" and response.url.startswith("https://www.cde.org.cn/"):
                    diagnostics["challenge_resources"].append(item)
            page.on("response", observe)
            response = page.goto(url, wait_until="domcontentloaded", timeout=120000)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if any(x["status"] == 400 for x in diagnostics["challenge_resources"]):
                    break
                try:
                    if page.evaluate("typeof myAjax === 'function'"):
                        break
                except PlaywrightError:
                    # The challenge may replace the main document in-place.
                    # Wait for that site-initiated navigation to settle.
                    page.wait_for_timeout(250)
                    continue
                page.wait_for_timeout(250)
            diagnostics["final_url"] = _safe_public_url(page.url)
            try:
                diagnostics["myAjax_is_function"] = page.evaluate("typeof myAjax === 'function'")
            except PlaywrightError:
                diagnostics["myAjax_is_function"] = False
            if any(x["status"] == 400 for x in diagnostics["challenge_resources"]):
                raise CDEBrowserUnavailable("CDE challenge resource returned HTTP 400", diagnostics)
            if not diagnostics["myAjax_is_function"]:
                status = response.status if response else None
                raise CDEBrowserUnavailable(f"CDE browser initialization unavailable (initial HTTP {status})", diagnostics)
            for kind in types:
                payload = page.evaluate("""async (kind) => {
                  const request = pageNum => new Promise((resolve, reject) => {
                    try { myAjax('/xxgk/getCommonQuestionList', {pageSize: 100, pageNum, probleContent: '', probleType: kind}, 'post').done(resolve).fail(e => reject(String(e))); }
                    catch (e) { reject(String(e)); }
                  });
                  const first = await request(1); const d = first.data || {};
                  const records = [...(d.records || [])];
                  for (let n = 2; n <= (d.pages || 1); n++) records.push(...((await request(n)).data.records || []));
                  return {records, total: d.total || records.length};
                }""", kind)
                records = payload.get("records", [])
                if int(payload.get("total", 0)) != len(records):
                    raise SafetyStop(f"CDE type {kind} response count mismatch")
                rows = [Row(str(x["probleContent"]), str(x["answerContent"]), str(x["publishTime"])[:10]) for x in records]
                result[kind] = (rows, {"remote_count": len(rows), "latest_date": max((r.date for r in rows), default=None)})
        finally:
            context.close()
    return result, diagnostics
