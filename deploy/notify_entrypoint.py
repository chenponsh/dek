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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve(strict=True).parents[1]))
from web.dingtalk_gateway import DingTalkClient
from web.review import IsolatedReviewClone, MemoryFormNonceStore, ReviewService, _item_title

REVIEW_ORIGIN = "https://regkb.chenponai.com"
MAX_TITLE_CHARS = 60
DEFAULT_STUCK_THRESHOLD_SECONDS = 30 * 60


def reviewer_ids() -> list[str]:
    return [value.strip() for value in os.environ.get("DEK_REVIEWER_IDS", "").split(",") if value.strip()]


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
    parser.add_argument("--stuck-threshold-seconds", type=int, default=DEFAULT_STUCK_THRESHOLD_SECONDS)
    args = parser.parse_args(argv)

    ids = reviewer_ids()
    if not ids:
        return 0

    decision_key = Path(os.environ["DEK_REVIEW_DECISION_KEY_FILE"]).read_bytes().strip()
    source = IsolatedReviewClone(args.bundle, args.clones)
    service = ReviewService(
        source, args.queue,
        audit_key=os.environ["DEK_REVIEW_AUDIT_KEY"].encode(),
        queue_key=decision_key,
        nonces=MemoryFormNonceStore(clock=time.time),
        clock=time.time,
    )

    pending = new_pending_titles(service, args.pending_state)
    stuck = newly_stuck_approved_titles(service, args.stuck_threshold_seconds, time.time(), args.stuck_state)
    if not pending and not stuck:
        return 0

    parts = []
    if pending:
        parts.append(_bulleted(f"有 {len(pending)} 条新内容待审核", pending))
    if stuck:
        minutes = args.stuck_threshold_seconds // 60
        parts.append(_bulleted(f"有 {len(stuck)} 条已批准超过 {minutes} 分钟仍未发布，请检查发布流程", stuck))
    message = f"DEK 知识库：\n" + "\n\n".join(parts) + f"\n{REVIEW_ORIGIN}/review/"
    dingtalk_client().send_work_notification(ids, message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
