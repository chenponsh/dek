from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class SafetyStop(RuntimeError):
    pass


@dataclass(frozen=True)
class Row:
    question: str
    answer: str
    date: str

    @property
    def key(self) -> tuple[str, str]:
        return (normalize(self.question), self.date[:10])


def normalize(value: str) -> str:
    value = re.sub(r"\[([^]]+)\]\((https?://[^)]+)\)", lambda m: m.group(2) if m.group(1) == m.group(2) else m.group(1), value or "")
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", value).strip()


def markdown_cell(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>", "\n", value or "")
    value = re.sub(
        r"(?is)<a\b[^>]*?href=[\"'](https?://[^\"']+)[\"'][^>]*>(.*?)</a>",
        lambda match: f"[{re.sub(r'<[^>]+>', '', match.group(2)).strip() or match.group(1)}]({match.group(1)})",
        value,
    )
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"\n+", "<br>", value.strip())
    return value.replace("|", r"\|")


def parse_table(note: str) -> list[Row]:
    marker = "## 内容"
    if marker not in note:
        raise SafetyStop("source note has no ## 内容 section")
    body = note.split(marker, 1)[1]
    rows: list[Row] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = re.split(r"(?<!\\)\|", line)[1:-1]
        cells = [c.strip().replace(r"\|", "|") for c in cells]
        if len(cells) != 3 or cells[0] == "问题" or all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        rows.append(Row(*cells))
    return rows


def replace_last_updated(note: str, day: str) -> str:
    changed, count = re.subn(r"(?m)^last_updated:\s*.*$", f"last_updated: {day}", note, count=1)
    if count != 1:
        raise SafetyStop("source note has no unique last_updated field")
    return changed


def last_updated(note: str) -> str:
    match = re.search(r"(?m)^last_updated:\s*(\d{4}-\d{2}-\d{2})\s*$", note)
    if not match:
        raise SafetyStop("source note has no valid last_updated field")
    return match.group(1)


# The separator row may be padded or aligned by an editor (e.g. Obsidian), so
# match its shape rather than the literal "| --- | --- | --- |".
CONTENT_TABLE_HEADER = re.compile(
    r"^\| 问题 \| 解答 \| 发布日期 \|[ \t]*\n\|(?:[ \t]*:?-+:?[ \t]*\|){3}[ \t]*\n", re.MULTILINE)


def note_urls(note: str) -> set[str]:
    """Article links (`### [title](url)（date）`) already in an article-layout note."""
    return set(re.findall(r"(?m)^###\s+\[[^\]]*\]\((https?://[^)\s]+)\)", note))


def insert_articles(note: str, rows: list[Row]) -> str:
    """Add one `### [title](url)（date）` section per new article, with its
    question/answer table, directly under `## 内容` (newest first, like the
    existing sections). `rows` carry article_title / article_url."""
    marker = re.search(r"(?m)^## 内容[ \t]*\n", note)
    if not marker:
        raise SafetyStop("source note has no ## 内容 section")
    groups: dict[str, list[Row]] = {}
    for row in rows:
        groups.setdefault(row.article_url, []).append(row)
    sections = []
    for url, group in groups.items():
        first = group[0]
        title = re.sub(r"[\[\]]", "", first.article_title)
        table = "".join(f"| {markdown_cell(r.question)} | {markdown_cell(r.answer)} | {r.date[:10]} |\n" for r in group)
        sections.append(
            f"### [{title}]({url})（{first.date[:10]}）\n\n| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n{table}\n"
        )
    rest = note[marker.end():].replace("_待整理。_", "").lstrip("\n")
    return note[:marker.end()] + "\n" + "".join(sections) + rest


def insert_rows(note: str, rows: list[Row]) -> str:
    if not rows:
        return note
    # The rules section may show the table format in a code block; only the
    # header under `## 内容` counts.
    start = note.find("## 内容")
    headers = list(CONTENT_TABLE_HEADER.finditer(note, max(start, 0)))
    if len(headers) != 1:
        raise SafetyStop("source note does not have one canonical content table")
    rendered = "".join(
        f"| {markdown_cell(r.question)} | {markdown_cell(r.answer)} | {r.date[:10]} |\n"
        for r in rows
    )
    end = headers[0].end()
    return note[:end] + rendered + note[end:]


def compare_rows(local: list[Row], remote: list[Row]) -> tuple[list[Row], list[dict[str, str]]]:
    local_by_key = {row.key: row for row in local}
    if len(local_by_key) != len(local):
        raise SafetyStop("duplicate question/date key in local table")
    additions: list[Row] = []
    revisions: list[dict[str, str]] = []
    for row in remote:
        old = local_by_key.get(row.key)
        if old is None:
            additions.append(row)
        elif normalize(old.answer) != normalize(markdown_cell(row.answer)):
            revisions.append({"question": row.question, "date": row.date[:10]})
    return additions, revisions


def merge_duplicate_rows(rows: list[Row]) -> list[Row]:
    """Merge rows sharing the same (question, date) key, joining answers with <br>.

    CDE can return the same question split across several records, and can also
    return fully identical duplicates. Distinct answers are joined in order;
    identical answers are dropped. Keeps a single row per key so compare_rows
    treats them as one entry.
    """
    groups: dict[tuple[str, str], list[Row]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        if row.key not in groups:
            groups[row.key] = []
            order.append(row.key)
        groups[row.key].append(row)
    result: list[Row] = []
    for key in order:
        group = groups[key]
        seen: list[str] = []
        for row in group:
            if row.answer not in seen:
                seen.append(row.answer)
        result.append(Row(group[0].question, "<br>".join(seen), group[0].date))
    return result


def restore_book_titles(text: str) -> str:
    """Restore CDE's angle-bracketed document titles <名称> to 《名称》.

    CDE encodes book titles (文件/程序/通告名称) as <中文内容>, which would
    otherwise be stripped as HTML tags. Only angle brackets whose content
    starts with a non-ASCII character are treated as book titles; real HTML
    tags such as <br> and <a href=...> are left untouched.
    """
    return re.sub(r"<([^\x00-\x7f][^<>]*)>", r"《\1》", text)


def repo_fingerprint(root: Path, config_path: Path) -> str:
    head = git(root, "rev-parse", "HEAD")
    digest = hashlib.sha256()
    digest.update(f"{head}\n".encode())
    # Paths are hashed relative to config_path's own directory, not root:
    # in the sandboxed source-ingest entrypoint, root is a freshly cloned
    # content checkout while this automation code (and config_path) is
    # loaded from a separately installed package tree -- the two are not
    # nested under each other, so relative_to(root) raised ValueError the
    # first time this ran outside a plain same-tree checkout.
    for path in sorted(config_path.parent.glob("*.py")) + [config_path]:
        digest.update(str(path.relative_to(config_path.parent)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if proc.returncode:
        raise SafetyStop(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def porcelain_path(line: str) -> str:
    if line.startswith(("?? ", "!! ")):
        return line[3:]
    if len(line) >= 3 and line[1:3] == "  ":
        return line[3:]
    if len(line) >= 2 and line[1] == " ":
        return line[2:]
    if len(line) >= 3 and line[2] == " ":
        return line[3:]
    raise SafetyStop(f"unrecognized git status entry: {line!r}")


def assert_git_safe(root: Path, allowed_dirty: set[str] | None = None) -> None:
    git(root, "fetch", "origin", "--prune")
    if git(root, "branch", "--show-current") != "main":
        raise SafetyStop("current branch is not main")
    dirty = git(root, "status", "--porcelain").splitlines()
    unexpected = [line for line in dirty if porcelain_path(line) not in (allowed_dirty or set()) and not any(porcelain_path(line).startswith(p.rstrip("/") + "/") for p in (allowed_dirty or set()))]
    if unexpected:
        raise SafetyStop(f"working tree has unrelated changes: {unexpected}")
    counts = git(root, "rev-list", "--left-right", "--count", "HEAD...origin/main").split()
    if counts != ["0", "0"]:
        raise SafetyStop(f"main differs from origin/main: ahead/behind={counts}")


def reconcile_remote(root: Path) -> None:
    """Align local main with origin/main inside the process lock.

    Called before the scheduled run's safety gate. Recovers the two states a
    previously interrupted run can leave behind without manual intervention:
      - behind only (ahead==0, behind>0) with a clean tree -> fast-forward;
      - ahead only (ahead>0, behind==0) with a clean tree where every ahead
        commit is an automated ingestion commit -> retry push.
    Diverged histories, dirty trees, or non-ingestion ahead commits stop.
    """
    git(root, "fetch", "origin", "--prune")
    counts = git(root, "rev-list", "--left-right", "--count", "HEAD...origin/main").split()
    ahead, behind = int(counts[0]), int(counts[1])
    if ahead == 0 and behind == 0:
        return
    dirty = git(root, "status", "--porcelain").splitlines()
    if ahead == 0 and behind > 0:
        if dirty:
            raise SafetyStop(f"working tree not clean; cannot fast-forward: {dirty}")
        git(root, "merge", "--ff-only", "origin/main")
        return
    if ahead > 0 and behind == 0:
        if dirty:
            raise SafetyStop(f"working tree not clean; cannot retry push: {dirty}")
        subjects = git(root, "log", "--format=%s", "origin/main..HEAD").splitlines()
        if not subjects or not all(subject.startswith("ingestion:") for subject in subjects):
            raise SafetyStop("ahead commits are not automated ingestion commits; refusing push")
        git(root, "push", "origin", "main")
        return
    raise SafetyStop(f"local and remote main have diverged: ahead/behind={counts}")


def report_path(root: Path, dry_run: bool, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%d_%H%M")
    if dry_run:
        return root / "_" / "ingestion" / f"dry-run-{stamp}.json"
    return root / "ingestion" / "logs" / f"source_ingest_{stamp}_report.json"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def workspace_snapshot(root: Path) -> str:
    return hashlib.sha256((git(root, "status", "--porcelain", "--untracked-files=all") + "\n").encode()).hexdigest()


def git_status_paths(root: Path) -> set[str]:
    return {
        porcelain_path(line)
        for line in git(root, "-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all").splitlines()
    }


@contextmanager
def ingestion_lock(root: Path):
    import fcntl
    path = root / "_" / "ingestion" / "source-ingest.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    os.chmod(path, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SafetyStop("another ingestion process is already running") from exc
        yield
    finally:
        handle.close()


@contextmanager
def atomic_write_batch(root: Path, writes: dict[Path, str]):
    if git_status_paths(root):
        raise SafetyStop("working tree changed before atomic write")
    originals = {path: (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None for path in writes}
    staged: dict[Path, Path] = {}
    try:
        for path, content in writes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
                os.fchmod(tmp.fileno(), originals[path][1] if originals[path] is not None else 0o644)
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
                staged[path] = Path(tmp.name)
        temporary_paths = {str(path.relative_to(root)) for path in staged.values()}
        if git_status_paths(root) - temporary_paths:
            raise SafetyStop("working tree changed during atomic write preparation")
        for path, temporary in staged.items():
            os.replace(temporary, path)
        yield
    except Exception:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                content, mode = original
                with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as tmp:
                    os.fchmod(tmp.fileno(), mode)
                    tmp.write(content)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    restore = Path(tmp.name)
                os.replace(restore, path)
        raise
