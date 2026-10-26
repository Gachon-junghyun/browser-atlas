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
DATED_LIST_WORDS = ("news", "뉴스", "공시", "기사", "filing", "filings", "press", "보도")
# Controls that change the page in place instead of leaving it.
TOGGLES = {"radio", "tab", "checkbox", "switch", "option", "menuitemradio", "menuitemcheckbox"}


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


_SEARCH = re.compile(r"^(q|query|search|keyword|kw|s|term|text|word|srchword)$", re.I)


def with_search(url: str, words: str) -> str | None:
    """The same address with its search box set to `words`, or None if it has no search query key."""
    u = urlparse(url or "")
    qs = parse_qsl(u.query, keep_blank_values=True)
    key = next((k for k, _ in qs if _SEARCH.match(k)), None)
    if key is None:
        return None
    return urlunparse(u._replace(query=urlencode([(k, words if k == key else v) for k, v in qs])))


def _segs(path: str) -> list[str]:
    return path.split("?")[0].strip("/").split("/")


def _slot(start: str, paths) -> tuple[int, list[str], str] | None:
    """A path segment that names a thing (a ticker, an item, a course): another known page has the same
    path as `start` except in that one segment, and it is not the last one (/price and /news are two
    pages; NVDA.O/price and AMD.O/price are one page about two things). Returns (index, prefix, value)."""
    s = _segs(start)
    for p in paths:
        q = _segs(p)
        if len(q) == len(s) and q != s:
            diff = [i for i in range(len(s)) if s[i] != q[i]]
            if len(diff) == 1 and diff[0] < len(s) - 1:
                return diff[0], s[:diff[0]], s[diff[0]]
    return None


def _rebase_path(path: str, slot) -> str:
    i, prefix, value = slot
    base, sep, q = path.partition("?")
    segs = base.strip("/").split("/")
    if len(segs) > i and segs[:i] == prefix and segs[i] != value:
        segs[i] = value
        return "/" + "/".join(segs) + sep + q
    return path


def _rebase_url(url: str, slot) -> str:
    u = urlparse(url)
    return urlunparse(u._replace(path=_rebase_path(u.path, slot)))


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


def miss(src_url: str, label, dst_path: str, con=None) -> None:
    """A planned click from `src_url` did not lead to `dst_path`: count it against that edge."""
    if not _web(src_url):
        return
    con = connect(con)
    lab = label if isinstance(label, str) else json.dumps(list(label), ensure_ascii=False)
    con.execute("update g_edges set fail=fail+1, last=? where host=? and src=? and kind in ('click', 'popup') and label=? and dst=?",
                (time.time(), host_of(src_url), path_of(src_url), lab[:200], dst_path))
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
        if r.get("wait_timed_out"):
            # what was waited for never came (observed: a guessed ticker suffix drew an empty frame and
            # became a page to go to); kept as a visit, never as a direct goto
            new["pages"] += page(after, str(r.get("title") or ""), direct=False, con=con)
            return new
        new["pages"] += page(after, str(r.get("title") or ""), direct=True if landed else None, con=con)
        if not landed and _web(asked):
            page(asked, direct=False, visit=False, con=con)   # asked for it, got sent elsewhere (a login wall)
    elif tool in ("browser_click", "browser_type"):
        popup = r.get("new_tab") if tool == "browser_click" else None
        if popup and popup.get("url"):
            new["pages"] += page(str(popup["url"]), con=con)
        role, name = args.get("role"), args.get("name")
        via = ""
        if not (role and name) and r.get("role") and r.get("name"):
            role, name, via = r["role"], r["name"], "css"  # clicked by css: keep what the element really is
        if role and name and before:
            new["controls"] += control(before, role, name, used=True, via=via, con=con)
            if tool == "browser_click":
                destination = str((popup or {}).get("url") or after)
                moved = destination and (path_of(destination) != path_of(before)
                                         or host_of(destination) != host_of(before))
                if _web(destination) and (popup or moved):
                    if not popup:
                        new["pages"] += page(destination, con=con)
                    idx = args.get("index")
                    if idx is None and (r.get("same_name_controls") or 0) > 1:
                        idx = 0                       # the first of several with this name was the one pressed
                    edge(before, "popup" if popup else "click",
                         [role, name] + ([int(idx)] if idx is not None else []), destination, True, con=con)
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
    return {"host": host, "pages": n["g_pages"], "controls": n["g_controls"], "edges": n["g_edges"], "top": out,
            "kinds": kinds(host, con=con)}


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


def _plan(start_url: str, target: str, host: str | None = None, con=None, picks=None) -> dict:
    """The cheapest known steps from `start_url` to a page matching `target`, for browser_run.

    Costs: a goto that landed directly before = 1 (plus a penalty for redirects seen), a click taken
    before = 1 (plus failures), a link only seen on a page = 1.5. Pages reached only through clicks
    have no direct goto, so the path walks there from where the tab is.

    A full URL as target is a goto to that URL, on whatever host it is (the caller gave the address;
    walking clicks there picks look-alike links). From a page about one thing (/stock/AMD.O/price)
    the pages learned about another (/stock/NVDA.O/finance) are planned for this one; observed: from
    AMD the plan sent the tab to NVDA's financials. A search page is planned with the target words.
    """
    con = connect(con)
    if "://" in target and host is None and host_of(target):
        host = host_of(target)
    host = (host or host_of(start_url)).lower().removeprefix("www.")
    start = path_of(start_url) if host_of(start_url) == host else None
    if "://" in target and host_of(target) == host and _web(target):
        # known or not: the caller gave the address (observed: EDGAR with count=10 instead of the
        # count=40 seen before matched no page and the go failed)
        row = con.execute("select title from g_pages where host=? and path=?", (host, path_of(target))).fetchone()
        return {"found": True, "host": host, "target": target, "page": path_of(target),
                "title": (row["title"] or "") if row else "", "match": 1.0, "cost": 1.0,
                "steps": [["goto", target]], "others": [], **({} if row else {"known": False})}
    pages = {r["path"]: dict(r) for r in con.execute("select * from g_pages where host=?", (host,))}
    slot = _slot(start, pages) if start else None
    if slot:
        i, prefix, _ = slot
        known = sorted({_segs(p)[i] for p in pages if len(_segs(p)) > i + 1 and _segs(p)[:i] == prefix})
        named = next((v for v in known if _named(v, picks.get("thing", target))), None)
        if named:
            slot = (i, prefix, named)
    rb = (lambda p: _rebase_path(p, slot)) if slot else (lambda p: p)
    rbu = (lambda u: _rebase_url(u, slot)) if slot else (lambda u: u)
    targets = _targets(con, host, target)
    if not targets:
        return _search_or_nothing(con, host, target)
    goal: dict[str, float] = {}
    for s, p in targets:
        goal[rb(p)] = max(goal.get(rb(p), 0.0), s)
    if start:
        # the same words on several pages (every course has "Class Announcements"): the one linked from
        # where the tab is now is meant (observed: from the database course, plain cost picked another course's board)
        for e in con.execute("select src, dst, kind, label from g_edges where host=?", (host,)):
            if rb(e["src"]) != start:
                continue
            lab = e["label"] if e["kind"] == "link" else " ".join(json.loads(e["label"])[1:2])
            s = _score(target, [lab])
            if s >= 0.35:
                goal[rb(e["dst"])] = max(goal.get(rb(e["dst"]), 0.0), s) + 0.5
    visited = {p for p, r in pages.items() if r["visits"]}
    merged: dict[str, dict] = {}
    for p, r in pages.items():                        # pages about another thing become pages about this one
        q = rb(p)
        r = dict(r, examples=json.dumps([rbu(x) for x in json.loads(r["examples"])], ensure_ascii=False))
        if q in merged:
            m = merged[q]
            first, second = (r, m) if p == q else (m, r)
            merged[q] = dict(first, visits=m["visits"] + r["visits"], direct_ok=m["direct_ok"] + r["direct_ok"],
                             direct_fail=m["direct_fail"] + r["direct_fail"], title=first["title"] or second["title"],
                             examples=json.dumps(json.loads(first["examples"]) + json.loads(second["examples"]),
                                                 ensure_ascii=False))
        else:
            merged[q] = r
    pages = merged
    adj: dict[str, list] = {}
    for e in con.execute("select * from g_edges where host=?", (host,)):
        if e["kind"] == "link":
            cost, step = 1.5, ["goto", rbu(e["example"])]
        elif e["kind"] in ("click", "popup"):
            lab = json.loads(e["label"])
            cost, step = 1.0 + 2.0 * e["fail"] / max(1, e["ok"] + e["fail"]), ["click", *lab[:3]]
        else:
            continue
        route = [step, ["tab", "new"]] if e["kind"] == "popup" else [step]
        adj.setdefault(rb(e["src"]), []).append((cost, rb(e["dst"]), route))
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
                    nxt.append((1.0 + 3.0 * fail, p, [["goto", ex[0]]]))
                elif r["visits"] == 0 and ex and not r["direct_fail"]:
                    # only seen as a link: its address is a goto too (pages reached by a click are not:
                    # their URLs may carry a one-time token, so the click path is kept for them)
                    nxt.append((1.5, p, [["goto", ex[0]]]))
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
    ambiguous = None
    if not slot:
        # the tab is on no thing's page (a news front page): the same page exists for several things
        # and only the target can say which (observed: from /news, "quarterly" went to whichever stock
        # was looked at last, without a word)
        th = _things(end, [p for _, p in reach])
        if th:
            seg, by = th
            named = [p for v, p in by.items() if _named(v, picks.get("thing", target))]
            here = _segs(start) if start else []
            if named:
                end = min((dist[p] - 2.0 * goal[p], p) for p in named)[1]
            elif len(here) > seg + 1 and here[:seg] == _segs(end)[:seg] and here[seg] in by:
                end = by[here[seg]]                   # the tab is on that thing's other page (/stock/JPM/price)
            else:
                ambiguous = {"segment": seg, "picked": _segs(end)[seg], "candidates": sorted(by)[:8],
                             "hint": "the start page names no thing; name it in the target ('JPM quarterly') "
                                     "or start from its page"}
    steps, node = [], end
    while node in prev:
        node, step = prev[node]
        if step:
            steps.append(step)
    steps = [st for route in reversed(steps) for st in route]
    for st in steps:
        if st[0] == "goto" and isinstance(st[1], str) and host_of(st[1]) != host:
            return {"found": False, "host": host, "target": target, "hint": "path left the host; refused"}
    out = {"found": True, "host": host, "target": target, "page": end,
           "title": (pages[end]["title"] or "") if end in pages else "",
           "match": round(goal[end], 2), "cost": round(dist[end], 2), "steps": steps,
           "others": [p for p in dict.fromkeys(rb(p) for _, p in targets) if p != end][:4]}
    if steps and steps[-1][0] == "goto" and "://" not in target and not target.startswith("/"):
        searched = with_search(steps[-1][1], target)   # a search page holds the last search; this one is meant
        if searched:
            steps[-1] = ["goto", searched]
            out["searched"] = True
    if slot and end not in visited:
        out["rebased"] = {"segment": slot[0], "to": slot[2]}   # inferred from another thing's page, never seen
    if ambiguous:
        out["ambiguous"] = ambiguous
    # the target names a control used on that page before (a "quarterly" toggle): press it there
    best = (0.0, None)
    t = " ".join(target.split()).casefold()
    for c in con.execute("select path, role, name from g_controls where host=? and used>0", (host,)):
        if rb(c["path"]) == end and c["role"] != "textbox":
            s = _score(target, [c["name"]])
            n = " ".join(c["name"].split()).casefold()
            if c["role"] in TOGGLES and len(n) >= 2 and n in t:
                s = max(s, 0.8)                       # "NVDA quarterly": the toggle is named inside the target
            best = max(best, (s, (c["role"], c["name"])), key=lambda x: x[0])
    if best[0] >= 0.8 and "toggle" not in picks:
        steps.append(["click", *best[1]])
    return out


def _things(end: str, paths) -> tuple[int, dict[str, str]] | None:
    """`end` and the known pages that are the same page about another thing (/stock/JPM/finance and
    /stock/NVDA.O/finance): (segment index, {thing: path}), or None if there are none."""
    e, idx, by = _segs(end), None, {}
    for p in paths:
        q = _segs(p)
        if len(q) != len(e) or q == e:
            continue
        diff = [i for i in range(len(e)) if e[i] != q[i]]
        if len(diff) == 1 and diff[0] < len(e) - 1 and idx in (None, diff[0]):
            idx = diff[0]
            by[q[idx]] = p
    if idx is None:
        return None
    by[e[idx]] = end
    return idx, by


def _named(value: str, target: str) -> bool:
    """Whether the target names this thing ("NVDA" for NVDA.O), as a word of its own."""
    t = target.casefold()
    return any(re.search(r"(?<![0-9a-z])" + re.escape(x) + r"(?![0-9a-z])", t)
               for x in re.split(r"[^0-9a-z가-힣]+", value.casefold()) if len(x) >= 2)


def _search_or_nothing(con, host: str, target: str) -> dict:
    """No page matches: a search page of the host asked for the target words, or the known pages."""
    known = []
    for r in con.execute("select path, examples from g_pages where host=? order by visits desc", (host,)):
        known.append(r["path"])
        if "://" in target or target.startswith("/"):
            continue
        for ex in json.loads(r["examples"]):
            url = with_search(ex, target)
            if url:
                return {"found": True, "host": host, "target": target, "page": r["path"], "title": "", "match": 0.5,
                        "cost": 1.0, "steps": [["goto", url]], "others": [], "searched": True}
    return {"found": False, "host": host, "target": target,
            "hint": "no page on this host matches; known pages are listed (visit it once and the graph learns it)",
            "known_pages": known[:15]}


def plan(start_url: str, target: str, host: str | None = None, con=None, picks=None) -> dict:
    """Keep uncertain choices explicit before replaying a remembered path."""
    picks = picks or {}
    con = connect(con)
    out = _plan(start_url, target, host, con, picks)
    if not out.get("found"):
        return out
    end = out["page"]
    texts = " ".join((end, out["title"], target)).casefold()
    fresh = picks.get("fresh", 14 if any(w in texts for w in DATED_LIST_WORDS) else 0)
    if fresh:
        out["checks"] = {"fresh_days": fresh}
    controls = con.execute("select path, role, name, used, seen from g_controls where host=?"
                           " order by used desc, seen desc, role, name", (out["host"],)).fetchall()
    slot = _slot(end, [r[0] for r in con.execute("select path from g_pages where host=?", (out["host"],))])
    toggles = [c for c in controls if c["role"] in TOGGLES
               and (_rebase_path(c["path"], slot) if slot else c["path"]) == end]
    t = " ".join(target.split()).casefold()
    named = any(_score(target, [c["name"]]) >= 0.8
                or (len(n := " ".join(c["name"].split()).casefold()) >= 2 and n in t) for c in toggles)
    if "toggle" in picks and picks["toggle"]:
        chosen = next((c for c in toggles if c["name"] == picks["toggle"]), None)
        if chosen:
            out["steps"].append(["click", chosen["role"], chosen["name"]])
    if out.get("ambiguous"):
        a = out["ambiguous"]
        out["choose"] = {"kind": "thing", "at": 0, "candidates": a["candidates"], "picked": a["picked"],
                         "read": "the target and the page: which thing is meant",
                         "answer": 'picks={"thing": "<name>"}'}
    elif any(c["used"] > 0 for c in toggles) and not named and "toggle" not in picks:
        out["choose"] = {"kind": "toggle", "at": len(out["steps"]),
                         "candidates": [list(c) for c in dict.fromkeys((c["role"], c["name"]) for c in toggles)][:8],
                         "picked": None,
                         "read": "the page's toggles: which one answers the target",
                         "answer": 'picks={"toggle": "<name>"} or {"toggle": ""} to keep the default'}
    return out


def kinds(host: str, con=None) -> list[dict]:
    """Fold observed paths into screen kinds without storing another map."""
    con = connect(con)
    host = host.lower().removeprefix("www.")
    pages = con.execute("select path, title from g_pages where host=? order by path", (host,)).fetchall()
    paths = [p["path"] for p in pages]
    templates, groups = {}, {}
    for p in pages:
        path = p["path"]
        th = _things(path, paths)
        template = path
        if th:
            base, sep, query = path.partition("?")
            segments = _segs(base)
            segments[th[0]] = "{thing}"
            template = "/" + "/".join(segments) + sep + query
        templates[path] = template
        group = groups.setdefault(template, {"template": template, "things": [], "title": p["title"] or "",
                                             "next": [], "toggles": []})
        if not group["title"]:
            group["title"] = p["title"] or ""
        if th and _segs(path)[th[0]] not in group["things"]:
            group["things"].append(_segs(path)[th[0]])
    for e in con.execute("select * from g_edges where host=? order by src, dst, kind, label", (host,)):
        if e["kind"] in ("click", "link", "popup") and e["src"] in templates and e["dst"] in templates:
            item = {"template": templates[e["dst"]], "via": e["kind"],
                    "label": e["label"] if e["kind"] == "link" else json.loads(e["label"])}
            onward = groups[templates[e["src"]]]["next"]
            if item not in onward:
                onward.append(item)
    for c in con.execute("select * from g_controls where host=? order by used desc, seen desc, role, name", (host,)):
        if c["path"] in templates and c["role"] in TOGGLES:
            toggles = groups[templates[c["path"]]]["toggles"]
            item = [c["role"], c["name"]]
            if item not in toggles:
                toggles.append(item)
    return [groups[t] for t in sorted(groups)]
