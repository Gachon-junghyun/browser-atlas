"""The atlas: which sites exist, what access level they have, where to go inside them."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

ACCESS = ("open", "session", "api", "blocked")
DEFAULT_MAP = Path(__file__).resolve().parent.parent / "atlas" / "sites.json"


def map_path(path=None) -> Path:
    return Path(path or os.environ.get("BROWSER_ATLAS_MAP") or DEFAULT_MAP)


def _home_file(name: str) -> Path:
    from ._mouse import home
    return home() / name


def overlay_path() -> Path:
    """Your own additions (private sites, login recipes): never part of the shared map."""
    return _home_file("sites.local.json")


def learned_path() -> Path:
    """Map fixes this machine learned from use (renamed and new controls), applied automatically."""
    return _home_file("sites.learned.json")


def community_path() -> Path:
    """Map fixes downloaded from the shared database."""
    return _home_file("sites.community.json")


def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_base(path=None) -> dict:
    """The reviewed map from the repository only, without any local layer."""
    raw = json.loads(map_path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load(path=None) -> dict:
    """Layers, later wins: repository map, your overlay, community fixes, your learned fixes."""
    sites = json.loads(json.dumps(load_base(path)))
    if path is not None:
        return sites
    for k, v in _read(overlay_path(), {}).items():
        if not k.startswith("_") and isinstance(v, dict):
            sites[k] = {**sites.get(k, {}), **v}
    apply_ops(sites, _read(community_path(), []))
    apply_ops(sites, _read(learned_path(), []))
    return sites


def _bare(url: str) -> str:
    u = urlparse(url or "")
    return f"{u.scheme}://{u.netloc}{u.path}"


def page_matches(template: str, url: str) -> bool:
    """Is `url` the page a map entry describes? Same host, same path, `{placeholders}` match one segment."""
    import re
    t, u = urlparse(template or ""), urlparse(url or "")
    th, uh = (t.hostname or "").removeprefix("www."), (u.hostname or "").removeprefix("www.")
    if not th or th != uh:
        return False
    pat = re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(t.path.rstrip("/")))
    return re.fullmatch(pat, u.path.rstrip("/")) is not None


def apply_ops(sites: dict, ops: list) -> dict:
    """Apply map fixes: {"kind": "rename", site, role, from, to} or {"kind": "new", site, role, name, url}."""
    for op in ops if isinstance(ops, list) else []:
        e = sites.get(op.get("site", ""))
        if not isinstance(e, dict) or op.get("kind") not in ("rename", "new"):
            continue
        pages = e.setdefault("pages", {})
        if op["kind"] == "rename":
            for key, pg in pages.items():
                if op.get("page") and key != op["page"]:
                    continue                            # a rename belongs to one page
                for c in pg.get("controls", []):
                    if c.get("role") == op.get("role") and c.get("name") == op.get("from"):
                        c["name"], c["learned"] = op.get("to", c["name"]), True
            continue
        key = next((k for k, pg in pages.items() if _bare(pg.get("url", "")) == _bare(op.get("url", ""))), None)
        if key is None:
            key = "auto:" + (urlparse(op.get("url", "")).path or "/")
            pages[key] = {"url": op.get("url", ""), "controls": []}
        ctrls = pages[key].setdefault("controls", [])
        if not any(c.get("role") == op.get("role") and c.get("name") == op.get("name") for c in ctrls):
            ctrls.append({"role": op.get("role"), "name": op.get("name"), "where": "", "act": "", "does": "",
                          "learned": True})
    return sites


def hosts_of(entry: dict) -> list[str]:
    """Hostnames an entry covers: explicit `hosts`, else the hosts of its `go` URLs."""
    hs = [h.lower().lstrip(".") for h in entry.get("hosts") or []]
    if not hs:
        hs = [urlparse(u).hostname or "" for u in (entry.get("go") or {}).values()]
        hs = [h[4:] if h.startswith("www.") else h for h in hs if h]
    return hs


def site_for(url: str, sites: dict | None = None) -> tuple[str, dict] | None:
    """The atlas entry whose hosts cover `url` (subdomains included), longest match first."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    sites = sites if sites is not None else load()
    best = None
    for key, e in sites.items():
        for h in hosts_of(e):
            if h and (host == h or host.endswith("." + h)) and (best is None or len(h) > best[0]):
                best = (len(h), key, e)
    return (best[1], best[2]) if best else None


class Blocked(PermissionError):
    pass


def guard(url: str, sites: dict | None = None) -> None:
    """Raise `Blocked` if `url` is on a site marked `access: blocked`."""
    hit = site_for(url, sites)
    if hit and hit[1].get("access") == "blocked":
        key, e = hit
        msg = f"{key} is access=blocked in the atlas: {e.get('why', '')}"
        if e.get("prefer"):
            msg += f" (instead: {e['prefer']})"
        raise Blocked(msg)
