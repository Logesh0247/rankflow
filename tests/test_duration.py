"""Duration preference tests (2026-11-21).

A Short is built from short clips: videos under 10 minutes get a
score boost, videos over the configured cap (default 15 minutes)
are excluded, and videos with unknown duration are kept (never
excluded, just not boosted). The rule lives in the shared scoring
path, so it covers Archive and YouTube alike — and it is
re-applied after Archive duration enrichment, because a movie's
length is often only known then. Live proof that motivated it:
the cat job's only approved candidate was "Hard Boiled & The
Killer [Director's Cut]", a ~2-hour movie.
"""
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.research.providers import InternetArchiveProvider, RawCandidate
from app.agents.research.scoring import (
    DEFAULT_MAX_CANDIDATE_MINUTES,
    SHORT_VIDEO_SECONDS,
    ScoredCandidate,
    duration_factor,
)
from app.agents.research.service import ResearchService
from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.models.models import CandidateVideo
from app.orchestrator.jobs import JobService
from app.schemas.schemas import JobCreate


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 5.0,
            "research_max_candidates": 5}
    base.update(overrides)
    return Settings(**base)


def _new_job(engine, topic="TOP 5 FUNNIEST CAT VIDEOS"):
    return JobService(engine).create_job(JobCreate(topic=topic))


class _StubProvider:
    """Deterministic in-memory provider for duration tests."""

    def __init__(self, name, candidates):
        self.name = name
        self._candidates = candidates

    def search(self, query, limit=25):
        return list(self._candidates)


def _raw(platform, external_id, *, title="Funny Cat Videos",
         duration=None, licence=None):
    return RawCandidate(
        platform=platform, external_id=external_id, title=title,
        creator="CatChannel", description="funny cats everywhere",
        url=(f"https://www.youtube.com/watch?v={external_id}"
             if platform == "youtube"
             else f"https://archive.org/details/{external_id}"),
        duration_seconds=duration, view_count=100,
        published_at=None, licence=licence)


# ------------------------------------------------------------------ factor
def test_duration_factor_unknown_is_neutral():
    assert duration_factor(None) == 1.0


def test_duration_factor_short_is_boosted():
    assert duration_factor(60.0) == 1.15
    assert duration_factor(SHORT_VIDEO_SECONDS) == 1.15


def test_duration_factor_between_boost_and_cap_is_neutral():
    assert duration_factor(SHORT_VIDEO_SECONDS + 1) == 1.0
    assert duration_factor(DEFAULT_MAX_CANDIDATE_MINUTES * 60.0) == 1.0


def test_duration_factor_over_cap_is_zero():
    assert duration_factor(DEFAULT_MAX_CANDIDATE_MINUTES * 60.0 + 1) == 0.0
    assert duration_factor(7200.0) == 0.0  # the ~2-hour movie


def test_duration_factor_cap_is_configurable():
    assert duration_factor(301.0, max_candidate_minutes=5.0) == 0.0
    # Under the 5-minute cap and under 10 minutes: still boosted.
    assert duration_factor(299.0, max_candidate_minutes=5.0) == 1.15


# ------------------------------------------------------------------ scoring
def test_two_hour_candidate_scores_below_sixty_second_candidate():
    """Same topic, same title relevance — the movie must lose to the
    short clip, not just rank below it: its combined score is 0."""
    keywords = ["cat", "videos"]
    movie = ScoredCandidate(_raw("youtube", "movie", duration=7200.0), keywords)
    short = ScoredCandidate(_raw("youtube", "short", duration=60.0), keywords)
    assert movie.combined == 0.0
    assert short.combined > movie.combined
    assert short.duration_factor == 1.15


def test_unknown_duration_candidate_is_kept_not_boosted():
    keywords = ["cat", "videos"]
    item = ScoredCandidate(_raw("internet_archive", "mystery", duration=None),
                           keywords)
    assert item.duration_factor == 1.0
    assert item.combined > 0.0


def test_rescore_duration_after_enrichment_excludes_movie():
    """An Archive candidate whose duration is filled in later (None
    -> 7200 s) is excluded once the length is known."""
    keywords = ["cat", "videos"]
    item = ScoredCandidate(_raw("internet_archive", "movie", duration=None),
                           keywords)
    assert item.duration_factor == 1.0
    item.candidate.duration_seconds = 7200.0
    item.rescore_duration(DEFAULT_MAX_CANDIDATE_MINUTES)
    assert item.duration_factor == 0.0
    assert item.combined == 0.0


# ------------------------------------------------------------------ service
def test_service_excludes_movie_and_keeps_short_clip(engine):
    """The live case: the movie has the better title match, so it
    would rank first without the cap — with the cap it is excluded
    and the 60-second clip is the kept candidate."""
    job = _new_job(engine)
    stub = _StubProvider("youtube", [
        _raw("youtube", "movie", title="Funny Cat Videos", duration=7200.0),
        _raw("youtube", "short", title="Funny Cat Fails", duration=60.0),
    ])
    service = ResearchService(engine, _settings())
    service._provider_chain = lambda: [stub]  # noqa: SLF001
    report = service.run(job.id)
    kept_ids = {c.external_video_id for c in report.candidates}
    assert kept_ids == {"short"}
    assert report.metrics["duration_excluded"] == 1
    assert any("duration cap" in lim for lim in report.limitations)


def test_service_keeps_unknown_duration_candidates(engine):
    job = _new_job(engine)
    stub = _StubProvider("internet_archive", [
        _raw("internet_archive", "mystery", duration=None),
        _raw("internet_archive", "short", duration=90.0),
    ])
    # Enrichment disabled: the point here is the scoring/first
    # filter — an unknown duration is kept, never excluded.
    service = ResearchService(engine, _settings(research_enrich_duration=False))
    service._provider_chain = lambda: [stub]  # noqa: SLF001
    report = service.run(job.id)
    kept_ids = {c.external_video_id for c in report.candidates}
    assert kept_ids == {"mystery", "short"}
    assert report.metrics["duration_excluded"] == 0


def test_service_excludes_movie_after_enrichment_with_backfill(engine, monkeypatch):
    """Archive movie with unknown duration at search time: enrichment
    reveals 7200 s, so it is excluded — and the short replacement
    from the same run is still kept (backfill, not an empty result)."""
    job = _new_job(engine)
    stub = _StubProvider("internet_archive", [
        _raw("internet_archive", "movie", title="Funny Cat Videos", duration=None),
        _raw("internet_archive", "short", title="Funny Cat Fails", duration=60.0),
        _raw("internet_archive", "short-2", title="Cats Being Cats",
             duration=45.0),
    ])
    service = ResearchService(engine, _settings())
    service._provider_chain = lambda: [stub]  # noqa: SLF001
    monkeypatch.setattr(
        InternetArchiveProvider, "enrich_duration",
        lambda self, external_id: 7200.0 if external_id == "movie" else None)
    report = service.run(job.id)
    kept_ids = {c.external_video_id for c in report.candidates}
    assert "movie" not in kept_ids
    assert {"short", "short-2"} <= kept_ids
    assert report.metrics["duration_excluded"] == 1


def test_service_all_excluded_is_a_clear_error(engine):
    job = _new_job(engine)
    stub = _StubProvider("youtube", [
        _raw("youtube", "movie-1", duration=3600.0),
        _raw("youtube", "movie-2", title="Cats Being Cats", duration=5400.0),
    ])
    service = ResearchService(engine, _settings())
    service._provider_chain = lambda: [stub]  # noqa: SLF001
    with pytest.raises(ExternalServiceError,
                       match="duration cap"):
        service.run(job.id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        count = (session.query(CandidateVideo)
                 .filter(CandidateVideo.job_id == job.id).count())
    assert count == 0, "an excluded run must persist no candidates"


def test_service_respects_configured_cap(engine):
    """RESEARCH_MAX_CANDIDATE_MINUTES=5: a 10-minute video is out,
    a 4-minute video stays."""
    job = _new_job(engine)
    stub = _StubProvider("youtube", [
        _raw("youtube", "ten-min", duration=600.0),
        _raw("youtube", "four-min", title="Cats Being Cats", duration=240.0),
    ])
    service = ResearchService(
        engine, _settings(research_max_candidate_minutes=5))
    service._provider_chain = lambda: [stub]  # noqa: SLF001
    report = service.run(job.id)
    kept_ids = {c.external_video_id for c in report.candidates}
    assert kept_ids == {"four-min"}
    assert report.metrics["duration_excluded"] == 1
