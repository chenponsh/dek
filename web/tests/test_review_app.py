import io
import json
import re
import socket
import tempfile
import subprocess
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from web.auth import sign_claim
from web.review import MemoryFormNonceStore, ReviewerLabelStore, ReviewError, ReviewService, rough_binding
from web.review_app import ReviewApp, UnixWSGIRequestHandler, UnixWSGIServer
from web.tests.test_review import CANDIDATE, ROUGH


REVIEW_ORIGIN = "https://regkb.chenponai.com"
REVIEW_PREFIX = "/review"
CLAIM_SECRET = b"claim-secret-0123456789abcdef"


class UnixWSGIServerTests(unittest.TestCase):
    def test_unix_socket_request_returns_wsgi_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "review.sock")
            server = UnixWSGIServer(path, UnixWSGIRequestHandler)
            server.set_app(lambda environ, start: (start("200 OK", [("Content-Type", "text/plain")]), [b"ready"])[1])
            worker = threading.Thread(target=server.handle_request)
            worker.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(path)
                    client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                    response = b""
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        response += chunk
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertIn(b"200 OK", response)
                self.assertTrue(response.endswith(b"ready"))
            finally:
                server.server_close()


class ReviewAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "repo/ingestion/rough").mkdir(parents=True)
        self.rough = self.root / "repo/ingestion/rough/pending.md"
        self.rough.write_text(ROUGH, encoding="utf-8")
        subprocess.run(["git","init","-q","-b","main"],cwd=self.root/"repo",check=True)
        subprocess.run(["git","add","."],cwd=self.root/"repo",check=True)
        subprocess.run(["git","-c","user.name=t","-c","user.email=t@invalid","commit","-qm","init"],cwd=self.root/"repo",check=True)
        self.clock_value = 1_900_000_000
        self.clock = lambda: self.clock_value
        self.nonces = MemoryFormNonceStore(clock=self.clock)
        self.service = ReviewService(
            self.root / "repo", self.root / "state/decisions.jsonl",
            audit_key=b"audit-key-0123456789abcdef",
            queue_key=b"queue-key-0123456789abcdef",
            nonces=self.nonces,
            clock=self.clock,
            labels=ReviewerLabelStore(self.root / "state/reviewer-labels.jsonl"),
            path_prefix=REVIEW_PREFIX,
        )
        self.app = ReviewApp(
            self.service, CLAIM_SECRET, ("reviewer-1",),
            expected_origin=REVIEW_ORIGIN, clock=self.clock,
        )

    def tearDown(self):
        self.temp.cleanup()

    def call(self, path="/", *, query="", cookie="", method="GET", form=None, origin=None, extra=None):
        payload = urlencode(form or {}).encode()
        status, headers = [], []
        environ = {
            "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
            "HTTP_COOKIE": cookie, "CONTENT_TYPE": "application/x-www-form-urlencoded",
            "CONTENT_LENGTH": str(len(payload)), "wsgi.input": io.BytesIO(payload),
        }
        if origin is not None:
            environ["HTTP_ORIGIN"] = origin
        environ.update(extra or {})
        body = b"".join(self.app(environ, lambda s, h: (status.append(s), headers.extend(h))))
        return status[0], headers, body

    @staticmethod
    def cookie_values(headers):
        return [value for name, value in headers if name.lower() == "set-cookie"]

    def claim(self, user_id="reviewer-1", *, exp=None, kbot_allowed=True, display_name="审阅人"):
        payload = {
            "user_id": user_id, "display_name": display_name,
            "kbot_allowed": kbot_allowed, "exp": exp if exp is not None else self.clock_value + 28800,
        }
        return sign_claim(payload, CLAIM_SECRET)

    def authenticate(self, user_id="reviewer-1", **kwargs):
        return "dek_session=" + self.claim(user_id, **kwargs)

    def test_unauthenticated_request_redirects_to_the_knowledge_base_login_bounce(self):
        status, headers, _ = self.call("/")
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], REVIEW_ORIGIN + "/auth/bounce?next=" + urlencode({"next": REVIEW_PREFIX + "/"})[len("next="):])

    def test_authenticated_non_reviewer_is_forbidden_not_redirected_again(self):
        # An outside DingTalk-authenticated user still has a valid dek_session,
        # so bouncing them back through login would loop forever. They must see
        # a terminal 403 instead.
        session = self.authenticate("someone-else")
        with self.assertLogs("web.review.auth", level="WARNING") as captured:
            self.assertEqual(self.call("/", cookie=session)[0], "403 Forbidden")
        self.assertIn("review_forbidden_not_a_reviewer user_id=someone-else", " ".join(captured.output))
        self.assertEqual(self.call("/decision", method="POST", cookie=session, origin=REVIEW_ORIGIN)[0], "403 Forbidden")

    def test_expired_claim_is_rejected(self):
        session = "dek_session=" + self.claim(exp=self.clock_value - 1)
        self.assertEqual(self.call("/", cookie=session)[0], "302 Found")

    def test_claim_without_kbot_allowed_is_rejected(self):
        session = "dek_session=" + self.claim(kbot_allowed=False)
        self.assertEqual(self.call("/", cookie=session)[0], "302 Found")

    def test_malformed_or_unsigned_session_cookie_is_rejected(self):
        status, _, _ = self.call(
            "/", cookie="dek_session=not-a-real-claim",
            extra={"HTTP_X_DEK_USER": "reviewer-1", "HTTP_X_FORWARDED_USER": "reviewer-1"},
        )
        self.assertEqual(status, "302 Found")

    def test_no_reviewers_configured_is_forbidden_even_when_authenticated(self):
        self.app = ReviewApp(self.service, CLAIM_SECRET, (), expected_origin=REVIEW_ORIGIN, clock=self.clock)
        self.assertEqual(self.call("/", cookie=self.authenticate())[0], "403 Forbidden")

    def test_logout_redirects_to_the_knowledge_base_logout(self):
        # Logout is owned by the knowledge base: one dek_session cookie covers
        # both origins, so review has nothing local left to revoke.
        status, headers, _ = self.call("/auth/logout", method="GET", cookie=self.authenticate())
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], REVIEW_ORIGIN + "/auth/logout")
        self.assertEqual(self.cookie_values(headers), [])

        status, headers, _ = self.call("/auth/logout", method="POST", cookie=self.authenticate())
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], REVIEW_ORIGIN + "/auth/logout")

        self.assertEqual(self.call("/auth/logout", method="DELETE")[0], "405 Method Not Allowed")

    def test_readiness_is_get_only(self):
        self.assertEqual(self.call("/__ready", method="GET")[0], "200 OK")
        status, headers, _ = self.call("/__ready", method="POST")
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertIn(("Allow", "GET"), headers)

    def test_readiness_reports_login_bounce_and_fails_with_empty_reviewers(self):
        status, _, body = self.call("/__ready")
        self.assertEqual(status, "200 OK")
        self.assertEqual(__import__("json").loads(body), {"status": "ready", "login_bounce": REVIEW_ORIGIN + "/auth/bounce"})
        self.app = ReviewApp(self.service, CLAIM_SECRET, (), expected_origin=REVIEW_ORIGIN, clock=self.clock)
        self.assertEqual(self.call("/__ready")[0], "503 Service Unavailable")

    def test_render_failure_returns_controlled_status_instead_of_server_error(self):
        # An unhandled ReviewError from the review repository reached the WSGI
        # server and produced a bare 500 "server error" page. The reviewer must
        # instead see a controlled status and the log must carry a safe reason.
        session = self.authenticate()

        def broken(session_id, **kwargs):
            raise ReviewError("review clone failed", "503 Service Unavailable")

        self.service.render_list = broken
        with self.assertLogs("web.review.auth", level="WARNING") as captured:
            status, _, body = self.call("/", cookie=session)
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(body, b"Service Unavailable")
        self.assertTrue(any("review_render_failed" in line for line in captured.output))
        self.assertNotIn("clone failed", " ".join(captured.output))

    def test_review_pages_let_the_browser_load_the_company_logo_from_the_same_site_only(self):
        # Without img-src the default-src 'none' policy silently blocks the header logo.
        _, headers, body = self.call("/", cookie=self.authenticate())
        policy = dict(headers)["Content-Security-Policy"]
        self.assertIn('src="/assets/logo.png"', body.decode("utf-8"))
        self.assertIn("img-src 'self';", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertNotIn("data:", policy)

    def test_work_queue_lists_items_and_links_to_a_detail_page(self):
        session = self.authenticate()
        status, _, body = self.call("/", cookie=session)
        self.assertEqual(status, "200 OK")
        page = body.decode("utf-8")
        self.assertIn("<th>内容</th>", page)
        self.assertIn("待审核", page)
        self.assertNotIn("name=\"q\"", page)
        identity = re.search(r'/review/item/([0-9a-f]{16})', page).group(1)

        # Nginx owns the public /review/ prefix and strips it before proxying to
        # the isolated WSGI app. Generated links stay public prefixed URLs while
        # direct app tests exercise the corresponding internal route.
        status, _, detail = self.call("/item/" + identity, cookie=session)
        self.assertEqual(status, "200 OK")
        self.assertIn('<button type="submit" name="action" value="approve" data-busy-label="提交中…">批准</button>', detail.decode("utf-8"))

        status, headers, _ = self.call("/item/" + identity)
        self.assertEqual(status, "302 Found")
        self.assertEqual(dict(headers)["Location"], REVIEW_ORIGIN + "/auth/bounce?next=" + urlencode({"next": REVIEW_PREFIX + "/item/" + identity})[len("next="):])
        self.assertEqual(self.call("/item/deadbeefdeadbeef", cookie=session)[0], "404 Not Found")
        self.assertEqual(self.call("/item/not-an-identity", cookie=session)[0], "404 Not Found")
        self.assertEqual(self.call("/item/" + identity, cookie=session, method="POST")[0], "405 Method Not Allowed")

    def test_decision_redirects_to_the_item_and_shows_the_reviewer_nickname(self):
        session = self.authenticate()
        _, _, body = self.call("/", cookie=session)
        identity = re.search(r'/review/item/([0-9a-f]{16})', body.decode("utf-8")).group(1)
        _, _, detail = self.call("/item/" + identity, cookie=session)
        nonce = re.search('name="form_nonce" value="([^"]+)"', detail.decode("utf-8")).group(1)
        binding = rough_binding(self.rough)
        status, headers, _ = self.call(
            "/decision", method="POST", cookie=session, origin=REVIEW_ORIGIN,
            form={
                "form_nonce": nonce,
                "rough_path": "ingestion/rough/pending.md",
                "rough_sha256": binding.sha256,
                "rough_version": binding.version,
                "action": "reject",
                "wiki_path": "",
                "candidate_markdown": "",
            },
        )
        self.assertEqual(status, "303 See Other")
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/item/" + identity + "?notice=rejected")

        status, _, after = self.call("/item/" + identity, query="notice=rejected", cookie=session)
        self.assertEqual(status, "200 OK")
        page = after.decode("utf-8")
        self.assertIn("已拒绝", page)
        self.assertIn("审阅人", page)

    def test_list_position_query_is_validated_and_reaches_the_item_page_and_decision_redirect(self):
        session = self.authenticate()
        # Unknown or malformed list state is ignored, never an error.
        for query in ("page=abc", "page=-3", "status=returned", "status=%3Cscript%3E&page=999"):
            with self.subTest(query=query):
                self.assertEqual(self.call("/", query=query, cookie=session)[0], "200 OK")
        _, _, body = self.call("/", cookie=session)
        identity = re.search(r'/review/item/([0-9a-f]{16})', body.decode("utf-8")).group(1)
        _, _, detail = self.call("/item/" + identity, query="status=pending&page=1&x=1", cookie=session)
        page = detail.decode("utf-8")
        self.assertIn('href="/review/?status=pending">← 返回列表', page)
        self.assertIn('action="/review/decision?status=pending"', page)
        nonce = re.search('name="form_nonce" value="([^"]+)"', page).group(1)
        binding = rough_binding(self.rough)
        form = {"form_nonce": nonce, "rough_path": "ingestion/rough/pending.md", "rough_sha256": binding.sha256,
                "rough_version": binding.version, "action": "reject", "wiki_path": "", "candidate_markdown": ""}
        status, headers, _ = self.call("/decision", method="POST", cookie=session, origin=REVIEW_ORIGIN, form=form,
                                       query="status=pending&page=2&status=%3Cx%3E")
        self.assertEqual(status, "303 See Other")
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/item/" + identity + "?notice=rejected&status=pending&page=2")

    def test_page_size_query_is_bounded_and_reaches_the_redirect(self):
        session = self.authenticate()
        for query in ("page_size=abc", "page_size=0", "page_size=99999999999", "page=2&page_size=5"):
            with self.subTest(query=query):
                self.assertEqual(self.call("/", query=query, cookie=session)[0], "200 OK")
        _, _, body = self.call("/", cookie=session)
        identity = re.search(r'/review/item/([0-9a-f]{16})', body.decode("utf-8")).group(1)
        _, _, detail = self.call("/item/" + identity, query="page_size=5", cookie=session)
        self.assertIn('href="/review/?page_size=5">← 返回列表', detail.decode("utf-8"))
        nonce = re.search('name="form_nonce" value="([^"]+)"', detail.decode("utf-8")).group(1)
        binding = rough_binding(self.rough)
        form = {"form_nonce": nonce, "rough_path": "ingestion/rough/pending.md", "rough_sha256": binding.sha256,
                "rough_version": binding.version, "action": "reject", "wiki_path": "", "candidate_markdown": ""}
        _, headers, _ = self.call("/decision", method="POST", cookie=session, origin=REVIEW_ORIGIN, form=form, query="page_size=5&page=3")
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/item/" + identity + "?notice=rejected&page=3&page_size=5")

    def test_page_size_values_below_one_or_not_a_number_all_fall_back_to_the_default(self):
        session = self.authenticate()
        expected = {"": "15", "abc": "15", "0": "15", "-5": "15", "-0": "15", "1.5": "15", "99999999": "100",
                    "1": "5", "4": "5", "5": "5", "37": "37", "100": "100", "101": "100", "5000": "100"}
        for raw, want in expected.items():
            with self.subTest(page_size=raw):
                status, _, body = self.call("/", query="page_size=" + raw, cookie=session)
                self.assertEqual(status, "200 OK")
                self.assertIn(f'name="page_size" min="5" max="100" step="1" value="{want}"', body.decode("utf-8"))

    def test_full_walk_reject_without_a_comment_then_change_your_mind_and_approve(self):
        session = self.authenticate()
        _, _, body = self.call("/", cookie=session)
        identity = re.search(r'/review/item/([0-9a-f]{16})', body.decode("utf-8")).group(1)
        binding = rough_binding(self.rough)

        def submit(action, *, edit=False, **extra):
            _, _, detail = self.call("/item/" + identity, query="edit=1" if edit else "", cookie=session)
            page = detail.decode("utf-8")
            self.assertNotIn('name="comment"', page)
            nonce = re.search('name="form_nonce" value="([^"]+)"', page).group(1)
            form = {"form_nonce": nonce, "rough_path": "ingestion/rough/pending.md", "rough_sha256": binding.sha256,
                    "rough_version": binding.version, "action": action, "wiki_path": "", "candidate_markdown": ""}
            form.update(extra)
            return self.call("/decision", method="POST", cookie=session, origin=REVIEW_ORIGIN, form=form)

        # 拒绝 with no comment field at all: no 400, redirected to the item, shown as 已拒绝.
        status, headers, _ = submit("reject")
        self.assertEqual(status, "303 See Other")
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/item/" + identity + "?notice=rejected")
        _, _, after = self.call("/item/" + identity, query="notice=rejected", cookie=session)
        self.assertIn("已拒绝", after.decode("utf-8"))
        self.assertIn("已记录：拒绝。", after.decode("utf-8"))
        # A rejected item is locked but can be re-opened, and 批准 then wins.
        status, headers, _ = submit("approve", edit=True, wiki_path="wiki/01_Test/01-0001.md", candidate_markdown=CANDIDATE)
        self.assertEqual(status, "303 See Other")
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/item/" + identity + "?notice=approved")
        _, _, done = self.call("/item/" + identity, cookie=session)
        self.assertIn("已批准待发布", done.decode("utf-8"))
        records = [json.loads(line) for line in (self.root / "state/decisions.jsonl").read_text().splitlines()]
        self.assertEqual([r["action"] for r in records], ["reject", "approve"])
        self.assertEqual([r["comment"] for r in records], ["", ""])

    def test_decision_redirect_without_list_position_is_unchanged(self):
        session = self.authenticate()
        _, _, body = self.call("/", cookie=session)
        identity = re.search(r'/review/item/([0-9a-f]{16})', body.decode("utf-8")).group(1)
        _, _, detail = self.call("/item/" + identity, cookie=session)
        nonce = re.search('name="form_nonce" value="([^"]+)"', detail.decode("utf-8")).group(1)
        binding = rough_binding(self.rough)
        form = {"form_nonce": nonce, "rough_path": "ingestion/rough/pending.md", "rough_sha256": binding.sha256,
                "rough_version": binding.version, "action": "reject", "wiki_path": "", "candidate_markdown": ""}
        _, headers, _ = self.call("/decision", method="POST", cookie=session, origin=REVIEW_ORIGIN, form=form)
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/item/" + identity + "?notice=rejected")

    def test_post_requires_exact_origin_and_single_use_nonce(self):
        session = self.authenticate()
        _, _, listing = self.call("/", cookie=session)
        identity = re.search(r'/review/item/([0-9a-f]{16})', listing.decode()).group(1)
        _, _, detail = self.call("/item/" + identity, cookie=session)
        nonce = detail.decode().split('name="form_nonce" value="', 1)[1].split('"', 1)[0]
        binding = rough_binding(self.rough)
        form = {
            "form_nonce": nonce, "rough_path": "ingestion/rough/pending.md",
            "rough_sha256": binding.sha256, "rough_version": binding.version,
            "action": "approve", "wiki_path": "wiki/01_Test/01-0001.md",
            "candidate_markdown": CANDIDATE,
        }
        for origin in ("https://review.regkb.chenponai.com", REVIEW_ORIGIN + "/", "http://regkb.chenponai.com", "https://regkb.chenponai.com.evil.test"):
            self.assertEqual(self.call("/decision", method="POST", form=form, cookie=session, origin=origin)[0], "403 Forbidden")
        self.assertEqual(self.call("/decision", method="POST", form=form, cookie=session, origin=REVIEW_ORIGIN)[0], "303 See Other")
        self.assertEqual(self.call("/decision", method="POST", form=form, cookie=session, origin=REVIEW_ORIGIN)[0], "403 Forbidden")
        self.assertEqual(len((self.root / "state/decisions.jsonl").read_text().splitlines()), 1)

    def test_post_without_an_origin_header_is_allowed_for_same_site_browsers(self):
        # Some webviews and browsers omit Origin on a same-origin form POST. The
        # request is still fail-closed: the SameSite=Lax session cookie is not sent
        # on a cross-site POST, and the single-use nonce is bound to that session.
        session = self.authenticate()
        _, _, listing = self.call("/", cookie=session)
        identity = re.search(r'/review/item/([0-9a-f]{16})', listing.decode()).group(1)
        _, _, detail = self.call("/item/" + identity, cookie=session)
        nonce = detail.decode().split('name="form_nonce" value="', 1)[1].split('"', 1)[0]
        binding = rough_binding(self.rough)
        form = {
            "form_nonce": nonce, "rough_path": "ingestion/rough/pending.md",
            "rough_sha256": binding.sha256, "rough_version": binding.version,
            "action": "reject", "wiki_path": "", "candidate_markdown": "",
        }
        with self.assertLogs("web.review.auth", level="WARNING") as captured:
            status, _, _ = self.call("/decision", method="POST", form=form, cookie=session)
        self.assertEqual(status, "303 See Other")
        self.assertTrue(any("origin_absent" in line for line in captured.output))

        # No session cookie at all still fails closed, with or without Origin.
        fresh_nonce = self.service.nonces.issue("unknown-session", "ingestion/rough/pending.md", int(self.clock()) + 900)
        anon = dict(form, form_nonce=fresh_nonce)
        self.assertEqual(self.call("/decision", method="POST", form=anon)[0], "401 Unauthorized")
        self.assertEqual(self.call("/decision", method="POST", form=anon, origin=REVIEW_ORIGIN)[0], "401 Unauthorized")

    def test_origin_mismatch_logs_only_the_sanitized_origin(self):
        session = self.authenticate()
        _, _, listing = self.call("/", cookie=session)
        identity = re.search(r'/review/item/([0-9a-f]{16})', listing.decode()).group(1)
        _, _, detail = self.call("/item/" + identity, cookie=session)
        nonce = detail.decode().split('name="form_nonce" value="', 1)[1].split('"', 1)[0]
        binding = rough_binding(self.rough)
        form = {
            "form_nonce": nonce, "rough_path": "ingestion/rough/pending.md",
            "rough_sha256": binding.sha256, "rough_version": binding.version,
            "action": "reject", "wiki_path": "", "candidate_markdown": "",
        }
        with self.assertLogs("web.review.auth", level="WARNING") as captured:
            status, _, _ = self.call(
                "/decision", method="POST", form=form, cookie=session,
                origin="https://gateway.example.test/secret?token=abc",
            )
        self.assertEqual(status, "403 Forbidden")
        joined = " ".join(captured.output)
        self.assertIn("origin_mismatch", joined)
        self.assertIn("origin=https://gateway.example.test", joined)
        self.assertNotIn("secret", joined)
        self.assertNotIn("token=abc", joined)

        with self.assertLogs("web.review.auth", level="WARNING") as captured:
            self.call("/decision", method="POST", form=form, cookie=session, origin="null")
        self.assertIn("origin_null", " ".join(captured.output))

    def test_null_origin_from_a_same_site_client_is_accepted_and_cross_site_is_not(self):
        # DingTalk's embedded browser submits the decision form with `Origin: null`.
        # The same-site signal then comes from Sec-Fetch-Site; a cross-site or
        # unknown value keeps failing closed (plus the Lax cookie and nonce).
        session = self.authenticate()
        _, _, listing = self.call("/", cookie=session)
        identity = re.search(r'/review/item/([0-9a-f]{16})', listing.decode()).group(1)

        def submission():
            _, _, detail = self.call("/item/" + identity, query="edit=1", cookie=session)
            nonce = detail.decode().split('name="form_nonce" value="', 1)[1].split('"', 1)[0]
            binding = rough_binding(self.rough)
            return {
                "form_nonce": nonce, "rough_path": "ingestion/rough/pending.md",
                "rough_sha256": binding.sha256, "rough_version": binding.version,
                "action": "reject", "wiki_path": "", "candidate_markdown": "",
            }

        with self.assertLogs("web.review.auth", level="WARNING") as captured:
            status, _, _ = self.call(
                "/decision", method="POST", form=submission(), cookie=session,
                origin="null", extra={"HTTP_SEC_FETCH_SITE": "same-origin"},
            )
        self.assertEqual(status, "303 See Other")
        self.assertIn("origin_null", " ".join(captured.output))

        with self.assertLogs("web.review.auth", level="WARNING") as captured:
            status, _, _ = self.call(
                "/decision", method="POST", form=submission(), cookie=session,
                origin="null", extra={"HTTP_SEC_FETCH_SITE": "cross-site"},
            )
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("origin_unusable", " ".join(captured.output))

    def test_state_changing_routes_require_an_exact_form_media_type(self):
        session = self.authenticate()
        _, _, listing = self.call("/", cookie=session)
        identity_match = re.search(r'/review/item/([0-9a-f]{16})', listing.decode())
        assert identity_match is not None
        _, _, detail = self.call("/item/" + identity_match.group(1), cookie=session)
        nonce_match = re.search(r'name="form_nonce" value="([^"]+)"', detail.decode())
        assert nonce_match is not None
        binding = rough_binding(self.rough)
        invalid_type = {"CONTENT_TYPE": "application/x-www-form-urlencoded-evil"}
        self.assertEqual(self.call(
            "/decision", method="POST", cookie=session, origin=REVIEW_ORIGIN,
            form={
                "form_nonce": nonce_match.group(1), "rough_path": "ingestion/rough/pending.md",
                "rough_sha256": binding.sha256, "rough_version": binding.version,
                "action": "reject", "wiki_path": "", "candidate_markdown": "",
            }, extra=invalid_type,
        )[0], "415 Unsupported Media Type")

    def test_responses_never_enable_cors(self):
        _, headers, _ = self.call("/", extra={"HTTP_ACCESS_CONTROL_REQUEST_METHOD": "POST"})
        self.assertFalse(any(name.lower().startswith("access-control-") for name, _ in headers))

    def test_the_source_list_is_for_reviewers_only(self):
        status, headers, body = self.call("/sources", cookie=self.authenticate())
        self.assertEqual(status, "200 OK")
        self.assertIn("来源列表".encode(), body)
        self.assertEqual(self.call("/sources", cookie=self.authenticate("someone-else"))[0], "403 Forbidden")
        self.assertEqual(self.call("/sources", method="POST", cookie=self.authenticate())[0], "405 Method Not Allowed")
        status, headers, _ = self.call("/sources")
        self.assertEqual(status, "302 Found")                              # not signed in: sent to log in, nothing shown
        self.assertNotIn(b"sjcx", self.call("/sources")[2])

    def test_trigger_ingest_is_404_when_not_configured(self):
        session = self.authenticate()
        self.assertEqual(self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)[0], "404 Not Found")

    def test_trigger_ingest_requires_reviewer_and_exact_origin(self):
        trigger_path = self.root / "state" / "ingest-trigger-requested"
        app = ReviewApp(self.service, CLAIM_SECRET, ("reviewer-1",), expected_origin=REVIEW_ORIGIN, clock=self.clock, ingest_trigger_path=trigger_path)
        self.app = app
        self.assertEqual(self.call("/trigger-ingest", method="GET", cookie=self.authenticate(), origin=REVIEW_ORIGIN)[0], "405 Method Not Allowed")
        self.assertEqual(self.call("/trigger-ingest", method="POST", origin=REVIEW_ORIGIN)[0], "401 Unauthorized")
        self.assertEqual(self.call("/trigger-ingest", method="POST", cookie=self.authenticate("someone-else"), origin=REVIEW_ORIGIN)[0], "403 Forbidden")
        self.assertEqual(self.call("/trigger-ingest", method="POST", cookie=self.authenticate(), origin="https://evil.example")[0], "403 Forbidden")
        self.assertFalse(trigger_path.exists())

    def test_trigger_ingest_writes_marker_then_cools_down(self):
        trigger_path = self.root / "state" / "ingest-trigger-requested"
        self.app = ReviewApp(self.service, CLAIM_SECRET, ("reviewer-1",), expected_origin=REVIEW_ORIGIN, clock=self.clock, ingest_trigger_path=trigger_path)
        session = self.authenticate()
        status, headers, _ = self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.assertEqual(status, "303 See Other")
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/?notice=ingest_triggered")
        self.assertTrue(trigger_path.exists())
        first_content = trigger_path.read_text()
        self.assertEqual(first_content, "reviewer-1 1900000000\n")

        # A second click seconds later is a no-op: cooldown, marker untouched.
        self.clock_value += 5
        status, headers, _ = self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.assertEqual(status, "303 See Other")
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/?notice=ingest_cooldown")
        self.assertEqual(trigger_path.read_text(), first_content)

        # After the cooldown window, a new click re-arms the trigger.
        self.clock_value += 700
        status, headers, _ = self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/?notice=ingest_triggered")
        self.assertNotEqual(trigger_path.read_text(), first_content)

    # --- snapshot time and "a pull is running" -------------------------------------------------
    def snapshot_app(self, snapshot_age_seconds=300):
        """The app with an ingest state file and a fixed snapshot time `snapshot_age_seconds` ago."""
        state = self.root / "state" / "ingest-requested-at"
        trigger = self.root / "state" / "ingest-trigger-requested"
        self.service.ingest_state_path = state
        self.snapshot_time = self.clock_value - snapshot_age_seconds
        self.service.snapshot_time = lambda: self.snapshot_time
        self.app = ReviewApp(self.service, CLAIM_SECRET, ("reviewer-1",), expected_origin=REVIEW_ORIGIN,
                             clock=self.clock, ingest_trigger_path=trigger)
        return state

    def decision_form(self, session):
        _, _, body = self.call("/", cookie=session)
        identity = re.search(r'/review/item/([0-9a-f]{16})', body.decode("utf-8")).group(1)
        _, _, detail = self.call("/item/" + identity, cookie=session)
        nonce = re.search('name="form_nonce" value="([^"]+)"', detail.decode("utf-8")).group(1)
        binding = rough_binding(self.rough)
        return identity, {"form_nonce": nonce, "rough_path": "ingestion/rough/pending.md", "rough_sha256": binding.sha256,
                          "rough_version": binding.version, "action": "reject", "wiki_path": "", "candidate_markdown": ""}

    def test_the_list_and_the_item_page_show_how_old_the_data_is(self):
        self.snapshot_app(snapshot_age_seconds=300)
        session = self.authenticate()
        list_page = self.call("/", cookie=session)[2].decode("utf-8")
        self.assertIn("数据快照：", list_page)
        self.assertIn("5 分钟前", list_page)
        identity = re.search(r'/review/item/([0-9a-f]{16})', list_page).group(1)
        self.assertIn("数据快照：", self.call("/item/" + identity, cookie=session)[2].decode("utf-8"))
        self.assertNotIn("抓取进行中", list_page)

    def test_clicking_pull_records_the_time_and_the_pages_say_a_pull_is_running(self):
        state = self.snapshot_app(snapshot_age_seconds=300)
        session = self.authenticate()
        self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.assertEqual(state.read_text().strip(), str(self.clock_value))
        page = self.call("/", cookie=session)[2].decode("utf-8")
        self.assertIn("抓取进行中", page)
        self.assertIn("不能批准或拒绝", page)

    def test_a_decision_made_while_the_pull_runs_is_refused_with_a_notice_and_nothing_is_recorded(self):
        self.snapshot_app()
        session = self.authenticate()
        identity, form = self.decision_form(session)
        self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.clock_value += 9                                    # nine seconds later, the very case seen in production
        status, headers, _ = self.call("/decision", method="POST", cookie=session, origin=REVIEW_ORIGIN, form=form)
        self.assertEqual(status, "303 See Other")
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/item/" + identity + "?notice=ingest_running")
        self.assertFalse((self.root / "state" / "decisions.jsonl").exists() and (self.root / "state" / "decisions.jsonl").read_text().strip())
        notice = self.call("/item/" + identity, query="notice=ingest_running", cookie=session)[2].decode("utf-8")
        self.assertIn("这次没有记录任何决定", notice)

    def test_once_the_data_is_refreshed_decisions_work_again(self):
        self.snapshot_app()
        session = self.authenticate()
        identity, form = self.decision_form(session)
        self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.clock_value += 60
        self.snapshot_time = self.clock_value                     # the pull finished and wrote a new snapshot
        status, headers, _ = self.call("/decision", method="POST", cookie=session, origin=REVIEW_ORIGIN, form=form)
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/item/" + identity + "?notice=rejected")
        self.assertNotIn("抓取进行中", self.call("/", cookie=session)[2].decode("utf-8"))

    def test_a_pull_that_never_refreshed_the_data_stops_blocking_and_says_so(self):
        self.snapshot_app()
        session = self.authenticate()
        identity, form = self.decision_form(session)
        self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.clock_value += ReviewService.INGEST_WAIT_SECONDS + 5
        page = self.call("/", cookie=session)[2].decode("utf-8")
        self.assertIn("可能失败", page)
        self.assertNotIn("抓取进行中", page)
        status, headers, _ = self.call("/decision", method="POST", cookie=session, origin=REVIEW_ORIGIN, form=form)
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/item/" + identity + "?notice=rejected")

    def test_without_an_ingest_state_file_nothing_changes(self):
        session = self.authenticate()
        page = self.call("/", cookie=session)[2].decode("utf-8")
        self.assertNotIn("抓取进行中", page)
        self.assertNotIn("可能失败", page)


    # --- buttons of an action that is still running ---------------------------------------------
    def busy_app(self):
        """The app with ingest and publish state files, publish and ingest triggers, and a snapshot 10 minutes old."""
        self.service.ingest_state_path = self.root / "state" / "ingest-requested-at"
        self.service.publish_state_path = self.root / "state" / "publish-requested-at"
        self.snapshot_time = self.clock_value - 600
        self.service.snapshot_time = lambda: self.snapshot_time
        self.ingest_trigger = self.root / "state" / "ingest-trigger-requested"
        self.publish_trigger = self.root / "state" / "publish-trigger-requested"
        self.app = ReviewApp(self.service, CLAIM_SECRET, ("reviewer-1",), expected_origin=REVIEW_ORIGIN, clock=self.clock,
                             ingest_trigger_path=self.ingest_trigger, publish_trigger_path=self.publish_trigger)
        return self.authenticate()

    def test_a_publish_in_progress_greys_the_publish_button_and_refuses_a_second_request(self):
        session = self.busy_app()
        self.call("/publish", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.assertEqual((self.root / "state" / "publish-requested-at").read_text().strip(), str(self.clock_value))
        page = self.call("/", cookie=session)[2].decode("utf-8")
        self.assertIn('<button type="submit" class="is-busy" disabled aria-busy="true">发布中…</button>', page)
        self.assertIn("发布进行中", page)
        self.assertNotIn("发布已批准内容", page)
        self.publish_trigger.unlink()                                    # the privileged unit picked the marker up
        self.clock_value += 120                                           # past the cooldown, but still running
        status, headers, _ = self.call("/publish", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.assertEqual((status, dict(headers)["Location"]), ("303 See Other", REVIEW_PREFIX + "/?notice=publish_already"))
        self.assertFalse(self.publish_trigger.exists())                  # nothing was queued

    def test_the_publish_button_is_back_once_the_data_was_refreshed_after_the_publish(self):
        session = self.busy_app()
        self.call("/publish", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.snapshot_time = self.clock_value + 90                       # the chain's last step rewrote the review data
        self.clock_value += 100
        page = self.call("/", cookie=session)[2].decode("utf-8")
        self.assertNotIn("发布中…", page)
        self.assertIn("发布已批准内容", page)

    def test_a_publish_that_never_refreshed_the_data_stops_blocking_after_fifteen_minutes(self):
        session = self.busy_app()
        self.call("/publish", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.clock_value += 901
        self.assertIn("发布已批准内容", self.call("/", cookie=session)[2].decode("utf-8"))

    def test_a_pull_in_progress_greys_the_pull_button_and_the_decision_buttons_and_refuses_a_second_pull(self):
        session = self.busy_app()
        self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        page = self.call("/", cookie=session)[2].decode("utf-8")
        self.assertIn('<button type="submit" class="is-busy" disabled aria-busy="true">抓取中…</button>', page)
        identity = re.search(r'/review/item/([0-9a-f]{16})', page).group(1)
        detail = self.call("/item/" + identity, cookie=session)[2].decode("utf-8")
        self.assertIn('class="is-busy" disabled aria-busy="true">抓取中，暂不能批准</button>', detail)
        self.assertNotIn('name="action" value="approve"', detail)
        self.ingest_trigger.unlink()
        self.clock_value += 5                                             # the pull is still running
        status, headers, _ = self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/?notice=ingest_already")
        self.assertFalse(self.ingest_trigger.exists())

    def test_while_a_pull_runs_the_state_is_one_message_not_three(self):
        session = self.busy_app()
        self.call("/trigger-ingest", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        page = self.call("/", query="notice=ingest_triggered", cookie=session)[2].decode("utf-8")
        body = page.split("</style>", 1)[1]
        self.assertEqual(body.count("抓取进行中（"), 1)
        self.assertEqual(body.count('class="notice'), 1)                  # a single block ...
        self.assertNotIn("已提交拉取请求", body)                            # ... without the separate "submitted" notice
        self.assertNotIn('class="meta snapshot-line"', body)               # ... and the snapshot age is inside it
        self.assertIn("数据快照：", body)

    def test_the_submitted_notice_still_shows_when_nothing_is_running(self):
        session = self.busy_app()
        page = self.call("/", query="notice=ingest_triggered", cookie=session)[2].decode("utf-8")
        self.assertIn("已提交拉取请求", page)
        self.assertIn('class="meta snapshot-line"', page)

    def test_the_buttons_are_normal_when_nothing_is_running(self):
        session = self.busy_app()
        page = self.call("/", cookie=session)[2].decode("utf-8")
        self.assertIn('data-busy-label="已提交…">立即拉取最新源</button>', page)
        self.assertNotIn("is-busy", page.split("</style>", 1)[1])

    def test_the_new_notices_have_text(self):
        from web.review_app import NOTICES
        self.assertIn("已经在进行", NOTICES["ingest_already"])
        self.assertIn("已经在进行", NOTICES["publish_already"])

    def test_publish_is_404_when_not_configured(self):
        session = self.authenticate()
        self.assertEqual(self.call("/publish", method="POST", cookie=session, origin=REVIEW_ORIGIN)[0], "404 Not Found")

    def test_publish_requires_reviewer_and_exact_origin(self):
        trigger_path = self.root / "state" / "publish-trigger-requested"
        app = ReviewApp(self.service, CLAIM_SECRET, ("reviewer-1",), expected_origin=REVIEW_ORIGIN, clock=self.clock, publish_trigger_path=trigger_path)
        self.app = app
        self.assertEqual(self.call("/publish", method="GET", cookie=self.authenticate(), origin=REVIEW_ORIGIN)[0], "405 Method Not Allowed")
        self.assertEqual(self.call("/publish", method="POST", origin=REVIEW_ORIGIN)[0], "401 Unauthorized")
        self.assertEqual(self.call("/publish", method="POST", cookie=self.authenticate("someone-else"), origin=REVIEW_ORIGIN)[0], "403 Forbidden")
        self.assertEqual(self.call("/publish", method="POST", cookie=self.authenticate(), origin="https://evil.example")[0], "403 Forbidden")
        self.assertFalse(trigger_path.exists())

    def test_publish_writes_marker_then_cools_down(self):
        trigger_path = self.root / "state" / "publish-trigger-requested"
        self.app = ReviewApp(self.service, CLAIM_SECRET, ("reviewer-1",), expected_origin=REVIEW_ORIGIN, clock=self.clock, publish_trigger_path=trigger_path)
        session = self.authenticate()
        status, headers, _ = self.call("/publish", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.assertEqual(status, "303 See Other")
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/?notice=publish_triggered")
        self.assertTrue(trigger_path.exists())
        first_content = trigger_path.read_text()
        self.assertEqual(first_content, "reviewer-1 1900000000\n")

        # A second click seconds later is a no-op: cooldown, marker untouched.
        self.clock_value += 5
        status, headers, _ = self.call("/publish", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.assertEqual(status, "303 See Other")
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/?notice=publish_cooldown")
        self.assertEqual(trigger_path.read_text(), first_content)

        # After the (shorter) publish cooldown window, a new click re-arms the trigger.
        self.clock_value += 70
        status, headers, _ = self.call("/publish", method="POST", cookie=session, origin=REVIEW_ORIGIN)
        self.assertEqual(dict(headers)["Location"], REVIEW_PREFIX + "/?notice=publish_triggered")
        self.assertNotEqual(trigger_path.read_text(), first_content)


class ReviewProxyConfigTests(unittest.TestCase):
    def test_main_origin_mounts_review_prefix_and_legacy_host_only_redirects_root(self):
        location = Path("deploy/nginx/dek-review-location.conf").read_text(encoding="utf-8")
        self.assertIn("location = /review", location)
        self.assertIn("return 302 /review/;", location)
        self.assertIn("location ^~ /review/", location)
        self.assertIn("proxy_pass http://dek_review/;", location)
        self.assertIn("proxy_set_header X-Forwarded-Prefix /review;", location)
        self.assertIn("if ($request_method != GET) { return 405; }", location)

        main_site = Path("deploy/nginx/regkb.chenponai.com").read_text(encoding="utf-8")
        self.assertIn("server_name regkb.chenponai.com;", main_site)
        self.assertIn("include /etc/nginx/snippets/dek-review-location.conf;", main_site)
        plaintext_start = main_site.rindex("server {", 0, main_site.index("listen 80;"))
        plaintext = main_site[plaintext_start:main_site.index("server {", plaintext_start + 1)]
        self.assertIn("location = /review", plaintext)
        self.assertIn("location ^~ /review/", plaintext)
        self.assertEqual(plaintext.count("if ($request_method != GET) { return 405; }"), 2)
        self.assertLess(
            main_site.index("include /etc/nginx/snippets/dek-review-location.conf;"),
            main_site.index("location / {\n        limit_except GET"),
        )

        legacy = Path("deploy/nginx/dek-review.conf").read_text(encoding="utf-8")
        self.assertIn("upstream dek_review", legacy)
        self.assertEqual(legacy.count("server_name review.regkb.chenponai.com;"), 2)
        self.assertEqual(legacy.count("if ($request_method != GET) { return 405; }"), 2)
        self.assertIn("listen 80;", legacy)
        self.assertIn("location ^~ /.well-known/acme-challenge/", legacy)
        self.assertIn("return 302 https://regkb.chenponai.com/review/;", legacy)
        self.assertIn("location /", legacy)
        self.assertIn("return 404;", legacy)


if __name__ == "__main__":
    unittest.main()
