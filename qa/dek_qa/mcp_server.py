from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .index import KnowledgeBase

TOOLS = [
    {
        "name": "dek_kb_search",
        "description": "Search only the reviewed dek wiki index and return citation metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dek_kb_get",
        "description": "Read one reviewed wiki document by an opaque ID returned by dek_kb_search.",
        "inputSchema": {
            "type": "object",
            "properties": {"document_id": {"type": "string", "pattern": "^[0-9a-f]{24}$"}},
            "required": ["document_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dek_kb_recent",
        "description": (
            "List recent reviewed information: recent_publications is based on source "
            "publication dates, while knowledge_base_updates is based on formal wiki Git "
            "last-commit timestamps."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
    },
]


def _read_opened_index(index_path: Path, maximum: int = 128 * 1024 * 1024) -> tuple[Path, bytes]:
    descriptor = os.open(index_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
            raise ValueError("index is not a bounded regular file")
        resolved = Path(f"/proc/self/fd/{descriptor}").resolve(strict=True)
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(raw)))
            if not chunk: return resolved, bytes(raw)
            raw.extend(chunk)
        raise ValueError("index is too large")
    finally: os.close(descriptor)


def load_knowledge_base(index_path: Path, proof_path: Path | None = None, *, boot_nonce: str = "", release_sha256: str = "", pid: int | None = None) -> tuple[KnowledgeBase, bytes]:
    resolved, raw = _read_opened_index(index_path)
    kb = KnowledgeBase.from_bytes(raw)
    if proof_path is None: return kb, raw
    digest = hashlib.sha256(raw).hexdigest()
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=proof_path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"index_path": str(resolved), "release": str(resolved.parent), "index_sha256": digest,
                       "release_sha256": release_sha256, "boot_nonce": boot_nonce, "pid": pid or os.getpid()}, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, proof_path); temporary = None
        descriptor = os.open(proof_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return kb, raw


def write_generation_proof(index_path: Path, proof_path: Path) -> None:
    """Compatibility wrapper; production uses the live load path below."""
    load_knowledge_base(index_path, proof_path, boot_nonce=secrets.token_urlsafe(24))


class ActiveIndex:
    """Pollable index holder; a failed candidate never displaces last-known-good."""
    def __init__(self, active_path: Path, releases_root: Path, proof_path: Path | None = None):
        self.active_path = Path(active_path)
        self.releases_root = Path(releases_root).resolve(strict=True)
        self.proof_path = Path(proof_path) if proof_path else None
        self._lock = threading.Lock()
        self._kb: KnowledgeBase | None = None
        self._generation: dict | None = None
        if not self.refresh(): raise ValueError("no valid active index")

    def _candidate(self) -> tuple[KnowledgeBase, dict]:
        # Matches activator.py's own _read_active() bound: a real active.json
        # lists one SHA-256 digest per static site artifact and can
        # legitimately run into the hundreds of kilobytes for a real release.
        maximum = 8 * 1024 * 1024
        descriptor = os.open(self.active_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size > maximum: raise ValueError("invalid active metadata")
            raw = bytearray()
            while len(raw) < details.st_size:
                chunk = os.read(descriptor, details.st_size - len(raw))
                if not chunk: break
                raw.extend(chunk)
            active = json.loads(raw)
        finally: os.close(descriptor)
        name = active.get("generation")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]{2,160}", name) is None: raise ValueError("invalid generation")
        release = (self.releases_root/name).resolve(strict=True)
        if release.parent != self.releases_root or release.is_symlink(): raise ValueError("generation escapes release root")
        if json.loads((release/"release.json").read_text(encoding="utf-8")) != active: raise ValueError("release metadata mismatch")
        index = release/"dek-kb.json"; kb, raw = load_knowledge_base(index)
        if hashlib.sha256(raw).hexdigest() != active.get("artifacts",{}).get("dek-kb.json"): raise ValueError("index digest mismatch")
        return kb, active

    def refresh(self) -> bool:
        try: candidate, generation = self._candidate()
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError): return False
        with self._lock:
            self._kb, self._generation = candidate, generation
            if self.proof_path:
                proof = dict(generation); proof["pid"] = os.getpid()
                self.proof_path.parent.mkdir(parents=True, exist_ok=True)
                temporary=None
                try:
                    with tempfile.NamedTemporaryFile("w",encoding="utf-8",dir=self.proof_path.parent,delete=False) as handle:
                        temporary=Path(handle.name); json.dump(proof,handle,sort_keys=True,separators=(",",":")); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
                    os.chmod(temporary,0o640); os.replace(temporary,self.proof_path); temporary=None
                    descriptor=os.open(self.proof_path.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
                    try: os.fsync(descriptor)
                    finally: os.close(descriptor)
                finally:
                    if temporary is not None: temporary.unlink(missing_ok=True)
        return True

    def current(self) -> KnowledgeBase:
        with self._lock:
            if self._kb is None: raise ValueError("no active index")
            return self._kb

    @property
    def generation(self) -> dict:
        with self._lock: return dict(self._generation or {})

    def watch(self, stop: threading.Event, interval: float = 1.0) -> None:
        while not stop.wait(interval): self.refresh()


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_error(request_id: object, tool_name: str) -> dict[str, Any]:
    result = {
        "content": [
            {"type": "text", "text": f"Invalid arguments for {tool_name}"}
        ],
        "isError": True,
    }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _validated_tool_call(params: object) -> tuple[str, dict[str, Any]]:
    if (
        not isinstance(params, dict)
        or not {"name", "arguments"}.issubset(params)
        or not set(params).issubset({"name", "arguments", "_meta"})
        or (params.get("_meta") is not None and not isinstance(params.get("_meta"), dict))
    ):
        raise ValueError("invalid tool parameters")
    name = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise ValueError("invalid tool parameters")
    if name == "dek_kb_search":
        if not set(arguments).issubset({"query", "limit"}) or "query" not in arguments:
            raise ValueError("invalid search arguments")
        query = arguments["query"]
        limit = arguments.get("limit", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError("invalid search query")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("invalid search limit")
    elif name == "dek_kb_get":
        if set(arguments) != {"document_id"}:
            raise ValueError("invalid get arguments")
        document_id = arguments["document_id"]
        if not isinstance(document_id, str) or re.fullmatch(r"[0-9a-f]{24}", document_id) is None:
            raise ValueError("invalid document id")
    elif name == "dek_kb_recent":
        if not set(arguments).issubset({"days", "limit"}):
            raise ValueError("invalid recent arguments")
        days = arguments.get("days", 7)
        limit = arguments.get("limit", 20)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValueError("invalid recent days")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("invalid recent limit")
    else:
        raise ValueError("unknown tool")
    return name, arguments


def _reply(request: object, kb: KnowledgeBase) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return _error(None, -32600, "invalid request")
    request_id = request.get("id")
    if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return _error(request_id, -32600, "invalid request")
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "dek-kb-readonly", "version": "1.0.0"}}
    elif method == "tools/list":
        params = request.get("params")
        if (
            params is not None
            and (
                not isinstance(params, dict)
                or not set(params).issubset({"cursor", "_meta"})
                or (
                    params.get("cursor") is not None
                    and not isinstance(params.get("cursor"), str)
                )
                or (
                    params.get("_meta") is not None
                    and not isinstance(params.get("_meta"), dict)
                )
            )
        ):
            return _error(request_id, -32602, "invalid method parameters")
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params")
        if (
            not isinstance(params, dict)
            or not {"name", "arguments"}.issubset(params)
            or not set(params).issubset({"name", "arguments", "_meta"})
            or not isinstance(params.get("name"), str)
            or not isinstance(params.get("arguments"), dict)
            or (params.get("_meta") is not None and not isinstance(params.get("_meta"), dict))
        ):
            return _error(request_id, -32602, "invalid tool parameters")
        if params["name"] not in {"dek_kb_search", "dek_kb_get", "dek_kb_recent"}:
            return _error(request_id, -32601, "tool not found")
        try:
            name, arguments = _validated_tool_call(params)
        except ValueError:
            return _tool_error(request_id, params["name"])
        if name == "dek_kb_search":
            value = kb.dek_kb_search(arguments.get("query", ""), arguments.get("limit", 5))
        elif name == "dek_kb_get":
            value = kb.dek_kb_get(arguments.get("document_id", ""))
        elif name == "dek_kb_recent":
            value = kb.dek_kb_recent(
                days=arguments.get("days", 7), limit=arguments.get("limit", 20)
            )
        result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path)
    parser.add_argument("--active", type=Path)
    parser.add_argument("--releases", type=Path)
    parser.add_argument("--generation-proof", type=Path)
    parser.add_argument("--boot-generation", type=Path)
    args = parser.parse_args()
    live = ActiveIndex(args.active,args.releases,args.generation_proof) if args.active and args.releases else None
    boot_nonce = ""
    if live is None and args.boot_generation:
        generation = json.loads(args.boot_generation.read_text(encoding="utf-8"))
        boot_nonce = generation.get("boot_nonce", "")
    if live is None:
        if args.index is None: raise SystemExit("--active/--releases or --index is required")
        kb, _loaded = load_knowledge_base(args.index, args.generation_proof, boot_nonce=boot_nonce, release_sha256=generation.get("release_sha256", "") if args.boot_generation else "")
    stop=threading.Event(); watcher=None
    if live is not None:
        watcher=threading.Thread(target=live.watch,args=(stop,),name="dek-index-watch",daemon=True); watcher.start()
    try:
        for line in sys.stdin:
            try:
                if live is not None: live.refresh()
                response = _reply(json.loads(line), live.current() if live is not None else kb)
            except json.JSONDecodeError:
                response = _error(None, -32700, "parse error")
            except (ValueError, TypeError):
                response = _error(None, -32600, "invalid request")
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        stop.set()
        if watcher is not None: watcher.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
