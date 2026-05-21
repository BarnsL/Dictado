# Whisper models supported by Dictado

Dictado loads any model the upstream `openai-whisper` package
recognises. The catalog of names plus per-model metadata lives in
[`dictado/models.py`](../dictado/models.py); this doc is a friendly
summary of what's available and which one to pick.

## Full lineup

| Model            | Params   | Disk    | CPU RAM | Multilingual? | Notes                                                            |
|------------------|---------:|--------:|--------:|:-------------:|------------------------------------------------------------------|
| `tiny.en`        |    39 M  |   75 MB |   0.4 GB|       No      | Fastest English-only checkpoint                                  |
| `tiny`           |    39 M  |   75 MB |   0.4 GB|      Yes      | Fastest multilingual checkpoint                                  |
| `base.en`        |    74 M  |  140 MB |   0.5 GB|       No      | Good streaming default for English                               |
| `base`           |    74 M  |  140 MB |   0.5 GB|      Yes      | Good streaming default for multilingual                          |
| `small.en`       |   244 M  |  460 MB |   1.0 GB|       No      | High-accuracy English; ~3× realtime on a laptop CPU              |
| `small`          |   244 M  |  460 MB |   1.0 GB|      Yes      | High-accuracy multilingual                                       |
| `medium.en`      |   769 M  |  1.5 GB |   1.5 GB|       No      | English accuracy near a human transcriber                        |
| `medium`         |   769 M  |  1.5 GB |   1.5 GB|      Yes      | The previous default                                             |
| `large-v1`       | 1 550 M  |  2.9 GB |   3.0 GB|      Yes      | Original (late-2022) checkpoint                                  |
| `large-v2`       | 1 550 M  |  2.9 GB |   3.0 GB|      Yes      | Improved over v1 on noisy / niche-language audio                 |
| `large-v3`       | 1 550 M  |  2.9 GB |   3.0 GB|      Yes      | Current state-of-the-art accuracy                                |
| `large-v3-turbo` |   809 M  |  1.5 GB |   1.5 GB|      Yes      | ~5× faster than `large-v3` on CPU; accuracy is comparable on EN  |
| `large` *(alias)*|        — |       — |       — |               | Resolves to `large-v3` in `openai-whisper`                       |
| `turbo` *(alias)*|        — |       — |       — |               | Resolves to `large-v3-turbo`                                     |

The `large` and `turbo` aliases are recognised wherever you'd specify a
model name. The catalog dedupes them so they don't appear twice in the
tray menu.

## Picking one

| Your machine                                | Try first                  | Why                                                                       |
|---------------------------------------------|----------------------------|---------------------------------------------------------------------------|
| Modern laptop, mostly English               | `medium.en` or `turbo`     | Cleanest English; turbo is ~5× faster.                                    |
| Older laptop                                | `base.en` or `small.en`    | Stays under 1 GB; sub-second transcription means streaming partials work. |
| You switch languages mid-session            | `medium` or `turbo`        | The `*.en` variants can't transcribe non-English audio.                   |
| You have a CUDA GPU                         | `large-v3` or `turbo`      | RAM cost scales differently on GPU — pick the most accurate that fits.    |
| Embedded / Raspberry Pi 5 / etc.            | `tiny.en`                  | Anything bigger is impractical without an accelerator.                    |

## Switching

The tray menu under **Model** lists five canonical defaults. Anything in
the full lineup is loadable via the IPC trigger:

```bash
dictado --switch-model medium.en
dictado --switch-model large-v3-turbo
dictado --switch-model large-v2
```

Or edit `config.json` and restart.

## Customising the tray-menu list

The five models exposed in the tray are configured at the top of
`dictado/models.py` as `DEFAULT_VISIBLE`:

```python
DEFAULT_VISIBLE: tuple[str, ...] = (
    "base",
    "small",
    "medium",
    "large-v3-turbo",
    "large-v3",
)
```

Reorder, remove, or add entries to taste. Every model in `CATALOG`
remains loadable from the CLI regardless of menu visibility.

## Where the weights end up

The first time Dictado loads a given model, `openai-whisper` downloads
the weights from OpenAI's public Azure CDN to:

```
~/.cache/whisper/<model_name>.pt
```

Subsequent loads are pure disk reads. Sizes are listed in the table
above; pre-warm the cache for everything you might want with:

```bash
python - <<'PY'
import whisper
for n in (
    "tiny.en", "tiny", "base.en", "base",
    "small.en", "small", "medium.en", "medium",
    "large-v3", "large-v3-turbo",
):
    whisper.load_model(n)
PY
```

About 10 GB total if you really do all of them. Don't do that on a
metered connection.

## Where these numbers come from

Parameter counts and approximate disk sizes are reproduced from
OpenAI's published Whisper README:
<https://github.com/openai/whisper#available-models-and-languages>.

RAM figures are observed working-set after `whisper.load_model(name)`
on Dictado's own benchmark harness; expect ~10% variance depending on
your PyTorch build and OS allocator.
