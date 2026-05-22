# Issue: AIM paste reaches the target window but not its input field


> **Resolved in v0.5.3.** New module `dictado/platform/uia.py` performs UI
> Automation `IUIAutomationElement.SetFocus()` on the chat input
> before the daemon's `SendInput` Ctrl+V chord fires. UIA threads
> through Chromium's accessibility integration and reliably moves
> the inner WebContents focus -- which is what the v0.5.2 / v0.6.3
> Ctrl+L workaround could not guarantee. The Ctrl+L chord stays as
> a fallback when UIA can't find a plausible input. Smoke test
> verified end-to-end. See the CHANGELOG entry for the picking
> heuristic. The doc below is preserved as a record of the
> investigation.

---


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

---

## Follow-up: paste_into_window's defensive re-focus was undoing the UIA SetFocus

**Discovered:** 2026-05-22, right after the v0.5.3 release went out.
**Symptom:** With `_focus_input_via_uia` correctly identifying and
SetFocus-ing the chat input, AIM dictation against an Electron AI app
*still* left the prompt input empty. The rating popup showed the
correct transcription; the clipboard contained the text; manual
Ctrl+V into the input worked fine.

**Root cause:** `dictado.platform.windows.paste_into_window(hwnd)` was
running a defensive `focus_window(hwnd)` call right before
synthesizing Ctrl+V. That call's `AttachThreadInput` +
`SetForegroundWindow` chain causes Chromium to reset WebContents
focus to its default — overwriting the UIA SetFocus we'd issued moments
earlier. The Ctrl+V then arrived at Chromium's freshly-defaulted focus
target (typically a button), not the chat input.

**Fix:** `paste_into_window` accepts a new `already_focused: bool`
keyword. The daemon sets it `True` whenever AIM has just routed
through `activate_target` and a post_activate hook landed inner focus
on a specific element. `paste_into_window` then skips its own
focus_window call, verifies the target window is foreground via
`GetForegroundWindow()`, and pumps Ctrl+V immediately — leaving the
WebContents focus untouched. If the foreground check fails (rare;
implies the user clicked away in the few ms after activate_target
returned), it falls back to the verified focus_window dance.

**Verified:** smoke test against Amazon Quick succeeded; the chat
input now contains the pasted text.

**Lesson:** defensive re-focus is the right move for the simple
"paste into the window I focused" case but the wrong move for the
"paste into the inner element I focused" case. The flag preserves
both behaviours under one entry point.

---

## Follow-up 2: the rating popup was stealing foreground

**Discovered:** 2026-05-22 (DictadoAR-side; mirrored back here for
posterity since the public twin shares the AIM dispatch shape).

**Root cause:** the daemon was creating the optional rating-loop Tk
popup BEFORE running the AIM activate_target + paste_into_window
chain. Tk window creation steals foreground from the just-activated
target on every Windows build we tried; by the time Ctrl+V was
synthesised, the rating popup owned the input queue, and the chord
landed there (or the previously-foregrounded window) instead of the
target's chat input.

**Status in Dictado:** Dictado does not ship a rating popup, so this
specific failure mode is not present here. Documented anyway so the
public twin doesn't reintroduce it when adding a similar feature.

**General lesson:** any post-AIM UI surface (rating popup, "did this
work?" toast, etc.) should be deferred until after `paste_into_window`
returns. Tk windows, Win32 dialogs, and pystray notifications all
have observable side effects on z-order and foreground.

---

## New feature: AIM auto-launch

Profiles now carry an optional `launch_paths: tuple[str, ...]` of
executable / shortcut hints. When `activate_target` can't locate a
live window for a profile, it walks the hints, expands environment
variables (`$LOCALAPPDATA`, `$PROGRAMFILES`, etc.), and launches the
first existing entry detached (`subprocess.Popen` for `.exe`,
`os.startfile` for `.lnk`). Then it polls `locate()` every 200 ms up
to 8 s and hands the resulting HWND back to the focus dance.

Today's profiles with launch hints: ChatGPT desktop, Claude desktop,
Cursor. Adding a new app is one tuple addition in `_PROFILES_RAW`.

---

## Follow-up 3: cold-launch race -- UIA tree wasn't ready yet

**Discovered:** 2026-05-22, after v0.5.4 / v0.6.5.dev0 went out.
The auto-launch path successfully spawned the target app and waited
for its OS window to appear (~1.6 s for AQ on this hardware). But
the UIA SetFocus immediately afterwards landed on the splash screen,
not the chat input -- because Chromium hadn't yet rendered the chat
input element into its accessibility tree.

**Symptom (verbatim from log):**

```
[INFO] activate_target('amazon-quick'): no live window; attempting auto-launch.
[INFO] launch_target('amazon-quick'): spawned C:\Program Files\Amazon Quick\Amazon Quick.exe
[INFO] launch_target('amazon-quick'): window appeared in 1.6s
[INFO] Rating: ... -- rated 10/10        ; rating popup fires later, AQ chat input still empty
```

The daemon thinks everything succeeded; the user sees an empty AQ
chat input and dictates again.

**Root cause:** The OS window appearing is a strict prerequisite
for, but not sufficient for, "this app is ready to receive input".
For Electron / Chromium apps, the window appears very early in the
load sequence, but the prompt input doesn't exist in the
accessibility tree until Chromium finishes paint. Empirically the
gap is 100-300 ms on a warm GPU cache and up to 2-3 s on a cold
start.

`_focus_input_via_uia` walked the UIA tree right after window
appeared, found only the giant Document (the WebContents scroll
host, which is always there), picked it as a fallback, and called
SetFocus. The Ctrl+V chord then arrived at the splash content,
which silently swallowed it.

**Fix shipped in v0.5.5 / v0.6.6.dev0:** new helper
`platform.uia.wait_for_chat_input(hwnd, deadline_s, poll_s)`.
Polls the UIA tree at 300 ms cadence and returns True the moment a
focusable Edit matching the chat-input heuristic shows up.
`activate_target` records `just_launched=True` on the thread-local
when `launch_target` actually spawned the app; `_focus_input_via_uia`
reads it and runs `wait_for_chat_input` first. Default deadline is
12 s, sized to comfortably exceed AQ cold-start on this hardware.

After the wait, foreground often drifts (splash-screen handoff),
so we re-assert it via a quick `focus_window(hwnd, timeout_s=0.5)`
before returning. That keeps `paste_into_window`'s
`already_focused` shortcut valid.

**Verified end-to-end:**

```
launch_target('amazon-quick'): spawned [...]Amazon Quick.exe
launch_target('amazon-quick'): window appeared in 1.6s
wait_for_chat_input(0x...): chat input appeared in 0.18s
hwnd = 0x... (took 2.30s end-to-end)
paste_ok: True
```

**Lesson logged:** the previous fix verified "window appears" then
trusted UIA. UIA-readiness is a separate gate from window-readiness,
and on Chromium-based apps the gap is non-trivial. Going forward,
any UIA-based focus operation against an Electron target should
gate on `wait_for_chat_input` (or an equivalent
ControlType-specific predicate) rather than just window presence.
