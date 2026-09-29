"""Shared, deterministic rules for wiki categories and single-note moves."""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_CATEGORY = re.compile(r"^(?P<code>\d{2}(?:\d{2}){0,2})_(?P<title>.+)$")
_LEAF = re.compile(r"^(?P<code>\d{2}(?:\d{2}){0,2})-(?P<number>\d{4})\.md$")
_BAD_TITLE = re.compile(r'[<>:"/\\|?*]')
_MARKDOWN_LINK = re.compile(r"(?P<prefix>!?\[[^\]\n]*\]\()(?P<target>[^)\n]+)(?P<suffix>\))")


class TaxonomyError(ValueError):
    pass


@dataclass(frozen=True)
class CategoryPlan:
    directory: str
    page_path: str
    page_markdown: str


def _category_parts(path: PurePosixPath) -> tuple[tuple[str, str], ...]:
    if len(path.parts) < 3 or path.parts[0] != "wiki" or len(path.parts[1:-1]) > 3:
        raise TaxonomyError("wiki target must use one to three category levels")
    result: list[tuple[str, str]] = []
    parent_code = ""
    for level, part in enumerate(path.parts[1:-1], start=1):
        match = _CATEGORY.fullmatch(part)
        if not match:
            raise TaxonomyError("category name must be code_title")
        code, title = match.group("code"), match.group("title")
        if len(code) != level * 2 or (parent_code and not code.startswith(parent_code)):
            raise TaxonomyError("category code does not match its parent")
        if (not title.strip() or title != title.strip() or _BAD_TITLE.search(title)
                or "排除" in title or part in {"_images", "_attachments"}):
            raise TaxonomyError("category title is invalid")
        result.append((code, part))
        parent_code = code
    return tuple(result)


def category_page_markdown(category_parts: tuple[tuple[str, str], ...]) -> str:
    if not category_parts:
        raise TaxonomyError("category is missing")
    names = [name for _, name in category_parts]
    tag = "/".join(names)
    if len(names) == 1:
        master = ""
    else:
        parent = "/".join(names[:-1])
        master = f'"[[wiki/{parent}/{names[-2]}]]"'
    directory = "wiki/" + tag
    return (
        "---\n"
        f"aliases:\n  - \"#{tag}\"\n"
        f"tags:\n  - \"{tag}\"\n"
        f"master: {master}\n"
        "cssclasses:\n  - dv-compact-table\n  - dv-overview-table\n"
        "---\n\n"
        "```dataview\n"
        "TABLE WITHOUT ID\n"
        "  file.link AS 项目,\n"
        "  question AS 问题,\n"
        "  source AS 来源,\n"
        "  dateformat(date, \"yyyy-MM-dd\") AS 日期\n"
        f'FROM "{directory}"\n'
        "WHERE no != null\n"
        "SORT file.folder ASC, no ASC\n"
        "```\n"
    )


def inspect_wiki_target(root: Path, raw_path: str) -> CategoryPlan | None:
    """Validate a leaf target and plan its missing final category, if any.

    Only one missing category level is accepted; its parent must already exist
    (the wiki root also counts). This keeps a mistyped path from silently
    creating a hierarchy the reviewer did not intend.
    """
    if not isinstance(raw_path, str) or "\\" in raw_path or "\x00" in raw_path:
        raise TaxonomyError("invalid wiki target")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise TaxonomyError("invalid wiki target")
    parts = _category_parts(path)
    leaf = _LEAF.fullmatch(path.name)
    if not leaf or leaf.group("code") != parts[-1][0]:
        raise TaxonomyError("knowledge filename must match the category code and four-digit number")

    root = Path(root).resolve(strict=True)
    current = root / "wiki"
    if not current.is_dir() or current.is_symlink():
        raise TaxonomyError("wiki root is unavailable")
    missing_at: int | None = None
    incomplete_existing = False
    for index, (_, name) in enumerate(parts):
        current = current / name
        if current.exists():
            if not current.is_dir() or current.is_symlink():
                raise TaxonomyError("category path is unsafe")
            home = current / f"{name}.md"
            if not home.is_file() or home.is_symlink():
                incomplete_existing = True
        else:
            missing_at = index
            break
    if missing_at is None:
        return None
    if incomplete_existing:
        raise TaxonomyError("parent category has no safe category page")
    if missing_at != len(parts) - 1:
        raise TaxonomyError("only one category level may be created at a time")
    directory = PurePosixPath(*path.parts[:-1]).as_posix()
    name = path.parts[-2]
    return CategoryPlan(
        directory=directory,
        page_path=f"{directory}/{name}.md",
        page_markdown=category_page_markdown(parts),
    )


def rewrite_note_for_target(text: str, old_path: str, new_path: str) -> str:
    """Move taxonomy metadata and local attachment links without changing prose."""
    new = PurePosixPath(new_path)
    parts = _category_parts(new)
    leaf = _LEAF.fullmatch(new.name)
    if not leaf or leaf.group("code") != parts[-1][0]:
        raise TaxonomyError("invalid move target")
    front = re.match(r"\A---\n(?P<body>[\s\S]*?)\n---(?P<ending>\n|\Z)", text.replace("\r\n", "\n").replace("\r", "\n"))
    if not front:
        raise TaxonomyError("knowledge note has no valid frontmatter")
    tags = ["/".join(name for _, name in parts[:index + 1]) for index in range(len(parts))]
    tag_pages = [f'  - "[[{name}]]"' for _, name in parts]
    tag_values = [f'  - "{tag}"' for tag in tags]
    lines = front.group("body").split("\n")
    out: list[str] = []
    found = {"no": False, "tag_pages": False, "tags": False}
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("no:"):
            out.append(f"no: {int(leaf.group('number'))}")
            found["no"] = True
            index += 1
        elif line in {"tag_pages:", "tags:"}:
            key = line[:-1]
            out.append(line)
            out.extend(tag_pages if key == "tag_pages" else tag_values)
            found[key] = True
            index += 1
            while index < len(lines) and re.match(r"^\s+- ", lines[index]):
                index += 1
        else:
            out.append(line)
            index += 1
    if not all(found.values()):
        raise TaxonomyError("knowledge note is missing taxonomy metadata")
    rebuilt = "---\n" + "\n".join(out) + "\n---" + front.group("ending") + text.replace("\r\n", "\n").replace("\r", "\n")[front.end():]

    old_dir = PurePosixPath(old_path).parent
    new_dir = new.parent

    def relocate(match: re.Match[str]) -> str:
        target = match.group("target")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith(("/", "#", "<")):
            return match.group(0)
        path_part, marker, suffix = target.partition("#")
        resolved = PurePosixPath(posixpath.normpath((old_dir / path_part).as_posix()))
        if not resolved.parts or resolved.parts[0] != "wiki" or len(resolved.parts) < 2 or resolved.parts[1] not in {"_images", "_attachments"}:
            return match.group(0)
        relative = posixpath.relpath(resolved.as_posix(), new_dir.as_posix())
        moved = relative + (marker + suffix if marker else "")
        return match.group("prefix") + moved + match.group("suffix")

    return _MARKDOWN_LINK.sub(relocate, rebuilt)
