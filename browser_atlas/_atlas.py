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


def overlay_path() -> Path:
    """Your own additions (private sites, login recipes): never part of the shared map."""
    from ._mouse import home
    return home() / "sites.local.json"


def load(path=None) -> dict:
    raw = json.loads(map_path(path).read_text(encoding="utf-8"))
    sites = {k: v for k, v in raw.items() if not k.startswith("_")}
    local = overlay_path()
    if path is None and local.exists():
        for k, v in json.loads(local.read_text(encoding="utf-8")).items():
            if k.startswith("_") or not isinstance(v, dict):
                continue
            sites[k] = {**sites.get(k, {}), **v}
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
