import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

from web.app import KnowledgeApp, make_dingtalk_client
from web.auth import sign_claim
from web.chat import ChatHistoryStore
from web.dingtalk_gateway import LoginError, LoginResult


class FakeChat:
    def __init__(self): self.calls = []
    def ask(self, user_id, message):
        self.calls.append((user_id, message))
        return "依据已审核知识库：测试回答"


class FakeDocuments:
    def __init__(self): self.added=[]; self.removed=[]
    def list(self): return [{"id":"a"*24,"name":"资料.txt","size":12,"created_at":123}]
    def add(self,user_id,filename,data):
        self.added.append((user_id,filename,data)); return {"id":"b"*24,"name":filename,"size":len(data),"created_at":124}
    def remove(self,document_id): self.removed.append(document_id)
    def context(self,query): return "共享资源：资料.txt\n场地变更"
    def download(self,document_id): return {"name":"资料.txt","mime":"text/plain"},b"document body"


class FakeGateway:
    def __init__(self): self.login_browser_id = None; self.callback_browser_id = None; self.redirect_uri = "https://regkb.example/auth/callback"
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
        self.history=ChatHistoryStore(root/"chat-history"/"history.sqlite3",self.secret,clock=lambda:1900000000)
        self.app=KnowledgeApp(root,FakeGateway(),self.secret,clock=lambda:1900000000,chat_history=self.history)
    def tearDown(self): self.tmp.cleanup()
    def call(self,path,query="",cookie="",method="GET",body=b"",content_type="application/x-www-form-urlencoded",origin="",extra=None):
        status=[];headers=[]
        env={"REQUEST_METHOD":method,"PATH_INFO":path,"QUERY_STRING":query,"HTTP_COOKIE":cookie,"HTTP_ORIGIN":origin,"wsgi.input":io.BytesIO(body),"CONTENT_LENGTH":str(len(body)),"CONTENT_TYPE":content_type}
        env.update(extra or {})
        body=b"".join(self.app(env,lambda s,h:(status.append(s),headers.extend(h))))
        self.last_headers = headers
        return status[0],dict(headers),body

    def test_generation_proof_identifies_pinned_immutable_release(self):
        (self.app.root / "index.html").write_text("generation one", encoding="utf-8")
        self.app.generation_proof_secret = "proof-secret"
        self.app.release_sha256 = "f" * 64
        expected = hashlib.sha256(b"generation one").hexdigest()

        self.assertEqual(self.call("/__dek_generation")[0], "404 Not Found")
        status = []; headers = []
        environ = {
            "REQUEST_METHOD": "GET", "PATH_INFO": "/__dek_generation", "QUERY_STRING": "",
            "HTTP_COOKIE": "", "HTTP_X_DEK_GENERATION_PROOF": "proof-secret",
            "wsgi.input": io.BytesIO(b""), "CONTENT_LENGTH": "0",
        }
        body = b"".join(self.app(environ, lambda value, found: (status.append(value), headers.extend(found))))
        proof = json.loads(body)
        self.assertEqual(status, ["200 OK"])
        self.assertEqual(proof["web_sha256"], expected)
        self.assertEqual(Path(proof["release"]).resolve(), self.app.root.resolve().parent)
        self.assertEqual(proof["pid"], os.getpid())
        self.assertEqual(proof["release_sha256"], "f" * 64)
    def test_ready_endpoint_reports_the_configured_oauth_callback_unauthenticated(self):
        """deploy/readiness.py's automation-readiness gate GETs this over a
        pinned connection and requires exactly {"status":"ready",
        "oauth_callback": <configured value>} with no redirect -- this
        endpoint never existed on the public site (only web/review_app.py's
        differently-shaped /__ready did), so the very first real readiness
        check against the live site 302'd to DingTalk login instead."""
        status, headers, body = self.call("/__ready")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body), {"status": "ready", "oauth_callback": self.app.gateway.redirect_uri})
        self.assertEqual(headers["Content-Type"], "application/json")
    def test_ready_endpoint_is_get_only(self):
        status, headers, _ = self.call("/__ready", method="POST")
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertEqual(headers["Allow"], "GET")
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

        self.assertEqual(status, "403 Forbidden")
        self.assertIn("当前账号没有访问权限", body.decode("utf-8"))
        self.assertEqual(captured.output, ["WARNING:web.auth:dingtalk_callback_failed reason=not_kbot_allowed"])
        self.assertNotIn("secret-code", captured.output[0])
        self.assertNotIn("secret-state", captured.output[0])

    def test_valid_cookie_serves_only_candidate_root(self):
        status,_,body=self.call("/wiki/a.html",cookie="dek_session="+self.token)
        self.assertEqual((status,body),("200 OK",b"OK"))
        self.assertEqual(self.call("/../etc/passwd",cookie="dek_session="+self.token)[0],"404 Not Found")

    def test_frontend_assets_come_from_installed_code_not_the_release(self):
        # CSS/JS are code, not reviewed content: a deploy + restart must be
        # enough to change them, without waiting on a content publish.
        (self.app.root / "assets").mkdir()
        for name in ("style.css", "app.js", "search.js", "page.js"):
            (self.app.root / "assets" / name).write_text("STALE RELEASE COPY", encoding="utf-8")
        (self.app.root / "assets" / "search-index.json").write_text("{}", encoding="utf-8")
        code_dir = Path(__file__).resolve().parents[1] / "assets"
        cookie = "dek_session=" + self.token
        for name, mime in (("style.css", "text/css"), ("app.js", "text/javascript"), ("search.js", "text/javascript"), ("page.js", "text/javascript")):
            with self.subTest(asset=name):
                status, headers, body = self.call("/assets/" + name, cookie=cookie)
                self.assertEqual(status, "200 OK")
                self.assertEqual(body, (code_dir / name).read_bytes())
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        # Everything else under /assets/ is still release content.
        status, _, body = self.call("/assets/search-index.json", cookie=cookie)
        self.assertEqual((status, body), ("200 OK", b"{}"))

    def test_the_company_logo_is_served_from_installed_code_as_a_png(self):
        (self.app.root / "assets").mkdir()
        (self.app.root / "assets" / "logo.png").write_bytes(b"RELEASE COPY")
        code = Path(__file__).resolve().parents[1] / "assets" / "logo.png"
        self.assertTrue(code.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        status, headers, body = self.call("/assets/logo.png", cookie="dek_session=" + self.token)
        self.assertEqual(status, "200 OK")
        self.assertEqual(body, code.read_bytes())
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.call("/assets/logo.png")[0], "302 Found")   # still behind login

    def test_frontend_assets_still_require_login(self):
        status, headers, _ = self.call("/assets/style.css")
        self.assertEqual(status, "302 Found")
        self.assertNotIn("Content-Type", headers)

    def test_site_release_is_pinned_until_process_restart(self):
        base = Path(self.tmp.name)
        first = base / "release-first"; second = base / "release-second"
        (first / "wiki").mkdir(parents=True); (second / "wiki").mkdir(parents=True)
        (first / "wiki/a.html").write_text("first", encoding="utf-8")
        (second / "wiki/a.html").write_text("second", encoding="utf-8")
        current = base / "current"; current.symlink_to(first)
        self.app = KnowledgeApp(current, FakeGateway(), self.secret, clock=lambda: 1900000000)
        self.assertEqual(self.call("/wiki/a.html", cookie="dek_session=" + self.token)[2], b"first")
        replacement = base / ".current-new"; replacement.symlink_to(second); replacement.replace(current)
        self.assertEqual(self.call("/wiki/a.html", cookie="dek_session=" + self.token)[2], b"first")

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

    def test_shared_resources_page_is_authenticated_get_only_and_uses_the_common_shell(self):
        self.assertEqual(self.call("/shared-resources")[0], "302 Found")
        cookie = "dek_session=" + self.token
        status, headers, body = self.call("/shared-resources", cookie=cookie)
        markup = body.decode("utf-8")
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn('<title>共享资源 · DEK</title>', markup)
        self.assertIn('"path":"shared-resources"', markup)
        self.assertIn('id="shared-resource-app"', markup)
        self.assertIn("自动参与 Kbot AI 问答", markup)
        self.assertIn('/assets/page.js', markup)
        status, headers, _ = self.call("/shared-resources", cookie=cookie, method="POST")
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertEqual(headers["Allow"], "GET")

    def test_authenticated_same_origin_chat_is_proxied_without_exposing_user_details(self):
        chat = FakeChat(); self.app.chat_client = chat
        payload = json.dumps({"message": "生产场地变更需要什么资料？"}, ensure_ascii=False).encode()
        status, headers, body = self.call("/api/chat", cookie="dek_session=" + self.token, method="POST",
            body=payload, content_type="application/json", origin="https://regkb.example")
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(json.loads(body), {"answer": "依据已审核知识库：测试回答"})
        self.assertEqual(chat.calls, [("u1", "生产场地变更需要什么资料？")])

        status, headers, body = self.call("/api/chat/history", cookie="dek_session=" + self.token)
        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertEqual(
            [(item["role"], item["content"]) for item in json.loads(body)["messages"]],
            [("user", "生产场地变更需要什么资料？"), ("assistant", "依据已审核知识库：测试回答")],
        )

    def test_chat_history_survives_a_new_app_instance_and_is_isolated_by_login(self):
        self.history.append_exchange("u1", "跨电脑问题", "跨电脑回答")
        recreated = KnowledgeApp(self.app.root, FakeGateway(), self.secret, clock=lambda: 1900000000,
                                 chat_history=ChatHistoryStore(self.history.path, self.secret))
        original = self.app; self.app = recreated
        try:
            status, _, body = self.call("/api/chat/history", cookie="dek_session=" + self.token)
            self.assertEqual(status, "200 OK")
            self.assertEqual(json.loads(body)["messages"][0]["content"], "跨电脑问题")
            other = sign_claim({"user_id":"u2","display_name":"李四","kbot_allowed":True,"exp":2000000000},self.secret)
            self.assertEqual(json.loads(self.call("/api/chat/history", cookie="dek_session=" + other)[2])["messages"], [])
        finally:
            self.app = original

    def test_chat_history_requires_login_and_get(self):
        self.assertEqual(self.call("/api/chat/history")[0], "302 Found")
        status, headers, _ = self.call("/api/chat/history", cookie="dek_session=" + self.token, method="POST")
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertEqual(headers["Allow"], "GET")

    def test_chat_requires_login_same_origin_json_and_bounded_message(self):
        self.app.chat_client = FakeChat()
        good = json.dumps({"message": "问题"}).encode()
        self.assertEqual(self.call("/api/chat", method="POST", body=good, content_type="application/json", origin="https://regkb.example")[0], "302 Found")
        cookie = "dek_session=" + self.token
        self.assertEqual(self.call("/api/chat", cookie=cookie, method="POST", body=good, content_type="application/json", origin="https://evil.example")[0], "403 Forbidden")
        self.assertEqual(self.call("/api/chat", cookie=cookie, method="POST", body=good, origin="https://regkb.example")[0], "400 Bad Request")
        large = json.dumps({"message": "问" * 2001}, ensure_ascii=False).encode()
        self.assertEqual(self.call("/api/chat", cookie=cookie, method="POST", body=large, content_type="application/json", origin="https://regkb.example")[0], "400 Bad Request")

    def test_shared_document_context_is_automatically_injected(self):
        chat=FakeChat(); self.app.chat_client=chat; self.app.document_store=FakeDocuments()
        payload=json.dumps({"message":"总结资料"},ensure_ascii=False).encode()
        status,_,_=self.call("/api/chat",cookie="dek_session="+self.token,method="POST",body=payload,
            content_type="application/json",origin="https://regkb.example")
        self.assertEqual(status,"200 OK")
        self.assertEqual(chat.calls[0][0],"u1")
        self.assertIn("<DEK_SHARED_DOCUMENT_CONTEXT>",chat.calls[0][1])
        self.assertIn("共享资源：资料.txt",chat.calls[0][1])

    def test_internal_shared_context_is_loopback_bearer_authenticated(self):
        self.app.document_store=FakeDocuments(); self.app.internal_api_key="internal-secret"
        payload=json.dumps({"query":"场地变更"},ensure_ascii=False).encode()
        common={"method":"POST","body":payload,"content_type":"application/json"}
        self.assertEqual(self.call("/internal/shared-context",extra={"REMOTE_ADDR":"203.0.113.8","HTTP_AUTHORIZATION":"Bearer internal-secret"},**common)[0],"404 Not Found")
        self.assertEqual(self.call("/internal/shared-context",extra={"REMOTE_ADDR":"127.0.0.1","HTTP_AUTHORIZATION":"Bearer wrong"},**common)[0],"404 Not Found")
        status,headers,body=self.call("/internal/shared-context",extra={"REMOTE_ADDR":"127.0.0.1","HTTP_AUTHORIZATION":"Bearer internal-secret"},**common)
        self.assertEqual(status,"200 OK")
        self.assertEqual(headers["Cache-Control"],"no-store")
        self.assertIn("共享资源：资料.txt",json.loads(body)["context"])

    def test_internal_shared_context_rejects_invalid_requests(self):
        self.app.document_store=FakeDocuments(); self.app.internal_api_key="internal-secret"
        auth={"REMOTE_ADDR":"::1","HTTP_AUTHORIZATION":"Bearer internal-secret"}
        self.assertEqual(self.call("/internal/shared-context",extra=auth)[0],"405 Method Not Allowed")
        payload=json.dumps({"query":"问题","extra":True},ensure_ascii=False).encode()
        self.assertEqual(self.call("/internal/shared-context",method="POST",body=payload,content_type="application/json",extra=auth)[0],"400 Bad Request")

    def test_document_listing_reports_feature_state_without_leaking_other_users(self):
        cookie="dek_session="+self.token
        status,_,body=self.call("/api/documents",cookie=cookie)
        self.assertEqual(status,"200 OK")
        self.assertEqual(json.loads(body),{"enabled":False,"can_upload":False,"documents":[]})
        self.app.document_store=FakeDocuments()
        status,_,body=self.call("/api/documents",cookie=cookie)
        self.assertEqual(status,"200 OK")
        self.assertTrue(json.loads(body)["enabled"])

    def test_document_upload_and_delete_require_same_origin_and_user_session(self):
        documents=FakeDocuments(); self.app.document_store=documents; self.app.document_uploaders=frozenset({"u1"}); cookie="dek_session="+self.token
        self.assertEqual(self.call("/api/documents",cookie=cookie,method="POST",body=b"text",origin="https://evil.example",
            extra={"HTTP_X_DEK_FILENAME":"a.txt"})[0],"403 Forbidden")
        status,_,body=self.call("/api/documents",cookie=cookie,method="POST",body=b"text",origin="https://regkb.example",
            content_type="text/plain",extra={"HTTP_X_DEK_FILENAME":"%E8%B5%84%E6%96%99.txt"})
        self.assertEqual(status,"201 Created")
        self.assertEqual(documents.added,[("u1","资料.txt",b"text")])
        document_id=json.loads(body)["id"]
        status,_,_=self.call("/api/documents/"+document_id,cookie=cookie,method="DELETE",origin="https://regkb.example")
        self.assertEqual(status,"204 No Content")
        self.assertEqual(documents.removed,[document_id])

    def test_all_users_can_list_and_download_shared_documents_but_only_reviewers_upload(self):
        documents=FakeDocuments(); self.app.document_store=documents; cookie="dek_session="+self.token
        status,_,body=self.call("/api/documents",cookie=cookie)
        self.assertEqual(status,"200 OK")
        self.assertEqual(json.loads(body)["documents"][0]["name"],"资料.txt")
        self.assertFalse(json.loads(body)["can_upload"])
        self.assertEqual(self.call("/api/documents",cookie=cookie,method="POST",body=b"text",origin="https://regkb.example",
            extra={"HTTP_X_DEK_FILENAME":"a.txt"})[0],"403 Forbidden")
        status,headers,body=self.call("/api/documents/"+"a"*24+"/download",cookie=cookie)
        self.assertEqual((status,body),("200 OK",b"document body"))
        self.assertEqual(headers["Content-Type"],"text/plain")
        self.assertIn("inline",headers["Content-Disposition"])

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

    def test_reviewer_routes_are_not_owned_by_knowledge_app(self):
        for path in ("/review/", "/review/decision", "/decision"):
            with self.subTest(path=path):
                status, _, _ = self.call(path, cookie="dek_session=" + self.token, method="POST")
                self.assertEqual(status, "404 Not Found")

    def test_bounce_sends_an_unauthenticated_visitor_through_login_with_next_as_return_path(self):
        status, headers, _ = self.call("/auth/bounce", query=urlencode({"next": "/review/item/abc"}))
        self.assertEqual(status, "302 Found")
        self.assertEqual(headers["Location"], "https://login.example/?return=/review/item/abc")
        self.assertIn("dek_oauth_browser=", headers["Set-Cookie"])

    def test_bounce_rejects_an_external_next_and_falls_back_to_root(self):
        for next_value in ("//evil.example/", "https://evil.example/", "not-a-path", ""):
            with self.subTest(next=next_value):
                status, headers, _ = self.call("/auth/bounce", query=urlencode({"next": next_value}))
                self.assertEqual(status, "302 Found")
                self.assertEqual(headers["Location"], "https://login.example/?return=/")

    def test_bounce_sends_an_already_authenticated_visitor_straight_to_next_without_a_dingtalk_round_trip(self):
        status, headers, _ = self.call(
            "/auth/bounce", query=urlencode({"next": "/review/"}), cookie="dek_session=" + self.token,
        )
        self.assertEqual(status, "302 Found")
        self.assertEqual(headers["Location"], "/review/")
        self.assertIsNone(self.app.gateway.login_browser_id)

    def test_bounce_is_get_only(self):
        status, headers, _ = self.call("/auth/bounce", method="POST")
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertEqual(headers["Allow"], "GET")

if __name__=="__main__": unittest.main()
