# Issue: AIM paste reaches the target window but not its input field

**Status:** Open  
**Severity:** High (intermittent across AIM Electron-app targets)  
**Affected component:** `dictado.agent_input` + `dictado.platform.windows`  
**Affected mode:** Agent Input Mode targeting Electron / Chromium-based
apps (ChatGPT desktop, Claude desktop, Microsoft Copilot, Cursor,
Slack, Teams, Discord, etc.)

---

## What happens

You set AIM to one of the supported Electron apps. You're working in a
different window. You hit the hotkey, dictate, hit it again. The
target app comes to the foreground, the rating popup appears with the
correct transcription — but the target's chat input is empty and
nothing was sent. The text is on the clipboard; manual Ctrl+V into
the input puts it there fine.

From the daemon's logs everything looks successful: the window was
located, focus_window confirmed the foreground swap, paste_into_window
returned True. The Ctrl+V chord went out. It just didn't go where the
user expected.

---

## Why it happens

Electron / Chromium apps have two separate kinds of focus:

- **Window focus** at the OS level. `SetForegroundWindow` controls
  this. Determines which window receives keyboard events.
- **WebContents focus** internal to Chromium. Controls which DOM
  element actually receives those events.

Dictado's `focus_window()` helper does the AttachThreadInput dance and
the verify-loop properly — window focus arrives at the target. But it
doesn't move WebContents focus. If WebContents focus was last on a
non-input element (a button, sidebar item, model picker, code block),
that's where the synthesized Ctrl+V lands. Most non-input elements
swallow the chord without doing anything visible.

---

## How to reproduce

1. Tray menu → AIM → pick an Electron AI app.
2. Use the target a bit; click into a button / sidebar / model picker
   so the last-focused WebContents element is something other than the
   prompt input.
3. Switch foreground to another window.
4. Hotkey, dictate, hotkey.
5. Inspect the target's input.

---

## Workaround in v0.5.2

The `App` profile dataclass has an optional `post_activate` callable.
For ChatGPT, Claude, Copilot, and Cursor profiles it's bound to
`_send_ctrl_l()` — most Electron AI apps bind Ctrl+L to "focus prompt"
or "new chat" (both leave the prompt input focused). The hook fires
after `focus_window()` confirms the foreground swap, then sleeps
120 ms before the daemon synthesizes Ctrl+V.

This works on some apps. It does not work on every Electron app — any
target whose Ctrl+L is bound to something else, or to nothing, falls
through.

---

## Better fixes worth implementing

### Option A — UI Automation (UIA) `Element.SetFocus()`

Use the Windows accessibility API (`UIAutomationCore.dll` via
`comtypes`) to find the prompt input by `ControlType=Edit` or
`AutomationName` and call `SetFocus()` on it. Same approach Narrator
uses; threads through Chromium's accessibility tree and reliably moves
WebContents focus to the right element.

- New dep: `comtypes` (~50 KB pure Python wrapper).
- Adds ~150 lines of UIA wiring.
- Pays off across every Electron / Chromium target without per-profile
  tuning.

### Option B — Per-profile AutomationId map

Each profile carries the AutomationId of its prompt input.
`UIA.FindFirst(...)` + `SetFocus()` is much faster than walking the
whole tree, and ties the fix to one stable identifier per app.

- Maintenance: AutomationIds occasionally change between app versions.
- Best paired with (A) as a cache.

### Option C — Synthetic click at the input's likely coordinates

Click at "X% from left, Y% from bottom" of the window's client rect.
Cheap and dep-free, but breaks the moment the user resizes narrow,
opens a modal, or the app changes layout.

### Option D — Multi-shortcut fallback

Try a list of focus-shortcuts in order: `Ctrl+L`, `Ctrl+/`, `End`, etc.
Stop when `GetFocus()` reports a different HWND than before. Easy to
implement; brittle in the long tail.

### Recommendation

Build option A as the production fix. Keep `_send_ctrl_l()` as the
fast path for apps where it works; UIA is the fallback for everything
else.

---

## A note on the rating loop

The rating popup fires after every successful **transcription**,
regardless of whether AIM actually landed the paste. So a 10/10
rating in this scenario means "the model heard me right" — not "my
message got sent." Worth considering: a second binary question on
the rating popup, *"did the AIM target receive the text?"*, when
AIM is configured. That'd give us per-clip signal on whether AIM
succeeded for each app.

---

## Affected files

| Concern | Path |
|---|---|
| Per-profile post-activate hook | `dictado/agent_input.py` (`App.post_activate`) |
| Current Ctrl+L workaround | `dictado/agent_input.py` (`_send_ctrl_l`) |
| Verified-focus dance | `dictado/platform/windows.py` (`focus_window`) |
| Pre-paste focus re-verification | `dictado/platform/windows.py` (`paste_into_window`) |
| Daemon AIM dispatch | `dictado/daemon.py` |
| Where a UIA fix would land | new module `dictado/platform/uia.py`, called from `_activate_hwnd` or as an alternative `post_activate` |

---

## Verifying the fix

1. Pick an Electron AI app that's been failing for you.
2. Click around its UI to put WebContents focus on a non-input
   element.
3. Switch foreground to another window.
4. Use AIM to dictate.
5. Confirm: the text appears in the prompt input AND the message is
   submitted (it shows up in the conversation).
6. Repeat with the tray menu's "Re-rate" path open during the
   dictation, to make sure the rating popup doesn't itself steal the
   focus that was supposed to land at the target.
