"""Momentrix AI — Ranking Agent, Phases 1-3: scoring and duplicate detection (Prompt 06).

Scores are EDITORIAL HEURISTICS derived only from the measured
frame signals Video Intelligence persisted on each AnalyzedMoment
(motion, sharpness, brightness, visual quality, context clarity,
event type, probe resolution / fps / audio, verification issues).
They are NOT proven viral-performance probabilities, and no
semantic event is claimed: the analyser never said what happens
in a scene, so the ranker cannot score "how funny the fall was" —
it scores how much measurable motion, change, sharpness and
clarity the moment has. A real vision-language model can replace
the signal mapping later without changing the moment schema or
the 0-100 formula.

Formula (Prompt 06, Phase 1):
    S = 0.25E + 0.25U + 0.20V + 0.15C + 0.15Q   (each component 0-100)

Component definitions — each from a different measured signal,
so the five numbers are not five copies of the same value:
* E Entertainment (immediate viewer interest): motion + event
  type. A high-motion segment scores above a static scene.
* U Unexpectedness: a sudden change (high-motion / scene cut)
  in a short, sharp burst scores above a long, unchanging shot.
* V Visual impact: the analyser's visual_quality (sharpness +
  brightness) plus motion — how readable the picture is.
* C Context clarity: the analyser's context_clarity, capped when
  timestamp verification flagged the moment truncated (starts /
  ends at the video edge, so the event may be cut off) or frozen.
* Q Technical quality: visual_quality + measured resolution
  (1080p = full marks) + audio presence (a Short with no audio
  track scores lower, it is not unusable).

Duplicate detection (Phase 3) — three rules, in order:
1. Same moment twice (same id) — never ranked twice.
2. Overlapping moments from the SAME source video (IoU > 0.5,
   the same rule Video Intelligence uses) — keep the higher score.
3. Same event from different uploads — heuristic fingerprint:
   the two candidates have the same normalised title AND creator,
   and the moments agree within tolerance on duration, mean
   motion, mean sharpness and mean brightness. This is a proxy:
   without a vision model two re-uploads of the same clip can
   only be recognised by their metadata + measured signals, so
   the fingerprint is deliberately strict (all four signals must
   agree) and every fingerprint drop is recorded in the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCORER = "heuristic_editorial_v1"

# Weights from the brief — S = 0.25E + 0.25U + 0.20V + 0.15C + 0.15Q.
WEIGHTS = {"entertainment": 0.25, "unexpectedness": 0.25, "visual_impact": 0.20,
           "context_clarity": 0.15, "technical_quality": 0.15}

MOTION_CEILING = 40.0      # same ceiling Video Intelligence normalises motion against
FULL_HD_PIXELS = 1920 * 1080
DUPLICATE_IOU = 0.5
FINGERPRINT_TOLERANCE = 0.10  # relative agreement required on every signal


@dataclass
class MomentSignals:
    """The measured signals of one AnalyzedMoment, read from its row + labels.

    No field here is invented: every value comes from the moment's
    persisted labels (written by Video Intelligence) or its probe.
    """

    moment_id: str
    candidate_video_id: str
    start_seconds: float
    end_seconds: float
    confidence: float = 0.0
    visual_quality: float = 0.0
    context_clarity: float = 0.0
    event_type: str = "scene_segment"
    mean_motion: float = 0.0
    mean_sharpness: float = 0.0
    mean_brightness: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    verification_issues: list[str] = field(default_factory=list)
    candidate_title: str | None = None
    candidate_creator: str | None = None

    @property
    def duration(self) -> float:
        return round(self.end_seconds - self.start_seconds, 3)


@dataclass
class ScoredMoment:
    """A moment with its five component scores, total and explanation."""

    signals: MomentSignals
    entertainment: float
    unexpectedness: float
    visual_impact: float
    context_clarity: float
    technical_quality: float
    total: float  # 0-100
    explanation: str

    @property
    def score(self) -> float:
        """Total on the 0-1 scale the shared RankingResult contract uses."""
        return round(self.total / 100.0, 4)

    @property
    def moment_id(self) -> str:
        return self.signals.moment_id

    @property
    def candidate_video_id(self) -> str:
        return self.signals.candidate_video_id

    def components(self) -> dict[str, float]:
        return {
            "entertainment": self.entertainment,
            "unexpectedness": self.unexpectedness,
            "visual_impact": self.visual_impact,
            "context_clarity": self.context_clarity,
            "technical_quality": self.technical_quality,
            "total": self.total,
        }


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _norm(value: float, ceiling: float) -> float:
    return _clamp01(value / ceiling) if ceiling > 0 else 0.0


def _event_factor(event_type: str) -> float:
    """How "eventful" the analyser found the segment: a measured
    high-motion segment is a sudden change; a plain scene segment
    is a cut with no measured motion spike."""
    return 1.0 if event_type == "high_motion_segment" else 0.45


def score_moment(signals: MomentSignals) -> ScoredMoment:
    """Score one moment from its measured signals only."""
    motion_norm = _norm(signals.mean_motion, MOTION_CEILING)
    event_factor = _event_factor(signals.event_type)
    # Burst: a short, sharp moment is more unexpected than a long,
    # unchanging one. 2 s scores ~0.83, 12 s scores 0.
    burst = 1.0 - min(1.0, signals.duration / 12.0)

    entertainment = 100.0 * (0.7 * motion_norm + 0.3 * event_factor)
    unexpectedness = 100.0 * (0.6 * event_factor + 0.4 * burst)
    visual_impact = 100.0 * (0.7 * signals.visual_quality + 0.3 * motion_norm)

    context = 100.0 * signals.context_clarity
    issues = " ".join(signals.verification_issues).lower()
    context_cap = 100.0
    if "frozen" in issues:
        context_cap = 50.0  # identical frames across the moment: nothing to follow
    elif "truncated" in issues:
        context_cap = 70.0  # starts/ends at the video edge: the event may be cut off
    context = min(context, context_cap)

    resolution_factor = _norm(signals.width * signals.height, FULL_HD_PIXELS)
    audio_factor = 1.0 if signals.has_audio else 0.5
    technical = 100.0 * (0.5 * signals.visual_quality
                         + 0.3 * resolution_factor + 0.2 * audio_factor)

    total = round(
        WEIGHTS["entertainment"] * entertainment
        + WEIGHTS["unexpectedness"] * unexpectedness
        + WEIGHTS["visual_impact"] * visual_impact
        + WEIGHTS["context_clarity"] * context
        + WEIGHTS["technical_quality"] * technical, 1)
    entertainment, unexpectedness, visual_impact = (
        round(entertainment, 1), round(unexpectedness, 1), round(visual_impact, 1))
    context, technical = round(context, 1), round(technical, 1)
    return ScoredMoment(
        signals=signals, entertainment=entertainment, unexpectedness=unexpectedness,
        visual_impact=visual_impact, context_clarity=context,
        technical_quality=technical, total=total,
        explanation=_explain(signals, entertainment, unexpectedness,
                             visual_impact, context, technical, total, context_cap))


def _explain(signals: MomentSignals, e: float, u: float, v: float,
             c: float, q: float, total: float, context_cap: float) -> str:
    """Why this moment scored what it did — from its numbers only.
    No semantic event is claimed; the text names the measured
    signals and the component that carried / dragged the score."""
    components = [("entertainment", e), ("unexpectedness", u), ("visual impact", v),
                  ("context clarity", c), ("technical quality", q)]
    best_name, best_value = max(components, key=lambda kv: kv[1])
    worst_name, worst_value = min(components, key=lambda kv: kv[1])
    event_word = "high-motion segment" if signals.event_type == "high_motion_segment" else "scene segment"
    parts = [
        f"Total {total:.1f}/100 (heuristic editorial score from measured "
        f"frame signals — not a viral-performance prediction).",
        f"Strongest: {best_name} {best_value:.1f} — {event_word} "
        f"{signals.start_seconds:.1f}–{signals.end_seconds:.1f}s, mean motion "
        f"{signals.mean_motion:.1f}, sharpness {signals.mean_sharpness:.0f}, "
        f"brightness {signals.mean_brightness:.0f}.",
        f"Weakest: {worst_name} {worst_value:.1f}.",
    ]
    if signals.width and signals.height:
        parts.append(f"Technical: {signals.width}x{signals.height} at "
                     f"{signals.fps:.0f} fps, audio={'yes' if signals.has_audio else 'no'}.")
    if context_cap < 100.0:
        parts.append(f"Context clarity capped at {context_cap:.0f}: "
                     + "; ".join(signals.verification_issues))
    return " ".join(parts)


# ---------------------------------------------------------------- duplicates
def _iou(a: ScoredMoment, b: ScoredMoment) -> float:
    overlap = max(0.0, min(a.signals.end_seconds, b.signals.end_seconds)
                  - max(a.signals.start_seconds, b.signals.start_seconds))
    union = max(a.signals.end_seconds, b.signals.end_seconds) - min(
        a.signals.start_seconds, b.signals.start_seconds)
    return overlap / union if union > 0 else 0.0


def _normalise(text: str | None) -> str:
    return " ".join((text or "").strip().lower().split())


def _rel_close(a: float, b: float, tolerance: float) -> bool:
    """Relative agreement, with an absolute floor so near-zero
    signals (e.g. a static shot's motion) do not need to match
    to the decimal."""
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) / scale <= tolerance


def is_fingerprint_duplicate(a: ScoredMoment, b: ScoredMoment,
                             tolerance: float = FINGERPRINT_TOLERANCE) -> bool:
    """Same event from different uploads (heuristic proxy).

    Only when the two candidates carry the same title AND creator
    AND the moments agree within tolerance on duration, mean
    motion, mean sharpness and mean brightness. All four signals
    must agree — a same-titled video with different footage is
    not a duplicate. A vision model could compare frames directly;
    this proxy is documented as the limit."""
    if a.candidate_video_id == b.candidate_video_id:
        return False
    title_a, title_b = _normalise(a.signals.candidate_title), _normalise(b.signals.candidate_title)
    creator_a, creator_b = _normalise(a.signals.candidate_creator), _normalise(b.signals.candidate_creator)
    if not title_a or title_a != title_b or not creator_a or creator_a != creator_b:
        return False
    return (abs(a.signals.duration - b.signals.duration) <= 1.0
            and _rel_close(a.signals.mean_motion, b.signals.mean_motion, tolerance)
            and _rel_close(a.signals.mean_sharpness, b.signals.mean_sharpness, tolerance)
            and _rel_close(a.signals.mean_brightness, b.signals.mean_brightness, tolerance))


def deduplicate(scored: list[ScoredMoment], *,
                iou_threshold: float = DUPLICATE_IOU,
                fingerprint_tolerance: float = FINGERPRINT_TOLERANCE
                ) -> tuple[list[ScoredMoment], list[dict]]:
    """Drop duplicate moments, keeping the higher-scoring one.

    Returns (kept, dropped) where each dropped entry records the
    moment id, the reason and the moment it lost to — so the
    report can show what was removed and why, instead of silently
    discarding it."""
    ordered = sorted(scored, key=_rank_key)
    kept: list[ScoredMoment] = []
    dropped: list[dict] = []
    seen_ids: set[str] = set()
    for candidate in ordered:
        if candidate.moment_id in seen_ids:
            dropped.append({"moment_id": candidate.moment_id, "reason": "same moment supplied twice"})
            continue
        seen_ids.add(candidate.moment_id)
        loser_to = None
        reason = None
        for other in kept:
            if (other.candidate_video_id == candidate.candidate_video_id
                    and _iou(candidate, other) > iou_threshold):
                loser_to, reason = other, (
                    f"overlaps {other.moment_id} from the same source video "
                    f"(IoU {_iou(candidate, other):.2f} > {iou_threshold})")
                break
            if is_fingerprint_duplicate(candidate, other, fingerprint_tolerance):
                loser_to, reason = other, (
                    f"same event as {other.moment_id} from a different upload "
                    f"(same title/creator and matching measured signals)")
                break
        if loser_to is not None:
            dropped.append({"moment_id": candidate.moment_id, "reason": reason,
                            "kept_moment_id": loser_to.moment_id})
            continue
        kept.append(candidate)
    return kept, dropped


def _rank_key(moment: ScoredMoment) -> tuple:
    """Deterministic ordering: total desc, then unexpectedness,
    then entertainment, then earlier start, then moment id.
    Two moments with identical signals therefore always rank the
    same way — ties never depend on insertion order."""
    return (-moment.total, -moment.unexpectedness, -moment.entertainment,
            moment.signals.start_seconds, moment.moment_id)


def rank_key(moment: ScoredMoment) -> tuple:
    """Public alias for the deterministic ordering used by selection."""
    return _rank_key(moment)
