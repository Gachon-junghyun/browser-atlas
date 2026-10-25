"""claude-browser-atlas: a site atlas and a learned pointer for browser agents."""
from ._atlas import Blocked, guard, load as load_atlas, site_for
from ._cdp import CDP, close_tab, launch, new_tab, status, tabs
from ._mouse import Model, fit, record, segments, summary
from ._pointer import Pointer
from ._keys import TypingModel

__all__ = ["Blocked", "CDP", "Model", "Pointer", "TypingModel", "close_tab", "fit", "guard", "launch",
           "load_atlas", "new_tab", "record", "segments", "site_for", "status", "summary", "tabs"]
