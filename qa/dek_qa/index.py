from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

INDEX_VERSION = 4
BUILDER_VERSION = "4"
WIKILINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
SEGMENT_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"\'')
    return fields, text[end + 5 :]


def _safe_markdown_files(root: Path) -> Iterable[Path]:
    root = root.resolve(strict=True)
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink():
            continue
        resolved = path.resolve(strict=True)
        if resolved.is_relative_to(root) and resolved.is_file():
            yield resolved


def _is_excluded_path(path: Path) -> bool:
    return any("排除" in part for part in path.parts)


def _input_digest(vault: Path, roots: Iterable[Path]) -> str:
    manifest = hashlib.sha256()
    for root in roots:
        if not root.exists():
            continue
        for path in _safe_markdown_files(root):
            if _is_excluded_path(path.relative_to(root)):
                continue
            relative = path.relative_to(vault).as_posix().encode("utf-8")
            content_digest = hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii")
            manifest.update(relative + b"\0" + content_digest + b"\n")
    return manifest.hexdigest()


def _git_last_updates(vault: Path) -> dict[str, str]:
    """Return the last committed timestamp for each formal wiki path."""
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "log",
            "--format=@@%cI",
            "--name-only",
            "--",
            "wiki",
        ],
        cwd=vault,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return {}
    current = ""
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if line.startswith("@@"):
            current = line[2:].strip()
        elif line and current and line not in result:
            result[line] = current
    return result


def _tokens(text: str) -> Counter[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    result: list[str] = []
    for segment in SEGMENT_RE.findall(normalized):
        if all("\u3400" <= char <= "\u9fff" for char in segment):
            result.extend(segment)
            result.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        else:
            result.append(segment)
    return Counter(result)


def _source_catalog(source_root: Path) -> dict[str, dict[str, str]]:
    catalog: dict[str, dict[str, str]] = {}
    stems: dict[str, str] = {}
    if not source_root.exists():
        return catalog
    for path in _safe_markdown_files(source_root):
        if _is_excluded_path(path.relative_to(source_root)):
            continue
        relative = path.relative_to(source_root).with_suffix("").as_posix()
        if path.stem in stems and stems[path.stem] != relative:
            raise ValueError(f"ambiguous source stem: {path.stem}")
        stems[path.stem] = relative
        text = path.read_text(encoding="utf-8-sig")
        fields, _ = _frontmatter(text)
        # Source notes in this vault historically use ``url`` while some
        # newer/fixture notes use the more explicit ``source_url``. Both are
        # authoritative source-note metadata and resolve identically.
        url = fields.get("source_url") or fields.get("url", "")
        parsed = urlsplit(url)
        if (
            url != url.strip()
            or any(char.isspace() for char in url)
            or parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            continue
        metadata = {
            "url": url,
            "name": fields.get("source_name") or fields.get("article_title") or fields.get("entity", ""),
            "type": fields.get("source_type", ""),
        }
        catalog[relative] = metadata
        catalog[path.stem] = metadata
    return catalog


def _source_evidence(
    text: str, fields: dict[str, str], catalog: dict[str, dict[str, str]]
) -> tuple[list[str], str, list[str], list[str]]:
    urls: set[str] = set()
    names: set[str] = set()
    types: set[str] = set()
    # A bare wikilink is authoritative only inside the frontmatter ``source``
    # field. In the body it may be an ordinary wiki reference whose stem just
    # happens to match a source note, so body mappings must be path-qualified.
    field_targets = WIKILINK_RE.findall(fields.get("source", ""))
    body_targets = [
        target for target in WIKILINK_RE.findall(text) if target.startswith("source/")
    ]
    source_targets: list[str] = []
    for target in (*field_targets, *body_targets):
        key = target.removeprefix("source/").removesuffix(".md")
        if key in catalog:
            source_targets.append(target)
            metadata = catalog[key]
            urls.add(metadata["url"])
            if metadata["name"]:
                names.add(metadata["name"])
            if metadata["type"]:
                types.add(metadata["type"])
    if urls:
        status = "verified"
    elif fields.get("source") or body_targets:
        status = "unknown"
    else:
        status = "none"
    return sorted(urls), status, sorted(names), sorted(types)


def build_index(vault: Path, output: Path) -> dict[str, Any]:
    vault = vault.resolve(strict=True)
    wiki_root = (vault / "wiki").resolve(strict=True)
    source_root = vault / "source"
    catalog = _source_catalog(source_root)
    git_updates = _git_last_updates(vault)
    docs: list[dict[str, Any]] = []
    for path in _safe_markdown_files(wiki_root):
        if _is_excluded_path(path.relative_to(wiki_root)):
            continue
        raw = path.read_text(encoding="utf-8-sig")
        fields, body = _frontmatter(raw)
        relative = path.relative_to(vault).as_posix()
        title = fields.get("question") or path.stem
        publication_date = fields.get("date", "")
        try:
            date.fromisoformat(publication_date)
        except ValueError:
            publication_date = ""
        source_urls, source_status, source_names, source_types = _source_evidence(body, fields, catalog)
        docs.append(
            {
                "id": hashlib.sha256(relative.encode()).hexdigest()[:24],
                "path": relative,
                "title": title,
                "content": body.strip(),
                "source_urls": source_urls,
                "source_names": source_names,
                "source_types": source_types,
                "source_status": source_status,
                "publication_date": publication_date or None,
                "updated_at": git_updates.get(relative),
            }
        )
    payload = {
        "version": INDEX_VERSION,
        "metadata": {
            "builder_version": BUILDER_VERSION,
            "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "input_sha256": _input_digest(vault, (wiki_root, source_root)),
            "document_count": len(docs),
        },
        "documents": docs,
    }
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".index-", dir=output.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, output)
        os.chmod(output, 0o600)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return payload


class KnowledgeBase:
    def __init__(self, index_path: Path):
        data = json.loads(index_path.read_text(encoding="utf-8"))
        version = data.get("version")
        if version not in {2, 3, INDEX_VERSION}:
            raise ValueError("unsupported index version")
        if version in {2, 3}:
            for doc in data["documents"]:
                doc["source_urls"] = doc.pop("official_urls", [])
                doc.setdefault("source_names", [])
                doc.setdefault("source_types", [])
        self._documents = {doc["id"]: doc for doc in data["documents"]}

    def dek_kb_search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query = query.strip()
        if not query or len(query) > 500:
            return []
        wanted = _tokens(query)
        required = {token for token in wanted if len(token) >= 2}
        if not required:
            return []
        scored = []
        for doc in self._documents.values():
            haystack = _tokens(f'{doc["title"]}\n{doc["content"]}')
            if not any(haystack[token] for token in required):
                continue
            score = sum(min(count, haystack[token]) for token, count in wanted.items())
            if score:
                scored.append((score, doc["path"], doc))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            {
                "id": doc["id"],
                "title": doc["title"],
                "path": doc["path"],
                "source_urls": doc["source_urls"],
                "source_names": doc["source_names"],
                "source_types": doc["source_types"],
                "score": score,
            }
            for score, _, doc in scored[: max(1, min(limit, 10))]
        ]

    def dek_kb_get(self, document_id: str) -> dict[str, Any] | None:
        doc = self._documents.get(document_id)
        return dict(doc) if doc else None

    def dek_kb_recent(
        self, days: int = 7, as_of: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        end = date.fromisoformat(as_of) if as_of else datetime.now().astimezone().date()
        start = end - timedelta(days=days - 1)

        def in_window(value: str | None) -> bool:
            if not value:
                return False
            try:
                observed = date.fromisoformat(value[:10])
            except ValueError:
                return False
            return start <= observed <= end

        updates = [doc for doc in self._documents.values() if in_window(doc.get("updated_at"))]
        publications = [
            doc for doc in self._documents.values() if in_window(doc.get("publication_date"))
        ]
        updates.sort(key=lambda doc: (doc.get("updated_at") or "", doc["path"]), reverse=True)
        publications.sort(
            key=lambda doc: (doc.get("publication_date") or "", doc["path"]), reverse=True
        )

        def summary(doc: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": doc["id"],
                "title": doc["title"],
                "path": doc["path"],
                "publication_date": doc.get("publication_date"),
                "updated_at": doc.get("updated_at"),
            }

        return {
            "as_of": end.isoformat(),
            "window_start": start.isoformat(),
            "days": days,
            "knowledge_base_update_count": len(updates),
            "publication_count": len(publications),
            "knowledge_base_updates": [summary(doc) for doc in updates[:limit]],
            "recent_publications": [summary(doc) for doc in publications[:limit]],
            "definitions": {
                "knowledge_base_updates": "正式 wiki 笔记的 Git 最后提交日期位于时间窗口内",
                "recent_publications": "正式 wiki 笔记 frontmatter 的来源发布日期位于时间窗口内",
            },
        }
