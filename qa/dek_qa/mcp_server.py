from __future__ import annotations

import argparse
import json
import re
import sys
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
]


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
    if not isinstance(params, dict) or set(params) != {"name", "arguments"}:
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
        if request.get("params") not in (None, {}):
            return _error(request_id, -32602, "invalid method parameters")
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params")
        if (
            not isinstance(params, dict)
            or set(params) != {"name", "arguments"}
            or not isinstance(params.get("name"), str)
            or not isinstance(params.get("arguments"), dict)
        ):
            return _error(request_id, -32602, "invalid tool parameters")
        if params["name"] not in {"dek_kb_search", "dek_kb_get"}:
            return _error(request_id, -32601, "tool not found")
        try:
            name, arguments = _validated_tool_call(params)
        except ValueError:
            return _tool_error(request_id, params["name"])
        if name == "dek_kb_search":
            value = kb.dek_kb_search(arguments.get("query", ""), arguments.get("limit", 5))
        elif name == "dek_kb_get":
            value = kb.dek_kb_get(arguments.get("document_id", ""))
        result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, required=True)
    args = parser.parse_args()
    kb = KnowledgeBase(args.index)
    for line in sys.stdin:
        try:
            response = _reply(json.loads(line), kb)
        except json.JSONDecodeError:
            response = _error(None, -32700, "parse error")
        except (ValueError, TypeError):
            response = _error(None, -32600, "invalid request")
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
