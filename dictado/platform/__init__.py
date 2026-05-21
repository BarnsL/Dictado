"""Per-OS adapters for the three things that can't be done in pure Python:

    * register_hotkey(callback)        -- a global hotkey
    * paste_into_window(token)         -- synthesize Ctrl+V (or Cmd+V)
    * install_autostart() / uninstall  -- launch the daemon at login

The daemon imports `from dictado.platform import adapter`, which
returns the correct module for the current OS. Each module exposes the
same public functions; the daemon never branches on sys.platform itself.

If you're porting to a new OS, copy linux.py and adapt the three sections.
"""
from __future__ import annotations

import sys


def adapter():
    """Return the platform module for the current OS."""
    if sys.platform == "win32":
        from . import windows as plat
    elif sys.platform == "darwin":
        from . import macos as plat
    else:
        from . import linux as plat
    return plat
