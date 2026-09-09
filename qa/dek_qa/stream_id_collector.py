from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import signal
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

PAIR_PREFIX = "DEK-QA PAIR"
PLACEHOLDERS = frozenset({"...", "实际值", "changeme", "change-me", "placeholder", "todo", "test"})
SENSITIVE_LOG_PATTERNS = (
    re.compile(r"(?i)(ticket|token|secret|authorization|session[_-]?webhook)(\s*[:=]\s*)([^\s,}\]]+)"),
    re.compile(r'(?i)(["\'](?:ticket|token|secret|clientSecret|sessionWebhook)["\']\s*:\s*["\'])(.*?)(["\'])'),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+"),
    re.compile(r"(?i)https://(?:api|oapi)\.dingtalk\.com/[^\s\"']*(?:token|sign|ticket|webhook)[^\s\"']*"),
)


def redact_log_text(value: object) -> str:
    text = str(value)
    text = SENSITIVE_LOG_PATTERNS[2].sub(r"\1[REDACTED]", text)
    text = SENSITIVE_LOG_PATTERNS[0].sub(r"\1\2[REDACTED]", text)
    text = SENSITIVE_LOG_PATTERNS[1].sub(r"\1[REDACTED]\3", text)
    return SENSITIVE_LOG_PATTERNS[3].sub("[REDACTED_DINGTALK_URL]", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_log_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_log_text("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


def configure_safe_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    redactor = RedactingFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(redactor)
    for name in ("dingtalk_stream", "dingtalk_stream.client", "dek_qa.stream_collector"):
        logger = logging.getLogger(name)
        logger.addFilter(redactor)
        for handler in logger.handlers:
            handler.addFilter(redactor)


def generate_pairing_code() -> str:
    return secrets.token_urlsafe(9)


def pairing_message(code: str) -> str:
    return f"{PAIR_PREFIX} {code}"


def extract_plain_text(payload: dict[str, Any]) -> str | None:
    if payload.get("msgtype") != "text":
        return None
    text = payload.get("text")
    if not isinstance(text, dict) or not isinstance(text.get("content"), str):
        return None
    return text["content"].strip()


def contains_pairing_message(text: str | None, expected: str) -> bool:
    if not text:
        return False
    return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(expected)}(?![A-Za-z0-9_-])", text) is not None


def ack_only_process(collector: "CandidateCollector", payload: dict[str, Any]) -> tuple[int, str]:
    collector.process_payload(payload)
    return 200, ""


def _private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".stream-candidates-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True)
class CollectorLimits:
    expires_at: datetime
    max_events: int


class CandidateCollector:
    def __init__(self, code: str, output: Path, limits: CollectorLimits,
                 on_complete: Callable[[], None] | None = None,
                 now: Callable[[], datetime] | None = None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", code):
            raise ValueError("invalid pairing code")
        if limits.max_events < 1 or limits.max_events > 10:
            raise ValueError("max_events must be between 1 and 10")
        self._expected = pairing_message(code)
        self._output = output
        self._limits = limits
        self._on_complete = on_complete or (lambda: None)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._candidates: list[dict[str, str]] = []
        self._completed = False
        self._write("collecting")

    @property
    def candidates(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(candidate) for candidate in self._candidates)

    def expired(self) -> bool:
        return self._now() >= self._limits.expires_at

    def process_payload(self, payload: dict[str, Any]) -> bool:
        if self.expired() or not contains_pairing_message(extract_plain_text(payload), self._expected):
            return False
        candidate = {
            "sender_staff_id": str(payload.get("senderStaffId") or ""),
            "sender_id": str(payload.get("senderId") or ""),
            "conversation_id": str(payload.get("conversationId") or payload.get("senderId") or ""),
            "conversation_type": str(payload.get("conversationType") or "1"),
            "sender_corp_id": str(payload.get("senderCorpId") or ""),
            "chatbot_corp_id": str(payload.get("chatbotCorpId") or ""),
        }
        if not candidate["conversation_id"] or not (candidate["sender_staff_id"] or candidate["sender_id"]):
            return False
        if candidate not in self._candidates:
            self._candidates.append(candidate)
            self._write("complete" if len(self._candidates) >= self._limits.max_events else "collecting")
        if len(self._candidates) >= self._limits.max_events and not self._completed:
            self._completed = True
            self._on_complete()
        return True

    def mark_expired(self) -> None:
        self._write("expired")

    def _write(self, status: str) -> None:
        _private_json(self._output, {
            "version": 1,
            "status": status,
            "generated_at": self._now().isoformat(),
            "expires_at": self._limits.expires_at.isoformat(),
            "max_events": self._limits.max_events,
            "candidates": self._candidates,
            "authorization_granted": False,
        })


def _read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def _valid_credential(value: str) -> bool:
    lowered = value.strip().lower()
    return bool(lowered) and lowered not in PLACEHOLDERS and not any(part in lowered for part in ("your-", "your_", "<", ">"))


async def run_managed_session(session, stop_event: asyncio.Event, ttl_seconds: float,
                              on_timeout: Callable[[], None]) -> None:
    session_task = asyncio.create_task(session(stop_event))
    stop_task = asyncio.create_task(stop_event.wait())
    timeout_task = asyncio.create_task(asyncio.sleep(ttl_seconds))
    tasks = {session_task, stop_task, timeout_task}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if timeout_task in done:
            on_timeout()
            stop_event.set()
        if session_task in done:
            await session_task
    finally:
        stop_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def install_stop_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_event.set)


def remove_stop_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.remove_signal_handler(signum)


def _open_connection_once(client, timeout: float) -> dict[str, str]:
    import requests

    topics = [{"type": "CALLBACK", "topic": topic} for topic in client.callback_handler_map]
    body = {
        "clientId": client.credential.client_id,
        "clientSecret": client.credential.client_secret,
        "subscriptions": topics,
        "ua": "dek-qa-stream-id-collector/1",
        "localIp": client.get_host_ip(),
    }
    response = requests.post(
        client.OPEN_CONNECTION_API,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        json=body,
        timeout=timeout,
    )
    response.raise_for_status()
    result = response.json()
    endpoint, ticket = result.get("endpoint"), result.get("ticket")
    if not isinstance(endpoint, str) or not endpoint.startswith("wss://") or not isinstance(ticket, str) or not ticket:
        raise RuntimeError("Stream handshake returned an invalid endpoint or missing ticket")
    return {"endpoint": endpoint, "ticket": ticket}


async def _stream_session(client, stop_event: asyncio.Event) -> None:
    from urllib.parse import quote_plus

    import websockets

    connection = await asyncio.to_thread(_open_connection_once, client, 15.0)
    uri = f'{connection["endpoint"]}?ticket={quote_plus(connection["ticket"])}'
    children: set[asyncio.Task] = set()
    async with websockets.connect(uri, open_timeout=15, close_timeout=5) as websocket:
        client.websocket = websocket

        async def close_when_stopped():
            await stop_event.wait()
            await websocket.close()

        children.add(asyncio.create_task(client.keepalive(websocket)))
        children.add(asyncio.create_task(close_when_stopped()))
        try:
            async for raw_message in websocket:
                if stop_event.is_set():
                    break
                task = asyncio.create_task(client.background_task(json.loads(raw_message)))
                children.add(task)
                task.add_done_callback(children.discard)
        finally:
            stop_event.set()
            for task in tuple(children):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*children, return_exceptions=True)
            client.websocket = None


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot DingTalk Stream ID collector (ACK only)")
    parser.add_argument("--environment", type=Path, default=Path("/var/lib/dek-qa/secrets/environment"))
    parser.add_argument("--output-dir", type=Path, default=Path("/var/lib/dek-qa/candidates"))
    parser.add_argument("--ttl-seconds", type=int, default=300)
    parser.add_argument("--max-events", type=int, default=2)
    args = parser.parse_args()
    if not 30 <= args.ttl_seconds <= 900:
        parser.error("ttl-seconds must be between 30 and 900")
    values = _read_environment(args.environment)
    client_id = values.get("DINGTALK_CLIENT_ID", "")
    client_secret = values.get("DINGTALK_CLIENT_SECRET", "")
    if not _valid_credential(client_id) or not _valid_credential(client_secret):
        parser.error("DingTalk credentials are missing or placeholders")

    from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage, Credential, DingTalkStreamClient

    now = datetime.now(timezone.utc)
    code = generate_pairing_code()
    output = args.output_dir / f"stream-candidates-{now.strftime('%Y%m%d_%H%M%S')}.json"
    stop_event: asyncio.Event | None = None

    def complete() -> None:
        if stop_event is not None:
            stop_event.set()

    collector = CandidateCollector(code, output,
        CollectorLimits(now + timedelta(seconds=args.ttl_seconds), args.max_events),
        on_complete=complete)

    class Handler(ChatbotHandler):
        async def process(self, message):
            try:
                code, response = ack_only_process(
                    collector, message.data if isinstance(message.data, dict) else {}
                )
            except Exception:
                logging.getLogger("dek_qa.stream_collector").exception("message rejected due to collector error")
                return AckMessage.STATUS_OK, ""
            return code, response

    configure_safe_logging()
    print(f"pairing_message={pairing_message(code)}")
    print(f"candidate_file={output}")
    print(f"expires_in_seconds={args.ttl_seconds}")
    client = DingTalkStreamClient(Credential(client_id, client_secret))
    client.register_callback_handler(ChatbotMessage.TOPIC, Handler())

    async def run() -> None:
        nonlocal stop_event
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        install_stop_signal_handlers(loop, stop_event)
        try:
            await run_managed_session(
                lambda event: _stream_session(client, event),
                stop_event,
                args.ttl_seconds,
                collector.mark_expired,
            )
        finally:
            remove_stop_signal_handlers(loop)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
