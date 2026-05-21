"""dictado.agent_input — Agent Input Mode (AIM).

Why this exists
---------------
Vanilla auto-paste lands transcribed text in whatever has focus and
stops there. For AI assistants (ChatGPT, Claude, Cursor, etc.) and
chat apps (Slack, Teams, Discord), the user's actual goal is "send the
message", not "type it and stop".

Agent Input Mode handles the last mile: paste, then press Enter. It can
also re-focus a specific app first, so you can dictate a message into,
say, the ChatGPT desktop window without having to alt-tab to it before
pressing the hotkey.

Three operating modes (selected from the tray menu)
---------------------------------------------------
1. Off          — today's behavior. Clipboard + auto-paste, no Enter.
2. Auto         — Clipboard + auto-paste + Enter into whatever has focus.
3. <app name>   — Activate <app> first, then clipboard + paste + Enter.

App detection
-------------
We discover installed apps two ways and union the results:

  * App profiles    — see APP_PROFILES below. A small curated list of
                      popular targets (chat, IDE, AI assistant). Each
                      profile knows how to detect "is this app
                      installed?" and how to "raise it to foreground"
                      using only Win32 / pure Python primitives.
  * Registry sweep  — read HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall
                      and the matching HKLM hive for DisplayName values.
                      We don't surface these in the menu unless they
                      match an APP_PROFILES regex; the registry sweep
                      is mainly useful for "what's installed?"
                      diagnostics and for future profile additions.

Only profiles whose "is installed?" check returns True appear in the
tray submenu. If a profile is selected but the user runs it on a
machine where the app was uninstalled later, AIM falls back to "Auto"
behavior so the user's hotkey still does something useful.

Cross-platform
--------------
The agent_input module exposes a stable API regardless of OS:

    detect_apps()                -> list[App]
    activate(app, timeout_s)     -> bool
    send_enter()                 -> None

`detect_apps()` returns an empty list on macOS/Linux today; profiles
for those platforms can be added incrementally without touching the
daemon.

Privacy
-------
Detection is read-only. We never spawn the discovered apps, never
write to their config, and never call into them via DLL/COM. The
Win32 calls used (`EnumWindows`, `GetWindowThreadProcessId`,
`SetForegroundWindow`) are the same ones every window-management
utility uses (Sizer, AltSnap, FancyZones).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("dictado.agent_input")


# ─── Public types ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class App:
    """One supported AIM target. id is stable; label is for the UI."""
    id: str                # e.g. "chatgpt"; used in config.json
    label: str             # e.g. "ChatGPT"; shown in the tray menu
    detect: Callable[[], bool]
    activate: Callable[[], bool]


# ─── Win32 plumbing (no-op on non-Windows) ────────────────────────────────────
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32   = ctypes.WinDLL("user32",   use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.argtypes  = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype  = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype  = wintypes.BOOL
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                 ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype  = wintypes.DWORD

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype  = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    SW_RESTORE = 9


    def _process_image_for_pid(pid: int) -> str:
        """Return the executable path for a PID, or '' on failure."""
        h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf  = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            _kernel32.CloseHandle(h)
        return ""


    def _enum_windows():
        """Yield (hwnd, title, class_name, image_path) for every visible
        top-level window. Skip our own popup windows."""
        EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL,
                                             wintypes.HWND, wintypes.LPARAM)
        results: list[tuple[int, str, str, str]] = []

        def _cb(hwnd, _lparam):
            if not _user32.IsWindowVisible(hwnd):
                return True
            title_len = _user32.GetWindowTextLengthW(hwnd)
            if title_len == 0:
                return True
            buf = ctypes.create_unicode_buffer(title_len + 1)
            _user32.GetWindowTextW(hwnd, buf, title_len + 1)
            title = buf.value
            cbuf = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, cbuf, 256)
            cls = cbuf.value
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            img = _process_image_for_pid(pid.value)
            results.append((hwnd, title, cls, img))
            return True

        _user32.EnumWindows(EnumWindowsProc(_cb), 0)
        return results


    def _find_window_for_image(image_basenames: tuple[str, ...]) -> int:
        """Return the HWND of the first visible window whose owning
        process matches one of `image_basenames` (case-insensitive). 0
        if not found."""
        wanted = {b.lower() for b in image_basenames}
        for hwnd, _title, _cls, img in _enum_windows():
            if not img:
                continue
            base = os.path.basename(img).lower()
            if base in wanted:
                return hwnd
        return 0


    def _find_window_for_title_regex(pattern: re.Pattern) -> int:
        """Return the HWND of the first visible window whose title
        matches `pattern`, 0 if not found."""
        for hwnd, title, _cls, _img in _enum_windows():
            if pattern.search(title):
                return hwnd
        return 0


    def _activate_hwnd(hwnd: int) -> bool:
        """Restore the window if minimized and bring it to foreground."""
        if not hwnd:
            return False
        _user32.ShowWindow(hwnd, SW_RESTORE)
        return bool(_user32.SetForegroundWindow(hwnd))


    def _send_enter() -> None:
        """SendInput one VK_RETURN press. Same SendInput primitive used
        for the Ctrl+V auto-paste; one chord per dictation, exactly the
        shape every clipboard manager produces."""
        from dictado.platform.windows import _INPUT, _KEYBDINPUT, \
            INPUT_KEYBOARD, KEYEVENTF_KEYUP
        VK_RETURN = 0x0D
        inputs = (_INPUT * 2)()
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].u.ki = _KEYBDINPUT(VK_RETURN, 0, 0, 0, None)
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].u.ki = _KEYBDINPUT(VK_RETURN, 0, KEYEVENTF_KEYUP, 0, None)
        n = _user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(_INPUT))
        if n != 2:
            logger.warning("Enter SendInput injected %d/2 events.", n)


    # ─── App profiles ─────────────────────────────────────────────────────
    # Each profile has a `detect()` returning True iff there's a live
    # window for the app (so the menu only lists running OR
    # window-discoverable apps), and an `activate()` that re-focuses
    # the window. The detection uses image basenames AND title regex
    # because Electron apps frequently spawn their own window class
    # ("Chrome_WidgetWin_1") and only the title or process image is
    # disambiguating.

    def _profile_by_image(*basenames: str):
        bn = basenames
        def _detect():  return _find_window_for_image(bn) != 0
        def _activate(): return _activate_hwnd(_find_window_for_image(bn))
        return _detect, _activate

    def _profile_by_title(pattern: str):
        rx = re.compile(pattern, re.IGNORECASE)
        def _detect():  return _find_window_for_title_regex(rx) != 0
        def _activate(): return _activate_hwnd(_find_window_for_title_regex(rx))
        return _detect, _activate

    _PROFILES_RAW: list[tuple[str, str, tuple]] = [
        # AI assistants ------------------------------------------------
        ("chatgpt",   "ChatGPT (desktop)",     _profile_by_image("ChatGPT.exe")),
        ("claude",    "Claude (desktop)",      _profile_by_image("Claude.exe")),
        ("copilot",   "Microsoft Copilot",     _profile_by_image("Copilot.exe", "ai.exe")),
        ("amazon-quick", "Amazon Quick",          _profile_by_image("Amazon Quick.exe")),
        # IDEs / editors ----------------------------------------------
        ("cursor",    "Cursor",                _profile_by_image("Cursor.exe")),
        ("vscode",    "Visual Studio Code",    _profile_by_image("Code.exe")),
        ("vscode_insiders", "VS Code Insiders",_profile_by_image("Code - Insiders.exe")),
        ("kiro",      "Kiro",                  _profile_by_image("Kiro.exe")),
        ("zed",       "Zed",                   _profile_by_image("Zed.exe")),
        ("neovide",   "Neovide",               _profile_by_image("neovide.exe")),
        ("idea",      "JetBrains IDE",         _profile_by_image("idea64.exe", "pycharm64.exe", "webstorm64.exe", "rider64.exe", "clion64.exe")),
        # Chat / messaging --------------------------------------------
        ("slack",     "Slack",                 _profile_by_image("slack.exe")),
        ("teams",     "Microsoft Teams",       _profile_by_image("Teams.exe", "ms-teams.exe")),
        ("discord",   "Discord",               _profile_by_image("Discord.exe")),
        ("telegram",  "Telegram",              _profile_by_image("Telegram.exe")),
        ("whatsapp",  "WhatsApp Desktop",      _profile_by_image("WhatsApp.exe")),
        ("signal",    "Signal",                _profile_by_image("Signal.exe")),
        # Browsers (helpful for chat in webapps) ----------------------
        ("chrome",    "Google Chrome",         _profile_by_image("chrome.exe")),
        ("edge",      "Microsoft Edge",        _profile_by_image("msedge.exe")),
        ("firefox",   "Firefox",               _profile_by_image("firefox.exe")),
        # Notes / writing ---------------------------------------------
        ("obsidian",  "Obsidian",              _profile_by_image("Obsidian.exe")),
        ("notion",    "Notion",                _profile_by_image("Notion.exe")),
    ]

    APP_PROFILES: list[App] = [
        App(id=pid, label=label, detect=detect_fn, activate=activate_fn)
        for pid, label, (detect_fn, activate_fn) in _PROFILES_RAW
    ]


    def detect_apps() -> list[App]:
        """Return profiles whose detect() returns True right now."""
        results = []
        for app in APP_PROFILES:
            try:
                if app.detect():
                    results.append(app)
            except Exception:
                logger.exception("detect() raised for %r; skipping.", app.id)
        return results


    def activate(app_id: str, timeout_s: float = 0.5) -> bool:
        """Activate the named app and wait briefly for focus to settle."""
        for app in APP_PROFILES:
            if app.id == app_id:
                ok = False
                try:
                    ok = bool(app.activate())
                except Exception:
                    logger.exception("activate() raised for %r.", app_id)
                # Spin briefly so subsequent SendInput lands on the new fg.
                if ok:
                    time.sleep(min(0.25, max(0.05, timeout_s)))
                return ok
        logger.warning("activate(%r): no such profile.", app_id)
        return False


    def send_enter() -> None:
        _send_enter()


else:  # macOS / Linux: stubs, no profiles surfaced today.
    APP_PROFILES: list[App] = []

    def detect_apps() -> list[App]:
        return []

    def activate(app_id: str, timeout_s: float = 0.5) -> bool:
        logger.warning("Agent Input Mode app activation isn't implemented "
                       "on %s yet.", sys.platform)
        return False

    def send_enter() -> None:
        # The right primitive on macOS is `osascript` "key code 36"; on
        # Linux X11 it's `xdotool key Return`. Wire these in dictado.platform
        # when a non-Windows user wants AIM.
        logger.warning("send_enter() is a no-op on %s for now.", sys.platform)


# ─── Public helpers used by the daemon ────────────────────────────────────────
def app_label_for(app_id: str) -> str:
    """Pretty-print an app id for tray-menu labels."""
    if not app_id or app_id == "off":
        return "Off"
    if app_id == "auto":
        return "Auto (focused window)"
    for app in APP_PROFILES:
        if app.id == app_id:
            return app.label
    return app_id  # fallback: show the raw id
