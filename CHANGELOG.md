# Changelog

This file tracks Dictado release notes. Format:
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [0.3.0] – 2026-05

The hotkey overhaul.

### New

- **Live-rebindable global hotkey.** The default is now `Alt+T` (single
  chord, no collision with editors that bind `Ctrl+Shift+V` to "paste
  without formatting"). The tray menu has a fresh **Hotkey** submenu
  that lists five presets and a *Set custom…* dialog where you can
  type any combination matching the hotkey grammar (see README). The
  rebind takes effect immediately — no daemon restart required — and
  the choice is persisted to `config.json` as `"hotkey"`.

- **Hotkey grammar parser.** Pre-validates user input before the
  platform adapter touches the OS, so a malformed config value can't
  crash the daemon at startup.

### Internal

- The platform `HotkeyHandle` classes (Windows / macOS / Linux) now
  expose a `rebind(spec)` method. On Windows it's implemented by
  posting a custom `WM_APP` message to the hotkey's own message-pump
  thread, because `RegisterHotKey` / `UnregisterHotKey` must run on the
  same thread that owns the binding.

## [0.2.0] – 2026-05

The "make it actually usable" release.

### New

- **Audio archive.** Each recording is saved as a 16 kHz mono WAV under
  `~/Documents/Sound Recordings/`. Filename is
  `<YYYYMMDD-HHMMSS>__<firstword>__<lastword>.wav`. A rolling weekly
  Markdown transcript log lives in the same folder; a new one rolls
  every 7 days. Both are excluded from git via `.gitignore`.

- **Live preview popup.** Borderless, topmost, no-activate window with
  a real-time audio-level meter and an incremental partial
  transcription as you speak. Implemented as a separate Tk thread; the
  no-activate flags ensure it never steals focus from the editor you
  intend to paste into.

- **Auto-paste.** After transcription, Dictado synthesizes a single
  `Ctrl+V` (`Cmd+V` on macOS) into the window that was foreground when
  recording started. Toggle from the tray menu; the setting persists.

- **Cross-platform skeleton.** The same package now runs on Windows,
  macOS, and Linux through a thin platform adapter (`register_hotkey`,
  `paste_into_window`, `install_autostart`).

- **`dictado --install-autostart` / `--uninstall-autostart`.**
  OS-appropriate autostart: Startup-folder `.lnk` (Windows), XDG
  `.desktop` (Linux), LaunchAgent plist (macOS).

- **Sequential benchmark script.** `benchmark.py` loads each model one
  at a time (RAM-bounded), warms up, runs N timed transcriptions, and
  writes both `benchmark_results.json` and `BENCHMARKS.md`. JFK
  public-domain clip lives at `samples/jfk.flac`.

### Changed

- **Streaming partials are serialized via `model_lock`.** PyTorch's CPU
  backend isn't thread-safe; concurrent calls between the streaming
  thread and the final-pass thread caused a `torch_cpu.dll` access
  violation (and a 7+ GB Windows Error Reporting dump). The streaming
  thread now uses a non-blocking acquire and skips its turn cleanly if
  the final pass is busy.

- **Removed the `keyboard` Python library.** Hotkey is now `RegisterHotKey`
  on Windows, `pynput` on X11/macOS. Paste is one `SendInput` /
  `osascript` / `xdotool` chord per dictation — same primitive
  clipboard managers use, no character-by-character pumping.

- **Removed the localhost TCP listener for IPC.** Replaced with a polled
  trigger-file directory under the per-user state dir. No socket means
  no port and no inbound surface for endpoint protection to flag.

- **Removed the AtLogon Scheduled Task.** Replaced with a Startup-folder
  shortcut.

- **Launcher is the system-installed Python interpreter** with
  `PYTHONPATH` pointing at the venv's `site-packages`. Some endpoint
  protection tenants flag any `pythonw.exe` running out of
  `%LOCALAPPDATA%\*\venv\` on path-shape grounds; launching from
  `C:\Program Files\Python313\pythonw.exe` (PSF-signed, in a trusted
  path) sidesteps that.

### Fixed

- `pythonw.exe` no longer crashes silently at startup. Previous build
  left `sys.stdout`/`sys.stderr` as `None`, which makes `whisper` and
  `tqdm` raise `AttributeError` on their first write attempt. Both are
  redirected to `os.devnull` before any heavy import now.

- The Tk popup no longer steals focus (added `WS_EX_NOACTIVATE |
  WS_EX_TOOLWINDOW` on Windows).

## [0.1.0] – initial prototype

Single-file Windows-only daemon. Used `keyboard.add_hotkey`,
`keyboard.write`, a TCP control socket, and a Scheduled Task. Not
suitable for managed endpoints.
