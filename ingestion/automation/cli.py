from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .audit import audit_history
from .core import (
    SafetyStop, assert_git_safe, atomic_write_batch, compare_rows, git,
    git_status_paths, ingestion_lock, insert_articles, insert_rows, note_urls, json_text, last_updated, markdown_cell,
    parse_table, reconcile_remote, replace_last_updated, repo_fingerprint, report_path,
    workspace_snapshot, write_json,
)
from .fetchers import CDEBrowserUnavailable, fetch_cde, fetch_cpc, fetch_cpc_content_hash, fetch_shanghai
from .sources import FETCHERS, FILE_FETCHERS, fetch_cpc_notes, note_text

MAX_NEW_PER_SOURCE = 50

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).with_name("config.json")
APPROVAL_PATH = ROOT / "_" / "ingestion" / "approval.json"
APPROVAL_HOURS = 24


def approval_scope_hash(report: dict[str, Any]) -> str:
    scope = {
        "report": report.get("report"), "rough_created": report.get("rough_created"),
        "planned_writes": report.get("planned_writes"), "blocking": report.get("blocking"),
    }
    return hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def earliest_date(config: dict[str, Any]) -> str:
    """Items published before this day are never fetched: the knowledge base was initialised
    from the reviewed Word collection up to 2026-02-28, and that stays the authority."""
    return str(config.get("earliest_date") or "")


def effective_since(config: dict[str, Any], note_text: str) -> str:
    """The note's `last_updated`, but never earlier than the day before `earliest_date`
    (adapters take items with a date strictly after this)."""
    since = last_updated(note_text)
    floor = earliest_date(config)
    if floor:
        since = max(since, (datetime.strptime(floor, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"))
    return since


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def base_report(now: datetime, mode: str) -> dict[str, Any]:
    report = {
        "date": now.strftime("%Y-%m-%d"), "mode": mode,
        "generated_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "baseline": repo_fingerprint(ROOT, CONFIG_PATH),
        "report": {}, "rough_created": [], "planned_writes": [],
        "blocking": False, "alerts": [], "auto_write_paths": [], "rough_sources": {}, "rough_also_sources": {},
    }
    run_nonce = os.environ.get("DEK_INGEST_RUN_NONCE")
    if run_nonce:
        report["run_nonce"] = run_nonce
    return report


def row_source_url(row: Any) -> str:
    """The official page a row came from: its own `url`, or its article's."""
    return str(getattr(row, "url", "") or getattr(row, "article_url", "") or "")


def rough_content(source_path: str, rows: list[Any], day: str, source_url: str = "") -> str:
    source_link = source_path.removesuffix(".md")
    published_date = max((str(row.date)[:10] for row in rows), default="")
    source_item_key = hashlib.sha256(
        json.dumps([row.key for row in rows], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    rendered = "".join(
        f"| {markdown_cell(row.question)} | {markdown_cell(row.answer)} | {row.date[:10]} |\n"
        for row in rows
    )
    return (
        "---\n"
        f"date: {day}\n"
        f"published_date: {published_date}\n"
        f"ingested_at: {day}\n"
        f'source: "[[{source_link}]]"\n'
        + (f'source_url: "{source_url}"\n' if source_url else "")
        + "status: pending_review\n"
        f"source_item_key: sha256:{source_item_key}\n"
        "recommended_tags:\n"
        "wiki_target:\n"
        "reviewed_at:\n"
        "---\n\n"
        "## 新增问答\n\n"
        "| 问题 | 解答 | 发布日期 |\n"
        "| --- | --- | --- |\n"
        f"{rendered}"
    )


def find_staged_duplicate(writes: dict[Path, str], row: Any) -> Path | None:
    """A draft already planned in this run for the very same question and answer (the same
    item listed under two columns of one site), or None."""
    cells = f"| {markdown_cell(row.question)} | {markdown_cell(row.answer)} |"
    rough_dir = ROOT / "ingestion" / "rough"
    for path, text in writes.items():
        if path.parent == rough_dir and not path.exists() and cells in text:
            return path
    return None


def add_source_to_draft(text: str, source_path: str) -> str:
    """Name one more source note on a planned draft's `source:` line."""
    link = f"[[{source_path.removesuffix('.md')}]]"
    updated, count = re.subn(r'(?m)^(source: ")([^"\n]*)(")$', lambda m: f"{m.group(1)}{m.group(2)} {link}{m.group(3)}", text, count=1)
    if count != 1:
        raise SafetyStop("planned draft has no source line to extend")
    return updated


def stage_source_rows(
    result: dict[str, Any], writes: dict[Path, str], source: dict[str, Any],
    rows: list[Any], meta: dict[str, Any], now: datetime, *, revisions_block: bool, earliest: str = "",
) -> None:
    """Compare fetched rows with a source note; plan the note update and one
    rough draft per new row. Nothing is written here, only planned."""
    day = now.strftime("%Y-%m-%d")
    path = ROOT / source["path"]
    note = path.read_text(encoding="utf-8")
    if earliest:
        # Older rows belong to the initialised collection: they are neither added nor
        # compared (a changed old answer must not block today's new items).
        rows = [row for row in rows if row.date[:10] >= earliest]
    additions, revisions = compare_rows(parse_table(note), rows)
    additions = [row for row in additions if row.date[:10] > last_updated(note)]
    entry = {**meta, "new_count": len(additions), "revision_count": len(revisions)}
    result["report"][source["path"]] = entry
    if revisions:
        entry.update(status="failed", reason="检测到远端正文修订，禁止自动覆盖", revisions=revisions[:20])
        if revisions_block:
            result["blocking"] = True
        result["alerts"].append(f"remote revision detected: {source['path']}")
        return
    if additions and not source["auto_classified"]:
        entry.update(status="failed", reason="新增条目需要人工主题分类", candidates=[{"question": r.question, "date": r.date} for r in additions[:20]])
        result["blocking"] = True
        result["alerts"].append(f"manual classification required: {source['path']}")
        return
    if len(additions) > MAX_NEW_PER_SOURCE:
        entry.update(status="failed", reason=f"一次新增 {len(additions)} 条，超过 {MAX_NEW_PER_SOURCE} 条上限，疑似页面结构变化", candidates=[{"question": r.question, "date": r.date} for r in additions[:20]])
        result["alerts"].append(f"too many new rows, not ingested: {source['path']}")
        return
    entry["status"] = "updated_with_new" if additions else "no_change"
    if not additions:
        return
    insert = insert_articles if source.get("layout") == "articles" else insert_rows
    writes[path] = replace_last_updated(insert(note, additions), day)
    # One draft per question: the review page turns one rough into one wiki
    # page, so a batched table could never be approved as-is.
    prefix = f"{now:%Y%m%d}_{path.stem}_增量_"
    taken = [int(m.group(1)) for existing in (ROOT / "ingestion" / "rough").glob(prefix + "*.md")
             if (m := re.fullmatch(re.escape(prefix) + r"(\d+)\.md", existing.name))]
    auto = source.get("auto_classified") is True and source.get("auto_ingest") is True
    if auto:
        result["auto_write_paths"].append(source["path"])
    number = max(taken, default=0)
    for row in additions:
        duplicate = find_staged_duplicate(writes, row) if auto else None
        if duplicate is not None and result["rough_sources"].get(str(duplicate.relative_to(ROOT))) == source["path"]:
            duplicate = None          # twice in the same column: keep both, as before
        if duplicate is not None:
            # The same item under another column: one draft for the reviewer, naming both
            # sources, and the report says which draft covers this source too.
            duplicate_relative = str(duplicate.relative_to(ROOT))
            writes[duplicate] = add_source_to_draft(writes[duplicate], source["path"])
            also = result.setdefault("rough_also_sources", {}).setdefault(duplicate_relative, [])
            if source["path"] not in also:
                also.append(source["path"])
            continue
        number += 1
        rough_path = ROOT / "ingestion" / "rough" / f"{prefix}{number}.md"
        if rough_path.exists():
            raise SafetyStop(f"rough draft already exists: {rough_path.relative_to(ROOT)}")
        writes[rough_path] = rough_content(source["path"], [row], day, row_source_url(row))
        rough_relative = str(rough_path.relative_to(ROOT))
        result["rough_created"].append(rough_relative)
        result["rough_sources"][rough_relative] = source["path"]
        if auto:
            result["auto_write_paths"].append(rough_relative)


def stage_new_notes(
    result: dict[str, Any], writes: dict[Path, str], notes: list[Any], directory: str,
    parent_note: str, now: datetime,
) -> None:
    """Each new article becomes its own excerpt note under `directory` plus one
    pending draft. Existing files are never touched."""
    from .core import Row
    day = now.strftime("%Y-%m-%d")
    stem = Path(parent_note).stem
    for note in notes:
        relative = f"{directory}/{note.filename}"
        path = ROOT / relative
        if path.exists():
            raise SafetyStop(f"excerpt note already exists: {relative}")
        writes[path] = note_text(note, stem)
        result["report"][relative] = {"status": "updated_with_new", "new_count": 1, "latest_date": note.date}
        result["auto_write_paths"].append(relative)
        prefix = f"{now:%Y%m%d}_{path.stem}_增量_"
        # One draft per question, like table rows.
        for number, (question, answer) in enumerate(note.rows, start=1):
            rough_path = ROOT / "ingestion" / "rough" / f"{prefix}{number}.md"
            if rough_path.exists():
                raise SafetyStop(f"rough draft already exists: {rough_path.relative_to(ROOT)}")
            writes[rough_path] = rough_content(relative, [Row(question, answer, note.date)], day, note.source_url)
            rough_relative = str(rough_path.relative_to(ROOT))
            result["rough_created"].append(rough_relative)
            result["rough_sources"][rough_relative] = relative
            result["auto_write_paths"].append(rough_relative)


def stage_file_sources(config: dict[str, Any], result: dict[str, Any], writes: dict[Path, str], now: datetime) -> None:
    """Sources kept as one excerpt note per article in a folder. A failing
    source is reported and leaves every other source alone."""
    for source in config.get("file_sources", []):
        try:
            known = set()
            for folder in [source["dir"], *source.get("known_dirs", [])]:
                known |= {p.name for p in (ROOT / folder).glob("*.md")}
            since = effective_since(config, (ROOT / source["note"]).read_text(encoding="utf-8"))
            notes, meta = FILE_FETCHERS[source["fetcher"]](source, known, since)
            if len(notes) > MAX_NEW_PER_SOURCE:
                raise SafetyStop(f"一次新增 {len(notes)} 篇，超过 {MAX_NEW_PER_SOURCE} 篇上限，疑似页面结构变化")
            stage_new_notes(result, writes, notes, source["dir"], source["note"], now)
            result["report"][source["note"]] = {**meta, "new_count": len(notes), "status": "new_articles_staged" if notes else "no_change"}
        except Exception as exc:
            result["report"][source["note"]] = {"status": "failed", "reason": str(exc)}
            result["alerts"].append(f"source failure: {source['note']}: {exc}")


def stage_table_sources(config: dict[str, Any], result: dict[str, Any], writes: dict[Path, str], now: datetime) -> None:
    """Sources fetched over plain HTTP into a `| 问题 | 解答 | 发布日期 |` note. A
    failure of one source is reported and leaves every other source alone."""
    for source in config.get("table_sources", []):
        try:
            note = (ROOT / source["path"]).read_text(encoding="utf-8")
            known: set[Any] = {row.key for row in parse_table(note)}
            if source.get("layout") == "articles":
                known |= note_urls(note)
            rows, meta = FETCHERS[source["fetcher"]](source, known, effective_since(config, note))
            stage_source_rows(result, writes, source, rows, meta, now, revisions_block=False, earliest=earliest_date(config))
        except Exception as exc:
            result["report"][source["path"]] = {"status": "failed", "reason": str(exc)}
            result["alerts"].append(f"source failure: {source['path']}: {exc}")


def inspect(config: dict[str, Any], now: datetime) -> tuple[dict[str, Any], dict[Path, str]]:
    result = base_report(now, "dry-run")
    writes: dict[Path, str] = {}

    for path in config["no_fetch_rule"]:
        result["report"][path] = {"status": "skipped_no_fetch_rule", "reason": "抓取规则尚未固化"}
    for path in config["known_unautomated"]:
        result["report"][path] = {"status": "skipped_adapter_pending", "reason": "已固化来源尚未接入首批自动适配器"}

    stage_table_sources(config, result, writes, now)
    stage_file_sources(config, result, writes, now)

    try:
        articles, meta = fetch_cpc(config["cpc"]["list_url"])
        if earliest_date(config):
            articles = [article for article in articles if article.date >= earliest_date(config)]
        existing: dict[str, Path] = {}
        for key in ("included_dir", "excluded_dir"):
            for existing_path in (ROOT / config["cpc"][key]).glob("*.md"):
                if existing_path.name in existing:
                    raise SafetyStop(f"duplicate CPC article filename: {existing_path.name}")
                existing[existing_path.name] = existing_path
        additions = [article for article in articles if article.filename not in existing]
        revisions: list[str] = []
        unavailable: list[dict[str, str]] = []
        verified = 0
        for article in articles:
            local_path = existing.get(article.filename)
            if local_path is None:
                continue
            local_note = local_path.read_text(encoding="utf-8")
            match = re.search(r'(?m)^source_content_hash:\s*["\']?(sha256:[0-9a-f]{64})["\']?\s*$', local_note)
            if not match:
                unavailable.append({"filename": article.filename, "status": "skipped_revision_check_unavailable", "reason": "missing source_content_hash baseline"})
                continue
            try:
                remote_hash = fetch_cpc_content_hash(config["cpc"]["detail_url"], article)
            except Exception:
                unavailable.append({"filename": article.filename, "status": "skipped_revision_check_unavailable", "reason": "remote detail unavailable or cannot be normalized"})
                continue
            verified += 1
            if remote_hash != match.group(1):
                revisions.append(article.filename)
        path = ROOT / config["cpc"]["path"]
        entry = {**meta, "new_count": len(additions), "verified_existing_count": verified, "unverified_existing_count": len(unavailable), "revision_count": len(revisions)}
        if revisions:
            entry.update(status="failed", reason="检测到既有文章规范化内容哈希变化，禁止自动覆盖", revisions=revisions[:20])
            result["blocking"] = True
            result["alerts"].append(f"CPC remote revision detected: {config['cpc']['path']}")
        elif additions:
            notes, detail_meta = fetch_cpc_notes(config["cpc"], additions)
            stage_new_notes(result, writes, notes, config["cpc"]["included_dir"], config["cpc"]["path"], now)
            entry.update(status="new_articles_staged", staged_count=len(notes), **detail_meta)
        elif unavailable:
            entry.update(status="skipped_revision_check_unavailable", reason="既有文章缺少内容哈希基线或详情无法可靠规范化", unverified=unavailable)
        else:
            entry["status"] = "no_change"
        result["report"][config["cpc"]["path"]] = entry
    except Exception as exc:
        path = config["cpc"]["path"]
        result["report"][path] = {"status": "failed", "reason": str(exc)}
        result["blocking"] = True
        result["alerts"].append(f"source failure: {path}: {exc}")

    if not config["cde"].get("enabled", True):
        reason = config["cde"].get("disabled_reason", "CDE browser source is disabled")
        diagnostics = {"attempted": False, "myAjax_is_function": False}
        result["cde_diagnostics"] = diagnostics
        for source in config["cde"]["sources"]:
            result["report"][source["path"]] = {"status": "skipped_browser_unavailable", "reason": reason, "browser_diagnostics": diagnostics}
        result["alerts"].append(f"CDE Playwright unavailable: {reason}")
    else:
        try:
            cde_config = config["cde"]["sources"]
            remote, diagnostics = fetch_cde(config["cde"]["url"], [x["type"] for x in cde_config], ROOT / "_" / "browser" / "cde-profile")
            result["cde_diagnostics"] = diagnostics
            for source in cde_config:
                rows, meta = remote[source["type"]]
                stage_source_rows(result, writes, source, rows, meta, now, revisions_block=True, earliest=earliest_date(config))
        except CDEBrowserUnavailable as exc:
            diagnostics = exc.diagnostics
            result["cde_diagnostics"] = diagnostics
            for source in config["cde"]["sources"]:
                entry = {"status": "skipped_browser_unavailable", "reason": str(exc)}
                entry["browser_diagnostics"] = diagnostics
                result["report"][source["path"]] = entry
            result["alerts"].append(f"CDE Playwright unavailable: {exc}")
        except Exception as exc:
            for source in config["cde"]["sources"]:
                result["report"][source["path"]] = {"status": "failed", "reason": str(exc)}
            result["blocking"] = True
            result["alerts"].append(f"CDE source failure: {exc}")

    result["planned_writes"] = sorted(str(p.relative_to(ROOT)) for p, text in writes.items() if not p.exists() or text != p.read_text(encoding="utf-8"))
    validate_report_invariants(result)
    add_pipeline_health(result)
    return result, writes


def validate_report_invariants(report: dict[str, Any]) -> None:
    updated = {
        path for path, entry in report.get("report", {}).items()
        if isinstance(entry, dict) and entry.get("status") == "updated_with_new"
    }
    rough_created = set(report.get("rough_created", []) or [])
    rough_sources = report.get("rough_sources", {}) or {}
    covered = {source for rough, source in rough_sources.items() if rough in rough_created}
    for rough, sources in (report.get("rough_also_sources", {}) or {}).items():
        if rough in rough_created and rough in rough_sources:
            covered.update(sources)
    missing = updated - covered
    if missing:
        raise SafetyStop(f"updated_with_new requires rough draft: {sorted(missing)}")


def add_pipeline_health(report: dict[str, Any]) -> None:
    audit = audit_history(ROOT)
    health = {
        key: audit[key] for key in (
            "missing_rough_events", "rough_total", "rough_statuses",
            "stale_pending_over_7_days", "rough_lifecycle_errors", "report_errors",
        )
    }
    report["pipeline_health"] = health
    if health["missing_rough_events"] or health["stale_pending_over_7_days"] or health["rough_lifecycle_errors"] or health["report_errors"]:
        report["alerts"].append(
            "pipeline backlog: "
            f"missing_rough={health['missing_rough_events']} "
            f"stale_pending={len(health['stale_pending_over_7_days'])} "
            f"lifecycle_errors={len(health['rough_lifecycle_errors'])} "
            f"report_errors={len(health['report_errors'])}"
        )
    if health["rough_lifecycle_errors"] or health["report_errors"]:
        report["blocking"] = True


def automatic_write_dirs(config: dict[str, Any]) -> set[str]:
    """Folders where a run may create new excerpt notes."""
    folders = [config["cpc"]["included_dir"]] if "cpc" in config else []
    folders += [source["dir"] for source in config.get("file_sources", [])]
    return {folder.rstrip("/") + "/" for folder in folders}


def automatic_write_allowlist(config: dict[str, Any]) -> set[str]:
    return {
        source["path"] for source in [*config["cde"]["sources"], *config.get("table_sources", [])]
        if source.get("auto_classified") is True and source.get("auto_ingest") is True
    }


def validate_automatic_plan(config: dict[str, Any], report: dict[str, Any], writes: dict[Path, str]) -> set[str]:
    planned = set(report.get("planned_writes", []))
    approved = set(report.get("auto_write_paths", []))
    actual = {str(path.relative_to(ROOT)) for path, content in writes.items() if not path.exists() or path.read_text(encoding="utf-8") != content}
    if planned != actual or planned != approved:
        raise SafetyStop("scheduled plan contains a write without an explicit automatic decision")
    source_allowlist = automatic_write_allowlist(config)
    source_paths = {path for path in planned if path.startswith("source/")}
    rough_paths = {path for path in planned if path.startswith("ingestion/rough/")}
    source_dirs = automatic_write_dirs(config)
    outside = {path for path in source_paths - source_allowlist if not any(path.startswith(d) and path.endswith(".md") for d in source_dirs)}
    if outside or planned != source_paths | rough_paths:
        raise SafetyStop("scheduled plan contains a non-allowlisted path")
    rough_sources = report.get("rough_sources", {})
    also_sources = report.get("rough_also_sources", {})
    paired = {rough_sources.get(path) for path in rough_paths}
    for path in rough_paths:
        paired.update(also_sources.get(path, []))
    if (rough_paths != set(report.get("rough_created", []))
            or paired != source_paths):
        raise SafetyStop("scheduled source and rough writes are not paired")
    if any(report.get("report", {}).get(path, {}).get("status") != "updated_with_new" for path in source_paths):
        raise SafetyStop("scheduled source is not explicitly marked updated_with_new")
    return planned


def scheduled_report_path(now: datetime) -> Path:
    return ROOT / "_" / "ingestion" / f"scheduled-run-{now:%Y%m%d_%H%M}.json"


def execute_scheduled(*, no_publication: bool = False) -> int:
    if not no_publication:
        reconcile_remote(ROOT)
    assert_git_safe(ROOT)
    start_head = git(ROOT, "rev-parse", "HEAD")
    now = datetime.now().astimezone()
    config = load_config()
    report, writes = inspect(config, now)
    report["mode"] = "scheduled-run"
    diagnostic_path = scheduled_report_path(now)
    write_json(diagnostic_path, report)
    for alert in report["alerts"]:
        print(f"ALERT: {alert}", file=sys.stderr)
    if report["blocking"]:
        raise SafetyStop(f"blocking findings; no writes performed; report={diagnostic_path.relative_to(ROOT)}")
    planned = validate_automatic_plan(config, report, writes)
    if not planned:
        print(f"no substantive changes; no files, commit, or push; report={diagnostic_path.relative_to(ROOT)}")
        return 0

    originals = {path: (path.read_bytes() if path.exists() else None) for path in writes}
    assert_git_safe(ROOT)
    if git(ROOT, "rev-parse", "HEAD") != start_head:
        raise SafetyStop("HEAD changed before scheduled write")
    for path, original in originals.items():
        current = path.read_bytes() if path.exists() else None
        if current != original:
            raise SafetyStop(f"target changed before scheduled write: {path.relative_to(ROOT)}")

    log_path = report_path(ROOT, False, now)
    if log_path.exists():
        raise SafetyStop(f"ingestion report already exists: {log_path.relative_to(ROOT)}")
    writes[log_path] = json_text(report)
    allowed = planned | {str(log_path.relative_to(ROOT))}
    committed = False
    try:
        with atomic_write_batch(ROOT, writes):
            changed = git_status_paths(ROOT)
            if changed != allowed:
                raise SafetyStop(f"unexpected changed paths during scheduled write: expected={sorted(allowed)} actual={sorted(changed)}")
            if no_publication:
                print(f"candidate results written locally; publication disabled; report={diagnostic_path.relative_to(ROOT)}")
                return 0
            git(ROOT, "fetch", "origin", "--prune")
            if git(ROOT, "rev-parse", "origin/main") != start_head:
                raise SafetyStop("origin/main changed before scheduled commit")
            git(ROOT, "add", "--", *sorted(allowed))
            git(ROOT, "commit", "-m", f"ingestion: 来源增量摄入 {now:%Y-%m-%d}")
            committed = True
    except Exception:
        if not committed:
            try:
                git(ROOT, "restore", "--staged", "--", *sorted(allowed))
            except SafetyStop:
                pass
        raise
    git(ROOT, "fetch", "origin", "--prune")
    if git(ROOT, "rev-parse", "origin/main") != start_head:
        raise SafetyStop("origin/main changed after scheduled commit; local commit retained; push refused")
    git(ROOT, "push", "origin", "main")
    print(f"completed and pushed: {log_path.relative_to(ROOT)}")
    return 0


def approve(report_file: Path) -> int:
    report_file = report_file.resolve()
    report_root = (ROOT / "_" / "ingestion").resolve()
    if report_root not in report_file.parents:
        raise SafetyStop("approval report must be under _/ingestion")
    raw = report_file.read_bytes()
    report = json.loads(raw)
    if report.get("mode") != "dry-run" or report.get("blocking"):
        raise SafetyStop("only a non-blocking dry-run report can be approved")
    if report.get("baseline") != repo_fingerprint(ROOT, CONFIG_PATH):
        raise SafetyStop("dry-run baseline no longer matches repository/configuration")
    generated = datetime.fromisoformat(report["generated_at"])
    now = datetime.now(timezone.utc)
    if generated.tzinfo is None or not timedelta(0) <= now - generated.astimezone(timezone.utc) <= timedelta(hours=APPROVAL_HOURS):
        raise SafetyStop("dry-run report is expired or has an invalid timestamp")
    assert_git_safe(ROOT)
    write_json(APPROVAL_PATH, {
        "baseline": report["baseline"], "report_path": str(report_file.relative_to(ROOT)),
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "approval_scope_hash": approval_scope_hash(report),
        "workspace_snapshot": workspace_snapshot(ROOT),
        "approved_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(hours=APPROVAL_HOURS)).isoformat(timespec="seconds"),
    })
    print(f"approval recorded: {APPROVAL_PATH.relative_to(ROOT)}")
    return 0


def execute(real: bool, commissioning: bool = False) -> int:
    allowed_dirty = {"ingestion/automation", "deploy", "requirements-ingestion.txt"} if commissioning and not real else None
    assert_git_safe(ROOT, allowed_dirty)
    now = datetime.now().astimezone()
    report, writes = inspect(load_config(), now)
    if real:
        for alert in report["alerts"]:
            print(f"ALERT: {alert}", file=sys.stderr)
    if not real:
        path = report_path(ROOT, True, now)
        write_json(path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"dry-run report: {path.relative_to(ROOT)}")
        return 2 if report["blocking"] else 0
    if report["blocking"]:
        path = report_path(ROOT, True, now)
        write_json(path, report)
        raise SafetyStop(f"blocking findings; no writes performed; report={path.relative_to(ROOT)}")
    if not writes:
        print("no substantive changes; no files, commit, or push")
        return 0
    approval = json.loads(APPROVAL_PATH.read_text(encoding="utf-8")) if APPROVAL_PATH.exists() else {}
    if approval.get("baseline") != report["baseline"]:
        raise SafetyStop("no valid dry-run approval for current baseline")
    expires = datetime.fromisoformat(approval.get("expires_at", "1970-01-01T00:00:00+00:00"))
    if datetime.now(timezone.utc) >= expires.astimezone(timezone.utc):
        raise SafetyStop("dry-run approval has expired")
    approved_report = ROOT / approval.get("report_path", "")
    if not approved_report.is_file() or hashlib.sha256(approved_report.read_bytes()).hexdigest() != approval.get("report_sha256"):
        raise SafetyStop("approved dry-run report is missing or its hash changed")
    if workspace_snapshot(ROOT) != approval.get("workspace_snapshot"):
        raise SafetyStop("workspace state differs from approved dry-run")
    if approval_scope_hash(report) != approval.get("approval_scope_hash"):
        raise SafetyStop("current ingestion result differs from the approved dry-run")
    report["mode"] = "run"
    log_path = report_path(ROOT, False, now)
    if log_path.exists():
        raise SafetyStop(f"ingestion report already exists: {log_path.relative_to(ROOT)}")
    writes[log_path] = json_text(report)
    allowed = set(str(path.relative_to(ROOT)) for path in writes)
    with atomic_write_batch(ROOT, writes):
        changed_paths = git_status_paths(ROOT)
        if changed_paths != allowed:
            raise SafetyStop(f"unexpected changed paths; refusing write: expected={sorted(allowed)} actual={sorted(changed_paths)}")
    git(ROOT, "add", "--", *sorted(allowed))
    git(ROOT, "commit", "-m", f"ingestion: 来源增量摄入 {now:%Y-%m-%d}")
    git(ROOT, "push", "origin", "main")
    print(f"completed and pushed: {log_path.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dek-ingest")
    sub = parser.add_subparsers(dest="command")
    dry = sub.add_parser("dry-run")
    dry.add_argument("--commissioning", action="store_true", help="allow only the new implementation files to be uncommitted")
    sub.add_parser("run")
    scheduled = sub.add_parser("scheduled-run")
    scheduled.add_argument("--no-publication", action="store_true")
    approval = sub.add_parser("approve")
    approval.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with ingestion_lock(ROOT):
            if args.command == "approve":
                return approve(args.report)
            if args.command == "scheduled-run":
                return execute_scheduled(no_publication=args.no_publication)
            return execute(real=args.command == "run", commissioning=getattr(args, "commissioning", False))
    except SafetyStop as exc:
        print(f"SAFETY STOP: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
