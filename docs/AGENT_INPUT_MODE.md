# Agent Input Mode

The previous behaviour for Dictado was: hold the hotkey, talk, release,
the transcription gets pasted at your cursor. Useful for typing into
documents and code, but for AI assistants and chat apps the next thing
you do is always the same — press Enter. Agent Input Mode automates
that step (and the "switch to the right window" step before it).

## What it does

When AIM is on, finishing a recording does this:

1. Writes the transcription to the system clipboard.
2. Optionally focuses the target app's window.
3. Synthesises a Ctrl+V (Cmd+V on macOS).
4. Synthesises a single Enter.

The result: you talk, you stop talking, the message sends. No mouse, no
alt-tab, no typing.

## Three modes

The mode lives in the tray menu under **Agent Input Mode** and is
written to `config.json` as the `agent_input` key.

### Off (`"off"`) — default

Identical to v0.3 behaviour. Clipboard write plus auto-paste, no Enter.
Pick this if you mostly use Dictado for prose and don't want surprise
sends.

### Auto (`"auto"`)

Clipboard + Ctrl+V + Enter into whichever window had focus when you
pressed the hotkey. Useful when you bounce between several chat apps
and don't want to bind Dictado to any one of them.

### A specific app (`"slack"`, `"chatgpt"`, etc.)

Dictado looks up the app's running window, brings it to the
foreground, then does paste + Enter. If the app isn't running anymore
when the dictation finishes, it logs a warning and falls back to Auto
behaviour for that one dictation, so your hotkey still does *something*
useful.

## Available app profiles

Dictado ships profiles for the apps people most commonly want to
dictate at:

| Category | Apps |
|----------|------|
| AI       | ChatGPT, Claude, Microsoft Copilot |
| IDE      | Cursor, VS Code (stable + Insiders), Kiro, Zed, Neovide, JetBrains family |
| Chat     | Slack, Microsoft Teams, Discord, Telegram, WhatsApp Desktop, Signal |
| Browser  | Chrome, Edge, Firefox |
| Notes    | Obsidian, Notion |

The list in the tray menu is filtered by what's actually running on
your machine — Dictado checks process image names against the profile
list every time you open the menu. Apps you don't have installed (or
that aren't running right now) don't show up.

To add a new profile, drop a tuple into `_PROFILES_RAW` in
`dictado/agent_input.py`:

```python
("notion-calendar", "Notion Calendar", _profile_by_image("Notion Calendar.exe")),
```

If the app shares a process name with something else (a webapp running
in your browser, for example), use `_profile_by_title(regex)` instead.

## Cross-platform support today

- **Windows**: full support. Detection via `EnumWindows` + process image
  match. Activation via `ShowWindow(SW_RESTORE) + SetForegroundWindow`.
  Enter via `SendInput`.
- **macOS** and **Linux**: stubs. The hotkey + paste + Enter pipeline
  needs an `osascript` (macOS) or `xdotool key Return` (Linux X11) call;
  detection needs `lsappinfo` (macOS) or `wmctrl -l` (Linux). Patches
  welcome.

The cross-platform API is fixed regardless of OS:

```python
from dictado import agent_input
running = agent_input.detect_apps()       # list[App]
agent_input.activate("slack")             # foreground a target
agent_input.send_enter()                  # synthesise Enter
```

## Endpoint protection

The Windows path adds two new primitives compared to v0.3:

- `EnumWindows` + `GetWindowThreadProcessId` + `OpenProcess` (with the
  *limited information* access right). Read-only window enumeration.
  Same primitive every taskbar replacement, window-snapper, and screen
  recorder uses.
- `SetForegroundWindow` against another process's top-level window.
  Allowed without ceremony because the foreground change is triggered
  by the user's hotkey, which Windows treats as user-initiated input.

If your environment's IOAs are stricter, leave AIM at Off — Dictado
behaves like v0.3 and never touches either of those APIs.

## Privacy

App detection is read-only: window enumeration only reads titles,
class names, and the executable path of the owning process. Nothing is
saved, sent, or logged beyond the local `daemon.log` line that says
"AIM target X activated". The detection result lives only in the tray
menu render and is recomputed every time you open the menu.

## Failure modes and what happens

| Situation | What Dictado does |
|---|---|
| Target app crashed since you set AIM | Logs warning, falls back to Auto for that dictation |
| Target app is minimized | `ShowWindow(SW_RESTORE)` brings it back, then activate |
| `SetForegroundWindow` refuses (Windows focus-stealing rules) | Falls back to "paste into whatever is foreground"; usually still works because the hotkey gave us recent input focus |
| Clipboard hasn't updated by the time Ctrl+V fires | Doesn't happen in practice (50 ms delay before the chord) |
| Enter would do something destructive | Up to you not to enable AIM in apps where Enter ≠ "send" |
