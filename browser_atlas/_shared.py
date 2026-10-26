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
map fixes and lessons for those same public sites, and site reports for hosts the map does not have
(`site_reports()`: checked values only). Private sites in your overlay never go.

What comes back with `sync()`: community rows go into the local memory as owner "community"
(recall passes them through the atlas sanitizer); community map fixes and lessons into
`sites.community.json`; site reports, checked again, into `site_reports.community.json`, which
`browser_open` shows as `unmapped.shared_reports` on that host.

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


# ── site reports: what was learned on a host the map does not have yet ─────────
# The other kinds attach to mapped sites. A host nobody mapped had no way to share at all, so its trial
# and error stayed on one machine. A report is the host digest reduced to checked values: closed roles,
# short names and bare URLs on that host, error class names, counts. Never the goal, the notes, what was
# typed or an error's text. Maintainers read reports to write real map entries; everyone gets them back
# as hints for that host.
ROLES = {"button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio", "tab", "menuitem", "option",
         "switch", "slider", "spinbutton", "listbox"}
TOOLS = {"browser_open", "browser_goto", "browser_click", "browser_type", "browser_read", "browser_back",
         "browser_wait", "browser_dismiss"}
_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_KIND = re.compile(r"^[A-Za-z]{1,40}$")
REPORT_BYTES = 3600                                      # the queue takes 4000 per payload


def _ok_name(name) -> bool:
    s = str(name or "")
    return 0 < len(s) <= 80 and bool(_FIELD.match(s)) and not re.search(r"\d{3,}", s) and not _memory._has_pii(s)


def _ok_url(url, host: str) -> bool:
    from urllib.parse import urlparse
    s = str(url or "")
    u = urlparse(s)
    h = (u.hostname or "").removeprefix("www.")
    return (u.scheme in ("http", "https") and (h == host or h.endswith("." + host)) and not u.query and not u.fragment
            and len(s) <= 300 and "@" not in s and not re.search(r"\d{5,}", u.path) and bool(_FIELD.match(s))
            and not _memory._has_pii(s))


def _clean_report(r: dict) -> dict | None:
    """A report with only checked values; used on the way out and again on the way in."""
    host = str(r.get("host") or "").lower().removeprefix("www.")
    if not _HOST.match(host) or len(host) > 253:
        return None
    worked = [[c[0], c[1], int(c[2])] for c in r.get("worked") or []
              if isinstance(c, list) and len(c) >= 3 and c[0] in ROLES and _ok_name(c[1])][:15]
    failed = []
    for f in r.get("failed") or []:
        if not (isinstance(f, list) and len(f) >= 4 and f[0] in TOOLS and _KIND.match(str(f[2]))):
            continue
        what = f[1]
        if isinstance(what, list) and len(what) >= 2 and what[0] in ROLES and _ok_name(what[1]):
            failed.append([f[0], [what[0], what[1]], f[2], int(f[3])])
        elif isinstance(what, str) and _ok_url(what, host):
            failed.append([f[0], what, f[2], int(f[3])])
    pages = [[u, int(n)] for u, n in (r.get("pages") or []) if _ok_url(u, host)][:10]
    refused = [[u, int(n)] for u, n in (r.get("refused") or []) if _ok_url(u, host)][:10]
    rb = r.get("robots") if isinstance(r.get("robots"), dict) else None
    robots = None
    if rb and rb.get("allowed") in (True, False, None):
        robots = {"allowed": rb.get("allowed")}
        if _FIELD.match(str(rb.get("rule") or "")) and len(str(rb.get("rule") or "")) <= 80:
            robots["rule"] = str(rb.get("rule") or "")
    outcomes = {k: int(v) for k, v in (r.get("outcomes") or {}).items() if k in _memory.OUTCOMES}
    out = {"host": host, "attempts": int(r.get("attempts") or 0), "outcomes": outcomes, "robots": robots,
           "worked": worked, "failed": failed[:10], "pages": pages, "refused": refused,
           "day": str(r.get("day") or "")[:10] or None}
    while len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > REPORT_BYTES:
        longest = max(("worked", "failed", "pages", "refused"), key=lambda k: len(out[k]))
        if not out[longest]:
            return None
        out[longest].pop()
    return out


def _report_hosts() -> list[str]:
    """Hosts this machine worked on that neither the repository map nor the private overlay covers."""
    from urllib.parse import urlparse
    public = _atlas.load_base()
    private = _atlas._read(_atlas.overlay_path(), {})
    hosts = set()
    for r in _memory.connect().execute("select route, notes from experience where owner = 'me'"):
        try:
            h = str(json.loads(r["notes"] or "{}").get("host") or "")
        except ValueError:
            h = ""
        if not h:
            first = next((s for s in json.loads(r["route"]) if isinstance(s, dict) and s.get("url")), None)
            h = (urlparse(first["url"]).hostname or "") if first else ""
        h = h.lower().removeprefix("www.")
        if not _HOST.match(h):
            continue
        url = "https://" + h + "/"
        if any(_atlas.site_for(u, s) for u in (url, "https://www." + h + "/") for s in (public, private)):
            continue
        hosts.add(h)
    return sorted(hosts)


def _sent_reports_path():
    return home() / "shared_reports.json"


def site_reports(unshared_only: bool = True) -> list[dict]:
    import hashlib
    import time
    try:
        sent = set(json.loads(_sent_reports_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        sent = set()
    out = []
    for host in _report_hosts():
        d = _memory.host_digest(host)
        if not d["attempts"]:
            continue
        rep = _clean_report({"host": host, "attempts": d["attempts"], "outcomes": d["outcomes"], "robots": d["robots"],
                             "worked": d["worked_controls"],
                             "failed": [[t, json.loads(w) if w.startswith("[") else w, k, n] for t, w, k, n in d["failed_steps"]],
                             "pages": d["pages_reached"], "refused": d["refused_by_robots"],
                             "day": time.strftime("%Y-%m-%d")})
        if not rep:
            continue
        body = {k: v for k, v in rep.items() if k != "day"}
        rep["id"] = hashlib.md5(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if unshared_only and rep["id"] in sent:
            continue
        out.append(rep)
    return out


def preview() -> dict:
    """Exactly what `upload()` would submit. Nothing is sent."""
    rows = _memory.shareable(unshared_only=True)
    ops = _public_ops()
    fixes = [o for o in ops if o["kind"] != "trap"]
    lessons = [o for o in ops if o["kind"] == "trap"]
    reports = site_reports()
    return {"experience": rows, "map_fixes": fixes, "lessons": lessons, "site_reports": reports,
            "summary": f"{len(rows)} experience rows, {len(fixes)} map fixes, {len(lessons)} lessons and "
                       f"{len(reports)} reports on sites not in the map would go to the review queue; "
                       "a maintainer decides what becomes public"}


def upload() -> dict:
    """Submit the preview to the review queue. Call only when the human asked for it."""
    p = preview()
    queue = [{"kind": "experience", "site": r["site"], "payload": r} for r in p["experience"]]
    queue += [{"kind": "map_fix", "site": o["site"], "payload": {k: v for k, v in o.items() if k != "site"}}
              for o in p["map_fixes"]]
    queue += [{"kind": "trap", "site": o["site"], "payload": {k: v for k, v in o.items() if k not in ("site", "kind")}}
              for o in p["lessons"]]
    queue += [{"kind": "site_report", "site": re.sub(r"[^a-z0-9]", "_", o["host"])[:64], "payload": o}
              for o in p["site_reports"]]
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
    if p["site_reports"]:
        try:
            sent_ids = json.loads(_sent_reports_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sent_ids = []
        _sent_reports_path().write_text(json.dumps(sent_ids + [o["id"] for o in p["site_reports"]]), encoding="utf-8")
    return {"submitted": len(queue), "experience": len(p["experience"]), "map_fixes": len(p["map_fixes"]),
            "lessons": len(p["lessons"]), "site_reports": len(p["site_reports"]), "status": "pending review"}


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
    try:
        reports = _req("GET", f"/rest/v1/site_reports?select=*&order=created_at.desc&limit={int(limit)}") or []
    except RuntimeError:                               # a server without the table yet: the rest still syncs
        reports = []
    clean = [c for c in (_clean_report(r) for r in reports) if c]
    community_reports_path().write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"experience_new": new, "experience_seen": len(rows), "map_fixes": len(fixes), "lessons": len(traps),
            "site_reports": len(clean)}


def community_reports_path():
    return home() / "site_reports.community.json"


def reports_for(host: str) -> dict | None:
    """Merged reports from other people on this host, summed: hints for a site the map does not have."""
    host = (host or "").lower().removeprefix("www.")
    try:
        reports = json.loads(community_reports_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mine = [r for r in (_clean_report(x) for x in reports) if r and (host == r["host"] or host.endswith("." + r["host"]))]
    if not mine:
        return None

    def add(acc: dict, key, n: int) -> None:
        k = json.dumps(key, ensure_ascii=False)
        acc[k] = acc.get(k, 0) + n

    worked, failed, refused, outcomes = {}, {}, {}, {}
    for r in mine:
        for role, name, n in r["worked"]:
            add(worked, [role, name], n)
        for tool, what, kind, n in r["failed"]:
            add(failed, [tool, what, kind], n)
        for u, n in r["refused"]:
            add(refused, u, n)
        for k, n in r["outcomes"].items():
            outcomes[k] = outcomes.get(k, 0) + n

    def top(d: dict, n: int) -> list:
        return [[*json.loads(k), v] if k.startswith("[") else [json.loads(k), v]
                for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]

    return {"reports": len(mine), "outcomes": outcomes, "worked_controls": top(worked, 10),
            "failed_steps": top(failed, 8), "refused_by_robots": top(refused, 8)}
