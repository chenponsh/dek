from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

INDEX_VERSION = 2
BUILDER_VERSION = "2"
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


def _source_catalog(source_root: Path) -> dict[str, str]:
    catalog: dict[str, str] = {}
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
        url = fields.get("source_url", "")
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
        catalog[relative] = url
        catalog[path.stem] = url
    return catalog


def _source_evidence(
    text: str, fields: dict[str, str], catalog: dict[str, str]
) -> tuple[list[str], str]:
    urls: set[str] = set()
    references = "\n".join((fields.get("source", ""), text))
    targets = WIKILINK_RE.findall(references)
    source_targets = [target for target in targets if target.startswith("source/")]
    for target in source_targets:
        key = target.removeprefix("source/").removesuffix(".md")
        if key in catalog:
            urls.add(catalog[key])
    if urls:
        status = "verified"
    elif fields.get("source") or source_targets:
        status = "unknown"
    else:
        status = "none"
    return sorted(urls), status


def build_index(vault: Path, output: Path) -> dict[str, Any]:
    vault = vault.resolve(strict=True)
    wiki_root = (vault / "wiki").resolve(strict=True)
    source_root = vault / "source"
    catalog = _source_catalog(source_root)
    docs: list[dict[str, Any]] = []
    for path in _safe_markdown_files(wiki_root):
        if _is_excluded_path(path.relative_to(wiki_root)):
            continue
        raw = path.read_text(encoding="utf-8-sig")
        fields, body = _frontmatter(raw)
        relative = path.relative_to(vault).as_posix()
        title = fields.get("question") or path.stem
        official_urls, source_status = _source_evidence(body, fields, catalog)
        docs.append(
            {
                "id": hashlib.sha256(relative.encode()).hexdigest()[:24],
                "path": relative,
                "title": title,
                "content": body.strip(),
                "official_urls": official_urls,
                "source_status": source_status,
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
        if data.get("version") != INDEX_VERSION:
            raise ValueError("unsupported index version")
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
                "official_urls": doc["official_urls"],
                "score": score,
            }
            for score, _, doc in scored[: max(1, min(limit, 10))]
        ]

    def dek_kb_get(self, document_id: str) -> dict[str, Any] | None:
        doc = self._documents.get(document_id)
        return dict(doc) if doc else None
