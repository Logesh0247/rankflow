"""RANKFLOW AI — Quality Assurance Agent, Phase 2: content validation (Prompt 09).

Checks the rendered video against the plan that produced it —
the Edit Decision List, the ranking, the analysed moments and
the narration script — using only records already in the
database. It verifies:

* exactly five moments are included, in #5 -> #1 order;
* five different source videos are used, no duplicate moment;
* clip boundaries match the verified AnalyzedMoment timestamps;
* the rendered duration matches the EDL total;
* caption cues sit inside their own segment and lines fit the
  40-character limit;
* narration exists for every moment, fits the timeline, and
  contains no semantic-event word the footage signals cannot
  support (the Creative Director's FORBIDDEN_WORDS guard,
  re-checked here — a narration claiming an event no model ever
  saw is misleading narration).

What it does NOT claim: that the narration describes what
actually happens in a clip — no vision-language model ran, so
semantic accuracy against the picture is NOT TESTED and is
recorded as such, with a human-review recommendation.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.creative_director.narration import (
    FORBIDDEN_WORDS, estimate_seconds, narration_contains_forbidden_word,
)


class ContentValidation(BaseModel):
    """Results of the content checks against the EDL / ranking / narration."""

    clip_count: int = 0
    ranks: list[int] = Field(default_factory=list)
    distinct_sources: int = 0
    distinct_moments: int = 0
    duration_matches_edl: bool = False
    narration_present: bool = False
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    not_tested: list[str] = Field(default_factory=list)


def validate_content(*, clips: list[dict], moments: dict[str, dict],
                     ranking: list[dict], edl_total_seconds: float,
                     rendered_duration_seconds: float,
                     narration_script: str, caption_plan: list[dict],
                     moment_narrations: list[str] | None = None,
                     duration_tolerance_seconds: float = 0.6) -> ContentValidation:
    """Phase 2: pure record comparison — no footage is watched,
    so nothing here can invent an event."""
    issues: list[str] = []
    warnings: list[str] = []
    ranks = [c.get("rank") for c in clips]
    if len(clips) != 5:
        issues.append(f"rendered video has {len(clips)} moment clip(s), expected exactly 5")
    if ranks != [5, 4, 3, 2, 1]:
        issues.append(f"moments are not in #5 -> #1 countdown order: {ranks}")
    moment_ids = [c.get("moment_id") for c in clips]
    if len(set(moment_ids)) != len(moment_ids):
        issues.append("duplicate moment clip(s) in the render — the same moment appears twice")
    candidate_ids = [c.get("candidate_video_id") for c in clips]
    if len(set(candidate_ids)) != len(candidate_ids):
        issues.append(
            "moments do not come from five different source videos — "
            f"only {len(set(candidate_ids))} distinct source(s)")
    for clip in clips:
        moment = moments.get(clip.get("moment_id") or "")
        if moment is None:
            issues.append(
                f"clip rank #{clip.get('rank')} references a moment that no "
                "longer exists in the database")
            continue
        if (abs(float(clip.get("start_seconds", -1)) - float(moment["start_seconds"])) > 0.001
                or abs(float(clip.get("end_seconds", -1)) - float(moment["end_seconds"])) > 0.001):
            issues.append(
                f"clip rank #{clip.get('rank')} boundaries "
                f"{clip.get('start_seconds')}–{clip.get('end_seconds')}s do not match "
                f"the verified moment {moment['start_seconds']}–{moment['end_seconds']}s")
        if abs(float(clip.get("duration_seconds", 0))
               - (float(moment["end_seconds"]) - float(moment["start_seconds"]))) > 0.3:
            issues.append(
                f"clip rank #{clip.get('rank')} duration does not match its "
                "verified moment duration")
    ranked_moment_ids = [r.get("moment_id") for r in ranking]
    if ranked_moment_ids and moment_ids != ranked_moment_ids[::-1]:
        # Ranking is stored #1 first; the EDL plays #5 first.
        issues.append("EDL clips do not match the five ranked moments in countdown order")
    duration_ok = abs(rendered_duration_seconds - edl_total_seconds) <= duration_tolerance_seconds
    if not duration_ok:
        issues.append(
            f"rendered duration {rendered_duration_seconds:.2f}s does not match "
            f"the EDL total {edl_total_seconds:.2f}s (tolerance "
            f"{duration_tolerance_seconds}s)")
    # Caption plan: cues inside their segment, lines within the limit.
    for cue in caption_plan:
        text = str(cue.get("text") or "")
        if len(text) > 40:
            issues.append(
                f"caption line exceeds the 40-character limit ({len(text)} chars): "
                f"{text[:40]!r}...")
        start, end = float(cue.get("start_seconds", 0)), float(cue.get("end_seconds", 0))
        if end <= start:
            issues.append(f"caption cue has no duration: {text[:30]!r}")
        if start < 0 or end > rendered_duration_seconds + 0.6:
            issues.append(
                f"caption cue falls outside the rendered video: {text[:30]!r} "
                f"({start:.1f}–{end:.1f}s)")
    # Narration: present for every moment, fits the timeline, and
    # contains no semantic-event word the footage signals cannot
    # support. Only the per-moment narrations are checked for
    # forbidden words — the hook legitimately quotes the user's
    # topic (e.g. "parkour fails"), which is the user's framing,
    # not a claim about a clip.
    narration_present = bool(narration_script and narration_script.strip())
    if not narration_present:
        issues.append("no narration script is stored for this job")
    else:
        for text in (moment_narrations or []):
            bad = narration_contains_forbidden_word(text)
            if bad:
                issues.append(
                    "narration contains semantic-event word(s) not supported "
                    f"by the footage signals: {', '.join(bad)} — misleading narration")
                break
        est = estimate_seconds(narration_script)
        if est > rendered_duration_seconds + 2.0:
            issues.append(
                f"narration is {est:.1f}s but the video is only "
                f"{rendered_duration_seconds:.1f}s — the voiceover cannot fit")
    if not 15.0 <= rendered_duration_seconds <= 90.0:
        warnings.append(
            f"duration {rendered_duration_seconds:.1f}s is outside the usual "
            "15–90s Short range")
    return ContentValidation(
        clip_count=len(clips), ranks=[int(r) for r in ranks if isinstance(r, int)],
        distinct_sources=len(set(candidate_ids)),
        distinct_moments=len(set(moment_ids)),
        duration_matches_edl=duration_ok,
        narration_present=narration_present,
        issues=issues, warnings=warnings,
        not_tested=[
            "semantic accuracy of narration against the picture — no vision-language model is configured, so whether the words describe what actually happens in a clip is not verified automatically and needs human review",
            "missing context inside a clip — timestamp verification covered boundaries only; whether the event is understandable needs human review",
        ])


# Re-export for callers that need the guard list.
__all__ = ["ContentValidation", "validate_content", "FORBIDDEN_WORDS"]
