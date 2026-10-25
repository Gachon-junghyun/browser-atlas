"""Read-only health checks for the atlas button map, in disposable tabs."""
from __future__ import annotations

import random
import time
from datetime import date

from . import _atlas, _ax
from ._cdp import CDP, close_tab, new_tab


def check(site_key, port, sites=None, wait=(3, 9)) -> dict:
    sites = sites if sites is not None else _atlas.load()
    site = sites[site_key]
    result = {"site": site_key, "checked": date.today().isoformat(), "pages": {}}
    if site.get("access") == "blocked":
        return result
    for i, (key, page) in enumerate(site.get("pages", {}).items()):
        if i:
            time.sleep(random.uniform(*wait))
        controls = page.get("controls", [])
        found, missing = [], []
        tab = None
        error = None
        try:
            _atlas.guard(page["url"], sites)
            tab = new_tab(port=port)
            with CDP(port, match=tab["id"]) as c:
                c.goto(page["url"])
                if page.get("ready"):
                    c.until(page["ready"], timeout=20)
                _atlas.guard(c.eval("location.href"), sites)
                nodes = _ax.snapshot(c)
                for control in controls:
                    hit = _ax.find(c, control["role"], control["name"], nodes=nodes)
                    (found if hit else missing).append(control)
        except (RuntimeError, OSError, TimeoutError, _atlas.Blocked) as exc:
            missing = list(controls)
            found = []
            error = str(exc)
        finally:
            if tab is not None:
                close_tab(tab["id"], port)
        report = {"ok": not missing and error is None, "found": found, "missing": missing}
        if error is not None:
            report["error"] = error
        result["pages"][key] = report
    return result
