# Sample audio for benchmarking

`jfk.flac` is the public-domain audio clip shipped in the OpenAI Whisper
test suite (https://github.com/openai/whisper/blob/main/tests/jfk.flac).
~11 seconds of mono speech.

To benchmark on your own audio, drop a WAV/MP3/FLAC anywhere and pass it:

    python benchmark.py path/to/clip.wav --models tiny base small medium --runs 3
