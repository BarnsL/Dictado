# Changelog

This file tracks Dictado release notes. Format:
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [0.5.0] — 2026-05

The "every Whisper model" release.

### New

- **Full Whisper catalog.** A new `dictado.models` module enumerates
  every checkpoint upstream `openai-whisper` accepts —
  `tiny.en` / `tiny` / `base.en` / `base` / `small.en` / `small` /
  `medium.en` / `medium` / `large-v1` / `large-v2` / `large-v3` /
  `large-v3-turbo`. The `large` and `turbo` aliases are recognised
  but de-duplicated from menus so the same checkpoint never shows up
  twice. Each catalog row carries a display label, parameter count,
  on-disk size, RAM cost, and a multilingual flag.

- **`docs/MODELS.md`** — full table, picking guide by hardware,
  switching mechanics, and download sizes.

### Changed

- Tray menu defaults to a 5-entry slice (`base`, `small`, `medium`,
  `large-v3-turbo`, `large-v3`). The CLI trigger
  `dictado --switch-model NAME` accepts any name in the catalog.
- `config.py` model validation defers to `dictado.models.is_known()`
  so the persisted `model` key can be anything in the catalog.

## [0.4.0] — 2026-05

The "press Enter for me" release.

### New

- **Agent Input Mode** (AIM). After transcription, Dictado can now
  optionally activate a specific app, paste, and synthesise a single
  Enter. The new tray submenu lists every supported profile that's
  currently running. Off / Auto / specific-app modes; persists to
  `config.json` under `agent_input`.

- **App-profile detection.** A small curated list of profiles for AI
  assistants, IDEs, chat apps, browsers, and notes apps. Detection is
  read-only and uses the same Win32 enumeration primitives every
  window-management utility uses (`EnumWindows`,
  `GetWindowThreadProcessId`, `QueryFullProcessImageNameW`). Adding a
  new profile is a one-tuple PR.

- **`docs/AGENT_INPUT_MODE.md`** — operator's guide and
  endpoint-protection notes for AIM.

### Changed

- The post-transcription decision tree in `dictado.daemon` was
  rewritten as a single block that handles all four
  AIM × auto-paste combinations.

## [0.3.0] – 2026-05

The hotkey overhaul.

### New

- **Live-rebindable global hotkey.** The default is now `Alt+T` (single
  chord, no collision with editors that bind `Ctrl+Shift+V` to "paste
  without formatting"). Five presets and a *Set custom…* dialog ship
  in the tray menu.

- **Hotkey grammar parser.** Pre-validates user input before the
  platform adapter touches the OS, so a malformed config value can't
  crash the daemon at startup.

### Internal

- Each platform's `HotkeyHandle` exposes a `rebind(spec)` method.

## [0.2.0] – 2026-05

The "make it actually usable" release.

### New

- Audio archive (WAV + rolling weekly Markdown transcript log).
- Live preview popup.
- Auto-paste.
- Cross-platform skeleton.
- `--install-autostart` / `--uninstall-autostart`.
- Sequential benchmark script.

### Changed

- Streaming partials serialised via `model_lock` (was racing with the
  final pass and crashing in `torch_cpu.dll`).
- Removed the `keyboard` Python lib in favour of OS-native hotkey APIs.
- Removed the localhost TCP listener for IPC; replaced with a polled
  trigger-file directory.
- Removed the AtLogon Scheduled Task; replaced with the OS's
  Startup-folder mechanism.

### Fixed

- `pythonw.exe` no longer crashes silently at startup.
- Tk popup no longer steals focus.

## [0.1.0] – initial prototype

Single-file Windows-only daemon. Used `keyboard.add_hotkey`,
`keyboard.write`, a TCP control socket, and a Scheduled Task.
