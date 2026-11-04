import io
import json
import unittest

from web.chat import ChatRateLimiter, HermesChatClient


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


if __name__ == "__main__": unittest.main()
