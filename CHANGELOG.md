# Changelog

This file tracks Dictado release notes. Format:
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [SemVer](https://semver.org/).

## [0.6.4] -- 2026-05-22

Wake-event silence auto-stop reliability + wake-sound playback
duration.

### Fixed

- **Silence auto-stop reliably ends wake-triggered recordings.**
  v0.6.1's static `wake_silence_rms_threshold = 0.010` was below
  most rooms' ambient noise floor; "silent" pauses kept getting
  classified as voice and the auto-stop never accumulated.
  Replaced with a hybrid threshold:
  `max(wake_silence_rms_threshold, voice_baseline * 0.35)` where
  `voice_baseline` is the RMS of the first 1.0 s of the recording.
  Auto-tunes per recording. Plus a per-second INFO-level
  countdown log line so users can watch the auto-stop progress.
  Static fallback raised from 0.010 to 0.030.
- **Wake-startup sound now plays to completion.** The
  PowerShell-via-WMF shim was hardcoded to a 1.5 s lifetime;
  clips longer than that got truncated. Now reads
  `MediaPlayer.NaturalDuration` and sleeps for the actual file
  length + 200 ms.

### Documentation
- `docs/WAKE_WORD.md` updated with a "Silence threshold tuning"
  subsection.
## [0.6.3] -- 2026-05-22

Tray-icon tooltip casing.

### Changed

- "dictado - starting..." -> "Dictado - starting..."; tooltip's
  active-state format string ("dictado â€” Ready [...]") -> "Dictado
  â€” Ready [...]". The pystray Icon ID and Python logger name stay
  lowercase.
## [0.6.2] -- 2026-05-22

Wake-word reliability fixes.

### Fixed

- **Wake listener now actually re-enables on daemon restart.**
  v0.6.1 persisted `wake_word_enabled: true` to config.json
  correctly but the daemon's `main()` startup path was missing
  the `_start_wake_detector_async()` call. Result: the toggle
  appeared "checked" in the tray menu but the listener wasn't
  running until the user toggled it off and on again. Closes
  the cycle.
- **Regex broadened with `Baby Boo` and `beep` variants** that
  Whisper produces frequently when transcribing "biboo" on
  noisy / quiet windows. Plus the symmetric `Baby + j-/zh-`
  pattern for "bijou", and defensive `b â†’ p` substitutions
  (`piboo`, `pee-joo`, etc).
## [0.6.1] -- 2026-05-22

Wake-event polish: startup sound, silence auto-stop, broader regex.

### New

- **Optional startup sound on wake activation.** Configure via
  `wake_sound_path` + `wake_sound_volume`. Empty path = silent
  cue. Any format your platform's audio decoder supports.
- **Silence auto-stop** for wake-triggered recordings. Default
  3 s of continuous silence ends the recording. Tunable via
  `wake_silence_stop_s` and `wake_silence_rms_threshold`. Set
  `wake_silence_stop_s` to 0 to disable. Hotkey-triggered
  recordings keep their existing behaviour.
- **Wake regex significantly broadened.** Added every
  Whisper-transcription variant of Bijou / Biboo we've observed
  ("Bee Boo", "Bibu", "peeboo", "big boo", "bee jew", "biggio",
  ...). RMS gate lowered from 0.012 to 0.008 so quieter
  utterances trigger.
- **"No wake match" log lines now visible at INFO level** so
  users debugging a missed phrase don't have to enable DEBUG to
  see what Whisper actually transcribed.

### Fixed

- **Config file BOM handling.** `config.load()` now reads
  `utf-8-sig`, so the daemon stops silently reverting to
  defaults when the user edits `config.json` in Notepad or any
  other tool that writes a UTF-8 BOM.
## [0.6.0] -- 2026-05-22

The "no-hands" release.

### New

- **Wake-word activation.** Tray menu now has a "Voice activation"
  toggle. Flip it on and Dictado starts listening for one of a
  configurable list of wake phrases ("hey bijou", "ok biboo",
  "salutations bijou", etc.). When a phrase is heard, the daemon
  starts a recording the same way pressing Alt+T would.
- **Implementation lives in `dictado/wake_word.py`.** A
  secondary Whisper `tiny.en` instance transcribes a 2.5-second
  rolling microphone buffer at 500 ms cadence. A cheap RMS check
  short-circuits when the room is silent so the model only runs
  when there\'s actually voice activity â€” keeps idle CPU around
  0.5%.
- **Two assistant names recognised by default.** Bijou
  (*bee-joo*) and Biboo (*bee-boo*), each prefixed with any of
  `hey`, `ok`, `okay`, `yo`, `hello`, `hi`, `greetings`,
  `salutations`. The matcher tolerates Whisper\'s common
  mistranscriptions of these unusual proper nouns (bayou,
  beejoo, bijoux, bibu, etc).
- **Custom phrase list via `wake_word_phrases` in
  `config.json`.** Each entry becomes one alternation; the last
  word is treated as the name, with phonetic-variant fuzzing if
  it\'s a recognised name (`bijou`, `biboo`).
- **`docs/WAKE_WORD.md`** â€” full design, architecture diagram,
  tuning, troubleshooting, and the playbook for adding more
  wake names.

### Notes

- Off by default â€” no listening happens unless the user opts
  in.
- Costs ~140 MB RAM and ~5% CPU when active speech is
  triggering inference; ~0.5% idle.
- The detector pauses while a recording is in progress so it
  can\'t terminate the user\'s own dictation.
## [0.5.7] -- 2026-05-22

No more console flash on recording end.

### Fixed

- **`whisper.audio.load_audio()`\'s ffmpeg subprocess no longer
  flashes a console window.** Root cause: the final-transcription
  path was writing a tmp WAV and calling `model.transcribe(path)`,
  which goes through `whisper.audio.load_audio(path)` -> `ffmpeg
  -i path ...` as a subprocess. On `pythonw.exe`, child
  subprocesses without `CREATE_NO_WINDOW` briefly flash a console.
  Fix: pass a numpy array directly to `model.transcribe()` -- we
  already have the audio as int16 frames, so the in-Python
  conversion is faster than re-decoding via ffmpeg anyway. The
  tmp WAV write stays for the audio archive.
- **Belt-and-braces.** New `_suppress_subprocess_consoles_on_windows()`
  monkey-patches `subprocess.Popen` at startup to default to
  `CREATE_NO_WINDOW`. Catches any other dependency that might
  spawn a console child now or later.
## [0.5.6] -- 2026-05-22

Cold-launch AIM paste now matches the right element in the a11y tree.

### Fixed

- **Per-profile `input_name_regex` removes ambiguity.** Each AIM
  profile can now supply a regex that the chosen UIA Edit/Document
  element's Name property must match. Without this, on landing
  pages with multiple focusable elements (suggested-action buttons,
  search boxes), `_pick_chat_input` could return the wrong element
  and `wait_for_chat_input` would happily report success against a
  bystander. Shipped regexes: ChatGPT `^Message`, Claude
  `^(Reply to |Talk with |How can I help)`, Copilot `^(Message|Ask)`,
  Cursor (none).
- **`wait_for_chat_input(set_focus=True)` atomic SetFocus.** When
  the regex matches, we call SetFocus and verify it inside the
  same function. No race window during which Chromium can reshuffle.
- **Cold-launch deadline 12 -> 30 s.** Observed on this hardware,
  Electron AI app cold-start to interactive can take 20-25 s after
  a reboot. 30 s margin handles it; warm-path latency unchanged.
- **DEBUG-level per-poll log in `wait_for_chat_input`.** Useful
  when adding a new profile and the heuristic isn't catching the
  right element.
## [0.5.5] -- 2026-05-22

Cold-launch AIM paste reliability.

### Fixed

- **Auto-launching an Electron AI app now waits for its chat input
  to render before pasting.** Previously, `activate_target`
  followed `launch_target` immediately with `_focus_input_via_uia`,
  which walked the UIA tree before Chromium had finished rendering
  the WebContents. The chat input wasn't yet in the tree, the UIA
  picker fell back to the giant Document, and the Ctrl+V chord
  landed on whatever element Chromium had focused for its splash.
  Fix: new `platform.uia.wait_for_chat_input(hwnd, deadline_s)`
  polls the a11y tree at 300 ms cadence until a focusable Edit
  matching the chat-input heuristic appears (up to 12 s). The
  `_focus_input_via_uia` hook uses it whenever `activate_target`
  signals the cold-launch path via a thread-local flag.
## [0.5.4] -- 2026-05-22

AIM auto-launch + paste-reliability followups.

### New

- **Auto-launch missing AIM targets.** Profiles can declare a
  `launch_paths` tuple; if the target app isn't running when the
  hotkey fires, the daemon spawns the first existing entry detached
  and polls for its window before proceeding. Hints use
  `$LOCALAPPDATA` / `$PROGRAMFILES` / `$APPDATA` etc. Today's
  shipping hints cover ChatGPT desktop, Claude desktop, Cursor.

### Fixed

- **Inner-focus state is now preserved across paste.**
  `paste_into_window` accepts a new `already_focused: bool` keyword.
  When the caller has just run `activate_target` and an inner-focus
  hook (UIA `SetFocus()` on the chat input), it passes
  `already_focused=True` so we don't re-run `focus_window` -- which
  was clobbering Chromium's WebContents focus and dropping the Ctrl+V
  on the wrong element. This was the missing piece in v0.5.3.
## [0.5.3] -- 2026-05-22

AIM paste reliably reaches Electron AI app chat inputs.

### Fixed

- **The AIM-into-Electron-input bug** documented in
  `docs/ISSUE_aim-electron-paste.md` is now resolved for the four
  shipped profiles (ChatGPT desktop, Claude desktop, Microsoft Copilot,
  Cursor). New module `dictado/platform/uia.py` walks the target's
  accessibility tree via UI Automation and calls
  `IUIAutomationElement.SetFocus()` on the Edit element identified as
  the chat input, BEFORE Dictado's existing `SendInput` Ctrl+V chord
  fires. UIA threads through Chromium's accessibility integration and
  reliably moves the inner WebContents focus -- which is what Ctrl+L
  alone could not guarantee.
- **Element-picking heuristic** documented in `uia.py`:
  - hard-prefer `ControlType=Edit` over `ControlType=Document`;
  - if exactly one focusable+enabled Edit is in the tree, pick it
    without rect heuristics (Chromium occasionally returns stale or
    inverted bounding rects on live inputs);
  - otherwise filter by area (no more than 30% of the window) and
    bottom-fraction location, then take the smallest.
- **Fallback chain unchanged**: when UIA cannot find a plausible
  input, the v0.5.2 Ctrl+L chord still fires as a backup focus hint.

### Notes

- Zero new third-party dependencies. `comtypes` is already pulled in
  transitively by `pystray`.
- UIA is the Microsoft accessibility API every screen-reader uses.
  Read-only tree walks plus one `SetFocus()` call per dictation; no
  input injection beyond the pre-existing single Ctrl+V chord. The
  endpoint-protection mapping in `docs/SECURITY.md` continues to
  apply.
## [0.5.2] -- 2026-05-22

One-click launchers, no code changes.

### New

- **`Dictado.cmd`, `Dictado.command`, `dictado.desktop`** at the repo
  root. Double-click to start the daemon on Windows / macOS / Linux
  respectively. The Windows launcher walks
  `C:\Program Files\Python313/312/311/310` to pick a PSF-signed
  `pythonw.exe`, then sets `PYTHONPATH` from a sibling `.venv` if one
  is present, then hands off to `python -m dictado`. The macOS and
  Linux equivalents do the same dance against their own conventions.
- **README's "Install" section now leads with the double-click flow.**
  The shell `dictado` / `python -m dictado` invocation stays as the
  power-user fallback.

### Notes

- Pure packaging change. Nothing in `dictado/` was touched.
- A real `.exe` / `.app` would weigh in north of 2 GB once Whisper +
  PyTorch are frozen â€” the launchers are the right tool here.
## [0.5.1] -- 2026-05

Benchmark refresh.

### New

- `benchmark.py` learns about ground-truth references. Pass
  `--reference path/to/text.txt` and the generated `BENCHMARKS.md`
  picks up a Word Error Rate column. Reference and hypothesis are
  normalised before alignment so the canonical all-caps LibriSpeech
  references compare cleanly with the model output.
- New benchmark clip: `samples/librispeech-1272-128104-0004.flac`,
  29.4 s, with the verbatim LibriSpeech reference next to it.
  Long-enough and varied-enough to actually rank the larger models.

### Changed

- `BENCHMARKS.md` rebuilt against the new clip across all six
  default-visible models. Headlines: WER 16.18% on tiny.en,
  8.82% on large-v3-turbo, realtime factors that move with the
  parameter count.

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
