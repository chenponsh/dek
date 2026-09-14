import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

from web.app import KnowledgeApp, make_dingtalk_client
from web.auth import sign_claim
from web.dingtalk_gateway import LoginError, LoginResult


class FakeGateway:
    def __init__(self): self.login_browser_id = None; self.callback_browser_id = None
    def login_url(self, return_path="/", *, browser_id):
        self.login_browser_id = browser_id
        return "https://login.example/?return=" + return_path
    def callback(self, code, state, *, browser_id):
        self.callback_browser_id = browser_id
        return LoginResult("u1", "同事", "/wiki/a.html", "signed", 2000000000)


class FailingGateway(FakeGateway):
    def callback(self, code, state, *, browser_id):
        raise LoginError("not_kbot_allowed")


class UnicodeReturnGateway(FakeGateway):
    def callback(self, code, state, *, browser_id):
        return LoginResult("u1", "同事", "/wiki/中文页面.html", "signed", 2000000000)


class AppTests(unittest.TestCase):
    def test_dingtalk_client_requires_application_agent_id(self):
        client = make_dingtalk_client({
            "DINGTALK_CLIENT_ID": "client-id",
            "DINGTALK_CLIENT_SECRET": "client-secret",
            "DINGTALK_AGENT_ID": "123",
        })
        self.assertEqual(client.agent_id, "123")

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name)
        (root/"wiki").mkdir(); (root/"wiki/a.html").write_text("OK",encoding="utf-8")
        (root/"wiki/中文页面.html").write_text("中文OK",encoding="utf-8")
        self.secret=b"0123456789abcdef0123456789abcdef"
        self.token=sign_claim({"user_id":"u1","display_name":"张三","kbot_allowed":True,"exp":2000000000},self.secret)
        self.app=KnowledgeApp(root,FakeGateway(),self.secret,clock=lambda:1900000000)
    def tearDown(self): self.tmp.cleanup()
    def call(self,path,query="",cookie=""):
        status=[];headers=[]
        env={"REQUEST_METHOD":"GET","PATH_INFO":path,"QUERY_STRING":query,"HTTP_COOKIE":cookie,"wsgi.input":io.BytesIO()}
        body=b"".join(self.app(env,lambda s,h:(status.append(s),headers.extend(h))))
        self.last_headers = headers
        return status[0],dict(headers),body
    def test_oauth_state_is_bound_to_initiating_browser_cookie(self):
        status, headers, _ = self.call("/wiki/a.html")
        self.assertEqual(status, "302 Found")
        binding_cookie = headers["Set-Cookie"]
        browser_id = binding_cookie.split("dek_oauth_browser=", 1)[1].split(";", 1)[0]
        self.assertEqual(self.app.gateway.login_browser_id, browser_id)

        self.call("/auth/callback", urlencode({"code": "c", "state": "s"}), "dek_oauth_browser=" + browser_id)
        self.assertEqual(self.app.gateway.callback_browser_id, browser_id)

    def test_unauthenticated_static_request_redirects_to_login(self):
        status,headers,_=self.call("/wiki/a.html")
        self.assertEqual(status,"302 Found");self.assertTrue(headers["Location"].startswith("https://login.example/"))
    def test_callback_sets_secure_http_only_cookie(self):
        status,headers,_=self.call("/auth/callback",urlencode({"code":"c","state":"s"}))
        self.assertEqual(status,"302 Found");self.assertIn("HttpOnly",headers["Set-Cookie"]);self.assertIn("Secure",headers["Set-Cookie"]);self.assertIn("SameSite=Lax",headers["Set-Cookie"])

    def test_callback_percent_encodes_unicode_return_path_for_wsgi_header(self):
        self.app = KnowledgeApp(self.app.root, UnicodeReturnGateway(), self.secret, clock=lambda: 1900000000)  # type: ignore[arg-type]

        status, headers, _ = self.call("/auth/callback", urlencode({"code": "c", "state": "s"}))

        self.assertEqual(status, "302 Found")
        self.assertEqual(headers["Location"], "/wiki/%E4%B8%AD%E6%96%87%E9%A1%B5%E9%9D%A2.html")
        headers["Location"].encode("latin-1")
    def test_callback_clears_browser_binding_cookie(self):
        self.call("/auth/callback", urlencode({"code": "c", "state": "s"}), "dek_oauth_browser=browser-a")

        cookies = [value for name, value in self.last_headers if name == "Set-Cookie"]
        self.assertTrue(any(value.startswith("dek_oauth_browser=") and "Max-Age=0" in value for value in cookies))

    def test_callback_logs_only_safe_failure_stage(self):
        self.app = KnowledgeApp(self.app.root, FailingGateway(), self.secret, clock=lambda: 1900000000)  # type: ignore[arg-type]

        with self.assertLogs("web.auth", level="WARNING") as captured:
            status, _, body = self.call(
                "/auth/callback",
                urlencode({"code": "secret-code", "state": "secret-state"}),
            )

        self.assertEqual((status, body), ("403 Forbidden", b"Forbidden"))
        self.assertEqual(captured.output, ["WARNING:web.auth:dingtalk_callback_failed reason=not_kbot_allowed"])
        self.assertNotIn("secret-code", captured.output[0])
        self.assertNotIn("secret-state", captured.output[0])

    def test_valid_cookie_serves_only_candidate_root(self):
        status,_,body=self.call("/wiki/a.html",cookie="dek_session="+self.token)
        self.assertEqual((status,body),("200 OK",b"OK"))
        self.assertEqual(self.call("/../etc/passwd",cookie="dek_session="+self.token)[0],"404 Not Found")

    def test_valid_cookie_serves_percent_encoded_chinese_path(self):
        encoded_path = "/wiki/" + quote("中文页面.html")

        status, _, body = self.call(encoded_path, cookie="dek_session=" + self.token)

        self.assertEqual((status, body), ("200 OK", "中文OK".encode()))

    def test_valid_cookie_serves_wsgi_latin1_decoded_chinese_path(self):
        encoded_path = "/wiki/" + quote("中文页面.html")
        wsgi_path = unquote(encoded_path, encoding="latin-1")

        status, _, body = self.call(wsgi_path, cookie="dek_session=" + self.token)

        self.assertEqual((status, body), ("200 OK", "中文OK".encode()))

    def test_authenticated_user_endpoint_returns_display_name_without_user_id(self):
        status, headers, body = self.call("/auth/me", cookie="dek_session=" + self.token)

        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), {"display_name": "张三"})

    def test_logout_clears_session_and_returns_to_homepage(self):
        status, headers, _ = self.call("/auth/logout", cookie="dek_session=" + self.token)

        self.assertEqual(status, "302 Found")
        self.assertEqual(headers["Location"], "/auth/signed-out")
        self.assertIn("dek_session=", headers["Set-Cookie"])
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_signed_out_page_is_public_and_requires_explicit_relogin(self):
        status, headers, body = self.call("/auth/signed-out")

        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Cache-Control"], "no-store")
        page = body.decode()
        self.assertIn("已安全退出", page)
        self.assertIn('href="/"', page)
        self.assertIn("重新登录", page)

    def test_executable_static_assets_are_not_cached_after_logout(self):
        (self.app.root / "assets").mkdir()
        (self.app.root / "assets" / "app.js").write_text("ok", encoding="utf-8")

        status, headers, _ = self.call("/assets/app.js", cookie="dek_session=" + self.token)

        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Cache-Control"], "no-store")

if __name__=="__main__": unittest.main()
