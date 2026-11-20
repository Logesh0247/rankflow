"""Research Agent tests (Prompt 03): topic understanding, scoring,
duplicate detection, providers (real Archive, disabled YouTube,
TEST-ONLY provider), persistence, failure handling, orchestrator
integration and the candidates API.

Live-network tests use the real Internet Archive provider (no key)
and assert on real returned records — never on invented ones.
"""
import pytest

from app.agents.research.providers import (
    InternetArchiveProvider,
    ProviderUnavailable,
    TestProvider,
    YouTubeProvider,
)
from app.agents.research.scoring import ScoredCandidate, deduplicate, relevance_score
from app.agents.research.service import ResearchService
from app.agents.research.topic import understand_topic
from app.core.config import Settings
from app.core.errors import AgentNotImplementedError, ExternalServiceError
from app.models.models import CandidateVideo
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.schemas.schemas import JobCreate
from app.agents.research.providers import RawCandidate

TOPICS = [
    "TOP 5 CRAZIEST PARKOUR FAILS",
    "TOP 5 FUNNIEST CAT VIDEOS",
    "HOW ROCKETS LAND IN SPACE",
]


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 5.0,
            "research_max_candidates": 5}
    base.update(overrides)
    return Settings(**base)


def _new_job(engine, topic=TOPICS[0]):
    return JobService(engine).create_job(JobCreate(topic=topic))


# ---------------------------------------------------------------- topic understanding
@pytest.mark.parametrize("topic,category,keywords", [
    ("TOP 5 CRAZIEST PARKOUR FAILS", "sports_action", ["parkour", "fails"]),
    ("TOP 5 FUNNIEST CAT VIDEOS", "animals", ["cat", "videos"]),
    ("HOW ROCKETS LAND IN SPACE", "science", ["rockets", "land", "space"]),
])
def test_topic_understanding(topic, category, keywords):
    plan = understand_topic(topic)
    assert plan.category == category
    assert plan.keywords == keywords, plan.keywords
    assert plan.queries, "a search strategy must always be generated"
    assert plan.queries[0].kind == "exact"
    assert plan.queries[0].text == " ".join(keywords)
    assert plan.expanded_keywords[:len(keywords)] == keywords
    assert len(plan.expanded_keywords) > len(keywords), "synonyms must expand the query"
    assert plan.content_type in ("ranking_compilation", "tutorial", "documentary", "review", "video")


def test_topic_understanding_parkour_details():
    plan = understand_topic("TOP 5 CRAZIEST PARKOUR FAILS")
    assert "fails" in plan.events
    assert plan.content_type == "ranking_compilation"
    assert "free running" in plan.expanded_keywords
    assert plan.related_concepts, "related concepts must be discovered"
    assert any(q.kind == "expanded" for q in plan.queries)


def test_topic_understanding_empty_topic():
    with pytest.raises(ValueError):
        understand_topic("   ")


# ---------------------------------------------------------------- scoring / dedup
def _raw(**overrides):
    base = dict(platform="internet_archive", external_id="x1", title="Parkour Fails",
                creator="someone", description="Amazing parkour fails compilation",
                url="https://archive.org/details/x1", duration_seconds=120.0,
                view_count=100, published_at=None, licence="https://example.org/cc")
    base.update(overrides)
    return RawCandidate(**base)


def test_relevance_scoring_title_beats_description():
    keywords = ["parkour", "fails"]
    exact = relevance_score(keywords, _raw(title="Parkour Fails"))
    desc_only = relevance_score(keywords, _raw(title="Untitled", description="parkour fails here"))
    unrelated = relevance_score(keywords, _raw(title="Cooking Show", description="recipes"))
    assert exact > desc_only > unrelated
    assert unrelated == 0.0
    assert 0.0 <= exact <= 1.0


def test_deduplicate_removes_same_id_url_and_reupload():
    keywords = ["parkour", "fails"]
    items = [
        ScoredCandidate(_raw(external_id="a", url="https://archive.org/details/a"), keywords),
        ScoredCandidate(_raw(external_id="a", url="https://archive.org/details/a"), keywords),
        ScoredCandidate(_raw(external_id="b", url="https://archive.org/details/a"), keywords),
        ScoredCandidate(_raw(external_id="c", url="https://archive.org/details/c",
                             title="Parkour Fails", creator="someone",
                             duration_seconds=125.0), keywords),
        ScoredCandidate(_raw(external_id="d", url="https://archive.org/details/d",
                             title="Different Title"), keywords),
    ]
    kept, removed = deduplicate(items)
    assert removed == 3
    assert {i.candidate.external_id for i in kept} == {"a", "d"}


# ---------------------------------------------------------------- providers
def test_test_provider_is_deterministic_and_labelled():
    first = TestProvider().search("parkour fails", limit=3)
    second = TestProvider().search("parkour fails", limit=3)
    assert [c.external_id for c in first] == [c.external_id for c in second]
    assert len(first) == 3
    for candidate in first:
        assert "test-only" in (candidate.licence or "")
        assert "example.invalid" in (candidate.url or "")


def test_youtube_provider_without_key_is_unavailable_not_faked():
    with pytest.raises(ProviderUnavailable, match="YOUTUBE_API_KEY"):
        YouTubeProvider(api_key="").search("parkour fails")


def test_internet_archive_live_search_returns_real_records():
    found = InternetArchiveProvider().search("parkour fails", limit=3)
    assert len(found) >= 1
    for candidate in found:
        assert candidate.platform == "internet_archive"
        assert candidate.external_id
        assert candidate.url == f"https://archive.org/details/{candidate.external_id}"
        assert candidate.title


# ---------------------------------------------------------------- service: test provider
def test_service_persists_candidates_and_is_idempotent(engine):
    job = _new_job(engine)
    service = ResearchService(engine, _settings(research_provider="test"))
    report = service.run(job.id)
    assert report.topic == TOPICS[0]
    assert len(report.candidates) == 5
    assert report.metrics["traceable"] == 5
    assert report.metrics["duplicate_rate"] == 0.0
    assert all(c.job_id == job.id for c in report.candidates)
    assert all(c.creator == "test-creator" for c in report.candidates)
    assert report.provider_results[0].provider == "test"
    assert report.provider_results[0].status == "ok"

    with engine.connect() as conn:  # rows are really in the database
        count = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT COUNT(*) FROM candidate_videos WHERE job_id = :j"),
            {"j": job.id}).scalar_one()
    assert count == 5

    second = service.run(job.id)  # re-run must not duplicate rows
    assert len(second.candidates) == 5
    with engine.connect() as conn:
        count2 = conn.execute(
            __import__("sqlalchemy").text(
                "SELECT COUNT(*) FROM candidate_videos WHERE job_id = :j"),
            {"j": job.id}).scalar_one()
    assert count2 == 5


def test_service_unknown_job():
    from app.database.session import make_engine, init_db
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="rankflow-research-"))
    settings = Settings(database_url=f"sqlite:///{tmp / 't.db'}", research_provider="test")
    eng = make_engine(settings)
    init_db(eng)
    with pytest.raises(Exception):
        ResearchService(eng, settings).run("no-such-job")


def test_service_no_candidates_is_a_clear_limitation(engine):
    job = _new_job(engine)

    class EmptyProvider:
        name = "empty"

        def search(self, query, limit=25):
            return []

    service = ResearchService(engine, _settings(research_provider="test"))
    service._provider_chain = lambda: [EmptyProvider()]  # noqa: SLF001
    with pytest.raises(ExternalServiceError, match="no eligible candidates"):
        service.run(job.id)


# ---------------------------------------------------------------- service: live Archive, 3 topics
@pytest.mark.parametrize("topic", TOPICS)
def test_live_archive_three_topics_measured(engine, topic):
    """Live measurement (Prompt 03 testing): relevance, duplicate
    rate, metadata completeness, source traceability, error handling
    for three unrelated topics, against the real provider."""
    job = _new_job(engine, topic)
    service = ResearchService(engine, _settings(research_provider="internet_archive"))
    report = service.run(job.id)
    metrics = report.metrics
    print(f"\n{topic!r}: {metrics} limitations={report.limitations}")
    assert len(report.candidates) >= 1
    assert metrics["duplicate_rate"] == 0.0, "deduplication must remove all duplicates"
    assert metrics["traceable"] == len(report.candidates), "every candidate must trace to its source"
    assert metrics["mean_completeness"] > 0.3, "metadata must be substantially complete"
    assert report.candidates[0].relevance_score > 0, "top candidate must match the topic"
    assert any(r.provider == "internet_archive" and r.status == "ok"
               for r in report.provider_results)
    # No candidate may claim a licence the provider did not publish.
    for candidate in report.candidates:
        if candidate.licence is not None:
            assert candidate.licence.startswith("http")


# ---------------------------------------------------------------- orchestrator integration
def test_orchestrator_runs_real_research_then_rights_then_501_at_video_analysis(engine):
    """Prompt 04 update: rights is now real. With the test provider
    every candidate is test-only, so rights rejects them all and the
    job FAILS honestly — nothing is approved merely to keep
    production moving. Research still completed and checkpointed."""
    job = _new_job(engine)
    settings = _settings(research_provider="test")
    registry = build_default_registry(engine, settings)
    assert registry.get("research").status == "ready"
    assert registry.get("rights").status == "ready"
    runner = WorkflowEngine(engine, settings, registry)
    result = runner.run_job(job.id)
    assert result.status == "failed"
    assert result.error_message is not None
    assert "RIGHTS_VERIFICATION" in result.error_message
    assert "approved 0 of" in result.error_message
    # Research completed and checkpointed; the job failed at rights.
    from app.orchestrator.states import WorkflowStage
    assert WorkflowStage.RESEARCH in JobService(engine).completed_stages(job.id)
    service = ResearchService(engine, settings)
    assert len(service.run(job.id).candidates) == 5  # idempotent re-run


def test_registry_without_engine_still_has_no_executors():
    assert build_default_registry().get("research").status == "not_implemented"


# ---------------------------------------------------------------- API
def test_api_agents_show_research_ready_and_candidates_endpoint():
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        agents = client.get("/agents").json()
        by_id = {a["agent_id"]: a for a in agents}
        assert by_id["research"]["status"] == "ready"
        assert by_id["rights"]["status"] == "ready"
        assert client.get("/agents/research/health").json()["status"] == "ready"

        created = client.post("/jobs", json={"topic": TOPICS[0]})
        job_id = created.json()["id"]
        candidates = client.get(f"/jobs/{job_id}/candidates")
        assert candidates.status_code == 200
        assert candidates.json() == []
