# Security model and audit notes

This document is the authoritative answer to "what does this daemon
actually do, what could go wrong, and how do we keep it from going
wrong". It's read by:

- developers porting the daemon to a new platform,
- corporate security reviewers approving install requests,
- the user themselves before bumping a version.

If you don't have time to read the whole thing: the daemon transcribes
voice into text, locally, with no network egress after the first model
download, and never installs a global keyboard hook. The expanded
sections below explain the design choices that get us there.

---

## 1. Threat model

| Adversary | Goal | What we do about it |
|---|---|---|
| Malware on the host | Read the user's transcribed text via the clipboard / read the WAV archive / send audio over the network | Daemon never opens an inbound listener. Clipboard is the OS-shared one (no extra exposure). Audio archive lives in the user's Documents folder with normal user-level ACLs; the daemon doesn't elevate. |
| Endpoint protection (EDR) products | Mistakenly classify the daemon as keylogger / RAT / persistence malware | We avoid every primitive that EDR products score as high-risk: no `SetWindowsHookEx`, no inbound socket, no AtLogon Scheduled Task. See section 4. |
| Co-resident user account on a shared machine | Read the audio archive of another user | The archive default is `~/Documents/Sound Recordings/` (per-user). On Windows that's protected by NTFS ACLs. On Linux/macOS by Unix file permissions. The daemon doesn't relax any of those. |
| Network attacker (LAN / Wi-Fi) | Intercept transcribed text in transit | Nothing leaves the machine after the one-time model download (HTTPS to OpenAI's CDN). The CDN is Microsoft Azure / Akamai-fronted and enforces TLS. |
| Supply-chain attacker via PyPI | Inject malicious code via a tampered dependency | We pin dependencies in `pyproject.toml` to known-good versions and pull from PyPI directly (no private indexes). Whisper model weights come from `openaipublic.azureedge.net` over TLS. We do not auto-update dependencies. |
| User social-engineered into editing config.json | Trigger remote code execution / privilege escalation via crafted config values | All config values are validated on load (see `config.py`'s whitelist + range checks). Any string that gets shelled out to PowerShell / shell is escaped (see section 5). |

What we explicitly do NOT defend against:

- A user who already has admin rights on the machine (they can do anything regardless).
- A malicious Whisper model file substituted into the user's `~/.cache/whisper/` directory (the user has write access to that dir; this would be local code execution, but only of code the user themselves placed). We rely on PyTorch's own model-loading safety; do not load Whisper checkpoints from untrusted sources.
- A motivated user who manually configures the daemon to dictate into a sensitive field (sudo prompt, password manager). The daemon is a generic input device. Use it the way you would any other speech-to-text product.

---

## 2. What goes on the network

After the one-time model download from `https://openaipublic.azureedge.net/`,
**nothing**. Specifically:

- `whisper.load_model("medium")` (or whichever model you've selected)
  fetches model weights into `~/.cache/whisper/`. About 1.5 GB for
  medium, larger for large variants. This is a one-time cost per
  model; subsequent loads are local.
- The daemon makes zero outbound connections after that.
- The daemon never opens an inbound listener, port, named pipe,
  mailslot, or D-Bus service.

You can verify with `netstat -ano | findstr "LISTENING"` (Windows),
`ss -ltnp` (Linux), or `lsof -i -P` (macOS): the only ports owned by
our process are those owned by the OS for our system tray and
X11 / Wayland connection.

---

## 3. What stays on disk

| Artifact | Default location | Contains | Sensitive? |
|---|---|---|---|
| Audio recordings | `~/Documents/Sound Recordings/` | One WAV per dictation | Yes (your voice) |
| Transcript log | `~/Documents/Sound Recordings/AudioTranscriptions_*.md` | Plaintext transcriptions | Yes (your words) |
| Daemon log | `%LOCALAPPDATA%\<pkg>\daemon.log` (Windows) | INFO-level events, no audio, no clipboard contents | No (only the first 80 chars of partials are logged at DEBUG) |
| Config | `%LOCALAPPDATA%\<pkg>\config.json` | User settings | No |
| Trigger files | `%LOCALAPPDATA%\<pkg>\trigger\` | Empty marker files; touched by `--toggle` IPC | No |

**To opt out of the archive entirely**, set `archive_dir` to `null` (or
to an empty string) in `config.json`. The daemon then stops writing
WAVs and transcript markdown. Live transcription still works — the
text still lands on the clipboard — it just isn't persisted to disk.

**To redact the log**, change the daemon's log level to `WARNING`. INFO
log entries include the wake-word listener's "no match" lines, which
contain Whisper's raw transcription of any speech in the room (this is
how the regex got iteratively tightened during development). At
WARNING level only error conditions are recorded.

---

## 4. Endpoint-protection (EDR) mapping

The daemon is engineered to be invisible to behavior-based EDR
products. Every primitive we use is one that a normal productivity
app uses; every primitive a keylogger / RAT uses we don't.

| Behavior | API | Category | Why this is safe |
|---|---|---|---|
| Global hotkey | `RegisterHotKey` (Win32) / `pynput.GlobalHotKeys` (X11/macOS) | None reliably flagged | Same primitive Notepad++, Greenshot, every screen recorder uses. |
| Wake-word listener | `pyaudio` open + `whisper.transcribe` on rolling buffer | Audio capture | Same as Teams / Zoom / Discord / Chrome `getUserMedia`. |
| Microphone capture | `pyaudio` -> WASAPI / CoreAudio / ALSA | Audio capture | Same as above. |
| Clipboard write | `pyperclip` (`OpenClipboard` / `NSPasteboard` / `xclip`) | None | Standard productivity primitive. |
| Auto-paste | `SendInput` Ctrl+V (Win) / `osascript` Cmd+V (mac) / `xdotool` (Linux) | Synthetic input | One chord per dictation. Same primitive Ditto, Maccy, ClipClip use. |
| Geometry-based click fallback (v0.6.9+) | `SetCursorPos` + `mouse_event` LEFTDOWN/UP at the AIM target's chat-input zone | Synthetic input | Triggered ONLY when UIA SetFocus fails on a Chromium-based AIM target. Cursor saved + restored. Same primitive AutoHotkey, FlaUI, Sikuli use; one click per dictation. |
| IPC | Polled trigger files in per-user state dir | None | No socket, pipe, or mailslot listener. |
| Auto-start | Startup-folder `.lnk` (Win) / `.desktop` (Linux) / LaunchAgent (mac) | Persistence | Same mechanism Spotify, Discord, Slack, OneDrive, Teams use. |

### Behaviors that are NOT in this build

These were deliberately removed because they're how an EDR identifies
a keylogger or remote-access trojan:

| Removed | Why |
|---|---|
| `keyboard.add_hotkey()` | Installs a low-level keyboard hook (`SetWindowsHookEx WH_KEYBOARD_LL`). The canonical keylogger primitive. |
| `keyboard.write(text)` | Pumps each character of an arbitrary string through `SendInput`. The canonical credential-stealer primitive. |
| Loopback TCP listener for IPC | Unsigned interpreter binding `127.0.0.1:N` from a user-writable path is the canonical C2 shape. |
| AtLogon Scheduled Task | EDR products treat AtLogon-triggered tasks owning unsigned interpreters as suspicious persistence. |
| `pythonw.exe` from `%LOCALAPPDATA%\*\venv\` | Some Falcon tenants flag any `pythonw.exe` running from a venv path under `%LOCALAPPDATA%`, regardless of behavior. |

The current build launches `pythonw.exe` from
`C:\Program Files\Python313` (the PSF-signed system Python in a
trusted location), with `PYTHONPATH` set to the venv's `site-packages`.
The venv is just a dependency container; we never run it as the entry
point.

---

## 5. Input-validation and command-injection postmortem

### Audit trail

| Round | Vector | Status |
|---|---|---|
| v0.6.10 | `wake_sound_path` interpolated into PowerShell command via single-quoted literal | **Fixed.** Single quotes in path are doubled before formatting. Volume rendered as a clamped float. The vulnerability had been latent since v0.6.1 (when wake-sound shipped); a config value containing `'` would have terminated the literal and run subsequent path content as PS. |
| v0.6.10 | `python_exe` / `script_path` / shortcut target interpolated into PowerShell in `install_autostart` | **Fixed.** Same single-quote-doubling escape. python_exe is sourced from `sys.executable` and script_path from `__file__` so exploitability is low (would need a rare username with a single quote, e.g. "Travis O'Brien"), but defense-in-depth applies. |

### Current invariants (verified by `grep` and code review)

- `os.system`, `os.popen`, `eval`, `exec`, `compile` -- never used.
- `subprocess.Popen` / `subprocess.run` -- always invoked with `args=[...]` (list form), never with a shell-interpolated string. `shell=True` -- never used.
- `pickle.loads` / `marshal.loads` / `yaml.load` (unsafe) -- never used. The only deserialiser is `json.loads` from the user-controlled `config.json`, which can only set known keys to known types (validated by a whitelist in `config.py`).

### What to do if a future feature needs a shell call

Use `subprocess.run(["program", "--flag", str(value)])` -- argv form,
no shell. If you must invoke PowerShell with an interpolated string
(e.g. for a COM call you can't make from Python), escape every
interpolated value's single quotes by doubling them (`s.replace("'",
"''")`) and validate the value's character set up front. Keep the
patterns documented in this section so the next reviewer sees them.

---

## 6. Secrets and PII

- The daemon never stores secrets. Auth tokens, API keys, passwords -- none of these exist in the codebase.
- The daemon does store user audio + transcripts (in the archive) and the user's own config. These are PII, treated as user-private data, written under per-user ACLs, and never transmitted off the host.
- The transcription rating log (`TranscriptionRatings.md`) embeds short transcription strings and the user's 1-10 ratings. Same handling: per-user file, never transmitted.
- The daemon logs at INFO. INFO logs include "no wake match" lines that contain Whisper's raw transcription of room audio. Treat `daemon.log` as PII unless you've set log level to WARNING.
- Whisper model weights cached under `~/.cache/whisper/` are public artifacts published by OpenAI; they are not PII but if you delete the daemon you may want to delete this cache for disk hygiene.

---

## 7. Defensive configuration

If you want the most paranoid possible setup:

```jsonc
{
  // No archive of WAVs or transcripts to disk.
  "archive_dir": null,

  // No auto-paste -- text only goes to the clipboard.
  "autopaste": false,

  // No agent-input integration (no synthetic clicks, no Enter key).
  "agent_input": "off",

  // No quality-rating popup (which displays the transcription text on screen).
  "ratings_enabled": false,

  // No wake-word listener. Hotkey only.
  "wake_word_enabled": false
}
```

This reduces the daemon to: hotkey -> mic capture -> transcribe -> copy to clipboard. No persistence, no secondary windows, no synthetic input.

---

## 8. Requesting an EDR exception

If your tenant's policy still blocks the daemon, file an exception with
this template:

```
Subject: Allowlist request -- <pkg> local voice-to-text utility

Host:           <your hostname>
Product:        <pkg> [<repo URL>]
Version:        <__version__>
Image SHA-256:  Get-FileHash "C:\Program Files\Python313\pythonw.exe" -Algorithm SHA256
Image path:     C:\Program Files\Python313\pythonw.exe
                (PSF-signed, default Python from python.org)
Parent process: explorer.exe
Persistence:    %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WhisperDaemon.lnk
                (Startup-folder shortcut, same mechanism Spotify / Discord / Slack use)

Network:        HTTPS to openaipublic.azureedge.net, FIRST RUN ONLY,
                downloads OpenAI Whisper model weights.
                No listener (no inbound port). No outbound traffic
                after the model cache is populated.

Audio:          Microphone capture via WASAPI shared mode (pyaudio).
                Identical to Teams / Zoom / Discord behavior.

Clipboard:      OpenClipboard / SetClipboardData. No keylogging.

Synthetic input: One Ctrl+V chord per dictation, via SendInput.
                Optionally one mouse left-click per AIM dictation
                (geometry-based fallback when UIA SetFocus fails on
                Chromium-based targets). No arbitrary keystroke
                pumping, no SendInput WHEEL/SCROLL events, no DLL
                injection.

IPC:            Polls files in %LOCALAPPDATA%\<pkg>\trigger\.
                Does NOT bind any socket, named pipe, or mailslot.

Hotkey:         Win32 RegisterHotKey(MOD_ALT, VK_T) by default.
                Does NOT use SetWindowsHookEx (no global keyboard hook).

Justification:  Local-only voice-to-text using OpenAI Whisper. All
                speech recognition is performed on-device. No data
                leaves the host except the one-time model download.
                Source code: <repo URL>
```

---

## 9. Post-incident checklist

If something looks wrong with a running daemon:

1. **Check `%LOCALAPPDATA%\<pkg>\daemon.log`** for the last 50-100 lines around the time of the incident. Crashes, race conditions, and config-load failures are all logged there.
2. **Check Windows Application Event Log** for `pythonw.exe` faults: `Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error'} -MaxEvents 5`. The fault offset + faulting module identifies which native DLL was involved (PortAudio, ntdll, tcl86t, _portaudio.pyd, etc.).
3. **Verify the running daemon is the version you expect**: read `<install>/__init__.py`'s `__version__`. Disk patches don't reload Python modules in a live process; if you bumped the version on disk, restart.
4. **If audio archive WAVs look anomalous** (much shorter than expected, contain only ambient hum, etc.), check whether `wake_silence_stop_s` / `wake_silence_rms_threshold` / `wake_sound_lead_s` settings are sane. The wake-word path's silence-auto-stop can fire prematurely if the static threshold is below the room's ambient floor.
5. **If the daemon is connecting outbound** (you'd see this in `Process Monitor` or in your firewall), it should be `*.azureedge.net` on port 443 only, and only for the few seconds during the first model download. Anything else is a regression worth reporting.

---

## 10. Reporting a vulnerability

Open a GitHub issue OR email the maintainer privately if the issue is
exploitable. Include:

- daemon version (`__version__`)
- OS + Python version
- the exact crash / unexpected-behavior trace (`daemon.log` excerpt + Application Event Log fault offset)
- a minimal reproduction config / sequence

Coordinated disclosure if the issue is a real RCE / privilege
escalation. We will acknowledge within 7 days and ship a fix within
30 unless the report involves an embargo we agreed to in advance.
