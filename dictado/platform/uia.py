"""dictado.platform.uia -- UI Automation helpers for Agent Input Mode.

Why this module exists
----------------------
SetForegroundWindow brings an Electron / Chromium window to the
foreground at the OS level, but the inner WebContents focus stays on
whatever sub-control was last interacted with. The synthesized Ctrl+V
chord then lands on a button or sidebar item instead of the chat
input, the paste silently no-ops, and the user sees the window come
forward with no text inserted.

The fix is to move WebContents focus to the chat input BEFORE pumping
Ctrl+V. Windows UI Automation -- the same accessibility API Narrator
uses -- can do that: walk the target window's accessibility tree,
find the prompt input by ControlType=Edit and a few heuristics, then
call IUIAutomationElement.SetFocus(). This threads through Chromium's
accessibility integration and reliably moves the inner focus.

Public API
----------
    focus_chat_input(hwnd, *, timeout_s=1.0) -> bool
        Bring the chat input under HWND to keyboard focus. Returns
        True on success, False if no plausible input was found or
        SetFocus failed within timeout_s.

    list_edits(hwnd) -> list[dict]
        Diagnostic. Return every Edit / Document descendant under
        HWND with its UIA properties. Used by the uia_probe.py
        debugging script.

Heuristics for picking the right Edit element
---------------------------------------------
1. Filter to elements with ControlType=Edit (50004) or sometimes
   ControlType=Document (50030, used by some web apps for
   contenteditable areas).
2. Require IsKeyboardFocusable=True and IsEnabled=True.
3. Prefer the element whose center-y is in the bottom 40% of the
   parent window's client rect (chat inputs always live near the
   bottom).
4. Among ties, prefer the largest by area (avoids tiny inline
   search boxes).
5. As a last resort, accept any focusable Edit/Document.

The heuristics survive Amazon Quick, ChatGPT desktop, Claude desktop,
Cursor, Slack, and Teams in spot checks. We document each step so
adding a new app rarely needs a per-profile override.

Threading
---------
UIA calls work fine off the main thread; Chromium's accessibility
provider is thread-safe. We do NOT spin a separate thread here -- the
caller (agent_input.activate_target's post_activate hook) already
runs in the daemon's stop_recording thread, which is the right
context.

Performance
-----------
A typical window has ~200 elements; walking via the RawViewWalker is
fast (~30-100 ms on this hardware). We cap the walk at depth 12 and
~600 nodes total to bound worst-case latency.

Endpoint-protection notes
-------------------------
UI Automation is a documented Microsoft accessibility API. Falcon /
Defender-for-Endpoint do not flag UIA traffic as suspicious; it's
how every screen-reader and accessibility tool works. We're using
read-only UIA queries plus one SetFocus call -- no input injection,
no DLL injection, no global hooks. See docs/SECURITY.md for the
endpoint-protection mapping.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass

logger = logging.getLogger("dictado.platform.uia")


@dataclass(frozen=True)
class _UiaEdit:
    """One Edit/Document candidate from the UIA tree."""
    element: object       # IUIAutomationElement
    name: str
    automation_id: str
    rect: tuple[float, float, float, float]  # left, top, right, bottom
    keyboard_focusable: bool
    enabled: bool
    control_type: int

    @property
    def width(self) -> float:
        return max(0.0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> float:
        return max(0.0, self.rect[3] - self.rect[1])

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center_y(self) -> float:
        return (self.rect[1] + self.rect[3]) / 2.0


# UIA property IDs, hard-coded so we don't have to import the COM
# typelib just to read them.
UIA_ControlTypePropertyId        = 30003
UIA_NamePropertyId               = 30005
UIA_AutomationIdPropertyId       = 30011
UIA_BoundingRectanglePropertyId  = 30001
UIA_IsKeyboardFocusablePropertyId = 30009
UIA_IsEnabledPropertyId          = 30010

UIA_CTRL_EDIT     = 50004
UIA_CTRL_DOCUMENT = 50030

# CLSID for CUIAutomation8 (Windows 8+). Supports the full IUIAutomation
# interface including IUIAutomation2/3 features.
CUIAutomation8_CLSID = "{E22AD333-B25F-460C-83D0-0581107395C9}"

_MAX_DEPTH        = 12
_MAX_NODES        = 600
_BOTTOM_FRACTION  = 0.40   # only consider Edits whose center-y is in
                           # the bottom 40% of the window
_FOCUS_VERIFY_INTERVAL_S = 0.025
_DEFAULT_TIMEOUT_S = 1.0


# Lazy module-level singletons -- the COM client is expensive to
# construct and there's no reason to do so on every call.
_uia_client = None
_uia_module = None


def _ensure_uia():
    """Construct the IUIAutomation singleton on first use."""
    global _uia_client, _uia_module
    if _uia_client is not None:
        return _uia_client, _uia_module

    if sys.platform != "win32":
        raise RuntimeError("UIA is Windows-only.")

    import comtypes
    import comtypes.client

    _uia_module = comtypes.client.GetModule("UIAutomationCore.dll")
    _uia_client = comtypes.client.CreateObject(
        CUIAutomation8_CLSID,
        interface=_uia_module.IUIAutomation,
    )
    return _uia_client, _uia_module


def _get_property_safely(element, prop_id: int):
    """Wrap GetCurrentPropertyValue; UIA throws on transient nodes."""
    try:
        return element.GetCurrentPropertyValue(prop_id)
    except Exception:
        return None


def _walk_for_edits(uia, walker, root, edits, depth=0, node_budget=None):
    """Append every Edit/Document descendant of root to `edits`."""
    if depth > _MAX_DEPTH:
        return
    if node_budget is None:
        node_budget = [_MAX_NODES]
    if node_budget[0] <= 0:
        return
    node_budget[0] -= 1

    ctype = _get_property_safely(root, UIA_ControlTypePropertyId)
    if ctype in (UIA_CTRL_EDIT, UIA_CTRL_DOCUMENT):
        rect = _get_property_safely(root, UIA_BoundingRectanglePropertyId)
        if rect and len(rect) == 4:
            edits.append(_UiaEdit(
                element=root,
                name=_get_property_safely(root, UIA_NamePropertyId) or "",
                automation_id=_get_property_safely(
                    root, UIA_AutomationIdPropertyId) or "",
                rect=tuple(float(v) for v in rect),
                keyboard_focusable=bool(_get_property_safely(
                    root, UIA_IsKeyboardFocusablePropertyId)),
                enabled=bool(_get_property_safely(
                    root, UIA_IsEnabledPropertyId)),
                control_type=int(ctype),
            ))

    try:
        child = walker.GetFirstChildElement(root)
    except Exception:
        return
    while child:
        _walk_for_edits(uia, walker, child, edits, depth + 1, node_budget)
        try:
            child = walker.GetNextSiblingElement(child)
        except Exception:
            return


def _window_client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Return the window's screen-space client rect (left, top, right, bottom).

    Used for the bottom-fraction heuristic.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    rc = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return None
    return (rc.left, rc.top, rc.right, rc.bottom)


def _abs_area(e: _UiaEdit) -> float:
    """Robust area: use |width| * |height| so degenerate / inverted
    rects (Chromium occasionally caches stale layouts that flip the
    rect's bottom above its top) don't read as zero-area."""
    w = abs(e.rect[2] - e.rect[0])
    h = abs(e.rect[3] - e.rect[1])
    return w * h


def _pick_chat_input(edits: list[_UiaEdit],
                     window_rect: tuple[int, int, int, int] | None
                     ) -> _UiaEdit | None:
    """Pick the Edit/Document most likely to be the chat input.

    Strategy:
      A. If there is exactly ONE focusable+enabled Edit
         (ControlType=Edit, IsKeyboardFocusable=True, IsEnabled=True),
         pick it directly. Don't consult the rect at all -- Chromium
         occasionally returns stale / inverted bounding rects for live
         input elements (we observed Amazon Quick reporting an Edit
         whose bottom edge was above its top), and a unique Edit is
         already a strong-enough signal that this is the chat input.
      B. If multiple Edits, score them with rect heuristics:
            - reject any Edit larger than 30% of the window area;
            - prefer Edits whose center-y is in the bottom fraction
              of the window;
            - among ties, prefer the smallest by absolute-value area.
      C. If zero Edits, repeat the same scoring against Documents
         (some web apps expose contenteditable as Document instead
         of Edit; rarer, but worth trying).
    """
    plausible = [
        e for e in edits
        if e.keyboard_focusable and e.enabled
    ]
    if not plausible:
        return None

    # ---- A. Unique focusable Edit -> pick it without rect checks. -----
    edits_only = [e for e in plausible if e.control_type == UIA_CTRL_EDIT]
    if len(edits_only) == 1:
        logger.debug("_pick_chat_input: exactly one focusable Edit; "
                     "picking it without rect heuristics.")
        return edits_only[0]

    # ---- B. Multiple Edits -> rect-based scoring among Edits. --------
    # ---- C. Zero Edits     -> rect-based scoring among Documents. ----
    pool = edits_only if edits_only else plausible

    # Compute window area (for the relative-size cap) up front.
    win_area = None
    if window_rect is not None:
        w_l, w_t, w_r, w_b = window_rect
        win_area = max(1.0, abs(w_r - w_l) * abs(w_b - w_t))

    MAX_RELATIVE_AREA = 0.30
    MAX_ABSOLUTE_AREA = 300_000

    def _passes_size(e):
        a = _abs_area(e)
        if win_area is not None:
            return a == 0 or (a / win_area) <= MAX_RELATIVE_AREA
        return a == 0 or a <= MAX_ABSOLUTE_AREA

    sized = [e for e in pool if _passes_size(e)]
    if sized:
        pool = sized
    if not pool:
        return None

    # Bottom-fraction filter, but only when at least one candidate has
    # a sane (non-degenerate) rect that puts it in the bottom slab.
    if window_rect is not None:
        w_l, w_t, w_r, w_b = window_rect
        w_h = max(1, abs(w_b - w_t))
        threshold = w_t + w_h * (1 - _BOTTOM_FRACTION)
        bottom_pool = [
            e for e in pool
            if min(e.rect[1], e.rect[3]) >= threshold
            or max(e.rect[1], e.rect[3]) >= threshold
        ]
        if bottom_pool:
            pool = bottom_pool

    # Final tie-breaker: smallest absolute-value area.
    pool.sort(key=_abs_area)
    return pool[0]


def list_edits(hwnd: int) -> list[dict]:
    """Diagnostic helper: dump every Edit/Document under HWND."""
    if not hwnd:
        return []
    uia, _mod = _ensure_uia()
    walker = uia.RawViewWalker
    root = uia.ElementFromHandle(hwnd)
    if not root:
        return []
    edits: list[_UiaEdit] = []
    _walk_for_edits(uia, walker, root, edits)
    return [
        {
            "name": e.name,
            "automation_id": e.automation_id,
            "control_type": e.control_type,
            "rect": e.rect,
            "width": e.width,
            "height": e.height,
            "keyboard_focusable": e.keyboard_focusable,
            "enabled": e.enabled,
        }
        for e in edits
    ]


def focus_chat_input(hwnd: int, *,
                     timeout_s: float = _DEFAULT_TIMEOUT_S) -> bool:
    """Move keyboard focus to the chat input under HWND. Returns True
    on success, False if no plausible input was found, SetFocus failed,
    or focus didn't transfer within `timeout_s`.

    The caller should sleep ~80-150 ms after a True return so Chromium
    has time to commit the focus change before the subsequent Ctrl+V.
    """
    if not hwnd:
        return False

    try:
        uia, _mod = _ensure_uia()
    except Exception:
        logger.exception("UIA bootstrap failed; cannot focus chat input.")
        return False

    try:
        root = uia.ElementFromHandle(hwnd)
    except Exception:
        logger.exception("ElementFromHandle(0x%08X) failed.", hwnd)
        return False
    if not root:
        logger.warning("ElementFromHandle(0x%08X) returned NULL.", hwnd)
        return False

    walker = uia.RawViewWalker
    edits: list[_UiaEdit] = []
    _walk_for_edits(uia, walker, root, edits)
    if not edits:
        logger.info("focus_chat_input(0x%08X): no Edit/Document elements "
                    "found in the UIA tree.", hwnd)
        return False

    target = _pick_chat_input(edits, _window_client_rect(hwnd))
    if target is None:
        logger.info("focus_chat_input(0x%08X): %d candidates but none "
                    "passed the chat-input heuristic.", hwnd, len(edits))
        return False

    logger.debug("focus_chat_input(0x%08X): target name=%r aid=%r "
                 "ctype=%d rect=%s area=%.0f",
                 hwnd, target.name, target.automation_id,
                 target.control_type, target.rect, target.area)

    try:
        target.element.SetFocus()
    except Exception:
        logger.exception("SetFocus raised on the chosen Edit; falling "
                         "through to verify-loop in case it took anyway.")

    # Verify by polling GetFocusedElement().
    deadline = time.monotonic() + max(0.05, timeout_s)
    while time.monotonic() < deadline:
        try:
            focused = uia.GetFocusedElement()
        except Exception:
            focused = None
        if focused is not None:
            try:
                # Compare via runtime IDs -- IUIAutomation.CompareElements
                # is the documented way to check element identity.
                if uia.CompareElements(focused, target.element):
                    return True
            except Exception:
                pass
        time.sleep(_FOCUS_VERIFY_INTERVAL_S)

    logger.warning("focus_chat_input(0x%08X): SetFocus issued but the "
                   "focused element did not match the target within "
                   "%.2fs.", hwnd, timeout_s)
    return False
