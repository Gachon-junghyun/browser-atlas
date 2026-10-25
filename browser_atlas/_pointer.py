"""A pointer that moves like its owner: random point inside the target, learned path to it.

    with CDP(match="example.com") as c:
        p = Pointer(c)                 # loads ~/.browser_atlas/mouse/model.json if present
        p.click("button[type=submit]")

- **Where**: a random point inside the element (Gaussian around the centre, kept off the
  edges), and only a point where `elementFromPoint` really hits the element. The centre is
  often an icon or badge that swallows the click; testing points finds one that lands.
- **How**: from the last pointer position (remembered per tab, across processes) to that
  point along a path planned by `Model.plan`, as real `mouseMoved` events on its timeline,
  then a hover pause, press, hold, release.
- **Never** on a site the atlas marks `access: blocked`: every action checks the URL first.
"""
from __future__ import annotations

import json
import random
import time

from . import _atlas
from ._mouse import Model, _interp, home

_T = time.perf_counter

_TARGET_JS = r"""
(() => {
  const e = document.querySelector(%(css)s);
  if (!e) return {err: 'no-element'};
  e.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = e.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return {err: 'zero-size'};
  let by = null;
  for (const [fx, fy] of %(cands)s) {
    const x = r.left + r.width * fx, y = r.top + r.height * fy;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
    const t = document.elementFromPoint(x, y);
    if (t && (t === e || e.contains(t)))
      return {x, y, w: r.width, h: r.height, vw: innerWidth, vh: innerHeight};
    if (t && !by) by = (t.tagName + '.' + (typeof t.className === 'string' ? t.className : '')
                        + '#' + (t.id || '')).slice(0, 80);
  }
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return {err: 'offscreen', x: cx, y: cy};
  return {err: 'covered', by: by || '(nothing)'};
})()
"""


def _state_path():
    p = home() / "pointer_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        _state_path().write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass


class Pointer:
    def __init__(self, cdp, model: Model | None = None, seed: int | None = None, hz: int = 125):
        self.c = cdp
        self.model = model if model is not None else Model.load()
        self.rng = random.Random(seed)
        self.hz = hz
        self._tid = cdp.target.get("id", "")
        xy = _load_state().get(self._tid)
        self.pos: tuple[float, float] | None = tuple(xy) if xy else None

    # -- where --
    def _candidates(self, n: int = 16) -> list[list[float]]:
        pts = []
        for _ in range(n):
            fx = min(0.85, max(0.15, self.rng.gauss(0.5, 1 / 6)))
            fy = min(0.80, max(0.20, self.rng.gauss(0.5, 1 / 7)))
            pts.append([round(fx, 3), round(fy, 3)])
        return pts + [[0.5, 0.5]]

    def target(self, css: str) -> dict:
        info = self.c.eval(_TARGET_JS % {"css": json.dumps(css), "cands": json.dumps(self._candidates())})
        err = info.get("err")
        if err == "no-element":
            raise RuntimeError(f"no element: {css}")
        if err == "zero-size":
            raise RuntimeError(f"zero-size element: {css} (a hidden node; suspect the selector)")
        if err == "offscreen":
            raise RuntimeError(f"outside the viewport: {css} (sticky header/footer or a scroll container)")
        if err == "covered":
            raise RuntimeError(f"covered: {css} <- '{info['by']}' is on top (close the modal/overlay first)")
        return info

    # -- how --
    def _guard(self) -> None:
        _atlas.guard(self.c.eval("location.href") or "")

    def _start(self, info: dict) -> tuple[float, float]:
        if self.pos is not None:
            return self.pos
        # unknown cursor (fresh tab): enter from a random point in the viewport
        return (self.rng.uniform(0.1, 0.9) * info["vw"], self.rng.uniform(0.1, 0.9) * info["vh"])

    def _remember(self) -> None:
        st = _load_state()
        st[self._tid] = [round(self.pos[0], 2), round(self.pos[1], 2)]
        _save_state(st)

    def _move(self, start, end, width: float) -> dict:
        path = self.model.plan(start, end, width=width, hz=self.hz, rng=self.rng)
        ts = [p[0] for p in path]
        xs = [p[1] for p in path]
        ys = [p[2] for p in path]
        total = ts[-1]
        # Chrome acknowledges one mouseMoved per frame (~16 ms), so sending every planned point
        # stretches the move. Send "where the hand is now" instead: duration stays as planned,
        # and the page sees one move per frame, which is also what a real mouse looks like.
        sent = 0
        t0 = _T()
        while True:
            el = (_T() - t0) * 1000
            done = el >= total
            x, y = (xs[-1], ys[-1]) if done else (_interp(ts, xs, el), _interp(ts, ys, el))
            self.c.call("Input.dispatchMouseEvent",
                        {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0})
            sent += 1
            if done:
                break
            time.sleep(0.004)
        self.pos = (float(end[0]), float(end[1]))
        self._remember()
        return {"points": sent, "planned_ms": total, "moved_ms": round((_T() - t0) * 1000, 1)}

    def move(self, css: str) -> dict:
        """Hover the element (opens hover menus) without clicking."""
        self._guard()
        info = self.target(css)
        start = self._start(info)
        r = self._move(start, (info["x"], info["y"]), min(info["w"], info["h"]))
        return {"x": info["x"], "y": info["y"], "from": start, **r}

    def click(self, css: str, button: str = "left") -> dict:
        self._guard()
        info = self.target(css)
        start = self._start(info)
        end = (info["x"], info["y"])
        r = self._move(start, end, min(info["w"], info["h"]))
        dwell = self.model.sample_dwell(self.rng)
        hold = self.model.sample_hold(self.rng)
        time.sleep(dwell / 1000)
        mask = {"left": 1, "right": 2, "middle": 4}[button]
        self.c.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": end[0], "y": end[1],
                                                 "button": button, "buttons": mask, "clickCount": 1})
        time.sleep(hold / 1000)
        self.c.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": end[0], "y": end[1],
                                                 "button": button, "buttons": 0, "clickCount": 1})
        return {"x": round(end[0], 1), "y": round(end[1], 1), "from": [round(v, 1) for v in start],
                "size": [round(info["w"]), round(info["h"])], "dwell_ms": round(dwell),
                "hold_ms": round(hold), "style": "learned" if self.model.learned else "synthetic", **r}
