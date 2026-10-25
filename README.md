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

## Button map

Use accessible roles and names with `p.click_control("button", "Search")` or
`p.type_control("textbox", "Company", "Example", clear=True)`.
`python -m browser_atlas controls --tab example.com` lists the current controls;
`python -m browser_atlas check <site>` (or `check --all`) checks mapped pages in fresh tabs.

`p.type("#search", "한글 and Latin", clear=True)` types individual keys with variable timing,
Shift handling and Hangul composition; the CLI also supports `type <CSS> <TEXT> --tab STR`.

## MCP server

```bash
claude mcp add browser-atlas -- python -m browser_atlas mcp
```

Atlas tools (`atlas_find`, `atlas_site`, `atlas_guard`), experience tools (`memory_recall` before a task,
`memory_record` after it, `memory_rate`, `memory_stats`) and browser tools (`browser_open`, `browser_read`,
`browser_click`, `browser_type`, `browser_screenshot`, `browser_tabs`, `browser_close`, `browser_launch`).

## Safety, in code

- `blocked` sites are refused, including after a redirect.
- Pay / submit / send / delete-like controls and Enter outside search fields stop with "ask the human".
  The human allows exactly one such action from a terminal: `python -m browser_atlas allow "결제하기"`.
  No MCP tool can grant that.
- Ports in `BROWSER_ATLAS_DENY_PORTS` (profiles that belong to someone else) are refused.
- Page text comes back labelled `untrusted_page_content`.
- Do not attach the server to an unattended agent that is supposed to have no way to act on the web.

See [docs/SCHEMA.md](docs/SCHEMA.md#trust-model-shared-text-must-not-become-instructions) for why shared
map text cannot steer an agent, and [docs/LESSONS.md](docs/LESSONS.md) for field notes from real sites.

## Experience memory: a loop that runs even when the agent forgets it

The MCP server records every browser call itself. One tab is one episode; when the tab is closed,
sits idle for 15 minutes, or the server exits (or is killed and restarted), the episode is judged
from signals and stored: captcha or rate limit means `blocked`, never reading content means `fail`,
ending on an error means `partial`, otherwise `success`, with quality lowered by errors and wasted steps.

`browser_open(url, goal=...)` returns `past_routes` for that site and goal: ranked by similarity x
Bayesian route quality x recency, spread across sites, plus one rarely tried route to explore.
Failed routes fade, confirmed ones rise. `memory_rate` lets a human or a second source re-grade.

Button-map repairs are learned too: a mapped control that is missing and then succeeds under another
name becomes a rename proposal; a working control that is not mapped becomes a new-control proposal.
`python -m browser_atlas proposals` lists them; `proposals apply N` writes one into your local map.

Only search-field input is kept as keywords. Everything stays on your machine;
`python -m browser_atlas memory shareable` prints the only fields that could ever be shared.

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
