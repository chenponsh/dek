import unittest
import io
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

from web.dingtalk_gateway import DingTalkClient, DingTalkGateway, LoginError, MemoryStateStore, scope_allows


class FakeClient:
    def exchange_code(self, code):
        if code == "bad": raise LoginError("token_exchange_failed")
        return "user-token"
    def current_user(self, token):
        return {"unionId": "union-1", "corpId": "corp-1", "nick": "同事"}
    def is_kbot_allowed(self, user): return True


class StageFailingClient(FakeClient):
    def __init__(self, stage): self.stage = stage
    def exchange_code(self, code):
        if self.stage == "exchange": raise LoginError("dingtalk_request_failed")
        return super().exchange_code(code)
    def current_user(self, token):
        if self.stage == "current_user": raise LoginError("dingtalk_request_failed")
        return super().current_user(token)
    def is_kbot_allowed(self, user):
        if self.stage == "scope": raise LoginError("dingtalk_request_failed")
        return True


class ScopeClient(DingTalkClient):
    def __init__(self, responses):
        super().__init__("client-id", "client-secret", agent_id="123")
        self.responses = iter(responses)

    def _json(self, request):
        return next(self.responses)


class DingTalkScopeTests(unittest.TestCase):
    def test_scope_rejects_missing_admin_flag_types_and_non_list_identifiers(self):
        valid = {"userIds": [], "deptIds": [], "roleIds": [], "onlyAdminVisible": True}
        malformed = [{"userIds": [], "deptIds": [], "roleIds": []}]
        malformed.extend((
            {**valid, "onlyAdminVisible": 1},
            {**valid, "onlyAdminVisible": "true"},
            {**valid, "userIds": "staff-1"},
            {**valid, "deptIds": {"0": 1}},
            {**valid, "roleIds": 7},
        ))
        for scope in malformed:
            with self.subTest(scope=scope):
                client = ScopeClient([{"accessToken": "app-token"}, {"result": scope}])
                with self.assertRaisesRegex(LoginError, r"^scope_response_invalid$"):
                    client.is_kbot_allowed({"unionId": "union-1"})

    def test_scope_omitting_empty_identifier_fields_still_authorizes_listed_user(self):
        # The live endpoint omits identifier fields that are empty instead of
        # returning an empty list; an absent dimension must behave as empty.
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": ["staff-1"], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
        ])

        user = {"unionId": "union-1"}
        self.assertTrue(client.is_kbot_allowed(user))
        self.assertEqual(user["_dek_enterprise_user_id"], "staff-1")

    def test_scope_omitting_every_identifier_field_denies_unknown_user(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
        ])

        user = {"unionId": "union-1"}
        self.assertFalse(client.is_kbot_allowed(user))
        self.assertNotIn("_dek_enterprise_user_id", user)

    def test_successful_non_object_json_responses_are_sanitized(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return self.payload

        client = DingTalkClient("client-id", "client-secret", agent_id="123")
        for payload in (b'[]', b'"secret-value"', b'null'):
            with self.subTest(payload=payload):
                with patch("urllib.request.urlopen", return_value=Response(payload)):
                    with self.assertRaisesRegex(LoginError, r"^dingtalk_response_invalid$") as raised:
                        client._json(Request("https://api.dingtalk.com/safe"))
                self.assertNotIn("secret-value", str(raised.exception))

    def test_http_failure_reports_only_status_and_machine_error_code(self):
        client = DingTalkClient("client-id", "client-secret", agent_id="123")
        error = HTTPError(
            "https://api.dingtalk.com/safe",
            403,
            "Forbidden",
            Message(),
            io.BytesIO(b'{"code":"Forbidden.AccessDenied","message":"contains sensitive detail"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(LoginError, r"^dingtalk_http_403_Forbidden.AccessDenied$") as raised:
                client._json(Request("https://api.dingtalk.com/safe"))

        self.assertNotIn("sensitive", str(raised.exception))

    def test_request_failure_does_not_chain_credential_bearing_url(self):
        client = DingTalkClient("client-id", "client-secret", agent_id="123")
        request = Request("https://example.invalid/path?access_token=secret-token")
        with patch("urllib.request.urlopen", side_effect=RuntimeError(request.full_url)):
            with self.assertRaises(LoginError) as raised:
                client._json(request)

        self.assertIsNone(raised.exception.__cause__)

    def test_direct_user_in_application_scope_is_allowed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": ["staff-1"], "deptIds": [], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
        ])

        user = {"unionId": "union-1"}
        self.assertTrue(client.is_kbot_allowed(user))
        self.assertEqual(user["_dek_enterprise_user_id"], "staff-1")

    def test_department_member_in_application_scope_is_allowed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [42], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"errcode": 0, "result": {"userid": "staff-1", "dept_id_list": [7, 42], "role_list": []}},
        ])

        self.assertTrue(client.is_kbot_allowed({"unionId": "union-1"}))

    def test_string_department_membership_fails_closed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": ["2"], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"errcode": 0, "result": {"userid": "staff-1", "dept_id_list": "42", "role_list": []}},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_department_items_must_be_scalar_ids(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [42], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"errcode": 0, "result": {"userid": "staff-1", "dept_id_list": [{"id": 42}], "role_list": []}},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_role_member_in_application_scope_is_allowed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [], "roleIds": [88], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"errcode": 0, "result": {"userid": "staff-1", "dept_id_list": [], "role_list": [{"id": 88}]}},
        ])

        self.assertTrue(client.is_kbot_allowed({"unionId": "union-1"}))

    def test_role_membership_must_be_a_list(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [], "roleIds": [88], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"errcode": 0, "result": {"userid": "staff-1", "dept_id_list": [], "role_list": "88"}},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_role_items_must_have_scalar_ids(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [], "roleIds": [88], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"errcode": 0, "result": {"userid": "staff-1", "dept_id_list": [], "role_list": [{"id": [88]}]}},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_only_admin_scope_allows_enterprise_admin(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [], "roleIds": [], "onlyAdminVisible": True}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"errcode": 0, "result": {"userid": "staff-1", "admin": True, "dept_id_list": [], "role_list": []}},
        ])

        self.assertTrue(client.is_kbot_allowed({"unionId": "union-1"}))

    def test_scope_identifier_fields_must_be_lists(self):
        for field in ("userIds", "deptIds", "roleIds"):
            with self.subTest(field=field):
                scope = {"userIds": [], "deptIds": [], "roleIds": [], "onlyAdminVisible": False}
                scope[field] = "staff-1"
                client = ScopeClient([
                    {"accessToken": "app-token"},
                    {"result": scope},
                ])

                with self.assertRaises(LoginError):
                    client.is_kbot_allowed({"unionId": "union-1"})

    def test_scope_identifier_items_must_be_scalar_ids(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [{"id": "staff-1"}], "deptIds": [], "roleIds": [], "onlyAdminVisible": False}},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_identity_result_must_be_object(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": "staff-1"},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_identity_user_id_must_be_scalar(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": ["staff-1"]}},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_nested_identity_user_id_fails_closed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": {"value": "staff-1"}}},
        ])

        with self.assertRaisesRegex(LoginError, r"^identity_response_invalid$"):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_legacy_identity_missing_error_code_fails_closed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": ["staff-1"], "deptIds": [], "roleIds": [], "onlyAdminVisible": False}},
            {"result": {"userid": "staff-1"}},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_legacy_identity_error_code_fails_closed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": ["staff-1"], "deptIds": [], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 1, "result": {"userid": "staff-1"}},
        ])

        with self.assertRaisesRegex(LoginError, r"^identity_response_invalid:errcode_1$"):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_user_detail_result_must_be_object(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [42], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"errcode": 0, "result": "staff-1"},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_user_detail_user_id_must_be_scalar(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [42], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"errcode": 0, "result": {"userid": ["staff-1"], "dept_id_list": [42], "role_list": []}},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_legacy_user_detail_missing_error_code_fails_closed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [42], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"result": {"userid": "staff-1", "dept_id_list": [42], "role_list": []}},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_legacy_user_detail_error_code_fails_closed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"result": {"userIds": [], "deptIds": [42], "roleIds": [], "onlyAdminVisible": False}},
            {"errcode": 0, "result": {"userid": "staff-1"}},
            {"errcode": 1, "result": {"userid": "staff-1", "dept_id_list": [42]}},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_non_object_scope_response_fails_closed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            ["unexpected"],
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})

    def test_malformed_scope_response_fails_closed(self):
        client = ScopeClient([
            {"accessToken": "app-token"},
            {"unexpected": "response"},
        ])

        with self.assertRaises(LoginError):
            client.is_kbot_allowed({"unionId": "union-1"})


class MemoryStateStoreTests(unittest.TestCase):
    def test_put_prunes_expired_states_and_enforces_capacity(self):
        store = MemoryStateStore(max_items=2, clock=lambda: 100)
        store.put("expired", 100, "/expired", "browser")
        store.put("later", 300, "/later", "browser")
        store.put("soon", 200, "/soon", "browser")
        store.put("latest", 400, "/latest", "browser")

        self.assertFalse(store.contains("expired"))
        self.assertFalse(store.contains("soon"))
        self.assertTrue(store.contains("later"))
        self.assertTrue(store.contains("latest"))


class DingTalkGatewayTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStateStore()
        self.gateway = DingTalkGateway(
            client_id="client-1", redirect_uri="https://kb.example/auth/callback",
            claim_secret=b"0123456789abcdef0123456789abcdef",
            client=FakeClient(), states=self.store,
        )

    def test_login_url_uses_one_time_state_and_registered_redirect(self):
        url = self.gateway.login_url("/wiki/page.html", browser_id="browser-a")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["client_id"], ["client-1"])
        self.assertEqual(query["redirect_uri"], ["https://kb.example/auth/callback"])
        self.assertEqual(query["scope"], ["openid corpid Contact.User.Read"])
        self.assertTrue(self.store.contains(query["state"][0]))

    def test_callback_rejects_missing_or_replayed_state(self):
        with self.assertRaises(LoginError): self.gateway.callback("code", "missing", browser_id="browser-a", now=100)
        state = parse_qs(urlparse(self.gateway.login_url("/", browser_id="browser-a", now=100)).query)["state"][0]
        result = self.gateway.callback("code", state, browser_id="browser-a", now=101)
        self.assertEqual(result.user_id, "union-1")
        self.assertEqual(result.user_id, "union-1")
        self.assertEqual(result.display_name, "同事")
        with self.assertRaises(LoginError): self.gateway.callback("code", state, browser_id="browser-a", now=102)

    def test_callback_uses_resolved_enterprise_user_id_for_claim(self):
        class ResolvedClient(FakeClient):
            def is_kbot_allowed(self, user):
                user["_dek_enterprise_user_id"] = "staff-1"
                return True
        gateway = DingTalkGateway(
            client_id="client-1", redirect_uri="https://kb.example/auth/callback",
            claim_secret=b"0123456789abcdef0123456789abcdef",
            client=ResolvedClient(), states=MemoryStateStore(),
        )
        state = parse_qs(urlparse(gateway.login_url("/", browser_id="browser-a", now=100)).query)["state"][0]
        result = gateway.callback("code", state, browser_id="browser-a", now=101)
        self.assertEqual(result.user_id, "staff-1")
        self.assertEqual(result.enterprise_user_id, "staff-1")

    def test_callback_rejects_state_from_a_different_browser(self):
        state = parse_qs(urlparse(self.gateway.login_url("/", browser_id="browser-a", now=100)).query)["state"][0]

        with self.assertRaises(LoginError):
            self.gateway.callback("code", state, browser_id="browser-b", now=101)

    def test_callback_identifies_external_failure_stage_without_sensitive_values(self):
        for stage, reason in (
            ("exchange", "token_exchange_request_failed"),
            ("current_user", "current_user_request_failed"),
            ("scope", "scope_check_request_failed"),
        ):
            with self.subTest(stage=stage):
                store = MemoryStateStore()
                gateway = DingTalkGateway(
                    client_id="client-1", redirect_uri="https://kb.example/auth/callback",
                    claim_secret=b"0123456789abcdef0123456789abcdef",
                    client=StageFailingClient(stage), states=store,
                )
                state = parse_qs(urlparse(gateway.login_url("/", browser_id="browser-a", now=100)).query)["state"][0]

                with self.assertRaisesRegex(LoginError, f"^{reason}:dingtalk_request_failed$"):
                    gateway.callback("secret-code", state, browser_id="browser-a", now=101)

    def test_callback_does_not_require_undocumented_corp_id_from_current_user(self):
        self.gateway.client.current_user = lambda token: {"unionId": "union-1", "nick": "同事"}
        state = parse_qs(urlparse(self.gateway.login_url("/", browser_id="browser-a", now=100)).query)["state"][0]

        result = self.gateway.callback("code", state, browser_id="browser-a", now=101)

    def test_callback_converts_malformed_current_user_to_login_error(self):
        for malformed in ([], "secret-identity", None, {"unionId": {"nested": "secret"}}):
            with self.subTest(malformed=malformed):
                self.gateway.client.current_user = lambda token, value=malformed: value
                state = parse_qs(urlparse(self.gateway.login_url("/", browser_id="browser-a", now=100)).query)["state"][0]
                with self.assertRaisesRegex(LoginError, r"^current_user_response_invalid$") as raised:
                    self.gateway.callback("code", state, browser_id="browser-a", now=101)
                self.assertNotIn("secret", str(raised.exception))

    def test_callback_mints_short_lived_kbot_claim_and_safe_return_path(self):
        state = parse_qs(urlparse(self.gateway.login_url("https://evil.example", browser_id="browser-a", now=100)).query)["state"][0]
        result = self.gateway.callback("code", state, browser_id="browser-a", now=101)
        self.assertEqual(result.return_path, "/")
        self.assertTrue(result.claim)
        self.assertEqual(result.expires_at, 101 + 8 * 3600)

    def test_application_scope_supports_direct_department_role_and_admin(self):
        self.assertTrue(scope_allows("u1", [2], [3], {"userIds": ["u1"]}))
        self.assertTrue(scope_allows("u1", [2], [3], {"deptIds": [2]}))
        self.assertTrue(scope_allows("u1", [2], [3], {"roleIds": [3]}))
        self.assertTrue(scope_allows("u1", [], [], {"onlyAdminVisible": True}, is_admin=True))
        self.assertFalse(scope_allows("u1", [2], [3], {"userIds": ["u2"], "deptIds": [4], "roleIds": [5]}))


if __name__ == "__main__": unittest.main()
