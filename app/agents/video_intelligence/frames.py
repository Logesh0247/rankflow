"""Momentrix AI — Video Intelligence Agent, Phase 2: frame extraction (Prompt 05).

Reads a video with OpenCV at a configurable sampling rate and
measures verifiable per-frame signals: brightness, sharpness
(Laplacian variance), frame-to-frame motion (mean absolute
difference) and colour-histogram correlation with the previous
sampled frame. Scene boundaries are where the histogram
correlation drops below the configured threshold — a measured
change in the picture, not an inferred event.

Only sampled frames are kept (at most ``max_frames``; the sampling
rate is lowered automatically for long videos so a hours-long
source does not produce thousands of stored frames). Sampling is
seek-based (Prompt 12): the reader jumps to each sampled frame
instead of decoding every frame — the sampled frames, and every
measured signal, are identical to the sequential scan, which is
kept as the fallback when seeking fails. The full
video is never sent to any model — there is no model to send it
to in this environment, and the analyser works on these sampled
frames only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from app.core.errors import StorageError, ValidationError


@dataclass
class FrameSample:
    """One sampled frame and its measured signals (all numbers are measured)."""

    timestamp: float
    brightness: float  # mean grey level, 0-255
    sharpness: float  # Laplacian variance (higher = sharper)
    motion: float  # mean abs diff vs previous sampled frame, 0-255
    hist_correlation: float  # HSV-histogram correlation vs previous frame, -1..1
    is_black: bool
    frame_index: int


@dataclass
class FrameExtraction:
    samples: list[FrameSample] = field(default_factory=list)
    effective_fps: float = 0.0
    sampled_frame_count: int = 0
    scene_boundaries: list[float] = field(default_factory=list)  # timestamps of scene changes


def extract_samples(path: str | Path, *, sample_fps: float = 1.0,
                    max_frames: int = 600, scene_threshold: float = 0.55) -> FrameExtraction:
    """Sample frames from a video and measure their signals.

    Raises StorageError if OpenCV cannot open the file (corrupt or
    unreadable despite a valid container) and ValidationError if no
    frame can be read at all.

    Sampling is seek-based (Prompt 12): the reader jumps straight to
    each sampled frame index instead of decoding every frame of the
    video to reach it. The sampled frames — and therefore every
    measured signal — are identical to the sequential scan; if any
    seek/read fails, the reader falls back to the sequential scan.
    """
    path = Path(path)
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
        targets = list(range(0, total, step))[:max_frames] if total > 0 else []
        if targets:
            try:
                return _extract_seek(cap, targets, fps, effective_fps, scene_threshold)
            except (ValidationError, StorageError):
                # A seek failure must not lose the analysis: fall back
                # to the sequential scan over the same file.
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        return _extract_sequential(cap, step, fps, effective_fps, max_frames, scene_threshold)
    finally:
        cap.release()


def _measure(samples: list[FrameSample], frame: np.ndarray, timestamp: float,
             frame_index: int, prev_gray: np.ndarray | None,
             prev_hist: np.ndarray | None, scene_threshold: float,
             boundaries: list[float]) -> tuple[np.ndarray, np.ndarray]:
    """Measure one frame's signals and append its sample. Returns (gray, hist)."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    if prev_gray is None or prev_hist is None:
        motion, corr = 0.0, 1.0
    else:
        motion = float(np.mean(cv2.absdiff(gray, prev_gray)))
        corr = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL))
        if corr < scene_threshold and timestamp > 0:
            boundaries.append(round(timestamp, 3))
    samples.append(FrameSample(
        timestamp=round(timestamp, 3), brightness=round(brightness, 2),
        sharpness=round(sharpness, 2), motion=round(motion, 2),
        hist_correlation=round(corr, 3),
        is_black=bool(brightness < 8.0), frame_index=frame_index))
    return gray, hist


def _extract_seek(cap: cv2.VideoCapture, targets: list[int], fps: float,
                  effective_fps: float, scene_threshold: float) -> FrameExtraction:
    """Seek-based extraction: jump to each sampled frame index directly."""
    samples: list[FrameSample] = []
    boundaries: list[float] = []
    prev_gray: np.ndarray | None = None
    prev_hist: np.ndarray | None = None
    for frame_index in targets:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = cap.read()
        if not ok or frame is None:
            raise ValidationError(f"seek/read failed at frame {frame_index}")
        timestamp = frame_index / fps if fps else 0.0
        prev_gray, prev_hist = _measure(
            samples, frame, timestamp, frame_index, prev_gray, prev_hist,
            scene_threshold, boundaries)
    if not samples:
        raise ValidationError("no frames could be read from video")
    return FrameExtraction(samples=samples, effective_fps=round(effective_fps, 3),
                           sampled_frame_count=len(samples), scene_boundaries=boundaries)


def _extract_sequential(cap: cv2.VideoCapture, step: int, fps: float,
                        effective_fps: float, max_frames: int,
                        scene_threshold: float) -> FrameExtraction:
    """Original sequential scan (fallback when seeking is unavailable)."""
    samples: list[FrameSample] = []
    boundaries: list[float] = []
    prev_gray: np.ndarray | None = None
    prev_hist: np.ndarray | None = None
    index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % step != 0:
            index += 1
            continue
        timestamp = index / fps if fps else 0.0
        prev_gray, prev_hist = _measure(
            samples, frame, timestamp, index, prev_gray, prev_hist,
            scene_threshold, boundaries)
        index += 1
        if len(samples) >= max_frames:
            break
    if not samples:
        raise ValidationError("no frames could be read from video")
    return FrameExtraction(samples=samples, effective_fps=round(effective_fps, 3),
                           sampled_frame_count=len(samples), scene_boundaries=boundaries)


def save_frame_jpeg(path: str | Path, timestamp: float, out_path: str | Path) -> Path:
    """Extract a single frame at ``timestamp`` and save it as a JPEG thumbnail."""
    out_path = Path(out_path)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise StorageError(f"OpenCV cannot open video file: {path}")
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            raise ValidationError(f"no frame at {timestamp:.2f}s in {path}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(out_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85]):
            raise StorageError(f"failed to write frame thumbnail: {out_path}")
        return out_path
    finally:
        cap.release()


def read_frame_at(path: str | Path, timestamp: float) -> np.ndarray | None:
    """Read one frame for verification; returns None when unreadable (never raises)."""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = cap.read()
        return frame if ok and frame is not None else None
    finally:
        cap.release()
