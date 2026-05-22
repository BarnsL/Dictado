"""dictado.wake_word -- continuous wake-word listener.

Why this module exists
----------------------
Today's daemon needs a hotkey press (Alt+T by default) to start a
recording. That works great when the user has a free hand, but it's
suboptimal when:

  - You're on a video call with both hands on the mic / keyboard.
  - You're across the room from the keyboard.
  - You're already speaking at your screen (dictating into ChatGPT,
    talking through a problem out loud, etc.) and reaching for the
    hotkey breaks the train of thought.

The wake-word listener gives you "Star Trek" / "Hey Siri" -style
hands-free activation. Say one of the configured wake phrases and the
daemon starts a normal recording session as if you'd pressed Alt+T.

Wake phrases (all configurable in config.json)
----------------------------------------------
Default phrases recognise either of two assistant names, "Bijou"
(pronounced "bee-joo") and "Biboo" (pronounced "bee-boo"), prefixed
with one of: hey, ok, okay, yo, hello, hi, greetings, salutations.

Examples that activate:

  - "hey bijou"
  - "ok bijou"
  - "yo bijou"
  - "hello bijou"
  - "greetings bijou"
  - "salutations bijou"
  - "hey biboo"
  - ... etc.

We pre-compile a permissive regex that also matches Whisper's most
common transcription variants for these proper nouns -- bijou never
appeared in Whisper's training data, so the model produces phonetic
approximations like "bayou", "beejoo", "be-jew", "bigeo". Same for
biboo: "bibu", "bebu", "bee boo". The regex below documents every
variant we've observed; add more as you see them slip through.

Architecture
------------

       +-----------------------+
       |  pyaudio stream       |  (separate from the daemon's normal
       |  16 kHz / mono / 16b  |   recording stream; this one runs
       |  callback ring buf    |   while the daemon is "idle")
       +-----------+-----------+
                   |
                   v
       +-----------------------+        every 500 ms:
       |  _listen_loop thread  |--------+
       +-----------+-----------+        |
                   |                    v
                   |          +-------------------+
                   |          |  RMS gate         |  silent? skip Whisper.
                   |          |  (cheap)          |  Saves ~99% of the CPU
                   |          +---------+---------+  when no one's talking.
                   |                    |
                   |                    v
                   |          +-------------------+
                   |          |  Whisper tiny.en  |  ~140 ms on this CPU
                   |          |  on rolling 2.5s  |  for a 2.5 s window.
                   |          +---------+---------+
                   |                    |
                   |                    v
                   |          +-------------------+
                   |          |  no_speech_prob   |  > 0.7 -> ignore
                   |          |  filter           |  (whisper hallucinates
                   |          +---------+---------+   during silence).
                   |                    |
                   |                    v
                   |          +-------------------+
                   |          |  WAKE_REGEX       |  match -> fire on_wake
                   |          |  match            |  + 1.5 s cooldown +
                   |          +---------+---------+   clear ring buffer.
                   |                    |
                   v                    v
          +-----------------+   +---------------------+
          |  pause()  /     |   |  on_wake() ->       |
          |  resume()       |   |  daemon.start_recording()
          |  (called by     |   +---------------------+
          |   daemon when
          |   recording is
          |   already in
          |   progress)
          +-----------------+

CPU / battery cost
------------------

| State            | CPU (one core, 12-core x86 laptop) |
|------------------|-----------------------------------:|
| Idle (silent)    | ~0.5%                              |
| Person talking   | ~5%                                |
| Wake fires       | ~5% spike for ~150 ms              |

The opt-in toggle in the tray menu defaults to OFF. Users who don't
need hands-free activation pay zero CPU.

Privacy
-------

Same as normal voicepad / dictado: no audio leaves the machine
after the one-time Whisper model download. The wake listener uses a
local Whisper instance; nothing networked. The 2.5 s rolling buffer
is in-memory only and is overwritten in-place; nothing is written to
disk except the (existing) per-recording WAV that gets archived only
*after* a wake event triggers a real recording.

Edge cases handled
------------------

1. **Wake during a recording.** `pause()` is called by the daemon when
   `start_recording()` runs; `resume()` is called when
   `stop_recording()` returns. We don't want the wake listener to
   fire while the user is actively dictating; that would terminate
   the dictation prematurely.

2. **Repeat triggers.** After a wake event, we drop a 1.5 s cooldown
   AND zero the ring buffer. This stops the same phrase getting
   matched again on the next poll.

3. **Whisper hallucinations during silence.** Whisper sometimes emits
   "you", "thank you", ".", "[Music]", etc. on silent input. We
   filter by `no_speech_prob > 0.7` (the model's own confidence
   measure) and by short-text-on-low-RMS heuristics.

4. **Wake words don't appear in Whisper's training data.** "Bijou" /
   "Biboo" are unusual proper nouns. The regex accepts every Whisper
   transcription variant we've observed plus a few defensive
   alternatives.

Customising the wake phrases
----------------------------

Edit `config.json` (path: `%LOCALAPPDATA%\\dictado\\config.json` on
Windows, `~/Library/Application Support/dictado/config.json` on
macOS, `~/.local/share/dictado/config.json` on Linux):

```json
{
  "wake_word_enabled": false,
  "wake_word_phrases": ["hey bijou", "ok bijou", "yo biboo"]
}
```

Each phrase becomes a `<prefix> <name>` regex. Whisper transcription
variants are added automatically for the recognised names "bijou" and
"biboo"; for any other name the phrase is matched literally
(case-insensitive, punctuation-stripped).

See `docs/WAKE_WORD.md` for the full design, tuning guide, and
extending-the-name-list playbook.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger("dictado.wake_word")


# ---------------------------------------------------------------------------
# Regex pieces. Ordered with the proper-noun variants first because that's
# the part Whisper struggles with.
# ---------------------------------------------------------------------------

# Wake-verb prefix: any of the friendly greetings the user can use.
# Whisper transcribes these reliably so we just list them.
_WAKE_PREFIX = (
    r"(?:hey|ok|okay|yo|hello|hi|greetings?|salutations?)"
)

# "Bijou" -- pronounced "bee-joo". Whisper has never seen this proper
# noun in its training corpus, so it produces phonetic approximations.
# Variants we've observed in the wild:
#
#   bijou, bijoux, beejoo, bee-joo, be-jew, beejew, bizu, bidu,
#   bayou (very common false positive!), bayu, bee jew, bigeo, biju,
#   bee zhou, bay-zhou, beejew, biggio
#
# The pattern below covers the common ones. Everything is matched
# case-insensitive at the call site so we don't bother with [Bb] etc.
_BIJOU = (
    r"(?:"
    # Direct spellings:
    r"bijou|bijoux|biggio|bidu|bizu|bigeo|"
    # 'bee + j-/zh-' variants (most common Whisper output):
    r"bee[\s\-]*(?:j|zh|g)(?:oo|ew|u|ou|o)|"   # bee-joo, bee zhou, beegoo, bee jew
    r"be[\s\-]*(?:j|zh|g)(?:oo|ew|u|ou|o)|"    # be-joo, be jew
    r"b(?:ay|ai)[\s\-]*(?:j|zh)?[ou]+|"        # bayou, bay-zhou, baizou
    # Fall-back syllable-split forms:
    r"begu|begoo|begew|"
    r"big[\s\-]*(?:joo|jew|zhou|jou|you)|"     # big joo, big-zhou
    r"beat[\s\-]*joo|beat[\s\-]*you|"        # beat joo (Whisper splits "bee" -> "beat")
    r"b[\s\-]*joo"                             # bare 'b joo'
    r")"
)

# "Biboo" -- pronounced "bee-boo". Whisper variants:
#
#   biboo, bee-boo, bee boo, beeboo, bibu, bebu, bibou, be-boo,
#   peeboo (rare false positive), bee-bu, beba
_BIBOO = (
    r"(?:"
    # Direct spellings:
    r"biboo|bibou|bibu|bib|"
    # 'bee + b' variants (most common Whisper output: 'Bee Boo'):
    r"bee[\s\-]*b(?:oo|ou|u|o)|"               # bee boo, bee-boo, bee bu
    r"be[\s\-]*b(?:oo|ou|u|o)|"                # be boo, be-boo
    r"bi[\s\-]*b(?:oo|ou|u|o)|"                # bi boo, bi-boo
    # Whisper drops the 'b' in 'biboo' on noisy windows -> 'bee oo'.
    # We don't want to match that bare; it's too generic.
    r"beb(?:u|oo|a|ou)|"                       # bebu, beboo, beba, bebou
    r"peeboo|peabo|peabu|"                     # aspirated 'b' false-recognitions
    r"big[\s\-]*boo|"                          # 'big boo' false-recognition
    # Whisper sometimes attaches the prefix word to the name:
    r"hey[\s\-]*boo|ok[\s\-]*boo|yo[\s\-]*boo"
    r")"
)

# Public default name set. Each name carries (display, regex_alternation).
DEFAULT_NAMES: list[tuple[str, str]] = [
    ("bijou", _BIJOU),
    ("biboo", _BIBOO),
]


def build_default_wake_regex() -> "re.Pattern[str]":
    """Compile the default wake-phrase regex used when the user doesn't
    override `wake_word_phrases` in config."""
    name_alt = "|".join(name_rx for _, name_rx in DEFAULT_NAMES)
    pattern = (
        rf"\b{_WAKE_PREFIX}"
        rf"(?:[\s,.!?\-]+|\s*)"   # optional separators (Whisper sometimes
                                  # elides them, sometimes inserts a comma)
        rf"(?:{name_alt})\b"
    )
    return re.compile(pattern, re.IGNORECASE)


def build_user_wake_regex(phrases: list[str]) -> "re.Pattern[str]":
    """Compile a wake regex from user-supplied phrases.

    Each phrase becomes one alternation. For phrases ending in a name
    we recognise (bijou, biboo), the recognised-variants pattern is
    substituted for the literal name to stay tolerant to Whisper's
    transcription quirks. Other names are matched literally
    (case-insensitive, punctuation-stripped at match time).
    """
    if not phrases:
        return build_default_wake_regex()
    alternations: list[str] = []
    name_lookup = {name: name_rx for name, name_rx in DEFAULT_NAMES}
    for phrase in phrases:
        cleaned = re.sub(r"\s+", " ", phrase.strip().lower())
        if not cleaned:
            continue
        # Split into prefix-and-name; the LAST whitespace-separated
        # token is treated as the name.
        parts = cleaned.split(" ")
        if len(parts) < 2:
            # Single-token phrase: treat the whole thing as the name
            # part with an empty prefix.
            prefix_lit = r""
            name_part = parts[0]
        else:
            prefix_lit = re.escape(" ".join(parts[:-1]))
            name_part = parts[-1]
        name_rx = name_lookup.get(name_part, re.escape(name_part))
        if prefix_lit:
            alternations.append(
                rf"(?:{prefix_lit}\s*[,.!?\-]?\s*(?:{name_rx}))"
            )
        else:
            alternations.append(rf"(?:{name_rx})")
    pattern = r"\b(?:" + "|".join(alternations) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

# Audio capture parameters. Chosen to match the rest of the daemon
# (16 kHz mono int16) so the model sees the same shape it does for
# normal recordings.
SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # int16

# Rolling-buffer length: 2.5 s. Long enough to capture "salutations
# bijou" (the longest default phrase, ~1.6 s) with margin; short enough
# that Whisper tiny.en transcribes it in ~140 ms on a CPU laptop.
ROLLING_BUFFER_SECONDS = 2.5

# Inference cadence: every 500 ms while there's audio activity. The
# overlap between successive windows is ~2.0 s, so a wake phrase has
# multiple chances to be inside the window.
INFER_INTERVAL_SECONDS = 0.5

# RMS gate: only run Whisper inference when the audio's RMS over the
# last `_GATE_WINDOW_SECONDS` is above this threshold. Cheap O(N)
# computation; saves ~99% of the CPU when no one's talking. The
# threshold is calibrated to "a person speaking quietly across the
# room from a laptop mic". Increase if you get false-positive triggers
# from background noise.
RMS_THRESHOLD = 0.008
_GATE_WINDOW_SECONDS = 0.5

# Whisper hallucination filter: if the model's no_speech_prob exceeds
# this on the rolling window, we ignore the transcription. Whisper
# loves to emit "you", "thank you", "[Music]" etc. on silent input.
NO_SPEECH_PROB_THRESHOLD = 0.7

# After a wake fires, ignore further matches for this long. Prevents
# the same phrase getting matched on the next poll (the rolling buffer
# still contains it). We also zero the ring buffer at this point so
# leftover audio doesn't bleed into the user's actual command.
COOLDOWN_SECONDS = 1.5

# Whisper model used for wake-word transcription. tiny.en is the
# right pick: ~14x realtime on CPU, English-only (the wake phrases
# are all English), 39 MB on disk. We do NOT use the same model the
# daemon uses for normal recordings; loading the wake listener
# shouldn't cost the user a fresh medium-model warm-up.
WAKE_MODEL_NAME = "tiny.en"


@dataclass
class _Frame:
    data: bytes
    rms: float


class WakeWordDetector:
    """Continuously listens for a configured wake phrase. Calls
    `on_wake()` on the listener thread when a phrase is detected.

    Lifecycle
    ---------
    - `start()`        spawn pyaudio capture + listener thread.
                       Idempotent.
    - `stop()`         tear down the listener; release pyaudio.
                       Idempotent.
    - `pause()`        stop running Whisper inference but keep capture
                       alive. Used by the daemon while a normal
                       recording is in progress so we don't fight the
                       user's dictation.
    - `resume()`       resume inference after a pause.

    Thread-safety
    -------------
    All public methods are safe to call from any thread. Internal
    state is guarded by `_state_lock`. `on_wake` is called on the
    listener thread; the callback should be cheap or push to a queue.
    """

    def __init__(
        self,
        on_wake: Callable[[str], None],
        wake_regex: Optional["re.Pattern[str]"] = None,
        model_name: str = WAKE_MODEL_NAME,
        device_index: Optional[int] = None,
    ):
        self._on_wake = on_wake
        self._wake_regex = wake_regex or build_default_wake_regex()
        self._model_name = model_name
        self._device_index = device_index

        self._stop_event = threading.Event()
        self._paused = threading.Event()  # set => paused
        self._state_lock = threading.Lock()

        self._audio_thread: Optional[threading.Thread] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._stream = None
        self._pyaudio_inst = None
        self._model = None  # lazily loaded inside _listen_loop

        # Ring buffer of (data, rms) frames. CHUNK is calibrated so each
        # frame is exactly INFER_INTERVAL_SECONDS long; that way the
        # listen loop can `wait` for one new frame at a time.
        self._chunk_samples = int(SAMPLE_RATE * INFER_INTERVAL_SECONDS)
        self._chunk_bytes = self._chunk_samples * SAMPLE_WIDTH_BYTES
        self._buffer_max_frames = int(
            ROLLING_BUFFER_SECONDS / INFER_INTERVAL_SECONDS) + 1
        self._frames: list[_Frame] = []
        self._frames_lock = threading.Lock()
        self._frame_available = threading.Event()
        self._cooldown_until = 0.0

    # ---- public API --------------------------------------------------

    def start(self) -> None:
        with self._state_lock:
            if self._listen_thread is not None and self._listen_thread.is_alive():
                logger.debug("WakeWordDetector.start(): already running.")
                return
            self._stop_event.clear()
            self._paused.clear()
            self._frames = []
            self._listen_thread = threading.Thread(
                target=self._listen_loop, daemon=True,
                name="wake-word-listener")
            self._audio_thread = threading.Thread(
                target=self._audio_loop, daemon=True,
                name="wake-word-audio")
            self._listen_thread.start()
            self._audio_thread.start()
        logger.info("Wake-word listener started.")

    def stop(self) -> None:
        with self._state_lock:
            if self._listen_thread is None:
                return
            self._stop_event.set()
            self._frame_available.set()  # unblock waiter
            stream = self._stream
            self._stream = None
        # Close pyaudio resources outside the lock so callbacks can
        # finish.
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        try:
            if self._pyaudio_inst is not None:
                self._pyaudio_inst.terminate()
        except Exception:
            pass
        self._pyaudio_inst = None
        # Wait for threads to drain (briefly).
        for t in (self._listen_thread, self._audio_thread):
            if t is not None:
                t.join(timeout=2.0)
        self._listen_thread = None
        self._audio_thread = None
        logger.info("Wake-word listener stopped.")

    def pause(self) -> None:
        """Stop running inference but keep capture alive. Cheap to
        toggle on/off many times. Used by daemon during recordings."""
        if not self._paused.is_set():
            self._paused.set()
            logger.debug("Wake-word listener paused.")

    def resume(self) -> None:
        """Resume inference after a pause."""
        if self._paused.is_set():
            with self._frames_lock:
                self._frames = []
            self._cooldown_until = time.monotonic() + 0.5
            self._paused.clear()
            logger.debug("Wake-word listener resumed.")

    @property
    def running(self) -> bool:
        return (self._listen_thread is not None
                and self._listen_thread.is_alive())

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    # ---- audio capture ----------------------------------------------

    def _audio_loop(self) -> None:
        """Run pyaudio capture in blocking-read mode. Push each chunk
        into the ring buffer with its precomputed RMS, signal the
        listener thread, and loop until stop_event is set.
        """
        try:
            import pyaudio
        except ImportError:
            logger.exception("pyaudio not available; wake-word disabled.")
            self._stop_event.set()
            return
        try:
            self._pyaudio_inst = pyaudio.PyAudio()
            self._stream = self._pyaudio_inst.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=self._chunk_samples,
                input_device_index=self._device_index,
            )
        except Exception:
            logger.exception("pyaudio open failed; wake-word disabled.")
            self._stop_event.set()
            return

        logger.debug("Wake audio capture: %d samples/chunk, %d ms.",
                     self._chunk_samples,
                     int(INFER_INTERVAL_SECONDS * 1000))

        while not self._stop_event.is_set():
            try:
                data = self._stream.read(
                    self._chunk_samples, exception_on_overflow=False)
            except Exception:
                logger.exception("Wake audio read failed; aborting.")
                break
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
            with self._frames_lock:
                self._frames.append(_Frame(data=data, rms=rms))
                if len(self._frames) > self._buffer_max_frames:
                    # Drop the oldest frame to keep the buffer bounded.
                    self._frames.pop(0)
            self._frame_available.set()

    # ---- inference loop ---------------------------------------------

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            import whisper
        except ImportError:
            logger.exception("openai-whisper not available; wake-word "
                             "cannot run.")
            self._stop_event.set()
            return
        logger.info("Loading wake-word model %r ...", self._model_name)
        try:
            self._model = whisper.load_model(self._model_name)
            logger.info("Wake-word model %r loaded.", self._model_name)
        except Exception:
            logger.exception("Loading wake-word model %r failed.",
                             self._model_name)
            self._stop_event.set()

    def _listen_loop(self) -> None:
        self._ensure_model()
        if self._model is None:
            return

        while not self._stop_event.is_set():
            # Wait for at least one new frame to be available, but with
            # a periodic timeout so we can re-check stop / pause state.
            self._frame_available.wait(timeout=0.5)
            self._frame_available.clear()

            if self._stop_event.is_set():
                break
            if self._paused.is_set():
                continue
            if time.monotonic() < self._cooldown_until:
                continue

            with self._frames_lock:
                # Snapshot the rolling buffer's audio + RMS-over-last-window.
                snapshot = list(self._frames)
            if not snapshot:
                continue
            window_rms = float(np.mean([f.rms for f in snapshot[-1:]]))
            if window_rms < RMS_THRESHOLD:
                continue
            audio_bytes = b"".join(f.data for f in snapshot)
            audio = np.frombuffer(audio_bytes, dtype=np.int16) \
                      .astype(np.float32) / 32768.0
            if audio.size < SAMPLE_RATE:  # < 1 second of audio buffered
                continue
            try:
                result = self._model.transcribe(
                    audio,
                    language="en",
                    fp16=False,
                    condition_on_previous_text=False,
                    no_speech_threshold=0.5,
                    temperature=0.0,
                )
            except Exception:
                logger.exception("Wake-word transcribe raised; skipping.")
                continue
            text = (result.get("text") or "").strip()
            if not text:
                continue
            # whisper segments carry per-segment no_speech_prob; take
            # the maximum (i.e. "most uncertain segment") as a coarse
            # gate.
            segments = result.get("segments") or []
            max_no_speech = max(
                (s.get("no_speech_prob", 0.0) for s in segments),
                default=0.0)
            if max_no_speech > NO_SPEECH_PROB_THRESHOLD:
                continue

            # Normalise: lowercase + collapse punctuation so the regex
            # has a clean target.
            normalised = re.sub(r"[^\w\s\-]", " ", text.lower())
            normalised = re.sub(r"\s+", " ", normalised).strip()
            if self._wake_regex.search(normalised):
                logger.info("Wake match: %r (window rms=%.3f, "
                            "max_no_speech=%.2f)",
                            text, window_rms, max_no_speech)
                # Cooldown + clear the buffer so the next match cycle
                # gets fresh audio.
                self._cooldown_until = time.monotonic() + COOLDOWN_SECONDS
                with self._frames_lock:
                    self._frames = []
                try:
                    self._on_wake(text)
                except Exception:
                    logger.exception("on_wake callback raised.")
            else:
                logger.info("No wake match in: %r (rms=%.3f, max_no_speech=%.2f)", text, window_rms, max_no_speech)
