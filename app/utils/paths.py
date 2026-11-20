"""Path and validation utilities."""
from __future__ import annotations

import shutil
import subprocess


def which_ffmpeg(ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> dict:
    """Check FFmpeg / FFprobe are callable. Returns versions or ''."""
    out: dict[str, str] = {}
    for name, path in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)):
        exe = shutil.which(path) or path
        try:
            proc = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10)
            out[name] = proc.stdout.splitlines()[0] if proc.stdout else ""
        except Exception:
            out[name] = ""
    return out
