"""Remove everything dated on/after a day from the repository working tree: wiki entries, drafts whose
rows are all that late, per-article source notes, table rows in the automatic source notes, and the
`last_updated` of those notes (set back so the next pull refetches). Dry run unless --apply.

    python3 deploy/tools/reset_from_date.py [--cut 2026-03-01] [--apply]

Then (see HANDOVER_OPERATIONS.md, section 9): run the audit (rc 0), run_gates.sh, commit, push, and
`systemctl start dek-review-publish-manual.service` for the deletion release. Ask the user first."""
import re, glob, sys, json
from datetime import date, timedelta
from pathlib import Path
root = Path(__file__).resolve().parents[2]
CUT = "2026-03-01"
if "--cut" in sys.argv:
    CUT = sys.argv[sys.argv.index("--cut") + 1]
NEW_LAST = (date.fromisoformat(CUT) - timedelta(days=1)).isoformat()
apply = "--apply" in sys.argv
def fm(t):
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n?", t, re.S); d = {}
    for line in (m.group(1).splitlines() if m else []):
        k = re.match(r"^([A-Za-z_]+):[ \t]*(.*)$", line)
        if k: d[k.group(1)] = k.group(2).strip().strip('"')
    return d
def day(v): m = re.search(r"\d{4}-\d{2}-\d{2}", v or ""); return m.group(0) if m else ""
def is_row(l): return l.startswith("|") and not re.match(r"^\|[\s:|-]+\|?\s*$", l)
def cells(l): return [c.strip() for c in re.split(r"(?<!\\)\|", l)[1:-1]]
def row_date(l):
    c = cells(l); return day(c[-1]) if c and c[0] not in ("问题", "标题") else ""
delete = []; rewrite = {}
for f in glob.glob(str(root / "wiki/**/*.md"), recursive=True):
    d = fm(Path(f).read_text(encoding="utf-8-sig"))
    if "question" in d and day(d.get("date")) >= CUT: delete.append(f)
for f in glob.glob(str(root / "ingestion/rough/*.md")):
    ds = [row_date(l) for l in Path(f).read_text(encoding="utf-8").splitlines() if is_row(l) and row_date(l)]
    if ds and all(x >= CUT for x in ds): delete.append(f)
cfg = json.load(open(root / "ingestion/automation/config.json"))
auto = {s["path"] for s in cfg["table_sources"]} | {s["note"] for s in cfg.get("file_sources", [])} | {s["path"] for s in cfg["cde"]["sources"]} | {cfg["cpc"]["path"]}
rows_removed = {}
for f in glob.glob(str(root / "source/**/*.md"), recursive=True):
    rel = str(Path(f).relative_to(root))
    if "SourceFetchMethod" in f or "摘录复核" in f: continue
    text = Path(f).read_text(encoding="utf-8-sig"); d = fm(text)
    if rel not in auto and "article_title" in d and day(d.get("date")) >= CUT:
        delete.append(f); continue
    if rel in auto:
        lines = text.split("\n"); kept = [l for l in lines if not (is_row(l) and row_date(l) >= CUT)]
        new = "\n".join(kept)
        if day(d.get("last_updated")) > NEW_LAST:
            new = re.sub(r"(?m)^last_updated:.*$", f"last_updated: {NEW_LAST}", new, count=1)
        if new != text:
            rewrite[f] = new; rows_removed[rel] = len(lines) - len(kept)
print("delete:", len(delete), "| wiki", sum("/wiki/" in f for f in delete), "drafts", sum("/rough/" in f for f in delete), "source notes", sum("/source/" in f for f in delete))
print("rewrite:", len(rewrite)); [print("   ", Path(f).name[:58], rows_removed[str(Path(f).relative_to(root))], "rows") for f in rewrite]
if apply:
    for f in delete: Path(f).unlink()
    for f, new in rewrite.items():
        raw = Path(f).read_bytes(); bom = raw.startswith(b"\xef\xbb\xbf")
        Path(f).write_bytes((b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8"))
    print("applied")
