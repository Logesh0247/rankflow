"""RANKFLOW AI — Video Production Agent, Phases 8-9: rendering and validation (Prompt 08).

Rendering is two passes, both real FFmpeg:
1. Every timeline segment (intro card, five clips, outro card) is
   already a uniform 1080x1920 @ 30fps H.264/AAC file (see
   extract.py) — uniform segments are what make concatenation
   reliable.
2. The segments are joined with the concat demuxer: the video
   stream is copied (the segments are already uniform 1080x1920
   @ 30fps H.264, so a second video encode only burns CPU) and
   only the audio is re-encoded through the final audio chain
   (loudnorm, fades, limiter). If stream-copy fails, the render
   falls back to a full re-encode instead of failing the job.

Validation (Phase 9) re-reads the finished file with ffprobe
and decodes it fully:
* file exists and is non-empty;
* width 1080, height 1920, fps 30 (±0.1), video codec h264,
  audio codec aac, audio stream present;
* duration matches the EDL total within 0.5s;
* `ffmpeg -v error -i file -f null -` reports no decode errors
  (playback check — a file that will not decode is not a video).

A render that fails any of these is a failure, never a "ready"
video. The validation result is part of the manifest.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.production.audio import final_audio_filter
from app.agents.production.extract import _run_ffmpeg
from app.agents.video_intelligence.probe import VideoProbe, probe_video
from app.core.errors import ExternalServiceError, ValidationError

DURATION_TOLERANCE_SECONDS = 0.5


class RenderValidation(BaseModel):
    valid: bool
    duration_seconds: float
    width: int
    height: int
    fps: float
    video_codec: str
    audio_codec: str | None = None
    has_audio: bool = False
    decode_errors: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


def _escape_concat_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace("'", "'\\''")


def concat_segments(segment_paths: list[str | Path], output_path: str | Path, *,
                    ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe",
                    total_duration_seconds: float,
                    music_path: str | None = None,
                    timeout_seconds: float = 600.0) -> Path:
    """Join uniform segments into the final MP4 with the audio chain.

    When a licensed music file is supplied it is looped under the
    segment audio at low volume (0.15) and mixed with amix
    (duration=first, so the total duration is the segments', not
    the music's). Without music the segment audio is used as-is.

    Video is stream-copied (Prompt 12): every segment is already a
    uniform 1080x1920 @ 30fps H.264 file, so re-encoding the video
    a second time only burns CPU and loses quality. Only the audio
    is re-encoded (loudnorm / fades / limiter). If the copy fails
    (e.g. a segment's codec parameters differ), the render falls
    back to the full re-encode instead of failing the job.
    """
    segments = [Path(p) for p in segment_paths]
    if not segments:
        raise ValidationError("no segments to concatenate")
    for segment in segments:
        if not segment.is_file():
            raise ValidationError(f"segment file missing before concat: {segment}")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    list_path = output.with_suffix(".concat.txt")
    list_path.write_text(
        "".join(f"file '{_escape_concat_path(str(s))}'\n" for s in segments),
        encoding="utf-8")
    audio_chain = final_audio_filter(total_duration_seconds)
    try:
        _concat_stream_copy(list_path, output, music_path, audio_chain,
                            ffmpeg_path=ffmpeg_path, timeout_seconds=timeout_seconds)
    except ExternalServiceError:
        _concat_reencode(list_path, output, music_path, audio_chain,
                         ffmpeg_path=ffmpeg_path, timeout_seconds=timeout_seconds)
    return output


def _concat_stream_copy(list_path: Path, output: Path, music_path: str | None,
                        audio_chain: str, *, ffmpeg_path: str,
                        timeout_seconds: float) -> None:
    """Concat with video stream-copy; audio is re-encoded through the chain."""
    args = ["-f", "concat", "-safe", "0", "-i", str(list_path)]
    if music_path:
        music = Path(music_path)
        if not music.is_file():
            raise ValidationError(f"music file missing before concat: {music}")
        args += ["-stream_loop", "-1", "-i", str(music)]
        args += [
            "-filter_complex",
            f"[1:a]volume=0.15[a1];[0:a][a1]amix=inputs=2:duration=first:"
            f"dropout_transition=0,{audio_chain}[aout]",
            "-map", "0:v:0", "-map", "[aout]",
        ]
    else:
        args += ["-af", audio_chain, "-map", "0:v:0", "-map", "0:a:0"]
    args += [
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(output),
    ]
    _run_ffmpeg(args, ffmpeg_path=ffmpeg_path, timeout_seconds=timeout_seconds)


def _concat_reencode(list_path: Path, output: Path, music_path: str | None,
                     audio_chain: str, *, ffmpeg_path: str,
                     timeout_seconds: float) -> None:
    """Full re-encode fallback when stream-copy cannot join the segments."""
    args = ["-f", "concat", "-safe", "0", "-i", str(list_path)]
    if music_path:
        music = Path(music_path)
        if not music.is_file():
            raise ValidationError(f"music file missing before concat: {music}")
        args += ["-stream_loop", "-1", "-i", str(music)]
        args += [
            "-filter_complex",
            f"[1:a]volume=0.15[a1];[0:a][a1]amix=inputs=2:duration=first:"
            f"dropout_transition=0,{audio_chain}[aout]",
            "-map", "0:v:0", "-map", "[aout]",
        ]
    else:
        args += ["-af", audio_chain]
    args += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(output),
    ]
    _run_ffmpeg(args, ffmpeg_path=ffmpeg_path, timeout_seconds=timeout_seconds)


def decode_check(path: str | Path, *, ffmpeg_path: str = "ffmpeg",
                 timeout_seconds: float = 300.0) -> list[str]:
    """Decode the whole file; return any error lines FFmpeg reports."""
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"decode check failed to run: {exc}"]
    errors = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
    return errors


def validate_render(path: str | Path, *, expected_duration_seconds: float,
                    ffprobe_path: str = "ffprobe", ffmpeg_path: str = "ffmpeg",
                    timeout_seconds: float = 300.0) -> RenderValidation:
    """Phase 9: validate a rendered MP4 against the EDL contract."""
    path = Path(path)
    issues: list[str] = []
    if not path.is_file():
        return RenderValidation(valid=False, duration_seconds=0.0, width=0,
                                height=0, fps=0.0, video_codec="",
                                issues=[f"rendered file does not exist: {path}"])
    if path.stat().st_size == 0:
        issues.append("rendered file is empty (0 bytes)")
    probe: VideoProbe | None = None
    try:
        probe = probe_video(path, ffprobe_path=ffprobe_path)
    except (ValidationError, Exception) as exc:  # noqa: BLE001 - any probe failure is an issue
        issues.append(f"ffprobe could not read the rendered file: {exc}")
    decode_errors = decode_check(path, ffmpeg_path=ffmpeg_path,
                                 timeout_seconds=timeout_seconds)
    if decode_errors:
        issues.append(f"decode check reported {len(decode_errors)} error(s)")
    if probe is None:
        return RenderValidation(valid=False, duration_seconds=0.0, width=0,
                                height=0, fps=0.0, video_codec="",
                                decode_errors=decode_errors, issues=issues)
    if probe.width != 1080 or probe.height != 1920:
        issues.append(f"resolution is {probe.width}x{probe.height}, expected 1080x1920")
    if abs(probe.fps - 30.0) > 0.1:
        issues.append(f"frame rate is {probe.fps:.2f} fps, expected 30")
    if probe.video_codec != "h264":
        issues.append(f"video codec is {probe.video_codec}, expected h264")
    if not probe.has_audio:
        issues.append("rendered file has no audio stream")
    elif probe.audio_codec != "aac":
        issues.append(f"audio codec is {probe.audio_codec}, expected aac")
    if abs(probe.duration_seconds - expected_duration_seconds) > DURATION_TOLERANCE_SECONDS:
        issues.append(
            f"duration is {probe.duration_seconds:.2f}s, expected "
            f"{expected_duration_seconds:.2f}s (tolerance "
            f"{DURATION_TOLERANCE_SECONDS}s)")
    return RenderValidation(
        valid=not issues, duration_seconds=probe.duration_seconds,
        width=probe.width, height=probe.height, fps=probe.fps,
        video_codec=probe.video_codec, audio_codec=probe.audio_codec,
        has_audio=probe.has_audio, decode_errors=decode_errors, issues=issues)
