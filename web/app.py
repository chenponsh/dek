"""Minimal fail-closed WSGI server for the generated internal site."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import secrets
import time
import stat
import fcntl
import threading
from socketserver import ThreadingMixIn
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
from wsgiref.simple_server import WSGIServer, make_server

from .auth import authorize_claim
from .dingtalk_gateway import DingTalkClient, DingTalkGateway, LoginError, MemoryStateStore
from .chat import ChatHistoryError, ChatHistoryStore, ChatRateLimiter, ChatUnavailable, HermesChatClient
from .personal_documents import DocumentError, MAX_FILE_BYTES, store_from_environment


auth_logger = logging.getLogger("web.auth")

SIGNED_OUT_PAGE = """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"robots\" content=\"noindex,nofollow\"><title>已退出 · DEK</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f8f9;color:#232323;font:16px/1.6 system-ui,sans-serif}.card{width:min(420px,calc(100% - 40px));padding:38px;text-align:center;background:#fff;border:1px solid rgba(35,35,35,.12);border-top:3px solid #00c5ce;border-radius:12px;box-shadow:0 16px 50px rgba(0,0,0,.08)}h1{font-size:26px;margin:0 0 8px}p{color:#6f6f6f;margin:0 0 25px}a{display:inline-block;padding:9px 20px;border-radius:7px;background:#007c84;color:#fff;text-decoration:none}</style></head><body><main class=\"card\"><h1>已安全退出</h1><p>当前知识库会话已清除。</p><a href=\"/\">重新登录</a></main></body></html>""".encode()
USER_ERROR_BODIES = {
    "401 Unauthorized": "登录已失效，请重新登录。",
    "403 Forbidden": "当前账号没有访问权限。如需使用，请联系管理员。",
    "404 Not Found": "页面不存在或已被移动。",
    "405 Method Not Allowed": "当前操作不受支持。",
    "429 Too Many Requests": "提问太频繁，请稍后再试。",
    "502 Bad Gateway": "机器人暂时无法回答，请稍后重试。",
    "503 Service Unavailable": "系统暂时无法处理请求，请稍后重试；若仍然失败，请联系管理员。",
}


def shared_resources_page() -> bytes:
    data = json.dumps({
        "v": 1, "kind": "resources", "title": "共享资源", "path": "shared-resources",
        "root": "/", "tags": [], "crumbs": [{"text": "共享资源"}], "props": [],
        "backlinks": [], "toc": [], "sourceNotes": [], "externalLinks": [], "sourceWiki": [],
    }, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    body = (
        '<section class="shared-resource-intro"><p>团队上传的参考资源，所有用户均可查看和下载，'
        '并自动参与 Kbot AI 问答。</p><p class="muted">共享资源不经过正式知识审核发布流程；'
        'Kbot 回答时会将其明确标注为共享资源。</p></section>'
        '<section id="shared-resource-app" class="shared-resource-app">'
        '<div class="shared-resource-toolbar"><div class="shared-resource-search">'
        '<label class="sr-only" for="shared-resource-query">搜索共享资源</label>'
        '<input type="search" id="shared-resource-query" placeholder="搜索文件名称…" autocomplete="off">'
        '</div><button type="button" id="shared-resource-upload" hidden>上传资源</button>'
        '<input type="file" id="shared-resource-file" accept=".pdf,.docx,.txt,.md" hidden></div>'
        '<div id="shared-resource-status" class="shared-resource-status" aria-live="polite">正在加载共享资源…</div>'
        '<div id="shared-resource-list" class="shared-resource-list"></div></section>'
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">'
        '<title>共享资源 · DEK</title><link rel="stylesheet" href="/assets/style.css">'
        '<script src="/assets/search.js" defer></script><script src="/assets/page.js" defer></script>'
        '<script src="/assets/app.js" defer></script></head><body><div id="page-loading" class="muted">正在加载页面…</div>'
        '<noscript>此页面需要启用 JavaScript。</noscript>'
        f'<script type="application/json" id="page-data">{data}</script>'
        f'<template id="page-body">{body}</template></body></html>'
    ).encode("utf-8")


def decode_request_path(raw_path: str) -> str:
    path = unquote(raw_path)
    try:
        return path.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return path


def make_dingtalk_client(environment) -> DingTalkClient:
    return DingTalkClient(
        environment["DINGTALK_CLIENT_ID"],
        environment["DINGTALK_CLIENT_SECRET"],
        environment["DINGTALK_AGENT_ID"],
    )


def generation_proof_secret(environment) -> str:
    path = environment.get("DEK_WEB_GENERATION_PROOF_SECRET_FILE", "")
    if not path:
        return environment.get("DEK_WEB_GENERATION_PROOF_SECRET", "")
    return Path(path).read_text(encoding="utf-8").strip()


class ActiveSite:
    """Atomically pins one immutable release for the duration of one request."""
    def __init__(self, active_path: Path, releases_root: Path, maximum: int = 8 * 1024 * 1024):
        self.active_path = Path(active_path)
        self.releases_root = Path(releases_root).resolve(strict=True)
        self.maximum = maximum

    class Pin:
        def __init__(self, root, metadata, descriptor): self.root,self.metadata,self.descriptor=root,metadata,descriptor
        def __iter__(self): return iter((self.root,self.metadata))
        def close(self):
            if self.descriptor is not None: os.close(self.descriptor); self.descriptor=None
        def __del__(self): self.close()

    def pin(self):
        descriptor = os.open(self.active_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size > self.maximum:
                raise ValueError("active generation is not a bounded regular file")
            raw = os.read(descriptor, self.maximum + 1)
        finally: os.close(descriptor)
        generation = json.loads(raw)
        name = generation.get("generation")
        if not isinstance(name, str) or not name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in name):
            raise ValueError("active generation identity is invalid")
        release = (self.releases_root / name).resolve(strict=True)
        if release.parent != self.releases_root or release.is_symlink(): raise ValueError("active release escapes store")
        lock=os.open(release/"release.lock",os.O_RDONLY)
        try:
            fcntl.flock(lock,fcntl.LOCK_SH)
            if json.loads((release/"release.json").read_text(encoding="utf-8")) != generation:
                raise ValueError("active metadata does not match release")
            return self.Pin(release / "site", generation, lock)
        except Exception:
            os.close(lock)
            raise


class _PinnedResponse(list):
    def __init__(self, body, pin): super().__init__([body]); self.pin=pin
    def close(self):
        if self.pin is not None: self.pin.close(); self.pin=None
    def __del__(self): self.close()


FRONTEND_ASSET_DIR=Path(__file__).resolve().parent/"assets"
FRONTEND_ASSETS=frozenset({"/assets/style.css","/assets/app.js","/assets/search.js","/assets/page.js","/assets/logo.png"})


class KnowledgeApp:
    def __init__(self, site_root: Path, gateway: DingTalkGateway, claim_secret: bytes, clock=time.time, generation_proof_secret: str = "", boot_nonce: str = "", release_sha256: str = "", chat_client=None, chat_limiter=None, chat_history=None, document_store=None, document_uploaders=frozenset(), internal_api_key: str = ""):
        self.active_site = site_root if isinstance(site_root, ActiveSite) else None
        self.root = None if self.active_site else Path(site_root).resolve(strict=True)
        self.gateway=gateway; self.secret=claim_secret; self.clock=clock
        self.generation_proof_secret = generation_proof_secret
        self.boot_nonce = boot_nonce
        self.release_sha256 = release_sha256
        self.chat_client = chat_client
        self.chat_limiter = chat_limiter or ChatRateLimiter()
        self.chat_history = chat_history
        self.document_store = document_store
        self.document_uploaders = frozenset(document_uploaders)
        self.internal_api_key = internal_api_key
    def _same_origin(self,environ):
        expected = urlsplit(self.gateway.redirect_uri)
        return environ.get("HTTP_ORIGIN") == f"{expected.scheme}://{expected.netloc}"
    def _response(self,start,status,body=b"",headers=(),pin=None):
        if status in USER_ERROR_BODIES and body in {
            b"Unauthorized", b"Forbidden", b"Not found", b"Not Found", b"Method Not Allowed", b"Unavailable", b"Too Many Requests", b"Bad Gateway",
        }:
            body=USER_ERROR_BODIES[status].encode("utf-8")
            headers=tuple((key,value) for key,value in headers if key.lower()!="content-type")+(("Content-Type","text/plain; charset=utf-8"),)
        start(status,[("Content-Length",str(len(body))),*headers]); return _PinnedResponse(body,pin) if pin else [body]
    def __call__(self,environ,start):
        path=decode_request_path(environ.get("PATH_INFO") or "/")
        if path=="/__ready":
            if (environ.get("REQUEST_METHOD") or "GET")!="GET":
                return self._response(start,"405 Method Not Allowed",b"Method Not Allowed",[("Allow","GET")])
            body=json.dumps({"status":"ready","oauth_callback":self.gateway.redirect_uri},separators=(",",":")).encode()
            return self._response(start,"200 OK",body,[("Content-Type","application/json"),("Cache-Control","no-store")])
        if path=="/internal/shared-context":
            if (environ.get("REQUEST_METHOD") or "GET").upper()!="POST":
                return self._response(start,"405 Method Not Allowed",b"Method Not Allowed",[("Allow","POST"),("Cache-Control","no-store")])
            remote=environ.get("REMOTE_ADDR","")
            authorization=environ.get("HTTP_AUTHORIZATION","")
            expected="Bearer "+self.internal_api_key if self.internal_api_key else ""
            if remote not in {"127.0.0.1","::1"} or not expected or not secrets.compare_digest(authorization,expected):
                return self._response(start,"404 Not Found",b"Not found",[("Cache-Control","no-store")])
            if environ.get("CONTENT_TYPE","").split(";",1)[0].strip().lower()!="application/json":
                return self._response(start,"400 Bad Request",b"Bad Request",[("Cache-Control","no-store")])
            try: length=int(environ.get("CONTENT_LENGTH") or "0")
            except ValueError: length=-1
            if length<1 or length>8192:
                return self._response(start,"400 Bad Request",b"Bad Request",[("Cache-Control","no-store")])
            try:
                value=json.loads(environ["wsgi.input"].read(length))
                query=value.get("query") if isinstance(value,dict) and set(value)=={"query"} else None
            except (ValueError,UnicodeDecodeError): query=None
            if not isinstance(query,str) or not query.strip() or len(query.strip())>2000:
                return self._response(start,"400 Bad Request",b"Bad Request",[("Cache-Control","no-store")])
            try: context=self.document_store.context(query.strip()) if self.document_store else ""
            except DocumentError:
                return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
            body=json.dumps({"context":context},ensure_ascii=False,separators=(",",":")).encode()
            return self._response(start,"200 OK",body,[("Content-Type","application/json"),("Cache-Control","no-store")])
        try:
            pin = self.active_site.pin() if self.active_site else None
            root, live_generation = pin if pin else (self.root, None)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
        if path == "/__dek_generation":
            supplied = environ.get("HTTP_X_DEK_GENERATION_PROOF", "")
            if not self.generation_proof_secret or not isinstance(supplied, str) or not secrets.compare_digest(supplied, self.generation_proof_secret):
                return self._response(start,"404 Not Found",b"Not found")
            root = root.resolve(strict=True)
            index = root / "index.html"
            if not index.is_file():
                return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
            if live_generation:
                for relative, expected in live_generation.get("artifacts", {}).items():
                    if not relative.startswith("site/"): continue
                    candidate=(root/relative.removeprefix("site/")).resolve()
                    if root not in candidate.parents or not candidate.is_file() or hashlib.sha256(candidate.read_bytes()).hexdigest()!=expected:
                        return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
            proof_value = dict(live_generation) if live_generation else {"release": str(root.parent), "web_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
                                "release_sha256": self.release_sha256, "boot_nonce": self.boot_nonce}
            proof_value["pid"] = os.getpid()
            proof = json.dumps(proof_value, separators=(",", ":")).encode()
            return self._response(start,"200 OK",proof,[("Content-Type","application/json"),("Cache-Control","no-store")])
        cookie=SimpleCookie(); cookie.load(environ.get("HTTP_COOKIE", ""))
        browser_morsel=cookie.get("dek_oauth_browser")
        browser_id=browser_morsel.value if browser_morsel else ""
        if path=="/auth/callback":
            q=parse_qs(environ.get("QUERY_STRING", ""))
            try: result=self.gateway.callback(q.get("code",[""])[0],q.get("state",[""])[0],browser_id=browser_id)
            except LoginError as error:
                auth_logger.warning("dingtalk_callback_failed reason=%s", str(error))
                return self._response(start,"403 Forbidden",b"Forbidden",[("Content-Type","text/plain")])
            session_cookie=f"dek_session={result.claim}; Path=/; Max-Age=28800; HttpOnly; Secure; SameSite=Lax"
            clear_binding_cookie="dek_oauth_browser=; Path=/auth; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            return_location=quote(result.return_path, safe="/%:@?&=+$,;~-._")
            return self._response(start,"302 Found",headers=[("Location",return_location),("Set-Cookie",session_cookie),("Set-Cookie",clear_binding_cookie),("Cache-Control","no-store")])
        morsel=cookie.get("dek_session")
        decision=authorize_claim(morsel.value if morsel else None,self.secret,now=int(self.clock()))
        if path=="/auth/signed-out":
            return self._response(start,"200 OK",SIGNED_OUT_PAGE,[("Content-Type","text/html; charset=utf-8"),("Cache-Control","no-store"),("Content-Security-Policy","default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'")])
        if path=="/auth/logout":
            clear_session="dek_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            return self._response(start,"302 Found",headers=[("Location","/auth/signed-out"),("Set-Cookie",clear_session),("Cache-Control","no-store")])
        if path=="/auth/bounce":
            if (environ.get("REQUEST_METHOD") or "GET")!="GET":
                return self._response(start,"405 Method Not Allowed",b"Method Not Allowed",[("Allow","GET")])
            next_path=parse_qs(environ.get("QUERY_STRING","")).get("next",["/"])[0]
            if not isinstance(next_path,str) or not next_path.startswith("/") or next_path.startswith("//"):
                next_path="/"
            if decision.allowed:
                return self._response(start,"302 Found",headers=[("Location",next_path),("Cache-Control","no-store")])
            if not browser_id: browser_id=secrets.token_urlsafe(32)
            binding_cookie=f"dek_oauth_browser={browser_id}; Path=/auth; Max-Age=300; HttpOnly; Secure; SameSite=Lax"
            return self._response(start,"302 Found",headers=[("Location",self.gateway.login_url(next_path,browser_id=browser_id)),("Set-Cookie",binding_cookie),("Cache-Control","no-store")])
        if not decision.allowed:
            if path=="/auth/check": return self._response(start,"401 Unauthorized",b"Unauthorized",[("Cache-Control","no-store")])
            if not browser_id: browser_id=secrets.token_urlsafe(32)
            binding_cookie=f"dek_oauth_browser={browser_id}; Path=/auth; Max-Age=300; HttpOnly; Secure; SameSite=Lax"
            return self._response(start,"302 Found",headers=[("Location",self.gateway.login_url(path,browser_id=browser_id)),("Set-Cookie",binding_cookie),("Cache-Control","no-store")])
        if path=="/auth/check": return self._response(start,"204 No Content",headers=[("X-DEK-User",decision.user_id or ""),("Cache-Control","no-store")])
        if path=="/auth/me":
            body=json.dumps({"display_name":decision.display_name or "同事"},ensure_ascii=False).encode()
            return self._response(start,"200 OK",body,[("Content-Type","application/json"),("Cache-Control","no-store")])
        if path=="/api/documents":
            method=(environ.get("REQUEST_METHOD") or "GET").upper()
            can_upload=(decision.user_id or "") in self.document_uploaders
            if method=="GET":
                values=self.document_store.list() if self.document_store else []
                body=json.dumps({"enabled":self.document_store is not None,"can_upload":can_upload,"documents":values},ensure_ascii=False).encode()
                return self._response(start,"200 OK",body,[("Content-Type","application/json"),("Cache-Control","no-store")])
            if method!="POST": return self._response(start,"405 Method Not Allowed",b"Method Not Allowed",[("Allow","GET, POST"),("Cache-Control","no-store")])
            if not self._same_origin(environ): return self._response(start,"403 Forbidden",b"Forbidden",[("Cache-Control","no-store")])
            if not can_upload: return self._response(start,"403 Forbidden",b"Forbidden",[("Cache-Control","no-store")])
            if self.document_store is None: return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
            try: length=int(environ.get("CONTENT_LENGTH") or "0")
            except ValueError: length=-1
            if length<1 or length>MAX_FILE_BYTES:
                return self._response(start,"400 Bad Request","文件为空或超过 10 MB。".encode(),[("Content-Type","text/plain; charset=utf-8"),("Cache-Control","no-store")])
            filename=unquote(environ.get("HTTP_X_DEK_FILENAME", ""))
            content=environ["wsgi.input"].read(length)
            if len(content)!=length:
                return self._response(start,"400 Bad Request","文件上传不完整。".encode(),[("Content-Type","text/plain; charset=utf-8"),("Cache-Control","no-store")])
            try: item=self.document_store.add(decision.user_id or "",filename,content)
            except DocumentError as error:
                return self._response(start,"400 Bad Request",str(error).encode("utf-8"),[("Content-Type","text/plain; charset=utf-8"),("Cache-Control","no-store")])
            body=json.dumps(item,ensure_ascii=False).encode()
            return self._response(start,"201 Created",body,[("Content-Type","application/json"),("Cache-Control","no-store")])
        if path.startswith("/api/documents/"):
            if path.endswith("/download"):
                if (environ.get("REQUEST_METHOD") or "GET").upper()!="GET":
                    return self._response(start,"405 Method Not Allowed",b"Method Not Allowed",[("Allow","GET"),("Cache-Control","no-store")])
                if self.document_store is None: return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
                document_id=path.removesuffix("/download").rsplit("/",1)[1]
                try: item,content=self.document_store.download(document_id)
                except DocumentError as error:
                    return self._response(start,"404 Not Found",str(error).encode("utf-8"),[("Content-Type","text/plain; charset=utf-8"),("Cache-Control","no-store")])
                fallback="document"+Path(item["name"]).suffix.lower()
                disposition=f"inline; filename=\"{fallback}\"; filename*=UTF-8''{quote(item['name'],safe='')}"
                return self._response(start,"200 OK",content,[("Content-Type",item["mime"]),("Content-Disposition",disposition),("Cache-Control","private, no-store"),("X-Content-Type-Options","nosniff")])
            if (environ.get("REQUEST_METHOD") or "GET").upper()!="DELETE":
                return self._response(start,"405 Method Not Allowed",b"Method Not Allowed",[("Allow","DELETE"),("Cache-Control","no-store")])
            if not self._same_origin(environ): return self._response(start,"403 Forbidden",b"Forbidden",[("Cache-Control","no-store")])
            if (decision.user_id or "") not in self.document_uploaders: return self._response(start,"403 Forbidden",b"Forbidden",[("Cache-Control","no-store")])
            if self.document_store is None: return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
            try: self.document_store.remove(path.rsplit("/",1)[1])
            except DocumentError as error:
                return self._response(start,"400 Bad Request",str(error).encode("utf-8"),[("Content-Type","text/plain; charset=utf-8"),("Cache-Control","no-store")])
            return self._response(start,"204 No Content",headers=[("Cache-Control","no-store")])
        if path=="/api/chat/history":
            if (environ.get("REQUEST_METHOD") or "GET").upper()!="GET":
                return self._response(start,"405 Method Not Allowed",b"Method Not Allowed",[("Allow","GET"),("Cache-Control","no-store")])
            if self.chat_history is None:
                return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
            try: messages=self.chat_history.list(decision.user_id or "")
            except ChatHistoryError:
                return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
            body=json.dumps({"messages":messages},ensure_ascii=False,separators=(",", ":")).encode()
            return self._response(start,"200 OK",body,[("Content-Type","application/json"),("Cache-Control","private, no-store")])
        if path=="/api/chat":
            if (environ.get("REQUEST_METHOD") or "GET") != "POST":
                return self._response(start,"405 Method Not Allowed",b"Method Not Allowed",[("Allow","POST"),("Cache-Control","no-store")])
            if not self._same_origin(environ):
                return self._response(start,"403 Forbidden",b"Forbidden",[("Cache-Control","no-store")])
            if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
                return self._response(start,"400 Bad Request","请求格式不正确。".encode(),[("Content-Type","text/plain; charset=utf-8"),("Cache-Control","no-store")])
            try: length = int(environ.get("CONTENT_LENGTH") or "0")
            except ValueError: length = -1
            if length < 1 or length > 8192:
                return self._response(start,"400 Bad Request","请求内容长度不正确。".encode(),[("Content-Type","text/plain; charset=utf-8"),("Cache-Control","no-store")])
            try:
                value = json.loads(environ["wsgi.input"].read(length))
                message = value.get("message") if isinstance(value, dict) else None
            except (ValueError, UnicodeDecodeError): message = None
            if not isinstance(message, str) or not message.strip() or len(message.strip()) > 2000:
                return self._response(start,"400 Bad Request","请输入 1—2000 个字符的问题。".encode(),[("Content-Type","text/plain; charset=utf-8"),("Cache-Control","no-store")])
            if self.chat_client is None:
                return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
            if not self.chat_limiter.allow(decision.user_id or ""):
                return self._response(start,"429 Too Many Requests",b"Too Many Requests",[("Cache-Control","no-store"),("Retry-After","60")])
            try:
                context=self.document_store.context(message.strip()) if self.document_store else ""
                prompt=message.strip() if not context else message.strip()+"\n\n<DEK_SHARED_DOCUMENT_CONTEXT>\n"+context+"\n</DEK_SHARED_DOCUMENT_CONTEXT>"
                answer = self.chat_client.ask(decision.user_id or "", prompt)
            except DocumentError as error:
                return self._response(start,"400 Bad Request",str(error).encode("utf-8"),[("Content-Type","text/plain; charset=utf-8"),("Cache-Control","no-store")])
            except ChatUnavailable:
                return self._response(start,"502 Bad Gateway",b"Bad Gateway",[("Cache-Control","no-store")])
            try:
                if self.chat_history is not None: self.chat_history.append_exchange(decision.user_id or "",message.strip(),answer)
            except ChatHistoryError:
                return self._response(start,"503 Service Unavailable",b"Unavailable",[("Cache-Control","no-store")])
            body=json.dumps({"answer":answer},ensure_ascii=False).encode()
            return self._response(start,"200 OK",body,[("Content-Type","application/json"),("Cache-Control","no-store")])
        if path in {"/shared-resources","/shared-resources/"}:
            if (environ.get("REQUEST_METHOD") or "GET").upper()!="GET":
                return self._response(start,"405 Method Not Allowed",b"Method Not Allowed",[("Allow","GET"),("Cache-Control","no-store")])
            return self._response(start,"200 OK",shared_resources_page(),[("Content-Type","text/html; charset=utf-8"),("Cache-Control","no-store"),("X-Content-Type-Options","nosniff"),("Content-Security-Policy","default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'self'")])
        relative="index.html" if path=="/" else path.lstrip("/")
        root=root.resolve()
        target=(root/relative).resolve()
        if path in FRONTEND_ASSETS:
            # CSS/JS are installed code, not reviewed content: serve them from
            # the deployed package so a deploy + restart updates them without
            # waiting for a content release to be built and activated.
            root=FRONTEND_ASSET_DIR; target=FRONTEND_ASSET_DIR/path.rsplit("/",1)[1]
        if root not in target.parents or not target.is_file(): return self._response(start,"404 Not Found",b"Not found")
        body=target.read_bytes(); content=mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        cache_control="no-store" if target.suffix.lower() in {".html", ".js", ".css"} else "private, max-age=60"
        return self._response(start,"200 OK",body,[("Content-Type",content),("Cache-Control",cache_control),("X-Content-Type-Options","nosniff"),("Content-Security-Policy","default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'self'")],pin=pin)


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    p=argparse.ArgumentParser();p.add_argument('--site-root',type=Path);p.add_argument('--active',type=Path);p.add_argument('--releases',type=Path);p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=9120);a=p.parse_args()
    import os
    required=['DINGTALK_CLIENT_ID','DINGTALK_CLIENT_SECRET','DINGTALK_AGENT_ID','DEK_WEB_REDIRECT_URI','DEK_WEB_CLAIM_SECRET','API_SERVER_KEY']
    missing=[k for k in required if not os.environ.get(k)]
    if missing: raise SystemExit('missing required environment variables: '+','.join(missing))
    secret=os.environ['DEK_WEB_CLAIM_SECRET'].encode()
    client=make_dingtalk_client(os.environ)
    gateway=DingTalkGateway(os.environ['DINGTALK_CLIENT_ID'],os.environ['DEK_WEB_REDIRECT_URI'],secret,client,MemoryStateStore())
    if a.active and a.releases:
        site = ActiveSite(a.active, a.releases); generation = {}
    elif a.site_root:
        site = a.site_root; generation = {}
    else: raise SystemExit("--active and --releases are required")
    chat=HermesChatClient(os.environ['API_SERVER_KEY'],secret)
    history=ChatHistoryStore(Path(os.environ.get("DEK_CHAT_HISTORY_DB","/var/lib/dek-web/chat-history/history.sqlite3")),secret)
    try: documents=store_from_environment(os.environ)
    except DocumentError as error: raise SystemExit(str(error)) from error
    uploaders=frozenset(value.strip() for value in os.environ.get("DEK_DOCUMENT_UPLOADER_IDS","").split(",") if value.strip())
    if documents is not None and not uploaders: raise SystemExit("Qiniu documents require DEK_DOCUMENT_UPLOADER_IDS")
    app=KnowledgeApp(site,gateway,secret,generation_proof_secret=generation_proof_secret(os.environ),chat_client=chat,chat_history=history,document_store=documents,document_uploaders=uploaders,internal_api_key=os.environ['API_SERVER_KEY'])
    with make_server(a.host,a.port,app,server_class=ThreadingWSGIServer) as server: server.serve_forever()

if __name__=='__main__': main()
