"""Audio archive + rolling Markdown transcript log.

Why this module exists
----------------------
The user wants a permanent record of every dictation session: the WAV file
itself plus a Markdown log noting timestamp, model, duration, and the text
that came back. The folder of recordings is intentionally OUTSIDE the
dictado state dir so it can sit in the user's normal Documents tree
(e.g. ~/Documents/Sound Recordings) and so it's NEVER part of the git
repo for this project — these files contain the user's actual voice and
must not be checked in.

Defaults (overridable via config.json -> "archive_dir"):
  * Windows : %USERPROFILE%\Documents\Sound Recordings
              (Documents is normally redirected to OneDrive, so this
              picks up the redirection automatically.)
  * macOS   : ~/Documents/Sound Recordings
  * Linux   : ~/Documents/Sound Recordings  (XDG_DOCUMENTS_DIR if set)

What gets written per recording
-------------------------------
Inside the archive directory:

  <YYYYMMDD-HHMMSS>__<firstword>__<lastword>.wav
      The audio file. 16 kHz mono PCM. The two words are pulled from the
      transcribed text and slug-sanitised. Empty or punctuation-only
      transcriptions fall back to "audio".

  AudioTranscriptions_<YYYYMMDD>_to_<YYYYMMDD>.md
      A rolling weekly log file. Each entry is a Markdown table row with
      timestamp, duration, model, file name, and the transcription text.
      A new log file is created when the previous one's start date is
      more than 7 days old at the time of a new entry.

Both files are written atomically: the WAV via temp-file + os.replace, the
Markdown via append (we re-read, append, write whole file, then rename;
small enough that the cost is irrelevant).

What gets EXCLUDED from git
---------------------------
The .gitignore at repo root explicitly excludes:
    Sound Recordings/
    AudioTranscriptions_*.md
    *.wav   *.mp3   *.flac   *.m4a   *.ogg
so even if you point archive_dir at a path inside the repo you won't
accidentally commit your voice memos.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("dictado.archive")

# A rolling-window length, in days, before a new transcript log file rolls.
LOG_ROLL_DAYS = 7

# Slug-friendly regex: anything not [A-Za-z0-9_-] -> dropped.
_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def default_archive_dir() -> Path:
    r"""Return the OS-appropriate default archive directory.

    On Windows we ask the shell for the Documents path so OneDrive
    redirection is honored automatically (`SHGetKnownFolderPath` /
    `KNOWNFOLDERID_Documents`). On other platforms we use ~/Documents.
    """
    if sys.platform == "win32":
        # Documents may be redirected (OneDrive, network share, etc.).
        try:
            import ctypes
            from ctypes import wintypes
            FOLDERID_Documents = ctypes.c_byte * 16
            # Documents = {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            guid = (ctypes.c_byte * 16)(*[
                0xD0, 0x9A, 0xD3, 0xFD, 0x8F, 0x23, 0xAF, 0x46,
                0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7,
            ])
            buf = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                         ctypes.byref(buf))
            base = Path(buf.value) if buf.value else Path.home() / "Documents"
        except Exception:
            base = Path.home() / "Documents"
    else:
        # Honor XDG_DOCUMENTS_DIR if set (linux), else fall back.
        xdg = os.environ.get("XDG_DOCUMENTS_DIR")
        base = Path(xdg) if xdg else Path.home() / "Documents"
    return base / "Sound Recordings"


def _slug(word: str, fallback: str = "audio") -> str:
    """Tame a single word for use in a filename. Lowercase, alphanumeric+dash."""
    if not word:
        return fallback
    cleaned = _SLUG_RE.sub("", word.strip().lower())
    return cleaned or fallback


def _first_last_words(text: str) -> tuple[str, str]:
    """Return (first_word, last_word) from a transcription string.

    Strips punctuation. Returns ('audio', 'audio') on empty input."""
    if not text:
        return "audio", "audio"
    tokens = re.findall(r"[A-Za-z0-9']+", text)
    if not tokens:
        return "audio", "audio"
    return tokens[0], tokens[-1]


def _week_window_for(when: datetime) -> tuple[datetime, datetime]:
    """Return (week_start, week_end_exclusive) covering `when`. Weeks roll
    every LOG_ROLL_DAYS days, anchored at `when`'s 00:00 local time. We
    keep the rolling-window naming the user asked for (start..end in the
    file name)."""
    start = when.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=LOG_ROLL_DAYS)
    return start, end


def _log_file_for(archive_dir: Path, when: datetime) -> Path:
    """Find the most recent active log file in archive_dir whose window
    still contains `when`. Otherwise create a new one starting today."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    # File name pattern: AudioTranscriptions_20260521_to_20260528.md
    for p in sorted(archive_dir.glob("AudioTranscriptions_*_to_*.md"),
                    reverse=True):
        m = re.match(r"AudioTranscriptions_(\d{8})_to_(\d{8})\.md$", p.name)
        if not m:
            continue
        try:
            start = datetime.strptime(m.group(1), "%Y%m%d")
            end   = datetime.strptime(m.group(2), "%Y%m%d")
        except ValueError:
            continue
        if start <= when < end:
            return p
    # No active log file -> create today's window.
    start, end = _week_window_for(when)
    name = f"AudioTranscriptions_{start.strftime('%Y%m%d')}_to_{end.strftime('%Y%m%d')}.md"
    p = archive_dir / name
    if not p.exists():
        p.write_text(_log_header(start, end), encoding="utf-8")
    return p


def _log_header(start: datetime, end: datetime) -> str:
    """Header for a freshly created weekly log file."""
    return (
        f"# Audio Transcriptions  ({start:%Y-%m-%d}  to  {(end - timedelta(days=1)):%Y-%m-%d})\n"
        f"\n"
        f"_Auto-generated by dictado. Do NOT commit this file or the "
        f"WAVs in this directory; they contain personal audio._\n"
        f"\n"
        f"| Timestamp | Duration | Model | File | Transcription |\n"
        f"|---|---|---|---|---|\n"
    )


def _md_escape(s: str) -> str:
    """Pipes break Markdown table rows; escape them. Newlines folded to space."""
    return s.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def archive_recording(
    archive_dir: Path | None,
    pcm_bytes: bytes,
    sample_rate: int,
    channels: int,
    sample_width_bytes: int,
    text: str,
    model_name: str,
    started_at: datetime,
    duration_s: float,
) -> Path | None:
    """Write the WAV and append to the rolling log. Returns the WAV path.

    Failure is non-fatal: any error is logged and `None` is returned. The
    daemon must keep working even if the archive disk is full / unmounted.
    """
    if archive_dir is None:
        archive_dir = default_archive_dir()
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create archive dir %s: %s", archive_dir, e)
        return None

    first, last = _first_last_words(text)
    ts = started_at.strftime("%Y%m%d-%H%M%S")
    wav_name = f"{ts}__{_slug(first)}__{_slug(last)}.wav"
    wav_path = archive_dir / wav_name

    # Write the WAV. We do it via a tmp file + os.replace so a Ctrl-C
    # mid-write can't leave a half-baked file behind.
    tmp_path = wav_path.with_suffix(".wav.partial")
    try:
        with wave.open(str(tmp_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width_bytes)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        os.replace(tmp_path, wav_path)
    except OSError as e:
        logger.warning("Failed writing archive WAV %s: %s", wav_path, e)
        try: tmp_path.unlink()
        except OSError: pass
        return None

    # Append a Markdown row to the rolling log file.
    try:
        log_path = _log_file_for(archive_dir, started_at)
        row = (
            f"| {started_at.strftime('%Y-%m-%d %H:%M:%S')} "
            f"| {duration_s:.1f} s "
            f"| {_md_escape(model_name)} "
            f"| `{wav_name}` "
            f"| {_md_escape(text or '_(no speech)_')} |\n"
        )
        # Append-then-rename to be atomic on every OS we care about.
        with log_path.open("a", encoding="utf-8") as f:
            f.write(row)
    except OSError as e:
        logger.warning("Failed appending transcript log: %s", e)

    logger.info("Archived recording -> %s", wav_path)
    return wav_path
