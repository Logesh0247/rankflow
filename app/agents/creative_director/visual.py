"""Momentrix AI — Creative Director Agent, Phase 5: visual direction (Prompt 07).

Defines countdown graphics, caption placement, text emphasis,
transition types, sound-effect cues, background-music
requirements and attribution display. All of it is derived from
the plan (rank, measured signals, rights attribution) — no
specific music track, sound file or graphic asset is named,
because no licensed music / SFX library is configured in this
environment. Production must supply licensed assets; this
direction says what is required, not what exists.
"""
from __future__ import annotations

from app.agents.creative_director.signals import MomentPlanInput

CAPTION_PLACEMENT = "bottom third, centred, inside the 10% safe margins"
CAPTION_MAX_CHARS = 40
TRANSITION_SECONDS = 0.5


def transition_for(moment: MomentPlanInput) -> str:
    """Transition into this moment's clip, from its measured motion.

    A high-motion segment cuts hard; a calmer one dissolves, so
    the cut does not jar. The rule is deterministic and recorded
    per clip, not chosen per clip by taste.
    """
    if moment.mean_motion >= 8.0 or moment.event_type == "high_motion_segment":
        return "hard cut"
    return f"cross-dissolve ({TRANSITION_SECONDS:.1f}s)"


def visual_direction(moment: MomentPlanInput) -> dict:
    """Visual direction for one countdown moment."""
    source_label = (f"{moment.candidate_title or 'Untitled video'}"
                    + (f" by {moment.candidate_creator}"
                       if moment.candidate_creator else ""))
    return {
        "rank": moment.rank,
        "moment_id": moment.moment_id,
        "countdown_graphics": {
            "number_overlay": f"#{moment.rank}",
            "rank_label": f"No. {moment.rank} of 5",
            "score_note": f"Score {moment.score_100:.1f}/100 (editorial heuristic, not a viral prediction)",
        },
        "caption_placement": CAPTION_PLACEMENT,
        "text_emphasis": f"Emphasise the rank number and the word '{moment.motion_word}' in the caption.",
        "transition_in": transition_for(moment),
        "sound_effect_cues": [
            "Countdown chime on the rank reveal.",
            "Soft whoosh on the transition into the clip.",
        ],
        "source_label": source_label,
        "attribution_display": (
            moment.attribution_text if moment.attribution_required
            else (f"Attribution: {moment.attribution_text}"
                  if moment.attribution_text
                  else "Attribution: not required by the licence on record.")),
        "attribution_required": moment.attribution_required,
        "licence_name": moment.licence_name,
    }


def music_requirements() -> dict:
    """Background-music requirements (no track is selected here)."""
    return {
        "required": True,
        "source": "Licensed background music only — no track is selected: "
                  "no licensed music library is configured in this environment.",
        "ducking": "Duck music under narration; narration stays clearly audible.",
        "fade_in_seconds": 0.5,
        "fade_out_seconds": 1.0,
        "normalisation": "Normalise the final mix; prevent clipping.",
    }


def overall_visual_direction(moments: list[MomentPlanInput]) -> dict:
    """The plan-level visual direction handed to Production."""
    return {
        "format": "9:16 vertical, 1080x1920, 30 fps",
        "caption_placement": CAPTION_PLACEMENT,
        "caption_max_chars_per_line": CAPTION_MAX_CHARS,
        "caption_lines_max": 2,
        "transition_seconds": TRANSITION_SECONDS,
        "music": music_requirements(),
        "sound_effects": "Licensed sound effects only — no SFX file is selected: "
                         "no licensed SFX library is configured in this environment.",
        "attribution_display": "Show the attribution for every clip whose licence "
                               "requires it, in the lower third at the end of its segment; "
                               "include the full credits in the outro.",
        "per_moment": [visual_direction(m) for m in moments],
    }
