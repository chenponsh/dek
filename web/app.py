"""Minimal fail-closed WSGI server for the generated internal site."""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import secrets
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote
from wsgiref.simple_server import make_server

from .auth import authorize_claim
from .dingtalk_gateway import DingTalkClient, DingTalkGateway, LoginError, MemoryStateStore


auth_logger = logging.getLogger("web.auth")

SIGNED_OUT_PAGE = """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta name=\"robots\" content=\"noindex,nofollow\"><title>已退出 · DEK</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f6f3;color:#2f2e2b;font:16px/1.6 system-ui,sans-serif}.card{width:min(420px,calc(100% - 40px));padding:38px;text-align:center;background:#fff;border:1px solid rgba(55,53,47,.12);border-radius:12px;box-shadow:0 16px 50px rgba(0,0,0,.08)}h1{font-size:26px;margin:0 0 8px}p{color:#78736d;margin:0 0 25px}a{display:inline-block;padding:9px 20px;border-radius:7px;background:#0075de;color:#fff;text-decoration:none}</style></head><body><main class=\"card\"><h1>已安全退出</h1><p>当前知识库会话已清除。</p><a href=\"/\">重新登录</a></main></body></html>""".encode()


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


class KnowledgeApp:
    def __init__(self, site_root: Path, gateway: DingTalkGateway, claim_secret: bytes, clock=time.time):
        self.root=Path(site_root).resolve(); self.gateway=gateway; self.secret=claim_secret; self.clock=clock
    def _response(self,start,status,body=b"",headers=()):
        start(status,[("Content-Length",str(len(body))),*headers]); return [body]
    def __call__(self,environ,start):
        path=decode_request_path(environ.get("PATH_INFO") or "/")
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
        target=(self.root/relative).resolve()
        if self.root not in target.parents or not target.is_file(): return self._response(start,"404 Not Found",b"Not found")
        body=target.read_bytes(); content=mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        cache_control="no-store" if target.suffix.lower() in {".html", ".js", ".css"} else "private, max-age=60"
        return self._response(start,"200 OK",body,[("Content-Type",content),("Cache-Control",cache_control),("X-Content-Type-Options","nosniff"),("Content-Security-Policy","default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'self'")])


def main():
    p=argparse.ArgumentParser();p.add_argument('--site-root',type=Path,required=True);p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=9120);a=p.parse_args()
    import os
    required=['DINGTALK_CLIENT_ID','DINGTALK_CLIENT_SECRET','DINGTALK_AGENT_ID','DEK_WEB_REDIRECT_URI','DEK_WEB_CLAIM_SECRET']
    missing=[k for k in required if not os.environ.get(k)]
    if missing: raise SystemExit('missing required environment variables: '+','.join(missing))
    secret=os.environ['DEK_WEB_CLAIM_SECRET'].encode()
    client=make_dingtalk_client(os.environ)
    gateway=DingTalkGateway(os.environ['DINGTALK_CLIENT_ID'],os.environ['DEK_WEB_REDIRECT_URI'],secret,client,MemoryStateStore())
    with make_server(a.host,a.port,KnowledgeApp(a.site_root,gateway,secret)) as server: server.serve_forever()

if __name__=='__main__': main()
