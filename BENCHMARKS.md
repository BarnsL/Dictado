# Benchmarks

These numbers were produced by `benchmark.py` and written to
`benchmark_results.json`. They are reproducible: run `python benchmark.py`
on your own machine to get a comparable table.

## Reproducing

```bash
python benchmark.py samples/jfk.flac \
    --models tiny base small medium \
    --runs   3
```

The script:

1. Loads each model **sequentially** (one in RAM at a time — `medium`
   uses ~1.5 GB and `large` uses ~3 GB, parallel runs would thrash).
2. Runs **one warm-up** transcription (PyTorch lazily compiles kernels on
   first call; the first run is always slower).
3. Runs **N timed transcriptions** of the same clip and reports the median.
4. Drops the model and forces a `gc.collect()` before loading the next.

The first-ever load of each model also downloads the weights from
`openaipublic.azureedge.net` to `~/.cache/whisper/`. That download time
is **not** included in the `load_time` figure unless you delete the
cache between runs.

## Reference numbers (CPU only)

Test environment: 12-core x86 laptop CPU, 32 GB RAM, no GPU, Python 3.13,
PyTorch 2.12 CPU build, Windows 11. Audio is the public-domain JFK clip
from the OpenAI Whisper test suite (~11 s, mono, 16 kHz).

| Model  | Load (s) | Warm-up (s) | Median run (s) | Realtime factor |
|--------|---------:|------------:|---------------:|----------------:|
| tiny   |    6.21  |       0.93  |          0.66  |       ~16.7×    |
| base   |    0.66  |       1.36  |          1.37  |        ~8.0×    |
| small  |    2.69  |       3.21  |          3.01  |        ~3.7×    |
| medium |    6.93  |      10.28  |          9.41  |        ~1.2×    |

> Realtime factor ≈ clip duration / median run. Anything > 1× transcribes
> faster than real-time. The streaming-preview feature wants the chosen
> model to comfortably exceed 1× — at `medium` on this CPU it's marginal.

The `tiny` load time looks anomalously high (6.2 s) because that was the
first model loaded in this session, so the OS file cache was cold for the
PyTorch shared libraries. The subsequent `base` load (0.66 s) is what a
warm load looks like — the kernel cached the libraries from the `tiny`
run.

## Accuracy (qualitative)

All four models on the same clip:

```
tiny   : And so my fellow Americans ask not what your country can do for you ask what you can do for your country.
base   : And so my fellow Americans ask not what your country can do for you, ask what you can do for your country.
small  : And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country.
medium : And so, my fellow Americans, ask not what your country can do for you, ask what you can do for your country.
```

Word-for-word every model gets it right on this clip. The differences are
in punctuation: `tiny` produces no commas at all; `base` adds the comma
after the first clause; `small` adds the second comma; `medium` adds the
opening comma after "and so". For most users `base` is the
accuracy/speed sweet spot on CPU; `small` is the right choice if commas
matter for downstream tooling (Markdown headers, code comments, etc.).

A more challenging clip with proper nouns, numbers, and disfluencies
would widen the accuracy gap; this one doesn't. If you have such a clip,
pass it to `benchmark.py` and the report will use it.

## Word Error Rate (optional)

`benchmark.py` does not compute WER by default because it requires a
ground-truth transcript. If you want it, add `jiwer`:

```bash
pip install jiwer
```

then pass `--reference reference.txt` and the script will compute
`jiwer.wer(reference, hypothesis)` per model.

## How to interpret

| Realtime factor | What it means in practice |
|---|---|
| > 4× | Streaming partials feel ahead-of-speech in the popup. |
| 2-4× | Streaming partials feel "live". |
| 1-2× | Streaming partials lag behind speech but catch up. |
| < 1× | Don't use this model for streaming partials; final-pass only. |

`tiny` and `base` are appropriate for the streaming-partials feature on
this kind of CPU. `small` is borderline. `medium` is final-pass only.

If you have a CUDA-capable GPU the picture changes substantially: every
model becomes 5-15× faster, and `medium` or even `large` becomes a fine
choice for streaming. Edit `WHISPER_FP16` to `True` in
`dictado/daemon.py` to enable mixed precision on CUDA.
