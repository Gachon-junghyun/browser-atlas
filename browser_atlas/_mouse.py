"""Learn how *you* move a mouse, then draw new movements in that style.

Pipeline:  record  ->  segments  ->  normalize  ->  fit  ->  plan

- **record**: poll the OS cursor (Windows, ctypes, no dependencies) and keep every change
  of position or left-button state, with a millisecond clock.
- **segments**: one aimed movement = from the last rest before a click to the click.
- **normalize**: rotate/scale each movement so it runs (0,0) -> (1,0), resample on a
  normalized time axis. Shape *and* velocity profile survive; distance and direction do not.
- **fit**: duration follows Fitts' law, T = a + b * log2(1 + D / W). We regress a, b on your
  own movements and keep the normalized paths as a library, plus your hover-before-click
  ("dwell") and button-hold times.
- **plan**: for a new (start, end), take two library paths recorded at a similar distance
  and direction, blend them, and stretch the blend onto the new segment with a
  Fitts-predicted duration. With no recordings it falls back to a minimum-jerk curve.

Why a library and not a neural net: a few hundred movements is a small dataset, and blending
real neighbours reproduces your curvature, overshoot and corrections without a training step.
The interface (`Model.plan`) does not care which is behind it.

Privacy: recordings are a behavioural fingerprint. They live in `~/.browser_atlas/mouse/`
(override with BROWSER_ATLAS_HOME), never in the repository. Only cursor position and the
left button are recorded, never keys.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

N_GRID = 48          # samples per normalized path
W_REF = 32.0         # assumed target width (CSS px) of recorded clicks, for Fitts' law
DEFAULT_A, DEFAULT_B = 130.0, 110.0   # ms, ms/bit: typical desktop mouse values


def home() -> Path:
    return Path(os.environ.get("BROWSER_ATLAS_HOME", Path.home() / ".browser_atlas"))


def data_dir() -> Path:
    d = home() / "mouse"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── record ───────────────────────────────────────────────────────────
def record(seconds: float = 120, hz: int = 500, out: Path | None = None, quiet: bool = False) -> Path:
    """Record cursor + left button for `seconds` (Ctrl+C stops early and still saves).

    Coordinates are stored in logical pixels (physical / DPI scale), which equal CSS pixels
    in a browser at 100% zoom.
    """
    if sys.platform != "win32":
        raise RuntimeError("the recorder is Windows-only for now (ctypes GetCursorPos)")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        scale = user32.GetDpiForSystem() / 96.0
    except Exception:
        scale = 1.0
    pt = wintypes.POINT()
    samples: list[list[float]] = []
    last = None
    t0 = time.perf_counter()
    period = 1.0 / hz
    clicks = 0
    next_note = 10.0
    try:
        while True:
            now = time.perf_counter() - t0
            if now >= seconds:
                break
            user32.GetCursorPos(ctypes.byref(pt))
            down = 1 if user32.GetAsyncKeyState(0x01) & 0x8000 else 0
            cur = (pt.x, pt.y, down)
            if cur != last:
                if last is not None and down and not last[2]:
                    clicks += 1
                samples.append([round(now * 1000, 1), round(pt.x / scale, 2), round(pt.y / scale, 2), down])
                last = cur
            if not quiet and now >= next_note:
                print(f"  {now:5.0f}s  clicks {clicks}", flush=True)
                next_note += 10.0
            time.sleep(period)
    except KeyboardInterrupt:
        pass
    out = Path(out) if out else data_dir() / f"rec_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"version": 1, "hz": hz, "scale": scale,
                               "screen": [user32.GetSystemMetrics(0) / scale, user32.GetSystemMetrics(1) / scale],
                               "samples": samples}), encoding="utf-8")
    return out


def load_recordings(paths: list[Path] | None = None) -> list[dict]:
    paths = paths or sorted(data_dir().glob("rec_*.json"))
    return [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]


# ── segments ─────────────────────────────────────────────────────────
def segments(samples: list, rest_ms: float = 120, max_ms: float = 2500, min_dist: float = 12,
             poll_ms: float = 2.0) -> list[dict]:
    """Cut a recording into aimed movements, one per left click.

    A movement starts after the cursor rested `rest_ms` (no samples = no motion) and ends at
    the last motion sample before the press. Drags and wandering paths are dropped.
    Each result: {"pts": [(t, x, y), ...], "dwell": ms, "hold": ms | None}.
    """
    out = []
    n = len(samples)
    for p in range(1, n):
        if not (samples[p][3] == 1 and samples[p - 1][3] == 0):
            continue
        # arrival = first moment the cursor got within 3 px of the click and stayed there,
        # so hand tremor while hovering is dwell, not a new tiny movement
        press_xy = samples[p][1:3]
        arrive = p
        while arrive > 0 and samples[arrive - 1][3] == 0 and \
                math.dist(samples[arrive - 1][1:3], press_xy) <= 3:
            arrive -= 1
        dwell = samples[p][0] - samples[arrive][0]
        i = arrive
        ok = True
        while i > 0:
            if samples[i][0] - samples[i - 1][0] >= rest_ms or samples[i - 1][3] == 1:
                break
            if samples[arrive][0] - samples[i - 1][0] > max_ms:
                ok = False
                break
            i -= 1
        if not ok or i == 0 or i >= arrive:
            continue
        s0 = samples[i - 1]                       # the rest position the movement left from
        pts = [(samples[i][0] - poll_ms, s0[1], s0[2])]
        pts += [(s[0], s[1], s[2]) for s in samples[i:arrive + 1]]
        D = math.dist(pts[0][1:], pts[-1][1:])
        T = pts[-1][0] - pts[0][0]
        if D < min_dist or not 50 <= T <= max_ms:
            continue
        length = sum(math.dist(a[1:], b[1:]) for a, b in zip(pts, pts[1:]))
        if length > 3 * D:
            continue
        hold = None
        for q in range(p + 1, n):
            if samples[q][3] == 0:
                moved = math.dist(samples[q][1:3], samples[p][1:3])
                if moved > 3:                     # a drag, not a click
                    ok = False
                hold = samples[q][0] - samples[p][0]
                break
        if not ok:
            continue
        out.append({"pts": pts, "dwell": dwell, "hold": hold if hold is not None and hold <= 400 else None})
    return out


# ── normalize ────────────────────────────────────────────────────────
def _interp(xs: list[float], ys: list[float], x: float) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    span = xs[hi] - xs[lo]
    f = (x - xs[lo]) / span if span else 0.0
    return ys[lo] + f * (ys[hi] - ys[lo])


def normalize(pts: list[tuple[float, float, float]], n: int = N_GRID) -> dict:
    """Map a movement onto (0,0)->(1,0) on a normalized time axis."""
    t0, x0, y0 = pts[0]
    t1, x1, y1 = pts[-1]
    D = math.hypot(x1 - x0, y1 - y0)
    th = math.atan2(y1 - y0, x1 - x0)
    c, s = math.cos(-th), math.sin(-th)
    T = t1 - t0
    taus = [(t - t0) / T for t, _, _ in pts]
    us = [((x - x0) * c - (y - y0) * s) / D for _, x, y in pts]
    vs = [((x - x0) * s + (y - y0) * c) / D for _, x, y in pts]
    grid = [k / (n - 1) for k in range(n)]
    return {"D": round(D, 2), "theta": round(th, 4), "T": round(T, 1),
            "u": [round(_interp(taus, us, g), 4) for g in grid],
            "v": [round(_interp(taus, vs, g), 4) for g in grid]}


def denormalize(u: list[float], v: list[float], start, end) -> list[tuple[float, float]]:
    (x0, y0), (x1, y1) = start, end
    D = math.hypot(x1 - x0, y1 - y0)
    th = math.atan2(y1 - y0, x1 - x0)
    c, s = math.cos(th), math.sin(th)
    return [(x0 + D * (a * c - b * s), y0 + D * (a * s + b * c)) for a, b in zip(u, v)]


def fitts_bits(D: float, W: float = W_REF) -> float:
    return math.log2(1 + D / max(W, 1.0))


# ── model ────────────────────────────────────────────────────────────
@dataclass
class Model:
    a: float = DEFAULT_A
    b: float = DEFAULT_B
    sd: float = 0.12                       # relative spread of durations around the fit
    paths: list = field(default_factory=list)
    dwell: list = field(default_factory=list)
    hold: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def learned(self) -> bool:
        return len(self.paths) >= 5

    def save(self, path: Path | None = None) -> Path:
        path = Path(path) if path else data_dir() / "model.json"
        path.write_text(json.dumps(asdict(self)), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> "Model":
        path = Path(path) if path else data_dir() / "model.json"
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def duration(self, D: float, W: float = W_REF) -> float:
        return max(60.0, self.a + self.b * fitts_bits(D, W))

    def sample_dwell(self, rng: random.Random) -> float:
        return rng.choice(self.dwell) if self.dwell else rng.uniform(60, 220)

    def sample_hold(self, rng: random.Random) -> float:
        return rng.choice(self.hold) if self.hold else rng.uniform(55, 130)

    def plan(self, start, end, width: float = W_REF, hz: int = 125,
             rng: random.Random | None = None) -> list[tuple[float, float, float]]:
        """Timed points [(t_ms, x, y), ...] from start to end; the last point is exactly `end`."""
        rng = rng or random.Random()
        D = math.dist(start, end)
        if D < 2:
            return [(0.0, float(end[0]), float(end[1]))]
        if self.learned:
            u, v, T = self._blend(start, end, width, rng)
        else:
            u, v = _synthetic(D, rng)
            T = self.duration(D, width) * math.exp(rng.gauss(0, self.sd))
        xy = denormalize(u, v, start, end)
        grid = [k / (len(u) - 1) for k in range(len(u))]
        xs, ys = [p[0] for p in xy], [p[1] for p in xy]
        steps = max(2, math.ceil(T / 1000 * hz))
        jit = min(0.6, 0.002 * D)
        out = []
        for k in range(steps + 1):
            tau = k / steps
            x, y = _interp(grid, xs, tau), _interp(grid, ys, tau)
            if 0 < k < steps:
                x, y = x + rng.gauss(0, jit), y + rng.gauss(0, jit)
            out.append((round(tau * T, 1), round(x, 2), round(y, 2)))
        out[-1] = (out[-1][0], float(end[0]), float(end[1]))
        return out

    def _blend(self, start, end, width, rng):
        D = math.dist(start, end)
        th = math.atan2(end[1] - start[1], end[0] - start[0])
        lnD = math.log(D)

        def gap(p):
            d_ang = abs((p["theta"] - th + math.pi) % (2 * math.pi) - math.pi)
            return abs(math.log(p["D"]) - lnD) + 0.5 * d_ang / math.pi

        near = sorted(self.paths, key=gap)[:8]
        p, q = (rng.sample(near, 2) if len(near) >= 2 else (near[0], near[0]))
        w = rng.uniform(0.25, 0.75)
        u = [w * a + (1 - w) * b for a, b in zip(p["u"], q["u"])]
        v = [w * a + (1 - w) * b for a, b in zip(p["v"], q["v"])]
        target = self.duration(D, width)
        # each neighbour's own speed, rescaled to the new distance by the fitted law
        tp = p["T"] * target / self.duration(p["D"])
        tq = q["T"] * target / self.duration(q["D"])
        T = max(60.0, (w * tp + (1 - w) * tq) * math.exp(rng.gauss(0, 0.05)))
        return u, v, T


def _synthetic(D: float, rng: random.Random, n: int = N_GRID):
    """Minimum-jerk speed profile on a gently bowed path, with an occasional overshoot."""
    bow = rng.gauss(0, 0.08)
    over = rng.uniform(0.01, 0.04) if D > 200 and rng.random() < 0.3 else 0.0
    u, v = [], []
    for k in range(n):
        tau = k / (n - 1)
        tw = tau ** 0.85                       # faster acceleration, longer deceleration
        s = 10 * tw ** 3 - 15 * tw ** 4 + 6 * tw ** 5
        back = max(0.0, min(1.0, (tau - 0.8) / 0.2))
        back = back * back * (3 - 2 * back)
        u.append(s * (1 + over) - over * back)
        v.append(4 * bow * s * (1 - s))
    return u, v


def fit(recordings: list[dict] | None = None, n: int = N_GRID) -> Model:
    """Fit a Model to recordings (default: everything in the data dir)."""
    recs = recordings if recordings is not None else load_recordings()
    segs = [s for r in recs for s in segments(r["samples"], poll_ms=1000.0 / r.get("hz", 500))]
    m = Model()
    m.paths = [normalize(s["pts"], n) for s in segs]
    m.dwell = [round(s["dwell"], 1) for s in segs if s["dwell"] <= 1500]
    m.hold = [round(s["hold"], 1) for s in segs if s["hold"] is not None]
    if len(m.paths) >= 5:
        xs = [fitts_bits(p["D"]) for p in m.paths]
        ts = [p["T"] for p in m.paths]
        mx, mt = sum(xs) / len(xs), sum(ts) / len(ts)
        sxx = sum((x - mx) ** 2 for x in xs)
        b = sum((x - mx) * (t - mt) for x, t in zip(xs, ts)) / sxx if sxx else 0.0
        b = max(20.0, b)
        a = max(0.0, mt - b * mx)
        res = [math.log(t / max(60.0, a + b * x)) for x, t in zip(xs, ts)]
        m.a, m.b = round(a, 1), round(b, 1)
        m.sd = round(math.sqrt(sum(r * r for r in res) / len(res)), 3)
    m.meta = {"fitted": time.strftime("%Y-%m-%d %H:%M"), "recordings": len(recs),
              "segments": len(segs)}
    return m


def summary(m: Model) -> dict:
    """Numbers that describe a style: how fast, how curved, how often it overshoots."""
    if not m.paths:
        return {"learned": False, "a_ms": m.a, "b_ms_per_bit": m.b}
    curve = [max(abs(x) for x in p["v"]) for p in m.paths]
    over = [max(p["u"]) - 1 for p in m.paths]
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None  # noqa: E731
    return {"learned": m.learned, "movements": len(m.paths), "a_ms": m.a, "b_ms_per_bit": m.b,
            "duration_spread": m.sd, "median_curvature": round(med(curve), 3),
            "overshoot_rate": round(sum(o > 0.02 for o in over) / len(over), 2),
            "median_dwell_ms": med(m.dwell), "median_hold_ms": med(m.hold),
            "distance_px": [round(min(p["D"] for p in m.paths)), round(max(p["D"] for p in m.paths))]}
