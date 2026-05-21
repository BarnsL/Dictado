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

SELECTABLE_MODELS = ("base", "small", "medium")
TRIGGER_POLL_SECONDS = 0.25


# ─── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger("dictado")
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
        icon.title = f"dictado — {status_text} [{m}, autopaste {ap}]"


def _update_tray_icon(color: str) -> None:
    if icon is not None:
        icon.icon = _create_icon_image(color)


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
        try:
            while recording:
                if time.time() - start_time > MAX_RECORD_SECONDS:
                    logger.warning("Max record time reached; auto-stopping.")
                    threading.Thread(target=stop_recording, daemon=True).start()
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
            result = model.transcribe(tmp_path, language=language,
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
            logger.info("Transcribed (%d chars). autopaste=%s",
                        len(text), autopaste_enabled)
            _popup("final", text); _popup("status", "Ready")
            if autopaste_enabled and foreground_token:
                try:
                    _plat.paste_into_window(foreground_token)
                except Exception:
                    logger.exception("Auto-paste failed; text on clipboard.")
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

def _build_tray_menu() -> Menu:
    model_items = [MenuItem(name.capitalize(),
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
                if m in SELECTABLE_MODELS or m in ("tiny", "large"):
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
        description="Local push-to-talk voice-to-text daemon.")
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
    autopaste_enabled = bool(cfg.get("autopaste", True))
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

    icon = Icon("dictado", _create_icon_image("gray"),
                title="dictado - starting...", menu=_build_tray_menu())
    try:
        logger.info("Tray loop starting.")
        icon.run()
    finally:
        logger.info("Daemon exited.")


if __name__ == "__main__":
    main()
