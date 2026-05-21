# Dictado

> Talk into your computer. Get text where the cursor was. That's the
> whole product.

`Dictado` is a tray-resident dictation utility that wraps OpenAI Whisper
behind a single global hotkey. The model is loaded into RAM at login and
stays warm, so each dictation is as fast as the model itself: hit the
hotkey, speak, hit it again, the transcription is on your clipboard and
(if you let it) auto-pasted where your cursor was.

It runs on **Windows, macOS, and Linux** from the same Python package.
There is no cloud, no telemetry, no account. After the one-time model
download from OpenAI's public CDN, no audio or text leaves the machine.

---

## Why this exists

Dictation tools either live in the cloud (Otter, Whisper.cpp Cloud,
Wispr Flow) or they're locked into a single editor (VS Code voice
plugins, Slack's web-only "voice notes"). Neither matches the workflow
of "type into whatever has focus, including the terminal, including a
GitHub PR description, including Slack's iframe-embedded message box".

Dictado is small enough that you can audit the entire codebase in an
afternoon, runs entirely on-device, and uses only the OS primitives
your endpoint protection software already trusts (more on that below).

---

## At a glance

- **Agent Input Mode** (new in 0.4): turn dictation into one-shot
  message-sending. Pick a target app from the tray menu and Dictado will
  activate it, paste, and press Enter for you. Works with ChatGPT, Claude,
  Cursor, VS Code, Slack, Teams, Discord, Telegram, and more.
  Full design notes in [docs/AGENT_INPUT_MODE.md](docs/AGENT_INPUT_MODE.md).
- **Configurable global hotkey.** Default is **Alt + T**. Change it from
  the tray menu without restarting; the choice persists across reboots.
  Five presets ship out of the box and a *Set custom…* dialog accepts
  any combination matching the hotkey grammar.
- **Live recording window.** A small frameless popup near the bottom of
  your screen shows a real-time audio-level meter and the transcription
  as it grows. The popup is borderless and configured to never steal
  focus, so the cursor stays exactly where you put it.
- **Auto-paste, optional.** When transcription finishes, Dictado places
  the text on the clipboard and synthesizes a single Ctrl+V (Cmd+V on
  macOS) into the window that was active when you started recording.
  Same primitive every clipboard manager uses. Disable from the tray
  if you want pure clipboard-only behaviour.
- **Audio archive.** Each recording is saved as a 16 kHz mono WAV next
  to a rolling weekly transcript log (Markdown). Filename is
  `<datetime>__<firstword>__<lastword>.wav`. The archive lives in
  `~/Documents/Sound Recordings/` by default and is excluded from git.
- **Picks the right model for your hardware.** Tray menu lets you swap
  between `base`, `small`, `medium` at runtime. `base` is fast enough
  for live partial previews on a laptop CPU; `medium` is the right pick
  for one-shot dictation when accuracy matters more than realtime.

---

## Install

You need Python 3.10 or newer.

```bash
git clone https://github.com/<you>/Dictado.git
cd Dictado
pip install --user .
```

The first launch downloads Whisper weights to `~/.cache/whisper/`. After
that there's nothing on the wire.

To start the daemon:

```bash
dictado            # foreground; logs to stdout AND ~/...local/share/dictado/daemon.log
python -m dictado  # equivalent
```

A microphone icon shows up in the system tray. Wait for it to turn green
and you're ready.

To make Dictado start at login:

```bash
dictado --install-autostart   # OS-appropriate autostart entry
dictado --uninstall-autostart # back out
```

The autostart mechanism varies per OS:

| OS      | What gets created                                                  |
|---------|--------------------------------------------------------------------|
| Windows | `dictado.lnk` in your Start Menu Startup folder                    |
| macOS   | `~/Library/LaunchAgents/io.github.dictado.daemon.plist`            |
| Linux   | `~/.config/autostart/dictado.desktop` (XDG-standard)               |

If you're on Linux Wayland, the global hotkey requires a system-wide
keyboard shortcut bound to `python -m dictado --toggle` (Wayland blocks
keyboard grabs from arbitrary user processes).

---

## Hotkey grammar

Anything matching this shape is accepted:

```
[<modifier>+ ...] <key>
```

Modifier tokens (case-insensitive):

| Token   | Aliases                       |
|---------|-------------------------------|
| `ctrl`  | `control`                     |
| `shift` | —                             |
| `alt`   | —                             |
| `win`   | `cmd`, `super`, `meta`        |

The final token is one of:

- a single character: `a`–`z`, `0`–`9`, punctuation
- a function key: `f1`–`f24`
- a named key: `space`, `enter`, `tab`, `escape`, `up`, `down`, `left`,
  `right`, `home`, `end`, `pageup`, `pagedown`, `insert`, `delete`,
  `backspace`

Examples that work: `alt+t`, `ctrl+shift+v`, `ctrl+alt+space`, `win+h`,
`f9`, `ctrl+\``.

If the combo is already owned by another app, the rebind quietly fails,
the previous binding stays, and you'll see a line in `daemon.log` —
nothing crashes.

---

## Tray menu

| Item                                | Action                                               |
|-------------------------------------|------------------------------------------------------|
| Record / Stop                       | Same as the hotkey.                                  |
| Hotkey ▶                            | Pick a preset, or **Set custom…** for a Tk prompt.   |
| Model ▶                             | Switch between tiny / base / small / medium / large. |
| Auto-paste after transcription      | Toggle the synthesized Ctrl+V step.                  |
| Quit                                | Drop the model from RAM and exit.                    |

The tray icon's colour reflects state: gray (loading), green (ready),
red (recording), yellow (transcribing).

---

## Models

Dictado loads any model the upstream `openai-whisper` package
accepts — tiny / base / small / medium plus the `*.en` English-only
variants, plus large-v1, large-v2, large-v3, and large-v3-turbo.
See **[docs/MODELS.md](docs/MODELS.md)** for the full table and a
picking guide.

The five surfaced in the tray menu by default:

| Tray label      | Model name        | Disk    | CPU RAM | Notes                                                  |
|-----------------|-------------------|--------:|--------:|--------------------------------------------------------|
| Base            | `base`            | 140 MB  | 0.5 GB  | Best for live partials on a slow CPU                   |
| Small           | `small`           | 460 MB  | 1.0 GB  | Sweet spot accuracy / speed on a laptop CPU            |
| Medium          | `medium`          | 1.5 GB  | 1.5 GB  | High-accuracy multilingual; default on first launch    |
| Large v3 Turbo  | `large-v3-turbo`  | 1.5 GB  | 1.5 GB  | ~5× faster than `large-v3` on CPU                      |
| Large v3        | `large-v3`        | 2.9 GB  | 3.0 GB  | Highest accuracy; sub-realtime on CPU                  |

Need an English-only or older-large variant? They are loadable from
the CLI:

```bash
dictado --switch-model medium.en
dictado --switch-model large-v2
```

---

## Performance

`benchmark.py` runs every model **sequentially** (only one in RAM at a
time) on a known clip and writes both `BENCHMARKS.md` and a JSON file.
Reproduce it on your own machine:

```bash
python benchmark.py samples/jfk.flac --models tiny base small medium --runs 3
```

Numbers from a 12-core x86 laptop with no GPU, transcribing the
~11-second public-domain JFK clip:

| Model  | Median run | Realtime factor | Notes                              |
|--------|-----------:|----------------:|------------------------------------|
| tiny   |    0.66 s  |        ~17×     | Fastest; punctuation imperfect.    |
| base   |    1.37 s  |        ~8×      | Sweet spot for live partials.      |
| small  |    3.01 s  |        ~3.7×    | Adds the comma after "Americans".  |
| medium |    9.41 s  |        ~1.2×    | Best punctuation; final pass only. |

See `BENCHMARKS.md` for the full table including load times and the
side-by-side transcriptions that let you grade accuracy by eye.

---

## Configuration

Settings live at:

| OS      | Path                                                |
|---------|-----------------------------------------------------|
| Windows | `%LOCALAPPDATA%\dictado\config.json`               |
| macOS   | `~/Library/Application Support/dictado/config.json`|
| Linux   | `~/.local/share/dictado/config.json`               |

```json
{
  "model":       "medium",
  "hotkey":      "alt+t",
  "autopaste":   true,
  "popup":       true,
  "language":    "en",
  "archive_dir": null
}
```

`archive_dir = null` (the default) means "use `~/Documents/Sound
Recordings/`". On Windows that folder is honoured via
`SHGetKnownFolderPath`, so OneDrive-redirected Documents folders work
correctly. Set it to a string to force a specific path, or to an empty
string to disable archiving.

---

## Endpoint-protection notes

Dictado is built so it doesn't trip the kind of behaviour-based
detections endpoint protection products use to flag keyloggers and
remote-access tools. The full mapping is in [docs/SECURITY.md](docs/SECURITY.md);
in summary:

- Hotkey is a Win32 `RegisterHotKey` (Windows) or pynput global hotkey
  (macOS / Linux X11). **No `keyboard` Python lib**, no
  `SetWindowsHookEx WH_KEYBOARD_LL`, no global low-level hook.
- Auto-paste is exactly **one** synthesised Ctrl+V chord per dictation
  via `SendInput` / `osascript` / `xdotool`. **No `keyboard.write()`**,
  no character-by-character keystroke pumping.
- IPC between the daemon and CLI shims is a **polled trigger file** in
  the per-user state dir. **No socket listener**, no named pipe.
- Auto-start uses the OS's standard mechanism: a Startup-folder shortcut
  on Windows, a `.desktop` entry on Linux, a LaunchAgent on macOS.
  **No Scheduled Task.**

If your Falcon / Defender for Endpoint / SentinelOne tenant still flags
the daemon, [docs/SECURITY.md](docs/SECURITY.md) includes a
copy-pasteable exception template.

---

## Repository layout

```
dictado/                     runtime package (cross-platform)
├─ daemon.py                 tray icon, recording loop, popup, transcribe
├─ archive.py                WAV writer + rolling weekly transcript log
├─ config.py                 persisted settings + hotkey grammar parser
└─ platform/
   ├─ windows.py             RegisterHotKey, SendInput, Startup .lnk
   ├─ macos.py               pynput, osascript Cmd+V, LaunchAgent
   └─ linux.py               pynput / wtype, xdotool, .desktop

scripts/                     install helpers (install.ps1, install.sh)
benchmark.py                 sequential model benchmark
samples/jfk.flac             public-domain audio for the benchmark
docs/SECURITY.md             endpoint-protection mapping
BENCHMARKS.md                measured speed and accuracy per model
ABOUT.md                     longer-form project background
CHANGELOG.md                 versioned release notes
```

---

## License

MIT. See [LICENSE](LICENSE).
