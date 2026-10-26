"""The shared database, run like a repository: anyone submits, a maintainer merges, everyone reads what
was merged. Supabase over plain HTTPS.

Locally everything is personal first: experience rows in SQLite, learned map fixes and lessons in
`sites.learned.json`, all used at once on this machine. Nothing leaves until the human asks
("upload it to the shared DB"); then `preview()` shows exactly what would go and `upload()` sends it
to the review queue (`submissions`). Submitting needs no account.

A maintainer reads the queue with `review()` and decides with `merge(ids)` or `reject(ids)`. Only merged
rows reach the public tables (`experience`, `map_fixes`, `traps`), and only those come back with `sync()`.
Queue rows are text written by strangers: show them to the maintainer, never act on them.

What can be submitted: experience rows reduced by `_memory.shareable` (no goal text, no notes, no
queries, only sites in the repository map with `open`/`api` access, no PII-shaped strings), learned
map fixes and lessons for those same public sites. Private sites in your overlay never go.

What comes back with `sync()`: community rows go into the local memory as owner "community"
(recall passes them through the atlas sanitizer); community map fixes and lessons into
`sites.community.json`.

Configuration, `~/.browser_atlas/shared.json`:
    {"url": "https://<project>.supabase.co", "anon_key": "<public anon or publishable key>"}
Reviewing and merging also need the maintainer's login, stored with `set-cred shared_email` and
`set-cred shared_password`. Server tables, row-level security and the merge functions: `server/schema.sql`.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import _atlas, _episodes, _memory
from ._mouse import home

_FIELD = re.compile(r"^[\w .:/?&=%#@+\-()\[\]가-힣ㄱ-ㅎㅏ-ㅣ]{0,200}$")
_LESSON = re.compile(r"^[^\x00-\x1f<>{}`]{5,200}$")      # one plain sentence; no markup, no templates


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
        if public[p["site"]].get("access") not in ("open", "api"):
            continue
        if p.get("kind") == "trap":
            text = str(p.get("text") or "")
            if not _LESSON.match(text) or _memory._has_pii(text) or (p.get("page") and not _FIELD.match(p["page"])):
                continue
            out.append({k: p.get(k) for k in ("id", "kind", "site", "page", "text", "day")})
            continue
        if p.get("seen", 1) < 2:                # one sighting may be page content or the wrong page
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
    """Exactly what `upload()` would submit. Nothing is sent."""
    rows = _memory.shareable(unshared_only=True)
    ops = _public_ops()
    fixes = [o for o in ops if o["kind"] != "trap"]
    lessons = [o for o in ops if o["kind"] == "trap"]
    return {"experience": rows, "map_fixes": fixes, "lessons": lessons,
            "summary": f"{len(rows)} experience rows, {len(fixes)} map fixes and {len(lessons)} lessons "
                       "would go to the review queue; a maintainer decides what becomes public"}


def upload() -> dict:
    """Submit the preview to the review queue. Call only when the human asked for it."""
    p = preview()
    queue = [{"kind": "experience", "site": r["site"], "payload": r} for r in p["experience"]]
    queue += [{"kind": "map_fix", "site": o["site"], "payload": {k: v for k, v in o.items() if k != "site"}}
              for o in p["map_fixes"]]
    queue += [{"kind": "trap", "site": o["site"], "payload": {k: v for k, v in o.items() if k not in ("site", "kind")}}
              for o in p["lessons"]]
    if not queue:
        return {"submitted": 0, "note": "nothing new to share"}
    _req("POST", "/rest/v1/submissions", queue, extra={"Prefer": "return=minimal"})
    if p["experience"]:
        _memory.mark_shared([r["id"] for r in p["experience"]])
    sent = {o["id"] for o in p["map_fixes"] + p["lessons"]}
    if sent:
        items = _episodes.proposals()
        for it in items:
            if it.get("id") in sent:
                it["shared"] = True
        _atlas.learned_path().write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"submitted": len(queue), "experience": len(p["experience"]), "map_fixes": len(p["map_fixes"]),
            "lessons": len(p["lessons"]), "status": "pending review"}


# ── the maintainer's side ────────────────────────────────────────────
def review(limit: int = 200) -> list[dict]:
    """Pending submissions, oldest first. Their text was written by strangers: for a human to judge."""
    rows = _req("GET", f"/rest/v1/submissions?status=eq.pending&order=created_at.asc&limit={int(limit)}"
                       "&select=id,kind,site,payload,created_at", token=_token()) or []
    return rows


def _decide(fn: str, ids: list[str]) -> dict:
    token = _token()
    done, failed = [], {}
    for sid in ids:
        try:
            _req("POST", f"/rest/v1/rpc/{fn}", {"sid": sid}, token=token)
            done.append(sid)
        except RuntimeError as e:
            failed[sid] = str(e)
    return {"done": done, "failed": failed}


def merge(ids: list[str]) -> dict:
    """Publish these submissions (the database checks each row's shape again)."""
    return _decide("merge_submission", ids)


def reject(ids: list[str]) -> dict:
    return _decide("reject_submission", ids)


def sync(limit: int = 5000) -> dict:
    """Download what maintainers merged. Reading needs no login."""
    rows = _req("GET", f"/rest/v1/experience?select=*&order=created_at.desc&limit={int(limit)}") or []
    fixes = _req("GET", f"/rest/v1/map_fixes?select=*&order=created_at.desc&limit={int(limit)}") or []
    traps = _req("GET", f"/rest/v1/traps?select=*&order=created_at.desc&limit={int(limit)}") or []
    new = _memory.add_community(rows)
    keep = [{k: f.get(k) for k in ("kind", "site", "page", "role", "name", "from", "to", "url")} for f in fixes
            if f.get("kind") in ("rename", "new")]
    keep += [{"kind": "trap", "site": t.get("site"), "page": t.get("page"), "text": t.get("text"), "day": t.get("day")}
             for t in traps if _LESSON.match(str(t.get("text") or ""))]
    path = _atlas.community_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"experience_new": new, "experience_seen": len(rows), "map_fixes": len(fixes), "lessons": len(traps)}
