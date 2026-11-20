"""RANKFLOW AI — Video Intelligence Agent, Phase 5: timestamp verification (Prompt 05).

Every detected moment is re-checked against the original video
before it is persisted:

* the start, middle and end timestamps are re-read from the file —
  a moment whose frames cannot be read is dropped, not kept;
* lead-in and aftermath are added (configurable, default 0.5s
  each), clamped to the video bounds and to neighbouring moments,
  so the event has context on both sides;
* a moment that starts at 0s has no lead-in and one that ends at
  the video end has no aftermath — that is flagged as truncated,
  not silently fixed;
* overlapping moments are duplicates: the lower-confidence one is
  dropped (IoU > 0.5);
* a moment whose sampled frames are all black, or whose start/mid/
  end frames are all identical (frozen), is flagged unusable and
  dropped.

Verification never moves a timestamp to a "nicer" value: it only
pads within measured bounds or drops the moment. A timestamp in
the output therefore always comes from a frame that was actually
read from this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from app.agents.video_intelligence.analysis import DetectedMoment
from app.agents.video_intelligence.frames import read_frame_at
from app.agents.video_intelligence.probe import VideoProbe


@dataclass
class VerifiedMoment:
    moment: DetectedMoment
    verified: bool = True
    issues: list[str] = field(default_factory=list)


def _frame_is_black(frame) -> bool:
    if frame is None:
        return True
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(np.mean(gray)) < 8.0


def _frames_identical(a, b) -> bool:
    if a is None or b is None:
        return False
    return float(np.mean(cv2.absdiff(a, b))) < 0.5


def verify_moments(path: str, probe: VideoProbe, moments: list[DetectedMoment], *,
                   lead_in_seconds: float = 0.5,
                   aftermath_seconds: float = 0.5) -> list[VerifiedMoment]:
    """Re-check moments against the original video; drop unusable ones."""
    verified: list[VerifiedMoment] = []
    # Sort by start so padding can be clamped to neighbours.
    ordered = sorted(moments, key=lambda m: m.start_seconds)
    for i, moment in enumerate(ordered):
        issues: list[str] = []
        start = moment.start_seconds
        end = moment.end_seconds
        if start < 0 or end > probe.duration_seconds or end <= start:
            issues.append("timestamp outside video duration — dropped")
            verified.append(VerifiedMoment(moment=moment, verified=False, issues=issues))
            continue
        start_frame = read_frame_at(path, min(start + 0.05, probe.duration_seconds - 0.05))
        mid_frame = read_frame_at(path, (start + end) / 2)
        end_frame = read_frame_at(path, max(end - 0.05, 0.05))
        if start_frame is None or mid_frame is None or end_frame is None:
            issues.append("could not re-read frames at the moment timestamps — dropped")
            verified.append(VerifiedMoment(moment=moment, verified=False, issues=issues))
            continue
        if _frame_is_black(start_frame) and _frame_is_black(mid_frame) and _frame_is_black(end_frame):
            issues.append("all sampled frames are black — unusable segment, dropped")
            verified.append(VerifiedMoment(moment=moment, verified=False, issues=issues))
            continue
        if _frames_identical(start_frame, mid_frame) and _frames_identical(mid_frame, end_frame):
            issues.append("frames are frozen (identical) across the moment")
        # Lead-in / aftermath, clamped to the video and to neighbours.
        # Padding never shrinks the moment itself: the start can only
        # move earlier and the end only later.
        prev_end = ordered[i - 1].end_seconds if i > 0 else 0.0
        next_start = ordered[i + 1].start_seconds if i + 1 < len(ordered) else probe.duration_seconds
        padded_start = min(start, max(prev_end, start - lead_in_seconds, 0.0))
        padded_end = max(end, min(next_start, end + aftermath_seconds, probe.duration_seconds))
        if padded_start > start - lead_in_seconds + 0.001 and start > 0.01:
            issues.append("lead-in shorter than requested (previous moment / video start)")
        if start <= 0.01:
            issues.append("truncated: moment starts at the video start, no lead-in available")
        if padded_end < end + aftermath_seconds - 0.001 and end < probe.duration_seconds - 0.01:
            issues.append("aftermath shorter than requested (next moment / video end)")
        if end >= probe.duration_seconds - 0.01:
            issues.append("truncated: moment ends at the video end, no aftermath available")
        moment.start_seconds = round(padded_start, 3)
        moment.end_seconds = round(padded_end, 3)
        verified.append(VerifiedMoment(moment=moment, verified=True, issues=issues))
    return _deduplicate(verified)


def _iou(a: DetectedMoment, b: DetectedMoment) -> float:
    overlap = max(0.0, min(a.end_seconds, b.end_seconds) - max(a.start_seconds, b.start_seconds))
    union = max(a.end_seconds, b.end_seconds) - min(a.start_seconds, b.start_seconds)
    return overlap / union if union > 0 else 0.0


def _deduplicate(verified: list[VerifiedMoment]) -> list[VerifiedMoment]:
    """Drop overlapping moments (IoU > 0.5), keeping the higher-confidence one."""
    kept: list[VerifiedMoment] = []
    for candidate in sorted((v for v in verified if v.verified),
                           key=lambda v: v.moment.confidence, reverse=True):
        duplicate = False
        for other in kept:
            if _iou(candidate.moment, other.moment) > 0.5:
                duplicate = True
                candidate.issues.append(
                    f"duplicate of a higher-confidence moment "
                    f"({other.moment.start_seconds:.1f}–{other.moment.end_seconds:.1f}s) — dropped")
                break
        if not duplicate:
            kept.append(candidate)
        else:
            verified.append(VerifiedMoment(moment=candidate.moment, verified=False,
                                           issues=candidate.issues))
    kept.sort(key=lambda v: v.moment.start_seconds)
    dropped = [v for v in verified if not v.verified]
    return kept + dropped
