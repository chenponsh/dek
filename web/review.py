"""Fail-closed reviewer authorization and append-only decision intake."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import stat
import subprocess
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import parse_qs, urlencode

import yaml
from deploy.release_bundle import APPROVAL_ID_PATTERN

from .suggest import question_key, read_suggestion, rough_qa, suggest_folders, wiki_questions


HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(r"^sha256:[A-Za-z0-9._:-]{1,200}$")
ROUGH_PREFIX = PurePosixPath("ingestion/rough")
WIKI_PREFIX = PurePosixPath("wiki")
MAX_DECISION_BYTES = 1_048_576
BEIJING = timezone(timedelta(hours=8))


def _display_time(value: str) -> str:
    """Render a stored UTC ISO-8601 timestamp as Beijing wall-clock time.

    The stored value (audit records, decision MAC input) stays UTC; only the
    reviewer-facing table cells switch to +08:00 for readability.
    """
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


class ReviewError(RuntimeError):
    def __init__(self, message: str, status: str = "400 Bad Request"):
        super().__init__(message)
        self.status = status


class IsolatedReviewClone:
    """Refresh a reviewer-owned clone from an ingestion-produced Git bundle."""
    def __init__(self,bundle:Path,clones:Path,retain:int=3,bundle_archive:Path|None=None):
        self.bundle=Path(bundle); self.clones=Path(clones); self.retain=retain; self._digest=""; self._current:Path|None=None
        self.bundle_archive=Path(bundle_archive) if bundle_archive else None

    def current(self)->Path:
        descriptor=os.open(self.bundle,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
        try:
            details=os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size>2*1024*1024*1024: raise ReviewError("review bundle invalid","503 Service Unavailable")
            chunks=[]; total=0
            while True:
                chunk=os.read(descriptor,65536)
                if not chunk: break
                chunks.append(chunk); total+=len(chunk)
                if total>2*1024*1024*1024: raise ReviewError("review bundle too large","503 Service Unavailable")
            raw=b"".join(chunks)
        finally: os.close(descriptor)
        digest=hashlib.sha256(raw).hexdigest()
        if self.bundle_archive is not None:
            archive=self.bundle_archive/(digest+".bundle")
            if not archive.exists():
                self.bundle_archive.mkdir(parents=True,exist_ok=True)
                temporary=self.bundle_archive/(".bundle-"+digest)
                temporary.unlink(missing_ok=True)
                with temporary.open("xb") as handle: handle.write(raw); handle.flush(); os.fsync(handle.fileno())
                os.chmod(temporary,0o440)
                os.replace(temporary,archive)
        if digest==self._digest and self._current is not None: return self._current
        self.clones.mkdir(parents=True,exist_ok=True); target=self.clones/digest
        if not target.exists():
            temporary=self.clones/(".clone-"+digest)
            pinned=self.clones/(".bundle-"+digest)
            if temporary.exists():
                import shutil; shutil.rmtree(temporary)
            pinned.unlink(missing_ok=True)
            with pinned.open("xb") as handle: handle.write(raw); handle.flush(); os.fsync(handle.fileno())
            os.chmod(pinned,0o400)
            command=["/usr/bin/git","--no-pager","-c","core.hooksPath=/dev/null","-c","credential.helper=","-c","credential.interactive=never","-c","core.fsmonitor=false","-c","core.sshCommand=","-c","diff.external=","-c","protocol.allow=never","-c","protocol.file.allow=always","clone","--branch","main","--no-local","--no-hardlinks","--",str(pinned),str(temporary)]
            environment={"HOME":"/var/empty/dek-review","PATH":"/usr/bin:/bin","LANG":"C.UTF-8","LC_ALL":"C.UTF-8","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_SYSTEM":"/dev/null","GIT_CONFIG_GLOBAL":"/dev/null","GIT_ATTR_NOSYSTEM":"1","GIT_TERMINAL_PROMPT":"0","GIT_ASKPASS":"/bin/false","SSH_ASKPASS":"/bin/false"}
            completed=subprocess.run(command,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60,check=False)
            if completed.returncode: raise ReviewError("review clone failed","503 Service Unavailable")
            os.replace(temporary,target)
            pinned.unlink()
        self._digest,self._current=digest,target
        old_clones=sorted((p for p in self.clones.iterdir() if p.is_dir() and not p.name.startswith(".")),key=lambda p:p.stat().st_mtime_ns,reverse=True)
        for old in old_clones[self.retain:]:
            import shutil; shutil.rmtree(old)
        return target


@dataclass(frozen=True)
class RoughBinding:
    path: str
    sha256: str
    version: str
    content: str


STATUS_LABELS = {
    "pending": "待审核",
    "approved": "已批准待发布",
    "rejected": "已拒绝",
    "published": "已发布",
}

ACTION_STATUS = {"approve": "approved", "reject": "rejected"}
ACTION_LABEL = {"approve": "批准发布", "return": "退回澄清", "reject": "拒绝"}
PAGE_SIZE = 15
MIN_PAGE_SIZE, MAX_PAGE_SIZE = 5, 100


def list_state_query(status: str = "", page: int = 1, query: str = "", size: int = PAGE_SIZE) -> str:
    """The list position as a query string ("" at the default position)."""
    params = []
    if status: params.append(("status", status))
    if query: params.append(("q", query))
    if page > 1: params.append(("page", str(page)))
    if size != PAGE_SIZE: params.append(("page_size", str(size)))
    return "?" + urlencode(params) if params else ""


_WIKILINK_NAME = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def _short_source(source: str) -> str:
    """"[[source/CDE/CDE_x|alias]]" -> "CDE_x": the note's own name is enough in a table cell.
    An item listed under two columns names both notes: "CDE_x、CDE_y"."""
    names = _WIKILINK_NAME.findall(source)
    if len(names) > 1:
        return "、".join(name.strip().rstrip("/").rsplit("/", 1)[-1] for name in names)
    name = source.strip().strip("[]").split("|", 1)[0].rstrip("/")
    return name.rsplit("/", 1)[-1] or source


QA_TABLE = re.compile(r"^\|[ \t]*问题[ \t]*\|[ \t]*解答[ \t]*\|[ \t]*发布日期[ \t]*\|[ \t]*\n\|[ \t:|-]+\|[ \t]*\n((?:\|[^\n]*(?:\n|\Z))*)", re.M)


QUESTION_LABELLED = re.compile(r"\s*(?:问题?|Q)\s*[0-9一二三四五六七八九十]*\s*[:：]", re.I)
ANSWER_LABELLED = re.compile(r"\s*(?:答案?|解答|回答|A)\s*[:：]", re.I)


def _qa_lines(body: str) -> str:
    """Turn the draft's | 问题 | 解答 | 发布日期 | table into 问：/答：/日期： lines (a text that already carries its own label keeps just that)."""
    table = QA_TABLE.search(body)
    if not table:
        return body
    blocks = []
    for line in table.group(1).splitlines():
        cells = [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
        if len(cells) == 3:
            question, answer, date = cells
            answer = re.sub(r'<br\s*/?>', chr(10), answer)
            # 问：/答：, the way the source words them; text that already starts with its own label keeps it.
            question_line = question if QUESTION_LABELLED.match(question) else f"问：{question}"
            answer_line = answer if ANSWER_LABELLED.match(answer) else f"答：{answer}"
            blocks.append(f"{question_line}\n{answer_line}\n日期：{date}")
    if not blocks:
        return body
    before = re.sub(r"(?m)^##[ \t]*新增问答[ \t]*\n*\Z", "", body[:table.start()])
    after = body[table.end():].strip("\n")
    return (before.rstrip("\n") + "\n\n" if before.strip() else "") + "\n\n".join(blocks) + ("\n\n" + after if after else "")


def rough_display(content: str, suggestion: str = "") -> str:
    """The text of a rough draft for the 原文 box: its Q&A as readable lines, then the suggested wiki path.

    The frontmatter itself stays out of the box and English on disk (ingestion,
    audit and the publisher read it by name). The suggested path is shown the
    way the form below is prefilled with it.
    """
    match = re.match(r"\A---\s*\n(.*?)\n---(?:[ \t]*\n|\Z)\n*", content, re.S)
    if not match:
        return content
    try:
        meta = _frontmatter(content)
    except ReviewError:
        return content
    raw = meta.get("wiki_target")
    written = "" if raw is None else str(raw).strip()
    shown = default_wiki_path(written) or written or suggestion or "暂无"
    return _qa_lines(content[match.end():]).rstrip("\n") + "\n\n建议路径：" + shown


def _question_text(item) -> str:
    """The draft's question on one line, without its own 问：/问题： label ("" when it has none)."""
    question = re.sub(r"\s+", " ", rough_qa(getattr(item, "content", "") or "")[0]).strip()
    return QUESTION_LABELLED.sub("", question, count=1).strip()


def _item_title(item) -> str:
    """What the item page is headed with: the question itself, else the file name."""
    return _question_text(item) or item.title


def _reviewer_cell(item) -> str:
    """Two lines: who decided, and when. Nothing decided yet: a single dash."""
    when = _display_time(item.decided_at)
    if not item.reviewer and not when:
        return "—"
    line = f'<div class="reviewer-name">{html.escape(item.reviewer or "—")}</div>'
    return line + (f'<div class="meta reviewer-time">{html.escape(when)}</div>' if when else "")


def _content_cell(item, href: str) -> str:
    """The list's content cell, three lines: the question (the link into the item), the
    date, and the draft's file name as a note. Each line wraps when it is long, so nothing is cut off."""
    question = re.sub(r"\s+", " ", rough_qa(getattr(item, "content", "") or "")[0]).strip()
    # A question that already starts with its own 问：keeps it, otherwise it gets one.
    first = (question if QUESTION_LABELLED.match(question) else f"问：{question}") if question else item.title
    lines = [f'<a class="content-title" href="{href}" title="{html.escape(first, quote=True)}">{html.escape(first)}</a>']
    if item.published_date:
        lines.append(f'<div class="meta content-meta">日期：{html.escape(item.published_date)}</div>')
    lines.append(f'<div class="meta content-meta content-note" title="{html.escape(item.path, quote=True)}">备注：{html.escape(PurePosixPath(item.path).name)}</div>')
    return "".join(lines)


def _page_window(page: int, pages: int) -> list[int | None]:
    if pages <= 7:
        return list(range(1, pages + 1))
    shown = sorted(n for n in {1, pages, page - 1, page, page + 1} if 1 <= n <= pages)
    window: list[int | None] = []
    previous = 0
    for number in shown:
        if number - previous > 1: window.append(None)
        window.append(number); previous = number
    return window

NICKNAME_LIMIT = 40
NICKNAME_FALLBACK = "同事"


def sanitize_nickname(value: object, *, fallback: str = NICKNAME_FALLBACK, limit: int = NICKNAME_LIMIT) -> str:
    """Reduce a DingTalk display name to a bounded, printable, HTML-safe label."""
    if not isinstance(value, str):
        return fallback
    cleaned = "".join(character for character in value if character.isprintable())
    cleaned = cleaned.strip()
    if not cleaned:
        return fallback
    return cleaned[:limit]


def item_identity(relative: str) -> str:
    """Return the opaque, stable identifier used in reviewer URLs."""
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]


def default_wiki_path(value: object) -> str:
    """Suggest the wiki note path a rough targets, or '' when it is unusable."""
    if not isinstance(value, str):
        return ""
    raw = value.strip().strip('"').strip("'").strip()
    if raw.startswith("[[") and raw.endswith("]]"):
        raw = raw[2:-2].strip()
    if not raw:
        return ""
    if not raw.endswith(".md"):
        raw += ".md"
    try:
        validate_relative_path(raw, WIKI_PREFIX)
    except ReviewError:
        return ""
    return raw


def wiki_folder_candidates(root: Path, taken: frozenset[str] | set[str] = frozenset()) -> list[tuple[str, str]]:
    """For every wiki folder that already holds numbered notes, return
    (display label, suggested next path) so the reviewer can search by
    folder name instead of typing the whole path by hand.

    `taken` holds paths already promised to approved-but-unpublished drafts; a
    suggestion never lands on one, so two drafts approved before either is
    published do not both get the same number."""
    wiki_root = root / "wiki"
    if not wiki_root.is_dir():
        return []
    groups: dict[str, dict[str, tuple[int, int]]] = {}
    for path in sorted(wiki_root.rglob("*.md")):
        stem = path.stem
        if "-" not in stem:
            continue
        prefix, _, number = stem.rpartition("-")
        if not number.isdigit():
            continue
        folder = path.parent.relative_to(root).as_posix()
        bucket = groups.setdefault(folder, {})
        value = int(number)
        current = bucket.get(prefix)
        if current is None or value > current[0]:
            bucket[prefix] = (value, len(number))
    candidates = []
    for folder in sorted(groups):
        prefix, (max_number, width) = max(groups[folder].items(), key=lambda item: item[1][0])
        label = folder[len("wiki/"):] if folder.startswith("wiki/") else folder
        number = max_number + 1
        while f"{folder}/{prefix}-{number:0{width}d}.md" in taken:
            number += 1
        candidates.append((label, f"{folder}/{prefix}-{number:0{width}d}.md"))
    return candidates


def suggest_wiki_paths(root: Path, content: str, candidates: list[tuple[str, str]] | None = None, limit: int = 3, preferred: str = "") -> list[tuple[str, str]]:
    """(folder label, next path) for the folders most like this draft, best first.

    `preferred` is a folder a model chose; it leads only if it is a real folder
    in this snapshot, and the similarity picks follow it (one more chip).
    """
    question, answer, _ = _rough_row(content)
    if not question and not answer:
        return []
    next_path = {f"wiki/{label}": path for label, path in (candidates if candidates is not None else wiki_folder_candidates(root))}
    folders = suggest_folders(root, question, answer, limit)
    if preferred in next_path:
        folders = [preferred] + [folder for folder in folders if folder != preferred]
        limit += 1
    return [(folder[len("wiki/"):], next_path[folder]) for folder in folders[:limit] if folder in next_path]


def _yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"%s"' % escaped


def _table_cells(line: str) -> list[str]:
    inner = line.strip().strip("|")
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", inner)]


def _rough_row(content: str) -> tuple[str, str, str]:
    """Return the first (question, answer, date) row of the rough Q&A table."""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= set("|-: "):
            continue
        cells = _table_cells(stripped)
        if len(cells) < 3 or cells[0] in {"问题", "解答"}:
            continue
        return tuple(_cell_text(cell) for cell in cells[:3])  # type: ignore[return-value]
    return "", "", ""


def _cell_text(value: str) -> str:
    return value.replace("<br>", "\n").replace("\\|", "|").strip()


def candidate_draft(content: str, wiki_path: str) -> str:
    """Build an editable wiki-note draft for the reviewer from a rough note."""
    meta = _frontmatter(content)
    question, answer, published = _rough_row(content)
    if not question:
        question = str(meta.get("question") or "").strip()
    if not published:
        published = str(meta.get("published_date") or meta.get("date") or "").strip()
    source = str(meta.get("source") or "").strip()
    tag_pages = [str(item).strip() for item in (meta.get("recommended_tags") or []) if str(item).strip()]
    segments = PurePosixPath(wiki_path).parts[1:-1] if wiki_path else ()
    tags = ["/".join(segments[:index + 1]) for index in range(len(segments))]
    if not tag_pages:
        tag_pages = ['"[[%s]]"' % tag.split("/")[-1] for tag in tags]
    stem = PurePosixPath(wiki_path).stem if wiki_path else ""
    number = stem.rsplit("-", 1)[-1] if "-" in stem else ""
    lines = ["---", "no: %s" % (number.lstrip("0") or number or ""), "date: %s" % published, "question: %s" % _yaml_scalar(question)]
    if source:
        lines.append("source: %s" % _yaml_scalar(source))
    else:
        lines.append("source:")
    source_url = str(meta.get("source_url") or "").strip()
    if re.fullmatch(r"https?://[^\s\"]+", source_url):
        lines.append('source_url: "%s"' % source_url)      # the official page of this very question
    lines.append("tag_pages:")
    lines.extend("  - %s" % page if page.startswith('"') else "  - %s" % _yaml_scalar(page) for page in tag_pages)
    lines.append("tags:")
    lines.extend('  - "%s"' % tag for tag in tags)
    lines.append("---")
    lines.append("")
    lines.append(answer)
    lines.append("")
    return "\n".join(lines)


def _wikilink_names(value: object) -> list[str]:
    """Every source note a draft names ("[[a]] [[b]]"), or its one bare name."""
    if not isinstance(value, str):
        return []
    found = [name.strip() for name in _WIKILINK_NAME.findall(value)]
    if found:
        return found
    name = _wikilink_name(value)
    return [name] if name else []


def _wikilink_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip().strip('"').strip("'").strip()
    first = _WIKILINK_NAME.search(raw)
    if first:
        return first.group(1).strip()
    if raw.startswith("[[") and raw.endswith("]]"):
        raw = raw[2:-2].strip()
    return raw


URL_PATTERN = re.compile(r"https?://[^\s)\]<>\"'，。；]+")
MAX_SOURCE_URLS = 5
MAX_SOURCE_NOTE_BYTES = 262144


def _http_urls(text: str) -> list[str]:
    return [value.rstrip(".,;") for value in URL_PATTERN.findall(text)]


def _source_note(root: Path, name: str) -> tuple[Path | None, dict]:
    """Resolve a rough/source wikilink to a source note inside the snapshot."""
    if not name:
        return None, {}
    source_root = root / "source"
    candidate: Path | None = None
    if name.startswith("source/"):
        candidate = root / (name if name.endswith(".md") else name + ".md")
    elif source_root.is_dir():
        for path in sorted(source_root.rglob("*.md")):
            if path.stem == name:
                candidate = path
                break
    if candidate is None or not candidate.is_file():
        return None, {}
    try:
        with candidate.open("rb") as handle:
            raw = handle.read(MAX_SOURCE_NOTE_BYTES)
        meta = _frontmatter(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ReviewError):
        return candidate, {}
    return candidate, meta


def source_urls(content: str, root: Path) -> list[dict]:
    """Collect the upstream source links a reviewer needs to verify an item."""
    try:
        meta = _frontmatter(content)
    except ReviewError:
        meta = {}
    found: list[dict] = []
    seen: set[str] = set()

    def add(value: object, label: str) -> None:
        if not isinstance(value, str):
            return
        url = value.strip().strip("\"'").strip()
        if not url.startswith(("http://", "https://")) or len(url) > 500 or url in seen:
            return
        seen.add(url)
        found.append({"label": label, "url": url})

    link_name = _wikilink_name(meta.get("source"))
    for field in ("source_url", "url", "source_urls"):
        value = meta.get(field)
        if isinstance(value, list):
            for item in value:
                add(item, link_name or "来源")
        else:
            add(value, link_name or "来源")
    for name in _wikilink_names(meta.get("source")) or [link_name]:
        note, note_meta = _source_note(root, name)
        if note is not None:
            label = str(note_meta.get("entity") or note_meta.get("entity_alias") or name or "来源").strip()
            for field in ("source_url", "url"):
                add(note_meta.get(field), label)
    _, body = _split_body(content)
    for url in _http_urls(body):
        add(url, link_name or "正文链接")
    return found[:MAX_SOURCE_URLS]


def _split_body(content: str) -> tuple[str, str]:
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end >= 0:
            return content[:end], content[end + 4:]
    return "", content


class ReviewerLabelStore:
    """Local display-only record of which nickname handled each decision.

    Deliberately separate from the MAC-bound decision queue: the decision record
    schema is frozen for the publisher contract, so the human-readable reviewer
    name lives in its own append-only 0600 file and never enters the queue, the
    audit digest or the logs.
    """

    def __init__(self, path: Path):
        self.path = Path(os.path.abspath(path))

    def append(self, decision_id: str, nickname: object) -> None:
        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("decision id is required")
        payload = (json.dumps({"decision_id": decision_id, "reviewer": sanitize_nickname(nickname)}, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with queue_lock(self.path):
            flags = os.O_APPEND | os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            try:
                details = os.fstat(descriptor)
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    raise ReviewError("reviewer label file is unsafe", "500 Internal Server Error")
                os.fchmod(descriptor, 0o600)
                os.write(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def load(self) -> dict[str, str]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ReviewError("reviewer label file is unreadable", "500 Internal Server Error") from exc
        labels: dict[str, str] = {}
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and isinstance(value.get("decision_id"), str):
                labels[value["decision_id"]] = sanitize_nickname(value.get("reviewer"))
        return labels


@dataclass(frozen=True)
class ReviewItem:
    identity: str
    path: str
    title: str
    source: str
    published_date: str
    status: str
    reviewer: str
    decided_at: str
    action: str
    content: str
    wiki_target: str

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


@dataclass(frozen=True)
class DecisionSummary:
    action: str
    reviewer: str
    created_at: str


class MemoryFormNonceStore:
    """Bounded, process-local, single-use form nonce store."""

    def __init__(self, max_items: int = 4096, clock=time.time):
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self._items: dict[str, tuple[int, str, str, str]] = {}
        self._max_items = max_items
        self._clock = clock

    def _prune(self, now: int) -> None:
        self._items = {key: value for key, value in self._items.items() if value[0] > now}

    def issue(self, session_id: str, rough_path: str, expires: int, snapshot_root: str = "") -> str:
        now = int(self._clock())
        self._prune(now)
        nonce = secrets.token_urlsafe(32)
        self._items[nonce] = (expires, session_id, rough_path, snapshot_root)
        while len(self._items) > self._max_items:
            del self._items[min(self._items, key=lambda key: self._items[key][0])]
        return nonce

    def consume(self, nonce: str, session_id: str, rough_path: str) -> str | None:
        now = int(self._clock())
        item = self._items.pop(nonce, None)
        return (item[3] if
            item and item[0] > now
            and secrets.compare_digest(item[1].encode("utf-8"), session_id.encode("utf-8"))
            and secrets.compare_digest(item[2].encode("utf-8"), rough_path.encode("utf-8"))
        else None)

    def peek(self, nonce: str, session_id: str, rough_path: str) -> str | None:
        """Validate a nonce without consuming it. A downstream validation
        failure (a stale rough_sha256, a missing required field) must not
        burn the reviewer's only submit attempt -- they should be able to
        fix the form and resubmit without a full page reload."""
        now = int(self._clock())
        item = self._items.get(nonce)
        return (item[3] if
            item and item[0] > now
            and secrets.compare_digest(item[1].encode("utf-8"), session_id.encode("utf-8"))
            and secrets.compare_digest(item[2].encode("utf-8"), rough_path.encode("utf-8"))
        else None)

    def invalidate(self, nonce: str) -> None:
        self._items.pop(nonce, None)


def _frontmatter(text: str) -> dict:
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|\Z)", text, re.S)
    if not match:
        raise ReviewError("rough has no YAML frontmatter", "409 Conflict")
    try:
        value = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ReviewError("rough has invalid YAML frontmatter", "409 Conflict") from exc
    if not isinstance(value, dict):
        raise ReviewError("rough frontmatter is not an object", "409 Conflict")
    return value


def validate_relative_path(raw: str, prefix: PurePosixPath) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ReviewError("invalid path")
    path = PurePosixPath(raw)
    if path.is_absolute() or path.suffix.lower() != ".md" or ".." in path.parts or "." in path.parts:
        raise ReviewError("invalid path")
    if path == prefix or prefix not in path.parents:
        raise ReviewError("path outside allowed area")
    return path


def resolve_existing(root: Path, relative: PurePosixPath) -> Path:
    root = Path(root).resolve()
    target = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ReviewError("symlink paths are not allowed")
    try:
        resolved = target.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ReviewError("path does not exist", "409 Conflict") from exc
    if root not in resolved.parents or not resolved.is_file():
        raise ReviewError("path outside repository")
    return resolved


def rough_binding(path: Path, *, relative: str | None = None) -> RoughBinding:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_size > 16 * 1024 * 1024:
            raise ReviewError("rough is not a bounded regular file", "409 Conflict")
        data = bytearray()
        while len(data) <= 16 * 1024 * 1024:
            chunk = os.read(descriptor, min(65536, 16 * 1024 * 1024 + 1 - len(data)))
            if not chunk: break
            data.extend(chunk)
        if len(data) > 16 * 1024 * 1024:
            raise ReviewError("rough is too large", "409 Conflict")
        raw = bytes(data)
    except OSError as exc:
        raise ReviewError("rough is unreadable", "409 Conflict") from exc
    finally:
        if descriptor is not None: os.close(descriptor)
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewError("rough is not UTF-8", "409 Conflict") from exc
    meta = _frontmatter(content)
    version = meta.get("source_item_key")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ReviewError("rough has no valid source_item_key", "409 Conflict")
    return RoughBinding(relative or path.as_posix(), "sha256:" + hashlib.sha256(raw).hexdigest(), version, content)


def rough_binding_at(root: Path, relative: PurePosixPath) -> RoughBinding:
    """Pin every path component and read the validated inode."""
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors = []
    try:
        descriptors.append(os.open(root, directory_flags))
        for part in relative.parts[:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
            if not stat.S_ISDIR(os.fstat(descriptors[-1]).st_mode):
                raise ReviewError("path parent is not a directory", "409 Conflict")
        descriptor = os.open(relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=descriptors[-1])
        descriptors.append(descriptor)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_size > 16 * 1024 * 1024:
            raise ReviewError("rough is not a bounded regular file", "409 Conflict")
        data = bytearray()
        while len(data) <= 16 * 1024 * 1024:
            chunk = os.read(descriptor, min(65536, 16 * 1024 * 1024 + 1 - len(data)))
            if not chunk: break
            data.extend(chunk)
        if len(data) > 16 * 1024 * 1024: raise ReviewError("rough is too large", "409 Conflict")
    except OSError as exc:
        raise ReviewError("path does not exist", "409 Conflict") from exc
    finally:
        for descriptor in reversed(descriptors): os.close(descriptor)
    raw = bytes(data)
    try: content = raw.decode("utf-8")
    except UnicodeDecodeError as exc: raise ReviewError("rough is not UTF-8", "409 Conflict") from exc
    meta = _frontmatter(content); version = meta.get("source_item_key")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ReviewError("rough has no valid source_item_key", "409 Conflict")
    return RoughBinding(relative.as_posix(), "sha256:" + hashlib.sha256(raw).hexdigest(), version, content)


def decision_mac(record: dict, key: bytes) -> str:
    payload = {name: value for name, value in record.items() if name != "decision_mac"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "hmac-sha256:" + hmac.new(key, b"dek-review-decision-v1\0" + canonical, hashlib.sha256).hexdigest()


def verify_decision_mac(record: dict, key: bytes) -> bool:
    supplied = record.get("decision_mac")
    return isinstance(supplied, str) and hmac.compare_digest(supplied, decision_mac(record, key))


def validate_decision(record: object, key: bytes) -> dict:
    """Validate the complete, MAC-bound publisher authorization record."""
    fields={"schema_version","record_type","decision_id","created_at","reviewer_digest","action","rough_path","rough_sha256","rough_version","wiki_path","candidate_markdown","comment","snapshot_commit","snapshot_tree","snapshot_bundle_sha256","decision_mac"}
    if not isinstance(record,dict) or set(record)!=fields or record.get("schema_version")!=2 or record.get("record_type")!="decision":
        raise ReviewError("decision schema invalid")
    if record.get("action") not in {"approve","reject","return"} or not isinstance(record.get("decision_id"), str) or not APPROVAL_ID_PATTERN.fullmatch(record["decision_id"]):
        raise ReviewError("decision schema invalid")
    if not re.fullmatch(r"[0-9a-f]{40,64}",str(record.get("snapshot_commit",""))) or not re.fullmatch(r"[0-9a-f]{40,64}",str(record.get("snapshot_tree",""))) or not re.fullmatch(r"[0-9a-f]{64}",str(record.get("snapshot_bundle_sha256",""))):
        raise ReviewError("decision schema invalid")
    validate_relative_path(record["rough_path"],ROUGH_PREFIX)
    if record["action"]=="approve": validate_relative_path(record["wiki_path"],WIKI_PREFIX)
    if not verify_decision_mac(record,key): raise ReviewError("decision MAC invalid","403 Forbidden")
    return dict(record)


ERROR_STATUS = "500 Internal Server Error"

STYLE = """<style>
.content{margin-left:var(--sidebar-w)}
.review-shell{max-width:900px;margin:0 auto;padding:88px 40px 100px}
.summary{margin-bottom:.8rem;color:var(--muted);font-size:.92rem}
.summary-row{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-bottom:.8rem}
.summary-row .summary{margin-bottom:0}
.summary-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-left:auto}
.ingest-trigger-form,.publish-trigger-form{margin:0}
.status-tabs{display:flex;gap:.7rem;flex-wrap:wrap;align-items:center;margin:0 0 1.2rem}
.status-tab{position:relative;display:inline-flex;align-items:center;padding:.4rem .75rem;border-radius:999px;color:var(--muted);text-decoration:none;font-weight:600;font-size:.88rem}
.status-tab:hover{background:var(--hover);color:var(--text)}
.status-tab.active{background:var(--accent);color:var(--on-accent)}
.filter-count{position:absolute;top:-.3rem;right:-.3rem;display:inline-block;min-width:1.3em;padding:0 .3rem;border-radius:999px;background:var(--bg);color:var(--text);font-size:.68em;line-height:1.4;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.18)}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
.table-wrap table{display:table;width:100%;min-width:600px}   /* a narrow screen scrolls the table sideways instead of crushing its columns */
.col-index{width:60px}
.col-status{width:7.5rem}.col-reviewer{width:9.5rem}
.table-wrap th,.table-wrap td.status,.table-wrap td.reviewer{white-space:normal;overflow-wrap:anywhere}
.reviewer-time{font-size:.85em;margin-top:.15rem}
th.index,td.index{width:60px;min-width:60px;text-align:center}
.table-wrap td.content{max-width:0;min-width:240px}
.content-meta,.content-title{white-space:normal;overflow-wrap:anywhere}.content-title{display:block}
.list-footer{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-top:18px}
.pager,.page-size{display:flex;align-items:center;flex-wrap:wrap;gap:6px;font-size:.9rem}
.page-size{margin-left:auto;color:var(--muted)}
.page-size input[type=number]{width:52px;box-sizing:border-box;padding:.3rem .5rem;text-align:center;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--muted);font:inherit;line-height:inherit;appearance:textfield;-moz-appearance:textfield}
.page-size input[type=number]:focus{outline:none;border-color:var(--accent);background:var(--panel);color:var(--text)}
.page-size input[type=number]::-webkit-inner-spin-button,.page-size input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.pager a,.pager .current,.pager .disabled{min-width:2rem;padding:.3rem .7rem;border:1px solid var(--line);border-radius:6px;text-align:center;text-decoration:none;color:var(--text)}
.pager a:hover{background:var(--hover)}
.pager .current{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.pager .disabled{color:var(--muted);opacity:.5}
.pager .gap,.pager .page-info{color:var(--muted);padding:0 .3rem}
.pager .page-info{margin-left:.6rem}
.table-wrap tbody tr:hover{background:var(--hover)}
.table-wrap tbody tr[data-href]{cursor:pointer}
.status{white-space:nowrap;font-weight:600}
.status-dot{display:inline-block;width:.55rem;height:.55rem;margin-right:.4rem;border-radius:50%;background:var(--accent);vertical-align:.04rem}
.meta{color:var(--muted);font-size:.9em}
.sources .meta a{overflow-wrap:anywhere}
pre,textarea,input:not([type=hidden]),select{box-sizing:border-box;width:100%;font:inherit;font-weight:400;border:1px solid var(--line);border-radius:8px;padding:.6rem .75rem;background:var(--bg);color:var(--text)}
pre{white-space:pre-wrap;max-height:30rem;overflow:auto;background:var(--panel)}
label{display:block;margin:1rem 0;color:var(--text);font-weight:600;font-size:.9rem}
button[type=submit]{margin-top:.5rem;padding:.55rem 1.1rem;border:0;border-radius:8px;background:var(--accent);color:var(--on-accent);font-weight:600;cursor:pointer;font-size:.95rem}
button[type=submit]:hover{filter:brightness(.94)}
.summary-actions button[type=submit]:hover{filter:none;background:var(--accent)}
.decision-actions{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:1rem}
.decision-actions button{margin-top:0}
.decision-actions .action-reject{background:#b3261e}
.snapshot-line{margin:.2rem 0 .4rem}
.snapshot-wait{border-left-color:#d97706}
.notice{border-left:4px solid var(--accent);background:var(--panel);padding:.65rem .85rem;margin:1rem 0;border-radius:0 8px 8px 0}
.combo{position:relative}
.path-suggestions{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem;margin:-.4rem 0 1rem;color:var(--muted);font-size:.85rem}
.suggestion-chip{padding:.2rem .7rem;border:1px solid var(--line);border-radius:999px;background:transparent;color:var(--text);font-size:.85rem;font-weight:400;cursor:pointer}
.combo-list{display:none;position:absolute;top:100%;left:0;right:0;max-height:14rem;overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:8px;box-shadow:0 10px 30px rgba(0,0,0,.14);z-index:5;margin-top:4px}
.combo-list.open{display:block}
.combo-option{padding:.5rem .7rem;cursor:pointer}
.combo-option:hover,.combo-option.active{background:var(--hover)}
.combo-path{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:400}
/* The list is as wide as the knowledge base's home page: from the sidebar to the right edge, up to 1440px, 64px in from each side */
.review-shell.review-list{max-width:1440px;margin:0;padding-left:64px;padding-right:64px}
@media(max-width:760px){.content{margin-left:0}.review-shell{padding:82px 20px 70px}.review-shell.review-list{padding:82px 20px 70px}}
</style>"""


class ReviewService:
    def __init__(
        self,
        repo_root: Path,
        queue_path: Path,
        *,
        audit_key: bytes,
        queue_key: bytes,
        nonces: MemoryFormNonceStore,
        clock: Callable[[], float],
        labels: ReviewerLabelStore | None = None,
        path_prefix: str = "",
        suggestions_path: Path | None = None,
        ingest_state_path: Path | None = None,
        publish_state_path: Path | None = None,
    ):
        if len(audit_key) < 16 or len(queue_key) < 16:
            raise ValueError("review secrets must be at least 16 bytes")
        if path_prefix and not re.fullmatch(r"/[A-Za-z0-9._~\-/]*[A-Za-z0-9._~\-]", path_prefix):
            raise ValueError("review path prefix must be an absolute, non-trailing-slash path")
        self.repository_source = repo_root
        self.ingest_state_path = Path(ingest_state_path) if ingest_state_path else None
        self.publish_state_path = Path(publish_state_path) if publish_state_path else None
        self.root = Path(repo_root).resolve() if isinstance(repo_root,Path) else None
        self.queue_path = Path(os.path.abspath(queue_path))
        self.audit_key, self.queue_key, self.nonces, self.clock = audit_key, queue_key, nonces, clock
        self.labels = labels
        self.path_prefix = path_prefix
        self.suggestions_path = suggestions_path

    def _pending(self, root=None) -> list[RoughBinding]:
        root=root or (self.repository_source.current() if hasattr(self.repository_source,"current") else self.root)
        rough_root = root / "ingestion" / "rough"
        if not rough_root.is_dir() or rough_root.is_symlink():
            return []
        result = []
        for path in sorted(rough_root.rglob("*.md")):
            relative = path.relative_to(root).as_posix()
            try:
                binding = rough_binding_at(root, validate_relative_path(relative, ROUGH_PREFIX))
                if _frontmatter(binding.content).get("status") == "pending_review":
                    result.append(binding)
            except ReviewError:
                continue
        return result

    def render(self, session_id: str) -> bytes:
        cards = []
        root=self.repository_source.current() if hasattr(self.repository_source,"current") else self.root
        for rough in self._pending(root):
            nonce = self.nonces.issue(session_id, rough.path, int(self.clock()) + 900, str(root))
            cards.append(self._form_card(rough, nonce, root=root, heading=PurePosixPath(rough.path).name))
        content = "".join(cards) or "<p>当前没有待审核 rough。</p>"
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>审核 · DEK</title>{STYLE}</head><body><h1>待审核内容</h1>{content}</body></html>""".encode()

    def _snapshot_root(self, root=None) -> Path:
        return root or (self.repository_source.current() if hasattr(self.repository_source, "current") else self.root)

    def _decisions(self) -> list[dict]:
        return list(iter_valid_decisions(self.queue_path, self.queue_key))

    def _labels(self) -> dict[str, str]:
        return self.labels.load() if self.labels is not None else {}

    def _title(self, binding: RoughBinding) -> tuple[str, str, str, str]:
        meta = _frontmatter(binding.content)
        title = str(meta.get("question") or PurePosixPath(binding.path).name).strip()
        source = str(meta.get("source") or "").strip()
        date = str(meta.get("published_date") or meta.get("date") or "").strip()
        target = str(meta.get("wiki_target") or "").strip()
        return title, source, date, target

    def history(self, relative: str) -> list[DecisionSummary]:
        labels = self._labels()
        return [
            DecisionSummary(record.get("action", ""), labels.get(record.get("decision_id", ""), ""), record.get("created_at", ""))
            for record in self._decisions()
            if record.get("rough_path") == relative
        ]

    def _promised_wiki_paths(self, root: Path, except_rough: str = "") -> frozenset[str]:
        """Wiki paths that approved drafts (other than `except_rough`) are waiting to be published at."""
        latest: dict[str, dict] = {}
        for record in self._decisions():
            if isinstance(record.get("rough_path"), str):
                latest[record["rough_path"]] = record
        return frozenset(
            str(record.get("wiki_path"))
            for path, record in latest.items()
            if path != except_rough and record.get("action") == "approve" and record.get("wiki_path")
            and not (root / str(record["wiki_path"])).exists()
            # only an approval whose draft is still waiting to be published reserves its number;
            # one whose draft was deleted (a data reset) can never publish, so the number is free
            and self._rough_status(root, path) not in ("", "promoted")
        )

    def _duplicate_notice(self, root: Path, item) -> str:
        """A warning when this draft asks a question another pending draft, or a wiki entry, already asks."""
        question, answer = rough_qa(item.content)
        key = question_key(question)
        if not key:
            return ""
        notices = []
        same_question = [(binding, rough_qa(binding.content)) for binding in self._pending(root)
                         if binding.path != item.path and question_key(rough_qa(binding.content)[0]) == key]
        if same_question:
            identical = [PurePosixPath(b.path).name for b, (_, other) in same_question if question_key(other) == question_key(answer)]
            different = [PurePosixPath(b.path).name for b, (_, other) in same_question if question_key(other) != question_key(answer)]
            if identical:
                notices.append(f"另有 {len(identical)} 份待审草稿问题和答案都相同：" + "、".join(html.escape(n) for n in identical)
                               + "。同一条问答只需批准一份，其余请拒绝。")
            if different:
                notices.append(f"另有 {len(different)} 份待审草稿问题相同、答案不同：" + "、".join(html.escape(n) for n in different)
                               + "。请核对后再决定。")
        existing = wiki_questions(str(root)).get(key)
        if existing:
            notices.append("Wiki 里已有相同问题的条目：" + "、".join(html.escape(path[len("wiki/"):]) for path in existing[:3]) + "。批准前请确认不是重复。")
        return "".join(f'<div class="notice duplicate-notice">注意：{text}</div>' for text in notices)

    @staticmethod
    def _rough_status(root: Path, relative: str) -> str:
        """The `status` a draft carries in this snapshot; "" when it is not there (or not readable)."""
        try:
            return str(_frontmatter(rough_binding_at(root, validate_relative_path(relative, ROUGH_PREFIX)).content).get("status") or "")
        except (ReviewError, OSError, ValueError, SystemExit):
            return ""

    def list_items(self, *, query: str = "", status: str = "", root=None) -> list[ReviewItem]:
        root = self._snapshot_root(root)
        labels = self._labels()
        items: dict[str, ReviewItem] = {}
        for binding in self._pending(root):
            title, source, date, target = self._title(binding)
            items[binding.path] = ReviewItem(
                item_identity(binding.path), binding.path, title, source, date,
                "pending", "", "", "", binding.content, target,
            )
        for path, record in ((record.get("rough_path"), record) for record in self._decisions()):
            if not isinstance(path, str) or not path:
                continue
            action = record.get("action", "")
            derived = ACTION_STATUS.get(action, "pending")
            # An approval is published once its draft is gone from the snapshot or the draft
            # itself says so (publishing keeps the file and marks it `promoted`).
            if action == "approve":
                draft_status = self._rough_status(root, path)
                wiki_path = str(record.get("wiki_path") or "")
                if draft_status == "" and wiki_path and not (root / wiki_path).exists() and path not in items:
                    # The draft is gone and so is the page it was to become (a data reset removed
                    # both): this approval published nothing, so it is not listed as published.
                    continue
                if draft_status in ("", "promoted"):
                    derived = "published"
            existing = items.get(path)
            reviewer = labels.get(record.get("decision_id", ""), "")
            items[path] = ReviewItem(
                item_identity(path), path,
                existing.title if existing else PurePosixPath(path).name,
                existing.source if existing else "",
                existing.published_date if existing else "",
                derived, reviewer, record.get("created_at", ""), action,
                existing.content if existing else "",
                existing.wiki_target if existing else str(record.get("wiki_path") or ""),
            )
        values = list(items.values())
        needle = query.strip().casefold()
        if needle:
            values = [
                item for item in values
                if needle in " ".join((item.path, item.title, item.source, item.reviewer)).casefold()
                or needle in item.content.casefold()
            ]
        if status:
            values = [item for item in values if item.status == status]
        pending = sorted((item for item in values if item.status == "pending"), key=lambda item: (item.published_date, item.path))
        decided = sorted((item for item in values if item.status != "pending"), key=lambda item: (item.decided_at, item.path), reverse=True)
        return pending + decided

    def find_item(self, identity: str, *, root=None) -> ReviewItem | None:
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{16}", identity):
            return None
        for item in self.list_items(root=root):
            if item.identity == identity:
                return item
        return None

    def _form_card(self, rough: RoughBinding, nonce: str, *, wiki_path: str = "", candidate: str = "", root: Path | None = None, action_query: str = "", heading: str = "原文", suggested: str = "", alternatives: list[tuple[str, str]] | None = None, taken: frozenset[str] = frozenset()) -> str:
        candidates = wiki_folder_candidates(root, taken) if root else []
        options_json = json.dumps([[label, path] for label, path in candidates], ensure_ascii=False)
        if self.ingest_status()["in_progress"]:
            # a decision now would be made on a list the pull is about to replace
            decision_buttons = ('<button type="submit" class="is-busy" disabled aria-busy="true">抓取中，暂不能批准</button>'
                                '<button type="submit" class="action-reject is-busy" disabled aria-busy="true">拒绝</button>')
        else:
            decision_buttons = ('<button type="submit" name="action" value="approve" data-busy-label="提交中…">批准</button>'
                                '<button type="submit" name="action" value="reject" class="action-reject" data-busy-label="提交中…">拒绝</button>')
        chips = ""
        if alternatives:
            buttons = "".join(f'<button type="button" class="suggestion-chip" data-path="{html.escape(path, quote=True)}">{html.escape(label)}</button>'
                              for label, path in alternatives)
            chips = f'<div class="path-suggestions">系统建议：{buttons}</div>'
        return f"""<article><h2>{html.escape(heading)}</h2><pre>{html.escape(rough_display(rough.content, suggested))}</pre>
<form method="post" action="{self.path_prefix}/decision{html.escape(action_query)}"><input type="hidden" name="form_nonce" value="{nonce}"><input type="hidden" name="rough_path" value="{html.escape(rough.path)}"><input type="hidden" name="rough_sha256" value="{rough.sha256}"><input type="hidden" name="rough_version" value="{html.escape(rough.version)}">
<label>Wiki 路径<div class="combo"><input name="wiki_path" class="wiki-path-input" autocomplete="off" value="{html.escape(wiki_path)}" placeholder="搜索 wiki 文件夹…" data-options="{html.escape(options_json)}"><div class="combo-list" role="listbox"></div></div></label>{chips}<label>候选 Wiki Markdown<textarea name="candidate_markdown" rows="18">{html.escape(candidate)}</textarea></label><div class="decision-actions">{decision_buttons}</div></form></article>"""

    def _page(self, title: str, body: str) -> bytes:
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{html.escape(title)} · DEK</title><link rel="stylesheet" href="/assets/style.css">{STYLE}</head><body>{body}<script src="/assets/app.js" defer></script></body></html>""".encode()

    def _header(self) -> str:
        return (
            '<header><button id="menu-toggle" aria-label="打开目录">☰</button>'
            '<img class="brand-logo" src="/assets/logo.png" alt="臣邦医药" width="150" height="28">'
            '<strong role="heading" aria-level="1">知识审核</strong>'
            '<div class="user-menu review-header-menu" data-auth-me="/auth/me"><span id="user-name">正在读取…</span>'
            '<a href="/auth/logout">退出</a></div>'
            '</header>'
        )

    def _sidebar(self) -> str:
        return (
            '<aside class="sidebar">'
            '<nav id="nav-tree" data-manifest="/manifest.json" data-current=""></nav></aside>'
        )

    # --- how fresh the list is, and whether a pull is on its way -------------------------
    INGEST_WAIT_SECONDS = 600
    PUBLISH_WAIT_SECONDS = 900

    def snapshot_time(self) -> float | None:
        """When the data behind the list was produced (the review bundle's write time)."""
        bundle = getattr(self.repository_source, "bundle", None)
        try:
            return os.stat(bundle).st_mtime if bundle else None
        except OSError:
            return None

    def _record_request(self, path: Path | None, when: float) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{int(when)}\n", encoding="utf-8")
        except OSError:
            pass

    def record_ingest_request(self, when: float) -> None:
        """Remember that a pull was asked for (the trigger marker is removed as soon as it is picked up)."""
        self._record_request(self.ingest_state_path, when)

    def record_publish_request(self, when: float) -> None:
        self._record_request(self.publish_state_path, when)

    def _request_status(self, path: Path | None, wait: int) -> dict:
        """`in_progress`: something was requested after the current snapshot and is younger than
        `wait` seconds. `overdue`: the same, but older: it did not refresh the data."""
        snapshot = self.snapshot_time()
        requested = None
        if path is not None:
            try:
                requested = float(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                requested = None
        pending = requested is not None and (snapshot is None or requested > snapshot)
        age = self.clock() - requested if requested is not None else 0
        return {"snapshot": snapshot, "requested": requested,
                "in_progress": bool(pending and 0 <= age < wait),
                "overdue": bool(pending and age >= wait)}

    def ingest_status(self) -> dict:
        """A pull is `in_progress` until the review data is rewritten (the snapshot moves past the request)."""
        return self._request_status(self.ingest_state_path, self.INGEST_WAIT_SECONDS)

    def publish_status(self) -> dict:
        """A publish is `in_progress` until its last step refreshes the review data."""
        return self._request_status(self.publish_state_path, self.PUBLISH_WAIT_SECONDS)

    def _snapshot_banner(self) -> str:
        status = self.ingest_status()
        clock = lambda value: datetime.fromtimestamp(value, BEIJING).strftime("%m-%d %H:%M")
        parts = []
        if status["snapshot"]:
            minutes = max(0, int((self.clock() - status["snapshot"]) // 60))
            ago = "刚刚" if minutes < 1 else f"{minutes} 分钟前" if minutes < 120 else f"{minutes // 60} 小时前"
            parts.append(f'<div class="meta snapshot-line">数据快照：{clock(status["snapshot"])}（{ago}）。列表和内容都来自这一时刻。</div>')
        if status["in_progress"]:
            parts.append(f'<div class="notice snapshot-wait">抓取进行中（{clock(status["requested"])} 开始，约需 1～2 分钟）。'
                         '完成后请刷新页面；进行期间不能批准或拒绝，避免处理到旧列表里已经不存在的内容。</div>')
        elif status["overdue"]:
            parts.append(f'<div class="notice">{clock(status["requested"])} 的拉取没有更新数据，可能失败了。请稍后再试，仍不行请联系管理员。</div>')
        publishing = self.publish_status()
        if publishing["in_progress"]:
            parts.append(f'<div class="notice snapshot-wait">发布进行中（{clock(publishing["requested"])} 开始，约需 1～2 分钟）。'
                         '完成后请刷新页面；进行期间发布按钮暂不能再点。</div>')
        return "".join(parts)

    def render_list(self, session_id: str, *, query: str = "", status: str = "", notice: str = "", page: int = 1, page_size: int = PAGE_SIZE) -> bytes:
        all_items = self.list_items(query=query)
        status_counts = {key: sum(item.status == key for item in all_items) for key in STATUS_LABELS}
        items = [item for item in all_items if not status or item.status == status]
        size = min(max(int(page_size), MIN_PAGE_SIZE), MAX_PAGE_SIZE)
        pages = max(1, -(-len(items) // size))
        page = min(max(1, int(page)), pages)
        first = (page - 1) * size
        shown = items[first:first + size]
        tab_size = html.escape(f"&page_size={size}") if size != PAGE_SIZE else ""
        filter_parts = []
        for key, label in (("", "全部"), ("pending", "待审核"), ("approved", "已批准待发布"), ("published", "已发布"), ("rejected", "已拒绝")):
            count = len(all_items) if not key else status_counts[key]
            badge = f'<sup class="filter-count">{count}</sup>' if count else ""
            filter_parts.append(
                f'<a class="status-tab{" active" if key == status else ""}"'
                + (' aria-current="page"' if key == status else '')
                + f' href="{self.path_prefix}/?status={key}{tab_size}">{html.escape(label)}{badge}</a>'
            )
        filters = "".join(filter_parts)
        position = html.escape(list_state_query(status, page, size=size))
        rows = "".join(
            f'<tr data-href="{self.path_prefix}/item/{item.identity}{position}">'
            f'<td class="meta index">{number}</td>'
            f'<td class="content">'
            + _content_cell(item, f"{self.path_prefix}/item/{item.identity}{position}")
            + "</td>"
            f'<td class="status status-{item.status}">'
            + ('<span class="status-dot" aria-hidden="true"></span>' if item.status == "pending" else '')
            + f'{html.escape(item.status_label)}</td>'
            f'<td class="reviewer">{_reviewer_cell(item)}</td>'
            "</tr>"
            for number, item in enumerate(shown, start=first + 1)
        )
        table = (
            '<div class="table-wrap"><table><colgroup><col class="col-index"><col class="col-task"><col class="col-status"><col class="col-reviewer"></colgroup><thead><tr><th class="index">序号</th><th>内容</th><th>状态</th><th>审核人</th></tr></thead><tbody>'
            + (rows or '<tr><td colspan="4">没有符合条件的条目。</td></tr>')
            + "</tbody></table></div>"
        )
        def page_href(number: int) -> str:
            return f'{self.path_prefix}/{html.escape(list_state_query(status, number, query, size))}'
        parts = [f'<a href="{page_href(page - 1)}">上一页</a>' if page > 1 else '<span class="disabled">上一页</span>']
        for number in _page_window(page, pages):
            if number is None: parts.append('<span class="gap">…</span>')
            elif number == page: parts.append(f'<span class="current" aria-current="page">{number}</span>')
            else: parts.append(f'<a href="{page_href(number)}">{number}</a>')
        parts.append(f'<a href="{page_href(page + 1)}">下一页</a>' if page < pages else '<span class="disabled">下一页</span>')
        parts.append(f'<span class="page-info">第 {page}/{pages} 页</span>')
        pager = f'<nav class="pager" aria-label="分页">{"".join(parts)}</nav>'
        hidden = "".join(f'<input type="hidden" name="{name}" value="{html.escape(value)}">'
                         for name, value in (("status", status), ("q", query)) if value)
        size_nav = (f'<form method="get" action="{self.path_prefix}/" class="page-size">每页{hidden}'
                    f'<input type="number" name="page_size" min="5" max="100" step="1" value="{size}" inputmode="numeric" aria-label="每页条数">条</form>')
        pulling, publishing = self.ingest_status()["in_progress"], self.publish_status()["in_progress"]
        ingest_button = (
            f'<form method="post" action="{self.path_prefix}/trigger-ingest" class="ingest-trigger-form">'
            + ('<button type="submit" class="is-busy" disabled aria-busy="true">抓取中…</button>' if pulling
               else '<button type="submit" data-busy-label="已提交…">立即拉取最新源</button>')
            + '</form>'
        )
        publish_button = (
            f'<form method="post" action="{self.path_prefix}/publish" class="publish-trigger-form">'
            + ('<button type="submit" class="is-busy" disabled aria-busy="true">发布中…</button>' if publishing
               else f'<button type="submit" data-busy-label="已提交…">发布已批准内容（{status_counts["approved"]}）</button>')
            + '</form>'
        )
        body = (
            self._header() + self._sidebar()
            + '<div class="content">'
            + '<main class="review-shell review-list">'
            + f'<div class="summary-row"><div class="summary">共 {len(items)} 条</div><div class="summary-actions">{ingest_button}{publish_button}</div></div>'
            + self._snapshot_banner()
            + (f'<div class="notice">{html.escape(notice)}</div>' if notice else "")
            + f'<nav class="status-tabs" aria-label="审核状态筛选">{filters}</nav>'
            + table + f'<div class="list-footer">{pager}{size_nav}</div>' + '</main>'
            + '</div>'
        )
        return self._page("知识审核", body)

    def render_item(self, session_id: str, identity: str, *, notice: str = "", unlocked: bool = False, list_status: str = "", list_page: int = 1, list_size: int = PAGE_SIZE) -> bytes | None:
        item = self.find_item(identity)
        if item is None:
            return None
        root = self._snapshot_root()
        position = list_state_query(list_status, list_page, size=list_size)
        history = self.history(item.path)
        rows = "".join(
            f"<tr><td>{html.escape(ACTION_LABEL.get(record.action, record.action))}</td>"
            f"<td>{html.escape(record.reviewer or '—')}</td><td class='meta'>{html.escape(_display_time(record.created_at))}</td></tr>"
            for record in reversed(history)
        )
        history_block = (
            f"<h2>处理历史（{len(history)}）</h2><table><thead><tr><th>决定</th><th>审核人</th><th>时间</th></tr></thead><tbody>{rows}</tbody></table>"
            if history else ""
        )
        decided = bool(history)
        if item.content and decided and not unlocked:
            form = (
                '<p class="notice">该条目已有处理决定（见下方处理历史）。表单已锁定，避免误改已批准/已处理的内容。'
                f'如确需修改并重新提交，<a href="{self.path_prefix}/item/{identity}?edit=1{html.escape(position.replace("?", "&", 1))}">点击重新编辑</a>。</p>'
            )
        elif item.content:
            nonce = self.nonces.issue(session_id, item.path, int(self.clock()) + 900, str(root))
            binding = rough_binding_at(root, validate_relative_path(item.path, ROUGH_PREFIX))
            suggested = default_wiki_path(item.wiki_target)
            alternatives = []
            # Numbers other approved drafts are already waiting for are not offered again.
            taken = self._promised_wiki_paths(root, item.path)
            if not suggested:
                # The draft carries no target: suggest the folders holding the most similar filed entries.
                alternatives = suggest_wiki_paths(
                    root, item.content, candidates=wiki_folder_candidates(root, taken),
                    preferred=read_suggestion(self.suggestions_path, binding.path, binding.sha256))
                if alternatives:
                    suggested = alternatives[0][1]
            form = self._form_card(binding, nonce, wiki_path=suggested, candidate=candidate_draft(item.content, suggested), root=root,
                                   action_query=position, suggested=suggested, alternatives=alternatives, taken=taken)
            form = self._duplicate_notice(root, item) + form
            if decided:
                form = (
                    '<div class="notice">注意：该条目已有处理决定，提交将新增一条决定并覆盖当前显示的状态，请谨慎确认后再提交。</div>' + form
                )
        else:
            form = '<p class="notice">该条目已不在待审核快照中，无法再提交决定。</p>'
        meta = " · ".join(part for part in (
            f"状态：{item.status_label}",
            f"来源：{_short_source(item.source)}" if item.source else "",
            f"日期：{item.published_date}" if item.published_date else "",
            f"目标：{item.wiki_target}" if item.wiki_target else "",
            f"备注：{PurePosixPath(item.path).name}",
        ) if part)
        links = source_urls(item.content, root) if item.content else []
        links_block = ""
        if links:
            rows_html = "".join(
                f'<li><a href="{html.escape(link["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(link["label"])}</a>'
                f'<div class="meta"><a href="{html.escape(link["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{html.escape(link["url"])}</a></div></li>'
                for link in links
            )
            links_block = f'<h2>来源网址</h2><ul class="sources">{rows_html}</ul>'
        body = (
            self._header() + self._sidebar()
            + '<div class="content">'
            + f'<main class="review-shell review-list"><p><a href="{self.path_prefix}/{html.escape(position)}">← 返回列表</a></p><h1>{html.escape(_item_title(item))}</h1><div class="meta">{html.escape(meta)}</div>'
            + self._snapshot_banner()
            + (f'<div class="notice">{html.escape(notice)}</div>' if notice else "")
            + links_block + form + history_block + '</main>'
            + '</div>'
        )
        return self._page(_item_title(item), body)

    def submit_form(self, body: bytes, *, session_id: str, user_id: str, reviewer_label: str = "") -> str:
        if len(body) > MAX_DECISION_BYTES:
            raise ReviewError("request too large", "413 Payload Too Large")
        if self.ingest_status()["in_progress"]:
            raise ReviewError("ingest in progress", "409 Conflict")
        try:
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=12)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ReviewError("invalid form") from exc
        def one(name: str) -> str:
            items = values.get(name, [])
            if len(items) != 1:
                raise ReviewError(f"invalid {name}")
            return items[0]
        rough_path = one("rough_path")
        nonce = one("form_nonce")
        snapshot_root=self.nonces.peek(nonce, session_id, rough_path)
        if snapshot_root is None:
            raise ReviewError("invalid form nonce", "403 Forbidden")
        action = one("action")
        if action not in {"approve", "reject"}:
            raise ReviewError("invalid action")
        relative = validate_relative_path(rough_path, ROUGH_PREFIX)
        root=Path(snapshot_root) if snapshot_root else (self.repository_source.current() if hasattr(self.repository_source,"current") else self.root)
        binding = rough_binding_at(root, relative)
        if not hmac.compare_digest(one("rough_sha256"), binding.sha256) or not hmac.compare_digest(one("rough_version"), binding.version):
            raise ReviewError("rough changed", "409 Conflict")
        if _frontmatter(binding.content).get("status") != "pending_review":
            raise ReviewError("rough is not pending", "409 Conflict")
        wiki_path = one("wiki_path")
        # Browsers submit <textarea> fields with CRLF line endings per the HTML
        # spec regardless of OS; .gitattributes normalizes *.md to LF on `git
        # add`, so an un-normalized candidate would never byte-match what the
        # publisher actually commits (deploy/release_bundle.py prepare_change()).
        candidate = one("candidate_markdown").replace("\r\n", "\n").replace("\r", "\n")
        if action == "approve":
            validate_relative_path(wiki_path, WIKI_PREFIX)
            if not candidate.strip():
                raise ReviewError("approve requires candidate markdown")
            _frontmatter(candidate)
        else:
            # Both decision buttons share one <form>; reject may submit
            # whatever wiki_path/candidate_markdown were left over from an
            # in-progress approve edit. Ignore rather than reject -- a
            # non-approve decision never uses either field.
            wiki_path = ""
            candidate = ""
        if len(candidate.encode("utf-8")) > 900_000:
            raise ReviewError("field too large", "413 Payload Too Large")
        decision_id = secrets.token_urlsafe(24)
        commit=subprocess.check_output(["/usr/bin/git","rev-parse","HEAD^{commit}"],cwd=root,env={"PATH":"/usr/bin:/bin","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null"},text=True).strip()
        tree=subprocess.check_output(["/usr/bin/git","rev-parse","HEAD^{tree}"],cwd=root,env={"PATH":"/usr/bin:/bin","GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":"/dev/null"},text=True).strip()
        bundle_digest=getattr(self.repository_source,"_digest","")
        if not re.fullmatch(r"[0-9a-f]{64}",bundle_digest): bundle_digest=hashlib.sha256((commit+"\0"+tree).encode()).hexdigest()
        record = {
            "schema_version": 2, "record_type": "decision", "decision_id": decision_id,
            "created_at": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(timespec="seconds"),
            "reviewer_digest": "hmac-sha256:" + hmac.new(self.audit_key, user_id.encode(), hashlib.sha256).hexdigest(),
            "action": action, "rough_path": binding.path, "rough_sha256": binding.sha256,
            "rough_version": binding.version, "wiki_path": wiki_path, "candidate_markdown": candidate,
            # Kept in the signed record schema (the publisher and the MAC expect
            # the field) but no longer collected: always empty.
            "comment": "", "snapshot_commit":commit,"snapshot_tree":tree,"snapshot_bundle_sha256":bundle_digest,
        }
        record["decision_mac"] = decision_mac(record, self.queue_key)
        # The decision queue stays append-only and MAC-bound; a later decision for the
        # same rough supersedes the earlier one for display and for the publisher, which
        # must select the newest valid record per rough_path.
        with queue_lock(self.queue_path):
            append_record(self.queue_path, record, already_locked=True)
            self.nonces.invalidate(nonce)
        if self.labels is not None:
            self.labels.append(decision_id, reviewer_label)
        return decision_id


@contextmanager
def queue_lock(queue_path: Path, *, read_only: bool = False):
    import fcntl
    lock_path = queue_path.with_name(queue_path.name + ".lock")
    parent_existed = lock_path.parent.exists()
    if not read_only:
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        descriptor = os.open(lock_path.parent.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    if lock_path.is_symlink():
        raise ReviewError("queue lock cannot be a symlink", "500 Internal Server Error")
    flags = os.O_RDONLY if read_only else os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o660)
    except OSError as exc:
        raise ReviewError("queue lock unavailable", "500 Internal Server Error") from exc
    try:
        details=os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink!=1:
            raise ReviewError("queue lock is unsafe", "500 Internal Server Error")
        if not read_only: os.fchmod(descriptor, 0o660)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def append_record(path: Path, record: dict, *, already_locked: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (nullcontext() if already_locked else queue_lock(path)):
        if path.exists() and path.is_symlink():
            raise ReviewError("queue cannot be a symlink", "500 Internal Server Error")
        created = not path.exists()
        flags = os.O_APPEND | os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o660)
        except OSError as exc:
            raise ReviewError("queue unavailable", "500 Internal Server Error") from exc
        try:
            os.fchmod(descriptor, 0o660)
            payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if len(payload) > MAX_DECISION_BYTES:
                raise ReviewError("queue record exceeds decision limit", "413 Payload Too Large")
            def write_all(value: bytes) -> None:
                remaining = memoryview(value)
                while remaining:
                    try:
                        written = os.write(descriptor, remaining)
                    except InterruptedError:
                        continue
                    if written <= 0:
                        raise ReviewError("queue write failed", "500 Internal Server Error")
                    remaining = remaining[written:]
            size = os.fstat(descriptor).st_size
            if size and os.pread(descriptor, 1, size - 1) != b"\n":
                write_all(b"\n")
            write_all(payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try: os.fsync(parent)
            finally: os.close(parent)


def iter_valid_decisions(path: Path, key: bytes, *, quarantine_dir: Path | None = None, on_corrupt=None):
    """Yield MAC-valid decisions and durably quarantine every corrupt line."""
    def quarantine(raw: bytes, line_number: int, reason: str) -> None:
        if quarantine_dir is None:
            return
        import base64
        directory=Path(quarantine_dir); directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        digest=hashlib.sha256(raw).hexdigest()
        target=directory/(f"{line_number:020d}-{digest}.json")
        if not target.exists():
            record={"status":"failed","error_type":"CorruptQueueRecord","line":line_number,"reason":reason,
                    "sha256":digest,"raw_base64":base64.b64encode(raw).decode("ascii")}
            temporary=target.with_name("."+target.name+".tmp")
            with temporary.open("w",encoding="utf-8") as handle:
                json.dump(record,handle,sort_keys=True,separators=(",",":")); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary,0o600); os.replace(temporary,target)
            parent=os.open(directory,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
            try: os.fsync(parent)
            finally: os.close(parent)
        if on_corrupt is not None: on_corrupt({"status":"failed","error_type":"CorruptQueueRecord","line":line_number,"reason":reason,"sha256":digest})
    try:
        with path.open("rb") as handle:
            for line_number,raw in enumerate(handle,1):
                reason=None
                if len(raw) > MAX_DECISION_BYTES: reason="oversized"
                elif not raw.endswith(b"\n"): reason="truncated"
                else:
                    try:
                        value=json.loads(raw.decode("utf-8"))
                        if not isinstance(value,dict) or value.get("record_type")!="decision": reason="invalid-record"
                        else:
                            supplied=value.get("decision_mac")
                            if not isinstance(supplied,str) or not hmac.compare_digest(supplied,decision_mac(value,key)): reason="invalid-mac"
                            else:
                                yield value
                                continue
                    except UnicodeDecodeError: reason="invalid-utf8"
                    except (json.JSONDecodeError,ValueError,TypeError): reason="invalid-json"
                quarantine(raw,line_number,reason or "invalid")
    except FileNotFoundError:
        return
