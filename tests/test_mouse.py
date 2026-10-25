import math
import random
import unittest

from browser_atlas import _atlas, _mouse

A_TRUE, B_TRUE = 150.0, 120.0


def fake_recording(n_clicks=60, seed=1, poll=2.0):
    """A synthetic 'human': Fitts-timed, bowed, min-jerk moves, a hover, then a click."""
    rng = random.Random(seed)
    t, x, y = 0.0, 500.0, 400.0
    samples = [[t, x, y, 0]]
    for _ in range(n_clicks):
        t += rng.uniform(300, 900)                    # rest
        tx, ty = rng.uniform(50, 1800), rng.uniform(50, 1000)
        D = math.dist((x, y), (tx, ty))
        T = A_TRUE + B_TRUE * _mouse.fitts_bits(D)
        u, v = _mouse._synthetic(D, rng)
        pts = _mouse.denormalize(u, v, (x, y), (tx, ty))
        grid = [k / (len(u) - 1) for k in range(len(u))]
        steps = int(T / poll)
        last = (round(x), round(y))
        for k in range(1, steps + 1):
            tau = k / steps
            px = _mouse._interp(grid, [p[0] for p in pts], tau)
            py = _mouse._interp(grid, [p[1] for p in pts], tau)
            cur = (round(px), round(py))
            if cur != last:
                samples.append([t + tau * T, cur[0], cur[1], 0])
                last = cur
        t += T
        x, y = float(last[0]), float(last[1])
        t += rng.uniform(80, 250)                     # dwell
        samples.append([t, x, y, 1])
        t += rng.uniform(60, 120)                     # hold
        samples.append([t, x, y, 0])
    return {"hz": 1000 / poll, "samples": samples}


class MouseTest(unittest.TestCase):
    def test_segments_find_every_click(self):
        rec = fake_recording(40)
        segs = _mouse.segments(rec["samples"])
        self.assertGreaterEqual(len(segs), 36)       # a few very short moves are dropped
        self.assertTrue(all(60 <= s["hold"] <= 120 for s in segs if s["hold"]))

    def test_fit_recovers_fitts_law(self):
        m = _mouse.fit([fake_recording(120, seed=3)])
        self.assertTrue(m.learned)
        self.assertAlmostEqual(m.b, B_TRUE, delta=25)
        # "arrived" = within 3 px of the click, so the slow tail is cut: ~0.9 of the true time
        for D in (80, 300, 900):
            ratio = m.duration(D) / (A_TRUE + B_TRUE * _mouse.fitts_bits(D))
            self.assertTrue(0.7 <= ratio <= 1.0, (D, ratio))

    def test_normalize_roundtrip(self):
        pts = [(0, 10, 10), (50, 60, 30), (100, 200, 90)]
        n = _mouse.normalize(pts, n=3)
        back = _mouse.denormalize(n["u"], n["v"], (10, 10), (200, 90))
        self.assertAlmostEqual(back[0][0], 10, places=2)
        self.assertAlmostEqual(back[-1][1], 90, places=2)

    def test_plan_ends_on_target_and_time_increases(self):
        for model in (_mouse.Model(), _mouse.fit([fake_recording(60)])):
            rng = random.Random(7)
            path = model.plan((10, 10), (640, 420), width=40, rng=rng)
            self.assertEqual(path[-1][1:], (640.0, 420.0))
            ts = [p[0] for p in path]
            self.assertEqual(ts, sorted(ts))
            self.assertGreater(ts[-1], 100)

    def test_plans_differ_between_runs(self):
        m = _mouse.fit([fake_recording(60)])
        a = m.plan((0, 0), (500, 300), rng=random.Random(1))
        b = m.plan((0, 0), (500, 300), rng=random.Random(2))
        self.assertNotEqual(a[len(a) // 2], b[len(b) // 2])


class GuardTest(unittest.TestCase):
    def test_blocked_host_and_subdomain(self):
        with self.assertRaises(_atlas.Blocked):
            _atlas.guard("https://claude.ai/new")
        with self.assertRaises(_atlas.Blocked):
            _atlas.guard("https://www.tradingview.com/chart/")

    def test_unknown_host_is_allowed(self):
        _atlas.guard("https://example.com/")
        _atlas.guard("file:///C:/x.html")


class FakeCDP:
    target = {"id": "fake"}

    def __init__(self, url):
        self.url, self.calls = url, []

    def eval(self, js):
        return self.url

    def call(self, method, params=None, timeout=30):
        self.calls.append(method)
        return {}


class PointerGuardTest(unittest.TestCase):
    def test_pointer_refuses_blocked_site_before_any_event(self):
        import os, tempfile
        os.environ["BROWSER_ATLAS_HOME"] = tempfile.mkdtemp()
        from browser_atlas._pointer import Pointer
        c = FakeCDP("https://claude.ai/new")
        with self.assertRaises(_atlas.Blocked):
            Pointer(c).click("button")
        self.assertEqual(c.calls, [])


if __name__ == "__main__":
    unittest.main()
