"""Isolated WSGI reviewer origin. Identity is a claim issued by the knowledge
base's own DingTalk login (`dek_session`, see `.auth`); this app never runs
its own OAuth handshake, it only checks the shared claim against the
reviewer whitelist."""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import logging
import os
import re
import socket
import socketserver
import stat
import time
import urllib.parse
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from .auth import authorize_claim
from .review import (
    ACTION_STATUS,
    PAGE_SIZE,
    MAX_DECISION_BYTES,
    IsolatedReviewClone,
    MemoryFormNonceStore,
    ReviewError,
    ReviewService,
    ReviewerLabelStore,
    item_identity,
    sanitize_nickname,
)


REVIEW_ORIGIN = "https://regkb.chenponai.com"
REVIEW_PREFIX = "/review"
KB_SESSION_COOKIE = "dek_session"
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("Content-Security-Policy", "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)

auth_logger = logging.getLogger("web.review.auth")


NOTICES = {
    "approved": "已记录：批准发布。发布流程启用后才会写入知识库。",
    "rejected": "已记录：拒绝。",
    "decided": "决定已记录。",
    "ingest_triggered": "已提交拉取请求，新的来源会在后台抓取，稍后刷新查看。",
    "ingest_cooldown": "刚触发过一次拉取，请稍等几分钟再试。",
    "ingest_running": "抓取正在进行，完成并刷新页面后才能批准或拒绝，避免处理到旧列表里已经不存在的内容。这次没有记录任何决定。",
    "publish_triggered": "已提交发布请求，已批准的内容会在后台构建并发布，稍后刷新查看。",
    "publish_cooldown": "刚触发过一次发布，请稍等片刻再试。",
}
STATUS_FILTERS = {"pending", "approved", "published", "rejected"}


def list_position(query: dict) -> tuple[str, int, int]:
    """Validated (status, page, page_size) from a query string; anything odd falls back to the default."""
    status = query.get("status", [""])[0]
    raw = query.get("page", [""])[0]
    size = query.get("page_size", [""])[0]
    return ((status if status in STATUS_FILTERS else ""),
            (int(raw) if raw.isdigit() and len(raw) <= 6 else 1),
            # Any positive integer is clamped into range later; zero, negatives and
            # anything that is not a whole number all mean "use the default".
            (int(size) if re.fullmatch(r"[0-9]{1,12}", size) and int(size) > 0 else PAGE_SIZE))
QUERY_LIMIT = 200
ORIGIN_PATTERN = re.compile(r"(https?)://([A-Za-z0-9.\-]{1,253})(?::(\d{1,5}))?")


def sanitize_origin(value: object) -> str:
    """Reduce a request Origin header to scheme://host[:port] for safe diagnosis."""
    if value is None:
        return "absent"
    if not isinstance(value, str):
        return "<unrecognised>"
    stripped = value.strip()
    if not stripped:
        return "absent"
    if stripped.lower() == "null":
        return "null"
    parts = urllib.parse.urlsplit(stripped)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "<unrecognised>"
    if not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", parts.hostname):
        return "<unrecognised>"
    try:
        port = parts.port
    except ValueError:
        return "<unrecognised>"
    return "%s://%s%s" % (parts.scheme, parts.hostname, ":%d" % port if port else "")


FETCH_SITE_VALUES = {"same-origin", "same-site", "cross-site", "none"}


def sanitize_fetch_site(value: object) -> str:
    if not isinstance(value, str) or value.strip().lower() not in FETCH_SITE_VALUES:
        return "absent" if value is None else "other"
    return value.strip().lower()


def origin_kind(value: object, expected: str) -> str:
    """Classify a request Origin header: exact, absent, null or other."""
    if value is None or not isinstance(value, str) or not value.strip():
        return "absent"
    if value.strip().lower() == "null":
        return "null"
    return "exact" if value == expected else "other"


def is_form_content_type(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.split(";", 1)[0].strip().lower() == "application/x-www-form-urlencoded"


INGEST_COOLDOWN_SECONDS = 600
PUBLISH_COOLDOWN_SECONDS = 60


class ReviewApp:
    def __init__(
        self, service: ReviewService, claim_secret: bytes, reviewer_ids, *, expected_origin: str = REVIEW_ORIGIN,
        clock=time.time, ingest_trigger_path: Path | None = None, ingest_cooldown_seconds: int = INGEST_COOLDOWN_SECONDS,
        publish_trigger_path: Path | None = None, publish_cooldown_seconds: int = PUBLISH_COOLDOWN_SECONDS,
    ):
        if expected_origin != REVIEW_ORIGIN:
            raise ValueError("review origin must match the dedicated production origin")
        self.service = service
        self.claim_secret = claim_secret
        self.reviewers = frozenset(value for value in reviewer_ids if isinstance(value, str) and value)
        self.expected_origin = expected_origin
        self.clock = clock
        self.ingest_trigger_path = ingest_trigger_path
        self.ingest_cooldown_seconds = ingest_cooldown_seconds
        self.publish_trigger_path = publish_trigger_path
        self.publish_cooldown_seconds = publish_cooldown_seconds

    def _response(self, start, status: str, body: bytes = b"", headers=()):
        start(status, [("Content-Length", str(len(body))), *SECURITY_HEADERS, *headers])
        return [body]

    @staticmethod
    def _cookies(environ) -> SimpleCookie:
        cookies = SimpleCookie()
        cookies.load(environ.get("HTTP_COOKIE", ""))
        return cookies

    def _begin_login(self, start, next_path: str):
        location = self.expected_origin + "/auth/bounce?" + urllib.parse.urlencode({"next": next_path})
        return self._response(start, "302 Found", headers=(("Location", location),))

    def __call__(self, environ, start):
        path = environ.get("PATH_INFO") or "/"
        if path == "/__ready":
            if (environ.get("REQUEST_METHOD") or "GET") != "GET":
                return self._response(start, "405 Method Not Allowed", b"Method Not Allowed", (("Allow", "GET"),))
            if not self.reviewers:
                return self._response(start,"503 Service Unavailable",b"Unavailable")
            body=json.dumps({"status":"ready","login_bounce":self.expected_origin+"/auth/bounce"},separators=(",",":")).encode()
            return self._response(start,"200 OK",body,(("Content-Type","application/json"),))
        method = environ.get("REQUEST_METHOD") or "GET"
        cookies = self._cookies(environ)

        if path == "/auth/logout":
            if method not in ("GET", "POST"):
                return self._response(start, "405 Method Not Allowed", b"Method Not Allowed", (("Allow", "GET, POST"),))
            # Logout is owned by the knowledge base: one `dek_session` cookie
            # covers both origins, so clearing it there ends this session too.
            return self._response(start, "302 Found", headers=(("Location", self.expected_origin + "/auth/logout"),))

        morsel = cookies.get(KB_SESSION_COOKIE)
        decision = authorize_claim(morsel.value if morsel else None, self.claim_secret, now=int(self.clock()))
        authenticated = decision.allowed and isinstance(decision.user_id, str)
        is_reviewer = authenticated and decision.user_id in self.reviewers
        if authenticated and not is_reviewer:
            auth_logger.warning("review_forbidden_not_a_reviewer user_id=%s", decision.user_id)
        user_id = decision.user_id if is_reviewer else None
        display_name = sanitize_nickname(decision.display_name) if is_reviewer else ""
        session_id = hashlib.sha256(morsel.value.encode("utf-8")).hexdigest() if is_reviewer and morsel else ""

        if path == "/":
            if method != "GET":
                return self._response(start, "405 Method Not Allowed", b"Method Not Allowed", (("Allow", "GET"),))
            if not self.reviewers:
                return self._response(start, "403 Forbidden", b"Forbidden")
            if not authenticated:
                return self._begin_login(start, REVIEW_PREFIX + "/")
            if not is_reviewer:
                return self._response(start, "403 Forbidden", b"Forbidden")
            query = parse_qs(environ.get("QUERY_STRING", ""))
            search = (query.get("q", [""])[0] or "")[:QUERY_LIMIT]
            status_filter, page, page_size = list_position(query)
            notice = NOTICES.get(query.get("notice", [""])[0], "")
            try:
                body = self.service.render_list(session_id, query=search, status=status_filter, notice=notice, page=page, page_size=page_size)
            except ReviewError as error:
                return self._render_failure(start, error)
            return self._response(start, "200 OK", body, (("Content-Type", "text/html; charset=utf-8"),))

        if path.startswith("/item/"):
            if method != "GET":
                return self._response(start, "405 Method Not Allowed", b"Method Not Allowed", (("Allow", "GET"),))
            if not self.reviewers:
                return self._response(start, "403 Forbidden", b"Forbidden")
            if not authenticated:
                return self._begin_login(start, REVIEW_PREFIX + path)
            if not is_reviewer:
                return self._response(start, "403 Forbidden", b"Forbidden")
            query = parse_qs(environ.get("QUERY_STRING", ""))
            notice = NOTICES.get(query.get("notice", [""])[0], "")
            unlocked = query.get("edit", [""])[0] == "1"
            list_status, list_page, list_size = list_position(query)
            try:
                body = self.service.render_item(session_id, path[len("/item/"):], notice=notice, unlocked=unlocked, list_status=list_status, list_page=list_page, list_size=list_size)
            except ReviewError as error:
                return self._render_failure(start, error)
            if body is None:
                return self._response(start, "404 Not Found", b"Not Found")
            return self._response(start, "200 OK", body, (("Content-Type", "text/html; charset=utf-8"),))

        if path == "/decision":
            if method != "POST":
                return self._response(start, "405 Method Not Allowed", b"Method Not Allowed", (("Allow", "POST"),))
            if not authenticated:
                return self._response(start, "401 Unauthorized", b"Unauthorized")
            if not is_reviewer:
                return self._response(start, "403 Forbidden", b"Forbidden")
            kind = origin_kind(environ.get("HTTP_ORIGIN"), self.expected_origin)
            if kind in {"absent", "null"}:
                # DingTalk's embedded browser submits the decision form with
                # `Origin: null`; some other clients omit the header entirely.
                # Fail-closed protection is unchanged: the SameSite=Lax session
                # cookie is not attached to a cross-site POST, the single-use nonce
                # below is bound to that exact session, and an explicit cross-site
                # fetch metadata value is still rejected.
                fetch_site = sanitize_fetch_site(environ.get("HTTP_SEC_FETCH_SITE"))
                if fetch_site in {"cross-site", "other"}:
                    auth_logger.warning("review_decision origin_unusable origin=%s fetch_site=%s", kind, fetch_site)
                    return self._response(start, "403 Forbidden", b"Forbidden")
                auth_logger.warning("review_decision origin_%s fetch_site=%s", kind, fetch_site)
            elif kind == "other":
                auth_logger.warning("review_decision origin_mismatch origin=%s", sanitize_origin(environ.get("HTTP_ORIGIN")))
                return self._response(start, "403 Forbidden", b"Forbidden")
            if not is_form_content_type(environ.get("CONTENT_TYPE")):
                return self._response(start, "415 Unsupported Media Type", b"Unsupported Media Type")
            try:
                length = int(environ.get("CONTENT_LENGTH") or "0")
                if length < 0 or length > MAX_DECISION_BYTES:
                    raise ReviewError("request too large", "413 Payload Too Large")
                body = environ["wsgi.input"].read(length)
                if len(body) != length:
                    raise ReviewError("incomplete request")
                values = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=12)
                self.service.submit_form(body, session_id=session_id, user_id=user_id, reviewer_label=display_name)
            except (ReviewError, ValueError, KeyError, UnicodeDecodeError) as error:
                status = error.status if isinstance(error, ReviewError) else "400 Bad Request"
                if status.startswith("409") and "ingest in progress" in str(error):
                    try:
                        rough = parse_qs(body.decode("utf-8"), keep_blank_values=True).get("rough_path", [""])[0]
                        target = "%s/item/%s?notice=ingest_running" % (REVIEW_PREFIX, item_identity(rough)) if rough else "%s/?notice=ingest_running" % REVIEW_PREFIX
                    except (ValueError, KeyError):
                        target = "%s/?notice=ingest_running" % REVIEW_PREFIX
                    auth_logger.warning("review_decision_blocked reason=ingest_in_progress")
                    return self._response(start, "303 See Other", headers=(("Location", target),))
                # The response body only ever carries the generic status
                # text (never the real reason, to avoid leaking form/nonce
                # internals to the client) -- log the actual message so a
                # real failure can be diagnosed from the journal instead of
                # guessed at from a bare "400 Bad Request".
                auth_logger.warning("review_decision_rejected status=%s reason=%s", status, str(error) or type(error).__name__)
                return self._response(start, status, status.encode("ascii"), (("Content-Type", "text/plain"),))
            rough_path = values.get("rough_path", [""])[0]
            action = values.get("action", [""])[0]
            notice = ACTION_STATUS.get(action, "decided")
            location = "%s/item/%s?notice=%s" % (REVIEW_PREFIX, item_identity(rough_path), notice)
            position_status, position_page, position_size = list_position(parse_qs(environ.get("QUERY_STRING", "")))
            if position_status: location += "&status=" + position_status
            if position_page > 1: location += "&page=%d" % position_page
            if position_size != PAGE_SIZE: location += "&page_size=%d" % position_size
            return self._response(start, "303 See Other", headers=(("Location", location),))

        if path == "/trigger-ingest":
            return self._handle_trigger(
                start, environ, method, authenticated, is_reviewer, user_id,
                trigger_path=self.ingest_trigger_path, cooldown_seconds=self.ingest_cooldown_seconds,
                log_label="review_trigger_ingest", triggered_notice="ingest_triggered", cooldown_notice="ingest_cooldown",
                on_triggered=self.service.record_ingest_request,
            )

        if path == "/publish":
            return self._handle_trigger(
                start, environ, method, authenticated, is_reviewer, user_id,
                trigger_path=self.publish_trigger_path, cooldown_seconds=self.publish_cooldown_seconds,
                log_label="review_trigger_publish", triggered_notice="publish_triggered", cooldown_notice="publish_cooldown",
            )

        return self._response(start, "404 Not Found", b"Not Found")

    def _handle_trigger(self, start, environ, method, authenticated, is_reviewer, user_id, *,
                        trigger_path: Path | None, cooldown_seconds: int,
                        log_label: str, triggered_notice: str, cooldown_notice: str, on_triggered=None):
        """Shared body for every reviewer-initiated, cooldown-protected marker write.

        The marker itself does the work: a privileged systemd .path unit watches
        it and does the actual (possibly root-requiring) action -- this process
        never escalates or calls systemctl itself.
        """
        if method != "POST":
            return self._response(start, "405 Method Not Allowed", b"Method Not Allowed", (("Allow", "POST"),))
        if not authenticated:
            return self._response(start, "401 Unauthorized", b"Unauthorized")
        if not is_reviewer:
            return self._response(start, "403 Forbidden", b"Forbidden")
        kind = origin_kind(environ.get("HTTP_ORIGIN"), self.expected_origin)
        if kind in {"absent", "null"}:
            fetch_site = sanitize_fetch_site(environ.get("HTTP_SEC_FETCH_SITE"))
            if fetch_site in {"cross-site", "other"}:
                auth_logger.warning("%s origin_unusable origin=%s fetch_site=%s", log_label, kind, fetch_site)
                return self._response(start, "403 Forbidden", b"Forbidden")
            auth_logger.warning("%s origin_%s fetch_site=%s", log_label, kind, fetch_site)
        elif kind == "other":
            auth_logger.warning("%s origin_mismatch origin=%s", log_label, sanitize_origin(environ.get("HTTP_ORIGIN")))
            return self._response(start, "403 Forbidden", b"Forbidden")
        if trigger_path is None:
            return self._response(start, "404 Not Found", b"Not Found")
        now = self.clock()
        cooling_down = False
        try:
            _, _, last_requested = trigger_path.read_text(encoding="utf-8").strip().rpartition(" ")
            cooling_down = (now - float(last_requested)) < cooldown_seconds
        except (FileNotFoundError, ValueError):
            cooling_down = False
        if cooling_down:
            notice = cooldown_notice
        else:
            trigger_path.parent.mkdir(parents=True, exist_ok=True)
            trigger_path.write_text(f"{user_id} {int(now)}\n", encoding="utf-8")
            if on_triggered is not None:
                on_triggered(now)
            auth_logger.warning("%s requested user=%s", log_label, user_id)
            notice = triggered_notice
        return self._response(start, "303 See Other", headers=(("Location", REVIEW_PREFIX + "/?notice=" + notice),))

    def _render_failure(self, start, error: ReviewError):
        auth_logger.warning("review_render_failed status=%s", error.status)
        reason = error.status.split(" ", 1)[-1]
        return self._response(start, error.status, reason.encode("ascii"), (("Content-Type", "text/plain"),))


class UnixWSGIServer(WSGIServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = 0
        self.setup_environ()


class UnixWSGIRequestHandler(WSGIRequestHandler):
    def address_string(self):
        return "local"

    def get_environ(self):
        client_address = self.client_address
        if not isinstance(client_address, tuple) or not client_address:
            self.client_address = ("localhost", 0)
        try:
            return super().get_environ()
        finally:
            self.client_address = client_address


def serve_unix(socket_path: Path, app: ReviewApp, *, socket_group: str | None = None) -> None:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        mode = path.lstat().st_mode
        if not stat.S_ISSOCK(mode):
            raise RuntimeError("refusing to replace non-socket path")
        path.unlink()
    server = UnixWSGIServer(str(path), UnixWSGIRequestHandler)
    try:
        os.chmod(path, 0o660)
        if socket_group:
            os.chown(path, -1, grp.getgrnam(socket_group).gr_gid)
        server.set_app(app)
        server.serve_forever()
    finally:
        server.server_close()
        if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--clones", type=Path)
    parser.add_argument("--bundle-archive", type=Path)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--suggestions", type=Path)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--socket-group")
    parser.add_argument("--ingest-trigger", type=Path)
    parser.add_argument("--publish-trigger", type=Path)
    args = parser.parse_args()
    required = ("DEK_REVIEW_AUDIT_KEY", "DEK_WEB_CLAIM_SECRET")
    missing = [name for name in required if not os.environ.get(name)]
    decision_key_file = os.environ.get("DEK_REVIEW_DECISION_KEY_FILE", "")
    if not decision_key_file:
        missing.append("DEK_REVIEW_DECISION_KEY_FILE")
    if missing:
        raise SystemExit("missing required environment variables: " + ",".join(missing))
    claim_secret = os.environ["DEK_WEB_CLAIM_SECRET"].encode()
    reviewers = tuple(value.strip() for value in os.environ.get("DEK_REVIEWER_IDS", "").split(",") if value.strip())
    if not reviewers:
        raise SystemExit("DEK_REVIEWER_IDS must contain independently verified enterprise IDs")
    nonces = MemoryFormNonceStore()
    if args.bundle and args.clones: repository=IsolatedReviewClone(args.bundle,args.clones,bundle_archive=args.bundle_archive)
    elif args.repo_root: repository=args.repo_root
    else: raise SystemExit("--bundle/--clones are required")
    service = ReviewService(
        repository, args.queue,
        audit_key=os.environ["DEK_REVIEW_AUDIT_KEY"].encode(),
        queue_key=Path(decision_key_file).read_bytes().strip(),
        nonces=nonces,
        clock=time.time,
        labels=ReviewerLabelStore(args.labels) if args.labels else None,
        path_prefix=REVIEW_PREFIX,
        suggestions_path=args.suggestions,
        ingest_state_path=args.ingest_trigger.with_name("ingest-requested-at") if args.ingest_trigger else None,
    )
    serve_unix(
        args.socket, ReviewApp(service, claim_secret, reviewers,
                              ingest_trigger_path=args.ingest_trigger, publish_trigger_path=args.publish_trigger),
        socket_group=args.socket_group,
    )


if __name__ == "__main__":
    main()
