import os
import time
import unittest
from unittest.mock import patch

from web.auth import AuthDecision, authorize_claim, sign_claim


class AuthBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"test-secret-with-enough-entropy"
        self.now = 1_800_000_000

    def test_missing_claim_is_denied(self):
        self.assertEqual(authorize_claim(None, self.secret, now=self.now), AuthDecision(False, None, "missing"))

    def test_kbot_permission_is_required(self):
        token = sign_claim({"user_id": "staff-1", "kbot_allowed": False, "exp": self.now + 60}, self.secret)
        self.assertEqual(authorize_claim(token, self.secret, now=self.now).reason, "not_kbot_allowed")

    def test_expired_or_tampered_claim_is_denied(self):
        expired = sign_claim({"user_id": "staff-1", "kbot_allowed": True, "exp": self.now - 1}, self.secret)
        self.assertEqual(authorize_claim(expired, self.secret, now=self.now).reason, "expired")
        self.assertEqual(authorize_claim(expired + "x", self.secret, now=self.now).reason, "invalid")

    def test_valid_kbot_staff_claim_is_allowed(self):
        token = sign_claim({"user_id": "staff-1", "display_name": "张三", "kbot_allowed": True, "exp": self.now + 60}, self.secret)
        decision = authorize_claim(token, self.secret, now=self.now)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.user_id, "staff-1")
        self.assertEqual(decision.display_name, "张三")


if __name__ == "__main__":
    unittest.main()
