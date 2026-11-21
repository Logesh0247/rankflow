"""Momentrix AI — Quality Assurance Agent, Phase 1: file validation (Prompt 09).

Re-reads the finished MP4 with ffprobe and a full decode check —
the same real checks Production's render validation uses, run
again here on the delivered file so a render that was valid when
written but is corrupt, truncated or mislabelled on disk cannot
reach the user. Nothing is assumed from the manifest: the file
itself is probed.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.production.render import decode_check
from app.agents.video_intelligence.probe import VideoProbe, probe_video
from app.core.errors import StorageError, ValidationError


class TechnicalValidation(BaseModel):
    """Measured properties of the delivered file."""

    valid: bool
    file_exists: bool = False
    file_size_bytes: int = 0
    duration_seconds: float = 0.0
    expected_duration_seconds: float | None = None
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str | None = None
    has_audio: bool = False
    decode_errors: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


def validate_file(path: str | Path, *, expected_duration_seconds: float | None = None,
                  duration_tolerance_seconds: float = 0.6,
                  ffprobe_path: str = "ffprobe",
                  ffmpeg_path: str = "ffmpeg") -> TechnicalValidation:
    """Phase 1: MP4 integrity, resolution, aspect ratio, frame
    rate, duration, codecs and playback (full decode)."""
    path = Path(path)
    issues: list[str] = []
    if not path.is_file():
        return TechnicalValidation(valid=False, issues=[f"rendered file does not exist: {path}"])
    size = path.stat().st_size
    if size == 0:
        issues.append("rendered file is empty (0 bytes)")
    probe: VideoProbe | None = None
    try:
        probe = probe_video(path, ffprobe_path=ffprobe_path)
    except (StorageError, ValidationError, Exception) as exc:  # noqa: BLE001
        issues.append(f"ffprobe could not read the rendered file: {exc}")
    decode_errors = decode_check(path, ffmpeg_path=ffmpeg_path)
    if decode_errors:
        issues.append(f"decode check reported {len(decode_errors)} error(s): "
                      + "; ".join(decode_errors[:3]))
    if probe is None:
        return TechnicalValidation(valid=False, file_exists=True, file_size_bytes=size,
                                   decode_errors=decode_errors, issues=issues)
    if probe.width != 1080 or probe.height != 1920:
        issues.append(f"resolution is {probe.width}x{probe.height}, expected 1080x1920")
    if probe.width and probe.height:
        ratio = probe.width / probe.height
        if abs(ratio - 9 / 16) > 0.01:
            issues.append(f"aspect ratio is {ratio:.3f}, expected 9:16 (0.5625)")
    if abs(probe.fps - 30.0) > 0.1:
        issues.append(f"frame rate is {probe.fps:.2f} fps, expected 30")
    if probe.video_codec != "h264":
        issues.append(f"video codec is {probe.video_codec}, expected h264")
    if not probe.has_audio:
        issues.append("rendered file has no audio stream")
    elif probe.audio_codec != "aac":
        issues.append(f"audio codec is {probe.audio_codec}, expected aac")
    if expected_duration_seconds is not None:
        if abs(probe.duration_seconds - expected_duration_seconds) > duration_tolerance_seconds:
            issues.append(
                f"duration is {probe.duration_seconds:.2f}s, expected "
                f"{expected_duration_seconds:.2f}s (tolerance "
                f"{duration_tolerance_seconds}s)")
    return TechnicalValidation(
        valid=not issues, file_exists=True, file_size_bytes=size,
        duration_seconds=probe.duration_seconds,
        expected_duration_seconds=expected_duration_seconds,
        width=probe.width, height=probe.height, fps=probe.fps,
        video_codec=probe.video_codec, audio_codec=probe.audio_codec,
        has_audio=probe.has_audio, decode_errors=decode_errors, issues=issues)
