"""Linux adapter for dictado.

Status: BEST-EFFORT. Tested briefly on X11; Wayland support is partial.

  1. Hotkey  -- via pynput on X11. Wayland: bind your DE's keyboard
     shortcut to `python -m dictado --toggle`.
  2. Paste   -- xdotool key ctrl+v (X11), wtype -M ctrl v -m ctrl (Wayland).
  3. Autostart -- ~/.config/autostart/dictado.desktop (XDG standard).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("dictado.linux")


def _is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" \
        or bool(os.environ.get("WAYLAND_DISPLAY"))


_PYNPUT_MOD = {"ctrl": "<ctrl>", "shift": "<shift>",
               "alt": "<alt>", "win": "<cmd>"}


def _spec_to_pynput(spec: str) -> str:
    from dictado.config import parse_hotkey
    mods, key = parse_hotkey(spec)
    return "+".join([_PYNPUT_MOD[m] for m in mods] + [key])


class HotkeyHandle:
    """Same shape as the Windows handle. Wayland: register_hotkey returns
    a handle whose listener is None and rebind() is a no-op."""
    def __init__(self, callback, spec: str):
        self._callback = callback
        self._listener = None
        self._spec = spec
        if not _is_wayland():
            self._start(spec)
        else:
            logger.warning("Wayland session: bind your DE's shortcut to "
                           "`python -m dictado --toggle`.")

    def _start(self, spec: str) -> None:
        try:
            from pynput import keyboard
        except ImportError:
            logger.warning("pynput not installed; install with `pip install pynput`.")
            return
        try:
            hk = _spec_to_pynput(spec)
        except ValueError as e:
            logger.error("hotkey spec %r rejected: %s", spec, e); return
        h = keyboard.GlobalHotKeys({hk: self._on_activate})
        h.start()
        self._listener = h
        self._spec = spec
        logger.info("hotkey registered (linux pynput X11): %s", spec)

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
        if not _is_wayland():
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
    if _is_wayland():
        if shutil.which("wtype"):
            subprocess.run(["wtype", "-M", "ctrl", "v", "-m", "ctrl"],
                           check=False)
        else:
            logger.warning("Wayland paste requires `wtype`.")
        return
    if shutil.which("xdotool"):
        subprocess.run(["xdotool", "key", "ctrl+v"], check=False)
    else:
        logger.warning("X11 paste requires `xdotool`.")


def _autostart_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME",
                               str(Path.home() / ".config"))) \
           / "autostart" / "dictado.desktop"


def install_autostart(python_exe: str, script_path: str) -> Path:
    p = _autostart_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"""[Desktop Entry]
Type=Application
Name=dictado
Comment=Local voice-activated and push-to-talk voice-to-text
Exec={python_exe} {script_path}
Terminal=false
X-GNOME-Autostart-enabled=true
""", encoding="utf-8")
    p.chmod(0o644)
    logger.info("Autostart .desktop installed at %s", p)
    return p


def uninstall_autostart() -> None:
    p = _autostart_path()
    if p.exists():
        p.unlink()
        logger.info("Autostart .desktop removed from %s", p)
