"""Non-GUI logic: locating tools, downloading, decoding, waveform peaks, export.

Everything here is GUI-independent so it can be tested on its own.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

# Folders where Homebrew / MacPorts / manual installs usually live. Apps
# launched from Finder do not inherit the shell PATH, so look there explicitly.
_EXTRA_BIN_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]


# --------------------------------------------------------------------------
# Locating bundled / system tools
# --------------------------------------------------------------------------

def _bundle_dirs() -> list[str]:
    """Directories where files bundled by PyInstaller may live."""
    dirs = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs += [meipass, os.path.join(meipass, "bin")]
        # In a macOS .app, data may sit in Contents/Resources next to Frameworks.
        dirs += [os.path.join(os.path.dirname(meipass), "Resources"),
                 os.path.join(os.path.dirname(meipass), "Resources", "bin")]
    here = os.path.dirname(os.path.abspath(__file__))
    dirs += [os.path.join(here, "bin"), os.path.join(os.path.dirname(here), "bin")]
    return dirs


def _which(name: str) -> Optional[str]:
    for d in _bundle_dirs():
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    p = shutil.which(name)
    if p:
        return p
    for d in _EXTRA_BIN_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def find_ffmpeg() -> Optional[str]:
    """Bundled ffmpeg (imageio-ffmpeg) first, then any system ffmpeg."""
    try:
        import imageio_ffmpeg  # noqa: WPS433
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    for d in _bundle_dirs():
        for p in sorted(glob.glob(os.path.join(d, "imageio_ffmpeg", "binaries", "ffmpeg-*"))):
            if os.access(p, os.X_OK):
                return p
    return _which("ffmpeg")


def find_js_runtimes() -> dict:
    """yt-dlp needs a JavaScript runtime to solve YouTube's challenges.

    Returns a ``js_runtimes`` dict for yt-dlp: the bundled deno if present,
    otherwise any deno / node / bun found on the system.
    """
    runtimes = {}
    for name in ("deno", "node", "bun"):
        p = _which(name)
        if p:
            runtimes[name] = {"path": p}
    return runtimes


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

class Cancelled(Exception):
    pass


def download_audio(url: str, workdir: str,
                   progress: Callable[[str, Optional[float]], None] = lambda m, f: None,
                   should_cancel: Callable[[], bool] = lambda: False) -> tuple[str, dict]:
    """Download the best audio stream of ``url`` into ``workdir``.

    Returns (path_to_downloaded_file, info_dict).
    """
    import yt_dlp  # imported lazily: it is slow to import

    def hook(d):
        if should_cancel():
            raise Cancelled()
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            frac = (done / total) if total else None
            speed = d.get("speed")
            msg = "Downloading audio"
            if total:
                msg += f"  {done / 1e6:.1f} / {total / 1e6:.1f} MB"
            if speed:
                msg += f"  ({speed / 1e6:.1f} MB/s)"
            progress(msg, frac)
        elif status == "finished":
            progress("Download finished", 1.0)

    class _Logger:
        def debug(self, msg):
            pass

        def info(self, msg):
            pass

        def warning(self, msg):
            pass

        def error(self, msg):
            pass

    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _Logger(),
        "progress_hooks": [hook],
        "overwrites": True,
        "retries": 5,
        "fragment_retries": 5,
    }
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    runtimes = find_js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes

    progress("Fetching video info…", None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError("Could not get video information.")
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise RuntimeError("Playlist contains no videos.")
            info = entries[0]
        path = None
        for rd in info.get("requested_downloads") or []:
            path = rd.get("filepath") or rd.get("_filename")
            if path:
                break
        if not path:
            path = ydl.prepare_filename(info)
    if not path or not os.path.exists(path):
        raise RuntimeError("Download finished but the audio file was not found.")
    return path, info


# --------------------------------------------------------------------------
# Decoding & waveform data
# --------------------------------------------------------------------------

@dataclass
class AudioData:
    samples: np.ndarray          # shape (frames, channels), int16 (memory-mapped)
    rate: int
    channels: int
    title: str = ""
    source: str = ""
    raw_path: str = ""
    env_min: np.ndarray = field(default=None, repr=False)  # per-block min (mono)
    env_max: np.ndarray = field(default=None, repr=False)  # per-block max (mono)

    BLOCK = 256  # frames per envelope block

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration(self) -> float:
        return self.frames / float(self.rate) if self.rate else 0.0

    def build_envelope(self) -> None:
        """Pre-compute min/max per BLOCK frames so zoomed-out drawing is fast."""
        n_blocks = (self.frames + self.BLOCK - 1) // self.BLOCK
        env_min = np.zeros(n_blocks, dtype=np.int16)
        env_max = np.zeros(n_blocks, dtype=np.int16)
        chunk_blocks = 4096
        for b0 in range(0, n_blocks, chunk_blocks):
            b1 = min(n_blocks, b0 + chunk_blocks)
            f0, f1 = b0 * self.BLOCK, min(self.frames, b1 * self.BLOCK)
            seg = np.asarray(self.samples[f0:f1])
            lo = seg.min(axis=1)
            hi = seg.max(axis=1)
            pad = (b1 - b0) * self.BLOCK - lo.shape[0]
            if pad:
                lo = np.concatenate([lo, np.zeros(pad, dtype=lo.dtype)])
                hi = np.concatenate([hi, np.zeros(pad, dtype=hi.dtype)])
            env_min[b0:b1] = lo.reshape(-1, self.BLOCK).min(axis=1)
            env_max[b0:b1] = hi.reshape(-1, self.BLOCK).max(axis=1)
        self.env_min, self.env_max = env_min, env_max

    def peaks(self, start: float, end: float, width: int):
        """Waveform for the time range [start, end] (seconds) at ``width`` pixels.

        Returns (kind, a, b):
          kind == "bars":  a = mins, b = maxs, each length ``width`` in [-1, 1]
          kind == "line":  a = x positions (pixels, float), b = values in [-1, 1]
        """
        width = max(1, int(width))
        f0 = max(0, int(start * self.rate))
        f1 = min(self.frames, int(np.ceil(end * self.rate)))
        if f1 - f0 < 2:
            return "bars", np.zeros(width), np.zeros(width)
        spp = (end - start) * self.rate / width  # samples per pixel
        scale = 1.0 / 32768.0

        if spp < 2.0:
            seg = np.asarray(self.samples[f0:f1]).astype(np.float32).mean(axis=1) * scale
            t = (np.arange(f0, f1) / self.rate - start) / (end - start) * width
            return "line", t, seg

        if spp >= self.BLOCK * 2 and self.env_min is not None:
            src_lo, src_hi = self.env_min, self.env_max
            i0, i1 = f0 // self.BLOCK, max(f0 // self.BLOCK + 1, f1 // self.BLOCK)
            i1 = min(i1, len(src_lo))
        else:
            seg = np.asarray(self.samples[f0:f1])
            src_lo, src_hi = seg.min(axis=1), seg.max(axis=1)
            i0, i1 = 0, len(src_lo)

        # Pixel boundaries, mapped into the source index range.
        t0 = start * self.rate
        t1 = end * self.rate
        edges = np.linspace(t0, t1, width + 1)
        if src_lo is self.env_min:
            idx = (edges[:-1] / self.BLOCK).astype(np.int64)
        else:
            idx = (edges[:-1] - f0).astype(np.int64)
        idx = np.clip(idx, i0, i1 - 1)
        lo = np.minimum.reduceat(src_lo[i0:i1], idx - i0).astype(np.float32) * scale
        hi = np.maximum.reduceat(src_hi[i0:i1], idx - i0).astype(np.float32) * scale
        # Blank out pixels that lie past the end of the audio.
        valid_end = (edges[:-1] < self.frames)
        lo[~valid_end] = 0
        hi[~valid_end] = 0
        return "bars", lo, hi


_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?: Audio: .*?(\d+) Hz, ([^,]+)")


def probe_audio(ffmpeg: str, src: str) -> tuple[int, int]:
    """Return (sample_rate, channels) of the first audio stream in ``src``."""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", src],
                          capture_output=True, text=True, errors="replace")
    m = _STREAM_RE.search(proc.stderr)
    if not m:
        raise RuntimeError("No audio stream found in this file.\n\n" + proc.stderr[-800:])
    rate = int(m.group(1))
    layout = m.group(2).strip().lower()
    channels = 1 if layout.startswith("mono") or layout == "1 channels" else 2
    return rate, channels


def decode(src: str, workdir: str, title: str = "",
           progress: Callable[[str, Optional[float]], None] = lambda m, f: None) -> AudioData:
    """Decode any audio/video file to 16-bit PCM and build its waveform envelope."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found.")
    rate, channels = probe_audio(ffmpeg, src)
    progress("Decoding audio…", None)
    raw = os.path.join(workdir, "decoded_%d.pcm" % abs(hash(src)))
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-vn", "-map", "a:0", "-f", "s16le", "-acodec", "pcm_s16le",
         "-ac", str(channels), "-ar", str(rate), raw],
        capture_output=True, text=True, errors="replace")
    if proc.returncode != 0 or not os.path.exists(raw):
        raise RuntimeError("ffmpeg failed to decode the audio:\n\n" + proc.stderr[-800:])
    size = os.path.getsize(raw)
    frames = size // (2 * channels)
    if frames == 0:
        raise RuntimeError("The audio stream is empty.")
    samples = np.memmap(raw, dtype="<i2", mode="r", shape=(frames, channels))
    audio = AudioData(samples=samples, rate=rate, channels=channels,
                      title=title or os.path.splitext(os.path.basename(src))[0],
                      source=src, raw_path=raw)
    progress("Building waveform…", None)
    audio.build_envelope()
    return audio


# --------------------------------------------------------------------------
# Export / preview
# --------------------------------------------------------------------------

def _frames_range(audio: AudioData, start: float, end: float) -> tuple[int, int]:
    f0 = max(0, min(audio.frames, int(round(start * audio.rate))))
    f1 = max(0, min(audio.frames, int(round(end * audio.rate))))
    if f1 <= f0:
        raise ValueError("The selection is empty.")
    return f0, f1


def export_wav(audio: AudioData, start: float, end: float, path: str,
               sample_rate: Optional[int] = None, channels: Optional[int] = None,
               fade_ms: float = 0.0) -> None:
    """Write the selected range to a 16-bit PCM WAV file.

    ``sample_rate`` / ``channels`` of None keep the original values (no
    resampling). Otherwise ffmpeg converts the audio.
    """
    f0, f1 = _frames_range(audio, start, end)
    out_rate = sample_rate or audio.rate
    out_ch = channels or audio.channels
    chunk = 1 << 18

    def chunks():
        fade_n = int(audio.rate * fade_ms / 1000.0)
        fade_n = min(fade_n, (f1 - f0) // 2)
        for c0 in range(f0, f1, chunk):
            c1 = min(f1, c0 + chunk)
            block = np.asarray(audio.samples[c0:c1])
            if fade_n > 0 and (c0 < f0 + fade_n or c1 > f1 - fade_n):
                block = block.astype(np.float32)
                pos = np.arange(c0, c1)
                gain = np.ones(c1 - c0, dtype=np.float32)
                gain = np.minimum(gain, (pos - f0) / fade_n)
                gain = np.minimum(gain, (f1 - pos) / fade_n)
                gain = np.clip(gain, 0.0, 1.0)
                block = np.clip(block * gain[:, None], -32768, 32767).astype("<i2")
            yield block.astype("<i2", copy=False).tobytes()

    tmp = path + ".part"
    try:
        if out_rate == audio.rate and out_ch == audio.channels:
            with wave.open(tmp, "wb") as w:
                w.setnchannels(audio.channels)
                w.setsampwidth(2)
                w.setframerate(audio.rate)
                for data in chunks():
                    w.writeframes(data)
        else:
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                raise RuntimeError("ffmpeg is needed to change sample rate/channels.")
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "s16le", "-ar", str(audio.rate), "-ac", str(audio.channels),
                   "-i", "pipe:0", "-ar", str(out_rate), "-ac", str(out_ch),
                   "-acodec", "pcm_s16le", "-f", "wav", tmp]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for data in chunks():
                    proc.stdin.write(data)
                proc.stdin.close()
            except BrokenPipeError:
                pass
            err = proc.stderr.read().decode(errors="replace")
            if proc.wait() != 0:
                raise RuntimeError("ffmpeg failed to write the WAV file:\n\n" + err[-800:])
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class Player:
    """Plays a range of audio through a command-line player.

    macOS ships ``afplay``; elsewhere ffplay / aplay / paplay are tried.
    """

    def __init__(self, workdir: str):
        self.workdir = workdir
        self.proc: Optional[subprocess.Popen] = None
        self._file = os.path.join(workdir, "preview.wav")

    @staticmethod
    def _command(path: str) -> Optional[list[str]]:
        if sys.platform == "darwin" and os.path.exists("/usr/bin/afplay"):
            return ["/usr/bin/afplay", path]
        ffplay = _which("ffplay")
        if ffplay:
            return [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
        for name in ("paplay", "aplay"):
            p = _which(name)
            if p:
                return [p, path]
        return None

    def available(self) -> bool:
        return self._command(self._file) is not None

    def play(self, audio: AudioData, start: float, end: float) -> None:
        self.stop()
        export_wav(audio, start, end, self._file)
        cmd = self._command(self._file)
        if not cmd:
            raise RuntimeError("No audio player found on this system.")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def is_playing(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


def make_workdir() -> str:
    return tempfile.mkdtemp(prefix="audioripper_")


# --------------------------------------------------------------------------
# Time formatting / parsing
# --------------------------------------------------------------------------

def format_time(t: float, decimals: int = 3) -> str:
    scale = 10 ** decimals
    units = int(round(max(0.0, t) * scale))  # integer count of 10^-decimals s
    h, rem = divmod(units, 3600 * scale)
    m, rem = divmod(rem, 60 * scale)
    sec, frac = divmod(rem, scale)
    s_str = f"{sec:02d}" + (f".{frac:0{decimals}d}" if decimals else "")
    if h:
        return f"{h}:{m:02d}:{s_str}"
    return f"{m}:{s_str}"


def parse_time(text: str) -> float:
    """Parse '83.5', '1:23.5' or '1:02:03.25' into seconds."""
    text = text.strip()
    if not text:
        raise ValueError("empty time")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad time: {text}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    if total < 0:
        raise ValueError("negative time")
    return total


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .")
    return (name or "audio")[:120]
