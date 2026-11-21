"""Momentrix AI — Quality Assurance Agent, Phase 4: audio inspection (Prompt 09).

One FFmpeg pass over the audio track measures, with
``volumedetect``, ``silencedetect`` and ``loudnorm``:

* whether an audio stream exists at all (from ffprobe, in technical.py);
* mean and maximum volume — an effectively silent track (mean
  below the configured silence floor) is flagged, not passed;
* the proportion of the track that is silence;
* true peak and loudness range — clipping and sudden volume
  changes.

What it cannot do: tell a narration voice from source-clip
audio by listening — there is no speech-recognition model
configured. Whether narration audio exists at all is therefore
checked against the production manifest (narration status
``not_generated_no_tts_provider`` means no voice was ever
rendered), and the measured silence is the corroborating
evidence. Music balance / ducking can only be checked when a
music track was mixed in; with no licensed music configured,
the check records that music is absent rather than claiming a
balance was measured.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

VOLUME_RE = re.compile(r"mean_volume:\s*(-?[0-9.]+)\s*dB")
MAX_VOLUME_RE = re.compile(r"max_volume:\s*(-?[0-9.]+)\s*dB")
SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+).*?silence_duration:\s*([0-9.]+)")
LOUDNORM_RE = re.compile(r"(\{[^{}]*\"input_i\"[^{}]*\})", re.DOTALL)


class AudioValidation(BaseModel):
    """Measured results of the audio inspection."""

    has_audio: bool = False
    mean_volume_db: float | None = None
    max_volume_db: float | None = None
    true_peak_db: float | None = None
    loudness_range_lu: float | None = None
    silent_seconds: float = 0.0
    silent_fraction: float = 0.0
    effectively_silent: bool = False
    clipping: bool = False
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def analyse_audio(path: str | Path, *, duration_seconds: float,
                  silence_db: float = -70.0, silence_noise_db: float = -50.0,
                  ffmpeg_path: str = "ffmpeg",
                  timeout_seconds: float = 300.0) -> AudioValidation:
    """Phase 4: one FFmpeg pass — volumedetect + silencedetect +
    loudnorm stats. Returns measured values; a file whose audio
    cannot be decoded is reported as an issue, never as silent."""
    path = Path(path)
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-nostats", "-i", str(path),
             "-af", f"volumedetect,silencedetect=noise={silence_noise_db}dB:d=0.5,"
                    "loudnorm=print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AudioValidation(issues=[f"audio analysis failed to run: {exc}"])
    stderr = proc.stderr or ""
    mean_match = VOLUME_RE.search(stderr)
    max_match = MAX_VOLUME_RE.search(stderr)
    if mean_match is None and max_match is None:
        return AudioValidation(
            issues=["audio analysis produced no volume measurement — "
                    "the audio track may be undecodable"])
    mean_db = float(mean_match.group(1)) if mean_match else None
    max_db = float(max_match.group(1)) if max_match else None
    silent_seconds = 0.0
    for match in SILENCE_END_RE.finditer(stderr):
        silent_seconds += float(match.group(2))
    # A trailing silence with no silence_end line is still silence.
    starts = SILENCE_START_RE.findall(stderr)
    ends = SILENCE_END_RE.findall(stderr)
    if len(starts) > len(ends) and duration_seconds > 0:
        silent_seconds += max(0.0, duration_seconds - float(starts[-1]))
    true_peak: float | None = None
    loudness_range: float | None = None
    loud_match = LOUDNORM_RE.search(stderr)
    if loud_match:
        try:
            stats = json.loads(loud_match.group(1))
            true_peak = float(stats.get("input_tp")) if stats.get("input_tp") is not None else None
            loudness_range = float(stats.get("input_lra")) if stats.get("input_lra") is not None else None
        except (ValueError, TypeError):
            pass
    issues: list[str] = []
    warnings: list[str] = []
    effectively_silent = mean_db is not None and mean_db <= silence_db
    if effectively_silent:
        issues.append(
            f"audio track is effectively silent — mean volume {mean_db:.1f} dB "
            f"(floor {silence_db:.0f} dB), max volume "
            f"{max_db:.1f} dB" if max_db is not None else
            f"audio track is effectively silent — mean volume {mean_db:.1f} dB "
            f"(floor {silence_db:.0f} dB)")
    silent_fraction = (silent_seconds / duration_seconds) if duration_seconds > 0 else 0.0
    if silent_fraction > 0.5 and not effectively_silent:
        warnings.append(
            f"{silent_fraction:.0%} of the audio track is silence "
            f"({silent_seconds:.1f}s of {duration_seconds:.1f}s)")
    clipping = bool(
        (max_db is not None and max_db >= -0.1)
        or (true_peak is not None and true_peak >= -0.1))
    if clipping:
        issues.append(
            "audio appears to clip — "
            + (f"max volume {max_db:.1f} dBFS" if max_db is not None else "")
            + (f", true peak {true_peak:.1f} dBTP" if true_peak is not None else ""))
    if loudness_range is not None and loudness_range > 20.0:
        warnings.append(
            f"loudness range is {loudness_range:.1f} LU — sudden volume "
            "changes are likely; check music/narration balance")
    return AudioValidation(
        has_audio=True, mean_volume_db=mean_db, max_volume_db=max_db,
        true_peak_db=true_peak, loudness_range_lu=loudness_range,
        silent_seconds=round(silent_seconds, 2),
        silent_fraction=round(silent_fraction, 3),
        effectively_silent=effectively_silent, clipping=clipping,
        issues=issues, warnings=warnings)
