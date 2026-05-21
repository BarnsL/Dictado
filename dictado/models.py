"""dictado.models — catalog of every OpenAI Whisper model the daemon supports.

Why this module exists
----------------------
The upstream `openai-whisper` package exposes a flat string list of model
names with no metadata. To populate the tray menu intelligently — which
models to show, how to label them, which to recommend by default — we
need to attach per-model context: parameter count, ~RAM cost, English-
only vs multilingual, and "is this an alias for another model?".

Keeping the catalog here (not inline in daemon.py) means anyone who wants
to add or restrict models on a custom install only has to touch one file.

Naming
------
Whisper has two naming conventions:

  * **Plain names** like `tiny`, `base`, `small`, `medium`, `large` are
    multilingual.
  * **`.en` suffixes** like `tiny.en`, `base.en`, `small.en`, `medium.en`
    are English-only fine-tunes that trade multilingual capability for
    a small accuracy boost on English.
  * **`large-vN`** are the three released large checkpoints (`v1`, `v2`,
    `v3`). `large` itself is an alias maintained by openai-whisper that
    currently points at `large-v3`.
  * **`turbo`** (a.k.a. `large-v3-turbo`) is the newer fast variant of
    `large-v3` — fewer decoder layers, ~5x faster on CPU, comparable
    accuracy on English. It's our recommendation for "best of both
    worlds" if you have the disk for it.

Aliases are de-duplicated in DEFAULT_VISIBLE so the tray menu doesn't
list the same checkpoint twice under two names.

Sizes and RAM costs
-------------------
The numbers below are reproduced from OpenAI's published Whisper README
(see https://github.com/openai/whisper#available-models-and-languages)
plus measurements on this codebase's benchmark harness. RAM figures are
the steady-state working set after `whisper.load_model(name)` finishes
on CPU; GPU RAM costs are typically 2–3× larger because activations
aren't paged out.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    """One row in the OpenAI Whisper model catalog."""
    name:        str           # what you pass to whisper.load_model()
    display:     str           # tray-menu label
    parameters:  str           # e.g. "39M"; from upstream README
    disk_mb:     int           # rough on-disk weight size in MB
    ram_gb:      float         # rough CPU steady-state RAM after load
    multilingual: bool         # False for *.en checkpoints
    aliases:     tuple[str, ...] = ()  # other names that resolve to the same checkpoint
    notes:       str = ""      # short description for MODELS.md


# Canonical catalog. Keep this sorted from smallest to largest so menus
# render in a useful order.
CATALOG: tuple[ModelInfo, ...] = (
    ModelInfo("tiny.en",       "Tiny (English)",          "39M",   75,  0.4, False,
              notes="Smallest English-only model. Fast on any CPU; "
                    "punctuation is rough and accuracy degrades on "
                    "accents/jargon. Ideal for streaming partials on "
                    "underpowered hardware."),
    ModelInfo("tiny",          "Tiny",                    "39M",   75,  0.4, True,
              notes="Multilingual counterpart of tiny.en. Same trade-offs."),
    ModelInfo("base.en",       "Base (English)",          "74M",  140,  0.5, False,
              notes="Sweet spot for streaming partials when you only "
                    "need English. Comparable speed to tiny.en, "
                    "noticeably better punctuation."),
    ModelInfo("base",          "Base",                    "74M",  140,  0.5, True,
              notes="Multilingual base. Faster than small with most of "
                    "the accuracy on clean speech."),
    ModelInfo("small.en",      "Small (English)",         "244M", 460,  1.0, False,
              notes="High accuracy English-only; usable for one-shot "
                    "dictation on a laptop CPU. ~3x realtime."),
    ModelInfo("small",         "Small",                   "244M", 460,  1.0, True,
              notes="Multilingual small. Recommended default for users "
                    "who switch languages."),
    ModelInfo("medium.en",     "Medium (English)",       "769M", 1500,  1.5, False,
              notes="Excellent English accuracy; punctuation matches "
                    "what a human transcriber would produce. Slower than "
                    "realtime on CPU."),
    ModelInfo("medium",        "Medium",                 "769M", 1500,  1.5, True,
              notes="Multilingual medium; the previous default before "
                    "turbo. Slower than realtime on CPU."),
    ModelInfo("large-v1",      "Large v1",              "1550M", 2900,  3.0, True,
              notes="The original large checkpoint from late 2022. "
                    "Superseded by v2 and v3 for accuracy."),
    ModelInfo("large-v2",      "Large v2",              "1550M", 2900,  3.0, True,
              notes="Improved over v1; many community evaluations still "
                    "show v2 leading on niche languages and noisy audio."),
    ModelInfo("large-v3",      "Large v3",              "1550M", 2900,  3.0, True,
              aliases=("large",),
              notes="Current large checkpoint. The bare name `large` is "
                    "an alias for this in openai-whisper."),
    ModelInfo("large-v3-turbo", "Large v3 Turbo",        "809M", 1500,  1.5, True,
              aliases=("turbo",),
              notes="Fewer decoder layers than large-v3; ~5x faster on "
                    "CPU with comparable accuracy on English. Best "
                    "speed/quality balance if you have the disk for it. "
                    "The bare name `turbo` is an alias."),
)

# Convenience views. ALL_NAMES is what `whisper.load_model(name)`
# accepts; CANONICAL_NAMES drops the alias entries (`large`, `turbo`) so
# we don't surface duplicate menu items.
ALL_NAMES:        tuple[str, ...] = tuple(m.name for m in CATALOG) + ("large", "turbo")
CANONICAL_NAMES:  tuple[str, ...] = tuple(m.name for m in CATALOG)

# Models surfaced in the tray menu by default. Five is enough to give
# the user a useful range without flooding the menu; the daemon's IPC
# trigger (`dictado --switch-model NAME`) accepts any name in
# ALL_NAMES so power users can still load tiny.en or large-v1 on demand.
# Order: smallest -> turbo -> medium -> large-v3.
DEFAULT_VISIBLE: tuple[str, ...] = (
    "base",
    "small",
    "medium",
    "large-v3-turbo",
    "large-v3",
)

# Recommended starting model on a fresh install. medium has good accuracy
# and reasonable RAM. Power users override via config.json before first launch.
DEFAULT_MODEL: str = "medium"


def info(name: str) -> ModelInfo | None:
    """Look up the metadata row for a model name (or alias)."""
    for m in CATALOG:
        if m.name == name or name in m.aliases:
            return m
    return None


def is_known(name: str) -> bool:
    """True iff the given string is one whisper.load_model accepts."""
    return name in ALL_NAMES


def display_for(name: str) -> str:
    """Pretty-print a model name for the tray menu. Falls back to the
    raw name if it's not in our catalog (e.g. a future model that the
    user requested via config.json)."""
    m = info(name)
    return m.display if m else name


def disk_size_mb(name: str) -> int:
    """Approximate on-disk size in MB. 0 if unknown."""
    m = info(name)
    return m.disk_mb if m else 0
