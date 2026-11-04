"""Headless check of decode -> waveform -> WAV export using the bundled ffmpeg."""
import os
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from audio_ripper import core  # noqa: E402

ff = core.find_ffmpeg()
assert ff, "ffmpeg not found"
d = tempfile.mkdtemp()
src = os.path.join(d, "tone.m4a")
subprocess.run([ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=30", "-ac", "2", "-c:a", "aac", src], check=True)
audio = core.decode(src, d)
assert abs(audio.duration - 30) < 0.2, audio.duration
kind, lo, hi = audio.peaks(0, audio.duration, 800)
assert kind == "bars" and hi.max() > 0.05
out = os.path.join(d, "clip.wav")
core.export_wav(audio, 5.0, 7.5, out)
with wave.open(out) as w:
    assert w.getnchannels() == audio.channels
    assert abs(w.getnframes() / w.getframerate() - 2.5) < 0.01
core.export_wav(audio, 5.0, 7.5, out, sample_rate=44100, channels=1, fade_ms=10)
with wave.open(out) as w:
    assert (w.getframerate(), w.getnchannels()) == (44100, 1)
print("js runtimes:", core.find_js_runtimes())
print("smoke test OK")
