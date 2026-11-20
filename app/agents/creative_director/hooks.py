"""RANKFLOW AI — Creative Director Agent, Phase 2: hook generation (Prompt 07).

Several hook candidates are generated internally from the
user-supplied topic only — never from footage, which the agent
has not watched — and scored deterministically on attention,
topic relevance, curiosity, clarity and natural delivery. The
strongest wins; ties break by candidate order, never by chance.

No hook makes a misleading claim: none says what happens in a
clip, none promises a moment the ranking did not select, and
none claims a viral outcome. The topic itself is the user's
words, so quoting it (title-cased for speech) is framing, not
a footage claim.
"""
from __future__ import annotations

from dataclasses import dataclass

GENERATOR = "deterministic_template_v1"


def _title_case_topic(topic: str) -> str:
    """Title-case the topic for spoken delivery ("TOP 5 ..." -> "Top 5 ...").

    Acronyms / numbers are left alone; this is presentation of the
    user's own topic, not a new claim.
    """
    words = []
    for word in topic.strip().split():
        if word.isupper() and len(word) > 1 and word.isalpha():
            words.append(word.capitalize())
        else:
            words.append(word)
    return " ".join(words)


def _keywords(topic: str) -> set[str]:
    stop = {"the", "a", "an", "and", "of", "for", "to", "in", "on", "top", "5", "five"}
    return {w.strip(".,!?:;").lower() for w in topic.split()
            if w.strip(".,!?:;").lower() not in stop and len(w.strip(".,!?:;")) > 1}


@dataclass
class HookCandidate:
    text: str
    attention: float
    relevance: float
    curiosity: float
    clarity: float
    natural_delivery: float
    total: float
    explanation: str


def generate_hooks(topic: str) -> list[HookCandidate]:
    """Generate and score hook candidates for a topic.

    Scoring (0-100 each, total = mean): attention — opens with a
    number / countdown word; relevance — shares keywords with the
    topic; curiosity — points at the #1 moment without spoiling
    it; clarity — 6-16 words, no filler; natural delivery — no
    ALL-CAPS shouting, no repeated word, reads as one sentence.
    """
    spoken = _title_case_topic(topic)
    keywords = _keywords(topic)
    templates = [
        f"Five moments, ranked — from number five to number one.",
        f"Here are the five most striking moments we found for {spoken}.",
        f"Counting down the five top moments for {spoken} — number one is last.",
        f"{spoken} — five moments, ranked five to one.",
        f"Five clips, one countdown — starting at number five.",
    ]
    candidates: list[HookCandidate] = []
    for text in templates:
        words = text.replace("—", " ").replace(".", "").split()
        word_count = len(words)
        lowered = text.lower()
        # Attention: opens with a number word or a countdown verb.
        attention = 90.0 if lowered.startswith(("five", "counting")) else (
            70.0 if lowered.startswith("here are") else 60.0)
        # Relevance: share of topic keywords present in the hook.
        present = sum(1 for kw in keywords if kw in lowered)
        relevance = round(100.0 * present / len(keywords), 1) if keywords else 50.0
        # Curiosity: points forward to #1 without describing any clip.
        curiosity = 90.0 if "number one" in lowered else (
            75.0 if "ranked" in lowered else 50.0)
        # Clarity: 6-16 words is ideal; shorter is abrupt, longer rambles.
        if 6 <= word_count <= 16:
            clarity = 90.0
        elif word_count < 6:
            clarity = 60.0
        else:
            clarity = max(40.0, 90.0 - (word_count - 16) * 5.0)
        # Natural delivery: no ALL-CAPS token, no word repeated, one sentence.
        caps = [w for w in text.split() if w.isupper() and len(w) > 1 and w.isalpha()]
        repeated = len(words) != len({w.lower() for w in words})
        natural = 90.0
        if caps:
            natural -= 30.0
        if repeated:
            natural -= 20.0
        if text.count(".") > 1:
            natural -= 10.0
        total = round((attention + relevance + curiosity + clarity + natural) / 5.0, 1)
        candidates.append(HookCandidate(
            text=text, attention=attention, relevance=relevance,
            curiosity=curiosity, clarity=clarity, natural_delivery=natural,
            total=total,
            explanation=(f"attention {attention:.0f}, relevance {relevance:.0f} "
                         f"({present}/{len(keywords)} topic keywords), curiosity "
                         f"{curiosity:.0f}, clarity {clarity:.0f} ({word_count} words), "
                         f"natural delivery {natural:.0f}")))
    return candidates


def select_hook(topic: str) -> HookCandidate:
    """The strongest hook candidate; ties break by template order."""
    candidates = generate_hooks(topic)
    return max(enumerate(candidates), key=lambda pair: (pair[1].total, -pair[0]))[1]
