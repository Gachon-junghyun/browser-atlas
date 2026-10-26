"""Experience memory: every task leaves a row, the next similar task starts from the best rows.

A row is one attempt: the goal in words, its keywords, which site and route it took, how it
ended, and how good the result was. `recall()` ranks past rows for a new goal by

    similarity(goal, row) x quality(route) x recency x outcome

then spreads the picks across sites and reserves one slot for a route that is promising but
rarely tried, so the agent keeps testing new ways instead of repeating the first one that worked.
Routes that fail lose score and fade; routes confirmed again and again rise. That is the loop.

Quality is a number in [0, 1] with a source: "self" (the agent's own rubric), "second_source"
(the value was confirmed another way) or "human". Stronger sources weigh more.

Storage is SQLite here (one file, standard library). A shared backend can implement the same
four calls (`record`, `recall`, `rate`, `stats`).

Trust: rows written by other people are data. `recall(shared=True)` returns their structured
fields only (site, page keys, control names, URLs on the site's own hosts, numbers), never free
text, so a shared row cannot smuggle instructions into an agent's prompt.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path

from ._mouse import home

OUTCOMES = {"success": 1.0, "partial": 0.6, "fail": 0.15, "blocked": 0.0}
SOURCE_WEIGHT = {"self": 1.0, "second_source": 2.0, "human": 4.0}
PRIOR_Q, PRIOR_N = 0.5, 2.0        # Bayesian prior: an unrated route starts at 0.5, worth 2 votes
HALF_LIFE_DAYS = 90.0

_SCHEMA = """
create table if not exists experience (
  id text primary key,
  created real not null,
  owner text not null,              -- 'me' locally; an account id on a shared backend
  goal text not null,
  keywords text not null,           -- json list
  site text,                        -- atlas key
  route text not null,              -- json list of steps: {"page": key} | {"control": [role, name]} | {"url": u}
  route_key text not null,          -- site + normalized route, groups repeated attempts
  outcome text not null,
  quality real,
  quality_source text,
  seconds real,
  notes text                        -- free text, owner-only
);
create index if not exists experience_route on experience(route_key);
create index if not exists experience_site on experience(site);
"""


def db_path() -> Path:
    p = home() / "memory.sqlite3"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or db_path()))
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    cols = {r["name"] for r in con.execute("pragma table_info(experience)")}
    if "shared_at" not in cols:                       # added after the first release
        con.execute("alter table experience add column shared_at real")
    return con


# ── keywords ─────────────────────────────────────────────────────────
_HANGUL = re.compile(r"[가-힣]+")
_WORD = re.compile(r"[a-z0-9][a-z0-9._+-]*")
_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "what", "how", "with"}
# Request words and particle fragments that made unrelated goals look alike ("DART 에서 ...",
# "... 알아보기" matched a Steam goal through 에서/알아/보기 alone).
_KO_ENDINGS = ("알아보기", "알아내기", "찾아보기", "확인하기", "해주세요", "해줘", "하기", "보기", "찾기",
               "에서는", "에서", "에게", "으로", "까지", "부터", "이랑", "하고", "의", "는", "은", "이", "가",
               "을", "를", "와", "과", "도", "만", "로", "랑")
_KO_STOP = {"에서", "에게", "으로", "하기", "알아", "아보", "보기", "하는", "해서", "에는", "까지", "부터",
            "하고", "찾기", "해줘", "세요", "주세", "알려", "려줘", "가져", "오기", "정도", "보고", "하면",
            "있는", "없는", "대한", "관한", "통해", "내기", "아내", "확인", "인하", "최근", "지금", "오늘",
            "알아보기", "알아내기", "찾아보기", "확인하기"}


def _strip_ending(run: str) -> str:
    """Drop one trailing particle or request ending: 삼성전자의 -> 삼성전자, 알아보기 -> ''."""
    for e in _KO_ENDINGS:
        if run.endswith(e) and (len(run) - len(e) >= 2 or run == e):
            return run[: len(run) - len(e)]
    return run


def keywords(text: str) -> list[str]:
    """Latin words, plus Hangul runs split into overlapping two-syllable pieces.

    Korean has no spaces between a noun and its particle (삼성전자의, 삼성전자는); bigrams match
    across those endings without a morphological analyzer.
    """
    t = text.lower()
    out = [w for w in _WORD.findall(t) if w not in _STOP and len(w) > 1]
    for run in _HANGUL.findall(t):
        run = _strip_ending(run)
        if not run or run in _KO_STOP:
            continue
        out.append(run)                                  # the whole word, so 광화문 matches 광화문
        out += [b for b in (run[i:i + 2] for i in range(len(run) - 1)) if b not in _KO_STOP]
    seen, uniq = set(), []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq


def similarity(a: list[str], b: list[str]) -> float:
    """Overlap coefficient: how much of the smaller keyword set the other one covers."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / min(len(sa), len(sb))


def _route_key(site: str | None, route: list) -> str:
    return (site or "") + "|" + json.dumps(route, ensure_ascii=False, sort_keys=True)


# ── write ────────────────────────────────────────────────────────────
def record(goal: str, site: str | None, route: list, outcome: str, quality: float | None = None,
           quality_source: str = "self", seconds: float | None = None, notes: str = "",
           owner: str = "me", con: sqlite3.Connection | None = None) -> str:
    """Store one attempt and return its id. `route` is structured, not prose."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}")
    if quality is not None and not 0.0 <= quality <= 1.0:
        raise ValueError("quality must be within [0, 1]")
    if quality_source not in SOURCE_WEIGHT:
        raise ValueError(f"quality_source must be one of {sorted(SOURCE_WEIGHT)}")
    con = con or connect()
    rid = uuid.uuid4().hex
    con.execute("insert into experience (id, created, owner, goal, keywords, site, route, route_key, outcome,"
                " quality, quality_source, seconds, notes) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, time.time(), owner, goal, json.dumps(keywords(goal), ensure_ascii=False), site,
                 json.dumps(route, ensure_ascii=False), _route_key(site, route), outcome,
                 quality, quality_source if quality is not None else None, seconds, notes))
    con.commit()
    return rid


def rate(rid: str, quality: float, source: str = "human", con: sqlite3.Connection | None = None) -> None:
    """Re-grade an attempt later, e.g. when a human or a second source confirms the result."""
    if source not in SOURCE_WEIGHT or not 0.0 <= quality <= 1.0:
        raise ValueError("bad quality or source")
    con = con or connect()
    con.execute("update experience set quality=?, quality_source=? where id=?", (quality, source, rid))
    con.commit()


# ── read ─────────────────────────────────────────────────────────────
def _route_scores(rows: list[sqlite3.Row]) -> dict[str, dict]:
    """Per route: Bayesian quality over every attempt, attempt count, last outcome."""
    acc: dict[str, dict] = {}
    for r in rows:
        a = acc.setdefault(r["route_key"], {"q": PRIOR_Q * PRIOR_N, "w": PRIOR_N, "n": 0, "last": 0.0})
        cap = OUTCOMES[r["outcome"]]
        q = min(r["quality"], cap) if r["quality"] is not None else cap   # a failed try is never good
        w = SOURCE_WEIGHT.get(r["quality_source"] or "self", 1.0)
        a["q"] += q * w
        a["w"] += w
        a["n"] += 1
        a["last"] = max(a["last"], r["created"])
    for a in acc.values():
        a["quality"] = a["q"] / a["w"]
    return acc


def _on_host(route_json: str, host: str) -> bool:
    from urllib.parse import urlparse
    for step in json.loads(route_json):
        h = (urlparse(str(step.get("url", ""))).hostname or "").lower() if isinstance(step, dict) else ""
        if h and (h == host or h.endswith("." + host) or host.endswith("." + h)):
            return True
    return False


def recall(goal: str, k: int = 5, explore: int = 1, shared: bool = False, site: str | None = None,
           owner: str = "me", now: float | None = None, con: sqlite3.Connection | None = None,
           host: str | None = None) -> list[dict]:
    """Best past routes for `goal`. `explore` slots go to similar but rarely tried routes.

    `site` or `host` keeps only routes on that site: a route from another site is not a starting
    point for this one, however similar the goal sounds.
    """
    con = con or connect()
    now = now or time.time()
    q_kw = keywords(goal)
    sql, args = "select * from experience", []
    if site:
        sql, args = sql + " where site = ?", [site]
    rows = con.execute(sql, args).fetchall()
    if host:
        host = host.lower().removeprefix("www.")
        rows = [r for r in rows if _on_host(r["route"], host)]
    if not shared:
        rows = [r for r in rows if r["owner"] == owner]
    routes = _route_scores(rows)
    total_n = sum(a["n"] for a in routes.values()) or 1

    best: dict[str, dict] = {}
    for r in rows:
        sim = similarity(q_kw, json.loads(r["keywords"]))
        if sim <= 0:
            continue
        a = routes[r["route_key"]]
        age_days = (now - a["last"]) / 86400
        recency = 0.5 ** (age_days / HALF_LIFE_DAYS)
        score = sim * a["quality"] * (0.5 + 0.5 * recency)
        bonus = sim * math.sqrt(2 * math.log(total_n + 1) / a["n"])       # UCB-style curiosity
        cur = best.get(r["route_key"])
        sources = (cur["sources"] if cur else set()) | {"mine" if r["owner"] == owner else "shared"}
        if cur is None or sim > cur["similarity"]:
            best[r["route_key"]] = {"row": r, "similarity": round(sim, 3), "score": score,
                                    "curiosity": bonus, "quality": round(a["quality"], 3),
                                    "attempts": a["n"], "sources": sources}
        else:
            cur["sources"] = sources

    ranked = sorted(best.values(), key=lambda c: -c["score"])
    picks, used_sites = [], {}
    for c in ranked:                                  # exploit, at most 2 per site
        if len(picks) >= max(0, k - explore):
            break
        s = c["row"]["site"]
        if used_sites.get(s, 0) >= 2:
            continue
        used_sites[s] = used_sites.get(s, 0) + 1
        picks.append({**c, "why": "best"})
    taken = {p["row"]["route_key"] for p in picks}
    rest = [c for c in best.values() if c["row"]["route_key"] not in taken]
    for c in sorted(rest, key=lambda c: -c["curiosity"])[:explore]:
        picks.append({**c, "why": "explore"})
    out = [_public(p, owner) for p in picks]
    return [o for o in out if o is not None]


def host_digest(host: str, owner: str = "me", con: sqlite3.Connection | None = None) -> dict:
    """Everything this machine tried on a host, whatever the goal was: how attempts ended, which
    controls worked, which failed and how, which pages were reached, what robots.txt said.

    `recall()` answers "which route for this goal"; this answers "what do we already know about this
    site", which is what a site with no atlas entry needs. Own rows only (their notes hold the trail).
    """
    con = con or connect()
    host = (host or "").lower().removeprefix("www.")
    rows = [r for r in con.execute("select * from experience where owner = ?", (owner,))
            if _on_host(r["route"], host)]
    outcomes: dict[str, int] = {}
    worked: dict[tuple, int] = {}
    failed: dict[tuple, int] = {}
    pages: dict[str, int] = {}
    refused: dict[str, int] = {}
    robots, last = None, 0.0
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        last = max(last, r["created"])
        try:
            notes = json.loads(r["notes"] or "{}")
        except ValueError:
            notes = {}
        robots = notes.get("robots") or robots
        for step in json.loads(r["route"]):
            if isinstance(step, dict) and step.get("url"):
                into = refused if notes.get("reason") == "robots" else pages   # refused before opening
                into[step["url"]] = into.get(step["url"], 0) + 1
            elif isinstance(step, dict) and step.get("control") and r["outcome"] in ("success", "partial"):
                k = tuple(step["control"][:2])
                worked[k] = worked.get(k, 0) + 1
        for tool, ok, what, err in notes.get("trail") or []:
            if not ok and str(err).startswith("Disallowed") and isinstance(what, str) and notes.get("reason") != "robots":
                refused[what] = refused.get(what, 0) + 1
        for tool, ok, what, err in notes.get("trail") or []:
            if not ok:
                kind = (err.split(":", 1)[0] or "error") if err else "error"
                k = (tool, json.dumps(what, ensure_ascii=False) if isinstance(what, list) else str(what), kind)
                failed[k] = failed.get(k, 0) + 1

    def top(d: dict, n: int) -> list:
        return sorted(d.items(), key=lambda kv: -kv[1])[:n]

    return {"host": host, "attempts": len(rows), "outcomes": outcomes,
            "last_seen": time.strftime("%Y-%m-%d", time.localtime(last)) if last else None,
            "robots": robots,
            "worked_controls": [[*k, n] for k, n in top(worked, 15)],
            "failed_steps": [[*k, n] for k, n in top(failed, 10)],
            "pages_reached": [[u, n] for u, n in top(pages, 10)],
            "refused_by_robots": [[u, n] for u, n in top(refused, 10)]}


def _public(p: dict, owner: str) -> dict | None:
    r = p["row"]
    mine = r["owner"] == owner
    route = json.loads(r["route"])
    if not mine:
        route = _sanitize(r["site"], route)
        if route is None:
            return None
    out = {"id": r["id"], "goal": r["goal"] if mine else None, "site": r["site"], "route": route,
           "outcome": r["outcome"], "quality": p["quality"], "attempts": p["attempts"],
           "similarity": p["similarity"], "why": p["why"],
           "age_days": round((time.time() - r["created"]) / 86400, 1), "mine": mine,
           "sources": sorted(p.get("sources") or ({"mine"} if mine else {"shared"}))}
    if mine:
        out["notes"] = r["notes"]
    return out


def _sanitize(site: str | None, route: list) -> list | None:
    """Someone else's route, reduced to things the atlas already vouches for.

    The site must be in the atlas and not blocked; URLs must be on its hosts; pages and controls
    must exist in its button map. Anything else is dropped, and an empty result drops the row.
    """
    from urllib.parse import urlparse
    from . import _atlas
    sites = _atlas.load()
    e = sites.get(site or "")
    if not e or e.get("access") == "blocked":
        return None
    hosts = _atlas.hosts_of(e)
    pages = e.get("pages") or {}
    controls = {(c.get("role"), c.get("name")) for pg in pages.values() for c in pg.get("controls", [])}
    clean = []
    for step in route if isinstance(route, list) else []:
        if not isinstance(step, dict):
            continue
        if "page" in step and step["page"] in pages:
            clean.append({"page": step["page"]})
        elif "control" in step and tuple(step["control"][:2]) in controls:
            clean.append({"control": list(step["control"][:2])})
        elif "url" in step:
            h = (urlparse(str(step["url"])).hostname or "").lower()
            if any(h == x or h.endswith("." + x) for x in hosts):
                clean.append({"url": str(step["url"])[:500]})
    return clean or None


# ── sharing ──────────────────────────────────────────────────────────
# Sharing is opt-in and decided by code, not by the agent's good manners: only the fields built
# here can leave the machine. Goal text and notes never do.
_PII = [re.compile(p) for p in (
    r"[\w.+-]+@[\w-]+\.[\w.-]+",                       # e-mail
    r"01[016789][- .]?\d{3,4}[- .]?\d{4}",            # Korean mobile number
    r"\+?\d{1,3}[- .]?\(?\d{2,4}\)?[- .]?\d{3,4}[- .]?\d{4}",  # other phone numbers
    r"\d{6}[- ]?[1-4]\d{6}",                           # resident registration number
    r"\d{3,6}-\d{2,6}-\d{2,8}",                        # bank-account-like digit groups
)]


def _has_pii(text: str) -> bool:
    return any(p.search(text) for p in _PII)


def _vocabulary(sites: dict) -> set[str]:
    """Words the public atlas already contains: anything else may be personal."""
    words: set[str] = set()
    for key, e in sites.items():
        texts = [key, e.get("label", ""), *e.get("has", []), *(e.get("go") or {}).keys()]
        for pg_key, pg in (e.get("pages") or {}).items():
            texts.append(pg_key)
            texts += [c.get("name", "") for c in pg.get("controls", [])]
        for t in texts:
            words.update(keywords(str(t)))
    return words


def shareable(owner: str = "me", con: sqlite3.Connection | None = None, unshared_only: bool = False) -> list[dict]:
    """Rows of `owner` reduced to what may be shared; rows that cannot be made safe are skipped.

    Only sites in the repository map count as public: a site you added to your own overlay is
    private even if you marked it `open` there.
    """
    from urllib.parse import urlparse, urlunparse
    from . import _atlas
    con = con or connect()
    public = _atlas.load_base()
    vocab = _vocabulary(public)
    out = []
    sql = "select * from experience where owner = ?" + (" and shared_at is null" if unshared_only else "")
    for r in con.execute(sql, (owner,)).fetchall():
        e = public.get(r["site"] or "")
        if not e or e.get("access") not in ("open", "api"):
            continue                                   # logged-in and private sites are personal by nature
        route = _sanitize(r["site"], json.loads(r["route"]))
        if route is None:
            continue
        for step in route:
            if "url" in step:                          # drop query and fragment: searches live there
                u = urlparse(step["url"])
                step["url"] = urlunparse((u.scheme, u.netloc, u.path, "", "", ""))
        kws = [w for w in json.loads(r["keywords"]) if w in vocab]
        row = {"id": r["id"], "site": r["site"], "keywords": kws, "route": route, "outcome": r["outcome"],
               "quality": r["quality"], "quality_source": r["quality_source"],
               "seconds": r["seconds"], "day": time.strftime("%Y-%m-%d", time.localtime(r["created"]))}
        if _has_pii(json.dumps([route, kws], ensure_ascii=False)):   # the free-form parts only
            continue
        out.append(row)
    return out


def reindex(con: sqlite3.Connection | None = None) -> dict:
    """Recompute keywords from the stored goals and fill missing sites from route URLs.

    Run after the tokenizer or the atlas changes: rows keep the keywords they were stored with,
    so old rows would otherwise match by yesterday's rules.
    """
    from . import _atlas
    con = con or connect()
    sites = _atlas.load()
    changed = filled = 0
    for r in con.execute("select id, goal, keywords, site, route from experience").fetchall():
        kws = json.dumps(keywords(r["goal"]), ensure_ascii=False)
        site = r["site"]
        if not site:
            for step in json.loads(r["route"]):
                hit = _atlas.site_for(str(step.get("url", "")), sites) if isinstance(step, dict) else None
                if hit:
                    site = hit[0]
                    filled += 1
                    break
        if kws != r["keywords"] or site != r["site"]:
            route = json.loads(r["route"])
            con.execute("update experience set keywords = ?, site = ?, route_key = ? where id = ?",
                        (kws, site, _route_key(site, route), r["id"]))
            changed += 1
    con.commit()
    return {"rows_changed": changed, "sites_filled": filled}


def mark_shared(ids: list[str], con: sqlite3.Connection | None = None) -> None:
    con = con or connect()
    con.executemany("update experience set shared_at = ? where id = ?", [(time.time(), i) for i in ids])
    con.commit()


def add_community(rows: list[dict], con: sqlite3.Connection | None = None) -> int:
    """Store rows downloaded from the shared database (owner 'community'); returns how many were new.

    Shared rows carry no goal text, so their keywords stand in for it. `recall(shared=True)` still
    passes them through `_sanitize`, so only routes the atlas vouches for reach an agent.
    """
    con = con or connect()
    n = 0
    for r in rows:
        if con.execute("select 1 from experience where id = ?", (r["id"],)).fetchone():
            continue
        if r.get("outcome") not in OUTCOMES:
            continue
        kws = [str(k)[:40] for k in (r.get("keywords") or [])][:40]
        route = r.get("route") if isinstance(r.get("route"), list) else []
        try:
            created = time.mktime(time.strptime(str(r.get("day")), "%Y-%m-%d"))
        except ValueError:
            created = time.time()
        q = r.get("quality")
        q = min(1.0, max(0.0, float(q))) if isinstance(q, (int, float)) else None
        src = r.get("quality_source") if r.get("quality_source") in SOURCE_WEIGHT else "self"
        con.execute("insert into experience (id, created, owner, goal, keywords, site, route, route_key, outcome,"
                    " quality, quality_source, seconds, notes, shared_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], created, "community", " ".join(kws), json.dumps(kws, ensure_ascii=False), r.get("site"),
                     json.dumps(route, ensure_ascii=False), _route_key(r.get("site"), route), r["outcome"],
                     q, src if q is not None else None, r.get("seconds"), "", time.time()))
        n += 1
    con.commit()
    return n


def stats(con: sqlite3.Connection | None = None) -> dict:
    con = con or connect()
    rows = con.execute("select outcome, count(*) n from experience group by outcome").fetchall()
    routes = con.execute("select count(distinct route_key) from experience").fetchone()[0]
    return {"attempts": sum(r["n"] for r in rows), "routes": routes,
            "by_outcome": {r["outcome"]: r["n"] for r in rows}}
