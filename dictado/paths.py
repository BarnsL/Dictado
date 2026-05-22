"""Bundled-asset path resolution for the dictado package.

The repo ships a small `assets/` folder alongside the Python package
so a fresh `git clone` is fully functional out of the box -- no
external sounds need to live anywhere on the user's disk.

Layout (relative to the repo root):

    dictado/
        __init__.py
        daemon.py
        ...
        paths.py        <- you are here
    assets/
        sounds/
            biboo-asmr-hello.m4a    (default wake cue; user-selected)
            chime.wav               (fallback wake cue; synthesized,
                                     public domain)

Robust to two install layouts:

    1. Source checkout / editable install
       (`pip install -e .` or just `python -m dictado` from the cloned
       tree). The assets folder sits one level above the package.

    2. Wheel install (`pip install dictado` once published).
       package-data carries the assets folder INSIDE the package, so
       both locations are checked.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def assets_root() -> Optional[Path]:
    """Return the bundled assets/ directory if it exists, else None."""
    for candidate in (
        _package_dir().parent / "assets",   # source checkout
        _package_dir() / "assets",          # wheel layout
    ):
        if candidate.is_dir():
            return candidate
    return None


def resolve_wake_sound(preferred: str = "") -> Optional[str]:
    """Return an absolute path to the wake-startup sound, or None.

    Priority:
      1. `preferred` (typically `config["wake_sound_path"]`) if it's
         a non-empty existing file on disk.
      2. `assets/sounds/biboo-asmr-hello.m4a` (the user-selected
         default that ships with the repo).
      3. `assets/sounds/chime.wav` (synthesized fallback).
      4. None -- caller skips the cue.
    """
    if preferred:
        p = Path(preferred).expanduser()
        if p.is_file():
            return str(p)
    root = assets_root()
    if root is None:
        return None
    for cand in (root / "sounds" / "biboo-asmr-hello.m4a",
                 root / "sounds" / "chime.wav"):
        if cand.is_file():
            return str(cand)
    return None
