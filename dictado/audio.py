"""Audio device enumeration + resolution helpers.

Two callers need a stable input-device index:

  1. start_recording() in daemon.py -- opens a pyaudio stream for
     the actual recording.
  2. WakeWordDetector._audio_loop in wake_word.py -- opens a separate
     stream for the rolling wake-word buffer.

Both want the same answer: which device should we be listening to?

PortAudio COM critical section
==============================

PortAudio's WASAPI / WDM-KS host APIs use a single COM apartment per
process. Concurrent `Pa_Initialize` + `Pa_Terminate` calls from
different threads corrupt that apartment and segfault later
`Pa_ReadStream` calls (live trace 2026-05-22: pythonw.exe crashed at
`_portaudio.cp313-win_amd64.pyd+0x9b7b` four times in 30 minutes).

Every PaInstance lifecycle in this codebase MUST go through the
module-level `pa_lock` below. Holding the lock around init / terminate
prevents the cross-thread COM-apartment teardown that 0x9b7b is.

Behaviour
---------
- If `config["audio_device_name"]` is None, return None. PyAudio
  picks the OS-default device. Equivalent to v0.6.9 behaviour.
- If a string, search PyAudio's device list for an INPUT device
  whose name contains the string (case-insensitive substring).
  Returns the device index of the first match. If no match, log a
  warning and return None (fall back to default).

The lookup is performed FRESH on every call -- we open a temporary
PyAudio() instance just to enumerate, then terminate it. That keeps
us robust to hotplug events without needing PnP notification
handling. Cost: ~50 ms per resolution. Negligible compared to the
~150 ms a recording stream takes to open anyway.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


# Module-level lock guarding all PaInstance init / terminate calls
# across the daemon. See the docstring above for why.
pa_lock = threading.Lock()


def list_input_devices() -> list[dict]:
    """Return a list of {index, name, channels, default} dicts for
    every input-capable device PyAudio currently sees.

    Returns [] if PyAudio isn't available or enumeration fails.

    Holds `pa_lock` for the entire duration so a concurrent wake-word
    listener doesn't see torn-down PortAudio state mid-enumerate.
    """
    try:
        import pyaudio
    except ImportError:
        return []
    pa = None
    with pa_lock:
        try:
            pa = pyaudio.PyAudio()
            try:
                default_info = pa.get_default_input_device_info()
                default_idx = int(default_info.get("index", -1))
            except Exception:
                default_idx = -1
            out = []
            for i in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(i)
                except Exception:
                    continue
                if int(info.get("maxInputChannels", 0)) <= 0:
                    continue
                out.append({
                    "index": int(info.get("index", i)),
                    "name": str(info.get("name", "")),
                    "channels": int(info.get("maxInputChannels", 0)),
                    "default": int(info.get("index", i)) == default_idx,
                })
            return out
        except Exception:
            logger.exception("Failed to enumerate audio input devices.")
            return []
        finally:
            if pa is not None:
                try: pa.terminate()
                except Exception: pass


def resolve_input_device_index(name_substring: Optional[str]
                                ) -> Optional[int]:
    """Resolve a config-supplied device-name substring to a current
    PyAudio device index. Returns None to mean "use OS default".

    None / empty input -> None (caller should pass no
    input_device_index to pyaudio.open, which falls back to default).

    A non-empty string is matched case-insensitively as a substring
    of the device's `name`. The first matching INPUT device wins.

    A string that matches NO current device logs a warning and
    returns None so the daemon stays usable rather than crashing
    when the configured device is unplugged.

    list_input_devices already holds pa_lock, so we don't need to
    take it here too.
    """
    if not name_substring:
        return None
    needle = name_substring.lower().strip()
    if not needle:
        return None
    devices = list_input_devices()
    for dev in devices:
        if needle in dev["name"].lower():
            logger.info("Resolved audio_device_name=%r -> #%d %r",
                        name_substring, dev["index"], dev["name"])
            return dev["index"]
    logger.warning(
        "audio_device_name=%r not found among %d input devices; "
        "falling back to OS default. Available: %s",
        name_substring, len(devices),
        [d["name"] for d in devices],
    )
    return None
