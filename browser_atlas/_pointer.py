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
- **Stops** before anything irreversible (pay, submit, send, delete...): `_safety` judges the
  live control by its own name and role, and only a human's one-time `allow` lets it through.
  Pressing Enter is allowed only in search fields, for the same reason.
"""
from __future__ import annotations

import difflib
import json
import random
import time

from . import _atlas, _ax, _safety
from ._keys import TypingModel, key_events
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
    if (t && (t === e || e.contains(t))) {
      const clean = v => (v || '').replace(/\s+/g, ' ').trim();
      const name = clean(e.getAttribute('aria-label')) || (e.labels && e.labels[0] && clean(e.labels[0].innerText))
        || clean(e.innerText).slice(0, 80) || clean(e.getAttribute('title') || e.getAttribute('alt')
        || e.getAttribute('placeholder') || (e.tagName === 'INPUT' ? e.value : ''));
      const tag = e.tagName, type = (e.getAttribute('type') || '').toLowerCase();
      const role = e.getAttribute('role') || (tag === 'A' ? 'link' : tag === 'BUTTON' ? 'button'
        : tag === 'INPUT' && ['submit', 'button', 'image', 'reset'].includes(type) ? 'button'
        : tag === 'INPUT' || tag === 'TEXTAREA' || e.isContentEditable ? 'textbox' : 'generic');
      // Enter is allowed only where it searches. Fields where Enter sends something never count.
      const hint = [e.name, e.id, e.getAttribute('placeholder'), e.getAttribute('aria-label'),
                    e.getAttribute('title'), name].join(' ');
      const sends = /mail|이메일|구독|subscribe|전화|phone|메시지|message|댓글|comment|비밀번호|password|인증|코드|code|쿠폰|coupon|카드|card/i;
      const form = e.form;
      const texts = form ? form.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button])'
                                                + ':not([type=checkbox]):not([type=radio]):not([type=image]), textarea').length : 0;
      const pw = form ? !!form.querySelector('input[type=password]') : false;
      const search = !sends.test(hint) && !['email', 'tel', 'password'].includes(type) && !pw && (
        type === 'search' || e.getAttribute('role') === 'searchbox' || !!e.closest('[role=search]')
        || /search|query|keyword|검색|찾기|종목|회사명|^q$|^q /i.test(hint)
        || /search|검색/i.test((form && form.getAttribute('action')) || '')
        || (form && texts === 1));
      return {x, y, w: r.width, h: r.height, vw: innerWidth, vh: innerHeight, name, role, search};
    }
    if (t && !by) by = (t.tagName + '.' + (typeof t.className === 'string' ? t.className : '')
                        + '#' + (t.id || '')).slice(0, 80);
  }
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) return {err: 'offscreen', x: cx, y: cy};
  return {err: 'covered', by: by || '(nothing)'};
})()
"""


def _nearest(role: str, name: str, nodes: list[dict], n: int = 6) -> list[str]:
    """Names worth trying instead: containing the wanted name first, then similar spellings;
    same role before other roles (a 'Remove' link may really be a 'Remove Stardew Valley' button)."""
    want = name.casefold().strip()
    named = [x for x in nodes if x["name"]]
    ranked: list[str] = []
    for pool in ([x for x in named if x["role"] == role], named):
        names = [x["name"] for x in pool]
        ranked += [s for s in names if want and (want in s.casefold() or s.casefold() in want)]
        ranked += difflib.get_close_matches(name, names, n=n, cutoff=0.45)
    if not ranked:                                       # nothing alike: show what this role does offer
        ranked = [x["name"] for x in named if x["role"] == role]
    seen, out = set(), []
    for s in ranked:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:n]


class NoControl(RuntimeError):
    """The page has no control with this role and name; `nearest` are names it does have."""

    def __init__(self, role: str, name: str, nearest: list[str]):
        self.role, self.name, self.nearest = role, name, nearest
        super().__init__(f"no control: role={role!r} name={name!r}; nearest: {nearest!r}")


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
        port = getattr(self.c, "port", None)
        if port is not None and not _safety.port_allowed(port):
            raise _safety.NeedsHuman(f"port {port} is in BROWSER_ATLAS_DENY_PORTS (someone else's profile)")
        self._url = self.c.eval("location.href") or ""
        _atlas.guard(self._url)

    def _mapped(self, name: str) -> dict | None:
        """The map's entry for a control with this name on this site, if any (it can only tighten)."""
        hit = _atlas.site_for(getattr(self, "_url", ""))
        if not hit:
            return None
        for page in (hit[1].get("pages") or {}).values():
            for c in page.get("controls", []):
                if " ".join(c.get("name", "").split()) == " ".join(name.split()):
                    return c
        return None

    def _control(self, role, name, exact=True, index: int | None = None) -> str:
        """Find a control by role and name. With several of the same name, `index` picks the n-th
        (0-based, page order); without it the first visible one is used."""
        self._guard()
        nodes = _ax.snapshot(self.c)
        norm = lambda s: " ".join((s or "").split())  # noqa: E731
        same = [n for n in nodes if n["role"] == role and (
            norm(n["name"]) == norm(name) if exact else norm(name).casefold() in norm(n["name"]).casefold())]
        node = (same[index] if index < len(same) else None) if index is not None else \
            _ax.find(self.c, role, name, exact=exact, nodes=nodes)
        if node is None:
            raise NoControl(role, name, _nearest(role, name, nodes))
        self.matches = len(same)
        return _ax.mark(self.c, node["backend"])

    def click_control(self, role, name, exact=True, index=None) -> dict:
        r = self.click(self._control(role, name, exact, index))
        return {**r, "same_name_controls": self.matches}

    def move_control(self, role, name, exact=True, index=None) -> dict:
        return self.move(self._control(role, name, exact, index))

    def type_control(self, role, name, text, exact=True, clear=False, model=None, index=None) -> dict:
        r = self.type(self._control(role, name, exact, index), text, clear=clear, model=model)
        return {**r, "same_name_controls": self.matches}

    def type(self, css, text, clear=False, model=None, secret=False) -> dict:
        """`secret=True` (passwords): the value never appears in the result or in an error."""
        self._guard()
        field = self.target(css)
        if ("\n" in text or "\r" in text) and not field.get("search"):
            _safety.require_enter(field.get("name") or css)
        self.click(css)
        if clear:
            ctrl = {"key": "Control", "code": "ControlLeft", "windowsVirtualKeyCode": 17}
            select = {"key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65}
            for kind, params, modifiers in (("keyDown", ctrl, 2), ("keyDown", select, 2),
                                            ("keyUp", select, 2), ("keyUp", ctrl, 0)):
                self.c.call("Input.dispatchKeyEvent", {"type": kind, **params, "modifiers": modifiers})
            for method, params in key_events("Backspace"):
                self.c.call(method, params)
        plan = (model if model is not None else TypingModel()).plan(text, self.rng)
        start = _T()
        for due, method, params in plan:
            time.sleep(max(0.0, due / 1000 - (_T() - start)))
            self.c.call(method, params)
        value = self.c.eval("(() => { const e = document.querySelector(" + json.dumps(css) +
                            "); return e ? (e.matches('input, textarea') ? e.value : "
                            "e.isContentEditable ? e.innerText : null) : null; })()")
        pressed_enter = "\n" in text or "\r" in text
        if pressed_enter and not value:                 # also when the site clears the box after searching
            value = text.rstrip("\r\n")                 # Enter submitted and the page moved on: that is success
        if not isinstance(value, str) or not value.rstrip("\r\n").endswith(text.rstrip("\r\n")):
            if secret:
                raise RuntimeError(f"input did not stick in {css} (secret field; values withheld)")
            raise RuntimeError(f"input did not stick: expected {text!r}, got {value!r}")
        return {"value": "<hidden>" if secret else value, "events": len(plan),
                "search": bool(field.get("search")) and not secret, "field": field.get("name", "")}

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
        _safety.require(info.get("name", ""), info.get("role", ""), self._mapped(info.get("name", "")))
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
