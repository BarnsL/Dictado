"""
benchmark.py — sequential model benchmark for the local Whisper service

Runs the requested models one at a time (NEVER in parallel — keeps RAM low)
on a single audio clip, and writes:

  * benchmark_results.json — machine-readable raw numbers
  * BENCHMARKS.md          — human-readable Markdown table

Usage
-----
    python benchmark.py path/to/clip.wav
    python benchmark.py                # uses the bundled samples/jfk.wav

If you don't pass an audio file and the bundled sample isn't present, the
script prints clear instructions for recording one yourself.

Why sequential?
---------------
Whisper holds the entire model in RAM. Loading the medium model takes
~1.5 GB, large takes ~3 GB. Running them in parallel on a typical laptop
would either OOM or thrash. We load -> transcribe -> drop -> next.

What we measure
---------------
  load_time_s        — whisper.load_model(name)            (first call only)
  warmup_time_s      — first transcription (often slower due to JIT cache)
  transcribe_time_s  — median of N transcriptions of the same clip
  realtime_factor    — clip_duration_s / transcribe_time_s
                       higher = faster than real-time
  text               — what the model heard (so you can grade accuracy)

Hardware/environment captured for the report
--------------------------------------------
  * CPU model and core count (platform.processor + os.cpu_count)
  * Total RAM (best-effort, psutil if available, else parsed from systeminfo)
  * OS string
  * PyTorch version + whether CUDA is available
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import statistics
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

# All defaults are conservative for a typical laptop. Override with --models.
DEFAULT_MODELS = ["tiny", "base", "small", "medium"]
DEFAULT_RUNS_PER_MODEL = 3   # one warm-up + this many timed runs


def human_size(bytes_):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if bytes_ < 1024:
            return f"{bytes_:.1f} {unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f} PB"


def detect_environment():
    """Return a dict describing the host. Used for the BENCHMARKS.md header."""
    info = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu": platform.processor() or "unknown",
        "cores": os.cpu_count() or 0,
        "ram_gb": None,
        "torch": None,
        "cuda": False,
        "device": "cpu",
    }
    try:
        import psutil  # type: ignore
        info["ram_gb"] = round(psutil.virtual_memory().total / (1024**3), 1)
    except ImportError:
        # Best-effort fallback on Windows.
        if os.name == "nt":
            try:
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [("dwLength", ctypes.c_ulong),
                                ("dwMemoryLoad", ctypes.c_ulong),
                                ("ullTotalPhys", ctypes.c_ulonglong),
                                ("ullAvailPhys", ctypes.c_ulonglong),
                                ("ullTotalPageFile", ctypes.c_ulonglong),
                                ("ullAvailPageFile", ctypes.c_ulonglong),
                                ("ullTotalVirtual", ctypes.c_ulonglong),
                                ("ullAvailVirtual", ctypes.c_ulonglong),
                                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
                m = MEMORYSTATUSEX()
                m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
                info["ram_gb"] = round(m.ullTotalPhys / (1024**3), 1)
            except Exception:
                pass
    try:
        import torch  # type: ignore
        info["torch"] = torch.__version__
        info["cuda"] = bool(torch.cuda.is_available())
        info["device"] = "cuda" if info["cuda"] else "cpu"
    except ImportError:
        pass
    return info


def audio_duration_seconds(path: Path) -> float:
    """Return the duration of a WAV in seconds. For non-WAV inputs we let
    whisper figure it out and we can't print this stat — but we'll still
    measure transcription time."""
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())
    except (wave.Error, EOFError):
        return 0.0


def benchmark_one(model_name: str, audio_path: Path, runs: int):
    """Load model_name, run `runs+1` transcriptions (1 warmup + `runs` timed),
    return a dict of measurements. Frees the model afterward."""
    import whisper
    print(f"\n=== {model_name} =========================================")
    sys.stdout.flush()

    t0 = time.perf_counter()
    model = whisper.load_model(model_name)
    load_time = time.perf_counter() - t0
    print(f"  load_time           : {load_time:6.2f} s")
    sys.stdout.flush()

    # Warm-up run (often slower due to JIT / first-call init).
    t0 = time.perf_counter()
    warm_result = model.transcribe(str(audio_path), language="en", fp16=False)
    warmup_time = time.perf_counter() - t0
    text = (warm_result.get("text") or "").strip()
    print(f"  warmup_transcribe   : {warmup_time:6.2f} s")
    print(f"  text (warmup)       : {text[:140]}")
    sys.stdout.flush()

    # Timed runs.
    timings = []
    for i in range(runs):
        t0 = time.perf_counter()
        result = model.transcribe(str(audio_path), language="en", fp16=False)
        timings.append(time.perf_counter() - t0)
        print(f"  run {i+1}/{runs}            : {timings[-1]:6.2f} s")
        sys.stdout.flush()
        # Sanity-check that text is identical run-over-run.
        run_text = (result.get("text") or "").strip()
        if run_text != text:
            print(f"  [warn] run {i+1} text differs from warmup")

    median = statistics.median(timings)
    mean   = statistics.mean(timings)

    duration = audio_duration_seconds(audio_path)
    realtime = (duration / median) if median > 0 and duration > 0 else None

    print(f"  median(runs)        : {median:6.2f} s")
    if realtime is not None:
        print(f"  realtime_factor     : x{realtime:.2f}")
    sys.stdout.flush()

    # Drop the model and force a GC pass before the next iteration so RAM
    # doesn't accumulate. Whisper models on CPU release reasonably cleanly.
    del warm_result, result, model
    gc.collect()
    try:
        import torch  # type: ignore
        if hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass

    return {
        "model":              model_name,
        "load_time_s":        round(load_time, 3),
        "warmup_time_s":      round(warmup_time, 3),
        "transcribe_time_s":  round(median, 3),
        "transcribe_mean_s":  round(mean, 3),
        "transcribe_runs":    [round(t, 3) for t in timings],
        "realtime_factor":    round(realtime, 2) if realtime else None,
        "text":               text,
    }


def find_default_audio() -> Path | None:
    """Look for samples/jfk.wav next to this script. That's the canonical
    11-second 'ask not what your country can do for you' clip the OpenAI
    whisper repo ships with."""
    here = Path(__file__).resolve().parent
    candidate = here / "samples" / "jfk.wav"
    if candidate.exists():
        return candidate
    return None


def write_markdown(env, audio_path, audio_duration, results, out_path: Path):
    """Render a human-friendly BENCHMARKS.md with hardware header + table."""
    lines = []
    lines.append("# Benchmark results")
    lines.append("")
    lines.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_")
    lines.append("")
    lines.append("## Test environment")
    lines.append("")
    lines.append(f"- **OS**: {env['os']}")
    lines.append(f"- **CPU**: {env['cpu']}  ({env['cores']} cores)")
    if env["ram_gb"]:
        lines.append(f"- **RAM**: {env['ram_gb']} GB")
    lines.append(f"- **Python**: {env['python']}")
    if env["torch"]:
        lines.append(f"- **PyTorch**: {env['torch']}")
    lines.append(f"- **Device**: {env['device']}"
                 + (" (CUDA available)" if env["cuda"] else ""))
    lines.append(f"- **Audio clip**: `{audio_path.name}`"
                 + (f" ({audio_duration:.1f} s)" if audio_duration else ""))
    lines.append("")

    lines.append("## Speed (per model)")
    lines.append("")
    lines.append("| Model | Load (s) | Warmup (s) | Median run (s) | Realtime factor |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        rt = f"x{r['realtime_factor']}" if r["realtime_factor"] else "—"
        lines.append(f"| **{r['model']}** | {r['load_time_s']} "
                     f"| {r['warmup_time_s']} "
                     f"| {r['transcribe_time_s']} "
                     f"| {rt} |")
    lines.append("")
    lines.append("- _Load_: time for `whisper.load_model(name)` (cold disk read).")
    lines.append("- _Warmup_: first `transcribe()` call. PyTorch lazily compiles "
                 "kernels on the first run, so this is usually the slowest.")
    lines.append("- _Median run_: median of the timed runs after warm-up.")
    lines.append("- _Realtime factor_: how many seconds of audio each "
                 "wall-clock second of compute can transcribe. Anything > 1 is "
                 "faster than real-time.")
    lines.append("")

    lines.append("## Accuracy (eyeball)")
    lines.append("")
    lines.append("Each model's transcription of the SAME audio clip. Compare "
                 "against the source and judge for yourself.")
    lines.append("")
    for r in results:
        lines.append(f"### {r['model']}")
        lines.append("")
        lines.append("> " + (r["text"] or "_(empty)_").replace("\n", " "))
        lines.append("")

    lines.append("## How this was measured")
    lines.append("")
    lines.append("Run with `python benchmark.py [audio.wav]`. The script:")
    lines.append("")
    lines.append("1. Loads each model **sequentially** (one in RAM at a time).")
    lines.append("2. Runs one warm-up transcription (untimed total but reported).")
    lines.append("3. Runs N timed transcriptions and reports the median.")
    lines.append("4. Drops the model and forces a GC before loading the next.")
    lines.append("")
    lines.append("The first-ever load of each model also downloads the weights "
                 "from `openaipublic.azureedge.net` to "
                 "`~/.cache/whisper/`. That download time is NOT included in "
                 "the load_time figure unless you delete the cache.")
    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {out_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", nargs="?", type=Path,
                        help="WAV/MP3/etc to transcribe. "
                             "Defaults to samples/jfk.wav next to this script.")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS,
                        help=f"Models to benchmark (default: {' '.join(DEFAULT_MODELS)})")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS_PER_MODEL,
                        help=f"Timed runs per model after warm-up (default: {DEFAULT_RUNS_PER_MODEL})")
    parser.add_argument("--out-dir", type=Path,
                        default=Path(__file__).resolve().parent,
                        help="Where to write benchmark_results.json and BENCHMARKS.md")
    args = parser.parse_args()

    audio_path = args.audio or find_default_audio()
    if audio_path is None or not audio_path.exists():
        print("No audio clip provided and samples/jfk.wav is missing.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Either pass a file:", file=sys.stderr)
        print("    python benchmark.py path/to/clip.wav", file=sys.stderr)
        print("", file=sys.stderr)
        print("Or download the sample (11s, public domain):", file=sys.stderr)
        print("    curl -L -o samples/jfk.wav \\", file=sys.stderr)
        print("      https://github.com/openai/whisper/raw/main/tests/jfk.flac", file=sys.stderr)
        sys.exit(2)

    duration = audio_duration_seconds(audio_path)
    env = detect_environment()

    print("Test environment:")
    for k, v in env.items():
        print(f"  {k:>8}: {v}")
    print(f"  audio   : {audio_path}  ({duration:.1f} s)")
    print(f"  models  : {' -> '.join(args.models)}  (sequential)")
    print(f"  runs    : {args.runs} timed per model (after one warm-up)")

    results = []
    for name in args.models:
        try:
            r = benchmark_one(name, audio_path, args.runs)
            results.append(r)
        except KeyboardInterrupt:
            print("\nInterrupted; writing partial results.")
            break
        except Exception as e:
            print(f"\n[error] {name}: {e}")
            results.append({"model": name, "error": str(e)})

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "benchmark_results.json"
    md_path   = out_dir / "BENCHMARKS.md"

    summary = {
        "generated":  datetime.now().isoformat(timespec="seconds"),
        "environment": env,
        "audio": {"path": str(audio_path), "duration_s": round(duration, 2)},
        "results": results,
    }
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nWrote {json_path}")
    write_markdown(env, audio_path, duration,
                   [r for r in results if "error" not in r], md_path)


if __name__ == "__main__":
    main()
