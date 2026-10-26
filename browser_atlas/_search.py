"""Search several engines and spread the picks, so research does not lock onto one source.

A single engine ranks the same few big sites first, and an agent that always opens the top
result of one engine ends up reading one site's view of everything. Here the same query goes to
several engines; their results are interleaved (one from each engine in turn), each domain
appears once, and domains you already read a lot are marked, so the next reads widen the base.
The pure parts are here; `mcp_server` drives the browser.
"""
from __future__ import annotations

import base64
import random
import re
from urllib.parse import parse_qs, quote_plus, urlparse

ENGINES = {
    "naver": "https://search.naver.com/search.naver?query={q}",
    "google": "https://www.google.com/search?q={q}&hl=ko",
    "bing": "https://www.bing.com/search?q={q}",
    "daum": "https://search.daum.net/search?q={q}",
    "duckduckgo": "https://html.duckduckgo.com/html/?q={q}",
}

# The engine's own pages, login/help links and ad redirects are not sources.
_NOT_SOURCES = ("google.", "gstatic.com", "googleusercontent.com", "bing.com", "microsoft.com", "msn.com",
                "duckduckgo.com", "search.naver.com", "nid.naver.com", "help.naver.com", "ad.search.naver.com",
                "adcr.naver.com", "keep.naver.com", "search.daum.net", "logins.daum.net", "cs.daum.net",
                "accounts.kakao.com", "policy.naver.com", "policies.google.com", "support.google.com")

LINKS_JS = ("Array.from(document.querySelectorAll('a[href]')).map(a=>{const r=a.getBoundingClientRect();"
            "return [(a.innerText||'').trim().replace(/\\s+/g,' ').slice(0,160), a.href, r.width>0&&r.height>0]})"
            ".filter(x=>x[2]&&x[0].length>=6).map(x=>[x[0],x[1]]).slice(0,400)")


def search_url(engine: str, query: str) -> str:
    return ENGINES[engine].format(q=quote_plus(query))


def unwrap(url: str) -> str:
    """Engines wrap result links in click trackers; return the real destination."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    qs = parse_qs(u.query)
    if host.endswith("duckduckgo.com") and u.path.startswith("/l/") and qs.get("uddg"):
        return qs["uddg"][0]
    if host.endswith("bing.com") and u.path.startswith("/ck/") and qs.get("u"):
        v = qs["u"][0]
        if v.startswith("a1"):
            try:
                return base64.urlsafe_b64decode(v[2:] + "=" * (-len(v[2:]) % 4)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return url
    if host.startswith("www.google.") and u.path == "/url":
        return (qs.get("q") or qs.get("url") or [url])[0]
    return url


def domain(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


# Inside the engines' own families only these are sources (blogs, news, cafés, Q&A); the rest is
# the engine's furniture (shopping, dictionaries, maps, site maps), observed leaking into results.
_FAMILY_SOURCES = {
    "naver.com": {"blog.naver.com", "m.blog.naver.com", "cafe.naver.com", "m.cafe.naver.com", "news.naver.com",
                  "n.news.naver.com", "m.news.naver.com", "post.naver.com", "kin.naver.com", "terms.naver.com",
                  "finance.naver.com", "stock.naver.com"},
    "daum.net": {"v.daum.net", "news.v.daum.net", "cafe.daum.net", "m.cafe.daum.net"},
}
_DISPLAY_URL = re.compile(r"https?://[^\s›»]+")


def is_source(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    h = domain(url)
    if not h or any(h == n.rstrip(".") or h.startswith(n) or h.endswith("." + n) or n in h for n in _NOT_SOURCES):
        return False
    for family, ok in _FAMILY_SOURCES.items():
        if h == family or h.endswith("." + family):
            return h in ok
    return True


def _google_goto(href: str, text: str) -> tuple[str, str] | None:
    """Google now wraps results as google.com/goto?url=<opaque>. The link still works when opened,
    and the visible text carries the real address ('Title Site https://site.com › path')."""
    u = urlparse(href)
    if not ((u.hostname or "").startswith("www.google.") and u.path == "/goto"):
        return None
    m = _DISPLAY_URL.search(text)
    return (href, domain(m.group(0))) if m else None


def _title(texts: list[str]) -> str:
    """Prefer a heading-like text over the 'Site https://site › path' citation line."""
    clean = [t for t in texts if "://" not in t and "›" not in t]
    if clean:
        return max(clean, key=len)
    t = max(texts, key=len)
    return _DISPLAY_URL.split(t)[0].strip(" ·-") or t


def results(links: list, per_engine: int = 8) -> list[dict]:
    """[[text, href], ...] from a results page -> the engine's top sources, in page order, deduplicated.
    Several links often point at one result (title, citation, thumbnail); they are merged."""
    order, texts, doms = [], {}, {}
    for text, href in links:
        g = _google_goto(href, text)
        url, dom = g if g else (unwrap(href), None)
        dom = dom or domain(url)
        if g is None and not is_source(url):
            continue
        if g is not None and not is_source("https://" + dom):
            continue
        key = url.split("#")[0].rstrip("/")
        if key not in texts:
            order.append((key, url))
            texts[key], doms[key] = [], dom
        texts[key].append(text)
    out = []
    for key, url in order[:per_engine]:
        out.append({"title": _title(texts[key]), "url": url, "domain": doms[key]})
    return out


def spread(by_engine: dict[str, list[dict]], per_domain: int = 1, familiar: dict[str, int] | None = None,
           rng: random.Random | None = None) -> list[dict]:
    """Interleave engines (one result from each in turn, engine order shuffled), at most `per_domain`
    per domain. Each pick says which engines listed the same domain (agreement) and how often you
    already used that domain (familiar), so the reader can deliberately go somewhere new."""
    rng = rng or random.Random()
    engines = [e for e in by_engine if by_engine[e]]
    rng.shuffle(engines)
    listed: dict[str, set] = {}
    for e, rows in by_engine.items():
        for r in rows:
            listed.setdefault(r["domain"], set()).add(e)
    queues = {e: list(by_engine[e]) for e in engines}
    taken: dict[str, int] = {}
    out = []
    while any(queues.values()):
        for e in engines:
            while queues[e]:
                r = queues[e].pop(0)
                if taken.get(r["domain"], 0) >= per_domain:
                    continue
                taken[r["domain"]] = taken.get(r["domain"], 0) + 1
                out.append({**r, "from_engine": e, "engines": sorted(listed[r["domain"]]),
                            "familiar": (familiar or {}).get(r["domain"], 0)})
                break
    return out
