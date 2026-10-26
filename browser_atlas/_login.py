"""Logins that never pass a password through the model.

The agent asks for `ensure_login(cdp, "hacker_news")`. The recipe says which fields to fill with
which stored credential (`@cred:NAME`); the value is read here and typed straight into the page.
It never appears in a return value, an exception or a log.

Most big sites cannot be logged into by a bot (captchas, device checks, magic links), and trying
gets accounts locked. Those recipes say `mode: manual`: the agent only checks whether the session
is alive and, when it is not, opens the login page for a human and stops. A human logs in once in
that Chrome profile and the session stays.

Recipes (no secrets inside) come from, in order: the atlas entry's `login` block (including the
local overlay `~/.browser_atlas/sites.local.json`), then `<creds dir>/sites.json`. The creds dir is
`BROWSER_ATLAS_CREDS`, else `~/.agent_creds`. On Windows values are DPAPI-encrypted to the user account.

    python -m browser_atlas set-cred hackernews_pw        # value from stdin or a hidden prompt
    python -m browser_atlas login hacker_news --port 9222

Note: this lowers the risk of leaking a password, not the power of the session. An agent that can
call `login("x")` can do whatever x allows; irreversible actions are still stopped by `_safety`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import _atlas

_PS_WRITE = ("$ErrorActionPreference='Stop';"
             "$v=[Console]::In.ReadToEnd().TrimEnd([char]13,[char]10);"
             "ConvertTo-SecureString $v -AsPlainText -Force | ConvertFrom-SecureString |"
             "Out-File -FilePath $env:CRED_FILE -Encoding utf8 -NoNewline")
_PS_READ = ("$ErrorActionPreference='Stop';"
            "$s=(Get-Content -Raw -Path $env:CRED_FILE).Trim() | ConvertTo-SecureString;"
            "$b=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s);"
            "[Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringAuto($b))")


class LoginError(RuntimeError):
    pass


# ── credential store ─────────────────────────────────────────────────
def cred_dir() -> Path:
    d = Path(os.environ.get("BROWSER_ATLAS_CREDS") or Path.home() / ".agent_creds")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ps(script: str, cred_file: Path, stdin: str | None = None) -> str:
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       input=stdin, capture_output=True, text=True, timeout=30,
                       env={**os.environ, "CRED_FILE": str(cred_file)})
    if p.returncode != 0:
        raise LoginError(f"DPAPI failed for {cred_file.name} (rc={p.returncode})")
    return p.stdout


def set_credential(name: str, value: str) -> Path:
    """Store a value. Refuses control characters and very short values: a failed paste
    (Ctrl+V arriving as \\x16) would otherwise be stored as a 'credential that exists'."""
    if any(ord(ch) < 32 for ch in value):
        raise ValueError(f"'{name}' contains control characters: the paste did not work")
    if len(value.strip()) < 4:
        raise ValueError(f"'{name}' is shorter than 4 characters: the paste probably did not work")
    d = cred_dir()
    if sys.platform == "win32":
        enc = d / f"{name}.cred"
        _ps(_PS_WRITE, enc, stdin=value)
        return enc
    plain = d / f"{name}.txt"
    plain.write_text(value, encoding="utf-8")
    os.chmod(plain, 0o600)
    return plain


def _credential(name: str) -> str:
    d = cred_dir()
    if (d / f"{name}.cred").exists():
        return _ps(_PS_READ, d / f"{name}.cred")
    if (d / f"{name}.txt").exists():
        return (d / f"{name}.txt").read_text(encoding="utf-8").strip()
    if name.upper() in os.environ:
        return os.environ[name.upper()]
    raise LoginError(f"credential '{name}' is not stored. A human runs: python -m browser_atlas set-cred {name}")


def list_credentials() -> list[str]:
    """Names only, never values."""
    d = cred_dir()
    return sorted({p.stem for p in d.glob("*.cred")} | {p.stem for p in d.glob("*.txt")})


def _resolve(value: str) -> str:
    if value.startswith("@cred:"):
        return _credential(value[6:])
    if value.startswith("@env:"):
        return os.environ[value[5:]]
    return value


# ── recipes ──────────────────────────────────────────────────────────
def recipes() -> dict:
    """All login recipes by site key. Atlas blocks win over the creds-dir file."""
    out: dict = {}
    f = cred_dir() / "sites.json"
    if f.exists():
        raw = json.loads(f.read_text(encoding="utf-8"))
        out.update({k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)})
    for key, e in _atlas.load().items():
        if isinstance(e.get("login"), dict):
            out[key] = e["login"]
    return out


def recipe(site: str) -> dict:
    r = recipes().get(site)
    if r is None:
        raise LoginError(f"no login recipe for '{site}'. Known: {sorted(recipes())}")
    return r


def summary() -> list[dict]:
    """What an agent may know: site, mode, whether the needed credentials exist. No values."""
    have = set(list_credentials())
    rows = []
    for k, r in sorted(recipes().items()):
        need = sorted({s["value"][6:] for s in r.get("steps", [])
                       if str(s.get("value", "")).startswith("@cred:")})
        rows.append({"site": k, "mode": r.get("mode", "auto"),
                     "credentials_ready": all(n in have for n in need), "missing": [n for n in need if n not in have]})
    return rows


# ── judging the session ──────────────────────────────────────────────
def _present(cdp, css: str, timeout: float, visible: bool = True) -> bool:
    """An element matching css within timeout; visible (non-zero size) unless visible=False."""
    js = ("(()=>{const e=document.querySelector(%s);if(!e)return false;const r=e.getBoundingClientRect();"
          "return r.width>0&&r.height>0})()" % json.dumps(css)) if visible else \
        "!!document.querySelector(%s)" % json.dumps(css)
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if cdp.eval(js):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _settle(cdp, seconds: float = 1.0) -> None:
    """Wait for load, then a moment for client-side redirects."""
    try:
        cdp.until("document.readyState === 'complete'", timeout=20)
    except TimeoutError:
        pass
    time.sleep(seconds)


def is_logged_in(cdp, r: dict, navigate: bool = True) -> bool:
    """All conditions in `logged_in_when` must hold (url_contains, url_not_contains, css, css_absent)."""
    cond = r.get("logged_in_when") or {}
    if not cond:
        return False                                  # no way to tell means "not logged in"
    if navigate and r.get("check_url"):
        cdp.goto(r["check_url"])
        _settle(cdp)
    url = cdp.eval("location.href") or ""
    if "url_contains" in cond and cond["url_contains"] not in url:
        return False
    if "url_not_contains" in cond and cond["url_not_contains"] in url:
        return False
    # a logout link often sits in a collapsed menu with zero size: being in the document is enough
    if "css" in cond and not _present(cdp, cond["css"], cond.get("timeout", 5), visible=False):
        return False
    if "css_absent" in cond and _present(cdp, cond["css_absent"], cond.get("timeout", 2)):
        return False
    return True


# ── logging in ───────────────────────────────────────────────────────
def _press_enter(cdp) -> None:
    from ._keys import key_events
    for method, params in key_events("\n"):
        cdp.call(method, params)


def _step(cdp, pointer, step: dict) -> None:
    action = step.get("action")
    css = step.get("css", "")
    if action == "goto":
        cdp.goto(step["url"])
        _settle(cdp, 0.5)
    elif action == "type":
        pointer.type(css, _resolve(step["value"]), clear=step.get("clear", True), secret=True)
    elif action == "click":
        if not _present(cdp, css, step.get("timeout", 10)):
            raise LoginError("not visible")
        pointer.click(css)
    elif action == "click_if":
        # optional gates such as "change your password later": a hidden copy of the modal may sit
        # in the DOM all the time, so wait for it to be *visible*, and move on if it never is
        if _present(cdp, css, step.get("timeout", 5)):
            pointer.click(css)
    elif action == "submit":                          # Enter in the field: the recipe is human-written
        pointer.click(css)
        _press_enter(cdp)
    elif action == "wait":
        time.sleep(float(step.get("seconds", 1)))
    elif action == "wait_css":
        if not _present(cdp, css, step.get("timeout", 15)):
            raise LoginError("did not appear")
    else:
        raise LoginError(f"unknown action {action!r}")


def login(cdp, site: str, pointer=None) -> dict:
    """Run the recipe once. Returns {ok, site, mode, needs_human, reason}; never retries,
    because repeated failed logins are what locks accounts."""
    from ._pointer import Pointer
    r = recipe(site)
    out = {"ok": False, "site": site, "mode": r.get("mode", "auto"), "needs_human": False, "reason": ""}
    if out["mode"] == "manual":
        if r.get("login_url") or r.get("check_url"):
            cdp.goto(r.get("login_url") or r["check_url"])
        out.update(needs_human=True, reason="manual-only site: a human logs in once in this Chrome window")
        return out
    try:
        cdp.call("Page.bringToFront")                 # a covered window swallows clicks silently
    except Exception:
        pass
    if r.get("login_url"):
        cdp.goto(r["login_url"])
        _settle(cdp, 0.5)
    pointer = pointer or Pointer(cdp)
    for i, step in enumerate(r.get("steps", [])):
        try:
            _step(cdp, pointer, step)
        except Exception as e:                        # never include values, only where it broke
            if is_logged_in(cdp, r, navigate=False):  # no login form because the session was alive after all
                out.update(ok=True, reason="already logged in (the login form was not there)")
                return out
            msg = str(e) if isinstance(e, LoginError) and "credential" in str(e) else type(e).__name__
            out.update(needs_human=True,
                       reason=f"step[{i}] {step.get('action')} css={step.get('css')!r} failed ({msg})")
            return out
    _settle(cdp)
    out["ok"] = is_logged_in(cdp, r)
    if not out["ok"]:
        said = "; ".join(d["message"][:120] for d in getattr(cdp, "dialogs", []))
        out.update(needs_human=True,
                   reason="steps ran but the session is not logged in (2FA, captcha or a new-device check?)"
                          + (f" Page said: {said}" if said else ""))
    return out


def ensure_login(cdp, site: str) -> dict:
    """Check first; log in only when the session is dead."""
    r = recipe(site)
    if is_logged_in(cdp, r):
        return {"ok": True, "site": site, "mode": r.get("mode", "auto"), "needs_human": False,
                "reason": "already logged in"}
    return login(cdp, site)
