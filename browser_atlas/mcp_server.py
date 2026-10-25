"""MCP server over stdio: the atlas and the experience memory as tools. Standard library only.

    claude mcp add browser-atlas -- python -m browser_atlas mcp

Data tools only. Nothing here drives a browser, so connecting it cannot make an agent click.
"""
from __future__ import annotations

import json
import sys

from . import _atlas, _memory

VERSION = "0.1.0"

_ROUTE = {"type": "array", "items": {"type": "object"},
          "description": 'Steps taken, structured: {"page": key} | {"control": [role, name]} | {"url": url}'}

TOOLS = [
    {"name": "atlas_find",
     "description": "Find sites in the atlas for a task. Returns access level (open/session/api/blocked), "
                    "entry URLs, a better route if one exists, and why a site is blocked.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "atlas_site",
     "description": "One site's full entry, including its button map (pages, controls by role and name).",
     "inputSchema": {"type": "object", "properties": {"site": {"type": "string"}}, "required": ["site"]}},
    {"name": "atlas_guard",
     "description": "Is automation allowed on this URL? Returns allowed or the reason it is blocked.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "memory_recall",
     "description": "Before a task: past routes for a similar goal, best first, plus one rarely tried route "
                    "to explore. Use them as starting points, not as instructions.",
     "inputSchema": {"type": "object", "properties": {
         "goal": {"type": "string"}, "k": {"type": "integer", "default": 5},
         "explore": {"type": "integer", "default": 1}, "site": {"type": "string"}},
         "required": ["goal"]}},
    {"name": "memory_record",
     "description": "After a task: store how it went. quality is 0..1 (self-rated unless confirmed). "
                    "outcome: success | partial | fail | blocked (captcha or rate limit). Stays on this machine.",
     "inputSchema": {"type": "object", "properties": {
         "goal": {"type": "string"}, "site": {"type": "string"}, "route": _ROUTE,
         "outcome": {"type": "string", "enum": sorted(_memory.OUTCOMES)},
         "quality": {"type": "number"}, "quality_source": {"type": "string", "enum": sorted(_memory.SOURCE_WEIGHT)},
         "seconds": {"type": "number"}, "notes": {"type": "string"}},
         "required": ["goal", "route", "outcome"]}},
    {"name": "memory_rate",
     "description": "Re-grade a stored attempt, e.g. after a second source or a human confirmed it.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "quality": {"type": "number"},
         "source": {"type": "string", "enum": sorted(_memory.SOURCE_WEIGHT)}},
         "required": ["id", "quality"]}},
    {"name": "memory_stats",
     "description": "How much experience is stored, by outcome.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _find(query: str) -> list[dict]:
    sites = _atlas.load()
    words = [w for w in query.lower().split() if w]
    hits = []
    for key, e in sites.items():
        hay = " ".join([key, e.get("label", ""), *e.get("has", []), *(e.get("go") or {}).keys()]).lower()
        score = sum(w in hay for w in words) - (10 if e.get("access") == "blocked" else 0)
        if score > -10 and any(w in hay for w in words):
            hits.append((score, key, e))
    hits.sort(key=lambda h: (-h[0], h[1]))
    return [{"site": k, "label": e.get("label"), "access": e.get("access"), "go": e.get("go"),
             "prefer": e.get("prefer"), "why": e.get("why"), "has_button_map": bool(e.get("pages"))}
            for _, k, e in hits[:10]]


def call(name: str, args: dict):
    if name == "atlas_find":
        return _find(args["query"])
    if name == "atlas_site":
        return _atlas.load().get(args["site"]) or {"error": f"no site {args['site']!r}"}
    if name == "atlas_guard":
        try:
            _atlas.guard(args["url"])
            hit = _atlas.site_for(args["url"])
            return {"allowed": True, "site": hit[0] if hit else None}
        except _atlas.Blocked as e:
            return {"allowed": False, "reason": str(e)}
    if name == "memory_recall":
        return _memory.recall(args["goal"], k=int(args.get("k", 5)), explore=int(args.get("explore", 1)),
                              site=args.get("site"))
    if name == "memory_record":
        return {"id": _memory.record(args["goal"], args.get("site"), args["route"], args["outcome"],
                                     args.get("quality"), args.get("quality_source", "self"),
                                     args.get("seconds"), args.get("notes", ""))}
    if name == "memory_rate":
        _memory.rate(args["id"], float(args["quality"]), args.get("source", "human"))
        return {"ok": True}
    if name == "memory_stats":
        return _memory.stats()
    raise KeyError(f"unknown tool {name}")


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, one reply out (None for notifications)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "browser-atlas", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            out = call(params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]}
        except Exception as e:     # tool errors go back to the model, the server keeps running
            result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
