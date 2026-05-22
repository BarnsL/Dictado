"""dictado.agent_input -- Agent Input Mode (AIM).

Why this exists
---------------
Vanilla auto-paste lands transcribed text in whatever has focus and
stops there. For AI assistants (ChatGPT, Claude, Cursor, Amazon Quick,
etc.) and chat apps (Slack, Teams, Discord), the user's actual goal is
"send the message", not "type it and stop".

Agent Input Mode handles the last mile: paste, then press Enter. It can
also re-focus a specific app first, so you can dictate a message into,
say, the ChatGPT desktop window without having to alt-tab to it before
pressing the hotkey.

Three operating modes (selected from the tray menu)
---------------------------------------------------
1. Off          -- today's behavior. Clipboard + auto-paste, no Enter.
2. Auto         -- Clipboard + auto-paste + Enter into whatever has focus.
3. <app name>   -- Activate <app> first, then clipboard + paste + Enter.

App detection
-------------
We discover installed apps two ways and union the results:

  * App profiles    -- see APP_PROFILES below. A small curated list of
                       popular targets (chat, IDE, AI assistant). Each
                       profile knows how to detect "is this app
                       installed?" and how to "raise it to foreground"
                       using only Win32 / pure Python primitives.
  * Registry sweep  -- read HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall
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
    activate(app_id, timeout_s)  -> bool       # back-compat wrapper
    activate_target(app_id, ...) -> int        # returns target HWND so
                                               # paste_into_window can
                                               # re-verify focus before
                                               # the Ctrl+V chord.
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


# --- Public types -----------------------------------------------------------
@dataclass(frozen=True)
class App:
    """One supported AIM target. id is stable; label is for the UI."""
    id: str                # e.g. "chatgpt"; used in config.json
    label: str             # e.g. "ChatGPT"; shown in the tray menu
    detect: Callable[[], bool]
    activate: Callable[[], bool]
    locate:   Callable[[], int]   # returns target HWND (0 if none)
    # Optional hook fired after the foreground swap has settled but
    # BEFORE the daemon's Ctrl+V chord is sent. Used for Electron AI
    # apps where SetForegroundWindow alone doesn't guarantee that the
    # prompt input has keyboard focus -- the hook sends a small chord
    # (typically Ctrl+L, the conventional "focus prompt input" combo
    # for ChatGPT desktop / Claude desktop / Cursor / Amazon Quick)
    # that nudges focus onto the input field.
    post_activate: Callable[[], None] | None = None


# --- Win32 plumbing (no-op on non-Windows) ----------------------------------
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
        top-level window."""
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
        """Return the HWND of the highest-scored visible window whose
        owning process matches one of `image_basenames` (case-insensitive),
        0 if not found.

        Skips obvious noise: tooltip-class windows and our own popup
        windows. Prefers windows that currently have a non-empty title
        (those are real top-level application windows; Electron also
        creates hidden helper windows we want to avoid)."""
        wanted = {b.lower() for b in image_basenames}
        candidates: list[tuple[int, int, str]] = []
        for hwnd, title, cls, img in _enum_windows():
            if not img:
                continue
            base = os.path.basename(img).lower()
            if base not in wanted:
                continue
            if cls in ("tooltips_class32", "MSCTFIME UI"):
                continue
            tl = title.lower()
            if "dictadoar" in tl or "voicepad" in tl or "dictado" == tl:
                continue
            score = 1
            if title.strip():
                score += 2
            candidates.append((hwnd, score, title))
        if not candidates:
            return 0
        candidates.sort(key=lambda t: t[1], reverse=True)
        return candidates[0][0]


    def _find_window_for_title_regex(pattern: re.Pattern) -> int:
        """Return the HWND of the first visible window whose title
        matches `pattern`, 0 if not found."""
        for hwnd, title, _cls, _img in _enum_windows():
            if pattern.search(title):
                return hwnd
        return 0


    def _activate_hwnd(hwnd: int) -> bool:
        """Bring window to foreground using the platform's verified focus
        helper (handles the AttachThreadInput dance + retry-on-fail).
        Falls back to bare SetForegroundWindow if the helper isn't
        available (e.g. older platform module)."""
        if not hwnd:
            return False
        try:
            from dictado.platform.windows import focus_window
        except Exception:
            return bool(_user32.SetForegroundWindow(hwnd))
        return focus_window(hwnd, timeout_s=1.0)


    # --- Per-profile post-activate focus hints --------------------------
    # These run AFTER the foreground swap has settled, BEFORE the daemon's
    # Ctrl+V chord. They're for Electron / Chromium apps where window-
    # level focus doesn't propagate to the inner WebContents prompt input.

    def _send_ctrl_l() -> None:
        """SendInput one Ctrl+L chord -- "focus the prompt input" hint
        for Electron AI apps.

        Why this is needed at all:
        SetForegroundWindow + AttachThreadInput brings an Electron
        window to the foreground but does NOT guarantee that the inner
        WebContents focus lands on the prompt input. Without a focus
        hint, our subsequent Ctrl+V chord ends up on whatever
        sub-control was last focused (sidebar item, dropdown, etc.),
        the paste silently no-ops, and the user sees the window come
        forward with no text inserted -- exactly the symptom observed
        with Amazon Quick.

        Ctrl+L is the cheapest portable focus hint that works across
        the major Electron AI apps (ChatGPT desktop, Claude desktop,
        Cursor, Amazon Quick all bind it to "focus prompt" / "new
        chat", both of which leave the prompt input focused).
        """
        try:
            from dictado.platform.windows import _INPUT, _KEYBDINPUT, \
                INPUT_KEYBOARD, KEYEVENTF_KEYUP
        except Exception:
            logger.exception("_send_ctrl_l: platform.windows missing the "
                             "SendInput primitives; skipping focus hint.")
            return
        VK_CONTROL = 0x11
        VK_L       = 0x4C
        inputs = (_INPUT * 4)()
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].u.ki = _KEYBDINPUT(VK_CONTROL, 0, 0, 0, None)
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].u.ki = _KEYBDINPUT(VK_L, 0, 0, 0, None)
        inputs[2].type = INPUT_KEYBOARD
        inputs[2].u.ki = _KEYBDINPUT(VK_L, 0, KEYEVENTF_KEYUP, 0, None)
        inputs[3].type = INPUT_KEYBOARD
        inputs[3].u.ki = _KEYBDINPUT(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0, None)
        n = _user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(_INPUT))
        if n != 4:
            logger.warning("Ctrl+L SendInput injected %d/4 events.", n)


    # --- App profiles ---------------------------------------------------
    # Each profile carries:
    #   detect()        -> bool : True iff there's a live window for the app
    #   activate()      -> bool : Re-focus it (verified-focus dance)
    #   locate()        -> int  : HWND that activate() targeted (so the
    #                             daemon can pass it to paste_into_window
    #                             for a second verification before pasting)
    #   post_activate   -> None : Optional Electron focus hint (Ctrl+L)

    def _profile_by_image(*basenames: str):
        bn = basenames
        def _locate():   return _find_window_for_image(bn)
        def _detect():   return _locate() != 0
        def _activate(): return _activate_hwnd(_locate())
        return _detect, _activate, _locate

    def _profile_by_title(pattern: str):
        rx = re.compile(pattern, re.IGNORECASE)
        def _locate():   return _find_window_for_title_regex(rx)
        def _detect():   return _locate() != 0
        def _activate(): return _activate_hwnd(_locate())
        return _detect, _activate, _locate

    # Each row: (id, label, _profile_pair) OR (id, label, _profile_pair, post_activate)
    _PROFILES_RAW: list[tuple] = [
        # AI assistants ------------------------------------------------
        ("chatgpt",      "ChatGPT (desktop)",  _profile_by_image("ChatGPT.exe"),         _send_ctrl_l),
        ("claude",       "Claude (desktop)",   _profile_by_image("Claude.exe"),          _send_ctrl_l),
        ("copilot",      "Microsoft Copilot",  _profile_by_image("Copilot.exe", "ai.exe"), _send_ctrl_l),
        ("amazon-quick", "Amazon Quick",       _profile_by_image("Amazon Quick.exe"),    _send_ctrl_l),
        # IDEs / editors ----------------------------------------------
        ("cursor",       "Cursor",             _profile_by_image("Cursor.exe"),          _send_ctrl_l),
        ("vscode",       "Visual Studio Code", _profile_by_image("Code.exe")),
        ("vscode_insiders", "VS Code Insiders",_profile_by_image("Code - Insiders.exe")),
        ("kiro",         "Kiro",               _profile_by_image("Kiro.exe")),
        ("zed",          "Zed",                _profile_by_image("Zed.exe")),
        ("neovide",      "Neovide",            _profile_by_image("neovide.exe")),
        ("idea",         "JetBrains IDE",      _profile_by_image("idea64.exe", "pycharm64.exe", "webstorm64.exe", "rider64.exe", "clion64.exe")),
        # Chat / messaging --------------------------------------------
        ("slack",        "Slack",              _profile_by_image("slack.exe")),
        ("teams",        "Microsoft Teams",    _profile_by_image("Teams.exe", "ms-teams.exe")),
        ("discord",      "Discord",            _profile_by_image("Discord.exe")),
        ("telegram",     "Telegram",           _profile_by_image("Telegram.exe")),
        ("whatsapp",     "WhatsApp Desktop",   _profile_by_image("WhatsApp.exe")),
        ("signal",       "Signal",             _profile_by_image("Signal.exe")),
        # Browsers (helpful for chat in webapps) ----------------------
        ("chrome",       "Google Chrome",      _profile_by_image("chrome.exe")),
        ("edge",         "Microsoft Edge",     _profile_by_image("msedge.exe")),
        ("firefox",      "Firefox",            _profile_by_image("firefox.exe")),
        # Notes / writing ---------------------------------------------
        ("obsidian",     "Obsidian",           _profile_by_image("Obsidian.exe")),
        ("notion",       "Notion",             _profile_by_image("Notion.exe")),
    ]

    # Build the App list, allowing the optional 4th tuple slot to be a
    # post_activate callable. Profiles without it default to None.
    APP_PROFILES: list[App] = []
    for _row in _PROFILES_RAW:
        if len(_row) == 4:
            _pid, _label, (_d, _a, _l), _post = _row
        else:
            _pid, _label, (_d, _a, _l) = _row
            _post = None
        APP_PROFILES.append(App(
            id=_pid,
            label=_label,
            detect=_d,
            activate=_a,
            locate=_l,
            post_activate=_post,
        ))


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


    def _profile(app_id: str) -> "App | None":
        for app in APP_PROFILES:
            if app.id == app_id:
                return app
        return None


    def activate(app_id: str, timeout_s: float = 1.0) -> bool:
        """Activate the named app and wait briefly for focus to settle.

        Kept for back-compat; new callers should use activate_target()
        which also returns the HWND so paste_into_window can verify
        focus a second time right before pressing Ctrl+V."""
        return activate_target(app_id, timeout_s=timeout_s) != 0


    def activate_target(app_id: str, *, timeout_s: float = 1.0) -> int:
        """Focus the named app's main window and return its HWND.

        Returns 0 if the profile is unknown, the app isn't running, or
        focus could not be transferred. The caller can then either fall
        back to 'auto' (paste into whatever is foreground now) or skip
        the paste step entirely.

        If the app profile defines a ``post_activate`` hook (used for
        Electron apps where SetForegroundWindow alone doesn't focus
        the prompt input), the hook fires AFTER focus_window has
        confirmed the foreground swap, and an extra 120 ms slice is
        inserted before returning so the daemon's subsequent Ctrl+V
        lands on the now-focused input.
        """
        app = _profile(app_id)
        if app is None:
            logger.warning("activate_target(%r): no such profile.", app_id)
            return 0
        try:
            hwnd = app.locate()
        except Exception:
            logger.exception("locate() raised for %r.", app_id)
            return 0
        if not hwnd:
            logger.info("activate_target(%r): no live window found.", app_id)
            return 0
        try:
            from dictado.platform.windows import focus_window
        except Exception:
            ok = bool(app.activate())
            if ok:
                time.sleep(min(0.25, max(0.05, timeout_s)))
                if app.post_activate is not None:
                    try:
                        app.post_activate()
                    except Exception:
                        logger.exception(
                            "post_activate() raised for %r.", app_id)
                    time.sleep(0.12)
                return hwnd
            return 0
        if focus_window(hwnd, timeout_s=timeout_s):
            if app.post_activate is not None:
                try:
                    app.post_activate()
                except Exception:
                    logger.exception(
                        "post_activate() raised for %r.", app_id)
                time.sleep(0.12)
            return hwnd
        logger.warning("activate_target(%r): focus_window did not transfer "
                       "focus within %.2fs; AIM will fall back to 'auto'.",
                       app_id, timeout_s)
        return 0


    def send_enter() -> None:
        """SendInput one VK_RETURN press. Same SendInput primitive used
        for the Ctrl+V auto-paste; one chord per dictation, exactly the
        shape every clipboard manager produces."""
        try:
            from dictado.platform.windows import _INPUT, _KEYBDINPUT, \
                INPUT_KEYBOARD, KEYEVENTF_KEYUP
        except Exception:
            logger.exception("send_enter: platform.windows missing the "
                             "SendInput primitives; Enter NOT sent.")
            return
        VK_RETURN = 0x0D
        inputs = (_INPUT * 2)()
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].u.ki = _KEYBDINPUT(VK_RETURN, 0, 0, 0, None)
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].u.ki = _KEYBDINPUT(VK_RETURN, 0, KEYEVENTF_KEYUP, 0, None)
        n = _user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(_INPUT))
        if n != 2:
            logger.warning("Enter SendInput injected %d/2 events.", n)


else:  # macOS / Linux: stubs, no profiles surfaced today.
    APP_PROFILES: list[App] = []

    def detect_apps() -> list[App]:
        return []

    def activate(app_id: str, timeout_s: float = 1.0) -> bool:
        logger.warning("Agent Input Mode app activation isn't implemented "
                       "on %s yet.", sys.platform)
        return False

    def activate_target(app_id: str, *, timeout_s: float = 1.0) -> int:
        logger.warning("Agent Input Mode app activation isn't implemented "
                       "on %s yet.", sys.platform)
        return 0

    def send_enter() -> None:
        # The right primitive on macOS is `osascript` "key code 36"; on
        # Linux X11 it's `xdotool key Return`. Wire these in
        # dictado.platform when a non-Windows user wants AIM.
        logger.warning("send_enter() is a no-op on %s for now.", sys.platform)


# --- Public helpers used by the daemon --------------------------------------
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
