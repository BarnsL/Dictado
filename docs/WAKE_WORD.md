# Voice activation (wake-word listener)

> **TL;DR.** Toggle `Voice activation ("Hey Bijou" / "Hey Biboo" ...)`
> in the tray menu. Then say "hey bijou", "ok biboo", "hello bijou", or
> any of the other configured phrases to start a recording hands-free.
> Stops recording the same way you started it: hotkey, tray menu, or
> say nothing for the auto-stop window.

---

## Overview

The wake-word listener gives the daemon a hands-free activation path
on top of the existing global hotkey. Once enabled, a separate
lightweight Whisper `tiny.en` instance continuously transcribes a
rolling 2.5-second buffer of microphone audio. When the transcription
matches one of the configured wake phrases, the daemon fires
`start_recording()` exactly as if the user had pressed Alt+T.

It's opt-in. The toggle defaults to OFF, so users who don't want it
pay zero CPU and the mic isn't being inferred-on continuously.

The implementation lives in `dictado/wake_word.py`. Read the module
docstring for the architecture diagram and edge-case coverage.

---

## Default wake phrases

The default regex matches **two assistant names**, each pronounced
phonetically:

- **Bijou** — "bee-joo"
- **Biboo** — "bee-boo"

prefixed with **any of**: `hey`, `ok`, `okay`, `yo`, `hello`, `hi`,
`greetings`, `salutations`.

Examples that activate:

```
hey bijou           ok bijou           yo bijou
hello bijou         hi bijou           greetings bijou
salutations bijou
hey biboo           ok biboo           yo biboo
hello biboo         hi biboo           greetings biboo
salutations biboo
```

The regex is **case-insensitive** and tolerates punctuation between
the prefix and the name (`"hey, bijou."`, `"Hey Bijou!"`, all match).

### Whisper transcription variants

Bijou and Biboo never appeared in OpenAI Whisper's training corpus,
so the model emits phonetic approximations. The default regex
includes every variant we've observed:

| Spoken | Whisper sometimes transcribes |
|---|---|
| Bijou | bijou, bijoux, beejoo, bee-joo, bee jew, bayou, biggio, bidu, bee zhou, be-jew |
| Biboo | biboo, bee-boo, bee boo, beeboo, bibu, bibou, bebu, bee-bu, peeboo |

If you hear "hey bijou" but the daemon doesn't trigger, the most
likely cause is Whisper transcribing it as something we haven't
captured. Open the daemon log (`%LOCALAPPDATA%\dictado\daemon.log`),
look for `[DEBUG] No wake match in: '...'`, and add the new spelling
to either `_BIJOU` or `_BIBOO` in `dictado/wake_word.py`.

---

## Enabling it

Open the system tray menu (right-click the dictado microphone icon),
then click **Voice activation ("Hey Bijou" / "Hey Biboo" ...)**.

When checked:
- The wake listener starts on its own thread.
- A second Whisper model (`tiny.en`, ~39 MB) is loaded on first enable
  (downloaded from OpenAI's CDN if not already cached).
- `wake_word_enabled: true` is persisted to `config.json` so it
  re-enables on next daemon start.

When unchecked:
- The wake listener is torn down (PyAudio stream released, model
  reference dropped).
- `wake_word_enabled: false` is persisted.

---

## Customising the phrases

Edit `config.json` directly (path varies by OS):

| OS | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\dictado\config.json` |
| macOS | `~/Library/Application Support/dictado/config.json` |
| Linux | `~/.local/share/dictado/config.json` |

```json
{
  "wake_word_enabled": true,
  "wake_word_phrases": [
    "hey bijou",
    "ok biboo",
    "yo computer"
  ]
}
```

Each entry becomes one alternation in a regex. The **last** word of
each phrase is treated as the name; everything before it is the
prefix.

If the name is one we know (`bijou` or `biboo`), the recognised-
variants pattern is substituted automatically. Other names are matched
literally (case-insensitive, punctuation-stripped).

You can mix recognised and custom names:

```json
"wake_word_phrases": [
  "hey bijou",
  "ok biboo",
  "computer engage"
]
```

In the third entry, "computer engage" matches the literal string
"computer engage" (and case-insensitive variants). No phonetic
fuzzing is applied because we don't have a curated variants list
for "engage".

When `wake_word_phrases` is empty or missing, the default regex
applies (bijou + biboo with all eight prefixes).

---

## Architecture

```
+-----------------------+
|  pyaudio capture      |  Separate from the daemon's normal
|  16 kHz / mono / 16b  |  recording stream. Runs while idle.
+-----------+-----------+
            |  500 ms chunks
            v
+-----------------------+
|  ring buffer          |  Fixed 2.5 s window. Each frame
|  (frames + RMS)       |  pre-computes its RMS so the
+-----------+-----------+  cheap gate below is O(1).
            |
            v
+-----------------------+
|  RMS gate             |  Below 0.012 -> skip Whisper.
|                       |  Saves ~99% of the CPU when
|                       |  no one is talking.
+-----------+-----------+
            |
            v
+-----------------------+
|  Whisper tiny.en      |  ~140 ms per 2.5 s window on
|  (rolling buffer)     |  this CPU.
+-----------+-----------+
            |
            v
+-----------------------+
|  no_speech_prob > 0.7 |  Whisper's own confidence check.
|  -> ignore            |  Filters out hallucinations on
|                       |  silent / noisy windows.
+-----------+-----------+
            |
            v
+-----------------------+
|  WAKE_REGEX match?    |  Matches -> on_wake() ->
|                       |  start_recording().
+-----------+-----------+
            |
            v
+-----------------------+
|  1.5 s cooldown       |  Plus zeroes the ring buffer so
|  + clear buffer       |  the next match cycle gets fresh
|                       |  audio (the wake phrase itself
|                       |  shouldn't make it into the
|                       |  user's command).
+-----------------------+
```

### Pause / resume during recording

When the daemon's `start_recording()` runs (whether triggered by
hotkey, tray menu, IPC trigger, or a wake event), it calls
`wake_detector.pause()`. Inference is suspended; PyAudio capture
keeps going so we don't churn the audio device.

When `stop_recording()` finishes, it calls `wake_detector.resume()`,
clears the ring buffer, and gives the user a short grace period
(~500 ms) before resuming inference. Without that grace, the
listener would re-fire on the trailing audio of the recording the
user just finished.

---

## CPU / battery cost

| State | CPU (one core, 12-core x86 laptop) |
|---|---:|
| Idle (silent room, no one talking) | ~0.5% |
| Person talking nearby (RMS gate firing) | ~5% |
| Wake event triggers `start_recording()` | ~5% spike for ~150 ms |

Memory cost: ~140 MB for the `tiny.en` model + a 2.5 s rolling
buffer (80 KB).

Battery impact on a laptop running idle for an hour with the
listener enabled and no speech: <0.5% above baseline. With moderate
speech triggering the RMS gate ~10% of the time: ~1-2% above
baseline.

If battery matters more than instant wake response: turn the toggle
off and use the hotkey, or bind the toggle itself to a hotkey via
the OS's keyboard-shortcut tooling.

---

## Tuning knobs (in `dictado/wake_word.py`)

| Constant | Default | Tuning hint |
|---|---:|---|
| `ROLLING_BUFFER_SECONDS` | 2.5 s | Increase if your wake phrase is longer than 1.5 s. |
| `INFER_INTERVAL_SECONDS` | 0.5 s | Decrease for snappier wake response (more CPU). |
| `RMS_THRESHOLD` | 0.012 | Increase if you get false positives from background fan / typing noise. Decrease if quiet voices aren't triggering. |
| `NO_SPEECH_PROB_THRESHOLD` | 0.7 | Whisper's hallucination filter. Raise to 0.85 if you see false-positive matches like "thank you" / "you" being treated as wake-eligible. |
| `COOLDOWN_SECONDS` | 1.5 s | Increase if the listener re-triggers on the trailing audio of the wake phrase. |
| `WAKE_MODEL_NAME` | `tiny.en` | The English-only tiny is the right pick for wake-word use. `base.en` adds a second of latency for ~no accuracy gain on these short phrases. |

---

## Edge cases handled

1. **Wake during a recording.** `pause()` is called by the daemon when
   `start_recording()` runs; `resume()` after `stop_recording()`.
   The listener won't re-trigger while you're dictating.

2. **Repeat triggers from the same phrase.** After a wake event, we
   apply a 1.5 s cooldown AND zero the ring buffer. The same phrase
   can't match twice on consecutive 0.5 s polls.

3. **Whisper hallucinations during silence.** We filter on the
   model's own `no_speech_prob` (per-segment) AND on RMS gate. Both
   have to pass before the regex even sees the text.

4. **Wake words not in Whisper's training data.** "Bijou" and
   "Biboo" are unusual proper nouns. The regex accepts every
   transcription variant we've observed and is easy to extend.

5. **Background music / TV.** Background voices triggering wake
   are the dominant false-positive vector. The RMS gate filters
   most of them; the no_speech_prob filter catches some more.
   Final defense: the regex is intentionally specific (a wake-prefix
   followed by a recognised name), not a single keyword.

---

## Privacy

Same as the rest of dictado: nothing networked.

- The `tiny.en` model is downloaded once from OpenAI's public CDN on
  first enable. After that, no audio or text leaves the machine.
- The 2.5 s rolling buffer is in-memory only and is overwritten in
  place. Nothing is written to disk.
- A successful wake event triggers a normal `start_recording()`,
  which then writes a WAV to the existing audio archive (and
  appends to `AudioTranscriptions_*.md`) — same as if you had
  pressed Alt+T. The wake-window itself is never archived.

If you don't want even the in-memory buffer running, leave the toggle
off. The listener thread doesn't exist when it's disabled.

---

## Endpoint-protection notes

The wake-word path uses the same Win32 / PyAudio primitives as the
existing recording path:

- PyAudio's blocking `stream.read()` (no callback-based callback IPC).
- An in-process numpy + Whisper inference call (no subprocess).
- No keyboard, mouse, screen-capture, or network primitive.

It does NOT introduce:

- A global low-level keyboard hook.
- A new Windows service.
- A network listener.
- A Scheduled Task.

If your endpoint-protection software is OK with normal voicepad /
dictado dictation, it'll be OK with this. See `docs/SECURITY.md` for
the per-primitive mapping.

---

## Adding a new name

Say you want to add a third assistant name "vega" (pronounced
"vee-guh"). Edit `dictado/wake_word.py`:

1. Add a new variants regex below `_BIBOO`:

   ```python
   _VEGA = (
       r"(?:"
       r"vega|veg(?:a|ah)|"
       r"v[ae]ig[ah]|"
       r"vee[\s\-]*guh"
       r")"
   )
   ```

2. Add it to `DEFAULT_NAMES`:

   ```python
   DEFAULT_NAMES: list[tuple[str, str]] = [
       ("bijou", _BIJOU),
       ("biboo", _BIBOO),
       ("vega",  _VEGA),
   ]
   ```

3. Restart the daemon.

The default regex picks up the new alternation automatically; users
can also add `"hey vega"` to `wake_word_phrases` in their
`config.json`.

---

## Troubleshooting

**The toggle does nothing.** Check `daemon.log` for
`Loading wake-word model 'tiny.en'`. If you see
`openai-whisper not available`, the venv is missing the package:
`pip install --user openai-whisper`.

**It triggers on TV background voices.** Raise `RMS_THRESHOLD` to
0.020 or 0.030. The default is calibrated for a quiet room.

**It misses my "hey bijou".** Check the log for the actual
transcription Whisper produced (`[DEBUG] No wake match in: 'hey
beegew'`). Add the new variant to `_BIJOU` or `_BIBOO`.

**Multiple matches per phrase.** Indicates the cooldown isn't
clearing the buffer. Likely a config drift; verify
`COOLDOWN_SECONDS = 1.5` in `wake_word.py`.

**It triggers when I'm dictating into a meeting.** That's the
expected interaction with `pause()` not firing. Confirm
`wake_detector.pause()` is called inside `start_recording()` (it is
in the shipping code; if you've forked, check the patch is intact).

---

## Why not Porcupine / Snowboy / openWakeWord?

Three reasons we're using Whisper-tiny.en instead of a dedicated
wake-word framework:

1. **No new dependency.** We already have Whisper loaded for normal
   dictation. Adding Porcupine would add a paid dependency and a
   new auth flow (Picovoice account key).

2. **Custom phrase support.** Porcupine's free tier only supports a
   handful of pre-trained keywords. We needed permutations of made-up
   names ("bijou", "biboo") with multiple prefixes, which would
   require Picovoice's paid model-training service.

3. **Same accuracy ceiling.** On 8 GB of RAM laptops, Whisper-tiny
   transcribes 2.5 s windows in ~140 ms with no GPU — fast enough
   that the user perceives wake response as instant. The latency
   difference vs Porcupine isn't material for this use case.

The trade-off: ~140 MB RAM and ~5% CPU when actively listening,
versus Porcupine's ~5 MB and 1% CPU. Worth it on dev laptops; tune
the toggle off on long battery runs.
