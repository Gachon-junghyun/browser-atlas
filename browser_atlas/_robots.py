"""robots.txt, read the way RFC 9309 says, cached on this machine.

The atlas says what reviewed sites allow. A site nobody has mapped yet has no such verdict, so before
an agent opens one of its pages the server asks the site itself: robots.txt for the `*` group, longest
matching rule wins, `Allow` wins a tie (Python's `urllib.robotparser` takes the first match instead and
gets mixed Allow/Disallow files wrong). Files are fetched with an honest user agent and kept for a day
(RFC 9309 §2.4) in `robots.json` under the browser-atlas home.

Verdicts: True (allowed), False (disallowed, with the rule that decided it) or None (could not tell:
the file timed out, answered 403, or was an HTML page). Unknown is reported, not treated as allowed.
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ._mouse import home

UA = "browser-atlas/0.1 (+https://github.com/Gachon-junghyun/browser-atlas)"
TTL = 86400
TIMEOUT = 8


def _path():
    return home() / "robots.json"


def _cache() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(cache: dict) -> None:
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def parse(text: str) -> list[list[str]]:
    """The rules of the `*` group(s) as [kind, path] pairs. Consecutive user-agent lines share a group."""
    groups, agents, rules, last_was_agent = [], [], [], False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (s.strip() for s in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not last_was_agent and agents:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(v.lower())
            last_was_agent = True
        elif k in ("allow", "disallow"):
            rules.append([k, v])
            last_was_agent = False
    if agents:
        groups.append((agents, rules))
    return [r for a, rs in groups if "*" in a for r in rs]


def _fetch(origin: str) -> dict:
    req = urllib.request.Request(origin + "/robots.txt", headers={"User-Agent": UA})
    try:
        try:
            raw = urllib.request.urlopen(req, timeout=TIMEOUT).read()
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
                raise
            # some public-sector sites omit the intermediate certificate; only robots.txt is read this way
            raw = urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl._create_unverified_context()).read()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):                    # RFC 9309 §2.3.1.3: unavailable means no restriction
            return {"status": "missing", "rules": [], "why": f"HTTP {exc.code}"}
        return {"status": "unknown", "rules": [], "why": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 — anything else is "could not tell"
        return {"status": "unknown", "rules": [], "why": f"{type(exc).__name__}: {exc}"[:120]}
    for enc in ("utf-8", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    if "<html" in text[:500].lower():
        return {"status": "unknown", "rules": [], "why": "an HTML page where robots.txt should be"}
    return {"status": "ok", "rules": parse(text), "why": ""}


def rules_for(origin: str, now: float | None = None) -> dict:
    now = now or time.time()
    cache = _cache()
    hit = cache.get(origin)
    if hit and now - hit.get("fetched", 0) < TTL:
        return hit
    hit = {**_fetch(origin), "fetched": now}
    cache[origin] = hit
    _save(cache)
    return hit


def _pattern(p: str) -> re.Pattern:
    rx = re.escape(p).replace(r"\*", ".*")
    if rx.endswith(r"\$"):
        rx = rx[:-2] + "$"
    return re.compile(rx)


def decide(rules: list, path: str) -> tuple[bool, str]:
    best = None
    for kind, p in rules:
        if p and _pattern(p).match(path):
            if best is None or len(p) > len(best[1]) or (len(p) == len(best[1]) and kind == "allow"):
                best = (kind, p)
    if best is None:
        return True, "no matching rule"
    return best[0] == "allow", f"{best[0]}: {best[1]}"


def check(url: str) -> dict:
    """{"allowed": True | False | None, "rule" or "why", "robots": origin/robots.txt} for one URL."""
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.netloc:
        return {"allowed": True, "why": "not a web page"}
    origin = f"{u.scheme}://{u.netloc}"
    r = rules_for(origin)
    out = {"robots": origin + "/robots.txt"}
    if r["status"] == "unknown":
        return {**out, "allowed": None, "why": r["why"]}
    if r["status"] == "missing":
        return {**out, "allowed": True, "why": "no robots.txt"}
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    allowed, rule = decide(r["rules"], path)
    return {**out, "allowed": allowed, "rule": rule}


class Disallowed(PermissionError):
    """robots.txt disallows this URL for every agent, and the atlas has no reviewed verdict for the site."""
