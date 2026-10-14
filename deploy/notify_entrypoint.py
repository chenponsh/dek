#!/usr/bin/python3
"""Send a DingTalk work notification when there's something reviewers should
look at: new pending review items, or an approved item that has sat too long
without actually publishing (a proxy for "the publish pipeline is stuck or
failing" -- built from data this identity already has access to, rather than
a separate read grant into the publisher's own state).

Runs as the dek-review identity, reusing its existing DingTalk credentials,
decision queue and review bundle read access -- no new credential surface
for a notification-only, read-mostly job."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve(strict=True).parents[1]))
from web.dingtalk_gateway import DingTalkClient
from web.review import IsolatedReviewClone, MemoryFormNonceStore, ReviewService, _item_title

REVIEW_ORIGIN = "https://regkb.chenponai.com"
MAX_TITLE_CHARS = 60
DEFAULT_STUCK_THRESHOLD_SECONDS = 30 * 60
BEIJING = timezone(timedelta(hours=8))


def reviewer_ids() -> list[str]:
    return [value.strip() for value in os.environ.get("DEK_REVIEWER_IDS", "").split(",") if value.strip()]


def failure_reviewer_ids() -> list[str]:
    return [value.strip() for value in os.environ.get("DEK_FAILURE_REVIEWER_IDS", "").split(",") if value.strip()]


def dingtalk_client() -> DingTalkClient:
    return DingTalkClient(
        os.environ["DEK_REVIEW_DINGTALK_CLIENT_ID"],
        os.environ["DEK_REVIEW_DINGTALK_CLIENT_SECRET"],
        os.environ["DEK_REVIEW_DINGTALK_AGENT_ID"],
    )


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name("." + path.name + ".tmp")
    staging.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(staging, path)


def notification_title(item) -> str:
    """How an item is named in a chat message: its question (the file name only when the
    draft has none), on one line and cut at MAX_TITLE_CHARS so a long question stays readable."""
    text = " ".join(_item_title(item).split())
    return text if len(text) <= MAX_TITLE_CHARS else text[:MAX_TITLE_CHARS].rstrip() + "…"


def _read_seen(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def _new_keyed_messages(current: dict[str, str], state_path: Path, *, baseline_first_run: bool = False) -> list[str]:
    existed = state_path.exists()
    seen = _read_seen(state_path)
    _atomic_write_json(state_path, sorted(current))
    if baseline_first_run and not existed:
        return []
    return [current[key] for key in sorted(current) if key not in seen]


def _clock(value: float) -> str:
    return datetime.fromtimestamp(value, BEIJING).strftime("%m-%d %H:%M")


def new_publish_success_messages(service: ReviewService, state_path: Path) -> list[str]:
    status = service.publish_status()
    current: dict[str, str] = {}
    if status["completed"]:
        elapsed = status["duration_seconds"]
        elapsed_text = f"{elapsed // 60} 分 {elapsed % 60} 秒" if elapsed >= 60 else f"{elapsed} 秒"
        key = f'publish:{int(status["requested"])}'
        current[key] = f'发布成功：{_clock(status["published_at"])} 上线，用时 {elapsed_text}'
    # Installing this feature must not announce the last historical publish.
    return _new_keyed_messages(current, state_path, baseline_first_run=True)


def new_workflow_failure_messages(service: ReviewService, source_failure_marker: Path,
                                  state_path: Path, now: float) -> list[str]:
    current: dict[str, str] = {}
    marker_time = None
    try:
        marker_time = source_failure_marker.stat().st_mtime
    except OSError:
        pass
    if marker_time is not None and marker_time <= now + 300:
        current[f"source:{source_failure_marker.stat().st_mtime_ns}"] = (
            f"数据更新失败：{_clock(marker_time)} 的任务未完成，请检查更新流程"
        )

    ingest = service.ingest_status()
    marker_covers_request = (marker_time is not None and ingest["requested"] is not None
                             and marker_time + 5 >= ingest["requested"])
    if ingest["overdue"] and not marker_covers_request:
        current[f'ingest:{int(ingest["requested"])}'] = (
            f'数据更新失败：{_clock(ingest["requested"])} 发起的任务未完成，请检查更新流程'
        )

    publish = service.publish_status()
    if publish["overdue"]:
        current[f'publish:{int(publish["requested"])}'] = (
            f'发布失败：{_clock(publish["requested"])} 发起的内容尚未上线，请检查发布流程'
        )
    return _new_keyed_messages(current, state_path)


def new_pending_titles(service: ReviewService, state_path: Path) -> list[str]:
    """Diff the current pending items against the last-seen set recorded in
    state_path, return newly seen titles in path order, and persist the
    updated seen set. Pure with respect to the passed-in service."""
    items = [item for item in service.list_items() if item.status == "pending"]
    current = {item.path: notification_title(item) for item in items}
    seen = _read_seen(state_path)
    new_paths = sorted(path for path in current if path not in seen)
    _atomic_write_json(state_path, sorted(current))
    return [current[path] for path in new_paths]


def newly_stuck_approved_titles(service: ReviewService, threshold_seconds: int, now: float, state_path: Path) -> list[str]:
    """Items approved more than threshold_seconds ago that still haven't
    published (their rough file hasn't disappeared yet). Reports each stuck
    item once; it can be reported again later if it recovers and then gets
    stuck a second time."""
    stuck: dict[str, str] = {}
    for item in service.list_items():
        if item.status != "approved":
            continue
        try:
            decided = datetime.fromisoformat(item.decided_at).timestamp()
        except (ValueError, TypeError):
            continue
        if now - decided >= threshold_seconds:
            stuck[item.path] = notification_title(item)
    seen = _read_seen(state_path)
    new_paths = sorted(path for path in stuck if path not in seen)
    _atomic_write_json(state_path, sorted(stuck))
    return [stuck[path] for path in new_paths]


def _bulleted(title: str, titles: list[str]) -> str:
    shown = titles[:10]
    preview = "\n".join(f"· {value}" for value in shown)
    more = f"\n（还有 {len(titles) - 10} 条）" if len(titles) > 10 else ""
    return f"{title}\n{preview}{more}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--clones", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--pending-state", type=Path, required=True)
    parser.add_argument("--stuck-state", type=Path, required=True)
    parser.add_argument("--publish-success-state", type=Path, required=True)
    parser.add_argument("--workflow-failure-state", type=Path, required=True)
    parser.add_argument("--ingest-request", type=Path, required=True)
    parser.add_argument("--ingest-summary", type=Path, required=True)
    parser.add_argument("--publish-request", type=Path, required=True)
    parser.add_argument("--source-failure-marker", type=Path, required=True)
    parser.add_argument("--stuck-threshold-seconds", type=int, default=DEFAULT_STUCK_THRESHOLD_SECONDS)
    args = parser.parse_args(argv)

    ids = reviewer_ids()
    if not ids:
        return 0
    failure_ids = failure_reviewer_ids()
    if len(failure_ids) != 1 or failure_ids[0] not in ids:
        raise RuntimeError("exactly one failure reviewer must also be an authorized reviewer")

    decision_key = Path(os.environ["DEK_REVIEW_DECISION_KEY_FILE"]).read_bytes().strip()
    source = IsolatedReviewClone(args.bundle, args.clones)
    service = ReviewService(
        source, args.queue,
        audit_key=os.environ["DEK_REVIEW_AUDIT_KEY"].encode(),
        queue_key=decision_key,
        nonces=MemoryFormNonceStore(clock=time.time),
        clock=time.time,
        ingest_state_path=args.ingest_request,
        ingest_summary_path=args.ingest_summary,
        publish_state_path=args.publish_request,
    )

    pending = new_pending_titles(service, args.pending_state)
    stuck = newly_stuck_approved_titles(service, args.stuck_threshold_seconds, time.time(), args.stuck_state)
    publish_success = new_publish_success_messages(service, args.publish_success_state)
    failures = new_workflow_failure_messages(
        service, args.source_failure_marker, args.workflow_failure_state, time.time()
    )
    reviewer_parts = []
    if pending:
        reviewer_parts.append(_bulleted(f"有 {len(pending)} 条新内容待审核", pending))
    reviewer_parts.extend(publish_success)
    failure_parts = list(failures)
    if stuck:
        minutes = args.stuck_threshold_seconds // 60
        failure_parts.append(_bulleted(f"有 {len(stuck)} 条已批准超过 {minutes} 分钟仍未发布，请检查发布流程", stuck))
    client = dingtalk_client()
    if failure_parts:
        message = f"DEK 知识库：\n" + "\n\n".join(failure_parts) + f"\n{REVIEW_ORIGIN}/review/"
        client.send_work_notification(failure_ids, message)
    if reviewer_parts:
        message = f"DEK 知识库：\n" + "\n\n".join(reviewer_parts) + f"\n{REVIEW_ORIGIN}/review/"
        client.send_work_notification(ids, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
