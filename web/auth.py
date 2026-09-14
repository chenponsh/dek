"""Fail-closed authorization contract for a future DingTalk login gateway.

The login gateway must independently verify DingTalk identity and current Kbot
eligibility, then mint this short-lived claim. The static site never receives
DingTalk credentials or calls its APIs directly.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    user_id: str | None
    reason: str
    display_name: str | None = None


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_claim(payload: dict, secret: bytes) -> str:
    if len(secret) < 16:
        raise ValueError("claim secret is too short")
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64encode(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def authorize_claim(token: str | None, secret: bytes, *, now: int) -> AuthDecision:
    if not token:
        return AuthDecision(False, None, "missing")
    try:
        body, supplied = token.split(".", 1)
        expected = _b64encode(hmac.new(secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied, expected):
            raise ValueError
        payload = json.loads(_b64decode(body))
        user_id = payload.get("user_id")
        display_name = payload.get("display_name") or "同事"
        exp = int(payload.get("exp", 0))
        if not isinstance(user_id, str) or not user_id or len(user_id) > 200:
            raise ValueError
        if not isinstance(display_name, str) or len(display_name) > 100 or any(ord(character) < 32 for character in display_name):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        return AuthDecision(False, None, "invalid")
    if exp <= now:
        return AuthDecision(False, user_id, "expired", display_name)
    if payload.get("kbot_allowed") is not True:
        return AuthDecision(False, user_id, "not_kbot_allowed", display_name)
    return AuthDecision(True, user_id, "allowed", display_name)
