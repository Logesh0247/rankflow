"""Momentrix AI — Quality Assurance Agent, Phase 6: automated repair requests (Prompt 09).

For each failure (and each issue that leaves the video in
``needs_review``), one structured correction request: what is
wrong, which component owns the fix, what the correction is,
and which stage/agent must run again. QA does not fix the video
itself — it returns the requests to the orchestrator, and a
corrected render is re-inspected by a fresh QA run. The number
of repair rounds is capped (``qa_max_repair_attempts``); past
that, the video fails and no further requests are issued.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class RepairRequest(BaseModel):
    """One structured correction request, returned to the orchestrator."""

    issue: str
    responsible_component: str  # e.g. "production", "creative_director", "rights", "config"
    correction: str
    target_stage: str  # workflow stage that must run again, or "manual_review"
    target_agent: str
    retry_limit: int = 2


# (keyword in the issue text) -> (component, correction, stage, agent)
_RULES: list[tuple[str, str, str, str, str]] = [
    ("resolution", "production",
     "Re-render at 1080x1920 (9:16, 30 fps) from the approved EDL.",
     "VIDEO_PRODUCTION", "production"),
    ("frame rate", "production",
     "Re-render at 30 fps from the approved EDL.",
     "VIDEO_PRODUCTION", "production"),
    ("video codec", "production",
     "Re-encode the render as H.264 (libx264, yuv420p).",
     "VIDEO_PRODUCTION", "production"),
    ("audio codec", "production",
     "Re-encode the audio track as AAC.",
     "VIDEO_PRODUCTION", "production"),
    ("no audio stream", "production",
     "Re-render with an audio track (source audio and/or narration).",
     "VIDEO_PRODUCTION", "production"),
    ("decode check", "production",
     "Re-render the video — the delivered file does not decode cleanly.",
     "VIDEO_PRODUCTION", "production"),
    ("does not exist", "production",
     "Re-run production — no rendered file is on disk for this job.",
     "VIDEO_PRODUCTION", "production"),
    ("duration", "production",
     "Re-render from the approved EDL so the timeline matches the plan.",
     "VIDEO_PRODUCTION", "production"),
    ("black frame", "production",
     "Re-render and check the segment at the reported timestamp — "
     "a content segment is black.",
     "VIDEO_PRODUCTION", "production"),
    ("frozen", "video_intelligence",
     "Re-analyse the source — the moment is a frozen segment — and "
     "re-rank with a moving moment, or drop it.",
     "VIDEO_ANALYSIS", "video_intelligence"),
    ("blank", "production",
     "Re-render — a content segment is blank (no footage or graphics).",
     "VIDEO_PRODUCTION", "production"),
    ("letterboxed", "production",
     "Re-render with the crop window corrected — the clip is letterboxed.",
     "VIDEO_PRODUCTION", "production"),
    ("duplicate", "ranking",
     "Re-run ranking — duplicate moments or sources were selected.",
     "RANKING", "ranking"),
    ("different source videos", "ranking",
     "Re-run ranking with five eligible sources — fewer than five "
     "distinct sources were used.",
     "RANKING", "ranking"),
    ("countdown order", "creative_director",
     "Rebuild the storyboard/EDL in #5 -> #1 order.",
     "CREATIVE_PLANNING", "creative_director"),
    ("boundaries", "video_intelligence",
     "Re-verify the moment timestamps against the original video.",
     "VIDEO_ANALYSIS", "video_intelligence"),
    ("caption", "creative_director",
     "Rebuild the caption plan — cues must sit inside their segment "
     "and lines must fit 40 characters.",
     "CREATIVE_PLANNING", "creative_director"),
    ("narration contains", "creative_director",
     "Rewrite the narration from the measured signals only — it "
     "claims an event the footage does not support.",
     "CREATIVE_PLANNING", "creative_director"),
    ("narration is", "creative_director",
     "Shorten the narration so the voiceover fits the video timeline.",
     "CREATIVE_PLANNING", "creative_director"),
    ("no narration script", "creative_director",
     "Re-run the Creative Director — no narration script is stored.",
     "CREATIVE_PLANNING", "creative_director"),
    ("not approved", "rights",
     "Supply licence evidence or a manual review for the candidate, "
     "or replace it with an approved source and re-run ranking.",
     "RIGHTS_VERIFICATION", "rights"),
    ("requires attribution", "production",
     "Re-render with the required attribution text burned in and in "
     "the outro credits.",
     "VIDEO_PRODUCTION", "production"),
    ("music is recorded as included", "production",
     "Re-render with the licensed music file actually mixed in, or "
     "correct the manifest.",
     "VIDEO_PRODUCTION", "production"),
    ("effectively silent", "config",
     "Configure a TTS provider (or supply narration audio) and "
     "re-render — the audio track carries no voice.",
     "VIDEO_PRODUCTION", "production"),
    ("narration audio is not generated", "config",
     "Enable TTS (TTS_ENABLED=true, TTS_PROVIDER=edge-tts) and "
     "re-render so the narration has a voice.",
     "VIDEO_PRODUCTION", "production"),
    ("narration audio generation failed", "production",
     "Re-run production — the TTS endpoint failed during this render; "
     "synthesis may succeed on retry, or check the endpoint / voice "
     "configuration before re-rendering.",
     "VIDEO_PRODUCTION", "production"),
    ("no licensed background music", "config",
     "Configure a licensed music directory (PROD_MUSIC_DIR) or accept "
     "a music-free render in human review.",
     "manual_review", "quality_assurance"),
    ("no licensed sound effects", "config",
     "Configure a licensed SFX directory (PROD_SFX_DIR) or accept "
     "an SFX-free render in human review.",
     "manual_review", "quality_assurance"),
]


def build_repair_requests(issues: list[str], *, retry_limit: int = 2) -> list[RepairRequest]:
    """One request per issue, routed by keyword to the owning
    component. Unknown issues go to manual review — never dropped."""
    requests: list[RepairRequest] = []
    for issue in issues:
        lowered = issue.lower()
        for keyword, component, correction, stage, agent in _RULES:
            if keyword in lowered:
                requests.append(RepairRequest(
                    issue=issue, responsible_component=component,
                    correction=correction, target_stage=stage,
                    target_agent=agent, retry_limit=retry_limit))
                break
        else:
            requests.append(RepairRequest(
                issue=issue, responsible_component="quality_assurance",
                correction="Human review required — no automated repair rule covers this issue.",
                target_stage="manual_review", target_agent="quality_assurance",
                retry_limit=retry_limit))
    return requests
