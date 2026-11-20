"""Orchestrator tests (Prompt 02): registry, job management, workflow
ordering, handoffs, validation, timeouts, retries, recovery and
concurrent-job isolation. Real agents are NOT tested here — only
the orchestrator, driven by TEST-ONLY test doubles."""
import tempfile
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import Settings
from app.core.errors import AgentNotImplementedError, ValidationError
from app.orchestrator.contracts import OrchestratorTask, RankingOutput
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.orchestrator.states import AGENT_STAGES, WorkflowStage, dependencies_satisfied
from app.orchestrator.test_doubles import (
    AGENT_IDS,
    DoubleConfig,
    build_test_registry,
    make_double,
    register_test_doubles,
)
from app.schemas.schemas import JobCreate

TOPIC = "TOP 5 CRAZIEST PARKOUR FAILS"


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01, "orchestrator_timeout_seconds": 5.0}
    base.update(overrides)
    return Settings(**base)


def _engine(engine, registry=None, **overrides) -> WorkflowEngine:
    return WorkflowEngine(engine, _settings(**overrides), registry or build_test_registry())


def _new_job(engine, topic=TOPIC):
    return JobService(engine).create_job(JobCreate(topic=topic))


def _succeeded_stages(engine, job_id: str) -> list[str]:
    return [e.stage for e in JobService(engine).job_history(job_id)
            if e.event == "agent_succeeded"]


# ---------------------------------------------------------------- registry
def test_registry_lists_all_seven_agents():
    agents = build_default_registry().list()
    assert [a.agent_id for a in agents] == AGENT_IDS
    for a in agents:
        assert a.description and a.input_schema and a.output_schema
        assert a.execution_method and a.status == "not_implemented"
        assert a.health.status == "not_implemented"


def test_registry_real_agent_has_no_executor():
    registry = build_default_registry()
    with pytest.raises(AgentNotImplementedError):
        registry.executor_for("research")


def test_registry_health_unknown_agent():
    with pytest.raises(Exception):
        build_default_registry().health("does_not_exist")


# ---------------------------------------------------------------- job management
def test_job_create_get_history_cancel(engine):
    service = JobService(engine)
    job = service.create_job(JobCreate(topic=TOPIC))
    assert job.status == "created"
    assert service.get_job(job.id).topic == TOPIC
    history = service.job_history(job.id)
    assert history[0].event == "job_created"
    cancelled = service.cancel_job(job.id)
    assert cancelled.status == "cancelled"
    with pytest.raises(ValidationError):
        service.cancel_job(job.id)  # already terminal
    assert service.is_cancelled(job.id) is True


def test_job_unknown_id(engine):
    with pytest.raises(Exception):
        JobService(engine).get_job("no-such-job")


# ---------------------------------------------------------------- workflow states
def test_workflow_stage_order_and_dependencies():
    assert [s.value for s in AGENT_STAGES] == [
        "RESEARCH", "RIGHTS_VERIFICATION", "VIDEO_ANALYSIS", "RANKING",
        "CREATIVE_PLANNING", "VIDEO_PRODUCTION", "QUALITY_ASSURANCE"]
    assert dependencies_satisfied(WorkflowStage.RESEARCH, set()) is True
    assert dependencies_satisfied(WorkflowStage.RANKING, {WorkflowStage.RESEARCH}) is False
    done = {WorkflowStage.RESEARCH, WorkflowStage.RIGHTS_VERIFICATION,
            WorkflowStage.VIDEO_ANALYSIS}
    assert dependencies_satisfied(WorkflowStage.RANKING, done) is True


def test_contracts_task_requires_retry_attempt_match():
    with pytest.raises(PydanticValidationError):
        OrchestratorTask(task_id="t", job_id="j", agent_id="research",
                         expected_output_schema="ResearchOutput",
                         deadline="2030-01-01T00:00:00Z", retry_count=2, attempt=1)


def test_contracts_ranking_requires_five_unique_sources():
    good = {"ranks": [{"rank": i + 1, "moment_id": f"m{i}", "candidate_video_id": f"c{i}",
                       "score": 0.9} for i in range(5)]}
    assert len(RankingOutput.model_validate(good).ranks) == 5
    bad = {"ranks": [{"rank": i + 1, "moment_id": f"m{i}", "candidate_video_id": "same",
                      "score": 0.9} for i in range(5)]}
    with pytest.raises(PydanticValidationError):
        RankingOutput.model_validate(bad)


# ---------------------------------------------------------------- full simulated run
def test_full_simulated_job_visits_every_stage(engine):
    job = _new_job(engine)
    result = _engine(engine).run_job(job.id)
    assert result.status == "awaiting_approval"  # COMPLETED -> awaiting_approval
    assert _succeeded_stages(engine, job.id) == AGENT_IDS
    history = JobService(engine).job_history(job.id)
    assert history[0].event == "job_created"
    assert len([e for e in history if e.event == "agent_succeeded"]) == 7


def test_run_job_twice_does_not_re_execute(engine):
    job = _new_job(engine)
    calls: dict[str, int] = {}

    def counting(task):
        calls[task["agent_id"]] = calls.get(task["agent_id"], 0) + 1
        return make_double(task["agent_id"])(task)

    registry = build_default_registry()
    for agent_id in AGENT_IDS:
        registry.register_executor(agent_id, counting, test_double=True)
    runner = _engine(engine, registry)
    runner.run_job(job.id)
    first = dict(calls)
    assert runner.run_job(job.id).status == "awaiting_approval"
    assert calls == first, "second run must not re-execute any stage"


def test_run_without_agents_returns_501_and_leaves_job(engine):
    job = _new_job(engine)
    with pytest.raises(AgentNotImplementedError):
        WorkflowEngine(engine, _settings(), build_default_registry()).run_job(job.id)
    # The job was not marked failed by the missing agent.
    assert JobService(engine).get_job(job.id).status == "created"


# ---------------------------------------------------------------- handoffs
def test_data_handoffs_between_stages(engine):
    job = _new_job(engine)
    seen: dict[str, dict] = {}

    def recording(task):
        seen[task["agent_id"]] = {"input_references": task["input_references"],
                                  "input": task["input"]}
        return make_double(task["agent_id"])(task)

    registry = build_default_registry()
    for agent_id in AGENT_IDS:
        registry.register_executor(agent_id, recording, test_double=True)
    assert _engine(engine, registry).run_job(job.id).status == "awaiting_approval"
    # Ranking received the moments video analysis produced.
    ranking_refs = seen["ranking"]["input_references"]["moment_ids"]
    assert ranking_refs == [f"{job.id}-moment-{i}" for i in range(1, 6)]
    # Creative planning received the ranking's moment ids.
    assert seen["creative_director"]["input_references"]["ranked_moment_ids"] == ranking_refs
    # QA received the production stage's rendered video id.
    assert seen["quality_assurance"]["input_references"]["rendered_video_id"] == f"{job.id}-render"


# ---------------------------------------------------------------- retries / validation / timeout
def test_retry_after_one_failure_then_succeeds(engine):
    job = _new_job(engine)
    registry = build_test_registry({"ranking": DoubleConfig(fail_attempts={1})})
    assert _engine(engine, registry).run_job(job.id).status == "awaiting_approval"
    failed = [e for e in JobService(engine).job_history(job.id) if e.event == "agent_failed"]
    assert len(failed) == 1 and "ranking" in (failed[0].stage or "")


def test_invalid_output_rejected_then_retried(engine):
    job = _new_job(engine)
    registry = build_test_registry({"ranking": DoubleConfig(invalid_attempts={1})})
    assert _engine(engine, registry).run_job(job.id).status == "awaiting_approval"
    failed = [e for e in JobService(engine).job_history(job.id) if e.event == "agent_failed"]
    assert failed, "invalid ranking output must be logged as a failure, not accepted"


def test_timeout_marks_job_failed_and_is_not_silent(engine):
    job = _new_job(engine)
    registry = build_test_registry({"research": DoubleConfig(slow_seconds=1.0)})
    result = _engine(engine, registry, orchestrator_timeout_seconds=0.1).run_job(job.id)
    assert result.status == "failed"
    assert result.error_message and "RESEARCH" in result.error_message
    assert "timeout" in result.error_message.lower()
    failed = [e for e in JobService(engine).job_history(job.id) if e.event == "agent_failed"]
    assert len(failed) == 3  # max_attempts = 3


# ---------------------------------------------------------------- recovery
def test_failure_recovery_resumes_from_failed_stage(engine):
    job = _new_job(engine)
    calls: dict[str, int] = {}

    def counting(task):
        calls[task["agent_id"]] = calls.get(task["agent_id"], 0) + 1
        return make_double(task["agent_id"])(task)

    failing = build_test_registry({"ranking": DoubleConfig(fail_attempts={1, 2, 3})})
    failed = _engine(engine, failing).run_job(job.id)
    assert failed.status == "failed"
    assert "RANKING" in (failed.error_message or "")
    assert _succeeded_stages(engine, job.id) == ["research", "rights", "video_intelligence"]

    good = build_default_registry()
    for agent_id in AGENT_IDS:
        good.register_executor(agent_id, counting, test_double=True)
    resumed = _engine(engine, good).resume_job(job.id)
    assert resumed.status == "awaiting_approval"
    # Resume did NOT re-run the three completed stages.
    assert calls == {"ranking": 1, "creative_director": 1, "production": 1,
                     "quality_assurance": 1}
    assert _succeeded_stages(engine, job.id) == AGENT_IDS


def test_resume_completed_job_rejected(engine):
    job = _new_job(engine)
    _engine(engine).run_job(job.id)
    with pytest.raises(ValidationError):
        _engine(engine).resume_job(job.id)


# ---------------------------------------------------------------- concurrency
def test_concurrent_jobs_are_isolated():
    results: dict[str, str] = {}

    def run_one(topic: str):
        tmp = Path(tempfile.mkdtemp(prefix="rankflow-conc-"))
        from app.database.session import init_db, make_engine
        settings = Settings(database_url=f"sqlite:///{tmp / 't.db'}",
                            orchestrator_backoff_base_seconds=0.01)
        eng = make_engine(settings)
        init_db(eng)
        job = JobService(eng).create_job(JobCreate(topic=topic))
        out = WorkflowEngine(eng, settings, build_test_registry()).run_job(job.id)
        results[topic] = out.status
        # Outputs are namespaced by this job's id, not the other job's.
        stages = _succeeded_stages(eng, job.id)
        assert stages == AGENT_IDS

    threads = [threading.Thread(target=run_one, args=(f"topic {i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert results == {"topic 0": "awaiting_approval", "topic 1": "awaiting_approval"}


# ---------------------------------------------------------------- API
def test_api_agents_and_run_with_test_doubles(engine):
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker

    from main import app
    from app.models.models import CandidateVideo, Source

    with TestClient(app) as client:
        agents = client.get("/agents")
        assert agents.status_code == 200
        assert [a["agent_id"] for a in agents.json()] == AGENT_IDS

        health = client.get("/agents/research/health")
        assert health.status_code == 200
        # All seven real agents are registered and ready (Prompts
        # 03-09, integrated in Prompt 10); none is a test double
        # in the default registry.
        assert health.json()["status"] == "ready"
        assert client.get("/agents/rights/health").json()["status"] == "ready"
        assert client.get("/agents/video_intelligence/health").json()["status"] == "ready"
        assert client.get("/agents/ranking/health").json()["status"] == "ready"
        assert client.get("/agents/creative_director/health").json()["status"] == "ready"
        assert client.get("/agents/production/health").json()["status"] == "ready"
        assert client.get("/agents/quality_assurance/health").json()["status"] == "ready"

        created = client.post("/jobs", json={"topic": TOPIC})
        job_id = created.json()["id"]

        # Seed one CC BY candidate so the real rights stage has
        # something it can honestly approve — otherwise the plain
        # run's outcome would depend on what live search returns.
        sessions = sessionmaker(bind=engine)
        with sessions() as session:
            source = Source(platform="internet_archive", external_id="seed-cc-clip",
                            title="Seed CC Clip",
                            url="https://archive.org/details/seed-cc-clip")
            session.add(source)
            session.flush()
            session.add(CandidateVideo(
                job_id=job_id, source_id=source.id, external_video_id="seed-cc-clip",
                title="Seed CC Clip", url="https://archive.org/details/seed-cc-clip",
                licence="https://creativecommons.org/licenses/by/4.0/"))
            session.commit()

        # Plain run: real research + real rights + real video
        # intelligence. The run fails the job honestly because no
        # approved candidate has a local footage file (downloading
        # is not configured) — the video agent runs and reports
        # that, rather than stopping with 501.
        plain = client.post(f"/jobs/{job_id}/run")
        assert plain.status_code == 200, plain.text
        assert plain.json()["status"] == "failed"
        assert "VIDEO_ANALYSIS" in (plain.json()["error_message"] or "")

        # A failed job cannot be re-run, so the test-double run uses
        # a fresh job.
        created_td = client.post("/jobs", json={"topic": TOPIC})
        td_job_id = created_td.json()["id"]
        run = client.post(f"/jobs/{td_job_id}/run?use_test_doubles=true")
        assert run.status_code == 200, run.text
        assert run.json()["status"] == "awaiting_approval"

        history = client.get(f"/jobs/{td_job_id}/history")
        assert history.status_code == 200
        assert len(history.json()) >= 8  # created + 7 succeeded

        created2 = client.post("/jobs", json={"topic": "cancel me"})
        job2 = created2.json()["id"]
        cancelled = client.post(f"/jobs/{job2}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert client.post(f"/jobs/{job2}/cancel").status_code == 422
