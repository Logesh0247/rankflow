"""RANKFLOW AI — Video Intelligence Agent, Phase 1: ingestion (Prompt 05).

Probes a local video file with ffprobe and validates it before any
frame is read. A file is rejected (never analysed) when it does not
exist, is not a readable container, has no video stream, has zero
or unknown duration, or ffprobe reports an error — that is the
corrupted-file detection. Audio streams are inspected (presence,
codec, sample rate) but audio content is not transcribed here.

Nothing in this module downloads a video or touches a candidate:
the rights gate runs before it, in the service.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.errors import StorageError, ValidationError


class VideoProbe(BaseModel):
    """Measured properties of a video file — all from ffprobe, never guessed."""

    path: str
    duration_seconds: float = Field(..., gt=0)
    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)
    fps: float = Field(..., gt=0)
    video_codec: str
    audio_codec: str | None = None
    has_audio: bool = False
    audio_sample_rate: int | None = None
    container: str = ""
    frame_count: int | None = None


def probe_video(path: str | Path, *, ffprobe_path: str = "ffprobe",
                timeout_seconds: float = 30.0) -> VideoProbe:
    """Run ffprobe on a local file and return its measured properties.

    Raises StorageError if the file is missing, ValidationError if
    the file is not a valid video (no video stream, zero duration,
    ffprobe error). The error message says what was measured, so a
    corrupted file is distinguishable from a missing one.
    """
    path = Path(path)
    if not path.exists():
        raise StorageError(f"video file not found: {path}")
    if not path.is_file():
        raise StorageError(f"video path is not a file: {path}")
    try:
        proc = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name,r_frame_rate,"
             "avg_frame_rate,nb_frames",
             "-show_entries", "format=duration,format_name",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StorageError(f"ffprobe failed on {path}: {exc}") from exc
    if proc.returncode != 0:
        raise ValidationError(
            f"file is not a readable video (ffprobe exit {proc.returncode}): {path}"
            + (f" — {proc.stderr.strip()[:200]}" if proc.stderr.strip() else ""))
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise ValidationError(f"ffprobe returned unparsable output for {path}: {exc}") from exc
    streams = data.get("streams") or []
    if not streams:
        raise ValidationError(f"file has no video stream (not a video): {path}")
    stream = streams[0]
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ValidationError(f"video has zero or unknown duration: {path}")
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValidationError(f"video stream has no usable dimensions: {path}")
    fps = _parse_fps(stream.get("r_frame_rate") or stream.get("avg_frame_rate"))
    if fps <= 0:
        raise ValidationError(f"video stream has no usable frame rate: {path}")
    audio = _probe_audio(path, ffprobe_path, timeout_seconds)
    frame_count = None
    try:
        frame_count = int(stream.get("nb_frames")) if stream.get("nb_frames") not in (None, "N/A") else None
    except (TypeError, ValueError):
        frame_count = None
    return VideoProbe(
        path=str(path),
        duration_seconds=duration,
        width=width, height=height, fps=fps,
        video_codec=str(stream.get("codec_name") or "unknown"),
        audio_codec=audio[0], has_audio=audio[0] is not None,
        audio_sample_rate=audio[1],
        container=str(fmt.get("format_name") or ""),
        frame_count=frame_count)


def _parse_fps(raw: str) -> float:
    if not raw or raw == "0/0":
        return 0.0
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _probe_audio(path: Path, ffprobe_path: str, timeout_seconds: float) -> tuple[str | None, int | None]:
    """Best-effort audio-stream inspection; absence of audio is not an error."""
    try:
        proc = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,sample_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=timeout_seconds)
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
        streams = data.get("streams") or []
        if not streams:
            return None, None
        rate = None
        try:
            rate = int(streams[0].get("sample_rate")) if streams[0].get("sample_rate") else None
        except (TypeError, ValueError):
            rate = None
        return str(streams[0].get("codec_name") or "unknown"), rate
    except Exception:
        return None, None
