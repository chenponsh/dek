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
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote
from wsgiref.simple_server import make_server

from .auth import authorize_claim
from .dingtalk_gateway import DingTalkClient, DingTalkGateway, LoginError, MemoryStateStore


auth_logger = logging.getLogger("web.auth")

SIGNED_OUT_PAGE = """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"robots\" content=\"noindex,nofollow\"><title>已退出 · DEK</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f8f9;color:#232323;font:16px/1.6 system-ui,sans-serif}.card{width:min(420px,calc(100% - 40px));padding:38px;text-align:center;background:#fff;border:1px solid rgba(35,35,35,.12);border-top:3px solid #00c5ce;border-radius:12px;box-shadow:0 16px 50px rgba(0,0,0,.08)}h1{font-size:26px;margin:0 0 8px}p{color:#6f6f6f;margin:0 0 25px}a{display:inline-block;padding:9px 20px;border-radius:7px;background:#007c84;color:#fff;text-decoration:none}</style></head><body><main class=\"card\"><h1>已安全退出</h1><p>当前知识库会话已清除。</p><a href=\"/\">重新登录</a></main></body></html>""".encode()


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
    def __init__(self, site_root: Path, gateway: DingTalkGateway, claim_secret: bytes, clock=time.time, generation_proof_secret: str = "", boot_nonce: str = "", release_sha256: str = ""):
        self.active_site = site_root if isinstance(site_root, ActiveSite) else None
        self.root = None if self.active_site else Path(site_root).resolve(strict=True)
        self.gateway=gateway; self.secret=claim_secret; self.clock=clock
        self.generation_proof_secret = generation_proof_secret
        self.boot_nonce = boot_nonce
        self.release_sha256 = release_sha256
    def _response(self,start,status,body=b"",headers=(),pin=None):
        start(status,[("Content-Length",str(len(body))),*headers]); return _PinnedResponse(body,pin) if pin else [body]
    def __call__(self,environ,start):
        path=decode_request_path(environ.get("PATH_INFO") or "/")
        if path=="/__ready":
            if (environ.get("REQUEST_METHOD") or "GET")!="GET":
                return self._response(start,"405 Method Not Allowed",b"Method Not Allowed",[("Allow","GET")])
            body=json.dumps({"status":"ready","oauth_callback":self.gateway.redirect_uri},separators=(",",":")).encode()
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


def main():
    p=argparse.ArgumentParser();p.add_argument('--site-root',type=Path);p.add_argument('--active',type=Path);p.add_argument('--releases',type=Path);p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=9120);a=p.parse_args()
    import os
    required=['DINGTALK_CLIENT_ID','DINGTALK_CLIENT_SECRET','DINGTALK_AGENT_ID','DEK_WEB_REDIRECT_URI','DEK_WEB_CLAIM_SECRET']
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
    with make_server(a.host,a.port,KnowledgeApp(site,gateway,secret,generation_proof_secret=generation_proof_secret(os.environ))) as server: server.serve_forever()

if __name__=='__main__': main()
