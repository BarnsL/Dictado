# About Dictado

## What you get

A small daemon that lives in your system tray and turns a global hotkey
into a "hold to talk" dictation interface for any text field on your
computer. Editor, browser, terminal, chat — anywhere keyboard input is
accepted, the transcription appears at the cursor.

Dictado wraps the OpenAI Whisper open-source model. Inference happens
locally on your CPU (or GPU, if you've got one). The first launch
downloads the model weights from OpenAI's public CDN; after that, the
process makes zero network calls.

## What's deliberately *not* here

- **No cloud.** No login screen, no API key, no telemetry, no usage
  beacons. If you firewall the daemon after the initial weight download,
  it keeps working forever.
- **No global keyboard hook.** The first prototype used the `keyboard`
  Python library, which installs `SetWindowsHookEx(WH_KEYBOARD_LL)` —
  the canonical keylogger primitive. That's gone. The hotkey is a
  Win32 `RegisterHotKey` (or pynput on the other platforms), which can
  only deliver the one combo it was registered for, and cannot read
  any other keystroke.
- **No synthesized typing.** Dictado never types your transcription out
  character by character. It puts the text on the clipboard and (if you
  enabled it) sends a *single* Ctrl+V chord — the same primitive every
  clipboard manager uses.
- **No background network listener.** Inter-process communication uses a
  polled file-system trigger in the per-user state directory. Nothing
  binds a socket. Nothing listens on a port. Falcon doesn't see it.
- **No Scheduled Task.** Autostart is the OS's standard mechanism —
  Startup-folder shortcut on Windows, `.desktop` entry on Linux,
  LaunchAgent on macOS. Same files Spotify, Discord, Slack drop in.

The cumulative effect is that Dictado looks, to endpoint protection
software, like a slightly weird text editor. Not like a keylogger and
not like a remote-access tool.

## Why the name

`Dictado` is "dictation" in Spanish/Portuguese, and short. It's a
pseudonymous handle for the project — function-first, doesn't tie the
tool to its author or to any specific Whisper-named distribution.

## Architecture (one paragraph)

The daemon runs five threads. The pystray icon owns the main thread.
A separate thread runs the OS hotkey message pump and (on Windows) can
re-register the hotkey live when you change it from the tray menu.
A third thread polls a per-user "trigger" directory so the
`dictado --toggle` CLI shim can talk to a running daemon without a
socket. A fourth thread runs the Tk popup window with the audio meter.
The fifth thread is spawned when you press the hotkey: it captures from
PyAudio, writes a WAV, calls `whisper.transcribe()`, copies the result
to the clipboard, optionally fires the Ctrl+V, and exits.

Cross-platform support is a thin adapter at `dictado.platform.adapter()`
that returns the right module for the current OS. Each adapter exposes
exactly three things: `register_hotkey`, `paste_into_window`, and
`install_autostart`. To port to a new OS, copy `linux.py`, fill in the
three sections, and you're done.

## What you'll find when you open the box

```
dictado/                       runtime package
benchmark.py                   sequential model benchmark
samples/jfk.flac               public-domain test audio
scripts/install.{ps1,sh}       one-shot installers
docs/SECURITY.md               endpoint-protection mapping
BENCHMARKS.md                  measured per-model speed and accuracy
README.md                      operator's guide
ABOUT.md                       this file
CHANGELOG.md                   release notes per version
```

## A note on the audio archive

By default Dictado saves a copy of every recording to your `Documents`
folder, plus a rolling weekly Markdown transcript log. That's nice for
"oh, what was the thing I dictated last Tuesday?", but worth knowing
about: on Windows, OneDrive's Documents redirection means those WAVs
sync to your OneDrive cloud automatically. If you don't want that, set
`archive_dir` in `config.json` to a non-synced path (or to `null` to
turn the archive off entirely). The transcribed text still lands on
your clipboard regardless.

The git ignore file in this repo excludes `Sound Recordings/`,
`AudioTranscriptions_*.md`, and every common audio extension by
default — so even if you point `archive_dir` inside the repo
checkout you can't accidentally commit your voice memos.
