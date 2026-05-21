"""macOS adapter for dictado.

Status: BEST-EFFORT.

  1. Hotkey  -- via pynput. The "alt+t" cross-platform spec is mapped to
     pynput's "<alt>+t" canonical form. On macOS Cmd is also available
     ("win+t" or "cmd+t" both work as the spec).
  2. Paste   -- AppleScript `key code 9 using command down`. Same primitive
     every clipboard manager on macOS uses (Paste, Maccy, ...).
  3. Autostart -- LaunchAgent plist in ~/Library/LaunchAgents/.

Required permissions on first launch:
  * Accessibility (System Settings > Privacy & Security > Accessibility)
  * Microphone (System Settings > Privacy & Security > Microphone)
  * Input Monitoring (only if you use the global hotkey via pynput)
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("dictado.macos")

# Cross-platform spec token -> pynput canonical token.
_PYNPUT_MOD = {"ctrl": "<ctrl>", "shift": "<shift>",
               "alt": "<alt>", "win": "<cmd>"}


def _spec_to_pynput(spec: str) -> str:
    """Translate "alt+t" -> "<alt>+t" for pynput.GlobalHotKeys."""
    from dictado.config import parse_hotkey
    mods, key = parse_hotkey(spec)
    return "+".join([_PYNPUT_MOD[m] for m in mods] + [key])


class HotkeyHandle:
    """Thin wrapper that lets the tray menu rebind the hotkey live."""
    def __init__(self, callback, spec: str):
        self._callback = callback
        self._listener = None
        self._spec = spec
        self._start(spec)

    def _start(self, spec: str) -> None:
        try:
            from pynput import keyboard
        except ImportError:
            logger.warning("pynput not installed; global hotkey unavailable. "
                           "`pip install pynput`.")
            return
        try:
            hk = _spec_to_pynput(spec)
        except ValueError as e:
            logger.error("hotkey spec %r rejected: %s", spec, e)
            return
        h = keyboard.GlobalHotKeys({hk: self._on_activate})
        h.start()
        self._listener = h
        self._spec = spec
        logger.info("hotkey registered (macos pynput): %s", spec)

    def _on_activate(self) -> None:
        import threading
        threading.Thread(target=self._callback, daemon=True).start()

    @property
    def spec(self) -> str:
        return self._spec

    def rebind(self, new_spec: str) -> None:
        if self._listener is not None:
            try: self._listener.stop()
            except Exception: pass
            self._listener = None
        self._start(new_spec)

    def stop(self) -> None:
        if self._listener is not None:
            try: self._listener.stop()
            except Exception: pass
            self._listener = None


def register_hotkey(callback, spec: str = "alt+t") -> HotkeyHandle:
    return HotkeyHandle(callback, spec)


def get_foreground_window() -> int:
    return 0


def paste_into_window(hwnd: int = 0) -> None:
    """key code 9 = V on the US layout. AppleScript `keystroke "v"` would
    rely on layout mapping and is less reliable across keyboards."""
    osa = ('tell application "System Events" to '
           'key code 9 using {command down}')
    try:
        subprocess.run(["osascript", "-e", osa], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        logger.warning("osascript paste failed: %s",
                       e.stderr.decode("utf-8", "ignore"))


LAUNCH_AGENT_LABEL = "io.github.dictado.daemon"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def install_autostart(python_exe: str, script_path: str) -> Path:
    p = _plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>{LAUNCH_AGENT_LABEL}</string>
  <key>ProgramArguments</key> <array>
    <string>{python_exe}</string>
    <string>{script_path}</string>
  </array>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <false/>
  <key>StandardOutPath</key>  <string>/tmp/dictado.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/dictado.err.log</string>
</dict>
</plist>
""", encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(p)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "load",   str(p)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logger.info("LaunchAgent installed at %s", p)
    return p


def uninstall_autostart() -> None:
    p = _plist_path()
    if p.exists():
        subprocess.run(["launchctl", "unload", str(p)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        p.unlink()
        logger.info("LaunchAgent removed from %s", p)
