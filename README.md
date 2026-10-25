# claude-browser-atlas

A site atlas and a learned pointer for browser agents.

Browser drivers tell an agent *how* to click. They do not tell it *where things are*,
*which sites it must not automate*, or *how its owner actually moves a mouse*.
This repository is about those three.

> Status: early. The pointer and the mouse-style learner work and are tested; the atlas is
> a seed (blocked sites only) and grows next.

## What is here

| Part | What it does |
|---|---|
| `browser_atlas/_atlas.py` + `atlas/sites.json` | One JSON entry per site: access level (`open` / `session` / `api` / `blocked`), entry URLs, and *why* a site is off limits. Every pointer action checks the current URL first and refuses on a `blocked` host. |
| `browser_atlas/_mouse.py` | Records your own mouse, cuts it into aimed movements, fits Fitts' law to your timing and keeps your paths as a style library. Plans new movements in that style. |
| `browser_atlas/_pointer.py` | Clicks a random point *inside* the target (only points where `elementFromPoint` really hits it), moving there from the last pointer position along a planned path. |
| `browser_atlas/_cdp.py` | A dependency-free Chrome DevTools Protocol client that attaches to a Chrome you already run. |

Standard library only. Python 3.10+.

## Quick start

```bash
# 1. Chrome with a debug port and its own profile (log in by hand once in that window)
python -m browser_atlas launch --port 9222

# 2. Teach it your mouse: use the computer normally for a few minutes and click real things
python -m browser_atlas mouse record --seconds 300
python -m browser_atlas mouse fit
python -m browser_atlas mouse stats
python -m browser_atlas mouse plot          # grey = your movement, blue = generated for the same endpoints

# 3. Click
python -m browser_atlas tabs
python -m browser_atlas click "button[type=submit]" --tab example.com
```

```python
from browser_atlas import CDP, Pointer

with CDP(9222, match="example.com") as c:
    p = Pointer(c)                  # uses your fitted style if present, else a minimum-jerk curve
    print(p.click("#search-button"))
```

## How the mouse learner works

1. **Record**: poll the cursor and the left button (Windows, via `ctypes`). Keys are never recorded.
2. **Segment**: one movement runs from the last rest (120 ms without motion) to the moment the
   cursor reaches within 3 px of a click. Drags and wandering paths are dropped.
   Hover time before the click and button-hold time are kept separately.
3. **Normalize**: rotate and scale each movement onto (0,0) → (1,0) and resample it on a
   normalized time axis, so the shape *and* the speed profile are kept.
4. **Fit**: least squares on `T = a + b·log2(1 + D/W)` gives your speed; the normalized paths become the library.
5. **Plan**: for a new start and end, blend two of your movements recorded at a similar distance
   and direction, stretch the blend onto the new segment, and give it a Fitts-predicted duration.

Chrome accepts about one `mouseMoved` per frame, so the pointer sends "where the hand is now"
on the planned timeline rather than every planned point; the total duration stays as planned.

Your recordings are a behavioural fingerprint. They stay in `~/.browser_atlas/mouse/`
(`BROWSER_ATLAS_HOME` to move it) and are never part of this repository. Do not commit or share them.

## What this is not for

The pointer is for working on sites you are allowed to automate, with accounts you own.
It does not solve captchas and does not try to. **A captcha or rate limit means stop for that
source for the day**, and a site whose terms forbid automation goes into the atlas as `blocked`,
where the pointer will not act at all.

## Tests

```bash
python -m unittest discover tests
```

## License

MIT
