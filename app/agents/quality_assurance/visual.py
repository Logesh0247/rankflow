"""Momentrix AI — Quality Assurance Agent, Phase 3: visual inspection (Prompt 09).

Samples frames from the rendered video with OpenCV and measures
them — brightness, frame-to-frame difference, pixel variance.
What it can actually detect:

* black frames — sampled frames whose mean grey level is below
  the configured threshold, outside the known black transition
  gaps Production deliberately renders between segments, and
  outside the 0.5 s fade-in at the start of the render and the
  0.5 s fade-out at the end (Production fades the intro in and
  the outro out — see production/service.py — so the first and
  last sampled frames are legitimately near-black);
* frozen frames — a moment clip whose consecutive sampled frames
  barely change (intro/outro cards are static by design and are
  excluded, as are the transition gaps);
* blank segments — a content segment whose sampled frames are
  near-uniform (no footage and no graphics visible);
* letterbox/pillarbox bands — top and bottom edge bands that are
  near-black while the middle of the frame is not, inside moment
  clips (a cropping problem).

What it cannot detect, and records as NOT TESTED rather than
passing on a guess: whether the countdown graphics / captions
actually read correctly, whether caption text overlaps other
text, whether text sits outside the safe margins, and whether a
frame shows unsuitable material — there is no OCR engine and no
vision-language model configured in this environment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from pydantic import BaseModel, Field

from app.core.errors import StorageError, ValidationError


@dataclass
class SegmentWindow:
    """A timeline window of the render, from the production manifest."""

    kind: str  # "intro" | "countdown_moment" | "outro" | "transition"
    rank: int | None = None
    start: float = 0.0
    duration: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.duration


class VisualValidation(BaseModel):
    """Measured results of the visual inspection."""

    sampled_frames: int = 0
    sample_fps: float = 0.0
    black_samples: list[float] = Field(default_factory=list)
    frozen_segments: list[str] = Field(default_factory=list)
    blank_segments: list[str] = Field(default_factory=list)
    letterboxed_segments: list[str] = Field(default_factory=list)
    mean_brightness: float = 0.0
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    not_tested: list[str] = Field(default_factory=list)


def _in_transition(timestamp: float, windows: list[SegmentWindow]) -> bool:
    return any(w.kind == "transition" and w.start - 0.05 <= timestamp <= w.end + 0.05
               for w in windows)


def _window_at(timestamp: float, windows: list[SegmentWindow]) -> SegmentWindow | None:
    for window in windows:
        if window.start - 0.05 <= timestamp < window.end + 0.05:
            return window
    return None


def inspect_visual(path: str | Path, windows: list[SegmentWindow] | None = None, *,
                   sample_fps: float = 1.0, max_frames: int = 120,
                   black_threshold: float = 8.0,
                   edge_fade_seconds: float = 0.5,
                   frozen_diff_threshold: float = 0.5) -> VisualValidation:
    """Phase 3: sample frames and measure black / frozen / blank /
    letterboxed segments. ``windows`` are the production manifest
    segments; without them, transition gaps cannot be excluded and
    every black sample is reported. Samples inside the first and
    last ``edge_fade_seconds`` of the render are not counted as
    black frames: Production fades the intro in and the outro out
    over 0.5 s, so the edge frames are legitimately near-black."""
    path = Path(path)
    windows = list(windows or [])
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise StorageError(f"OpenCV cannot open video file (corrupt or unsupported): {path}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = total / fps if fps and total else 0.0
        effective_fps = sample_fps
        if duration > 0 and duration * sample_fps > max_frames:
            effective_fps = max_frames / duration
        step = max(1, int(round(fps / effective_fps))) if effective_fps > 0 else 1
        # Per-segment sampled frames: (timestamp, grey frame, brightness, stddev).
        per_segment: dict[str, list[tuple[float, np.ndarray, float, float]]] = {}
        brightnesses: list[float] = []
        black_samples: list[float] = []
        index = 0
        sampled = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if index % step != 0:
                index += 1
                continue
            timestamp = index / fps if fps else 0.0
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            brightness = float(np.mean(gray))
            stddev = float(np.std(gray))
            brightnesses.append(brightness)
            window = _window_at(timestamp, windows)
            key = f"{window.kind}#{window.rank}" if window and window.rank else (
                window.kind if window else "unknown")
            per_segment.setdefault(key, []).append((timestamp, gray, brightness, stddev))
            if (brightness < black_threshold
                    and not _in_transition(timestamp, windows)
                    and not (timestamp < edge_fade_seconds
                             or timestamp > duration - edge_fade_seconds)):
                black_samples.append(round(timestamp, 2))
            sampled += 1
            index += 1
            if sampled >= max_frames:
                break
        if sampled == 0:
            raise ValidationError(f"no frames could be read from video: {path}")
    finally:
        cap.release()

    issues: list[str] = []
    warnings: list[str] = []
    frozen: list[str] = []
    blank: list[str] = []
    letterboxed: list[str] = []
    for key, samples in per_segment.items():
        kind = key.split("#")[0]
        if kind in ("transition", "unknown"):
            continue
        # Blank segment: every sampled frame near-uniform.
        if samples and all(std < 2.0 for _, _, _, std in samples):
            blank.append(key)
            issues.append(
                f"segment {key} appears blank — every sampled frame is "
                "near-uniform (no footage or graphics visible)")
        if kind == "countdown_moment":
            # Frozen: consecutive sampled frames barely change.
            diffs = []
            for (_, prev, _, _), (_, cur, _, _) in zip(samples, samples[1:]):
                diffs.append(float(np.mean(cv2.absdiff(prev, cur))))
            if len(diffs) >= 3 and sum(d < frozen_diff_threshold for d in diffs) / len(diffs) >= 0.8:
                frozen.append(key)
                issues.append(
                    f"segment {key} appears frozen — {len(diffs)} sampled frame "
                    "pairs barely change (mean abs diff below "
                    f"{frozen_diff_threshold})")
            # Letterbox: top/bottom 5% bands near-black while the middle is not.
            for timestamp, gray, brightness, _ in samples:
                if brightness < 30:
                    continue
                h = gray.shape[0]
                band = max(1, h // 20)
                top = float(np.mean(gray[:band, :]))
                bottom = float(np.mean(gray[-band:, :]))
                middle = float(np.mean(gray[h // 3: 2 * h // 3, :]))
                if top < 5.0 and bottom < 5.0 and middle > 30.0:
                    letterboxed.append(key)
                    warnings.append(
                        f"segment {key} looks letterboxed/pillarboxed at "
                        f"{timestamp:.1f}s — top and bottom bands are black "
                        "while the middle of the frame is bright (cropping problem)")
                    break
    if len(black_samples) >= 2:
        issues.append(
            f"{len(black_samples)} black frame(s) outside the transition gaps "
            f"at {black_samples[:6]}{'...' if len(black_samples) > 6 else ''}")
    elif len(black_samples) == 1:
        warnings.append(
            f"1 black frame outside the transition gaps at {black_samples[0]}s")
    return VisualValidation(
        sampled_frames=sampled, sample_fps=round(effective_fps, 3),
        black_samples=black_samples, frozen_segments=frozen,
        blank_segments=blank, letterboxed_segments=letterboxed,
        mean_brightness=round(sum(brightnesses) / len(brightnesses), 2) if brightnesses else 0.0,
        issues=issues, warnings=warnings,
        not_tested=[
            "caption text overlap and text outside safe margins — no OCR engine is configured, so caption rendering is not verified by reading the frames",
            "countdown graphic text correctness — no OCR engine is configured; only that segments are not blank is measured",
            "unsuitable visual material — no vision-language model is configured, so content suitability is not verified automatically and needs human review",
        ])
