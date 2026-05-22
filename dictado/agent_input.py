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
    # Optional launch hints: ordered tuple of paths (or path templates
    # using $LOCALAPPDATA / $PROGRAMFILES / $PROGRAMFILES(X86) / $APPDATA)
    # to try when locate() returns 0 because the app isn't running.
    # First existing entry wins; subprocess.Popen launches it detached
    # and activate_target then polls locate() until the window appears.
    # Empty tuple means "we don't know how to launch this app".
    launch_paths: tuple[str, ...] = ()
    # Optional regex (case-insensitive) the chat-input element's
    # UIA Name property must match. Disambiguates from buttons,
    # search boxes, suggested-action chips that share the bottom
    # of the window. Examples:
    #   amazon-quick:  r"^Ask a question"
    #   chatgpt:       r"^Message ChatGPT"
    #   claude:        r"^Reply to "
    # When empty/None, the picker falls back to area + position
    # heuristics only (legacy behaviour for profiles without
    # known input names).
    input_name_regex: str = ""


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


    # --- UIA-based prompt-input focusing -------------------------------
    #
    # Ctrl+L works for ChatGPT desktop / Claude desktop / Cursor but
    # does NOT reliably work for every Electron AI app (Amazon Quick
    # is the known counter-example). The proper fix is UI Automation:
    # walk the target window's accessibility tree, find the prompt
    # input by ControlType=Edit + IsKeyboardFocusable + the bottom-of-
    # window heuristic, then call IUIAutomationElement.SetFocus().
    #
    # We share the target HWND between activate_target() and the
    # post_activate hook via a threading.local. The hook runs on the
    # same thread that called activate_target (the daemon's
    # stop_recording thread), so the local is the right shape.

    import threading as _threading
    _aim_local = _threading.local()
    # _aim_local also carries `just_launched: bool`. activate_target
    # sets it True when launch_target had to spawn the app (cold start
    # path); the post_activate hook reads it to decide between waiting
    # for the chat input to appear (cold) or proceeding immediately
    # (warm).


    def _focus_input_via_uia() -> None:
        """post_activate hook: use UIA to focus the chat input under
        the HWND set by activate_target. Falls back to _send_ctrl_l
        if UIA can't find the input -- some apps still respond to the
        chord even when their accessibility tree doesn't expose the
        input cleanly.

        If activate_target signalled `just_launched=True` via the
        thread-local (cold launch path), we first poll the UIA tree
        until the chat input element actually appears in it, up to
        12 s. Without this wait, the Chromium WebContents is still
        rendering the splash / handoff screen and the synthesized
        Ctrl+V we're about to fire lands on whatever placeholder
        element happens to be focused."""
        hwnd = getattr(_aim_local, "hwnd", 0)
        just_launched = getattr(_aim_local, "just_launched", False)
        if not hwnd:
            logger.debug("_focus_input_via_uia: no hwnd in thread-local; "
                         "skipping.")
            return
        try:
            from dictado.platform.uia import focus_chat_input, wait_for_chat_input
        except Exception:
            logger.exception("UIA helper unavailable; falling back to "
                             "Ctrl+L.")
            _send_ctrl_l()
            return
        # If the active profile supplies an input_name_regex, compile
        # it once. Used by both the cold-launch wait and the warm-path
        # focus_chat_input call to disambiguate the chat input from
        # other focusable Edits / Documents in the tree (suggested-
        # action buttons, search boxes, etc.).
        active_app = getattr(_aim_local, "active_app", None)
        compiled_rx = None
        if active_app is not None and active_app.input_name_regex:
            try:
                import re as _re
                compiled_rx = _re.compile(active_app.input_name_regex,
                                          _re.IGNORECASE)
            except Exception:
                logger.exception("Bad input_name_regex %r for %r; ignoring.",
                                 active_app.input_name_regex, active_app.id)
                compiled_rx = None

        if just_launched:
            # Cold launch: Chromium hasn't rendered the chat input yet;
            # poll the a11y tree until it does. 12 s deadline matches
            # the worst case observed for cold-start to interactive.
            # set_focus=True does the SetFocus() atomically with the
            # discovery so Chromium can't reshuffle inner focus between
            # "I found the input" and "I focused the input".
            ready = wait_for_chat_input(hwnd, deadline_s=30.0,
                                        poll_s=0.30,
                                        name_regex=compiled_rx,
                                        set_focus=True)
            if ready:
                # SetFocus already happened inside wait_for_chat_input.
                return
            logger.info("Cold-launch wait timed out; trying focus_"
                        "chat_input anyway then falling back to "
                        "Ctrl+L.")
        try:
            ok = focus_chat_input(hwnd, timeout_s=0.6, name_regex=compiled_rx)
        except Exception:
            logger.exception("focus_chat_input raised on hwnd 0x%08X; "
                             "falling back to Ctrl+L.", hwnd)
            _send_ctrl_l()
            return
        if not ok:
            logger.info("UIA could not focus a chat input under hwnd "
                        "0x%08X; falling back to Ctrl+L.", hwnd)
            _send_ctrl_l()


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
    #
    # _focus_input_via_uia is the production focus-the-chat-input hook;
    # it tries UIA's IUIAutomationElement.SetFocus() first and falls
    # back to a Ctrl+L chord if UIA can't find a plausible Edit. Wire
    # it for every Electron AI app that has WebContents-focus issues.
    # Each row: (id, label, _profile_pair, post_activate, launch_paths)
    # launch_paths can be ()  to mean "don't auto-launch this app".
    _PROFILES_RAW: list[tuple] = [
        # AI assistants ------------------------------------------------
        ("chatgpt",      "ChatGPT (desktop)",  _profile_by_image("ChatGPT.exe"),         _focus_input_via_uia,
         (r"$LOCALAPPDATA\Programs\ChatGPT\ChatGPT.exe",),
         r"^Message"),
        ("claude",       "Claude (desktop)",   _profile_by_image("Claude.exe"),          _focus_input_via_uia,
         (r"$LOCALAPPDATA\AnthropicClaude\Claude.exe",
          r"$LOCALAPPDATA\Programs\Claude\Claude.exe"),
         r"^(Reply to |Talk with |How can I help)"),
        ("copilot",      "Microsoft Copilot",  _profile_by_image("Copilot.exe", "ai.exe"), _focus_input_via_uia, (),
         r"^(Message|Ask)"),
        ("amazon-quick", "Amazon Quick",       _profile_by_image("Amazon Quick.exe"),    _focus_input_via_uia,
         (r"$PROGRAMFILES\Amazon Quick\Amazon Quick.exe",
          r"$LOCALAPPDATA\Programs\Amazon Quick\Amazon Quick.exe",
          r"$APPDATA\Microsoft\Windows\Start Menu\Programs\Amazon Quick.lnk"),
         r"^Ask"),
        # IDEs / editors ----------------------------------------------
        ("cursor",       "Cursor",             _profile_by_image("Cursor.exe"),          _focus_input_via_uia,
         (r"$LOCALAPPDATA\Programs\Cursor\Cursor.exe",),
         r""),
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

    # Build the App list. _PROFILES_RAW rows can be 3, 4, 5, or 6 long:
    #   (id, label, profile_triple)
    #   (id, label, profile_triple, post_activate)
    #   (id, label, profile_triple, post_activate, launch_paths)
    #   (id, label, profile_triple, post_activate, launch_paths, input_name_regex)
    APP_PROFILES: list[App] = []
    for _row in _PROFILES_RAW:
        _pid    = _row[0]
        _label  = _row[1]
        _d, _a, _l = _row[2]
        _post   = _row[3] if len(_row) > 3 else None
        _launch = _row[4] if len(_row) > 4 else ()
        _name_rx = _row[5] if len(_row) > 5 else ""
        APP_PROFILES.append(App(
            id=_pid,
            label=_label,
            detect=_d,
            activate=_a,
            locate=_l,
            post_activate=_post,
            launch_paths=_launch,
            input_name_regex=_name_rx,
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



    def _expand(path_template: str) -> str:
        """Expand $LOCALAPPDATA / $PROGRAMFILES / $PROGRAMFILES(X86) /
        $APPDATA placeholders inside a launch_paths entry."""
        from os import environ
        out = path_template
        # Order matters: longer keys first so $PROGRAMFILES doesn't
        # eat the "(X86)" half of $PROGRAMFILES(X86).
        for key in ("PROGRAMFILES(X86)", "LOCALAPPDATA", "PROGRAMFILES",
                    "APPDATA", "PROGRAMDATA", "USERPROFILE"):
            out = out.replace(f"${key}", environ.get(key, ""))
        return out


    def launch_target(app_id: str, *, wait_s: float = 8.0) -> int:
        """Try to launch the named app if it's not already running, then
        return its window HWND (0 on failure).

        - First call locate() to see if the app is already running. If
          so, return that HWND immediately.
        - Otherwise iterate through the profile's launch_paths in order;
          for each entry that resolves to an existing file, spawn it
          detached via subprocess.Popen (with .lnk handled via shell
          start). Then poll locate() at 200 ms intervals up to wait_s.
        """
        import subprocess
        app = _profile(app_id)
        if app is None:
            return 0
        try:
            hwnd = app.locate()
        except Exception:
            hwnd = 0
        if hwnd:
            return hwnd
        if not app.launch_paths:
            logger.info("launch_target(%r): no launch_paths configured.",
                        app_id)
            return 0
        spawned = False
        for raw in app.launch_paths:
            candidate = _expand(raw)
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                if candidate.lower().endswith(".lnk"):
                    # .lnk needs the shell to resolve it; os.startfile
                    # is the standard, non-blocking way on Windows.
                    os.startfile(candidate)  # noqa: S606
                else:
                    subprocess.Popen(
                        [candidate],
                        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                        close_fds=True,
                    )
                logger.info("launch_target(%r): spawned %s", app_id, candidate)
                spawned = True
                break
            except Exception:
                logger.exception(
                    "launch_target(%r): spawn of %s raised; trying next.",
                    app_id, candidate)
        if not spawned:
            logger.info("launch_target(%r): no launch_paths resolved to an "
                        "existing file.", app_id)
            return 0
        # Poll locate() until the window shows up or we time out.
        deadline = time.monotonic() + max(0.5, wait_s)
        poll = 0.20
        while time.monotonic() < deadline:
            try:
                hwnd = app.locate()
            except Exception:
                hwnd = 0
            if hwnd:
                logger.info("launch_target(%r): window appeared in %.1fs",
                            app_id, wait_s - (deadline - time.monotonic()))
                return hwnd
            time.sleep(poll)
        logger.warning("launch_target(%r): spawned the app but no window "
                       "appeared within %.1fs.", app_id, wait_s)
        return 0


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
        just_launched = False
        if not hwnd:
            # Try to launch the app, then re-locate.
            if app.launch_paths:
                logger.info("activate_target(%r): no live window; "
                            "attempting auto-launch.", app_id)
                hwnd = launch_target(app_id)
                if hwnd:
                    just_launched = True
            if not hwnd:
                logger.info("activate_target(%r): no live window found "
                            "and auto-launch failed or unavailable.", app_id)
                return 0
        try:
            from dictado.platform.windows import focus_window
        except Exception:
            ok = bool(app.activate())
            if ok:
                time.sleep(min(0.25, max(0.05, timeout_s)))
                if app.post_activate is not None:
                    _aim_local.hwnd = hwnd
                    try:
                        app.post_activate()
                    except Exception:
                        logger.exception(
                            "post_activate() raised for %r.", app_id)
                    finally:
                        _aim_local.hwnd = 0
                    time.sleep(0.12)
                return hwnd
            return 0
        if focus_window(hwnd, timeout_s=timeout_s):
            if app.post_activate is not None:
                # Share the target HWND + cold-launch flag with the
                # hook via thread-local so the UIA path knows which
                # window to walk and whether to wait for Chromium to
                # render the chat input.
                _aim_local.hwnd = hwnd
                _aim_local.just_launched = just_launched
                _aim_local.active_app = app
                try:
                    app.post_activate()
                except Exception:
                    logger.exception(
                        "post_activate() raised for %r.", app_id)
                finally:
                    _aim_local.hwnd = 0
                    _aim_local.just_launched = False
                    _aim_local.active_app = None
                # If the cold-launch path waited several seconds for
                # the UIA tree to populate, foreground may have
                # drifted (splash screen handoff often resets it).
                # Re-assert focus so paste_into_window's
                # already_focused check still passes.
                if just_launched:
                    focus_window(hwnd, timeout_s=0.5)
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


    def _expand(path_template: str) -> str:
        """Expand $LOCALAPPDATA / $PROGRAMFILES / $PROGRAMFILES(X86) /
        $APPDATA placeholders inside a launch_paths entry."""
        from os import environ
        out = path_template
        # Order matters: longer keys first so $PROGRAMFILES doesn't
        # eat the "(X86)" half of $PROGRAMFILES(X86).
        for key in ("PROGRAMFILES(X86)", "LOCALAPPDATA", "PROGRAMFILES",
                    "APPDATA", "PROGRAMDATA", "USERPROFILE"):
            out = out.replace(f"${key}", environ.get(key, ""))
        return out


    def launch_target(app_id: str, *, wait_s: float = 8.0) -> int:
        """Try to launch the named app if it's not already running, then
        return its window HWND (0 on failure).

        - First call locate() to see if the app is already running. If
          so, return that HWND immediately.
        - Otherwise iterate through the profile's launch_paths in order;
          for each entry that resolves to an existing file, spawn it
          detached via subprocess.Popen (with .lnk handled via shell
          start). Then poll locate() at 200 ms intervals up to wait_s.
        """
        import subprocess
        app = _profile(app_id)
        if app is None:
            return 0
        try:
            hwnd = app.locate()
        except Exception:
            hwnd = 0
        if hwnd:
            return hwnd
        if not app.launch_paths:
            logger.info("launch_target(%r): no launch_paths configured.",
                        app_id)
            return 0
        spawned = False
        for raw in app.launch_paths:
            candidate = _expand(raw)
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                if candidate.lower().endswith(".lnk"):
                    # .lnk needs the shell to resolve it; os.startfile
                    # is the standard, non-blocking way on Windows.
                    os.startfile(candidate)  # noqa: S606
                else:
                    subprocess.Popen(
                        [candidate],
                        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                        close_fds=True,
                    )
                logger.info("launch_target(%r): spawned %s", app_id, candidate)
                spawned = True
                break
            except Exception:
                logger.exception(
                    "launch_target(%r): spawn of %s raised; trying next.",
                    app_id, candidate)
        if not spawned:
            logger.info("launch_target(%r): no launch_paths resolved to an "
                        "existing file.", app_id)
            return 0
        # Poll locate() until the window shows up or we time out.
        deadline = time.monotonic() + max(0.5, wait_s)
        poll = 0.20
        while time.monotonic() < deadline:
            try:
                hwnd = app.locate()
            except Exception:
                hwnd = 0
            if hwnd:
                logger.info("launch_target(%r): window appeared in %.1fs",
                            app_id, wait_s - (deadline - time.monotonic()))
                return hwnd
            time.sleep(poll)
        logger.warning("launch_target(%r): spawned the app but no window "
                       "appeared within %.1fs.", app_id, wait_s)
        return 0


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
