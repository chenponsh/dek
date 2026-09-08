from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core import (
    SafetyStop, assert_git_safe, atomic_write_batch, compare_rows, git,
    git_status_paths, ingestion_lock, insert_rows, json_text, last_updated, markdown_cell,
    parse_table, replace_last_updated, repo_fingerprint, report_path,
    workspace_snapshot, write_json,
)
from .fetchers import CDEBrowserUnavailable, fetch_cde, fetch_cpc, fetch_cpc_content_hash, fetch_shanghai

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


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def base_report(now: datetime, mode: str) -> dict[str, Any]:
    return {
        "date": now.strftime("%Y-%m-%d"), "mode": mode,
        "generated_at": now.astimezone().isoformat(timespec="seconds"),
        "baseline": repo_fingerprint(ROOT, CONFIG_PATH),
        "report": {}, "rough_created": [], "planned_writes": [],
        "blocking": False, "alerts": [], "auto_write_paths": [],
    }


def rough_content(source_path: str, rows: list[Any], day: str) -> str:
    source_link = source_path.removesuffix(".md")
    rendered = "".join(
        f"| {markdown_cell(row.question)} | {markdown_cell(row.answer)} | {row.date[:10]} |\n"
        for row in rows
    )
    return (
        "---\n"
        f"date: {day}\n"
        f'source: "[[{source_link}]]"\n'
        "status: rough\n"
        "---\n\n"
        "## 新增问答\n\n"
        "| 问题 | 解答 | 发布日期 |\n"
        "| --- | --- | --- |\n"
        f"{rendered}"
    )


def inspect(config: dict[str, Any], now: datetime) -> tuple[dict[str, Any], dict[Path, str]]:
    result = base_report(now, "dry-run")
    writes: dict[Path, str] = {}
    day = now.strftime("%Y-%m-%d")

    for path in config["no_fetch_rule"]:
        result["report"][path] = {"status": "skipped_no_fetch_rule", "reason": "抓取规则尚未固化"}
    for path in config["known_unautomated"]:
        result["report"][path] = {"status": "skipped_adapter_pending", "reason": "已固化来源尚未接入首批自动适配器"}

    try:
        rows, meta = fetch_shanghai(config["shanghai"]["url"])
        path = ROOT / config["shanghai"]["path"]
        note = path.read_text(encoding="utf-8")
        additions, revisions = compare_rows(parse_table(note), rows)
        additions = [row for row in additions if row.date[:10] > last_updated(note)]
        entry = {**meta, "new_count": len(additions), "revision_count": len(revisions)}
        if revisions:
            entry.update(status="failed", reason="检测到远端正文修订，禁止自动覆盖", revisions=revisions[:20])
            result["blocking"] = True
            result["alerts"].append(f"remote revision detected: {config['shanghai']['path']}")
        elif additions:
            entry.update(status="failed", reason="新增条目需要人工主题分类", candidates=[{"question": r.question, "date": r.date} for r in additions[:20]])
            result["blocking"] = True
            result["alerts"].append(f"manual classification required: {config['shanghai']['path']}")
        else:
            entry["status"] = "no_change"
        result["report"][config["shanghai"]["path"]] = entry
    except Exception as exc:
        path = config["shanghai"]["path"]
        result["report"][path] = {"status": "failed", "reason": str(exc)}
        result["blocking"] = True
        result["alerts"].append(f"source failure: {path}: {exc}")

    try:
        articles, meta = fetch_cpc(config["cpc"]["list_url"])
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
            entry.update(status="candidate_new", reason="新增文章需要人工主题分类和正文核对", candidates=[article.filename for article in additions[:20]])
            result["blocking"] = True
            result["alerts"].append(f"manual classification required: {config['cpc']['path']}")
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
                path = ROOT / source["path"]
                note = path.read_text(encoding="utf-8")
                rows, meta = remote[source["type"]]
                additions, revisions = compare_rows(parse_table(note), rows)
                additions = [row for row in additions if row.date[:10] > last_updated(note)]
                entry = {**meta, "new_count": len(additions), "revision_count": len(revisions)}
                if revisions:
                    entry.update(status="failed", reason="检测到远端正文修订，禁止自动覆盖", revisions=revisions[:20])
                    result["blocking"] = True
                    result["alerts"].append(f"remote revision detected: {source['path']}")
                elif additions and not source["auto_classified"]:
                    entry.update(status="failed", reason="新增条目需要人工主题分类", candidates=[{"question": r.question, "date": r.date} for r in additions[:20]])
                    result["blocking"] = True
                    result["alerts"].append(f"manual classification required: {source['path']}")
                else:
                    entry["status"] = "updated_with_new" if additions else "no_change"
                    if additions:
                        writes[path] = replace_last_updated(insert_rows(note, additions), day)
                        rough_path = ROOT / "ingestion" / "rough" / f"{now:%Y%m%d}_{path.stem}_增量.md"
                        if rough_path.exists():
                            raise SafetyStop(f"rough draft already exists: {rough_path.relative_to(ROOT)}")
                        writes[rough_path] = rough_content(source["path"], additions, day)
                        result["rough_created"].append(str(rough_path.relative_to(ROOT)))
                        if source.get("auto_classified") is True and source.get("auto_ingest") is True:
                            result["auto_write_paths"].extend((source["path"], str(rough_path.relative_to(ROOT))))
                result["report"][source["path"]] = entry
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
    return result, writes


def automatic_write_allowlist(config: dict[str, Any]) -> set[str]:
    return {
        source["path"] for source in config["cde"]["sources"]
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
    if source_paths - source_allowlist or planned != source_paths | rough_paths:
        raise SafetyStop("scheduled plan contains a non-allowlisted path")
    if rough_paths != set(report.get("rough_created", [])) or len(source_paths) != len(rough_paths):
        raise SafetyStop("scheduled source and rough writes are not paired")
    if any(report.get("report", {}).get(path, {}).get("status") != "updated_with_new" for path in source_paths):
        raise SafetyStop("scheduled source is not explicitly marked updated_with_new")
    return planned


def scheduled_report_path(now: datetime) -> Path:
    return ROOT / "_" / "ingestion" / f"scheduled-run-{now:%Y%m%d_%H%M}.json"


def execute_scheduled() -> int:
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
    sub.add_parser("scheduled-run")
    approval = sub.add_parser("approve")
    approval.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with ingestion_lock(ROOT):
            if args.command == "approve":
                return approve(args.report)
            if args.command == "scheduled-run":
                return execute_scheduled()
            return execute(real=args.command == "run", commissioning=getattr(args, "commissioning", False))
    except SafetyStop as exc:
        print(f"SAFETY STOP: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
