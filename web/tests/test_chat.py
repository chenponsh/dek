import io
import json
import tempfile
import unittest
from pathlib import Path

from web.chat import ChatHistoryStore, ChatRateLimiter, HermesChatClient


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): return False


class ChatTests(unittest.TestCase):
    def test_client_uses_fixed_loopback_bearer_and_pseudonymous_stable_session(self):
        seen = []
        def opener(request, timeout):
            seen.append((request, timeout))
            return Response(json.dumps({"choices": [{"message": {"content": " 回答 "}}]}).encode())
        client = HermesChatClient("strong-api-key-0123456789", b"session-secret", opener=opener)
        self.assertEqual(client.ask("employee-1", "问题"), "回答")
        request, timeout = seen[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8642/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer strong-api-key-0123456789")
        session = request.get_header("X-hermes-session-id")
        self.assertTrue(session.startswith("web-"))
        self.assertNotIn("employee-1", session)
        self.assertEqual(session, client.session_id("employee-1"))
        self.assertEqual(json.loads(request.data)["messages"][0]["content"], "问题")
        self.assertEqual(timeout, 90)

    def test_rate_limiter_is_per_user_and_reopens_after_window(self):
        now = [100.0]
        limiter = ChatRateLimiter(limit=2, window=60, clock=lambda: now[0])
        self.assertTrue(limiter.allow("a")); self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a")); self.assertTrue(limiter.allow("b"))
        now[0] = 161.0
        self.assertTrue(limiter.allow("a"))

    def test_history_is_persistent_isolated_and_pseudonymous(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history" / "chat.sqlite3"
            store = ChatHistoryStore(path, b"history-secret-0123456789", maximum_messages=4, clock=lambda: 123)
            store.append_exchange("employee-1", "问题一", "回答一")
            store.append_exchange("employee-1", "问题二", "回答二")
            store.append_exchange("employee-1", "问题三", "回答三")

            self.assertEqual(store.list("employee-2"), [])
            self.assertEqual(
                [(item["role"], item["content"]) for item in store.list("employee-1")],
                [("user", "问题二"), ("assistant", "回答二"), ("user", "问题三"), ("assistant", "回答三")],
            )
            reopened = ChatHistoryStore(path, b"history-secret-0123456789", maximum_messages=4)
            self.assertEqual(reopened.list("employee-1"), store.list("employee-1"))
            self.assertNotIn(b"employee-1", path.read_bytes())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__": unittest.main()
