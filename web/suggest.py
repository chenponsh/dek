"""Suggest which wiki folder a new rough draft belongs in.

The suggestion is the folder holding the already-filed entries whose text is
most similar to the draft (character bigrams, TF-IDF, cosine similarity, the
five nearest entries voting). Measured by leaving each of the 955 filed
entries out in turn, the first suggestion is the right folder about 54% of the
time and one of the first three about 73% of the time - a starting point for
the reviewer, never a decision.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

# Below this best-neighbour similarity nothing is suggested: a blank box makes
# the reviewer choose, a confident-looking wrong folder invites a rubber stamp.
MIN_SIMILARITY = 0.10
NEIGHBOURS = 5

_NOISE = re.compile(r"[\s，。、；：？！（）()“”\"'《》\[\]|·\-—/\\<>*#`~0-9a-zA-Z]")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.S)
_QUESTION = re.compile(r'(?m)^question:\s*"?(.*?)"?\s*$')
_NUMBERED = re.compile(r"(?m)^no:\s*\d")


def _grams(text: str) -> list[str]:
    text = _NOISE.sub("", text)
    return [text[i:i + 2] for i in range(len(text) - 1)]


def _counts(question: str, body: str) -> Counter:
    counts = Counter(_grams(question))
    counts.update(_grams(question))  # the question counts double
    counts.update(_grams(body))
    return counts


class FolderIndex:
    def __init__(self, entries: list[tuple[str, str, str]]):
        self.folders = [folder for folder, _, _ in entries]
        counts = [_counts(question, body) for _, question, body in entries]
        document_frequency: Counter = Counter()
        for c in counts:
            document_frequency.update(c.keys())
        total = len(entries)
        self._idf = {g: math.log((total + 1) / (n + 1)) + 1 for g, n in document_frequency.items()}
        self._vectors = [self._normalise(c) for c in counts]
        self.size = total

    def _normalise(self, counts: Counter) -> dict[str, float]:
        weights = {g: (1 + math.log(c)) * self._idf.get(g, 1.0) for g, c in counts.items()}
        length = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        return {g: w / length for g, w in weights.items()}

    def rank(self, question: str, answer: str) -> tuple[list[str], float]:
        """Folders best first, and the similarity of the single nearest entry."""
        if not self._vectors:
            return [], 0.0
        query = self._normalise(_counts(question, answer))
        if not query:
            return [], 0.0
        scored = []
        for index, vector in enumerate(self._vectors):
            small, large = (query, vector) if len(query) <= len(vector) else (vector, query)
            scored.append((sum(w * large.get(g, 0.0) for g, w in small.items()), index))
        scored.sort(reverse=True)
        votes: dict[str, float] = defaultdict(float)
        for similarity, index in scored[:NEIGHBOURS]:
            votes[self.folders[index]] += similarity
        ranked = sorted(votes.items(), key=lambda item: (-item[1], item[0]))
        return [folder for folder, _ in ranked], scored[0][0]


def _load_entries(root: Path) -> list[tuple[str, str, str]]:
    wiki = root / "wiki"
    if not wiki.is_dir():
        return []
    entries = []
    for path in sorted(wiki.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = _FRONTMATTER.match(text)
        if not match:
            continue
        front, body = match.groups()
        question = _QUESTION.search(front)
        # Overview pages have no number and no question: they are not evidence.
        if not question or not question.group(1).strip() or not _NUMBERED.search(front):
            continue
        entries.append((path.parent.relative_to(root).as_posix(), question.group(1).strip(), body.strip()))
    return entries


@lru_cache(maxsize=4)
def folder_index(root: str) -> FolderIndex:
    # A review clone lives in a digest-named directory and never changes, so the
    # path alone identifies its contents.
    return FolderIndex(_load_entries(Path(root)))


def suggest_folders(root: Path, question: str, answer: str, limit: int = 3) -> list[str]:
    """Up to `limit` wiki folders (as "wiki/...") for a draft, best first; [] when unsure."""
    ranked, best = folder_index(str(root)).rank(question or "", answer or "")
    if best < MIN_SIMILARITY:
        return []
    return ranked[:limit]


def rough_qa(content: str) -> tuple[str, str]:
    """The (question, answer) of the first row of a rough draft's Q&A table."""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped)[1:-1]]
        if len(cells) < 3 or cells[0] in {"问题", "解答"}:
            continue
        clean = lambda value: value.replace("<br>", "\n").replace("\\|", "|").strip()
        return clean(cells[0]), clean(cells[1])
    return "", ""


def read_suggestion(path: Path | None, rough_path: str, rough_sha256: str) -> str:
    """The folder a model suggested for this exact version of a draft, or "".

    Anything missing, damaged or made for a different version of the draft
    counts as no suggestion; the caller still checks the folder exists.
    """
    if path is None:
        return ""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entry = data["items"][rough_path]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if not isinstance(entry, dict) or entry.get("rough_sha256") != rough_sha256:
        return ""
    folder = entry.get("folder")
    return folder if isinstance(folder, str) and folder.startswith("wiki/") else ""
