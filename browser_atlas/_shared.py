"""The shared database: everyone reads, only the maintainer writes. Supabase over plain HTTPS.

Locally everything is personal first: experience rows in SQLite, learned map fixes in
`sites.learned.json`, both used at once on this machine. Nothing leaves until the human asks
("upload it to the shared DB"); then `preview()` shows exactly what would go and `upload()` sends it.

What can go: experience rows reduced by `_memory.shareable` (no goal text, no notes, no queries,
only sites in the repository map with `open`/`api` access, no PII-shaped strings) and learned map
fixes for those same public sites. Private sites in your overlay never go.

What comes back with `sync()`: community rows go into the local memory as owner "community"
(recall passes them through the atlas sanitizer), community map fixes into `sites.community.json`.

Configuration, `~/.browser_atlas/shared.json`:
    {"url": "https://<project>.supabase.co", "anon_key": "<public anon key>"}
Writing also needs the maintainer's login, stored with `set-cred shared_email` and
`set-cred shared_password`. Readers need only the url and the anon key.
Server tables and row-level security: `server/schema.sql`.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import _atlas, _episodes, _memory
from ._mouse import home

_FIELD = re.compile(r"^[\w .:/?&=%#@+\-()\[\]가-힣ㄱ-ㅎㅏ-ㅣ]{0,200}$")


class NotConfigured(RuntimeError):
    pass


def config() -> dict:
    p = home() / "shared.json"
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise NotConfigured(f"shared database is not configured: write {p} with url and anon_key "
                            "(server/schema.sql sets up the tables)")
    if not cfg.get("url") or not cfg.get("anon_key"):
        raise NotConfigured(f"{p} needs url and anon_key")
    return cfg


def _req(method: str, path: str, body=None, token: str | None = None, extra: dict | None = None):
    cfg = config()
    headers = {"apikey": cfg["anon_key"], "Authorization": f"Bearer {token or cfg['anon_key']}",
               "Content-Type": "application/json", **(extra or {})}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(cfg["url"].rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}")


def _token() -> str:
    """Maintainer sign-in. The password goes from the credential store to the request, nowhere else."""
    from . import _login
    body = {"email": _login._credential("shared_email"), "password": _login._credential("shared_password")}
    r = _req("POST", "/auth/v1/token?grant_type=password", body)
    return r["access_token"]


# ── what would leave ─────────────────────────────────────────────────
def _public_ops() -> list[dict]:
    public = _atlas.load_base()
    out = []
    for p in _episodes.proposals():
        if p.get("shared") or p.get("site") not in public:
            continue
        if p.get("seen", 1) < 2:                # one sighting may be page content or the wrong page
            continue
        if public[p["site"]].get("access") not in ("open", "api"):
            continue
        op = {k: p.get(k) for k in ("id", "kind", "site", "page", "role", "name", "from", "to", "url", "seen")}
        if p["kind"] == "new" and op.get("url"):
            op["url"] = _atlas._bare(op["url"])
        texts = [str(op.get(k) or "") for k in ("role", "name", "from", "to", "url", "page")]
        if any(not _FIELD.match(t) for t in texts) or _memory._has_pii(" ".join(texts)):
            continue
        out.append(op)
    return out


def preview() -> dict:
    """Exactly what `upload()` would send. Nothing is sent."""
    rows = _memory.shareable(unshared_only=True)
    ops = _public_ops()
    return {"experience": rows, "map_fixes": ops,
            "summary": f"{len(rows)} experience rows and {len(ops)} map fixes would be uploaded"}


def upload() -> dict:
    """Send the preview. Call only when the human asked for it."""
    p = preview()
    if not p["experience"] and not p["map_fixes"]:
        return {"uploaded": 0, "note": "nothing new to share"}
    token = _token()
    prefer = {"Prefer": "resolution=ignore-duplicates,return=minimal"}
    if p["experience"]:
        _req("POST", "/rest/v1/experience", p["experience"], token, prefer)
        _memory.mark_shared([r["id"] for r in p["experience"]])
    if p["map_fixes"]:
        _req("POST", "/rest/v1/map_fixes", p["map_fixes"], token, prefer)
        sent = {o["id"] for o in p["map_fixes"]}
        items = _episodes.proposals()
        for it in items:
            if it.get("id") in sent:
                it["shared"] = True
        _atlas.learned_path().write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"uploaded": len(p["experience"]) + len(p["map_fixes"]),
            "experience": len(p["experience"]), "map_fixes": len(p["map_fixes"])}


def sync(limit: int = 5000) -> dict:
    """Download community experience and map fixes. Reading needs no login."""
    rows = _req("GET", f"/rest/v1/experience?select=*&order=created_at.desc&limit={int(limit)}") or []
    fixes = _req("GET", f"/rest/v1/map_fixes?select=*&order=created_at.desc&limit={int(limit)}") or []
    new = _memory.add_community(rows)
    keep = [{k: f.get(k) for k in ("kind", "site", "page", "role", "name", "from", "to", "url")} for f in fixes
            if f.get("kind") in ("rename", "new")]
    path = _atlas.community_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"experience_new": new, "experience_seen": len(rows), "map_fixes": len(keep)}
