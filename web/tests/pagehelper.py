"""Rebuild a published page the way the browser does, so tests can check what people see."""
import json
import re
import subprocess
from pathlib import Path

PAGE_JS = Path(__file__).parents[1] / "assets" / "page.js"
_DATA = re.compile(r'<script type="application/json" id="page-data">(.*?)</script>', re.S)
_BODY = re.compile(r'<template id="page-body">(.*)</template>', re.S)


def page_parts(html: str):
    data, body = _DATA.search(html), _BODY.search(html)
    if not data or not body:
        return None
    return json.loads(data.group(1)), body.group(1)


def rendered_page(path) -> str:
    """The page at `path` as the browser shows it (page.js drawing the frame around the content)."""
    html = Path(path).read_text(encoding="utf-8")
    parts = page_parts(html)
    if parts is None:
        return html
    script = (f"const p=require({json.dumps(str(PAGE_JS))});const i=JSON.parse(require('fs').readFileSync(0,'utf8'));"
              "process.stdout.write(p.pageHtml(i.data,i.body));")
    result = subprocess.run(["node", "-e", script], input=json.dumps({"data": parts[0], "body": parts[1]}), check=True, text=True, capture_output=True)
    return html.split("<body>", 1)[0] + "<body>" + result.stdout + "</body></html>"
