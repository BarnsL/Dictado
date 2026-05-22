"""dictado - local voice-activated and push-to-talk voice-to-text for desktop.

Cross-platform daemon that turns OpenAI Whisper into an instant
press-and-talk dictation tool. The transcription happens entirely on the
local machine - no audio ever leaves the host after the one-time model
download.

Public modules:
    dictado.daemon     - the tray daemon, hotkey, popup, transcription
    dictado.config     - persisted settings (model, autopaste, etc.)
    dictado.archive    - WAV + rolling Markdown transcript log writer
    dictado.platform   - per-OS adapters (hotkey, paste, autostart)

Run as a script:
    python -m dictado           # start the daemon (foreground)
    python -m dictado --toggle  # IPC: toggle recording in a running daemon
"""
__version__ = "0.6.9"
