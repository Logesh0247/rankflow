"""RANKFLOW AI — Video Intelligence Agent, Phases 3-4: analysis (Prompt 05).

Deterministic heuristic analyser over the measured frame signals
from ``frames.py``. It segments a video at its scene boundaries,
scores each segment on motion, sharpness and brightness, and
returns candidate moments — one per segment, at most
``max_moments_per_video``, best first.

HONEST LIMIT, stated everywhere this analyser is used: there is no
vision-language or video-understanding model in this environment
(no key, no endpoint), so the analyser CANNOT name what happens in
a scene ("a person falls", "a dog jumps"). Every description it
produces is a factual, signal-based label — scene change, motion
level, brightness, sharpness — and says the analyser is the
heuristic one. A semantic event label would be a fabrication, so
none is ever produced. Prompt 06 (Ranking) scores these moments
from the same measured signals; a real VLM can replace this
analyser later without changing the moment schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.video_intelligence.frames import FrameExtraction, FrameSample
from app.agents.video_intelligence.probe import VideoProbe

ANALYSER = "heuristic_frame_signals_v1"


@dataclass
class Segment:
    start: float
    end: float
    samples: list[FrameSample] = field(default_factory=list)
    mean_motion: float = 0.0
    mean_sharpness: float = 0.0
    mean_brightness: float = 0.0
    black_fraction: float = 0.0

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


@dataclass
class DetectedMoment:
    """A candidate moment before timestamp verification (Phase 5)."""

    start_seconds: float
    end_seconds: float
    confidence: float
    visual_quality: float
    context_clarity: float
    description: str
    event_type: str  # "scene_segment" or "high_motion_segment"
    mean_motion: float
    mean_sharpness: float
    mean_brightness: float
    frame_refs: list[str] = field(default_factory=list)


def segment_samples(extraction: FrameExtraction, duration: float) -> list[Segment]:
    """Split sampled frames into segments at the measured scene boundaries."""
    cuts = [0.0] + [b for b in extraction.scene_boundaries if 0 < b < duration] + [duration]
    cuts = sorted(set(round(c, 3) for c in cuts))
    segments: list[Segment] = []
    for start, end in zip(cuts, cuts[1:]):
        if end - start < 0.3:
            continue
        samples = [s for s in extraction.samples if start <= s.timestamp < end]
        if not samples:
            continue
        segments.append(_summarise(Segment(start=start, end=end, samples=samples)))
    return segments


def _summarise(segment: Segment) -> Segment:
    samples = segment.samples
    # Motion is a between-frame measure: a segment's first sample is
    # compared against the previous segment's last frame, so its
    # motion is the scene change itself, not motion inside this
    # segment. Exclude it, or a static segment that follows a cut
    # would be scored as high-motion.
    inner_motions = [s.motion for s in samples[1:]] if len(samples) > 1 else [0.0]
    segment.mean_motion = round(sum(inner_motions) / len(inner_motions), 2)
    segment.mean_sharpness = round(sum(s.sharpness for s in samples) / len(samples), 2)
    segment.mean_brightness = round(sum(s.brightness for s in samples) / len(samples), 2)
    segment.black_fraction = round(
        sum(1 for s in samples if s.is_black) / len(samples), 3)
    return segment


def _norm(value: float, ceiling: float) -> float:
    return max(0.0, min(1.0, value / ceiling)) if ceiling > 0 else 0.0


def detect_moments(probe: VideoProbe, extraction: FrameExtraction, *,
                   min_moment_seconds: float = 2.0,
                   max_moment_seconds: float = 12.0,
                   max_moments_per_video: int = 5,
                   motion_threshold: float = 8.0) -> list[DetectedMoment]:
    """Detect candidate moments from segments.

    Unusable segments are skipped, never returned: segments that
    are (almost) entirely black, and segments shorter than the
    minimum moment length. Long segments are trimmed to the
    highest-motion window of ``max_moment_seconds`` — the window
    boundaries are measured sample timestamps, never invented.
    """
    segments = segment_samples(extraction, probe.duration_seconds)
    moments: list[DetectedMoment] = []
    for segment in segments:
        if segment.black_fraction >= 0.9:
            continue  # unusable: (almost) entirely black frames
        if segment.duration < min_moment_seconds:
            continue
        start, end = segment.start, segment.end
        event_type = "scene_segment"
        if segment.mean_motion >= motion_threshold:
            event_type = "high_motion_segment"
        if end - start > max_moment_seconds:
            start, end = _best_window(segment, max_moment_seconds)
        moments.append(_score_moment(segment, start, end, event_type,
                                     min_moment_seconds, max_moment_seconds,
                                     motion_threshold))
    moments.sort(key=lambda m: (m.confidence, m.mean_motion), reverse=True)
    return moments[:max_moments_per_video]


def _best_window(segment: Segment, window_seconds: float) -> tuple[float, float]:
    """Highest-motion contiguous window of ``window_seconds`` inside a segment."""
    samples = segment.samples
    if not samples:
        return segment.start, segment.end
    best_start, best_motion = segment.start, -1.0
    for sample in samples:
        window_end = sample.timestamp + window_seconds
        if window_end > segment.end + 0.001:
            break
        window = [s for s in samples if sample.timestamp <= s.timestamp < window_end]
        if not window:
            continue
        motion = sum(s.motion for s in window) / len(window)
        if motion > best_motion:
            best_motion, best_start = motion, sample.timestamp
    return round(best_start, 3), round(min(best_start + window_seconds, segment.end), 3)


def _score_moment(segment: Segment, start: float, end: float, event_type: str,
                  min_moment_seconds: float, max_moment_seconds: float,
                  motion_threshold: float) -> DetectedMoment:
    duration = end - start
    motion_score = _norm(segment.mean_motion, 40.0)
    sharpness_score = _norm(segment.mean_sharpness, 400.0)
    # Brightness is best in the middle: very dark or blown-out frames score lower.
    brightness_score = 1.0 - abs(segment.mean_brightness - 128.0) / 128.0
    brightness_score = max(0.0, min(1.0, brightness_score))
    visual_quality = round(0.6 * sharpness_score + 0.4 * brightness_score, 3)
    # Context clarity: a moment long enough to be understood, not so
    # long it loses the event; full marks inside [min, max].
    if duration < min_moment_seconds:
        context_clarity = round(duration / min_moment_seconds, 3)
    elif duration <= max_moment_seconds:
        context_clarity = 1.0
    else:
        context_clarity = round(max(0.0, 1.0 - (duration - max_moment_seconds) / max_moment_seconds), 3)
    confidence = round(0.4 * motion_score + 0.3 * visual_quality + 0.3 * context_clarity, 3)
    motion_word = "high-motion" if segment.mean_motion >= motion_threshold else (
        "moderate-motion" if segment.mean_motion >= motion_threshold / 4 else "low-motion")
    description = (
        f"Scene segment {start:.1f}–{end:.1f}s: {motion_word} segment "
        f"(mean frame difference {segment.mean_motion:.1f}, mean brightness "
        f"{segment.mean_brightness:.0f}, mean sharpness {segment.mean_sharpness:.0f}). "
        f"Analyser: {ANALYSER} — frame signals only; no vision-language "
        f"model ran, so no semantic event (what happens in the scene) is claimed.")
    return DetectedMoment(
        start_seconds=round(start, 3), end_seconds=round(end, 3),
        confidence=confidence, visual_quality=visual_quality,
        context_clarity=context_clarity, description=description,
        event_type=event_type, mean_motion=segment.mean_motion,
        mean_sharpness=segment.mean_sharpness, mean_brightness=segment.mean_brightness)
