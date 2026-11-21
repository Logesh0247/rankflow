"""Momentrix AI — Research Agent, Phase 1: topic understanding (Prompt 03).

Deterministic and rule-based, not an LLM call: no Muse AI key or
endpoint is configured in this environment (see docs/ARCHITECTURE.md),
so topic understanding is a transparent keyword / taxonomy pass.
Every decision it makes — category, keywords, expansions, queries —
is returned in the SearchPlan and can be inspected, which is also
what makes it testable without an external model.

Nothing here claims a search result can be downloaded or reused:
that decision belongs to the Rights Verification Agent (Prompt 04).
"""
from __future__ import annotations

import re
from collections import Counter

from pydantic import BaseModel, Field

STOPWORDS = frozenset(
    "a an the of for to in on at by with from and or is are was were be "
    "it its this that these those my your his her their our i you we he "
    "she they them us me him so do does did will would can could should "
    "about into over under after before between through during each "
    "more most other some any no not only own same than too very just "
    "also how what when where who whom why which".split()
)

# Rank / list words describe the *format*, not the subject. They are
# dropped from keywords but kept as content-type evidence.
RANK_WORDS = frozenset(
    "top best worst greatest craziest funniest biggest longest shortest "
    "most least first last ultimate epic amazing insane wild extreme "
    "crazy funny hilarious".split()
)

NUMBER_WORDS = frozenset(
    "zero one two three four five six seven eight nine ten 0 1 2 3 4 5 "
    "6 7 8 9 10".split()
)

# category -> indicative keywords (lowercase, single tokens / phrases)
CATEGORIES: dict[str, list[str]] = {
    "sports_action": ["parkour", "freerunning", "traceur", "skateboard", "skateboarding",
                      "surfing", "snowboard", "skiing", "bmx", "climbing", "gymnastics",
                      "stunt", "motocross", "trick", "vault", "flip", "jump"],
    "comedy_fails": ["fail", "fails", "funny", "hilarious", "blooper", "bloopers",
                     "mishap", "crash", "wipeout", "prank", "comedy"],
    "animals": ["cat", "cats", "dog", "dogs", "puppy", "kitten", "bird", "horse",
                "elephant", "lion", "panda", "monkey", "animal", "animals", "pet", "pets"],
    "nature": ["nature", "wildlife", "ocean", "forest", "mountain", "waterfall",
               "desert", "volcano", "storm", "aurora"],
    "science": ["science", "space", "rocket", "rockets", "nasa", "physics",
                "experiment", "astronaut", "planet", "mars", "moon", "black", "hole"],
    "history": ["history", "ancient", "war", "empire", "king", "queen", "battle",
                "medieval", "roman", "egypt"],
    "finance": ["money", "finance", "billion", "billionaire", "stock", "investing",
                "business", "salary", "wealth", "bank", "crypto"],
    "gaming": ["game", "gaming", "minecraft", "fortnite", "speedrun", "gameplay",
               "gamer", "roblox", "zelda", "mario"],
    "music": ["music", "song", "concert", "dance", "singing", "guitar", "piano", "band"],
    "cooking": ["cooking", "recipe", "food", "chef", "baking", "restaurant", "kitchen"],
    "technology": ["technology", "ai", "robot", "computer", "gadget", "iphone",
                   "software", "internet"],
    "travel": ["travel", "city", "beach", "adventure", "vlog", "tour", "island", "japan"],
    "sports": ["football", "soccer", "basketball", "tennis", "goal", "cricket",
               "baseball", "hockey", "olympics", "marathon"],
}

SYNONYMS: dict[str, list[str]] = {
    "parkour": ["free running", "freerunning", "traceur", "urban acrobatics"],
    "fail": ["falls", "bloopers", "mishaps", "crashes", "wipeouts"],
    "fails": ["falls", "bloopers", "mishaps", "crashes", "wipeouts"],
    "funny": ["hilarious", "comedy", "humour", "humor"],
    "cat": ["kitten", "feline"],
    "cats": ["kittens", "felines"],
    "dog": ["puppy", "canine"],
    "dogs": ["puppies", "canines"],
    "rocket": ["spaceship", "launch", "nasa"],
    "rockets": ["spaceships", "launches", "nasa"],
    "space": ["outer space", "cosmos", "astronaut"],
    "crazy": ["insane", "wild", "extreme", "unbelievable"],
    "craziest": ["insane", "wildest", "extreme", "unbelievable"],
    "top": ["best", "greatest"],
    "best": ["top", "greatest"],
    "stunt": ["trick", "feat"],
    "stunts": ["tricks", "feats"],
    "jump": ["leap", "vault"],
    "money": ["wealth", "cash", "finance"],
}

RELATED_CONCEPTS: dict[str, list[str]] = {
    "sports_action": ["stunt", "athlete", "training", "competition", "trick"],
    "comedy_fails": ["bloopers", "prank", "reaction", "compilation"],
    "animals": ["cute", "pet", "wildlife", "funny animal"],
    "nature": ["landscape", "documentary", "wildlife"],
    "science": ["documentary", "experiment", "discovery", "explained"],
    "history": ["documentary", "battle", "empire", "ancient"],
    "finance": ["business", "investing", "wealth", "explained"],
    "gaming": ["gameplay", "speedrun", "walkthrough", "funny moments"],
    "music": ["live", "concert", "cover", "performance"],
    "cooking": ["recipe", "tutorial", "food", "chef"],
    "technology": ["review", "gadget", "tutorial", "explained"],
    "travel": ["vlog", "tour", "guide", "adventure"],
    "sports": ["highlights", "goal", "match", "championship"],
}

EVENT_WORDS = frozenset(
    "fail fails stunt trick race goal dance crash jump flip rescue launch "
    "landing tutorial review unboxing documentary interview compilation "
    "battle fight wedding proposal".split()
)


class SearchQuery(BaseModel):
    """One query the agent will send to a discovery provider."""

    text: str
    purpose: str
    kind: str = Field(..., pattern=r"^(exact|expanded|broad)$")


class SearchPlan(BaseModel):
    """Everything Phase 1 decided about a topic, before any search."""

    topic: str
    category: str
    keywords: list[str] = Field(default_factory=list)
    expanded_keywords: list[str] = Field(default_factory=list)
    related_concepts: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    content_type: str
    queries: list[SearchQuery] = Field(default_factory=list)


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _classify(tokens: list[str], text: str) -> str:
    """Best-matching category by keyword hits; 'general' if none hit."""
    scores: Counter[str] = Counter()
    for category, words in CATEGORIES.items():
        for word in words:
            if " " in word:
                if word in text:
                    scores[category] += 2
            elif word in tokens:
                scores[category] += 1
    if not scores:
        return "general"
    # Deterministic tie-break: highest score, then category order in CATEGORIES.
    best = max(scores.values())
    for category in CATEGORIES:
        if scores.get(category) == best:
            return category
    return "general"  # unreachable


def _keywords(tokens: list[str]) -> list[str]:
    seen: list[str] = []
    for token in tokens:
        if len(token) < 2:
            continue
        if token in STOPWORDS or token in RANK_WORDS or token in NUMBER_WORDS:
            continue
        if token not in seen:
            seen.append(token)
    return seen


def _expand(keywords: list[str]) -> list[str]:
    expanded: list[str] = list(keywords)
    for keyword in keywords:
        for synonym in SYNONYMS.get(keyword, []):
            if synonym not in expanded:
                expanded.append(synonym)
        # Singular / plural variants: "cat" <-> "cats", "fail" <-> "fails".
        if keyword.endswith("s") and keyword[:-1] not in expanded:
            expanded.append(keyword[:-1])
        elif f"{keyword}s" not in expanded:
            expanded.append(f"{keyword}s")
    return expanded


def _content_type(topic_lower: str, tokens: list[str]) -> str:
    if any(w in topic_lower for w in ("how to", "how do", "tutorial", "guide to")):
        return "tutorial"
    if "documentary" in tokens or "history of" in topic_lower:
        return "documentary"
    if "review" in tokens or "unboxing" in tokens:
        return "review"
    if any(w in tokens for w in ("top", "best", "worst", "greatest")):
        return "ranking_compilation"
    return "video"


def understand_topic(topic: str) -> SearchPlan:
    """Phase 1: classify, extract keywords, expand, build a search strategy."""
    cleaned = " ".join(topic.strip().split())
    if not cleaned:
        raise ValueError("topic must not be empty")
    lower = cleaned.lower()
    tokens = tokenize(lower)
    keywords = _keywords(tokens)
    category = _classify(tokens, lower)
    expanded = _expand(keywords)
    events = sorted({t for t in tokens if t in EVENT_WORDS})
    related = RELATED_CONCEPTS.get(category, [])

    queries: list[SearchQuery] = []
    if keywords:
        queries.append(SearchQuery(
            text=" ".join(keywords), purpose="exact subject keywords", kind="exact"))
    if len(expanded) > len(keywords):
        queries.append(SearchQuery(
            text=" ".join(expanded[:8]), purpose="synonym / plural expansion", kind="expanded"))
    if category != "general":
        broad = " ".join(dict.fromkeys([category.replace("_", " ")] + keywords[:2]))
        if broad not in {q.text for q in queries}:
            queries.append(SearchQuery(
                text=broad, purpose="broad category fallback", kind="broad"))
    if not queries:  # topic was only stopwords / rank words, e.g. "top 5"
        queries.append(SearchQuery(
            text=cleaned.lower(), purpose="topic as given (no keywords extracted)",
            kind="exact"))
    return SearchPlan(
        topic=cleaned,
        category=category,
        keywords=keywords,
        expanded_keywords=expanded,
        related_concepts=related,
        events=events,
        content_type=_content_type(lower, tokens),
        queries=queries,
    )
