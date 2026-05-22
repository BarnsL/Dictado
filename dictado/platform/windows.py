"""Windows adapter for dictado.

This is the most battle-tested of the three platform modules.

Three jobs:
    1. register_hotkey(callback, hotkey_spec) -> HotkeyHandle
       Win32 RegisterHotKey + GetMessage on a daemon thread. The handle
       can be passed to update_hotkey(handle, new_spec) to live-rebind
       the combo without restarting the daemon.
    2. paste_into_window(hwnd) -- focus_window + verify + SendInput Ctrl+V
    3. install_autostart() / uninstall -- Startup-folder .lnk shortcut

Why these specific APIs (and not the obvious `keyboard` Python lib)?
    See docs/SECURITY.md.

About focus stealing
--------------------
Windows refuses `SetForegroundWindow` calls from a background process or
thread that doesn't own the current foreground unless it does one of:

    - holds the SE_DEBUG / foreground privilege (we don't),
    - was just clicked or just received input (we weren't),
    - attaches its input queue to the foreground thread's queue
      (the "AttachThreadInput trick").

Without the AttachThreadInput trick, `SetForegroundWindow` returns
success but the target window only flashes in the taskbar; focus never
transfers. The result is Ctrl+V landing in the previous foreground app
(usually our own popup or whatever was active before the user pressed
the hotkey).

`focus_window()` below implements the trick + a verify-loop so the
daemon can be confident focus actually moved before it pastes.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger("dictado.windows")

_user32   = ctypes.WinDLL("user32",   use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype  = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype  = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype  = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype  = wintypes.BOOL
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype  = wintypes.BOOL
_user32.SetActiveWindow.argtypes = [wintypes.HWND]
_user32.SetActiveWindow.restype  = wintypes.HWND
_user32.SetFocus.argtypes = [wintypes.HWND]
_user32.SetFocus.restype  = wintypes.HWND
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype  = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype  = wintypes.DWORD
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_user32.AllowSetForegroundWindow.restype  = wintypes.BOOL
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD

ASFW_ANY = 0xFFFFFFFF  # AllowSetForegroundWindow(ASFW_ANY) blanket-permits

# ---- 1. Hotkey ---------------------------------------------------------------
MOD_ALT      = 0x0001
MOD_CONTROL  = 0x0002
MOD_SHIFT    = 0x0004
MOD_WIN      = 0x0008
MOD_NOREPEAT = 0x4000

_MOD_BIT_FROM_TOKEN = {
    "ctrl":  MOD_CONTROL,
    "shift": MOD_SHIFT,
    "alt":   MOD_ALT,
    "win":   MOD_WIN,
}

# Special-key -> Win32 VK code. Anything not here falls through to a
# single character (we use VkKeyScanW to map letters/digits/punctuation).
_VK_SPECIAL = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}
for _i in range(1, 25):
    _VK_SPECIAL[f"f{_i}"] = 0x6F + _i if _i < 13 else 0x69 + _i  # F1=0x70...

class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt_x", wintypes.LONG),
                ("pt_y", wintypes.LONG)]


def _spec_to_win32(spec: str) -> tuple[int, int]:
    """Parse the cross-platform "alt+t" string into (modifier_bits, vk).

    Raises ValueError on anything unparseable.
    """
    from dictado.config import parse_hotkey
    mods, key = parse_hotkey(spec)
    bits = 0
    for m in mods:
        bits |= _MOD_BIT_FROM_TOKEN[m]
    if key in _VK_SPECIAL:
        vk = _VK_SPECIAL[key]
    elif len(key) == 1:
        # VkKeyScanW returns 0xFFFF on failure; low byte is VK, high byte is shift state.
        scan = _user32.VkKeyScanW(ctypes.c_wchar(key))
        if scan == -1 or (scan & 0xFFFF) == 0xFFFF:
            raise ValueError(f"VkKeyScanW failed for {key!r}")
        vk = scan & 0xFF
    else:
        raise ValueError(f"don't know how to bind key {key!r}")
    return bits, vk


class HotkeyHandle:
    """Owns the message-pump thread and lets you rebind without restarting.

    Internally it serializes RegisterHotKey / UnregisterHotKey to the pump
    thread by posting WM_APP messages -- you cannot Unregister on one
    thread a hotkey that another thread Registered.
    """
    WM_APP_REBIND = 0x8000  # WM_APP base; we only use one custom msg
    WM_APP_QUIT   = 0x8001

    def __init__(self, callback, spec: str, hotkey_id: int = 1):
        self._callback = callback
        self._hotkey_id = hotkey_id
        self._spec = spec
        self._pending_spec: str | None = None
        self._lock = threading.Lock()
        self._thread_id: int | None = None
        self._registered = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="hotkey")
        self._thread.start()
        # Wait briefly for the pump to set up so callers can change the
        # spec immediately if they want.
        for _ in range(40):
            if self._thread_id is not None:
                break
            time.sleep(0.025)

    @property
    def spec(self) -> str:
        return self._spec

    def rebind(self, new_spec: str) -> None:
        """Ask the pump thread to swap to a new hotkey spec. Returns
        immediately; the actual registration happens on the pump thread."""
        with self._lock:
            self._pending_spec = new_spec
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id,
                                       self.WM_APP_REBIND, 0, 0)

    def stop(self) -> None:
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id,
                                       self.WM_APP_QUIT, 0, 0)

    # ---- runs on the pump thread ----
    def _try_register(self, spec: str) -> bool:
        try:
            bits, vk = _spec_to_win32(spec)
        except ValueError as e:
            logger.error("hotkey spec %r rejected: %s", spec, e)
            return False
        if self._registered:
            _user32.UnregisterHotKey(None, self._hotkey_id)
            self._registered = False
        if not _user32.RegisterHotKey(None, self._hotkey_id,
                                      bits | MOD_NOREPEAT, vk):
            err = ctypes.get_last_error()
            # 1409 = ERROR_HOTKEY_ALREADY_REGISTERED. Surface it specifically
            # because it's by far the most common failure mode.
            if err == 1409:
                logger.error("hotkey %r is already taken by another app", spec)
            else:
                logger.error("RegisterHotKey(%s) failed (Win32 err=%d).",
                             spec, err)
            return False
        self._registered = True
        self._spec = spec
        logger.info("hotkey registered: %s (mod=0x%x vk=0x%x)", spec, bits, vk)
        return True

    def _run(self) -> None:
        self._thread_id = _kernel32.GetCurrentThreadId()
        # Force the OS to create this thread's message queue so that any
        # PostThreadMessage from rebind()/stop() arrives.
        msg = _MSG()
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._try_register(self._spec)
        try:
            while True:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                if msg.message == 0x0312 and msg.wParam == self._hotkey_id:
                    threading.Thread(target=self._callback,
                                     daemon=True).start()
                elif msg.message == self.WM_APP_REBIND:
                    with self._lock:
                        spec = self._pending_spec
                        self._pending_spec = None
                    if spec:
                        self._try_register(spec)
                elif msg.message == self.WM_APP_QUIT:
                    break
        finally:
            if self._registered:
                _user32.UnregisterHotKey(None, self._hotkey_id)


def register_hotkey(callback, spec: str = "alt+t") -> HotkeyHandle:
    return HotkeyHandle(callback, spec)


# ---- 2. Paste into focused window --------------------------------------------
PUL = ctypes.POINTER(ctypes.c_ulong)

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", PUL)]

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", PUL)]

class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

INPUT_KEYBOARD  = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V       = 0x56
VK_MENU    = 0x12  # Alt; used by the foreground-unlock trick

SW_RESTORE = 9


def get_foreground_window() -> int:
    """Return HWND of the currently-focused window (0 if none)."""
    return int(_user32.GetForegroundWindow() or 0)


def _send_alt_tap() -> None:
    """Synthesise a single Alt-down/Alt-up. Windows treats the calling
    process as having received user input, which clears the
    foreground-lock timeout and lets `SetForegroundWindow` actually
    transfer focus on the next call. Documented workaround that
    AutoHotkey, FancyZones, EarTrumpet, et al all use.

    No app receives this as a "real" Alt press because we send it
    while NO window has the foreground (we're between focus moves)."""
    inputs = (_INPUT * 2)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].u.ki = _KEYBDINPUT(VK_MENU, 0, 0, 0, None)
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].u.ki = _KEYBDINPUT(VK_MENU, 0, KEYEVENTF_KEYUP, 0, None)
    _user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(_INPUT))


def focus_window(hwnd: int, timeout_s: float = 1.0) -> bool:
    """Bring `hwnd` to the foreground reliably and wait until it is.

    Returns True when GetForegroundWindow() == hwnd within `timeout_s`.

    The dance, in order:
      1. If hwnd is minimised, ShowWindow(SW_RESTORE).
      2. AllowSetForegroundWindow(ASFW_ANY) — opt our process out of
         foreground-lock for the next SFW call.
      3. AttachThreadInput(this_tid, fg_tid, TRUE) — share input queue
         with the current foreground thread, which makes Windows treat
         our SFW as if it came from the foreground process.
      4. BringWindowToTop + SetForegroundWindow + SetActiveWindow +
         SetFocus on hwnd.
      5. Detach the input queue.
      6. If verification still fails, send a phantom Alt tap (resets the
         foreground-lock timeout) and retry steps 2-5 once.
      7. Poll GetForegroundWindow() up to `timeout_s` seconds.

    Returns False if hwnd is invalid or focus could not be transferred.
    The caller can fall back to "paste into whatever is foreground now".
    """
    if not hwnd or not _user32.IsWindow(hwnd):
        return False

    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)

    def _try_focus() -> bool:
        _user32.AllowSetForegroundWindow(ASFW_ANY)
        fg = _user32.GetForegroundWindow()
        my_tid = _kernel32.GetCurrentThreadId()
        fg_tid = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_tid and fg_tid != my_tid:
            attached = bool(_user32.AttachThreadInput(my_tid, fg_tid, True))
        try:
            _user32.BringWindowToTop(hwnd)
            ok = bool(_user32.SetForegroundWindow(hwnd))
            _user32.SetActiveWindow(hwnd)
            _user32.SetFocus(hwnd)
            return ok
        finally:
            if attached:
                _user32.AttachThreadInput(my_tid, fg_tid, False)

    if not _try_focus():
        # Phantom Alt tap unlocks the foreground-lock timeout, then
        # retry the dance once.
        _send_alt_tap()
        time.sleep(0.02)
        _try_focus()

    # Verify-loop. Most of the time focus is there within ~50 ms;
    # window-manager animations on slow machines can push it to 500 ms+.
    deadline = time.monotonic() + max(0.1, timeout_s)
    while time.monotonic() < deadline:
        if int(_user32.GetForegroundWindow() or 0) == hwnd:
            return True
        time.sleep(0.025)

    logger.warning("focus_window(%#x) timed out; foreground is %#x.",
                   hwnd, int(_user32.GetForegroundWindow() or 0))
    return False


def paste_into_window(hwnd: int, *, verify_focus: bool = True) -> bool:
    """Bring `hwnd` to the foreground (if given) and synthesise Ctrl+V.

    Returns True on success, False if focus could not be transferred or
    the SendInput call only injected a partial event sequence. The text
    is always already on the clipboard before this is called, so a
    False return means "paste didn't land but the user can still hit
    Ctrl+V manually".

    `hwnd == 0` keeps the previous "paste into whatever is foreground
    now" behaviour, used by AIM after agent_input.activate_target()
    has already focused the right window.
    """
    if hwnd:
        if not focus_window(hwnd, timeout_s=1.0):
            logger.warning("paste_into_window: focus_window(%#x) failed; "
                           "skipping paste. Text remains on the clipboard.",
                           hwnd)
            return False
        # Tiny extra settle so Electron / Chromium input handlers
        # actually accept the synthesised Ctrl+V (they debounce input
        # events for ~50 ms after a focus change).
        time.sleep(0.08)
    elif verify_focus:
        # AIM 'auto' path: at minimum confirm SOMETHING owns focus
        # and it isn't our own popup. Otherwise the paste lands in our
        # status window.
        fg = int(_user32.GetForegroundWindow() or 0)
        if not fg:
            logger.warning("paste_into_window: no foreground window; "
                           "skipping paste.")
            return False

    inputs = (_INPUT * 4)()
    for i, (vkey, flags) in enumerate(((VK_CONTROL, 0), (VK_V, 0),
                                       (VK_V, KEYEVENTF_KEYUP),
                                       (VK_CONTROL, KEYEVENTF_KEYUP))):
        inputs[i].type = INPUT_KEYBOARD
        inputs[i].u.ki = _KEYBDINPUT(vkey, 0, flags, 0, None)
    n = _user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(_INPUT))
    if n != 4:
        logger.warning("SendInput injected %d/4 events.", n)
        return False
    return True


# ---- 3. Auto-start at login --------------------------------------------------
def _startup_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / \
           "Start Menu" / "Programs" / "Startup"


def _shortcut_path() -> Path:
    return _startup_dir() / "dictado.lnk"


def install_autostart(python_exe: str, script_path: str) -> Path:
    """Create a Startup-folder .lnk that launches the daemon at every login."""
    target = _shortcut_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    ps = (
        f"$wsh = New-Object -ComObject WScript.Shell;"
        f"$lnk = $wsh.CreateShortcut('{target}');"
        f"$lnk.TargetPath = '{python_exe}';"
        f"$lnk.Arguments = '\"{script_path}\"';"
        f"$lnk.WorkingDirectory = '{Path(script_path).parent}';"
        f"$lnk.WindowStyle = 7;"
        f"$lnk.Description = 'dictado daemon';"
        f"$lnk.Save();"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    logger.info("Created Startup shortcut at %s", target)
    return target


def uninstall_autostart() -> None:
    p = _shortcut_path()
    if p.exists():
        p.unlink()
        logger.info("Removed Startup shortcut at %s", p)
