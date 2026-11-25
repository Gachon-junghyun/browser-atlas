"""Sensitive-word filter for this server's output: tool results never carry the words you listed.

Switchable word sets live outside the repo, in ~/.browser_atlas/filters.json (or $BROWSER_ATLAS_HOME):

    {"on": ["filter1"],
     "sets": {"filter1": {"Acme Corp": "Company A", "Jane Doe": "Person B"},
              "filter2": {"...": "..."}},
     "ignore_case": true,
     "block_media": true}

Each set maps a word to its stand-in; only sets named in "on" apply. The file is re-read when it
changes, so switching needs no restart. Edit it by hand (`edit` opens it): never type the words into
a chat, that is the leak this exists to stop.

While a set is on, every tool result is masked in `mcp_result()` (the server's last step), and
screenshots are withheld when "block_media" is true (the words on an image cannot be masked).
This covers what this server returns, in any client. What the client reads by itself (its own file
or shell tools, the user's prompt) is outside this server and not filtered here.

CLI. Its output can reach a model, so it never prints the words themselves:
  python -m browser_atlas filter status
  python -m browser_atlas filter on [SET ...]       # no names: every set
  python -m browser_atlas filter off [SET ...]      # no names: all off
  python -m browser_atlas filter edit               # open the word file in an editor (creates it)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

TEMPLATE = {"on": [], "sets": {"filter1": {"example word": "stand-in"}},
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
    key = (st.st_mtime_ns, st.st_size)
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


def matcher(cfg: dict | None = None) -> Matcher | None:
    """The active filter, or None when nothing is on."""
    cfg = cfg or load()
    cached = cfg is _cache["cfg"]
    if cached and _cache["m"] is not None:
        return _cache["m"] or None
    sets = cfg.get("sets") or {}
    mapping: dict = {}
    for n in cfg.get("on") or []:
        if isinstance(sets.get(n), dict):
            mapping.update(sets[n])
    m = Matcher(mapping, bool(cfg.get("ignore_case", True))) if mapping else None
    if cached:
        _cache["m"] = m or False
    return m


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
    return {"file": str(config_path()), "exists": config_path().exists(), "broken": bool(cfg.get("_broken")),
            "sets": {n: {"words": len(v) if isinstance(v, dict) else 0, "on": n in on} for n, v in sets.items()},
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
