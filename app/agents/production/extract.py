"""RANKFLOW AI — Video Production Agent, Phase 2: clip extraction (Prompt 08).

Extracts one precise segment from an approved source file with
FFmpeg, re-encoded to the 9:16 target (1080x1920 @ 30fps, H.264)
with its audio (or a silent track when the source has none, so
every segment has the same streams for concatenation).

Validation before any byte is rendered:
* the source file exists and ffprobe reads it (corrupt / missing
  files are rejected, never rendered);
* start >= 0, end > start;
* end does not run past the source duration (a 0.1s tolerance for
  container rounding);
* the rendered segment's measured duration matches the requested
  duration within 0.25s — otherwise the extraction is a failure,
  not a silent short clip.

Seek is placed AFTER -i (accurate seek) and the segment is
re-encoded, because frame-accurate cuts are what the EDL
timestamps require; -ss before -i would be fast but keyframe-bound.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.production.reframe import TARGET_HEIGHT, TARGET_WIDTH, reframe_filter
from app.agents.video_intelligence.probe import VideoProbe, probe_video
from app.core.errors import ExternalServiceError, StorageError, ValidationError

DURATION_TOLERANCE_SECONDS = 0.25
END_TOLERANCE_SECONDS = 0.1


class ExtractedClip(BaseModel):
    """One extracted + reframed segment, measured after rendering."""

    source_path: str
    start_seconds: float
    end_seconds: float
    requested_duration_seconds: float
    output_path: str
    duration_seconds: float = Field(..., gt=0)
    width: int = TARGET_WIDTH
    height: int = TARGET_HEIGHT
    fps: float = 30.0
    video_codec: str = "h264"
    has_audio: bool = True
    audio_codec: str = "aac"
    crop_window: dict = Field(default_factory=dict)
    subject_aware: bool = False


def _run_ffmpeg(args: list[str], *, ffmpeg_path: str, timeout_seconds: float) -> None:
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-y", "-v", "error", *args],
            capture_output=True, text=True, timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExternalServiceError(f"ffmpeg failed to run: {exc}") from exc
    if proc.returncode != 0:
        raise ExternalServiceError(
            f"ffmpeg exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}")


def extract_clip(source_path: str | Path, start_seconds: float, end_seconds: float,
                 output_path: str | Path, *, ffmpeg_path: str = "ffmpeg",
                 ffprobe_path: str = "ffprobe", fps: int = 30,
                 extra_video_filters: str = "",
                 subject_x: float | None = None,
                 timeout_seconds: float = 300.0) -> ExtractedClip:
    """Extract [start, end) from a source file into a 1080x1920 segment.

    `extra_video_filters` is appended after the reframe chain (and
    before format conversion) — Production uses it for graphics,
    captions and fades. The segment always has an audio stream:
    the source audio when present, otherwise generated silence.
    """
    source = Path(source_path)
    output = Path(output_path)
    if start_seconds < 0:
        raise ValidationError(f"clip start {start_seconds}s is negative")
    if end_seconds <= start_seconds:
        raise ValidationError(
            f"clip end {end_seconds}s must be after start {start_seconds}s")
    duration = end_seconds - start_seconds
    probe = probe_video(source, ffprobe_path=ffprobe_path)
    if end_seconds > probe.duration_seconds + END_TOLERANCE_SECONDS:
        raise ValidationError(
            f"clip end {end_seconds}s runs past the source duration "
            f"{probe.duration_seconds:.2f}s for {source}")
    chain, window = reframe_filter(
        probe.width, probe.height, subject_x=subject_x, fps=fps)
    video_filters = chain
    if extra_video_filters:
        video_filters += f",{extra_video_filters}"
    video_filters += ",format=yuv420p"
    output.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = ["-i", str(source)]
    if not probe.has_audio:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    args += [
        "-ss", f"{start_seconds:.3f}",
        "-t", f"{duration:.3f}",
        "-vf", video_filters,
        "-map", "0:v:0",
        "-map", "1:a:0" if not probe.has_audio else "0:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-r", str(fps),
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        str(output),
    ]
    _run_ffmpeg(args, ffmpeg_path=ffmpeg_path, timeout_seconds=timeout_seconds)
    rendered = probe_video(output, ffprobe_path=ffprobe_path)
    if rendered.width != TARGET_WIDTH or rendered.height != TARGET_HEIGHT:
        raise ValidationError(
            f"extracted clip is {rendered.width}x{rendered.height}, "
            f"expected {TARGET_WIDTH}x{TARGET_HEIGHT}")
    if abs(rendered.duration_seconds - duration) > DURATION_TOLERANCE_SECONDS:
        raise ValidationError(
            f"extracted clip duration {rendered.duration_seconds:.2f}s does not "
            f"match the requested {duration:.2f}s (tolerance "
            f"{DURATION_TOLERANCE_SECONDS}s)")
    if not rendered.has_audio:
        raise ValidationError(f"extracted clip has no audio stream: {output}")
    return ExtractedClip(
        source_path=str(source), start_seconds=start_seconds, end_seconds=end_seconds,
        requested_duration_seconds=round(duration, 3), output_path=str(output),
        duration_seconds=rendered.duration_seconds,
        width=rendered.width, height=rendered.height, fps=rendered.fps,
        video_codec=rendered.video_codec, has_audio=rendered.has_audio,
        audio_codec=rendered.audio_codec or "aac",
        crop_window={"width": window.width, "height": window.height,
                     "x": window.x, "y": window.y},
        subject_aware=window.subject_aware)


def render_silent_segment(output_path: str | Path, duration_seconds: float,
                          *, ffmpeg_path: str = "ffmpeg",
                          extra_video_filters: str = "",
                          timeout_seconds: float = 300.0) -> ExtractedClip:
    """Render a solid-colour title/outro card segment (no source file).

    Used for the intro and outro cards. The card is a dark frame
    with the overlay filters supplied by the caller (drawtext);
    audio is generated silence so the segment concatenates cleanly.
    """
    output = Path(output_path)
    if duration_seconds <= 0:
        raise ValidationError("card duration must be positive")
    video_filters = f"format=yuv420p"
    if extra_video_filters:
        video_filters = f"{extra_video_filters},{video_filters}"
    output.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-f", "lavfi", "-i",
        f"color=c=0x0d0d1a:s={TARGET_WIDTH}x{TARGET_HEIGHT}:r=30:d={duration_seconds:.3f}",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-vf", video_filters,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-r", "30", "-c:a", "aac", "-b:a", "128k",
        "-t", f"{duration_seconds:.3f}",
        str(output),
    ]
    _run_ffmpeg(args, ffmpeg_path=ffmpeg_path, timeout_seconds=timeout_seconds)
    rendered = probe_video(output)
    return ExtractedClip(
        source_path="", start_seconds=0.0, end_seconds=duration_seconds,
        requested_duration_seconds=round(duration_seconds, 3),
        output_path=str(output), duration_seconds=rendered.duration_seconds,
        width=rendered.width, height=rendered.height, fps=rendered.fps,
        video_codec=rendered.video_codec, has_audio=rendered.has_audio,
        audio_codec=rendered.audio_codec or "aac",
        crop_window={}, subject_aware=False)
