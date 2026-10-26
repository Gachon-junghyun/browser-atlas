"""Which site for this task: the atlas and the experience memory, ranked together.

`atlas_find` matches words and knows nothing about how a site went; `memory_recall` ranks routes, not
sites. `site_rank(goal)` gathers candidates from both (sites in the map whose words match, and every
site or unmapped host this machine or the shared rows tried for a similar goal) and scores each one:

    score = relevance x (0.5 x quality + 0.5 x readiness) x recent-block factor

- relevance: the better of the atlas word match (share of the goal's word weight the entry covers, rare
  words weighing more) and the most similar past goal
- quality: Bayesian over the similar attempts on that site, each weighted by its similarity (prior 0.5 x 2)
- readiness: how ready the site is to work on: a button map, an official API, a login, or no reviewed entry
- recent block: a captcha, rate limit or robots refusal in the last week cuts the score to a third

Blocked sites are never candidates; they come back under `excluded` with the reason. One slot goes to
a matching site nobody tried yet, so the ranking keeps testing new places instead of repeating the first
one that worked. Every score comes with the reasons that made it, for the agent and for people.
"""
from __future__ import annotations

import json
import math
import re
import time
from urllib.parse import urlparse

from . import _atlas, _memory

READINESS = {"map": 1.0, "api": 0.8, "open": 0.7, "session": 0.5, "unmapped": 0.4}
RECENT_DAYS = 7
BLOCK_FACTOR = 1 / 3
MIN_MATCH = 0.25                                          # share of the goal's word weight an entry must cover
_WHOLE = re.compile(r"[a-z0-9]+")


def _entry_text(key: str, e: dict) -> str:
    """The words an entry is found by: its name, what it has, its entry and page names, what its
    controls do and its official route. Positions (`where`) and traps say nothing about the topic."""
    parts = [key, e.get("label", ""), *e.get("has", []), *(e.get("go") or {}).keys(), str(e.get("prefer") or "")]
    for pk, pg in (e.get("pages") or {}).items():
        parts.append(pk)
        for c in pg.get("controls", []) if isinstance(pg, dict) else []:
            if not c.get("dynamic"):
                parts += [str(c.get("name") or ""), str(c.get("does") or "")]
    return " ".join(parts)


def _row_place(r, sites: dict) -> tuple[str | None, str]:
    """(atlas key or None, host) for an experience row. Rows recorded before a site joined the map
    have no key; their host still finds it now."""
    host = ""
    try:
        host = str(json.loads(r["notes"] or "{}").get("host") or "")
    except ValueError:
        pass
    if not host:
        for step in json.loads(r["route"]):
            if isinstance(step, dict) and step.get("url"):
                host = urlparse(step["url"]).hostname or ""
                break
    host = host.lower().removeprefix("www.")
    if r["site"] and r["site"] in sites:
        return r["site"], host
    hit = None
    for h in (host, "www." + host) if host else ():       # entries list hosts with or without www.
        hit = hit or _atlas.site_for("https://" + h + "/", sites)
    return (hit[0] if hit else None), host


def site_rank(goal: str, k: int = 5, now: float | None = None) -> dict:
    now = now or time.time()
    sites = _atlas.load()
    q_kw = _memory.keywords(goal)
    low = goal.lower()

    cands: dict[str, dict] = {}

    def cand(key: str | None, host: str) -> dict:
        cid = key or host
        if cid not in cands:
            cands[cid] = {"site": key, "host": host, "match": 0.0, "sim": 0.0, "w": 0.0, "qsum": 0.0,
                          "attempts": 0, "last": 0.0, "last_outcome": None, "recent_block": False}
        return cands[cid]

    # atlas: how much of the goal's weight the entry's words cover (key, label, what it has, entry names).
    # A word on many entries ("search", "검색") weighs little; a two-syllable piece of a Hangul word half
    # as much as a whole word (마켓플레이스 contains 플레 and 레이, which says nothing about 인플레이션).
    docs = {key: set(_memory.keywords(_entry_text(key, e))) for key, e in sites.items()}
    df: dict[str, int] = {}
    for words in docs.values():
        for w in words:
            df[w] = df.get(w, 0) + 1
    whole = set(_WHOLE.findall(low)) | {w for w in _memory._HANGUL.findall(low)}
    weight = {w: math.log(1 + len(docs) / df.get(w, 1)) * (1.0 if w in whole or len(w) > 2 else 0.5) for w in q_kw}
    total = sum(weight.values()) or 1.0

    def cover(words) -> float:
        return sum(weight[w] for w in q_kw if w in words) / total

    for key, words in docs.items():
        e = sites[key]
        m = cover(words)
        if key in low or any(h and h in low for h in _atlas.hosts_of(e)):
            m = 1.0                                       # named outright
        if m >= MIN_MATCH:
            cand(key, (_atlas.hosts_of(e) or [""])[0])["match"] = m

    # experience: every attempt with a similar goal, on whichever site or host it ran
    for r in _memory.connect().execute("select * from experience"):
        sim = cover(set(json.loads(r["keywords"])))      # same weights as the atlas match
        if sim < MIN_MATCH:
            continue
        key, host = _row_place(r, sites)
        if not (key or host):
            continue
        c = cand(key, host)
        cap = _memory.OUTCOMES[r["outcome"]]
        q = min(r["quality"], cap) if r["quality"] is not None else cap
        c["sim"] = max(c["sim"], sim)
        c["w"] += sim
        c["qsum"] += sim * q
        c["attempts"] += 1
        if r["created"] > c["last"]:
            c["last"], c["last_outcome"] = r["created"], r["outcome"]
        if r["outcome"] == "blocked" and now - r["created"] < RECENT_DAYS * 86400:
            c["recent_block"] = True

    ranked, excluded = [], []
    for c in cands.values():
        e = sites.get(c["site"] or "") or {}
        access = e.get("access") if e else None
        if access == "blocked":
            excluded.append({"site": c["site"], "label": e.get("label"), "why": e.get("why"),
                             "instead": e.get("prefer")})
            continue
        why = []
        relevance = max(c["match"], c["sim"])
        if c["match"]:
            why.append(f"atlas words match {c['match']:.2f}")
        if c["sim"]:
            why.append(f"a past goal matches {c['sim']:.2f}")
        quality = (_memory.PRIOR_Q * _memory.PRIOR_N + c["qsum"]) / (_memory.PRIOR_N + c["w"])
        if c["attempts"]:
            why.append(f"{c['attempts']} similar attempts, quality {quality:.2f}, last {c['last_outcome']}")
        else:
            why.append("never tried for a goal like this")
        if not e:
            ready, how = READINESS["unmapped"], "unmapped"
            why.append("not in the atlas: robots.txt decides, read the terms first")
        elif access == "api" or e.get("prefer"):
            ready, how = max(READINESS["api"], READINESS["map"] if e.get("pages") else 0), "api"
            why.append("official route: " + str(e.get("prefer") or "API")[:120])
        elif access == "session":
            ready, how = READINESS["session"], "session"
            why.append("needs a human login once")
        elif e.get("pages"):
            ready, how = READINESS["map"], "map"
            why.append("button map")
        else:
            ready, how = READINESS["open"], "open"
            why.append("entry URLs only, no button map")
        factor = BLOCK_FACTOR if c["recent_block"] else 1.0
        if c["recent_block"]:
            why.append(f"blocked within {RECENT_DAYS} days (captcha, rate limit or robots)")
        score = relevance * (0.5 * quality + 0.5 * ready) * factor
        ranked.append({"site": c["site"], "host": c["host"], "label": e.get("label") if e else None,
                       "score": round(score, 3), "access": access or "unmapped", "use": how,
                       "go": e.get("go") if e else None, "prefer": e.get("prefer") if e else None,
                       "attempts": c["attempts"], "quality": round(quality, 3), "why": why})

    ranked.sort(key=lambda x: -x["score"])
    if k < 2:
        picks = [dict(x, pick="best") for x in ranked[:max(0, k)]]
    else:
        picks = [dict(x, pick="best") for x in ranked[:k - 1]]
        taken = {(x["site"], x["host"]) for x in picks}
        fresh = [x for x in ranked if x["attempts"] == 0 and (x["site"], x["host"]) not in taken]
        if fresh:                                         # one place nobody tried yet
            picks.append(dict(fresh[0], pick="explore"))
        elif len(ranked) >= k:
            picks.append(dict(ranked[k - 1], pick="best"))
    return {"goal": goal, "ranked": picks, "excluded": excluded[:5],
            "advice": "start at the first; for use=api take the official route; an explore pick is worth one try. "
                      "Scores come from this machine's attempts and the reviewed map, not from the sites"}
