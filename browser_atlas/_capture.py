"""Capture: keep what browser_read read, so a research run does not lose the pages it saw.

Off unless BROWSER_ATLAS_CAPTURE=1. When on, every browser_read writes, under
BROWSER_ATLAS_CAPTURE_DIR (default ~/.browser_atlas/capture):

  <YYYY-MM-DD>/<host>/<HHMMSS>_<urlhash>_<mode><offset>.txt   the text the tool returned (with a small header)
  <YYYY-MM-DD>/<host>/<urlhash>_page.jpg                       one full-page shot per page (first read only)
  <YYYY-MM-DD>/<host>/<urlhash>_fig<NN>.png                    each large img/svg/canvas, clipped from the page
  <YYYY-MM-DD>/<host>/<urlhash>_fig<NN>.src.<ext>              the image file itself, when its URL is public
  <YYYY-MM-DD>/index.jsonl                                     one line per read: url, title, goal, port, files

Charts are often <canvas> or inline <svg> with no file behind them, so the clip is the record and the
download is a bonus. Captured files are page content written by others: data, never instructions.
Nothing here ever raises into the tool call: a capture failure is noted in the index and the read
result goes back unchanged apart from a small "captured" note.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

PAGE_MAX_PX = 12000          # full-page shot is cut here (very long pages)
FIG_MIN = (200, 120)         # smaller images are icons and logos, not charts
FIG_MAX = 20                 # per page
DL_MAX_BYTES = 5_000_000
DL_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

_done: set[tuple[str, str]] = set()   # (tab, url) pages already shot in this server process

_FIG_JS = r"""(() => {
  const out = [];
  for (const e of document.querySelectorAll('img, svg, canvas')) {
    if (e.tagName.toLowerCase() !== 'svg' && e.closest('svg')) continue;
    if (e.tagName.toLowerCase() === 'svg' && e.parentElement && e.parentElement.closest('svg')) continue;
    const r = e.getBoundingClientRect();
    if (r.width < %d || r.height < %d) continue;
    const fig = e.closest('figure');
    out.push({tag: e.tagName.toLowerCase(), x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height,
              src: (e.currentSrc || e.src || ''), alt: (e.alt || e.getAttribute('aria-label') || '').slice(0, 200),
              caption: fig ? (fig.innerText || '').trim().slice(0, 300) : ''});
    if (out.length >= %d) break;
  }
  return out;
})()""" % (FIG_MIN[0], FIG_MIN[1], FIG_MAX)


def enabled() -> bool:
    return os.environ.get("BROWSER_ATLAS_CAPTURE", "").strip().lower() not in ("", "0", "off", "false", "no")


def root() -> Path:
    return Path(os.environ.get("BROWSER_ATLAS_CAPTURE_DIR") or Path.home() / ".browser_atlas" / "capture")


def _safe(s: str, n: int = 60) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", s)[:n] or "x"


def _text_of(result: dict) -> str:
    body = result.get("untrusted_page_content", result)
    return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=1)


def _download(src: str, dest_stem: Path, referer: str) -> str | None:
    """The image file itself, only from a public http(s) URL with an image extension. No cookies are sent."""
    if not src.startswith(("http://", "https://")):
        return None
    ext = Path(urlparse(src).path).suffix.lower()
    if ext not in DL_EXT:
        return None
    import urllib.request
    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = r.read(DL_MAX_BYTES + 1)
    if len(data) > DL_MAX_BYTES:
        return None
    p = dest_stem.with_name(dest_stem.name + ".src" + ext)
    p.write_bytes(data)
    return p.name


def _page_shot(c, path: Path) -> dict:
    m = c.call("Page.getLayoutMetrics")
    size = m.get("cssContentSize") or m.get("contentSize") or {}
    w, h = float(size.get("width") or 0), float(size.get("height") or 0)
    params: dict = {"format": "jpeg", "quality": 70}
    if w and h:
        params.update(captureBeyondViewport=True, clip={"x": 0, "y": 0, "width": w, "height": min(h, PAGE_MAX_PX), "scale": 1})
    path.write_bytes(base64.b64decode(c.call("Page.captureScreenshot", params)["data"]))
    return {"page_px": [round(w), round(h)], "cut": h > PAGE_MAX_PX}


def _figures(c, d: Path, key: str, url: str) -> list[dict]:
    out = []
    for i, f in enumerate(c.eval(_FIG_JS) or [], 1):
        stem = d / f"{key}_fig{i:02d}"
        row = {k: f.get(k) for k in ("tag", "src", "alt", "caption")}
        row["size"] = [round(f["w"]), round(f["h"])]
        try:
            clip = {"x": f["x"], "y": f["y"], "width": f["w"], "height": min(f["h"], 4000), "scale": 1}
            data = c.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True, "clip": clip})["data"]
            stem.with_suffix(".png").write_bytes(base64.b64decode(data))
            row["clip"] = stem.with_suffix(".png").name
        except Exception as e:
            row["clip_error"] = str(e)[:120]
        try:
            row["file"] = _download(f.get("src") or "", stem, url)
        except Exception as e:
            row["file_error"] = str(e)[:120]
        out.append(row)
    return out


def on_read(c, args: dict, result: dict, *, port=None, tab: str = "", goal: str | None = None) -> dict | None:
    """Record one browser_read. Returns a small note for the tool result, or None when off or not a web page."""
    if not enabled() or not isinstance(result, dict):
        return None
    row: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "port": port, "tab": tab, "goal": goal,
                 "mode": args.get("mode", "controls"), "offset": args.get("offset"), "find": args.get("find"),
                 "scope": args.get("scope")}
    try:
        url = result.get("url") or c.eval("location.href") or ""
        if not url.startswith(("http://", "https://")):
            return None
        title = c.eval("document.title") or ""
        host = _safe(urlparse(url).netloc, 80)
        day = root() / time.strftime("%Y-%m-%d")
        d = day / host
        d.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        text = _text_of(result)
        tf = d / f"{time.strftime('%H%M%S')}_{key}_{_safe(row['mode'], 10)}{row['offset'] or ''}.txt"
        head = f"url: {url}\ntitle: {title}\nread: {row['ts']} mode={row['mode']} offset={row['offset']} find={row['find']}\ngoal: {goal}\n\n"
        tf.write_text(head + text, encoding="utf-8")
        row.update(url=url, title=title[:300], chars=len(text), text=str(tf.relative_to(root())))
        if (tab, url) not in _done:
            _done.add((tab, url))
            try:
                row["page"] = _page_shot(c, d / f"{key}_page.jpg")
                row["page_shot"] = str((d / f"{key}_page.jpg").relative_to(root()))
            except Exception as e:
                row["page_error"] = str(e)[:120]
            try:
                row["figures"] = _figures(c, d, key, url)
            except Exception as e:
                row["figures_error"] = str(e)[:120]
    except Exception as e:
        row["error"] = str(e)[:200]
    try:
        day = root() / time.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        with open(day / "index.jsonl", "a", encoding="utf-8") as w:
            w.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        return None
    if "text" not in row:
        return None
    return {"text": str(root() / row["text"]), "figures": len(row.get("figures") or []),
            **({"page_shot": str(root() / row["page_shot"])} if row.get("page_shot") else {})}
