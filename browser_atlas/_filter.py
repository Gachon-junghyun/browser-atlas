"""Sensitive-word filter for this server's output: tool results never carry the words you listed.

Switchable word sets live outside the repo, in ~/.browser_atlas/filters.json (or $BROWSER_ATLAS_HOME):

    {"on": ["people", "company"],
     "sets": {"people":  {"mode": "prune", "up": 1, "words": {"Jane Doe": "Person B"}},
              "company": {"mode": "swap",            "words": {"Acme Corp": "Company A"}}},
     "ignore_case": true,
     "block_media": true}

Each set maps a word to its stand-in; only sets named in "on" apply. The file is re-read when it
changes, so switching needs no restart. Edit it by hand (`edit` opens it): never type the words into
a chat, that is the leak this exists to stop.

Every set has a character, its "mode":
  "swap"  (default) only the text changes: a listed word becomes its stand-in, the page stays whole.
  "prune" before `browser_read`, every element whose text or alt/title/aria-label/placeholder/value
          holds one of this set's words is found, and the ancestor "up" levels above it (default 1,
          the element's parent; 0 is the element itself) is deleted from the live page (never body or
          html; the highest element below body instead). Whatever survives is still swapped as text.
          The words never enter the page: it hands its texts over, matching happens here, only
          indices go back.
A set written the old way, a bare {"word": "stand-in"} map, is a "swap" set, unless the file still has
the old top-level "prune_up": N, which makes it a "prune" set with "up" N.

While a set is on, every tool result is masked in `mcp_result()` (the server's last step), and
screenshots are withheld when "block_media" is true (the words on an image cannot be masked).
This covers what this server returns, in any client. What the client reads by itself (its own file
or shell tools, the user's prompt) is outside this server and not filtered here.

What this server keeps is masked too, on the way to disk: the memory and page-graph database
(`MaskedConnection`, every string bound into a query), open episodes and proposals, and captures. So a
word listed while a page is read never lands in storage, and switching the filter off or editing the
list later cannot bring it back. Storage written before a word was listed still holds it.

CLI. Its output can reach a model, so it never prints the words themselves:
  python -m browser_atlas filter status
  python -m browser_atlas filter on [SET ...]       # no names: every set
  python -m browser_atlas filter off [SET ...]      # no names: all off
  python -m browser_atlas filter mode SET swap|prune [UP]   # a set's character
  python -m browser_atlas filter edit               # open the word file in an editor (creates it)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

MODES = ("swap", "prune")
DEFAULT_UP = 1
TEMPLATE = {"on": [], "sets": {"filter1": {"mode": "swap", "up": DEFAULT_UP,
                                           "words": {"example word": "stand-in"}}},
            "ignore_case": True, "block_media": True}


def home() -> Path:
    return Path(os.environ.get("BROWSER_ATLAS_HOME") or Path.home() / ".browser_atlas")


def config_path() -> Path:
    return home() / "filters.json"


_cache: dict = {"key": None, "cfg": None, "m": None}


def load() -> dict:
    """The config, re-read only when the file changes. A missing file means: no filter."""
    p = config_path()
    try:
        st = p.stat()
    except OSError:
        return {"on": [], "sets": {}}
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _cache["key"] != key:
        try:
            cfg = json.loads(p.read_text(encoding="utf-8-sig"))
            if not isinstance(cfg, dict) or not isinstance(cfg.get("sets", {}), dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            cfg = {"on": [], "sets": {}, "_broken": True}
        _cache.update(key=key, cfg=cfg, m=None)
    return _cache["cfg"]


def save(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")},
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


class Matcher:
    """One pass, longest word first, so a stand-in is never filtered again in the same pass."""

    def __init__(self, mapping: dict, ignore_case: bool = True):
        self.ic = ignore_case
        self.map = {(k.casefold() if ignore_case else k): str(v)
                    for k, v in mapping.items() if isinstance(k, str) and k.strip()}
        keys = sorted(self.map, key=len, reverse=True)
        flags = re.IGNORECASE if ignore_case else 0
        self.rx = re.compile("|".join(map(re.escape, keys)), flags) if keys else None

    def _one(self, m: re.Match) -> str:
        w = m.group(0)
        return self.map.get(w.casefold() if self.ic else w) or self.map.get(w.lower()) or "■"

    def sub(self, s: str) -> str:
        if not self.rx or not s:
            return s
        return self.rx.sub(self._one, s)

    def hit(self, s) -> bool:
        return bool(self.rx and isinstance(s, str) and s and self.rx.search(s))

    def chained(self) -> int:
        """Stand-ins that are themselves listed words: masking would not be stable."""
        return sum(1 for v in self.map.values() if self.hit(v))


def _int(v, default: int) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def set_spec(cfg: dict, raw) -> dict | None:
    """One set as {"mode", "up", "words"}; the old bare word map is read as well."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("words"), dict):
        mode = raw.get("mode", "swap")
        return {"mode": mode if mode in MODES else "swap",
                "up": _int(raw.get("up", DEFAULT_UP), DEFAULT_UP), "words": raw["words"]}
    legacy_up = _int(cfg.get("prune_up", 0) or 0, 0)       # old global switch, kept for old files
    return {"mode": "prune" if legacy_up > 0 else "swap", "up": legacy_up or DEFAULT_UP, "words": raw}


def active_sets(cfg: dict | None = None) -> dict:
    cfg = cfg or load()
    sets = cfg.get("sets") or {}
    out = {}
    for n in cfg.get("on") or []:
        spec = set_spec(cfg, sets.get(n))
        if spec:
            out[n] = spec
    return out


def matcher(cfg: dict | None = None) -> Matcher | None:
    """The active filter, or None when nothing is on."""
    cfg = cfg or load()
    cached = cfg is _cache["cfg"]
    if cached and _cache["m"] is not None:
        return _cache["m"] or None
    mapping: dict = {}
    for spec in active_sets(cfg).values():
        mapping.update(spec["words"])
    m = Matcher(mapping, bool(cfg.get("ignore_case", True))) if mapping else None
    if cached:
        _cache["m"] = m or False
    return m


def sub(s):
    """One string through the active filter; anything else, or no filter, comes back as it is."""
    m = matcher()
    return m.sub(s) if m and isinstance(s, str) else s


def _deep(o, m: Matcher):
    if isinstance(o, str):
        return m.sub(o)
    if isinstance(o, dict):
        return {(m.sub(k) if isinstance(k, str) else k): _deep(v, m) for k, v in o.items()}
    if isinstance(o, list):
        return [_deep(x, m) for x in o]
    if isinstance(o, tuple):
        return tuple(_deep(x, m) for x in o)
    return o


def mask(obj):
    """Every string in a JSON-like structure (keys included) through the active filter."""
    m = matcher()
    return _deep(obj, m) if m else obj


def _params(p):
    m = matcher()
    if not m or not p:
        return p
    if isinstance(p, dict):
        return {k: (m.sub(v) if isinstance(v, str) else v) for k, v in p.items()}
    return tuple(m.sub(v) if isinstance(v, str) else v for v in p)


class MaskedConnection(sqlite3.Connection):
    """sqlite3 connection whose bound string parameters go through the filter: what is written is
    masked, and lookups by a masked key find the masked row."""

    def execute(self, sql, parameters=(), /):
        return super().execute(sql, _params(parameters))

    def executemany(self, sql, seq, /):
        return super().executemany(sql, (_params(p) for p in seq))


_PRUNE_COLLECT_JS = r"""(() => {
  const root = document.body || document.documentElement, els = [], texts = [];
  if (!root) return [];
  const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const el = n.parentElement;
    if (el && !skip.has(el.tagName) && n.nodeValue && n.nodeValue.trim()) { els.push(el); texts.push(n.nodeValue); }
  }
  const attrs = ['alt', 'title', 'aria-label', 'placeholder', 'value'];
  for (const el of root.querySelectorAll('[alt],[title],[aria-label],[placeholder],input[value]')) {
    const t = attrs.map(a => el.getAttribute(a) || '').join(' ');
    if (t.trim()) { els.push(el); texts.push(t); }
  }
  window.__atlasPrune = els;
  return texts;
})()"""

_PRUNE_CUT_JS = """((hits) => {
  const els = window.__atlasPrune || [], body = document.body, top = document.documentElement, cut = new Set();
  window.__atlasPrune = null;
  for (const [i, up] of hits) {
    let el = els[i];
    if (!el || el === body || el === top) continue;
    for (let k = 0; k < up; k++) {
      const p = el.parentElement;
      if (!p || p === body || p === top) break;
      el = p;
    }
    cut.add(el);
  }
  let removed = 0;
  for (const el of cut) {
    let a = el.parentElement, inside = false;
    while (a) { if (cut.has(a)) { inside = true; break; } a = a.parentElement; }
    if (!inside && el.isConnected) { el.remove(); removed++; }
  }
  return removed;
})(%s)"""


def prune_sets(cfg: dict | None = None) -> list[tuple[Matcher, int]]:
    """(matcher, up) for every set that is on and has the "prune" character."""
    cfg = cfg or load()
    ic = bool(cfg.get("ignore_case", True))
    out = []
    for spec in active_sets(cfg).values():
        if spec["mode"] == "prune":
            m = Matcher(spec["words"], ic)
            if m.rx:
                out.append((m, spec["up"]))
    return out


def prune(c) -> int:
    """Delete from the live page, for every "prune" set that is on, the ancestor `up` levels above
    every element holding one of its words. Where sets disagree on one element, the larger `up` wins.
    Returns how many elements were removed (0 when no prune set is on). `c` is a CDP session with eval()."""
    ps = prune_sets()
    if not ps:
        return 0
    texts = c.eval(_PRUNE_COLLECT_JS) or []
    hits = []
    for i, t in enumerate(texts):
        ups = [up for m, up in ps if m.hit(t)]
        if ups:
            hits.append([i, max(ups)])
    if not hits:
        c.eval("window.__atlasPrune = null")
        return 0
    return int(c.eval(_PRUNE_CUT_JS % json.dumps(hits)) or 0)


def mcp_result(result: dict) -> dict:
    """The server's last step: mask text blocks, withhold images while a filter is on."""
    m = matcher()
    if not m or not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return result
    block_media = load().get("block_media", True)
    out = []
    for b in result["content"]:
        if b.get("type") == "text":
            out.append({**b, "text": m.sub(b.get("text", ""))})
        elif b.get("type") == "image" and block_media:
            out.append({"type": "text", "text": "[sensitive-word filter is on: image withheld, "
                                                "the words on it cannot be masked]"})
        else:
            out.append(b)
    return {**result, "content": out}


def status() -> dict:
    cfg = load()
    sets = cfg.get("sets") or {}
    on = cfg.get("on") or []
    m = matcher(cfg)

    def one(n, v):
        spec = set_spec(cfg, v) or {"mode": "swap", "up": DEFAULT_UP, "words": {}}
        row = {"words": len(spec["words"]), "on": n in on, "mode": spec["mode"]}
        if spec["mode"] == "prune":
            row["up"] = spec["up"]
        return row
    return {"file": str(config_path()), "exists": config_path().exists(), "broken": bool(cfg.get("_broken")),
            "sets": {n: one(n, v) for n, v in sets.items()},
            "unknown_on": [n for n in on if n not in sets],
            "active_words": len(m.map) if m else 0,
            "unstable_stand_ins": m.chained() if m else 0,
            "ignore_case": bool(cfg.get("ignore_case", True)),
            "block_media": bool(cfg.get("block_media", True))}


def cli(argv: list[str]) -> int:
    dump = lambda v: print(json.dumps(v, ensure_ascii=False, indent=2))  # noqa: E731
    cmd, rest = (argv[0], argv[1:]) if argv else ("status", [])
    if cmd == "status":
        dump(status())
        return 0
    if cmd in ("on", "off"):
        cfg = dict(load()) if config_path().exists() else dict(TEMPLATE)
        if cfg.get("_broken"):
            print(f"broken file, fix it first: {config_path()}", file=sys.stderr)
            return 1
        sets = cfg.get("sets") or {}
        missing = [n for n in rest if n not in sets]
        if missing:
            print(f"no such set: {', '.join(missing)} (have: {', '.join(sets) or 'none'})", file=sys.stderr)
            return 2
        on = list(cfg.get("on") or [])
        if cmd == "on":
            on = list(sets) if not rest else on + [n for n in rest if n not in on]
            test = matcher({**cfg, "on": on})
            if test and test.chained():
                print(f"{test.chained()} stand-in(s) are themselves listed words: fix the file first", file=sys.stderr)
                return 1
        else:
            on = [] if not rest else [n for n in on if n not in rest]
        cfg["on"] = on
        save(cfg)
        dump(status())
        return 0
    if cmd == "mode":
        if len(rest) < 2 or rest[1] not in MODES:
            print("usage: filter mode SET swap|prune [UP]", file=sys.stderr)
            return 2
        cfg = dict(load()) if config_path().exists() else dict(TEMPLATE)
        if cfg.get("_broken"):
            print(f"broken file, fix it first: {config_path()}", file=sys.stderr)
            return 1
        sets = dict(cfg.get("sets") or {})
        name, mode = rest[0], rest[1]
        spec = set_spec(cfg, sets.get(name))
        if spec is None:
            print(f"no such set: {name} (have: {', '.join(sets) or 'none'})", file=sys.stderr)
            return 2
        up = _int(rest[2], -1) if len(rest) > 2 else spec["up"]
        if up < 0:
            print("UP is a number, 0 or more", file=sys.stderr)
            return 2
        for n, v in list(sets.items()):                # every set says its own character from now on,
            old = set_spec(cfg, v)                     # so the old global "prune_up" can go
            if old:
                sets[n] = old
        sets[name] = {"mode": mode, "up": up, "words": spec["words"]}
        cfg["sets"] = sets
        cfg.pop("prune_up", None)
        save(cfg)
        dump(status())
        return 0
    if cmd == "edit":
        if not config_path().exists():
            save(dict(TEMPLATE))
        path = str(config_path())
        if hasattr(os, "startfile"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            import shutil
            import subprocess
            opener = ["open", "-t"] if sys.platform == "darwin" else ["xdg-open"]
            if shutil.which(opener[0]):
                subprocess.Popen([*opener, path])
        print(path)
        return 0
    print(__doc__)
    return 2
