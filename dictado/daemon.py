"""dictado.daemon — the cross-platform tray daemon.

Runs as a single long-lived process. Exposes:

    main()                   - entry point, with optional --toggle / --quit /
                               --switch-model NAME / --install / --uninstall
    start_recording() / stop_recording() / toggle_recording()
                             - internal API; the hotkey, IPC, and tray menu
                               all funnel into these.

Design choices that matter for endpoint protection (CrowdStrike Falcon,
SentinelOne, etc.):
  * No `keyboard` Python lib  -- hotkey via OS-native APIs (RegisterHotKey
    on Windows, pynput on Linux X11 / macOS).
  * One single synthesized Ctrl+V (or Cmd+V) per dictation, NOT
    character-by-character keystroke pumping. This is the same primitive
    every clipboard manager (Ditto, Maccy, Paste, ...) uses.
  * IPC via files in the per-user state dir. NO TCP/UDP listener, no named
    pipes, no DBus.
  * Auto-start via the OS's standard mechanism (Startup folder shortcut on
    Windows, .desktop on Linux, LaunchAgent on macOS), NOT a Scheduled Task.

Threading model:
  main thread        : pystray Icon.run()        (tray icon + tray menu)
  hotkey thread      : OS-specific message pump
  trigger thread     : poll <state_dir>/trigger/
  popup thread       : tk.Tk().mainloop()
  recorder thread    : pyaudio stream.read loop  (one per recording)
  streaming thread   : periodic whisper.transcribe on a rolling window
"""
from __future__ import annotations

# pythonw on Windows leaves sys.stdout/sys.stderr at None, which crashes
# whisper/tqdm at import time. Re-point them at devnull BEFORE any heavy
# import. Real diagnostics still go through the rotating log file.
import sys as _sys
import os as _os
if _sys.stdout is None:
    _sys.stdout = open(_os.devnull, "w", encoding="utf-8")
if _sys.stderr is None:
    _sys.stderr = open(_os.devnull, "w", encoding="utf-8")

import argparse
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
import pyaudio
import pyperclip
import whisper
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from . import config as _cfg
from .platform import adapter as _platform_adapter
from .archive import archive_recording, default_archive_dir
from . import agent_input as _aim
from . import wake_word as _wake
from . import paths as _paths

# Wake-word detector instance, lazily started when the user enables
# the feature from the tray menu. None when wake-word is OFF.
wake_detector = None  # type: _wake.WakeWordDetector | None
wake_word_enabled = False
# True iff the current/most-recent recording was triggered
# by the wake-word listener rather than the hotkey or tray.
# Read by start_recording / the recorder thread to decide
# whether to play the wake sound and enable silence auto-stop.
_recording_was_wake_triggered = False

# Silence-detection ratio for wake-triggered
# recordings. Effective threshold is the larger of
# `wake_silence_rms_threshold` (config) and
# `voice_baseline_rms * WAKE_SILENCE_RATIO`. The
# voice baseline is sampled from the first 1.0 s
# of the recording (the user just said the wake
# phrase, so it captures their actual speaking
# volume on this mic in this room).
WAKE_SILENCE_RATIO = 0.35

# Grace period at the start of a wake-triggered recording during
# which the silence-auto-stop check is suspended. This covers
# the duration of the wake-startup sound (typical: 1-2 s) plus
# a small margin for the sound's tail / mic-bleed reverb. Without
# this, the cue's echo can register as voice and either
# (a) reset _last_voice_time on every frame, or
# (b) get treated as a voice baseline that artificially raises
# the silence threshold.
WAKE_SOUND_GRACE_S = 2.0
from . import models as _models

_plat = _platform_adapter()


# ─── Tunables (most you'll ever want to change live above runtime) ────────────
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK = 1024
FORMAT = pyaudio.paInt16

MAX_RECORD_SECONDS = 120

WHISPER_FP16 = False        # False is required on CPU. Set True on CUDA.

# How often to re-run the partial transcription, and how much trailing
# audio to feed it. 1.5 s + 8 s feels live without burning CPU.
STREAM_INTERVAL_SECONDS      = 1.5
STREAM_WINDOW_SECONDS        = 8.0
STREAM_MIN_NEW_AUDIO_SECONDS = 0.6

# Tray-menu model list. Defaults to a five-entry slice of the full catalog
# (see dictado/models.py). Power users can switch to any other Whisper
# checkpoint via the IPC trigger: `dictado --switch-model large-v3-turbo`,
# `dictado --switch-model tiny.en`, etc.
SELECTABLE_MODELS = _models.DEFAULT_VISIBLE
TRIGGER_POLL_SECONDS = 0.25


# ─── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger("dictado")


def _suppress_subprocess_consoles_on_windows() -> None:
    """Default subprocess.Popen to CREATE_NO_WINDOW on Windows.

    Why this exists: when pythonw.exe spawns a child via subprocess
    (us, or any dependency like whisper.audio.load_audio's ffmpeg
    call) without explicit creationflags, Windows briefly creates a
    console window for the child. Even if the child is a GUI app,
    the console flashes for a fraction of a second.

    DETACHED_PROCESS alone doesn't suppress this; CREATE_NO_WINDOW
    does. We monkey-patch Popen to OR CREATE_NO_WINDOW into
    creationflags by default, while still letting callers opt out
    by passing creationflags explicitly (the patch only applies
    when creationflags isn't already set).
    """
    if sys.platform != "win32":
        return
    import subprocess as _sp
    if getattr(_sp.Popen, "_dictado_no_window_patched", False):
        return
    CREATE_NO_WINDOW = 0x08000000
    _orig_init = _sp.Popen.__init__

    def _patched_init(self, *args, **kwargs):
        # Only inject the flag if the caller didn't specify creationflags.
        # That way callers who DO want a console (e.g. for diagnostics)
        # still get one.
        if "creationflags" not in kwargs or kwargs["creationflags"] is None:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            kwargs["creationflags"] |= CREATE_NO_WINDOW
        return _orig_init(self, *args, **kwargs)

    _sp.Popen.__init__ = _patched_init
    _sp.Popen._dictado_no_window_patched = True
    logger.debug("subprocess.Popen patched to default to CREATE_NO_WINDOW.")

logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(_cfg.log_path(), maxBytes=512 * 1024,
                               backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(_handler)
if sys.stdout is not None and hasattr(sys.stdout, "fileno"):
    try:
        _console = logging.StreamHandler(sys.stdout)
        _console.setFormatter(logging.Formatter("[dictado] %(message)s"))
        logger.addHandler(_console)
    except (AttributeError, OSError):
        pass


# ─── Runtime state ────────────────────────────────────────────────────────────
model = None
current_model_name = None
recording = False
recording_started_at: datetime | None = None
audio_frames: list[bytes] = []
foreground_token = None      # OS-specific handle from get_foreground_window()
icon: Icon | None = None
status_text = "Idle"
recording_lock = threading.Lock()
model_lock     = threading.Lock()
autopaste_enabled = True
popup_enabled     = True
language          = "en"

popup_cmds: queue.Queue = queue.Queue()
_stream_stop = threading.Event()
_last_partial_frame_count = 0
hotkey_handle = None  # platform HotkeyHandle; lets us rebind live
current_hotkey_spec = "alt+t"
agent_input_mode = "off"  # 'off' | 'auto' | <app id from agent_input.APP_PROFILES>


# ─── Model loading ────────────────────────────────────────────────────────────
def load_model(name: str) -> None:
    """Load (or swap to) a whisper model. First load downloads weights."""
    global model, current_model_name, status_text
    with model_lock:
        status_text = f"Loading {name}..."
        _update_icon_tooltip()
        _update_tray_icon("gray")
        logger.info("Loading whisper model %r ...", name)
        t0 = time.time()
        model = whisper.load_model(name)
        current_model_name = name
        _cfg.update(model=name)
        logger.info("Model %r loaded in %.1f s", name, time.time() - t0)
        status_text = "Ready"
        _update_icon_tooltip()
        _update_tray_icon("green")
        if icon is not None:
            icon.menu = _build_tray_menu()
            icon.update_menu()


# ─── Tray icon + tooltip helpers ──────────────────────────────────────────────
def _create_icon_image(color: str = "green") -> Image.Image:
    colors = {"green": "#4CAF50", "red": "#F44336",
              "yellow": "#FFC107", "gray": "#9E9E9E"}
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([8, 8, 56, 56], fill=colors.get(color, "#4CAF50"))
    d.rectangle([26, 16, 38, 38], fill="white")
    d.arc([22, 28, 42, 48], 0, 180, fill="white", width=2)
    d.line([32, 48, 32, 54], fill="white", width=2)
    d.line([24, 54, 40, 54], fill="white", width=2)
    return img


def _update_icon_tooltip() -> None:
    if icon is not None:
        m = current_model_name or "?"
        ap = "on" if autopaste_enabled else "off"
        icon.title = f"Dictado — {status_text} [{m}, autopaste {ap}]"


def _update_tray_icon(color: str) -> None:
    if icon is not None:
        icon.icon = _create_icon_image(color)


def _play_wake_sound() -> None:
    """Play the configured wake-startup sound.

    Honours config keys:
      - wake_sound_path: absolute path to a .wav / .m4a / .mp3 file.
        Empty / missing => no sound.
      - wake_sound_volume: 0.0 - 1.0 (only honoured for the WMF
        path; winsound has no per-call volume control).

    Runs synchronously on the calling thread; the file must be short
    (< 1 s recommended) or the user perceives a delay before the
    record stream opens. Wraps everything in try/except so a missing
    or corrupted file never crashes start_recording.
    """
    try:
        cfg = _cfg.load()
    except Exception:
        return
    requested = (cfg.get("wake_sound_path") or "").strip()
    # Resolve through paths.resolve_wake_sound so an empty config
    # value (or a missing/erased file) falls back to the bundled
    # default at assets/sounds/biboo-asmr-hello.m4a, then to
    # assets/sounds/chime.wav. Returns None only if the repo is
    # missing the assets folder, which we treat as "user
    # explicitly disabled the cue".
    path = _paths.resolve_wake_sound(requested)
    if not path:
        return
    if not os.path.exists(path):
        logger.warning("wake-sound resolved to %r but file missing; "
                       "skipping.", path)
        return
    try:
        volume = float(cfg.get("wake_sound_volume", 0.7))
    except (TypeError, ValueError):
        volume = 0.7
    volume = max(0.0, min(1.0, volume))
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".wav" and sys.platform == "win32":
            # winsound is the cheapest path: zero-deps, ~10 ms latency.
            # SND_ASYNC means we don't block. SND_FILENAME tells it path,
            # not registry alias. SND_NOSTOP means a previous play
            # doesn't get clobbered (rare with our 1-shot calls).
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        if sys.platform == "win32":
            # Non-WAV (m4a / mp3 / aac / ogg / flac): hand off to
            # Windows Media Foundation via PowerShell. This is more
            # expensive (~80 ms PowerShell startup) but works for any
            # codec WMF supports -- including .m4a which winsound
            # does NOT handle.
            import subprocess
            # Why this PS shim is the way it is:
            #
            #   1. winsound only handles WAV. Anything else (m4a, mp3,
            #      aac, flac, ogg) needs Windows Media Foundation,
            #      which Python doesn't expose directly. PowerShell
            #      gives us cheap WMF access via
            #      System.Windows.Media.MediaPlayer.
            #
            #   2. MediaPlayer.Open() is asynchronous. We can't just
            #      Play() and exit -- the spawned PS process would
            #      die before MediaPlayer finishes buffering and the
            #      audio would never play.
            #
            #   3. MediaPlayer.NaturalDuration is the file's real
            #      length (HH:MM:SS) once HasAudio = $true. We sleep
            #      for that duration + 200 ms (audio-buffer flush
            #      margin), capped at 8 s. Previous versions hard-
            #      coded a 1.5 s sleep, which truncated any clip
            #      longer than 1.5 s -- the user's 2.0 s
            #      biboo-asmr-hello.m4a was being cut off at ~75%.
            #
            #   4. Wrapping in try/finally + $p.Close() releases the
            #      WMF media handle cleanly so the file isn't held
            #      open if the user later edits it.
            ps_cmd = (
                f"Add-Type -AssemblyName presentationCore;"
                f"$p=New-Object System.Windows.Media.MediaPlayer;"
                f"$p.Volume={volume};"
                f"$p.Open([System.Uri]::new('{path}'));"
                # Wait up to 2 s for the async Open to populate
                # NaturalDuration. HasAudio flips True when the
                # decoder has confirmed the file actually contains
                # audio.
                f"$ready=$false;"
                f"for ($i=0; $i -lt 40; $i++) {{"
                f"  if ($p.NaturalDuration.HasTimeSpan) {{ $ready=$true; break }}"
                f"  Start-Sleep -Milliseconds 50"
                f"}};"
                # Compute total milliseconds: real duration + 200 ms
                # buffer-flush margin, capped at 8 s. Falls back to
                # 3000 ms if NaturalDuration was never resolved.
                f"$ms = if ($ready) {{"
                f"  [Math]::Min(8000, [int]$p.NaturalDuration.TimeSpan.TotalMilliseconds + 200)"
                f"}} else {{ 3000 }};"
                f"$p.Play();"
                f"try {{ Start-Sleep -Milliseconds $ms }}"
                f"finally {{ $p.Stop(); $p.Close() }}"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-Command", ps_cmd],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        # macOS: use afplay (always available).
        if sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["afplay", "-v", str(volume), path],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        # Linux: paplay (PulseAudio) -> aplay (ALSA) fallback.
        if sys.platform.startswith("linux"):
            import subprocess
            for cmd in (["paplay", "--volume",
                         str(int(volume * 65536)), path],
                        ["aplay", "-q", path]):
                try:
                    subprocess.Popen(cmd,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    return
                except FileNotFoundError:
                    continue
            logger.info("No paplay/aplay found; skipping wake sound.")
    except Exception:
        logger.exception("wake-sound playback raised; skipping.")


# ─── Live popup window (tkinter) ──────────────────────────────────────────────
def _popup_loop() -> None:
    """tkinter mainloop on its own thread. Window is shown/hidden on demand,
    but the Tk root and event loop live for the entire daemon process."""
    if not popup_enabled:
        return
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    root.title("Whisper")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#1e1e1e")

    width = 520
    status_var = tk.StringVar(value="Recording")
    text_var   = tk.StringVar(value="")

    header = tk.Frame(root, bg="#1e1e1e")
    header.pack(fill="x", padx=12, pady=(10, 2))
    dot = tk.Canvas(header, width=14, height=14, bg="#1e1e1e",
                    highlightthickness=0)
    dot_id = dot.create_oval(2, 2, 12, 12, fill="#F44336", outline="")
    dot.pack(side="left", padx=(0, 8))
    tk.Label(header, textvariable=status_var, fg="#E0E0E0", bg="#1e1e1e",
             font=("Segoe UI", 10, "bold")).pack(side="left")
    hint_var = tk.StringVar(value="Toggle hotkey to stop")
    tk.Label(header, textvariable=hint_var, fg="#7a7a7a", bg="#1e1e1e",
             font=("Segoe UI", 9)).pack(side="right")

    meter = tk.Canvas(root, width=width - 24, height=6, bg="#2d2d2d",
                      highlightthickness=0)
    meter.pack(padx=12, pady=(0, 6))
    bar_id = meter.create_rectangle(0, 0, 0, 6, fill="#4CAF50", outline="")

    body = tk.Label(root, textvariable=text_var, fg="#cfcfcf", bg="#1e1e1e",
                    font=("Segoe UI", 11), wraplength=width - 24,
                    justify="left", anchor="w")
    body.pack(fill="x", padx=12, pady=(0, 12))

    def _set_status(s: str) -> None:
        status_var.set(s)
        color = {"Recording": "#F44336",
                 "Transcribing": "#FFC107",
                 "Ready": "#4CAF50"}.get(s, "#9E9E9E")
        dot.itemconfig(dot_id, fill=color)
        if s == "Transcribing":
            hint_var.set("working on the final transcription...")
        elif s == "Ready":
            hint_var.set("copied to clipboard")
        else:
            hint_var.set("toggle hotkey to stop")

    def _set_level(rms_norm: float) -> None:
        w = max(0.0, min(1.0, rms_norm)) * (width - 24)
        meter.coords(bar_id, 0, 0, w, 6)
        if rms_norm > 0.85: meter.itemconfig(bar_id, fill="#F44336")
        elif rms_norm > 0.5: meter.itemconfig(bar_id, fill="#FFC107")
        else:                meter.itemconfig(bar_id, fill="#4CAF50")

    def _set_text(s: str, *, partial: bool = True) -> None:
        body.configure(fg="#9a9a9a" if partial else "#f0f0f0")
        text_var.set(s if s else ("listening..." if partial else ""))

    def _reposition() -> None:
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        h = root.winfo_reqheight()
        x = (sw - width) // 2
        y = sh - h - 80
        root.geometry(f"{width}x{h}+{x}+{y}")

    def _show() -> None:
        _set_status("Recording"); _set_text("", partial=True); _set_level(0)
        _reposition()
        root.deiconify()
        root.attributes("-topmost", True)

    def _pump() -> None:
        try:
            while True:
                cmd = popup_cmds.get_nowait()
                op = cmd[0]
                if op == "show":      _show()
                elif op == "hide":    root.withdraw()
                elif op == "status":  _set_status(cmd[1])
                elif op == "level":   _set_level(cmd[1])
                elif op == "partial": _set_text(cmd[1], partial=True)
                elif op == "final":   _set_text(cmd[1], partial=False); _reposition()
        except queue.Empty:
            pass
        root.after(50, _pump)

    root.after(50, _pump)
    try:
        root.mainloop()
    except Exception:
        logger.exception("Popup mainloop crashed.")


def _popup(*cmd) -> None:
    popup_cmds.put(cmd)


# ─── Streaming partial transcription ──────────────────────────────────────────
def _frames_to_float32(frames: list[bytes]) -> np.ndarray:
    raw = b"".join(frames)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _streaming_loop() -> None:
    """Periodically transcribe the rolling tail of the audio buffer for
    live preview. CRITICAL: take model_lock with a non-blocking acquire,
    because PyTorch's CPU backend is NOT thread-safe and concurrent calls
    to model.transcribe() produce a torch_cpu.dll access violation."""
    global _last_partial_frame_count
    last_run = 0.0
    while not _stream_stop.is_set():
        time.sleep(0.08)
        if not audio_frames:
            _popup("level", 0.0)
            continue
        # Animate the meter every ~80 ms so it feels alive.
        try:
            tail = audio_frames[-1]
            arr = np.frombuffer(tail, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(arr * arr)))
            _popup("level", min(1.0, rms * 4.0))
        except Exception:
            pass

        now = time.time()
        if now - last_run < STREAM_INTERVAL_SECONDS:
            continue
        new_frames = len(audio_frames) - _last_partial_frame_count
        if new_frames * CHUNK / SAMPLE_RATE < STREAM_MIN_NEW_AUDIO_SECONDS:
            continue
        last_run = now
        _last_partial_frame_count = len(audio_frames)

        try:
            window_chunks = int(STREAM_WINDOW_SECONDS * SAMPLE_RATE / CHUNK)
            audio = _frames_to_float32(audio_frames[-window_chunks:])
            if audio.size < SAMPLE_RATE // 2:
                continue
            if model is None:
                continue
            if not model_lock.acquire(blocking=False):
                continue
            try:
                t0 = time.time()
                result = model.transcribe(
                    audio, language=language, fp16=WHISPER_FP16,
                    condition_on_previous_text=False,
                    no_speech_threshold=0.5,
                )
            finally:
                model_lock.release()
            text_p = (result.get("text") or "").strip()
            _popup("partial", text_p)
            logger.debug("partial (%.0f ms): %s",
                         (time.time() - t0) * 1000, text_p[:80])
        except Exception:
            logger.exception("Streaming partial failed (continuing).")


# ─── Recording ────────────────────────────────────────────────────────────────
def start_recording() -> None:
    global recording, audio_frames, foreground_token, recording_started_at
    global _last_partial_frame_count, _stream_stop, status_text

    with recording_lock:
        if recording:
            return
        if model is None:
            logger.warning("Record requested but model not loaded yet.")
            return
    # Pause the wake-word listener while a recording is in progress
    # so it can't fire again on whatever the user is currently saying.
    if wake_detector is not None:
        try: wake_detector.pause()
        except Exception: logger.exception("wake_detector.pause raised.")

    # Wake-event extras: sound + silence auto-stop. Both gated on
    # _recording_was_wake_triggered so the hotkey path keeps its
    # exact previous behaviour.
    #
    # NEW (v0.6.5): the cue plays BEFORE the recording mic opens,
    # then we sleep `wake_sound_lead_s` seconds before opening the
    # stream. By default the cue starts playing through speakers
    # 1.0 s before the mic goes live; that 1.0 s of cue is NEVER
    # captured in the recording, so the user gets a clean
    # "I heard you" confirmation followed by a clean recording.
    # WAKE_SOUND_GRACE_S still suppresses any cue tail that bleeds
    # past the lead-in.
    #
    # CRITICAL: the audio_frames reset + state init runs for EVERY
    # entry to start_recording (was: only inside the wake-only
    # branch in v0.6.4, which broke hotkey recordings).
    wake_triggered = _recording_was_wake_triggered
    if wake_triggered:
        try:
            cfg_now = _cfg.load()
            lead_s = float(cfg_now.get("wake_sound_lead_s", 1.0))
        except Exception:
            lead_s = 1.0
        threading.Thread(target=_play_wake_sound, daemon=True,
                         name="wake-cue").start()
        if lead_s > 0:
            time.sleep(min(5.0, max(0.0, lead_s)))

    recording = True
    audio_frames = []
    _last_partial_frame_count = 0
    recording_started_at = datetime.now()
    foreground_token = _plat.get_foreground_window()
    logger.info("Recording started. focus_token=%s", foreground_token)

    status_text = "Recording..."
    _update_icon_tooltip()
    _update_tray_icon("red")
    _popup("show")
    _popup("status", "Recording")

    pa = pyaudio.PyAudio()
    stream = pa.open(format=FORMAT, channels=CHANNELS, rate=SAMPLE_RATE,
                     input=True, frames_per_buffer=CHUNK)

    def _record_loop(_pa, _stream):
        global recording
        start_time = time.time()
        # Wake-trigger silence-auto-stop state. These are initialised
        # for every recording (wake or hotkey); the silence block
        # below only reads them when wake_triggered is True so the
        # hotkey path is unaffected.
        _last_voice_time = start_time
        _last_silence_log = 0.0
        _wake_voice_baseline_rms = 0.0
        try:
            while recording:
                if time.time() - start_time > MAX_RECORD_SECONDS:
                    logger.warning("Max record time reached; auto-stopping.")
                    threading.Thread(target=stop_recording, daemon=True).start()
                    break

                # Wake-trigger silence auto-stop. Only enabled when this
                # recording was started by the wake-word listener; the
                # hotkey path keeps its old "stop only when the user
                # toggles" behaviour.
                #
                # Why this is more involved than just "RMS < threshold
                # for N seconds":
                #
                #   1. Static defaults are wrong for most rooms. Empirically
                #      the user's ambient floor was 0.03-0.05 RMS while the
                #      static default was 0.010 -- so during "silent"
                #      pauses every chunk's RMS still exceeded the
                #      threshold and the recording never auto-stopped.
                #
                #   2. Adaptive baseline: we sample the first 1.0 s of the
                #      recording as a voice-volume baseline. The user just
                #      said the wake phrase, so that 1 s captures their
                #      actual speaking volume. Silence threshold becomes
                #      max(static_threshold, baseline * SILENCE_RATIO),
                #      auto-tuning to mic gain + room noise.
                #
                #   3. We log a "silence countdown" line every ~1 s at
                #      INFO level so the user can watch the auto-stop
                #      progress in daemon.log without enabling DEBUG.
                if wake_triggered:
                    try:
                        cfg_now = _cfg.load()
                        silence_stop_s = float(
                            cfg_now.get("wake_silence_stop_s", 3.0))
                        silence_rms_floor = float(
                            cfg_now.get("wake_silence_rms_threshold",
                                        0.030))
                    except Exception:
                        silence_stop_s, silence_rms_floor = 3.0, 0.030

                    # Grace period: skip silence checks AND voice-baseline
                    # sampling for the first WAKE_SOUND_GRACE_S of the
                    # recording. The wake-startup sound is playing through
                    # the speakers during this window; the mic is hearing
                    # both the user and the cue, and we don't want either
                    # to corrupt the silence-stop logic. _last_voice_time
                    # is bumped to NOW so when the grace expires, the
                    # silence countdown starts fresh.
                    elapsed = time.time() - start_time
                    if elapsed < WAKE_SOUND_GRACE_S:
                        _last_voice_time = time.time()
                        # Note: not break/continue here; we still need
                        # to fall through to the audio-read at the
                        # bottom of the loop body below.
                    elif silence_stop_s > 0 and len(audio_frames) > 0:
                        tail = audio_frames[-1]
                        arr = (np.frombuffer(tail, dtype=np.int16)
                                 .astype(np.float32) / 32768.0)
                        rms_now = (float(np.sqrt(np.mean(arr * arr)))
                                   if arr.size else 0.0)

                        # Build the voice-volume baseline from the
                        # SECOND second of the recording (chunks
                        # 16-31 inclusive at 16 kHz / 1024-sample
                        # chunks). The first second can be silence
                        # because the user just said the wake phrase
                        # and is listening for the cue / waiting to
                        # speak. Sampling that as "voice baseline"
                        # gives near-zero RMS and the silence-stop
                        # fires immediately. Sampling second-2
                        # captures the user's actual speech.
                        chunks_per_sec = max(1, SAMPLE_RATE // CHUNK)
                        baseline_start = chunks_per_sec
                        baseline_end = chunks_per_sec * 2
                        if (_wake_voice_baseline_rms == 0.0
                                and len(audio_frames) >= baseline_end):
                            sample = audio_frames[baseline_start:baseline_end]
                            sample_arr = (np.frombuffer(b"".join(sample),
                                            dtype=np.int16)
                                            .astype(np.float32) / 32768.0)
                            if sample_arr.size:
                                _wake_voice_baseline_rms = float(
                                    np.sqrt(np.mean(sample_arr * sample_arr)))
                                logger.info(
                                    "wake-stop: voice baseline rms=%.3f "
                                    "(threshold floor=%.3f, ratio=%.2f -> "
                                    "effective threshold=%.3f) "
                                    "[sampled chunks %d-%d]",
                                    _wake_voice_baseline_rms,
                                    silence_rms_floor,
                                    WAKE_SILENCE_RATIO,
                                    max(silence_rms_floor,
                                        _wake_voice_baseline_rms
                                        * WAKE_SILENCE_RATIO),
                                    baseline_start, baseline_end)

                        # Pick the larger of the static floor and the
                        # baseline-relative threshold. If baseline isn't
                        # ready yet, just use the floor.
                        if _wake_voice_baseline_rms > 0.0:
                            effective_thresh = max(
                                silence_rms_floor,
                                _wake_voice_baseline_rms * WAKE_SILENCE_RATIO)
                        else:
                            effective_thresh = silence_rms_floor

                        if rms_now >= effective_thresh:
                            _last_voice_time = time.time()
                        else:
                            silent_for = time.time() - _last_voice_time
                            # Periodic INFO heartbeat so the log shows
                            # the countdown progressing. Rate-limited
                            # to once per second to avoid spam.
                            if (time.time() - _last_silence_log
                                    >= 1.0):
                                logger.info(
                                    "wake-stop: silent for %.1fs / "
                                    "%.1fs (rms=%.3f thresh=%.3f)",
                                    silent_for, silence_stop_s,
                                    rms_now, effective_thresh)
                                _last_silence_log = time.time()
                            if silent_for >= silence_stop_s:
                                logger.info(
                                    "wake-stop: %.1fs of silence "
                                    "reached; auto-stopping recording.",
                                    silence_stop_s)
                                threading.Thread(target=stop_recording,
                                                 daemon=True).start()
                                break
                try:
                    audio_frames.append(_stream.read(CHUNK,
                                                     exception_on_overflow=False))
                except Exception:
                    logger.exception("Audio read error.")
                    break
        finally:
            try: _stream.stop_stream(); _stream.close()
            except Exception: pass
            try: _pa.terminate()
            except Exception: pass

    threading.Thread(target=_record_loop, args=(pa, stream),
                     daemon=True, name="recorder").start()
    _stream_stop = threading.Event()
    threading.Thread(target=_streaming_loop, daemon=True,
                     name="streamer").start()


def stop_recording() -> None:
    global recording, audio_frames, status_text

    with recording_lock:
        if not recording:
            return
        recording = False
        status_text = "Transcribing..."
        _update_icon_tooltip()
        _update_tray_icon("yellow")

    _stream_stop.set()
    _popup("status", "Transcribing")
    _popup("level", 0.0)
    logger.info("Recording stopped (%d frames). Final transcription...",
                len(audio_frames))

    if not audio_frames:
        _popup("final", "(no audio captured)")
        time.sleep(0.6); _popup("hide")
        status_text = "Ready"; _update_icon_tooltip(); _update_tray_icon("green")
        return

    tmp_path = None
    text = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(CHANNELS); wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE); wf.writeframes(b"".join(audio_frames))

        with model_lock:
            # Pass an in-memory ndarray instead of the tmp WAV path. Whisper's
            # transcribe(path) calls whisper.audio.load_audio(path) which
            # shells out to ffmpeg, and on pythonw.exe that subprocess
            # briefly flashes a console window. We already have the raw
            # audio as int16 frames; converting to float32 normalised in
            # Python skips the redundant ffmpeg decode AND eliminates the
            # console flash. The tmp WAV file is still written (the
            # audio archive depends on it).
            audio_np = _frames_to_float32(audio_frames)
            result = model.transcribe(audio_np, language=language,
                                       fp16=WHISPER_FP16)
        text = (result.get("text") or "").strip()

        # Persist a copy of the audio + the transcribed text. Best-effort.
        try:
            duration_s = len(audio_frames) * CHUNK / float(SAMPLE_RATE)
            cfg = _cfg.load()
            archive_dir = cfg.get("archive_dir") or default_archive_dir()
            archive_recording(
                archive_dir=Path(archive_dir),
                pcm_bytes=b"".join(audio_frames),
                sample_rate=SAMPLE_RATE,
                channels=CHANNELS,
                sample_width_bytes=2,
                text=text,
                model_name=current_model_name or "unknown",
                started_at=recording_started_at or datetime.now(),
                duration_s=duration_s,
            )
        except Exception:
            logger.exception("archive_recording raised; ignoring.")

        if text:
            try: pyperclip.copy(text)
            except Exception: logger.exception("Clipboard copy failed.")
            logger.info("Transcribed (%d chars). autopaste=%s aim=%s",
                        len(text), autopaste_enabled, agent_input_mode)
            _popup("final", text); _popup("status", "Ready")

            # Agent Input Mode (AIM): if a target app is configured,
            # raise its window to the foreground first, then paste, then Enter.
            # If AIM is 'off', behave like the previous build.
            # If AIM is 'auto', paste + Enter into whatever is focused.
            target_hwnd = foreground_token
            aim = agent_input_mode or 'off'
            if aim not in ('off', 'auto'):
                # Specific-app target. Locate-and-focus its main window
                # via the verified focus dance (AttachThreadInput + retry
                # + GetForegroundWindow polling). On success we get the
                # HWND back so paste_into_window can re-verify focus
                # right before pumping Ctrl+V; that closes the race
                # window where Electron apps flash the title bar but
                # don't actually transfer focus to the input field.
                target_hwnd = _aim.activate_target(aim, timeout_s=1.0)
                if not target_hwnd:
                    logger.warning("AIM target %r could not be activated; "
                                   "falling back to 'auto' for this dictation.", aim)
                    # Fall back to whatever has focus right now so the
                    # user still gets their text pasted somewhere
                    # sensible (typically their previous window).
                    target_hwnd = foreground_token

            # When activate_target() handled focus (specific-app AIM path),
            # tell paste_into_window NOT to re-run focus_window. The
            # second AttachThreadInput + SetForegroundWindow would clobber
            # whatever inner WebContents focus the post_activate hook
            # established (UIA SetFocus on the chat input, in particular).
            aim_handled_focus = (
                aim not in ('off', 'auto')
                and target_hwnd != foreground_token
            )
            pasted = False
            if (autopaste_enabled or aim != 'off') and (target_hwnd or aim != 'off'):
                try:
                    pasted = bool(_plat.paste_into_window(
                        target_hwnd,
                        already_focused=aim_handled_focus,
                    ))
                except Exception:
                    logger.exception("Auto-paste failed; text on clipboard.")

            if aim != 'off' and pasted:
                # AIM always finishes with Enter so the message/prompt sends.
                # 120 ms settle gives Electron / Chromium input handlers
                # time to commit the pasted text before the Return chord
                # arrives; without this delay Slack and Discord
                # occasionally send an empty message because Ctrl+V's
                # internal commit and the Enter event interleave.
                time.sleep(0.12)
                try:
                    _aim.send_enter()
                except Exception:
                    logger.exception("AIM send_enter failed.")
            elif aim != 'off' and not pasted:
                logger.warning("AIM: paste did not land (focus did not "
                               "transfer to %r); suppressing Enter. Text "
                               "remains on the clipboard.", aim)
        else:
            _popup("final", "(no speech detected)"); _popup("status", "Ready")
            logger.info("No speech detected.")
    except Exception:
        logger.exception("Final transcription failed.")
        _popup("final", "(transcription error - see daemon.log)")
        _popup("status", "Ready")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except OSError: pass

    time.sleep(1.2); _popup("hide")
    status_text = "Ready"; _update_icon_tooltip(); _update_tray_icon("green")
    # Resume the wake-word listener now that the recording's done.
    if wake_detector is not None:
        try: wake_detector.resume()
        except Exception: logger.exception("wake_detector.resume raised.")
    # Reset the wake-trigger flag so the next recording's source is
    # accurately classified.
    global _recording_was_wake_triggered
    _recording_was_wake_triggered = False


def toggle_recording() -> None:
    if recording: stop_recording()
    else:         start_recording()


# ─── Tray menu ────────────────────────────────────────────────────────────────
def _make_model_switcher(name: str):
    def _click(icon_ref, item):
        if name == current_model_name: return
        threading.Thread(target=load_model, args=(name,), daemon=True).start()
    return _click


def _is_current_model(name: str):
    return lambda item: current_model_name == name


def _toggle_autopaste(icon_ref, item) -> None:
    global autopaste_enabled
    autopaste_enabled = not autopaste_enabled
    _cfg.update(autopaste=autopaste_enabled)
    _update_icon_tooltip()
    if icon_ref is not None:
        icon_ref.menu = _build_tray_menu()
        icon_ref.update_menu()
    logger.info("autopaste -> %s", autopaste_enabled)


def _is_autopaste(item) -> bool:
    return autopaste_enabled


from dictado.config import HOTKEY_PRESETS, parse_hotkey

def _format_hotkey(spec: str) -> str:
    """Pretty-print 'alt+t' as 'Alt+T' for the tray-menu label."""
    parts = spec.split('+')
    return '+'.join(p[:1].upper() + p[1:] if len(p) > 1 else p.upper() for p in parts)

def _set_hotkey(spec: str) -> bool:
    """Try to rebind the global hotkey live and persist on success.

    Returns True if the rebind worked, False otherwise (e.g. malformed
    spec or the combo is already taken by another app -- in which case
    the previous binding is left in place)."""
    global current_hotkey_spec
    try:
        parse_hotkey(spec)  # validate before we touch the OS
    except ValueError as e:
        logger.warning('rejected hotkey spec %r: %s', spec, e)
        return False
    if hotkey_handle is None:
        return False
    hotkey_handle.rebind(spec)
    current_hotkey_spec = spec
    _cfg.update(hotkey=spec)
    _update_icon_tooltip()
    if icon is not None:
        icon.menu = _build_tray_menu()
        icon.update_menu()
    logger.info('hotkey rebound -> %s', spec)
    return True

def _make_hotkey_setter(spec: str):
    def _click(icon_ref, item):
        threading.Thread(target=_set_hotkey, args=(spec,), daemon=True).start()
    return _click

def _is_current_hotkey(spec: str):
    return lambda item: current_hotkey_spec == spec

def _prompt_custom_hotkey(icon_ref, item) -> None:
    """Pop a tiny tk dialog asking for a custom hotkey string.

    Runs on its own thread so the tray callback returns instantly. The
    dialog stays foreground only long enough to grab the user's input,
    then we rebind via _set_hotkey()."""
    def _run():
        try:
            import tkinter as tk
            from tkinter import simpledialog
        except Exception:
            logger.exception('tk not available; cannot prompt for hotkey.')
            return
        root = tk.Tk()
        root.withdraw()
        spec = simpledialog.askstring(
            'dictado - set hotkey',
            'Hotkey (e.g. alt+t, ctrl+shift+v, win+h):',
            parent=root, initialvalue=current_hotkey_spec)
        root.destroy()
        if spec:
            ok = _set_hotkey(spec.strip().lower())
            if not ok:
                logger.warning('custom hotkey %r was rejected', spec)
    threading.Thread(target=_run, daemon=True).start()

def _set_agent_input(mode: str) -> None:
    """Switch the AIM target. mode is 'off', 'auto', or an app id.

    Persisted to config.json. The submenu is rebuilt so the radio
    indicator follows the new selection without needing a restart."""
    global agent_input_mode
    agent_input_mode = mode
    _cfg.update(agent_input=mode)
    _update_icon_tooltip()
    if icon is not None:
        icon.menu = _build_tray_menu()
        icon.update_menu()
    logger.info("agent_input_mode -> %s", mode)

def _make_aim_setter(mode: str):
    def _click(icon_ref, item):
        threading.Thread(target=_set_agent_input, args=(mode,),
                         daemon=True).start()
    return _click

def _is_aim(mode: str):
    return lambda item: agent_input_mode == mode

def _build_aim_submenu() -> Menu:
    """Build the AIM submenu live so newly-launched apps appear without
    a daemon restart. Menu items: Off (default), Auto (paste+Enter into
    focused window), then one entry per detected app."""
    items = [MenuItem('Off',  _make_aim_setter('off'),
                      checked=_is_aim('off'),  radio=True),
             MenuItem('Auto (focused window)',
                      _make_aim_setter('auto'), checked=_is_aim('auto'), radio=True),
             Menu.SEPARATOR]
    detected = _aim.detect_apps()
    if not detected:
        items.append(MenuItem('No supported apps detected', None, enabled=False))
    else:
        for app in detected:
            items.append(MenuItem(app.label, _make_aim_setter(app.id),
                                  checked=_is_aim(app.id), radio=True))
    return Menu(*items)

def _on_wake_phrase_detected(matched_text: str) -> None:
    """Called by the wake_word detector thread when a phrase fires.
    Sets _recording_was_wake_triggered so start_recording knows to
    play the wake sound and arm the silence auto-stop, then hands
    off to start_recording() on a fresh thread."""
    global _recording_was_wake_triggered
    logger.info("Wake-word triggered start_recording (matched: %r).",
                matched_text)
    _recording_was_wake_triggered = True
    threading.Thread(target=start_recording, daemon=True,
                     name="wake-start").start()


def _start_wake_detector_async() -> None:
    """Build + start the WakeWordDetector on a daemon thread so the
    initial whisper.load_model('tiny.en') doesn't block the tray."""
    global wake_detector
    cfg = _cfg.load()
    phrases = cfg.get("wake_word_phrases") or []
    try:
        rx = (_wake.build_user_wake_regex(phrases)
              if phrases else _wake.build_default_wake_regex())
    except Exception:
        logger.exception("Bad wake_word_phrases; using default regex.")
        rx = _wake.build_default_wake_regex()
    wake_detector = _wake.WakeWordDetector(
        on_wake=_on_wake_phrase_detected,
        wake_regex=rx,
    )
    threading.Thread(target=wake_detector.start, daemon=True,
                     name="wake-bootstrap").start()


def _stop_wake_detector() -> None:
    global wake_detector
    if wake_detector is None:
        return
    try: wake_detector.stop()
    except Exception: logger.exception("wake_detector.stop raised.")
    wake_detector = None


def _toggle_wake_word(icon_ref, item) -> None:
    global wake_word_enabled
    wake_word_enabled = not wake_word_enabled
    _cfg.update(wake_word_enabled=wake_word_enabled)
    if wake_word_enabled:
        _start_wake_detector_async()
    else:
        _stop_wake_detector()
    if icon_ref is not None:
        icon_ref.menu = _build_tray_menu()
        icon_ref.update_menu()
    logger.info("wake_word_enabled -> %s", wake_word_enabled)


def _is_wake_word_enabled(item) -> bool:
    return wake_word_enabled


def _build_tray_menu() -> Menu:
    model_items = [MenuItem(_models.display_for(name),
                            _make_model_switcher(name),
                            checked=_is_current_model(name),
                            radio=True)
                   for name in SELECTABLE_MODELS]
    return Menu(
        MenuItem("Record / Stop",
                 lambda icon_ref, item: threading.Thread(
                     target=toggle_recording, daemon=True).start(),
                 default=True),
        Menu.SEPARATOR,
        MenuItem("Model", Menu(*model_items)),
        MenuItem(f'Hotkey  ({_format_hotkey(current_hotkey_spec)})',
                 Menu(*[MenuItem(_format_hotkey(p),
                                 _make_hotkey_setter(p),
                                 checked=_is_current_hotkey(p),
                                 radio=True)
                        for p in HOTKEY_PRESETS],
                      Menu.SEPARATOR,
                      MenuItem('Set custom...', _prompt_custom_hotkey)),
                 ),
        MenuItem("Auto-paste after transcription",
                 _toggle_autopaste, checked=_is_autopaste),
        MenuItem(f'Agent Input Mode  ({_aim.app_label_for(agent_input_mode)})',
                 _build_aim_submenu()),
        MenuItem('Voice activation ("Hey Bijou" / "Hey Biboo" ...)',
                 _toggle_wake_word, checked=_is_wake_word_enabled),
        Menu.SEPARATOR,
        MenuItem("Quit", _quit_daemon),
    )


def _quit_daemon(icon_ref=None, item=None) -> None:
    global recording
    logger.info("Quit requested.")
    _stream_stop.set()
    if recording: stop_recording()
    if icon is not None: icon.stop()
    sys.exit(0)


# ─── Trigger-file IPC ─────────────────────────────────────────────────────────
def _trigger_loop() -> None:
    """Poll <state_dir>/trigger/ for files and act on them.

    Recognized file names (the file's CONTENTS are ignored):
      toggle              -> toggle_recording()
      quit                -> exit cleanly
      switch.<modelname>  -> swap model

    This replaces the old TCP listener so the daemon doesn't need to bind
    a socket, which used to flag on EDR products that dislike unsigned
    interpreters with localhost listeners.
    """
    trig = _cfg.trigger_dir()
    while True:
        time.sleep(TRIGGER_POLL_SECONDS)
        try:
            entries = os.listdir(trig)
        except OSError:
            continue
        for name in entries:
            try: (trig / name).unlink()
            except OSError: continue
            logger.info("Trigger received: %r", name)
            if name == "toggle":
                threading.Thread(target=toggle_recording, daemon=True).start()
            elif name == "quit":
                threading.Thread(target=_quit_daemon, daemon=True).start()
            elif name.startswith("switch."):
                m = name.split(".", 1)[1]
                if _models.is_known(m):
                    threading.Thread(target=load_model, args=(m,),
                                     daemon=True).start()


def _write_trigger(name: str) -> int:
    try:
        trig = _cfg.trigger_dir()
        trig.mkdir(parents=True, exist_ok=True)
        (trig / name).write_text("", encoding="utf-8")
        return 0
    except OSError as e:
        sys.stderr.write(f"Could not write trigger {name!r}: {e}\n")
        return 1


# ─── Entry point ──────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dictado",
        description="Local voice-activated and push-to-talk voice-to-text daemon.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--toggle",       action="store_true",
                   help="Tell a running daemon to start/stop recording.")
    g.add_argument("--switch-model", metavar="NAME",
                   help="Tell a running daemon to load a different model.")
    g.add_argument("--quit",         action="store_true",
                   help="Tell a running daemon to exit.")
    g.add_argument("--install-autostart", action="store_true",
                   help="Install the OS-native autostart entry and exit.")
    g.add_argument("--uninstall-autostart", action="store_true",
                   help="Remove the OS-native autostart entry and exit.")
    return p.parse_args()


def main() -> None:
    # Suppress brief console flashes from any subprocess child
    # (whisper's ffmpeg decode, agent_input launch_target, etc.)
    # before any subprocess work happens. No-op on non-Windows.
    _suppress_subprocess_consoles_on_windows()
    global icon, autopaste_enabled, popup_enabled, language

    args = parse_args()

    # IPC shims first - they don't load whisper.
    if args.toggle:        sys.exit(_write_trigger("toggle"))
    if args.switch_model:  sys.exit(_write_trigger(f"switch.{args.switch_model}"))
    if args.quit:          sys.exit(_write_trigger("quit"))
    if args.install_autostart:
        py = sys.executable
        script = str(Path(sys.modules["__main__"].__file__).resolve())
        _plat.install_autostart(py, script); print("ok"); return
    if args.uninstall_autostart:
        _plat.uninstall_autostart(); print("ok"); return

    # Daemon mode.
    cfg = _cfg.load()
    global agent_input_mode
    autopaste_enabled = bool(cfg.get("autopaste", True))
    agent_input_mode = cfg.get("agent_input", "off") or "off"
    global wake_word_enabled
    wake_word_enabled = bool(cfg.get("wake_word_enabled", False))
    popup_enabled     = bool(cfg.get("popup", True))
    language          = cfg.get("language", "en")
    initial_model     = cfg.get("model", "medium")

    logger.info("Daemon starting (model=%r autopaste=%s popup=%s language=%r).",
                initial_model, autopaste_enabled, popup_enabled, language)

    threading.Thread(target=load_model, args=(initial_model,),
                     daemon=True, name="loader").start()
    global hotkey_handle, current_hotkey_spec
    current_hotkey_spec = cfg.get("hotkey", "alt+t")
    hotkey_handle = _plat.register_hotkey(toggle_recording, current_hotkey_spec)
    threading.Thread(target=_trigger_loop, daemon=True, name="trigger").start()
    threading.Thread(target=_popup_loop,   daemon=True, name="popup").start()

    # Bring the wake-word detector up if the user had it enabled
    # at last shutdown. Off by default, so this is a no-op for users
    # who haven't toggled it on. The detector loads tiny.en lazily on
    # its own thread so daemon startup isn't blocked.
    if wake_word_enabled:
        _start_wake_detector_async()

    icon = Icon("dictado", _create_icon_image("gray"),
                title="Dictado - starting...", menu=_build_tray_menu())
    try:
        logger.info("Tray loop starting.")
        icon.run()
    finally:
        logger.info("Daemon exited.")


if __name__ == "__main__":
    main()
