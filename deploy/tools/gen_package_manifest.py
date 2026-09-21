"""Regenerate deploy/PACKAGE.sha256 (run from the repository root).

The manifest lists every file the installer copies: deploy/, web/, qa/, ingestion/automation/
plus ingestion/__init__.py. A file missing from it is silently NOT installed (this once broke the
ingest service at start-up); tests/test_package_manifest.py guards that. Keep fixtures LF: the
manifest is checked against `git archive`, which normalises line endings.
"""
import hashlib
from pathlib import Path

lines = [("ingestion/__init__.py", hashlib.sha256(Path("ingestion/__init__.py").read_bytes()).hexdigest())]
for top in ("deploy", "web", "qa", "ingestion/automation"):
    for path in sorted(Path(top).rglob("*")):
        if path.is_dir():
            continue
        rel = path.as_posix()
        if rel == "deploy/PACKAGE.sha256":
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        lines.append((rel, hashlib.sha256(path.read_bytes()).hexdigest()))
lines.sort(key=lambda item: item[0])
content = "".join(f"{digest}  {rel}\n" for rel, digest in lines)
target = Path("deploy/PACKAGE.sha256")
target.write_text(content, encoding="utf-8")
print(f"wrote {len(lines)} entries to {target}")
