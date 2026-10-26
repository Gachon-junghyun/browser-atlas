"""The local page graph: every address, button and link this machine meets, and paths between them.

The atlas map is reviewed and sparse; this graph is the opposite. It fills itself from use, on every
host including the ones the atlas does not know:

- **pages**: each address reached (host + path; a few full example URLs and the title are kept),
- **controls**: buttons, links and fields seen on a page (`browser_read(mode="controls")`) or used
  there (`browser_click` / `browser_type`, also by css: the clicked element's role and name are kept),
- **edges**: how one page led to another (a goto, a click, or a link read on the page).

`plan()` turns that into steps for `browser_run`: the cheapest known way from where the tab is to a
page matching a target (a URL, a path or words from the page's title, its link texts or its controls).

Everything here stays on this machine. Nothing in this module is read by the sharing code, and the
example URLs keep their query strings, which is why they never leave: queries hold searches and ids.
"""
from __future__ import annotations

import heapq
import json
import re
import sqlite3
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import _memory

_SCHEMA = """
create table if not exists g_pages (
  host text not null, path text not null,
  title text, examples text not null default '[]',   -- json list of full URLs, newest first
  visits integer not null default 0, direct_ok integer not null default 0, direct_fail integer not null default 0,
  first real not null, last real not null,
  primary key (host, path));
create table if not exists g_controls (
  host text not null, path text not null, role text not null, name text not null,
  seen integer not null default 0, used integer not null default 0, via text, first real not null, last real not null,
  primary key (host, path, role, name));
create table if not exists g_edges (
  host text not null, src text not null, kind text not null,  -- click | link | type
  label text not null, dst text not null, example text,         -- label: json [role, name] or link text
  ok integer not null default 0, fail integer not null default 0, seen integer not null default 0,
  last real not null,
  primary key (host, src, kind, label, dst));
"""

EXAMPLES = 5
CONTROLS_PER_READ = 150
LINKS_PER_READ = 200
GENERIC = {"닫기", "close", "x", "×", "확인", "취소", "ok", "cancel", "닫기 버튼", "팝업 닫기",
           "오늘 하루 보지 않기", "다시 보지 않기", "오늘 그만 보기", "dismiss", "got it", "no thanks"}


def connect(con: sqlite3.Connection | None = None) -> sqlite3.Connection:
    con = con or _memory.connect()
    con.executescript(_SCHEMA)
    return con


def host_of(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


# Query keys that hold a search, a session or tracking, not which page it is.
_NOT_IDENTITY = re.compile(r"^(q|query|search|keyword|kw|s|term|text|word|srchword|token|sesskey|enc|sid|session|"
                           r"jsessionid|phpsessid|state|code|nonce|ts|t|_|ref|from|utm_.*|fbclid|gclid)$", re.I)


def path_of(url: str) -> str:
    """The page's key: the path plus the query values that say which page it is (?id=7 and ?id=8 are two
    boards; observed: keying by path alone merged every board of an LMS into one and planned the wrong
    one). Searches, sessions and long values are left out."""
    u = urlparse(url or "")
    keep = sorted((k, v) for k, v in parse_qsl(u.query) if not _NOT_IDENTITY.match(k) and len(v) <= 24)
    return (u.path.rstrip("/") or "/") + ("?" + urlencode(keep) if keep else "")


def _bare(url: str) -> str:
    u = urlparse(url or "")
    return urlunparse((u.scheme, u.netloc, u.path, "", "", ""))


def _web(url: str) -> bool:
    return urlparse(url or "").scheme in ("http", "https") and bool(host_of(url))


def _content_like(role: str, name: str, repeats: int = 1) -> bool:
    """A row of the page (a result title, a date, a count) rather than part of its interface."""
    n = name.strip()
    return (not n or n.casefold() in GENERIC or repeats >= 3 or len(n) > 60
            or (role == "link" and len(n) > 40) or bool(re.search(r"\d{2,}", n)))


# ── write ────────────────────────────────────────────────────────────
def page(url: str, title: str = "", direct: bool | None = None, visit: bool = True, con=None) -> bool:
    """A page was reached (or, visit=False, only seen as a link target); returns True if it was new.
    `direct` says whether a goto to exactly this page landed here (True), was redirected elsewhere
    (False), or the page came another way (None)."""
    if not _web(url):
        return False
    con = connect(con)
    host, path, now = host_of(url), path_of(url), time.time()
    row = con.execute("select examples, title from g_pages where host=? and path=?", (host, path)).fetchone()
    ex = json.loads(row["examples"]) if row else []
    if visit or not ex:
        ex = [url] + [e for e in ex if e != url]
    if row and not visit and row["title"]:
        title = ""                                    # a link text never replaces a title already known
    if row:
        con.execute("update g_pages set examples=?, visits=visits+?, last=?, title=coalesce(nullif(?, ''), title),"
                    " direct_ok=direct_ok+?, direct_fail=direct_fail+? where host=? and path=?",
                    (json.dumps(ex[:EXAMPLES], ensure_ascii=False), int(visit), now, title[:120], int(direct is True),
                     int(direct is False), host, path))
    else:
        con.execute("insert into g_pages (host, path, title, examples, visits, direct_ok, direct_fail, first, last)"
                    " values (?,?,?,?,?,?,?,?,?)", (host, path, title[:120], json.dumps(ex[:EXAMPLES], ensure_ascii=False),
                                                   int(visit), int(direct is True), int(direct is False), now, now))
    con.commit()
    return row is None


def control(url: str, role: str, name: str, used: bool = False, via: str = "", con=None) -> bool:
    """A control on a page; returns True if it was new. Content-like names are skipped."""
    if not _web(url) or not role or _content_like(role, name or ""):
        return False
    con = connect(con)
    host, path, now = host_of(url), path_of(url), time.time()
    if con.execute("select 1 from g_pages where host=? and path=?", (host, path)).fetchone() is None:
        page(url, visit=False, con=con)                # a control always belongs to a page on the map
    key = (host, path, role, " ".join(name.split()))
    new = con.execute("select 1 from g_controls where host=? and path=? and role=? and name=?", key).fetchone() is None
    if new:
        con.execute("insert into g_controls (host, path, role, name, seen, used, via, first, last) values (?,?,?,?,?,?,?,?,?)",
                    (*key, int(not used), int(used), via or None, now, now))
    else:
        con.execute("update g_controls set seen=seen+?, used=used+?, last=?, via=coalesce(?, via)"
                    " where host=? and path=? and role=? and name=?", (int(not used), int(used), now, via or None, *key))
    con.commit()
    return new


def edge(src_url: str, kind: str, label, dst_url: str, ok: bool | None, con=None) -> None:
    """One way from a page to another. ok=None: only seen (a link on the page), not taken."""
    if not (_web(src_url) and _web(dst_url)) or host_of(src_url) != host_of(dst_url):
        return
    con = connect(con)
    lab = label if isinstance(label, str) else json.dumps(list(label), ensure_ascii=False)
    key = (host_of(src_url), path_of(src_url), kind, lab[:200], path_of(dst_url))
    con.execute("insert into g_edges (host, src, kind, label, dst, example, ok, fail, seen, last) values (?,?,?,?,?,?,?,?,?,?)"
                " on conflict(host, src, kind, label, dst) do update set ok=ok+excluded.ok, fail=fail+excluded.fail,"
                " seen=seen+excluded.seen, last=excluded.last, example=excluded.example",
                (*key, dst_url, int(ok is True), int(ok is False), int(ok is None), time.time()))
    con.commit()


def observe(tool: str, args: dict, result, before: str, con=None) -> dict:
    """Learn from one successful browser call. Returns what was new, for the caller to report."""
    r = result if isinstance(result, dict) else {}
    after = str(r.get("url") or "")
    new = {"pages": 0, "controls": 0}
    con = connect(con)
    if tool in ("browser_open", "browser_goto") and after:
        asked = str(args.get("url") or after)
        landed = path_of(asked) == path_of(after) and host_of(asked) == host_of(after)
        new["pages"] += page(after, str(r.get("title") or ""), direct=True if landed else None, con=con)
        if not landed and _web(asked):
            page(asked, direct=False, visit=False, con=con)   # asked for it, got sent elsewhere (a login wall)
    elif tool in ("browser_click", "browser_type"):
        role, name = args.get("role"), args.get("name")
        via = ""
        if not (role and name) and r.get("role") and r.get("name"):
            role, name, via = r["role"], r["name"], "css"  # clicked by css: keep what the element really is
        if role and name and before:
            new["controls"] += control(before, role, name, used=True, via=via, con=con)
            if tool == "browser_click" and after and _web(after):
                moved = path_of(after) != path_of(before) or host_of(after) != host_of(before)
                if moved:
                    new["pages"] += page(after, con=con)
                    edge(before, "click", [role, name], after, True, con=con)
    elif tool == "browser_read" and after:
        data = r.get("untrusted_page_content")
        mode = args.get("mode", "controls")
        if mode == "controls" and isinstance(data, list):
            counts: dict[tuple, int] = {}
            for item in data:
                if isinstance(item, list) and len(item) > 1:
                    counts[(item[0], item[1])] = counts.get((item[0], item[1]), 0) + 1
            for (role, name), n in list(counts.items())[:CONTROLS_PER_READ]:
                if not _content_like(role, name, n):
                    new["controls"] += control(after, role, name, con=con)
        elif mode == "links" and isinstance(data, list):
            for item in data[:LINKS_PER_READ]:
                if isinstance(item, list) and len(item) > 1 and _web(str(item[1])):
                    text, href = " ".join(str(item[0]).split())[:80], str(item[1])
                    if text and host_of(href) == host_of(after):
                        new["pages"] += page(href, title=text, visit=False, con=con)   # seen as a link, not visited
                        edge(after, "link", text, href, None, con=con)
    return new


# ── read ─────────────────────────────────────────────────────────────
def host_map(host: str, con=None, limit: int = 40) -> dict:
    """This machine's map of a host: pages (most visited first) with their used and seen controls."""
    con = connect(con)
    host = host.lower().removeprefix("www.")
    pages = con.execute("select * from g_pages where host=? order by visits desc, last desc limit ?", (host, limit)).fetchall()
    out = []
    for p in pages:
        ctrls = con.execute("select role, name, used, seen, via from g_controls where host=? and path=?"
                            " order by used desc, seen desc limit 30", (host, p["path"])).fetchall()
        out.append({"path": p["path"], "title": p["title"] or "", "visits": p["visits"],
                    "direct": [p["direct_ok"], p["direct_fail"]], "example": (json.loads(p["examples"]) or [""])[0],
                    "controls": [[c["role"], c["name"], c["used"], c["seen"]] + ([c["via"]] if c["via"] else [])
                                 for c in ctrls]})
    n = {t: con.execute(f"select count(*) from {t} where host=?", (host,)).fetchone()[0]
         for t in ("g_pages", "g_controls", "g_edges")}
    return {"host": host, "pages": n["g_pages"], "controls": n["g_controls"], "edges": n["g_edges"], "top": out}


def _score(target: str, texts: list[str]) -> float:
    t = target.casefold().strip()
    if not t:
        return 0.0
    best = 0.0
    tk = _memory.keywords(target)
    for x in texts:
        x = (x or "").casefold()
        if not x:
            continue
        if t == x:
            return 1.0
        if t in x:
            best = max(best, 0.8)
        best = max(best, 0.7 * _memory.similarity(tk, _memory.keywords(x)))
    return best


def _targets(con, host: str, target: str) -> list[tuple[float, str]]:
    """Pages of `host` that match the target, best first: (match 0..1, path)."""
    if "://" in target or target.startswith("/"):
        want = path_of(target if "://" in target else "https://" + host + target)
        return [(1.0, want)] if con.execute("select 1 from g_pages where host=? and path=?", (host, want)).fetchone() \
            else []
    hits: dict[str, float] = {}
    for p in con.execute("select path, title from g_pages where host=?", (host,)):
        hits[p["path"]] = _score(target, [p["title"], p["path"]])
    for e in con.execute("select dst, kind, label from g_edges where host=?", (host,)):
        lab = e["label"] if e["kind"] == "link" else " ".join(json.loads(e["label"])[1:2])
        hits[e["dst"]] = max(hits.get(e["dst"], 0.0), 0.9 * _score(target, [lab]))
    for c in con.execute("select path, name from g_controls where host=?", (host,)):
        hits[c["path"]] = max(hits.get(c["path"], 0.0), 0.6 * _score(target, [c["name"]]))
    return sorted(((s, p) for p, s in hits.items() if s >= 0.35), reverse=True)[:12]


def plan(start_url: str, target: str, host: str | None = None, con=None) -> dict:
    """The cheapest known steps from `start_url` to a page matching `target`, for browser_run.

    Costs: a goto that landed directly before = 1 (plus a penalty for redirects seen), a click taken
    before = 1 (plus failures), a link only seen on a page = 1.5. Pages reached only through clicks
    have no direct goto, so the path walks there from where the tab is.
    """
    con = connect(con)
    host = (host or host_of(start_url)).lower().removeprefix("www.")
    targets = _targets(con, host, target)
    if not targets:
        known = [r["path"] for r in con.execute("select path from g_pages where host=? order by visits desc limit 15", (host,))]
        return {"found": False, "host": host, "target": target,
                "hint": "no page on this host matches; known pages are listed (visit it once and the graph learns it)",
                "known_pages": known}
    goal = {p: s for s, p in targets}
    start = path_of(start_url) if host_of(start_url) == host else None
    if start:
        # the same words on several pages (every course has "Class Announcements"): the one linked from
        # where the tab is now is meant (observed: from the database course, plain cost picked another course's board)
        for e in con.execute("select dst, kind, label from g_edges where host=? and src=?", (host, start)):
            lab = e["label"] if e["kind"] == "link" else " ".join(json.loads(e["label"])[1:2])
            s = _score(target, [lab])
            if s >= 0.35:
                goal[e["dst"]] = max(goal.get(e["dst"], 0.0), s) + 0.5
    pages = {r["path"]: r for r in con.execute("select * from g_pages where host=?", (host,))}
    adj: dict[str, list] = {}
    for e in con.execute("select * from g_edges where host=?", (host,)):
        if e["kind"] == "link":
            cost, step = 1.5, ["goto", e["example"]]
        elif e["kind"] == "click":
            role, name = json.loads(e["label"])[:2]
            cost, step = 1.0 + 2.0 * e["fail"] / max(1, e["ok"] + e["fail"]), ["click", role, name]
        else:
            continue
        adj.setdefault(e["src"], []).append((cost, e["dst"], step))
    # a virtual start: from anywhere, a page that took a direct goto before is one goto away
    dist, prev = {"*": 0.0}, {}
    heap = [(0.0, "*")]
    while heap:
        d, node = heapq.heappop(heap)
        if d > dist.get(node, 1e9):
            continue
        nxt = list(adj.get(node, []))
        if node == "*":
            if start:
                nxt.append((0.0, start, None))
            for p, r in pages.items():
                ex = json.loads(r["examples"])
                fail = r["direct_fail"] / max(1, r["direct_ok"] + r["direct_fail"])
                if r["direct_ok"]:
                    nxt.append((1.0 + 3.0 * fail, p, ["goto", ex[0]]))
                elif r["visits"] == 0 and ex and not r["direct_fail"]:
                    # only seen as a link: its address is a goto too (pages reached by a click are not:
                    # their URLs may carry a one-time token, so the click path is kept for them)
                    nxt.append((1.5, p, ["goto", ex[0]]))
        for cost, dst, step in nxt:
            nd = d + cost
            if nd < dist.get(dst, 1e9):
                dist[dst], prev[dst] = nd, (node, step)
                heapq.heappush(heap, (nd, dst))
    reach = [(dist[p] - 2.0 * goal[p], p) for p in goal if p in dist]
    if not reach:
        return {"found": False, "host": host, "target": target, "matches": [p for _, p in targets],
                "hint": "matching pages are known but no known way leads there; open one by URL"}
    _, end = min(reach)
    steps, node = [], end
    while node in prev:
        node, step = prev[node]
        if step:
            steps.append(step)
    steps.reverse()
    for st in steps:
        if st[0] == "goto" and isinstance(st[1], str) and host_of(st[1]) != host:
            return {"found": False, "host": host, "target": target, "hint": "path left the host; refused"}
    return {"found": True, "host": host, "target": target, "page": end, "title": (pages[end]["title"] or "") if end in pages else "",
            "match": round(goal[end], 2), "cost": round(dist[end], 2), "steps": steps,
            "others": [p for _, p in targets if p != end][:4]}
