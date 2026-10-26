"""Talk to an already-running Chrome over the DevTools protocol. Standard library only.

Why not Selenium/Playwright: attaching chromedriver costs ~1.5 s per call, a raw CDP
websocket costs ~20 ms. Agents call the browser many times in small steps, so the
attach cost dominates. This file does its own websocket framing (about 40 lines).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_PORT = 9222
_T = time.perf_counter


# ── launching (no websocket needed) ──────────────────────────────────
# Reads and waits share this same-origin traversal. Inaccessible frames are skipped.
_TEXT_JS = r"""(topText => {
  const parts = [topText];
  function visit(doc, depth) {
    if (depth >= 3) return;
    for (const frame of doc.querySelectorAll('iframe,frame')) {
      try {
        const child = frame.contentDocument;
        if (!child) continue;
        const label = frame.name || new URL(frame.src || 'about:blank', doc.baseURI).pathname;
        parts.push('--- frame: ' + label + ' ---', (child.body || {}).innerText || '');
        visit(child, depth + 1);
      } catch (_) {} // cross-origin or detached; continue with the next sibling
    }
  }
  visit(document, 0);
  return parts.join('\n');
})"""


def text_js(top: str = "(document.body||{}).innerText||''") -> str:
    """Top document first, then same-origin frame text in depth-first DOM order."""
    return _TEXT_JS + "(" + top + ")"


def status(port: int = DEFAULT_PORT, timeout: float = 2.0) -> dict:
    out = {"ok": False, "browser": "", "error": ""}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            out["browser"] = json.loads(r.read().decode("utf-8")).get("Browser", "")
        out["ok"] = True
    except (urllib.error.URLError, OSError, ValueError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _find_chrome() -> str:
    cands = [os.environ.get("CHROME_PATH", "")]
    if sys.platform == "win32":
        cands += [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    for c in cands:
        if c and Path(c).exists():
            return c
    for name in ("google-chrome", "chrome", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    raise RuntimeError("Chrome not found. Set CHROME_PATH.")


def profile_dir(port: int) -> str:
    """BROWSER_ATLAS_PROFILE, else an existing `%LOCALAPPDATA%\\ChromeDebugProfile<port>` (the layout
    other CDP tools use, so sessions you already logged into keep working), else our own folder."""
    if os.environ.get("BROWSER_ATLAS_PROFILE"):
        return os.environ["BROWSER_ATLAS_PROFILE"]
    legacy = Path(os.environ.get("LOCALAPPDATA", "")) / f"ChromeDebugProfile{port}"
    if os.environ.get("LOCALAPPDATA") and legacy.exists():
        return str(legacy)
    return str(Path.home() / ".browser_atlas" / f"chrome-profile-{port}")


def launch(port: int = DEFAULT_PORT, url: str = "about:blank", profile: str | None = None,
           wait: float = 20) -> dict:
    """Start Chrome with a debug port and its own profile. No-op if the port is already up.

    Each port gets its own profile directory, so **a port is an account boundary**:
    log in once by hand in that window and the session stays in that profile.
    Chrome 136+ refuses remote debugging on the default profile, hence the separate one.
    """
    prof = profile or profile_dir(port)
    if status(port, 1.0)["ok"]:
        return {"ok": True, "port": port, "profile": prof, "launched": False}
    args = [_find_chrome(), f"--remote-debugging-port={port}", "--remote-allow-origins=*",
            f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check",
            # When the window is fully covered, Windows Chrome marks the tab hidden and
            # React/antd portals (dropdowns, modals) never mount: clicks land, nothing happens.
            "--disable-features=CalculateNativeWinOcclusion",
            "--disable-backgrounding-occluded-windows", url]
    kw: dict = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    deadline = time.time() + wait
    while time.time() < deadline:
        if status(port, 1.0)["ok"] and tabs(port):
            return {"ok": True, "port": port, "profile": prof, "launched": True}
        time.sleep(0.3)
    return {"ok": False, "port": port, "profile": prof, "launched": True,
            "error": f"port {port} did not open in {wait}s"}


# ── targets (plain HTTP) ─────────────────────────────────────────────
def tabs(port: int = DEFAULT_PORT, timeout: float = 2.0) -> list[dict]:
    """Open page tabs. Look at these before touching anything: some may be a human's work."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    return [t for t in raw if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def new_tab(url: str = "about:blank", port: int = DEFAULT_PORT) -> dict:
    """Open a fresh tab. The only start that cannot disturb someone else's tab."""
    ep = f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}"
    try:
        with urllib.request.urlopen(urllib.request.Request(ep, method="PUT"), timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError:          # Chrome < 111 only accepts GET
        with urllib.request.urlopen(ep, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))


def close_tab(tab_id: str, port: int = DEFAULT_PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{tab_id}", timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def page_urls(c) -> dict[str, str]:
    """Page targets in this browser right now and their addresses, seen from one tab's session
    (taken before a click)."""
    return {t["targetId"]: t.get("url", "") for t in c.call("Target.getTargets", timeout=5).get("targetInfos") or []
            if t.get("type") == "page"}


def opened_by(c, known: dict[str, str], wait: float = 0.0, load: float = 3.0, since: int | None = None) -> dict | None:
    """A new page c's tab opened (CDP openerId) that was not in `known`, watched for `wait` seconds.
    The opener id, not a tab-list diff: a tab someone else opens meanwhile is not this click's.
    With `since`, only after this tab sent a Page.windowOpen past c.window_opens[since]: an event of
    this click, not a page that merely changed. A named window the site reuses sends no such event
    (checked in Chrome), so it is not reported as opened.
    A window.open() tab starts at about:blank, so once asked for it gets `load` seconds to show its address."""
    me = c.target.get("id")
    until, heard, hit = _T() + wait, False, None
    while True:
        infos = c.call("Target.getTargets", timeout=5).get("targetInfos") or []
        asked = since is None or len(c.window_opens) > since
        mine = [t for t in infos if t.get("type") == "page" and t.get("openerId") == me
                and t.get("targetId") not in known] if asked else []
        if mine:
            hit = mine[-1]
            if hit.get("url") not in ("", "about:blank"):
                return hit
        if (mine or (asked and since is not None)) and not heard:
            heard, until = True, max(until, _T() + load)
        if _T() >= until:
            return hit
        time.sleep(0.1)


def _pick(pages: list[dict], match: str | None) -> dict:
    if not pages:
        raise RuntimeError("No page tabs. Launch Chrome first.")
    if not match:
        return pages[0]
    hit = [t for t in pages if t.get("id") == match] or [
        t for t in pages if match.lower() in (t.get("url", "") + " " + t.get("title", "")).lower()]
    if not hit:
        have = "\n  ".join(f"{t.get('title', '')[:40]!r} {t.get('url', '')[:70]}" for t in pages)
        raise RuntimeError(f"No tab matches {match!r}. Open tabs:\n  {have}")
    return hit[0]


# ── session ──────────────────────────────────────────────────────────
class CDP:
    """One websocket session on one tab.

    `match` is a target id or a URL/title substring. Without it you get the *first*
    tab, which is usually not the one you just opened.
    """

    def __init__(self, port: int = DEFAULT_PORT, match: str | None = None, timeout: float = 30.0):
        self.port = port
        self.target = _pick(tabs(port), match)
        hostpath = self.target["webSocketDebuggerUrl"][len("ws://"):]
        host, path = hostpath.split("/", 1)
        h, p = host.rsplit(":", 1)
        self.s = socket.create_connection((h, int(p)), timeout=timeout)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                       f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                       f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed during handshake")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"upgrade refused: {buf[:200]!r}")
        self._buf = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0
        self._frame_id = None
        self._ctx = None
        self.dialogs: list[dict] = []
        self.window_opens: list[dict] = []
        self._ready = False   # dialogs open before we attached may be a human's; leave them
        try:
            self.call("Page.enable", timeout=5)   # needed to hear javascriptDialogOpening
        except Exception:
            pass
        self._ready = True

    # websocket framing (client frames must be masked)
    def _send(self, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            hdr = b"\x81" + bytes([0x80 | n])
        elif n < 1 << 16:
            hdr = b"\x81\xfe" + struct.pack(">H", n)
        else:
            hdr = b"\x81\xff" + struct.pack(">Q", n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _frame(self) -> tuple[int, bytes]:
        # Only consume a frame once it has fully arrived, so a timeout never desyncs the stream.
        while True:
            b = self._buf
            if len(b) >= 2:
                op, n, i = b[0] & 0x0F, b[1] & 0x7F, 2
                if n == 126 and len(b) >= 4:
                    n, i = struct.unpack(">H", b[2:4])[0], 4
                elif n == 127 and len(b) >= 10:
                    n, i = struct.unpack(">Q", b[2:10])[0], 10
                if (n < 126 or i > 2) and len(b) >= i + n:
                    self._buf = b[i + n:]
                    return op, b[i:i + n]
            chunk = self.s.recv(1 << 16)
            if not chunk:
                raise ConnectionError("connection closed")
            self._buf += chunk

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        self._send(json.dumps({"id": mid, "method": method, "params": params or {}}).encode())
        deadline = _T() + timeout
        try:
            while True:
                left = deadline - _T()
                if left <= 0:
                    break
                self.s.settimeout(left)
                op, data = self._frame()
                if op == 0x8:
                    raise ConnectionError("browser closed the session")
                if op == 0x9:
                    continue
                msg = json.loads(data)
                if msg.get("method") == "Page.javascriptDialogOpening":
                    self._on_dialog(msg.get("params") or {})
                    continue
                if msg.get("method") == "Page.windowOpen":   # this tab asked for a window (window.open, a target link)
                    self.window_opens.append(msg.get("params") or {})
                    continue
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})
        except socket.timeout:
            pass
        raise TimeoutError(f"{method}: no reply in {timeout}s")

    def _on_dialog(self, p: dict) -> None:
        """alert: dismiss and go on. confirm/prompt/beforeunload: cancel, and let a human decide.

        An unhandled dialog freezes the renderer and every later call waits for its timeout.
        """
        kind = str(p.get("type", ""))
        d = {"type": kind, "message": str(p.get("message", ""))[:300]}
        if not self._ready:
            d["handled"] = "left alone (was open before attach)"
        else:
            accept = kind == "alert"
            self._id += 1
            self._send(json.dumps({"id": self._id, "method": "Page.handleJavaScriptDialog",
                                   "params": {"accept": accept}}).encode())
            d["handled"] = "dismissed" if accept else "cancelled"
        self.dialogs.append(d)

    def _isolated_ctx(self, timeout: float = 30.0) -> int:
        """Evaluate in an isolated world: same DOM, but page scripts cannot observe our calls."""
        if self._frame_id is None:
            self._frame_id = self.call("Page.getFrameTree", timeout=timeout)["frameTree"]["frame"]["id"]
        if self._ctx is None:
            r = self.call("Page.createIsolatedWorld",
                          {"frameId": self._frame_id, "worldName": "__atlas",
                           "grantUniveralAccess": True}, timeout=timeout)   # sic, spec spelling
            self._ctx = r["executionContextId"]
        return self._ctx

    def _eval_raw(self, js: str, ctx: int, timeout: float):
        r = self.call("Runtime.evaluate", {"expression": js, "contextId": ctx,
                                           "returnByValue": True, "awaitPromise": True}, timeout=timeout)
        exc = r.get("exceptionDetails")
        if exc:
            raise RuntimeError(exc.get("text", "") + " :: " +
                               str(exc.get("exception", {}).get("description", ""))[:300])
        return r.get("result", {}).get("value")

    def eval(self, js: str, timeout: float = 30.0):
        """Run JS and return the value. Batch reads into one array/object: one round trip each."""
        try:
            return self._eval_raw(js, self._isolated_ctx(timeout), timeout)
        except RuntimeError as e:
            m = str(e)
            if self._ctx is not None and any(s in m for s in (
                    "Cannot find context", "Execution context was destroyed",
                    "uniqueContextId", "Inspected target")):
                self._ctx = None      # navigation destroyed the world; rebuild once
                return self._eval_raw(js, self._isolated_ctx(timeout), timeout)
            raise

    def until(self, js: str, timeout: float = 20.0, poll: float = 0.04):
        """Poll until `js` is truthy. Use instead of sleep(N). Prefix DOM nodes with `!!`."""
        t0, last = _T(), None
        while _T() - t0 < timeout:
            try:
                v = self.eval(js)
                if v:
                    return v
            except Exception as e:
                last = e
            time.sleep(poll)
        raise TimeoutError(f"not met in {timeout}s: {js[:120]} (last error: {last})")

    def goto(self, url: str, until: str | None = None, timeout: float = 30.0) -> dict:
        """Navigate and wait for the *new* document (a stamp on the old one must disappear)."""
        t0 = _T()
        try:
            self.eval("window.__atlas_nav = 1")
        except Exception:
            pass
        self.call("Page.navigate", {"url": url}, timeout=timeout)
        while _T() - t0 < timeout:
            try:
                if not self.eval("window.__atlas_nav"):
                    break
            except Exception:
                pass
            time.sleep(0.03)
        got = self.until(until, timeout=max(1.0, timeout - (_T() - t0))) if until else None
        return {"url": self.eval("location.href"), "title": self.eval("document.title"),
                "elapsed_s": round(_T() - t0, 3), "until": got}

    def close(self) -> None:
        try:
            self.s.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
