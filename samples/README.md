# Sample audio for benchmarking

Two clips are bundled, both public domain:

## `jfk.flac` (~11 s, single sentence)

Public-domain sample shipped in the OpenAI Whisper test suite
(https://github.com/openai/whisper/tree/main/tests). Useful as a quick
smoke test; not long or varied enough to differentiate models on
accuracy.

## `librispeech-1272-128104-0004.flac` (~29 s, with verified reference)

Excerpt from LibriSpeech `dev-clean`
(https://www.openslr.org/12, CC BY 4.0). Speaker 1272, chapter 128104,
utterance 0004. The ground-truth transcript is in
`librispeech-1272-128104-0004.txt` next to the audio file. Includes
proper nouns, archaic phrasing, and rhetorical structure -- a much
better accuracy probe than the JFK clip.

## Running the benchmark

`python benchmark.py samples/librispeech-1272-128104-0004.flac --models tiny base small medium large-v3-turbo --runs 3`

Pass `--reference path/to/reference.txt` to compute Word Error Rate.
The benchmark script normalizes both sides (lowercased, punctuation
stripped, whitespace collapsed) before computing WER, so the LibriSpeech
all-caps ground truth compares cleanly to whisper's mixed-case output.
