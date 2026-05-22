"""Persisted user settings for the dictado daemon.

Cross-platform: stored under the OS's standard per-user data dir.

  Linux:    $XDG_DATA_HOME/dictado/   (~/.local/share/dictado)
  macOS:    ~/Library/Application Support/dictado/
  Windows:  %LOCALAPPDATA%\\dictado\\

Hotkey shape
------------
The "hotkey" config value is a string like "alt+t" or "ctrl+shift+v" --
case-insensitive, plus-separated, modifiers in any order. Recognised
modifier tokens: ctrl / control, shift, alt, win / cmd / super. The final
token is the key itself (a single letter, F1..F24, or any of the symbolic
names listed in HOTKEY_KEY_TOKENS below).

The platform adapters convert the parsed (modifiers, vk) tuple into their
native form: a Win32 RegisterHotKey + virtual-key on Windows, a pynput
canonical hotkey on macOS / Linux X11.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

DEFAULTS = {
    "model":     "medium",     # tiny | base | small | medium | large
    "autopaste": True,         # synthesize Ctrl+V after transcription
    "popup":     True,         # show the live recording popup window
    "language":  "en",         # whisper language code, or null for auto-detect
    "hotkey":    "alt+t",      # see "Hotkey shape" above
    # Agent Input Mode. "off" = clipboard + Ctrl+V, no Enter.
    # "auto" = clipboard + Ctrl+V + Enter into the focused window.
    # any other string = an app id from dictado.agent_input.APP_PROFILES
    # which is activated first, then clipboard + Ctrl+V + Enter.
    "agent_input": "off",
    # archive_dir = None means "use the OS-appropriate default"
    # (~/Documents/Sound Recordings on every platform).
    "archive_dir": None,
    # ---- Wake-word listener ---------------------------------------
    # See docs/WAKE_WORD.md for the full design + tuning guide.
    "wake_word_enabled":  False,           # opt-in. Tray-menu toggle persists this.
    "wake_word_phrases":  None,            # list[str] or None (= use default phrases)
    # ---- Wake-event extras ----------------------------------------
    # Sound played at the moment a wake-triggered recording begins.
    # Empty string = no sound. Anything supported by your platform's
    # audio decoder works (.wav, .m4a, .mp3, .flac, .ogg, ...).
    "wake_sound_path":    "",
    "wake_sound_volume":  0.7,             # 0.0 - 1.0 (non-WAV formats only)
    # Wake-triggered recordings auto-stop after this many seconds of
    # continuous silence. 0 = disable (run to MAX_RECORD_SECONDS).
    "wake_silence_stop_s":         3.0,
    # RMS below which a captured frame counts as "silent". Lower =
    # stops on quieter pauses; higher = stops only on real silence.
    "wake_silence_rms_threshold":  0.010,
}

# Convenience presets surfaced in the tray menu. The user can also pick
# "Set custom..." which opens a tiny prompt and accepts any HOTKEY_RE match.
HOTKEY_PRESETS = (
    "alt+t",
    "ctrl+shift+v",
    "ctrl+alt+space",
    "ctrl+`",
    "win+h",
)

# A modifier always before the final key token. The set is intentionally
# small; if you need something exotic (multimedia keys, scancodes), edit
# parse_hotkey() and the platform adapter in lockstep.
HOTKEY_MOD_TOKENS = {
    "ctrl": "ctrl", "control": "ctrl",
    "shift": "shift",
    "alt":  "alt",
    "win":  "win", "cmd": "win", "super": "win", "meta": "win",
}

# Special-case key names. Anything not here is treated as a single character
# (a-z, 0-9, punctuation) by the platform adapter.
HOTKEY_KEY_TOKENS = (
    "space", "enter", "return", "tab", "escape", "esc",
    "backspace", "delete", "insert", "home", "end",
    "pageup", "pagedown",
    "up", "down", "left", "right",
    *(f"f{i}" for i in range(1, 25)),
)

HOTKEY_RE = re.compile(
    r"^(?:(?:ctrl|control|shift|alt|win|cmd|super|meta)\+)*[^+\s]+$",
    re.IGNORECASE,
)


def parse_hotkey(spec: str) -> tuple[frozenset[str], str]:
    """Return (modifier_set, key_token) for a hotkey string.

    Modifier set uses the canonical tokens 'ctrl', 'shift', 'alt', 'win'.
    key_token is lowercased; for letters it's a single char, for special
    keys one of HOTKEY_KEY_TOKENS.

    Raises ValueError on a malformed string -- callers should catch and
    fall back to DEFAULTS["hotkey"].
    """
    if not spec or not HOTKEY_RE.match(spec):
        raise ValueError(f"invalid hotkey spec: {spec!r}")
    parts = [p.strip().lower() for p in spec.split("+")]
    *mods, key = parts
    canonical_mods = set()
    for m in mods:
        if m not in HOTKEY_MOD_TOKENS:
            raise ValueError(f"unknown modifier {m!r} in hotkey {spec!r}")
        canonical_mods.add(HOTKEY_MOD_TOKENS[m])
    if not key:
        raise ValueError(f"missing key in hotkey spec: {spec!r}")
    return frozenset(canonical_mods), key


def state_dir() -> Path:
    """Per-user state directory. Created on first call."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA",
                                   tempfile.gettempdir())) / "dictado"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "dictado"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME",
                                   str(Path.home() / ".local" / "share"))
                    ) / "dictado"
    base.mkdir(parents=True, exist_ok=True)
    (base / "trigger").mkdir(exist_ok=True)
    return base


def config_path() -> Path:    return state_dir() / "config.json"
def log_path()    -> Path:    return state_dir() / "daemon.log"
def trigger_dir() -> Path:    return state_dir() / "trigger"


def load() -> dict:
    """Read config.json, applying DEFAULTS for missing/invalid keys."""
    cfg = dict(DEFAULTS)
    p = config_path()
    try:
        # utf-8-sig strips a BOM if the file has one. Notepad,
        # PowerShell Set-Content -Encoding UTF8, and several
        # editors write BOMs by default; without sig-mode the
        # daemon silently fell back to defaults whenever the user
        # touched the file in such a tool.
        with p.open("r", encoding="utf-8-sig") as f:
            on_disk = json.load(f)
        m = on_disk.get("model")
        if isinstance(m, str):
            # Accept any name the model catalog recognises (or any alias).
            # We import lazily to avoid a circular dependency at module load.
            from . import models as _models
            if _models.is_known(m):
                cfg["model"] = m
        if isinstance(on_disk.get("autopaste"), bool):
            cfg["autopaste"] = on_disk["autopaste"]
        if isinstance(on_disk.get("popup"), bool):
            cfg["popup"] = on_disk["popup"]
        lang = on_disk.get("language", DEFAULTS["language"])
        if lang is None or isinstance(lang, str):
            cfg["language"] = lang
        hk = on_disk.get("hotkey")
        if isinstance(hk, str):
            try:
                parse_hotkey(hk)        # validate; ignore the parsed value
                cfg["hotkey"] = hk
            except ValueError:
                pass                    # fall back to default
        ad = on_disk.get("archive_dir")
        if ad is None or isinstance(ad, str):
            cfg["archive_dir"] = ad
        ai = on_disk.get("agent_input")
        if isinstance(ai, str) and ai:
            cfg["agent_input"] = ai
        # ---- Wake-word listener ----
        wwe = on_disk.get("wake_word_enabled")
        if isinstance(wwe, bool):
            cfg["wake_word_enabled"] = wwe
        wwp = on_disk.get("wake_word_phrases")
        if (wwp is None
            or (isinstance(wwp, list)
                and all(isinstance(x, str) for x in wwp))):
            cfg["wake_word_phrases"] = wwp
        # ---- Wake-event extras ----
        wsp = on_disk.get("wake_sound_path")
        if isinstance(wsp, str):
            cfg["wake_sound_path"] = wsp
        wsv = on_disk.get("wake_sound_volume")
        if isinstance(wsv, (int, float)) and 0.0 <= wsv <= 1.0:
            cfg["wake_sound_volume"] = float(wsv)
        wss = on_disk.get("wake_silence_stop_s")
        if isinstance(wss, (int, float)) and 0.0 <= wss <= 60.0:
            cfg["wake_silence_stop_s"] = float(wss)
        wsr = on_disk.get("wake_silence_rms_threshold")
        if isinstance(wsr, (int, float)) and 0.0 <= wsr <= 1.0:
            cfg["wake_silence_rms_threshold"] = float(wsr)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    save(cfg)
    return cfg


def save(cfg: dict) -> None:
    """Atomically write the config dict to disk."""
    p = config_path()
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, p)


def update(**kw) -> dict:
    """Merge kw into the on-disk config and rewrite. Returns the merged dict."""
    cfg = load()
    cfg.update(kw)
    save(cfg)
    return cfg
