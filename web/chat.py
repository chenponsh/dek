"""Small, fail-closed bridge from the authenticated website to Hermes QA."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections import defaultdict, deque
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ChatUnavailable(RuntimeError):
    pass


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
