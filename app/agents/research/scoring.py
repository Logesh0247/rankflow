"""Momentrix AI — Research Agent, Phase 4: candidate evaluation (Prompt 03).

Scoring is deliberately separated into three named quantities so
nobody confuses one for another (the brief warns about this):

* ``relevance`` — how well the candidate's title / description match
  the topic keywords. 0-1. This is search relevance only.
* ``completeness`` — fraction of the metadata fields actually present
  (title, creator, description, URL, duration, publication date,
  licence). A candidate with no duration or licence is *incomplete*,
  not wrong — it is marked, not dropped.
* ``reliability`` — how trustworthy the *provider* record is:
  Internet Archive 0.8 (a real archive with provenance, but
  user-uploaded content varies), YouTube 0.7 (platform metadata, but
  licence/reuse is unverified until Prompt 04), test 0.3 (labelled
  test data). Popularity (downloads / views) adds a small capped
  bonus. Reliability is NOT content quality and NOT reuse permission.

Prioritisation uses ``combined = 0.6*relevance + 0.2*completeness
+ 0.2*reliability``. ``relevance_score`` stored on the candidate is
the pure relevance value, so later stages can re-weight it.

Duration preference (2026-11-21): a Short is built from short
clips, so the combined score is multiplied by a duration factor
(see ``duration_factor``): videos under 10 minutes get a boost,
videos over the configured cap (default 15 minutes) score 0 and
are excluded by the service, and videos with unknown duration
are neither boosted nor penalised — they are kept. The factor
applies in the shared scoring path, so it covers every provider
(Archive durations arrive late via enrichment — the service
re-applies the cap after enrichment, so a movie whose duration
only becomes known then is still excluded).
"""
from __future__ import annotations

import math
import re
from urllib.parse import urlparse

from app.agents.research.providers import RawCandidate
from app.agents.research.topic import tokenize

PROVIDER_RELIABILITY: dict[str, float] = {
    "internet_archive": 0.8,
    "youtube": 0.7,
    # Stock libraries: curated platform metadata and stable file
    # URLs, but generic/staged footage — trustworthy records, not
    # specific viral moments.
    "pexels": 0.75,
    "pixabay": 0.75,
    "test": 0.3,
}

# A Short is cut from short clips: under this long a video is the
# right shape for a moment, so it is boosted.
SHORT_VIDEO_SECONDS = 600.0  # 10 minutes
DEFAULT_MAX_CANDIDATE_MINUTES = 15.0


def duration_factor(duration_seconds: float | None,
                    max_candidate_minutes: float = DEFAULT_MAX_CANDIDATE_MINUTES) -> float:
    """Score multiplier for a candidate's duration.

    Unknown duration (None) -> 1.0: kept, never excluded, just not
    boosted — many Archive candidates report no duration. Under
    10 minutes -> 1.15: a clear boost for the short clips a Short
    is built from. Between 10 minutes and the cap -> 1.0. Over the
    cap -> 0.0: excluded by the service (a feature film is never
    the right source). The cap is configurable
    (RESEARCH_MAX_CANDIDATE_MINUTES, default 15).
    """
    if duration_seconds is None:
        return 1.0
    # The cap is checked first: a video over the cap is excluded
    # even when it is under the boost threshold (e.g. a 6-minute
    # video with a 5-minute cap).
    if duration_seconds > max_candidate_minutes * 60.0:
        return 0.0
    if duration_seconds <= SHORT_VIDEO_SECONDS:
        return 1.15
    return 1.0

COMPLETENESS_FIELDS = ("title", "creator", "description", "url",
                       "duration_seconds", "published_at", "licence")


def relevance_score(keywords: list[str], candidate: RawCandidate) -> float:
    """Keyword overlap: title counts most, description next, creator least."""
    if not keywords:
        return 0.0
    keyword_set = set(keywords)
    title_tokens = set(tokenize(candidate.title or ""))
    desc_tokens = set(tokenize(candidate.description or ""))
    creator_tokens = set(tokenize(candidate.creator or ""))
    title_hit = len(keyword_set & title_tokens) / len(keyword_set)
    desc_hit = len(keyword_set & desc_tokens) / len(keyword_set)
    creator_hit = len(keyword_set & creator_tokens) / len(keyword_set)
    score = 0.65 * title_hit + 0.30 * desc_hit + 0.05 * creator_hit
    # Exact-phrase bonus: the whole keyword phrase in the title.
    phrase = " ".join(keywords)
    if len(keywords) > 1 and phrase in (candidate.title or "").lower():
        score += 0.10
    return round(min(1.0, max(0.0, score)), 4)


def completeness_score(candidate: RawCandidate) -> float:
    present = sum(1 for f in COMPLETENESS_FIELDS if getattr(candidate, f) not in (None, ""))
    return round(present / len(COMPLETENESS_FIELDS), 4)


def reliability_score(candidate: RawCandidate) -> float:
    base = PROVIDER_RELIABILITY.get(candidate.provider, 0.5)
    popularity = candidate.view_count or 0
    # log-scaled, capped at +0.1: 10 downloads ≈ +0.03, 10,000 ≈ +0.1.
    bonus = min(0.1, 0.025 * math.log10(popularity + 1)) if popularity > 0 else 0.0
    return round(min(1.0, base + bonus), 4)


def combined_score(relevance: float, completeness: float, reliability: float) -> float:
    return round(0.6 * relevance + 0.2 * completeness + 0.2 * reliability, 4)


class ScoredCandidate:
    """A RawCandidate with its three evaluation scores attached.

    `combined` is the weighted score multiplied by the duration
    factor (short videos boosted, over-cap videos score 0 — see
    `duration_factor`). `max_candidate_minutes` is the exclusion
    cap; the service passes the configured value, the default
    (15) matches the default setting.
    """

    def __init__(self, candidate: RawCandidate, keywords: list[str],
                 max_candidate_minutes: float = DEFAULT_MAX_CANDIDATE_MINUTES):
        self.candidate = candidate
        self.relevance = relevance_score(keywords, candidate)
        self.completeness = completeness_score(candidate)
        self.reliability = reliability_score(candidate)
        self.duration_factor = duration_factor(
            candidate.duration_seconds, max_candidate_minutes)
        self.combined = round(
            combined_score(self.relevance, self.completeness,
                           self.reliability) * self.duration_factor, 4)

    def rescore_duration(self, max_candidate_minutes: float) -> None:
        """Re-apply the duration factor after the duration changed
        (e.g. Archive enrichment filled it in). Recomputes
        completeness too, since a known duration is a present field."""
        self.completeness = completeness_score(self.candidate)
        self.duration_factor = duration_factor(
            self.candidate.duration_seconds, max_candidate_minutes)
        self.combined = round(
            combined_score(self.relevance, self.completeness,
                           self.reliability) * self.duration_factor, 4)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"ScoredCandidate({self.candidate.external_id!r} "
                f"rel={self.relevance} comb={self.combined})")


def _normalize_url(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url.lower())
    host = parsed.netloc.removeprefix("www.")
    path = parsed.path.rstrip("/")
    # Keep the query string: YouTube watch URLs carry the video id
    # in `?v=…`, so dropping it would collapse every YouTube video
    # to the same key ("youtube.com/watch") and dedupe would keep
    # only one YouTube candidate per job. Identical URLs still
    # normalise identically.
    query = parsed.query
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def _normalize_title(title: str | None) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return re.sub(r"\s+", " ", text)


def deduplicate(scored: list[ScoredCandidate]) -> tuple[list[ScoredCandidate], int]:
    """Remove duplicates, keeping the highest-combined record each time.

    Duplicate keys, in order:
    1. (platform, external_id) — the same item returned twice.
    2. Normalised URL — the same item under a different id.
    3. Normalised title + creator (+ duration within 10 s, when known)
       — the same video re-uploaded under a new id.
    Returns (kept, duplicates_removed).
    """
    kept: list[ScoredCandidate] = []
    seen_ids: set[tuple[str, str]] = set()
    seen_urls: set[str] = set()
    seen_title_creator: set[tuple[str, str, int | None]] = set()
    removed = 0
    for item in sorted(scored, key=lambda s: s.combined, reverse=True):
        cand = item.candidate
        id_key = (cand.platform, cand.external_id)
        url_key = _normalize_url(cand.url)
        duration_bucket = (int(cand.duration_seconds // 10)
                           if cand.duration_seconds else None)
        title_key = (_normalize_title(cand.title), _normalize_title(cand.creator),
                     duration_bucket)
        if id_key in seen_ids or (url_key and url_key in seen_urls) or title_key in seen_title_creator:
            removed += 1
            continue
        seen_ids.add(id_key)
        if url_key:
            seen_urls.add(url_key)
        seen_title_creator.add(title_key)
        kept.append(item)
    return kept, removed
