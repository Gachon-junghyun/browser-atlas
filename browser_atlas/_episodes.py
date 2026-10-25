"""The loop that runs even when the agent forgets it.

The MCP server routes every browser tool call through a `Recorder`. One tab is one episode:
what was opened, which controls were used, what failed, whether content was read. The agent
does not have to call anything:

- **record**: when the tab is closed, idles for 15 minutes, or the server exits, the episode is
  judged and written to the experience memory.
- **judge**: from signals, not self-assessment. A captcha, a rate limit or a blocked site is
  `blocked`; never reading any content is `fail`; ending on an error is `partial`; otherwise
  `success`. Errors and wasted steps lower the quality. A human or the agent can re-grade later.
- **recall**: `browser_open` returns the best past routes for that site and goal.
- **repair proposals**: a mapped control that is missing, and then succeeds under another name of
  the same role, becomes a rename proposal; a control that worked but is not in the map becomes a
  new-control proposal. Proposals wait for a human (`python -m browser_atlas proposals apply N`),
  because the map steers every agent that reads it.

Only search-field input is kept as keywords, never what was typed into other fields.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse, urlunparse

from . import _atlas, _memory
from ._mouse import home

IDLE_SECONDS = 900
_CAPTCHA = re.compile(r"captcha|recaptcha|hcaptcha|turnstile|보안\s*확인|자동입력\s*방지|unusual traffic|"
                      r"rate limit|too many requests|비정상(적인)?\s*접근|\b429\b", re.I)
BASE_QUALITY = {"success": 0.8, "partial": 0.5, "fail": 0.1, "blocked": 0.0}


def _bare(url: str) -> str:
    u = urlparse(url or "")
    return urlunparse((u.scheme, u.netloc, u.path, "", "", ""))


def looks_blocked(text: str) -> bool:
    return bool(_CAPTCHA.search(text or ""))


def judge(ep: dict) -> tuple[str, float]:
    """Outcome and quality from what happened in the episode."""
    if ep["blocked"]:
        outcome = "blocked"
    elif ep["reads"] == 0:
        outcome = "fail"
    elif ep["steps"] and not ep["steps"][-1]["ok"]:
        outcome = "partial"
    elif ep["needs_human"]:
        outcome = "partial"
    else:
        outcome = "success"
    errors = sum(1 for s in ep["steps"] if not s["ok"])
    extra = max(0, len(ep["steps"]) - 8)
    q = BASE_QUALITY[outcome] - 0.05 * min(errors, 6) - 0.01 * min(extra, 20)
    return outcome, round(max(0.0, min(1.0, q)), 3)


def _open_path():
    return home() / "episodes_open.json"


class Recorder:
    """Open episodes are mirrored to disk after every change: a server that is killed (no atexit)
    leaves them there, and the next server finishes them as 'recovered'."""

    def __init__(self, idle: float = IDLE_SECONDS, recover: bool = True):
        self.idle = idle
        self.open: dict[str, dict] = {}
        if recover:
            try:
                self.open = json.loads(_open_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.open = {}
            self.finish_all("recovered")

    def _save(self) -> None:
        try:
            _open_path().parent.mkdir(parents=True, exist_ok=True)
            _open_path().write_text(json.dumps(self.open, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def current_url(self, tab: str) -> str:
        route = (self.open.get(tab) or {}).get("route", [])
        return next((s["url"] for s in reversed(route) if "url" in s), "")

    # -- episode life --
    def start(self, tab: str, url: str, goal: str | None = None) -> dict:
        hit = _atlas.site_for(url)
        self.open[tab] = {"tab": tab, "goal": goal, "site": hit[0] if hit else None,
                          "host": urlparse(url).hostname or "", "title": "", "started": time.time(),
                          "last": time.time(), "steps": [], "route": [{"url": _bare(url)}],
                          "reads": 0, "blocked": False, "needs_human": False, "searches": [],
                          "missing": []}
        self._save()
        return self.open[tab]

    def step(self, tab: str, tool: str, ok: bool, error: Exception | None = None,
             control: list | None = None, css: str | None = None, result=None, url: str = "") -> None:
        ep = self.open.get(tab)
        if ep is None:
            return
        ep["last"] = time.time()
        err = f"{type(error).__name__}: {error}"[:200] if error else ""
        ep["steps"].append({"tool": tool, "ok": ok, "error": err, "t": round(time.time() - ep["started"], 1)})
        name = type(error).__name__ if error else ""
        if name in ("Blocked",) or looks_blocked(err):
            ep["blocked"] = True
        if name == "NeedsHuman":
            ep["needs_human"] = True
        if name == "NoControl" and control:
            ep["missing"].append({"role": control[0], "name": control[1], "nearest": getattr(error, "nearest", []),
                                  "url": _bare(url)})
        if not ok:
            self._save()
            return
        if tool == "browser_read":
            ep["reads"] += 1
            if looks_blocked(json.dumps(result, ensure_ascii=False)[:20000] if result else ""):
                ep["blocked"] = True
        if tool in ("browser_click", "browser_type"):
            ep["route"].append({"control": list(control)} if control else {"css": css})
            if control:
                self._propose_from_success(ep, control, url)
            if tool == "browser_type" and isinstance(result, dict) and result.get("search"):
                ep["searches"].append(result.get("value", "")[:80])
        if url and _bare(url) != self.current_url(tab) and tool == "browser_click":
            ep["route"].append({"url": _bare(url)})
        self._save()

    def finish(self, tab: str, reason: str = "closed") -> dict | None:
        ep = self.open.pop(tab, None)
        self._save()
        if ep is None or not ep["steps"]:
            return None
        outcome, quality = judge(ep)
        site_label = (_atlas.load().get(ep["site"] or "", {}) or {}).get("label", "")
        goal = " ".join(x for x in (ep["goal"] or site_label, ep["host"] if not ep["goal"] else "",
                                     *ep["searches"]) if x)
        notes = json.dumps({"auto": True, "reason": reason, "steps": len(ep["steps"]),
                            "errors": [s["error"] for s in ep["steps"] if s["error"]][:5],
                            "searches": ep["searches"][:5]}, ensure_ascii=False)
        rid = _memory.record(goal, ep["site"], ep["route"], outcome,
                             quality, "self", round(ep["last"] - ep["started"], 1), notes)
        return {"id": rid, "outcome": outcome, "quality": quality, "steps": len(ep["steps"]),
                "route": ep["route"]}

    def sweep(self) -> list[dict]:
        """Finish episodes idle for too long."""
        now = time.time()
        stale = [t for t, ep in self.open.items() if now - ep["last"] > self.idle]
        return [r for r in (self.finish(t, "idle") for t in stale) if r]

    def finish_all(self, reason: str = "server exit") -> None:
        for t in list(self.open):
            self.finish(t, reason)

    # -- map repair --
    def _propose_from_success(self, ep: dict, control: list, url: str) -> None:
        role, name = control[0], control[1]
        site = ep["site"]
        if not site:
            return
        for miss in ep["missing"]:
            if miss["role"] == role and name in miss["nearest"] and miss["name"] != name:
                propose({"kind": "rename", "site": site, "role": role, "from": miss["name"], "to": name,
                         "url": miss["url"]})
        mapped = {(c.get("role"), c.get("name")) for pg in (_atlas.load().get(site, {}).get("pages") or {}).values()
                  for c in pg.get("controls", [])}
        if (role, name) not in mapped:
            propose({"kind": "new", "site": site, "role": role, "name": name, "url": _bare(url)})


# ── proposals (human-approved map changes) ──────────────────────────
def _prop_path():
    return home() / "map_proposals.json"


def proposals() -> list[dict]:
    try:
        return json.loads(_prop_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def propose(p: dict) -> None:
    items = proposals()
    key = json.dumps({k: p[k] for k in sorted(p) if k != "url"}, ensure_ascii=False)
    for it in items:
        if it["key"] == key:
            it["seen"] += 1
            it["last"] = time.strftime("%Y-%m-%d")
            break
    else:
        items.append({**p, "key": key, "seen": 1, "last": time.strftime("%Y-%m-%d")})
    _prop_path().parent.mkdir(parents=True, exist_ok=True)
    _prop_path().write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def apply(index: int) -> dict:
    """Human-side: write proposal `index` into the local overlay map, and drop it from the queue."""
    items = proposals()
    p = items.pop(index)
    path = _atlas.overlay_path()
    overlay = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    base = _atlas.load()
    entry = overlay.setdefault(p["site"], {})
    pages = entry.setdefault("pages", json.loads(json.dumps(base.get(p["site"], {}).get("pages") or {})))
    if p["kind"] == "rename":
        for pg in pages.values():
            for c in pg.get("controls", []):
                if c.get("role") == p["role"] and c.get("name") == p["from"]:
                    c["name"] = p["to"]
                    c["verified"] = time.strftime("%Y-%m-%d")
    else:
        key = next((k for k, pg in pages.items() if _bare(pg.get("url", "")) == p["url"]), None)
        if key is None:
            key = "auto:" + (urlparse(p["url"]).path or "/")
            pages[key] = {"url": p["url"], "controls": []}
        pages[key]["controls"].append({"role": p["role"], "name": p["name"], "where": "", "act": "",
                                       "does": "", "verified": time.strftime("%Y-%m-%d"), "learned": True})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overlay, ensure_ascii=False, indent=1), encoding="utf-8")
    _prop_path().write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
