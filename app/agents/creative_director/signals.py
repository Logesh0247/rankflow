"""Momentrix AI — Creative Director Agent, Phase 1: creative planning inputs (Prompt 07).

Reads a ranked moment's measured signals out of its
AnalyzedMoment row — the same labels Video Intelligence wrote
and Ranking scored. Nothing here is inferred from footage: every
field comes from the persisted row, its candidate, or its ranking
row. The signal set is what the narration is allowed to describe;
anything not in it (what happens in the scene, who does what,
dialogue) is out of bounds for the narration by design.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models.models import AnalyzedMoment, CandidateVideo, RankingResult


@dataclass
class MomentPlanInput:
    """One ranked moment, with everything the creative plan may use."""

    rank: int  # 1 = best, 5 = last — the countdown runs 5 -> 1
    moment_id: str
    candidate_video_id: str
    start_seconds: float
    end_seconds: float
    description: str | None
    # Measured frame signals (Video Intelligence labels).
    event_type: str = "scene_segment"
    confidence: float = 0.0
    visual_quality: float = 0.0
    context_clarity: float = 0.0
    mean_motion: float = 0.0
    mean_sharpness: float = 0.0
    mean_brightness: float = 0.0
    frame_refs: list[str] = field(default_factory=list)
    verification_issues: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    thumbnail: str | None = None
    # Ranking scores (editorial heuristics, Prompt 06).
    score_100: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    explanation: str = ""
    # Provider-published source metadata (for on-screen labels only —
    # never as evidence of what the footage shows).
    candidate_title: str | None = None
    candidate_creator: str | None = None
    candidate_url: str | None = None
    # Rights attribution (Prompt 04), for the credits / lower third.
    attribution_text: str | None = None
    attribution_required: bool = False
    licence_name: str | None = None
    # Local footage file when on record (ApprovedFootage.storage_path);
    # None means Production must resolve the source URL first.
    footage_path: str | None = None

    @property
    def duration(self) -> float:
        return round(self.end_seconds - self.start_seconds, 3)

    @property
    def motion_word(self) -> str:
        """Motion descriptor, from the measured mean frame difference."""
        if self.mean_motion >= 20.0:
            return "fast-moving"
        if self.mean_motion >= 8.0:
            return "high-motion"
        if self.mean_motion >= 2.0:
            return "moderate-motion"
        return "low-motion"

    @property
    def strongest_component(self) -> str:
        """The ranking component that carried this moment's score."""
        if not self.components:
            return "entertainment"
        order = ("entertainment", "unexpectedness", "visual_impact",
                 "context_clarity", "technical_quality")
        return max(order, key=lambda name: float(self.components.get(name, 0.0)))


def moment_plan_input(moment: AnalyzedMoment, candidate: CandidateVideo,
                      ranking: RankingResult, *, attribution: dict | None = None) -> MomentPlanInput:
    """Build the planning input for one ranked moment from its rows."""
    labels = dict(moment.labels or {})
    probe = labels.get("probe") or {}
    verification = labels.get("verification") or {}
    breakdown = dict(ranking.score_breakdown or {})
    attribution = attribution or {}
    return MomentPlanInput(
        rank=ranking.rank, moment_id=moment.id,
        candidate_video_id=moment.candidate_video_id,
        start_seconds=moment.start_seconds, end_seconds=moment.end_seconds,
        description=moment.description,
        event_type=str(labels.get("event_type") or "scene_segment"),
        confidence=float(labels.get("confidence") or moment.score or 0.0),
        visual_quality=float(labels.get("visual_quality") or 0.0),
        context_clarity=float(labels.get("context_clarity") or 0.0),
        mean_motion=float(labels.get("mean_motion") or 0.0),
        mean_sharpness=float(labels.get("mean_sharpness") or 0.0),
        mean_brightness=float(labels.get("mean_brightness") or 0.0),
        frame_refs=[str(r) for r in (labels.get("frame_refs") or [])],
        verification_issues=[str(i) for i in (verification.get("issues") or [])],
        width=int(probe.get("width") or 0), height=int(probe.get("height") or 0),
        fps=float(probe.get("fps") or 0.0),
        has_audio=bool(probe.get("has_audio") or False),
        thumbnail=labels.get("thumbnail"),
        score_100=float(breakdown.get("total") or ranking.score * 100.0),
        components={k: float(v) for k, v in breakdown.items()
                    if k in ("entertainment", "unexpectedness", "visual_impact",
                             "context_clarity", "technical_quality")},
        explanation=ranking.reason or "",
        candidate_title=candidate.title, candidate_creator=candidate.creator,
        candidate_url=candidate.url,
        attribution_text=attribution.get("attribution_text"),
        attribution_required=bool(attribution.get("attribution_required")),
        licence_name=attribution.get("licence_name"))
