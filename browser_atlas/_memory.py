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
    return con


# ── keywords ─────────────────────────────────────────────────────────
_HANGUL = re.compile(r"[가-힣]+")
_WORD = re.compile(r"[a-z0-9][a-z0-9._+-]*")
_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "what", "how", "with"}


def keywords(text: str) -> list[str]:
    """Latin words, plus Hangul runs split into overlapping two-syllable pieces.

    Korean has no spaces between a noun and its particle (삼성전자의, 삼성전자는); bigrams match
    across those endings without a morphological analyzer.
    """
    t = text.lower()
    out = [w for w in _WORD.findall(t) if w not in _STOP and len(w) > 1]
    for run in _HANGUL.findall(t):
        if len(run) == 1:
            out.append(run)
        out += [run[i:i + 2] for i in range(len(run) - 1)]
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
    con.execute("insert into experience values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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


def recall(goal: str, k: int = 5, explore: int = 1, shared: bool = False, site: str | None = None,
           owner: str = "me", now: float | None = None, con: sqlite3.Connection | None = None) -> list[dict]:
    """Best past routes for `goal`. `explore` slots go to similar but rarely tried routes."""
    con = con or connect()
    now = now or time.time()
    q_kw = keywords(goal)
    sql, args = "select * from experience", []
    if site:
        sql, args = sql + " where site = ?", [site]
    rows = con.execute(sql, args).fetchall()
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
        if cur is None or sim > cur["similarity"]:
            best[r["route_key"]] = {"row": r, "similarity": round(sim, 3), "score": score,
                                    "curiosity": bonus, "quality": round(a["quality"], 3),
                                    "attempts": a["n"]}

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
           "age_days": round((time.time() - r["created"]) / 86400, 1), "mine": mine}
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


def shareable(owner: str = "me", con: sqlite3.Connection | None = None) -> list[dict]:
    """Rows of `owner` reduced to what may be shared; rows that cannot be made safe are skipped."""
    from urllib.parse import urlparse, urlunparse
    from . import _atlas
    con = con or connect()
    sites = _atlas.load()
    vocab = _vocabulary(sites)
    out = []
    for r in con.execute("select * from experience where owner = ?", (owner,)).fetchall():
        e = sites.get(r["site"] or "")
        if not e or e.get("access") not in ("open", "api"):
            continue                                   # logged-in sites are personal by nature
        route = _sanitize(r["site"], json.loads(r["route"]))
        if route is None:
            continue
        for step in route:
            if "url" in step:                          # drop query and fragment: searches live there
                u = urlparse(step["url"])
                step["url"] = urlunparse((u.scheme, u.netloc, u.path, "", "", ""))
        kws = [w for w in json.loads(r["keywords"]) if w in vocab]
        row = {"site": r["site"], "keywords": kws, "route": route, "outcome": r["outcome"],
               "quality": r["quality"], "quality_source": r["quality_source"],
               "seconds": r["seconds"], "day": time.strftime("%Y-%m-%d", time.gmtime(r["created"]))}
        if _has_pii(json.dumps([route, kws], ensure_ascii=False)):   # the free-form parts only
            continue
        out.append(row)
    return out


def stats(con: sqlite3.Connection | None = None) -> dict:
    con = con or connect()
    rows = con.execute("select outcome, count(*) n from experience group by outcome").fetchall()
    routes = con.execute("select count(distinct route_key) from experience").fetchone()[0]
    return {"attempts": sum(r["n"] for r in rows), "routes": routes,
            "by_outcome": {r["outcome"]: r["n"] for r in rows}}
