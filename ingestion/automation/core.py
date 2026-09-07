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
        if len(cells) != 3 or cells[0] in {"问题", "---"}:
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


def insert_rows(note: str, rows: list[Row]) -> str:
    if not rows:
        return note
    header = "| 问题 | 解答 | 发布日期 |\n| --- | --- | --- |\n"
    if note.count(header) != 1:
        raise SafetyStop("source note does not have one canonical content table")
    rendered = "".join(
        f"| {markdown_cell(r.question)} | {markdown_cell(r.answer)} | {r.date[:10]} |\n"
        for r in rows
    )
    return note.replace(header, header + rendered, 1)


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


def repo_fingerprint(root: Path, config_path: Path) -> str:
    head = git(root, "rev-parse", "HEAD")
    digest = hashlib.sha256()
    digest.update(f"{head}\n".encode())
    for path in sorted(config_path.parent.glob("*.py")) + [config_path]:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if proc.returncode:
        raise SafetyStop(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def assert_git_safe(root: Path, allowed_dirty: set[str] | None = None) -> None:
    git(root, "fetch", "origin", "--prune")
    if git(root, "branch", "--show-current") != "main":
        raise SafetyStop("current branch is not main")
    dirty = git(root, "status", "--porcelain").splitlines()
    unexpected = [line for line in dirty if line[3:] not in (allowed_dirty or set()) and not any(line[3:].startswith(p.rstrip("/") + "/") for p in (allowed_dirty or set()))]
    if unexpected:
        raise SafetyStop(f"working tree has unrelated changes: {unexpected}")
    counts = git(root, "rev-list", "--left-right", "--count", "HEAD...origin/main").split()
    if counts != ["0", "0"]:
        raise SafetyStop(f"main differs from origin/main: ahead/behind={counts}")


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
    return {line[3:] for line in git(root, "status", "--porcelain", "--untracked-files=all").splitlines()}


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
    originals = {path: path.read_bytes() if path.exists() else None for path in writes}
    staged: dict[Path, Path] = {}
    try:
        for path, content in writes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
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
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as tmp:
                    tmp.write(content)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    restore = Path(tmp.name)
                os.replace(restore, path)
        raise
