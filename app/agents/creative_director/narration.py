"""RANKFLOW AI — Creative Director Agent, Phases 3-4: countdown storytelling and narration (Prompt 07).

Narration is built ONLY from what Video Intelligence measured
for the moment — motion, sharpness, brightness, visual quality,
context clarity, duration, verification issues — plus the rank
and the ranking component that carried its score. It never names
what happens in the scene, who does what, or any dialogue, because
no vision-language model ran and none of that is in the data.
The source title/creator are shown on screen (visual direction),
never spoken as if they described the footage.

FORBIDDEN_WORDS is the guard the validator (service.py) checks
every moment narration against: any word in it means a semantic
event leaked into the narration, and the plan is rejected, not
quietly edited. The templates below are written to contain none
of them; the guard exists so a future template change cannot
silently start inventing events.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.creative_director.signals import MomentPlanInput

# Semantic-event words that must never appear in per-moment
# narration. The topic itself may contain some of these (e.g.
# "parkour fails") — that is the user's framing in the hook/title,
# not a claim about a clip, so the hook is checked separately and
# moment narration is checked against this list.
FORBIDDEN_WORDS = frozenset({
    "fall", "falls", "falling", "fell", "jump", "jumps", "jumping", "jumped",
    "flip", "flips", "flipping", "flipped", "crash", "crashes", "crashing",
    "crashed", "slip", "slips", "slipping", "slipped", "trip", "trips",
    "tripping", "tripped", "fail", "fails", "failing", "failed", "parkour",
    "stunt", "stunts", "trick", "tricks", "dog", "cat", "car", "bike",
    "skateboard", "climb", "climbs", "climbing", "run", "runs", "running",
    "says", "said", "shouts", "screams", "laughs", "cries",
})

# Commentary per strongest ranking component — each phrase is a
# paraphrase of the measured signal that carried the score, not a
# description of an event.
_COMMENTARY = {
    "entertainment": "Fast, clear, and hard to look away from.",
    "unexpectedness": "Short, sharp, and sudden.",
    "visual_impact": "Bright, sharp, and easy to follow.",
    "context_clarity": "The picture stays easy to read.",
    "technical_quality": "Clean picture, steady and clear.",
}


def _word_count(text: str) -> int:
    return len(text.split())


def _context_phrase(moment: MomentPlanInput) -> str:
    issues = " ".join(moment.verification_issues).lower()
    if "frozen" in issues:
        return "A still, unchanging segment."
    if "truncated" in issues:
        return "A short segment, cut at the edge of the clip."
    if moment.context_clarity >= 0.8:
        return "Clear from start to finish."
    return "A steady segment, easy to follow."


def _descriptor_phrase(moment: MomentPlanInput) -> str:
    if moment.visual_quality >= 0.7:
        return f"A sharp, {moment.motion_word} segment."
    if moment.mean_brightness < 60:
        return f"A {moment.motion_word} segment, dark at the edges."
    if moment.mean_brightness > 200:
        return f"A {moment.motion_word} segment, very bright throughout."
    return f"A {moment.motion_word} segment."


@dataclass
class MomentNarration:
    rank: int
    moment_id: str
    text: str
    word_count: int
    estimated_seconds: float
    basis: str  # the measured signals this wording rests on
    phrases_used: list[str] = field(default_factory=list)


def build_moment_narration(moment: MomentPlanInput, *, word_budget: int,
                           words_per_minute: float = 150.0) -> MomentNarration:
    """Build one moment's narration within a word budget.

    Phrases are added in priority order — rank intro, descriptor,
    context, commentary — and a phrase is skipped entirely if it
    does not fit the remaining budget, so the narration is never
    cut mid-sentence. The rank intro is always kept: a countdown
    moment without its number is not a countdown.
    """
    intro = f"Number {moment.rank}."
    phrases: list[str] = [intro]
    remaining = max(0, word_budget - _word_count(intro))
    # Score phrase last: a factual ranking fact that also varies
    # the narration between moments with otherwise similar signals.
    score_phrase = f"Score: {moment.score_100:.0f} out of 100."
    for phrase in (_descriptor_phrase(moment), _context_phrase(moment),
                   _COMMENTARY[moment.strongest_component], score_phrase):
        words = _word_count(phrase)
        if words <= remaining:
            phrases.append(phrase)
            remaining -= words
    text = " ".join(phrases)
    basis = (
        f"rank #{moment.rank} of 5, score {moment.score_100:.1f}/100 "
        f"(strongest: {moment.strongest_component}); measured signals: "
        f"{moment.event_type}, mean motion {moment.mean_motion:.1f}, "
        f"mean sharpness {moment.mean_sharpness:.0f}, mean brightness "
        f"{moment.mean_brightness:.0f}, visual quality "
        f"{moment.visual_quality:.2f}, context clarity "
        f"{moment.context_clarity:.2f}, duration {moment.duration:.1f}s"
        + (f"; verification: {'; '.join(moment.verification_issues)}"
           if moment.verification_issues else ""))
    return MomentNarration(
        rank=moment.rank, moment_id=moment.moment_id, text=text,
        word_count=_word_count(text),
        estimated_seconds=round(_word_count(text) / words_per_minute * 60.0, 2),
        basis=basis, phrases_used=phrases)


def closing_text() -> str:
    """Outro narration. States only the ranking fact, not an event."""
    return "That is number one — the highest-scoring segment we found."


def narration_contains_forbidden_word(text: str) -> list[str]:
    """Semantic-event words found in a narration (empty = clean)."""
    words = {w.strip(".,!?:;—").lower() for w in text.split()}
    return sorted(words & FORBIDDEN_WORDS)


def estimate_seconds(text: str, *, words_per_minute: float = 150.0) -> float:
    return round(_word_count(text) / words_per_minute * 60.0, 2)


def split_captions(text: str, *, max_chars: int = 40) -> list[str]:
    """Split narration into caption lines of at most `max_chars`.

    Splits on word boundaries, never mid-word; a single word
    longer than the limit is kept whole on its own line.
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
