from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

AUDIT_EXCLUSIONS_RELPATH = "ingestion/automation/audit_exclusions.json"


def _updated_sources(report: Any) -> list[dict[str, str]]:
    """Return updated_with_new events as {source, source_item_key?} dicts.

    Accepts both the current dict-shaped report and legacy list-shaped
    reports. ``source_item_key`` is carried through only when present, so
    callers can prefer it over the legacy (date, source) identity.
    """
    events: list[dict[str, str]] = []
    if isinstance(report, dict):
        entries = report.items()
    elif isinstance(report, list):
        entries = ((str(index), value) for index, value in enumerate(report))
    else:
        return events
    for key, value in entries:
        if not isinstance(value, dict) or value.get("status") != "updated_with_new":
            continue
        source = value.get("source") or value.get("source_note") or key
        event = {"source": str(source)}
        item_key = value.get("source_item_key")
        if isinstance(item_key, str) and item_key:
            event["source_item_key"] = item_key
        events.append(event)
    return events


def _rough_status(path: Path) -> str:
    if not path.is_file():
        return "missing"
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    for line in text.splitlines()[:30]:
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip() or "unknown"
    return "legacy_untracked"


def _rough_fields(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in text.splitlines()[1:80]:
        if line == "---":
            break
        match = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if match:
            key = match.group(1)
            current_key = key
            fields[key] = match.group(2).strip().strip("\"'")
        elif current_key and re.match(r"^\s+-\s+\S", line):
            fields[current_key] = fields[current_key] or "<list>"
    return fields


def _rough_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _rough_identities(path: Path) -> list[tuple[str, str]]:
    """Legacy identities: (ingested_at, source_path), one per source note the draft names.
    A draft for an item listed under two columns names both notes on its `source:` line."""
    text = _rough_text(path)
    ingested = re.search(r"(?m)^ingested_at:\s*(\d{4}-\d{2}-\d{2})\s*$", text)
    line = re.search(r'(?m)^source:[ \t]*(.*)$', text)
    if not ingested or not line:
        return []
    identities = []
    for name in re.findall(r"\[\[([^]]+)\]\]", line.group(1)):
        identities.append((ingested.group(1), name if name.endswith(".md") else name + ".md"))
    return identities


def _rough_identity(path: Path) -> tuple[str, str] | None:
    """The first identity of a draft, or None."""
    identities = _rough_identities(path)
    return identities[0] if identities else None


def _rough_source_item_key(path: Path) -> str | None:
    text = _rough_text(path)
    match = re.search(r"(?m)^source_item_key:\s*(\S+)\s*$", text)
    return match.group(1) if match else None


def _payload_errors(payload: Any, relative: str) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(payload, dict):
        return [{"report": relative, "reason": "payload is not a JSON object"}]
    report_date = payload.get("date")
    if not isinstance(report_date, str):
        errors.append({"report": relative, "reason": f"invalid 'date': {report_date!r}"})
    else:
        try:
            date.fromisoformat(report_date)
        except ValueError:
            errors.append({"report": relative, "reason": f"invalid 'date': {report_date!r}"})
    rough = payload.get("rough_created")
    if rough is not None and (not isinstance(rough, list) or any(not isinstance(item, str) for item in rough)):
        errors.append({"report": relative, "reason": "'rough_created' must be a list of strings"})
    rough_sources = payload.get("rough_sources")
    if rough_sources is not None and (
        not isinstance(rough_sources, dict)
        or any(not isinstance(k, str) or not isinstance(v, str) for k, v in rough_sources.items())
    ):
        errors.append({"report": relative, "reason": "'rough_sources' must be a string-keyed mapping"})
    also = payload.get("rough_also_sources")
    if also is not None and (
        not isinstance(also, dict)
        or any(not isinstance(k, str) or not isinstance(v, list) or any(not isinstance(i, str) for i in v) for k, v in also.items())
    ):
        errors.append({"report": relative, "reason": "'rough_also_sources' must map a draft to a list of source paths"})
    return errors


def _load_exclusions(root: Path) -> tuple[dict[tuple[str, str], str], list[dict[str, str]]]:
    path = root / AUDIT_EXCLUSIONS_RELPATH
    if not path.is_file():
        return {}, []
    relative = str(path.relative_to(root))
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {}, [{"report": relative, "reason": f"unreadable exclusions: {exc}"}]
    errors: list[dict[str, str]] = []
    exclusions: dict[tuple[str, str], str] = {}
    items = payload.get("exclusions") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {}, [{"report": relative, "reason": "'exclusions' must be a list"}]
    for item in items:
        if not isinstance(item, dict):
            errors.append({"report": relative, "reason": "exclusion entry is not an object"})
            continue
        date_value = item.get("date")
        source = item.get("source")
        reason = item.get("reason")
        if not isinstance(date_value, str) or not isinstance(source, str) or not source:
            errors.append({"report": relative, "reason": "exclusion entry requires string 'date' and non-empty 'source'"})
            continue
        if not isinstance(reason, str) or not reason.strip():
            errors.append({"report": relative, "reason": f"exclusion for {source} {date_value} requires a non-empty 'reason'"})
            continue
        exclusions[(date_value, source)] = reason.strip()
    return exclusions, errors


def audit_history(root: Path, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    rough_files = sorted((root / "ingestion" / "rough").glob("*.md"))
    reconciled_keys: set[str] = set()
    reconciled_legacy: set[tuple[str, str]] = set()
    for path in rough_files:
        item_key = _rough_source_item_key(path)
        if item_key:
            reconciled_keys.add(item_key)
        reconciled_legacy.update(_rough_identities(path))

    exclusions, exclusion_errors = _load_exclusions(root)
    errors: list[dict[str, str]] = list(exclusion_errors)
    excluded_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    backlog_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    updated_events = 0

    report_paths = sorted((root / "ingestion" / "logs").glob("source_ingest_*_report.json"))
    for path in report_paths:
        relative = str(path.relative_to(root))
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            errors.append({"report": relative, "reason": f"unreadable JSON: {exc}"})
            continue
        payload_errors = _payload_errors(payload, relative)
        if payload_errors:
            errors.extend(payload_errors)
            continue

        report_date = str(payload["date"])
        events = _updated_sources(payload.get("report"))
        updated_events += len(events)
        if not events:
            continue
        rough = [str(item) for item in payload.get("rough_created") or []]
        rough_sources = payload.get("rough_sources") if isinstance(payload.get("rough_sources"), dict) else None

        if rough_sources is not None:
            covered_sources = {source for rough_path, source in rough_sources.items() if rough_path in rough}
            also_sources = payload.get("rough_also_sources") if isinstance(payload.get("rough_also_sources"), dict) else {}
            for rough_path, sources in also_sources.items():
                # Counted only when the draft really names that source on its `source:` line.
                if rough_path not in rough or rough_path not in rough_sources or not (root / rough_path).is_file():
                    continue
                named = {name for _, name in _rough_identities(root / rough_path)}
                covered_sources.update(source for source in sources if source in named)
        else:
            # Legacy reports predate ``rough_sources``: a non-empty
            # ``rough_created`` covered the whole report at the time.
            covered_sources = {event["source"] for event in events} if rough else set()

        for event in events:
            exclusion_key = (report_date, event["source"])
            if exclusion_key in exclusions:
                item = excluded_by_key.setdefault(exclusion_key, {
                    "date": report_date,
                    "source": event["source"],
                    "reason": exclusions[exclusion_key],
                    "reports": [],
                })
                if relative not in item["reports"]:
                    item["reports"].append(relative)
                continue
            item_key = event.get("source_item_key")
            if item_key is not None:
                reconciled = item_key in reconciled_keys or event["source"] in covered_sources
                backlog_key: tuple[Any, ...] = ("key", item_key)
            else:
                reconciled = (report_date, event["source"]) in reconciled_legacy or event["source"] in covered_sources
                backlog_key = ("legacy", report_date, event["source"])
            if reconciled:
                continue
            item = backlog_by_key.setdefault(backlog_key, {
                "date": report_date,
                "source": event["source"],
                "reason": "updated_with_new without a matching rough draft",
                "reports": [],
            })
            if item_key is not None:
                item["source_item_key"] = item_key
            if relative not in item["reports"]:
                item["reports"].append(relative)

    rough_statuses: dict[str, int] = {}
    stale_pending: list[str] = []
    lifecycle_errors: list[dict[str, str]] = []
    for path in rough_files:
        status = _rough_status(path)
        fields = _rough_fields(path)
        rough_statuses[status] = rough_statuses.get(status, 0) + 1
        if status == "pending_review":
            identity = _rough_identity(path)
            stamp = date.fromisoformat(identity[0]) if identity else datetime.fromtimestamp(path.stat().st_mtime).date()
            if (today - stamp).days > 7:
                stale_pending.append(str(path.relative_to(root)))
        if status == "promoted" and not fields.get("wiki_target"):
            lifecycle_errors.append({
                "rough": str(path.relative_to(root)),
                "reason": "promoted rough requires wiki_target",
            })
        if status == "rejected" and not fields.get("rejection_reason"):
            lifecycle_errors.append({
                "rough": str(path.relative_to(root)),
                "reason": "rejected rough requires rejection_reason",
            })
    backlog = sorted(backlog_by_key.values(), key=lambda item: (item["date"], item["source"]))
    excluded_events = sorted(excluded_by_key.values(), key=lambda item: (item["date"], item["source"]))
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "updated_events": updated_events,
        "missing_rough_events": len(backlog),
        "backlog": backlog,
        "excluded_total": len(excluded_events),
        "excluded_events": excluded_events,
        "rough_total": len(rough_files),
        "rough_statuses": rough_statuses,
        "stale_pending_over_7_days": stale_pending,
        "rough_lifecycle_errors": lifecycle_errors,
        "report_errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dek-ingestion-audit")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = audit_history(args.root)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 2 if result["missing_rough_events"] or result["rough_lifecycle_errors"] or result["report_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
