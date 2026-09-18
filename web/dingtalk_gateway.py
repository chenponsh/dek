"""DingTalk OAuth login flow for the internal read-only site."""
from __future__ import annotations

import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .auth import sign_claim

AUTHORIZE_URL = "https://login.dingtalk.com/oauth2/auth"
TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"
ME_URL = "https://api.dingtalk.com/v1.0/contact/users/me"
APP_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
SCOPE_URL = "https://api.dingtalk.com/v1.0/microApp/apps/{agent_id}/scopes"
USER_BY_UNION_URL = "https://oapi.dingtalk.com/topapi/user/getbyunionid?access_token={token}"
USER_DETAIL_URL = "https://oapi.dingtalk.com/topapi/v2/user/get?access_token={token}"


class LoginError(RuntimeError): pass


def _is_identifier(value: object) -> bool:
    return (isinstance(value, str) and bool(value)) or (isinstance(value, int) and not isinstance(value, bool))


def scope_allows(user_id: object, department_ids: list, role_ids: list, scope: dict, *, is_admin: bool = False) -> bool:
    """Return whether a user belongs to a DingTalk application scope."""
    if not _is_identifier(user_id): return False
    if scope.get("onlyAdminVisible") is True: return is_admin
    direct = {str(item) for item in scope.get("userIds") or []}
    departments = {str(item) for item in department_ids}
    roles = {str(item) for item in role_ids}
    return str(user_id) in direct or bool(departments & {str(item) for item in scope.get("deptIds") or []}) or bool(roles & {str(item) for item in scope.get("roleIds") or []})


class DingTalkClientProtocol(Protocol):
    def exchange_code(self, code: str) -> str: ...
    def current_user(self, token: str) -> dict: ...
    def is_kbot_allowed(self, user: dict) -> bool: ...


class MemoryStateStore:
    def __init__(self, max_items: int = 1024, clock=time.time):
        if max_items < 1: raise ValueError("max_items must be positive")
        self._items: dict[str, tuple[int, str, str]] = {}
        self._max_items, self._clock = max_items, clock
    def put(self, state: str, expires: int, return_path: str, browser_id: str) -> None:
        now = int(self._clock())
        self._items = {key: item for key, item in self._items.items() if item[0] > now}
        self._items[state] = (expires, return_path, browser_id)
        while len(self._items) > self._max_items:
            del self._items[min(self._items, key=lambda key: self._items[key][0])]
    def contains(self, state: str) -> bool: return state in self._items
    def consume(self, state: str, now: int, browser_id: str) -> str | None:
        item = self._items.pop(state, None)
        return item[1] if item and item[0] > now and secrets.compare_digest(item[2], browser_id) else None


class DingTalkClient:
    def __init__(self, client_id: str, client_secret: str, agent_id: str, timeout: float = 10):
        self.client_id, self.client_secret = client_id, client_secret
        self.agent_id, self.timeout = agent_id, timeout
    def _json(self, request: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            machine_code = "unknown"
            try:
                payload = json.loads(error.read())
                candidate = payload.get("code") or payload.get("errcode")
                if isinstance(candidate, (str, int)) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(candidate)):
                    machine_code = str(candidate)
            except Exception:
                pass
            raise LoginError(f"dingtalk_http_{error.code}_{machine_code}") from None
        except Exception:
            raise LoginError("dingtalk_request_failed") from None
        if not isinstance(payload, dict):
            raise LoginError("dingtalk_response_invalid")
        return payload
    def exchange_code(self, code: str) -> str:
        body = json.dumps({"clientId": self.client_id, "clientSecret": self.client_secret, "code": code, "grantType": "authorization_code"}).encode()
        data = self._json(urllib.request.Request(TOKEN_URL, body, {"Content-Type": "application/json"}, method="POST"))
        token = data.get("accessToken")
        if not token: raise LoginError("token_exchange_failed")
        return token
    def current_user(self, token: str) -> dict:
        return self._json(urllib.request.Request(ME_URL, headers={"x-acs-dingtalk-access-token": token}))
    def _app_token(self) -> str:
        body = json.dumps({"appKey": self.client_id, "appSecret": self.client_secret}).encode()
        data = self._json(urllib.request.Request(APP_TOKEN_URL, body, {"Content-Type": "application/json"}, method="POST"))
        token = data.get("accessToken")
        if not token: raise LoginError("app_token_failed")
        return str(token)
    def is_kbot_allowed(self, user: dict) -> bool:
        union_id = user.get("unionId")
        if not union_id: return False
        token = self._app_token()
        scope_data = self._json(urllib.request.Request(
            SCOPE_URL.format(agent_id=urllib.parse.quote(self.agent_id, safe="")),
            headers={"x-acs-dingtalk-access-token": token},
        ))
        if not isinstance(scope_data, dict) or not isinstance(scope_data.get("result"), dict):
            raise LoginError("scope_response_invalid")
        scope = scope_data["result"]
        identifier_fields = ("userIds", "deptIds", "roleIds")
        if "onlyAdminVisible" not in scope or type(scope["onlyAdminVisible"]) is not bool:
            raise LoginError("scope_response_invalid")
        for field in identifier_fields:
            # The live endpoint omits a dimension that has no entries instead of
            # returning an empty list; an absent dimension must behave as empty.
            if field not in scope:
                scope[field] = []
            elif not isinstance(scope[field], list):
                raise LoginError("scope_response_invalid")
        if any(not _is_identifier(item) for field in identifier_fields for item in scope[field]):
            raise LoginError("scope_response_invalid")
        body = json.dumps({"unionid": union_id}).encode()
        identity_data = self._json(urllib.request.Request(
            USER_BY_UNION_URL.format(token=urllib.parse.quote(token, safe="")), body,
            {"Content-Type": "application/json"}, method="POST",
        ))
        if not isinstance(identity_data, dict) or identity_data.get("errcode") != 0 or not isinstance(identity_data.get("result"), dict):
            error_code = identity_data.get("errcode") if isinstance(identity_data, dict) else None
            if isinstance(error_code, (str, int)) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(error_code)):
                raise LoginError(f"identity_response_invalid:errcode_{error_code}")
            raise LoginError("identity_response_invalid")
        identity = identity_data["result"]
        user_id = identity.get("userid") or identity.get("userId")
        if user_id is None or user_id == "": return False
        if not _is_identifier(user_id):
            raise LoginError("identity_response_invalid")
        if scope_allows(user_id, [], [], scope):
            user["_dek_enterprise_user_id"] = str(user_id)
            return True
        if not (scope.get("deptIds") or scope.get("roleIds") or scope.get("onlyAdminVisible")): return False
        detail_body = json.dumps({"userid": user_id}).encode()
        detail_data = self._json(urllib.request.Request(
            USER_DETAIL_URL.format(token=urllib.parse.quote(token, safe="")), detail_body,
            {"Content-Type": "application/json"}, method="POST",
        ))
        if not isinstance(detail_data, dict) or detail_data.get("errcode") != 0 or not isinstance(detail_data.get("result"), dict):
            raise LoginError("user_detail_response_invalid")
        detail = detail_data["result"]
        detail_user_id = detail.get("userid") or detail.get("userId")
        if not _is_identifier(detail_user_id) or str(detail_user_id) != str(user_id):
            raise LoginError("user_detail_response_invalid")
        department_items = detail.get("dept_id_list") or detail.get("deptIdList") or []
        if not isinstance(department_items, list) or any(not _is_identifier(item) for item in department_items):
            raise LoginError("user_detail_response_invalid")
        departments = {str(item) for item in department_items}
        roles = detail.get("role_list") or detail.get("roleList") or detail.get("roles") or []
        if not isinstance(roles, list) or any(
            not isinstance(item, dict) or not _is_identifier(item.get("id")) for item in roles
        ):
            raise LoginError("user_detail_response_invalid")
        role_ids = {str(item["id"]) for item in roles}
        allowed = scope_allows(
            user_id, list(departments), list(role_ids), scope,
            is_admin=detail.get("admin") is True or detail.get("isAdmin") is True,
        )
        if allowed:
            user["_dek_enterprise_user_id"] = str(user_id)
        return allowed


@dataclass(frozen=True)
class LoginResult:
    user_id: str
    display_name: str
    return_path: str
    claim: str
    expires_at: int
    enterprise_user_id: str | None = None


class DingTalkGateway:
    def __init__(self, client_id: str, redirect_uri: str, claim_secret: bytes | None, client: DingTalkClientProtocol, states: MemoryStateStore):
        if not redirect_uri.startswith("https://"): raise ValueError("redirect_uri must use https")
        self.client_id, self.redirect_uri = client_id, redirect_uri
        self.claim_secret, self.client, self.states = claim_secret, client, states
    def login_url(self, return_path: str = "/", *, browser_id: str, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        if not return_path.startswith("/") or return_path.startswith("//"): return_path = "/"
        state = secrets.token_urlsafe(32)
        self.states.put(state, now + 300, return_path, browser_id)
        query = urllib.parse.urlencode({"redirect_uri": self.redirect_uri, "response_type": "code", "client_id": self.client_id, "scope": "openid corpid Contact.User.Read", "state": state, "prompt": "consent"})
        return f"{AUTHORIZE_URL}?{query}"
    def callback(self, code: str, state: str, *, browser_id: str, now: int | None = None) -> LoginResult:
        now = int(time.time()) if now is None else now
        return_path = self.states.consume(state, now, browser_id)
        if not code or return_path is None: raise LoginError("invalid_state")
        try:
            token = self.client.exchange_code(code)
        except LoginError as error:
            raise LoginError(f"token_exchange_request_failed:{error}") from None
        try:
            user = self.client.current_user(token)
        except LoginError as error:
            raise LoginError(f"current_user_request_failed:{error}") from None
        if not isinstance(user, dict):
            raise LoginError("current_user_response_invalid")
        user_id = user.get("unionId") or user.get("userId")
        if not _is_identifier(user_id):
            raise LoginError("current_user_response_invalid")
        try:
            allowed = self.client.is_kbot_allowed(user)
        except LoginError as error:
            raise LoginError(f"scope_check_request_failed:{error}") from None
        if not user_id or not allowed:
            raise LoginError("not_kbot_allowed")
        enterprise_user_id = user.get("_dek_enterprise_user_id")
        if enterprise_user_id is not None:
            if not _is_identifier(enterprise_user_id):
                raise LoginError("identity_response_invalid")
            user_id = enterprise_user_id
        expires = now + 8 * 3600
        nick = user.get("nick")
        display_name = nick if isinstance(nick, str) and nick else "同事"
        claim = sign_claim({"user_id": str(user_id), "display_name": display_name, "kbot_allowed": True, "exp": expires}, self.claim_secret) if self.claim_secret else ""
        exact_enterprise_id = str(enterprise_user_id) if enterprise_user_id is not None else None
        return LoginResult(str(user_id), display_name, return_path, claim, expires, exact_enterprise_id)
