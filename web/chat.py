"""Small, fail-closed bridge from the authenticated website to Hermes QA."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ChatUnavailable(RuntimeError):
    pass


class ChatHistoryError(RuntimeError):
    pass


class ChatHistoryStore:
    """Private, per-user chat transcript storage for the authenticated website."""

    def __init__(self, path, user_key_secret: bytes, *, maximum_messages: int = 200, clock=time.time):
        self.path = os.fspath(path)
        self.user_key_secret = user_key_secret
        self.maximum_messages = maximum_messages
        self.clock = clock
        self._lock = threading.Lock()
        if len(user_key_secret) < 16 or maximum_messages < 2:
            raise ValueError("chat history configuration is invalid")
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_key TEXT NOT NULL,
                        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                        content TEXT NOT NULL,
                        created_at INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS messages_user_id ON messages(user_key, id);
                """)
            os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error) as exc:
            raise ChatHistoryError("chat history initialization failed") from exc

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _user_key(self, user_id: str) -> str:
        return hmac.new(self.user_key_secret, user_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def list(self, user_id: str) -> list[dict]:
        if not user_id:
            return []
        try:
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    "SELECT role, content, created_at FROM ("
                    "SELECT id, role, content, created_at FROM messages WHERE user_key=? "
                    "ORDER BY id DESC LIMIT ?) ORDER BY id",
                    (self._user_key(user_id), self.maximum_messages),
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise ChatHistoryError("chat history read failed") from exc
        return [{"role": role, "content": content, "created_at": created_at} for role, content, created_at in rows]

    def append_exchange(self, user_id: str, question: str, answer: str) -> None:
        if not user_id or not question or not answer:
            raise ChatHistoryError("chat history entry is invalid")
        now = int(self.clock())
        user_key = self._user_key(user_id)
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "INSERT INTO messages(user_key, role, content, created_at) VALUES(?,?,?,?)",
                    ((user_key, "user", question, now), (user_key, "assistant", answer, now)),
                )
                connection.execute(
                    "DELETE FROM messages WHERE user_key=? AND id NOT IN ("
                    "SELECT id FROM messages WHERE user_key=? ORDER BY id DESC LIMIT ?)",
                    (user_key, user_key, self.maximum_messages),
                )
                connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise ChatHistoryError("chat history write failed") from exc


class ChatRateLimiter:
    def __init__(self, limit: int = 10, window: int = 60, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, user_id: str) -> bool:
        now = self.clock()
        with self._lock:
            events = self._events[user_id]
            while events and events[0] <= now - self.window:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


class HermesChatClient:
    endpoint = "http://127.0.0.1:8642/v1/chat/completions"

    def __init__(self, api_key: str, session_secret: bytes, *, timeout: int = 90, opener=urlopen):
        self.api_key, self.session_secret = api_key, session_secret
        self.timeout, self.opener = timeout, opener

    def session_id(self, user_id: str) -> str:
        digest = hmac.new(self.session_secret, user_id.encode("utf-8"), hashlib.sha256).hexdigest()
        return "web-" + digest

    def ask(self, user_id: str, message: str) -> str:
        payload = json.dumps({
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": message}],
            "stream": False,
        }, ensure_ascii=False).encode("utf-8")
        request = Request(self.endpoint, data=payload, method="POST", headers={
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "X-Hermes-Session-Id": self.session_id(user_id),
        })
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise ChatUnavailable("QA service unavailable") from exc
        if len(raw) > 1024 * 1024:
            raise ChatUnavailable("QA response too large")
        try:
            content = json.loads(raw)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ChatUnavailable("QA response invalid") from exc
        if not isinstance(content, str) or not content.strip():
            raise ChatUnavailable("QA response empty")
        return content.strip()[:20000]
