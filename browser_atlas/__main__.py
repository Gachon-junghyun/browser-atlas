"""CLI.

  python -m browser_atlas launch [--port N]
  python -m browser_atlas tabs   [--port N]
  python -m browser_atlas guard  <URL>                  # is this URL allowed?
  python -m browser_atlas click  <CSS> --tab STR [--port N] [--seed N]
  python -m browser_atlas hover  <CSS> --tab STR [--port N]
  python -m browser_atlas type <CSS> <TEXT> --tab STR [--port N] [--clear]
  python -m browser_atlas controls --tab STR [--port N]
  python -m browser_atlas check <site|--all> [--port N]
  python -m browser_atlas allow "<control name>"        # human only: one irreversible action, 10 min
  python -m browser_atlas set-cred <name>               # human only: store a credential (stdin / hidden prompt)
  python -m browser_atlas creds | login-sites           # names and recipe readiness, never values
  python -m browser_atlas login <site> [--port N]       # check the session, log in if a recipe allows
  python -m browser_atlas login-check <site> [--port N]
  python -m browser_atlas learned                       # map fixes learned from use (applied locally)
  python -m browser_atlas share [--yes] | share sync    # preview / upload to the shared DB, or pull from it
  python -m browser_atlas mcp                           # MCP server on stdio
  python -m browser_atlas memory stats|recall <GOAL>|shareable

  python -m browser_atlas mouse record [--seconds 120]  # use your mouse normally; clicks are the labels
  python -m browser_atlas mouse fit                     # learn from every recording
  python -m browser_atlas mouse stats                   # what your style looks like in numbers
  python -m browser_atlas mouse plot [--out FILE.svg]   # recorded (grey) vs generated (blue)
"""
from __future__ import annotations

import json
import math
import random
import sys

from . import _atlas, _ax, _cdp, _check, _mouse
from ._pointer import Pointer


def _opt(argv: list[str], name: str, default=None):
    if name in argv:
        i = argv.index(name)
        val = argv[i + 1]
        del argv[i:i + 2]
        return val
    return default


def _dump(v) -> None:
    print(json.dumps(v, ensure_ascii=False, indent=2))


def plot_svg(model: _mouse.Model, n: int = 12, per: int = 3, seed: int = 0) -> str:
    """Grid of panels: one recorded movement in grey, `per` generated ones for the same endpoints."""
    rng = random.Random(seed)
    paths = rng.sample(model.paths, min(n, len(model.paths))) if model.paths else []
    if not paths:   # nothing recorded: show synthetic movements at a few distances
        paths = [{"D": d, "theta": rng.uniform(-math.pi, math.pi), "u": None, "v": None}
                 for d in (60, 120, 240, 400, 600, 900)]
    cols, size, pad = 4, 260, 16
    rows = math.ceil(len(paths) / cols)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{cols * size}" height="{rows * size}" '
           f'style="background:#fff;font:11px sans-serif">']
    for k, p in enumerate(paths):
        ox, oy = (k % cols) * size, (k // cols) * size
        start = (0.0, 0.0)
        end = (p["D"] * math.cos(p["theta"]), p["D"] * math.sin(p["theta"]))
        lines = []
        if p["u"]:
            lines.append(("#999", _mouse.denormalize(p["u"], p["v"], start, end)))
        for _ in range(per):
            lines.append(("#1f6feb", [(x, y) for _, x, y in model.plan(start, end, rng=rng)]))
        xs = [x for _, pts in lines for x, _ in pts]
        ys = [y for _, pts in lines for _, y in pts]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        sc = (size - 2 * pad) / span
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        out.append(f'<rect x="{ox}" y="{oy}" width="{size}" height="{size}" fill="none" stroke="#eee"/>')
        for color, pts in lines:
            d = " ".join(f"{ox + size / 2 + (x - cx) * sc:.1f},{oy + size / 2 + (y - cy) * sc:.1f}" for x, y in pts)
            out.append(f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="1.4" opacity="0.85"/>')
        out.append(f'<text x="{ox + 6}" y="{oy + 14}" fill="#555">{p["D"]:.0f}px</text>')
    out.append("</svg>")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    argv = list(argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    port = int(_opt(argv, "--port", _cdp.DEFAULT_PORT))
    tab = _opt(argv, "--tab")
    cmd, rest = argv[0], argv[1:]

    if cmd == "allow":
        # human-only: permit one irreversible action on a control with this name, for 10 minutes
        if not rest:
            print('usage: allow "<control name>" | allow Enter')
            return 2
        from . import _safety
        item = _safety.allow(" ".join(rest))
        print(f"allowed once: {item['name']!r} (expires in {_safety.TOKEN_TTL // 60} min)")
        return 0
    if cmd == "set-cred":
        # human-only: the value comes from stdin or a hidden prompt, never from argv
        if not rest:
            print("usage: set-cred <name>   (value from stdin or a hidden prompt)")
            return 2
        import getpass
        from . import _login
        value = sys.stdin.read().strip() if not sys.stdin.isatty() else getpass.getpass(f"{rest[0]}: ")
        print(f"saved: {_login.set_credential(rest[0], value)}")
        return 0
    if cmd == "learned":
        # map fixes this machine learned from use; already applied locally
        from . import _episodes
        for i, p in enumerate(_episodes.proposals()):
            what = f"{p['from']!r} -> {p['to']!r}" if p["kind"] == "rename" else repr(p["name"])
            shared = "shared" if p.get("shared") else "local"
            print(f"[{i}] {p['kind']:6s} {p['site']:18s} {p['role']:9s} {what}  (seen {p['seen']}x, {p['last']}, {shared})")
        return 0
    if cmd == "share":
        # what would go to the shared database (--yes uploads it); pull with: share sync
        from . import _shared
        if rest and rest[0] == "sync":
            _dump(_shared.sync())
        else:
            _dump(_shared.upload() if "--yes" in rest else _shared.preview())
        return 0
    if cmd in ("creds", "login-sites"):
        from . import _login
        _dump(_login.list_credentials() if cmd == "creds" else _login.summary())
        return 0
    if cmd in ("login", "login-check"):
        if not rest:
            print("usage: login|login-check <site> [--port N]")
            return 2
        from . import _login
        _cdp.launch(port)
        tid = _cdp.new_tab("about:blank", port)["id"]
        with _cdp.CDP(port, tid) as c:
            if cmd == "login-check":
                ok = _login.is_logged_in(c, _login.recipe(rest[0]))
                print(f"{rest[0]}: {'logged in' if ok else 'not logged in'}")
                res = {"ok": ok}
            else:
                res = _login.ensure_login(c, rest[0])
                _dump(res)
        return 0 if res["ok"] else 1
    if cmd == "mcp":
        from . import mcp_server
        sys.stdin.reconfigure(encoding="utf-8")
        mcp_server.serve()
        return 0
    if cmd == "memory":
        from . import _memory
        sub = rest[0] if rest else "stats"
        if sub == "stats":
            _dump(_memory.stats())
        elif sub == "recall" and len(rest) > 1:
            _dump(_memory.recall(" ".join(rest[1:])))
        elif sub == "shareable":
            _dump(_memory.shareable())       # exactly what would leave this machine; nothing is sent
        else:
            print("usage: memory stats | recall <GOAL> | shareable")
            return 2
        return 0
    if cmd == "launch":
        _dump(_cdp.launch(port))
        return 0
    if cmd == "tabs":
        _dump([{"id": t["id"], "title": t.get("title", "")[:60], "url": t.get("url", "")[:100]}
               for t in _cdp.tabs(port)])
        return 0
    if cmd == "guard":
        try:
            _atlas.guard(rest[0])
            hit = _atlas.site_for(rest[0])
            print(f"allowed ({hit[0]}: {hit[1].get('access')})" if hit else "allowed (not in the atlas)")
            return 0
        except _atlas.Blocked as e:
            print(f"BLOCKED: {e}")
            return 1
    if cmd == "check":
        if len(rest) != 1:
            print("usage: check <site|--all> [--port N]")
            return 2
        sites = _atlas.load()
        keys = list(sites) if rest[0] == "--all" else rest
        reports = [_check.check(key, port, sites=sites) for key in keys]
        _dump(reports if rest[0] == "--all" else reports[0])
        return int(any(not page["ok"] for report in reports for page in report["pages"].values()))
    if cmd == "controls":
        if not tab:
            print("usage: controls --tab STR [--port N]")
            return 2
        with _cdp.CDP(port, tab) as c:
            for node in _ax.snapshot(c):
                print(f"{node['role']}\t{node['name']}")
        return 0
    if cmd in ("click", "hover", "type"):
        clear = "--clear" in rest
        if clear:
            rest.remove("--clear")
        if cmd == "type" and len(rest) != 2:
            print("usage: type <CSS> <TEXT> --tab STR [--port N] [--clear]")
            return 2
        if not rest or not tab:
            print("usage: click|hover <CSS> --tab STR   (--tab is required: never act on the first tab)")
            return 2
        seed = _opt(rest, "--seed")
        with _cdp.CDP(port, tab) as c:
            p = Pointer(c, seed=int(seed) if seed else None)
            if cmd == "type":
                _dump(p.type(rest[0], rest[1], clear=clear))
            else:
                _dump(p.click(rest[0]) if cmd == "click" else p.move(rest[0]))
        return 0
    if cmd == "mouse":
        sub, more = (rest[0], rest[1:]) if rest else ("stats", [])
        if sub == "record":
            secs = float(_opt(more, "--seconds", 120))
            print(f"recording {secs:.0f}s - use the mouse as usual, click real things. Ctrl+C stops early.")
            path = _mouse.record(secs)
            segs = _mouse.segments(_mouse.load_recordings([path])[0]["samples"])
            print(f"saved {path}  ({len(segs)} usable movements)")
            return 0
        if sub == "fit":
            m = _mouse.fit()
            print(f"saved {m.save()}")
            _dump(_mouse.summary(m))
            return 0
        if sub == "stats":
            _dump(_mouse.summary(_mouse.Model.load()))
            return 0
        if sub == "plot":
            out = _opt(more, "--out", str(_mouse.data_dir() / "plot.svg"))
            with open(out, "w", encoding="utf-8") as f:
                f.write(plot_svg(_mouse.Model.load()))
            print(out)
            return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


def _entry() -> None:
    sys.exit(main(sys.argv[1:]))
