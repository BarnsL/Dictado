# Endpoint protection notes

This document maps every behavior of `dictado` to the
endpoint-protection IOA category it touches, and explains the design
choices that keep the daemon out of trouble on EDR-managed machines
(CrowdStrike Falcon, SentinelOne, Microsoft Defender for Endpoint, etc.).

If your endpoint blocks the daemon, jump to
[Requesting an exception](#requesting-an-exception) for a copy-pasteable
ticket template.

---

## Behaviors and the categories they touch

| Behavior | API | Category | What we do |
|---|---|---|---|
| Global hotkey | `RegisterHotKey` (Win32) / `pynput.GlobalHotKeys` (X11/macOS) | None reliably flagged | Used by Notepad++, Greenshot, every screen-recorder. |
| Microphone capture | `pyaudio` → WASAPI / CoreAudio / ALSA | Audio capture | Same as Teams, Zoom, Discord, Chrome `getUserMedia`. |
| Clipboard write | `pyperclip` (`OpenClipboard` / `NSPasteboard` / xclip) | None | Standard productivity primitive. |
| Auto-paste | `SendInput` Ctrl+V (Windows) / `osascript` Cmd+V (macOS) / `xdotool key ctrl+v` (Linux) | Synthetic input | One chord per dictation. Same primitive Ditto, Maccy, Paste, ClipClip use. |
| IPC | Polled trigger files in per-user state dir | None | No socket, pipe, or mailslot listener. |
| Auto-start | Startup-folder `.lnk` (Win) / `.desktop` (Linux) / LaunchAgent (mac) | Persistence | Same mechanism Spotify, Discord, Slack, OneDrive, Teams use. |

---

## Behaviors that are **not** in this build (but were in the prototype)

These were deliberately removed because they're how an EDR product
identifies a keylogger / RAT:

| Removed | Why |
|---|---|
| `keyboard.add_hotkey()` | Installs a low-level keyboard hook (`SetWindowsHookEx WH_KEYBOARD_LL`). The canonical keylogger primitive. |
| `keyboard.write(text)` | Pumps each character of an arbitrary string through `SendInput`. The canonical credential-stealer primitive. |
| Loopback TCP listener for IPC | Unsigned interpreter binding `127.0.0.1:N` from a user-writable path is the canonical C2 shape. |
| AtLogon Scheduled Task | EDR products treat AtLogon-triggered tasks owning unsigned interpreters as suspicious persistence. |
| `pythonw.exe` from `%LOCALAPPDATA%\*\venv\` | Some Falcon tenants flag any `pythonw.exe` running from a venv path under `%LOCALAPPDATA%`, regardless of behavior. |

The current build launches `pythonw.exe` from `C:\Program Files\Python313`
(the PSF-signed system Python in a Falcon-trusted location), with
`PYTHONPATH` set to the venv's `site-packages`. The venv is just a
dependency container; we never run it as the entry point.

---

## What goes on the network

After the one-time model download, **nothing**. Specifically:

- The first `whisper.load_model("medium")` call downloads weights from
  `https://openaipublic.azureedge.net/...` (Microsoft Azure CDN, hosting
  OpenAI's published model artifacts) into `~/.cache/whisper/`.
- After that, the daemon makes zero outbound connections.
- The daemon never opens a listener — local or otherwise.

You can verify with `netstat -ano | findstr "LISTENING"` (Windows) or
`ss -ltnp` (Linux): the only ports our process owns are the ones owned
by the OS for our system tray and X11/Wayland connection.

---

## Crash handling

The daemon catches exceptions in every thread (`recorder`, `streamer`,
`popup`, `loader`, `trigger`) and logs them to `daemon.log` rather than
crashing the process. The one exception we don't catch is
`whisper.load_model` failing at startup; if the model can't be loaded
the daemon exits cleanly and the autostart entry will retry on next
login.

A previous build crashed in PyTorch's CPU backend (`torch_cpu.dll`,
access violation 0xC0000005) when the streaming and final-pass threads
ran `model.transcribe()` simultaneously. It produced a 7.6 GB Windows
Error Reporting dump. The current build serializes through a
`model_lock` and uses a non-blocking acquire in the streaming thread, so
partials skip cleanly when the final pass is running.

---

## Requesting an exception

If your tenant still blocks the daemon, file a Falcon (or equivalent)
exception with the following template. Replace bracketed fields.

```
Subject: Allowlist request — dictado local voice-to-text utility

Host:           [your hostname]
Product:        dictado [https://github.com/<org>/dictado]
Version:        0.2.0
Image SHA-256:  [Get-FileHash "C:\Program Files\Python313\pythonw.exe" -Algorithm SHA256]
Image path:     C:\Program Files\Python313\pythonw.exe
                (PSF-signed, default Python from python.org)
Parent process: explorer.exe
Persistence:    %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\WhisperDaemon.lnk
                (Startup-folder shortcut, same mechanism as Spotify / Discord / Slack)

Network:        HTTPS to openaipublic.azureedge.net, FIRST RUN ONLY,
                downloads OpenAI Whisper model weights.
                No listener (no inbound port). No outbound traffic after
                the model cache is populated.

Audio:          Microphone capture via WASAPI shared mode (pyaudio).
                Identical to Teams / Zoom / Discord behavior.

Clipboard:      OpenClipboard / SetClipboardData. No keylogging.

Synthetic input: ONE Ctrl+V chord per dictation, via SendInput. No
                arbitrary-string keystroke pumping. Same primitive
                used by every clipboard manager (Ditto, ClipClip,
                PhraseExpress, Win+V history paste).

IPC:            Polls files in %LOCALAPPDATA%\dictado\trigger\.
                Does NOT bind any socket, named pipe, or mailslot.

Hotkey:         Win32 RegisterHotKey(MOD_CONTROL|MOD_SHIFT, VK_V).
                Does NOT use SetWindowsHookEx (no global keyboard hook).

Justification:  Local-only voice-to-text using OpenAI Whisper. All
                speech recognition is performed on-device. No data
                leaves the host except the one-time model download.
                Source code: [https://github.com/<org>/dictado]
```

---

## What to do if Falcon flags it after install

1. Check the local Falcon Operational log:
   ```powershell
   Get-WinEvent -LogName 'CrowdStrike-Falcon Sensor-CSFalconService/Operational' `
       -MaxEvents 50 | Where-Object Id -eq 3 |
       Select TimeCreated, @{n='img';e={$_.Properties[0].Value}}
   ```
   If the blocked image is `C:\Program Files\Python313\pythonw.exe`, the
   IOA is behavior-based and you need to look at what the daemon was
   doing in the seconds before the block. If the blocked image is
   anywhere else (a venv copy, a tmp dir, a OneDrive synced folder),
   it's path-based and the fix is to move the launcher.

2. Disable auto-paste (tray menu → uncheck **Auto-paste after
   transcription**). The text still lands on the clipboard so manual
   `Ctrl+V` continues to work.

3. As a last resort, set `archive_dir` to `null` and remove the autostart
   entry. The daemon then becomes a foreground command-line tool with no
   persistence and no disk artifacts, which is essentially impossible
   for a behavior-based IOA to flag.
