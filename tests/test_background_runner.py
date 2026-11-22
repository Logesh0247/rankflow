"""Background job runner tests (2026-11-21 Render health-check fix).

Live incident: POST /jobs/{id}/run ran the whole job inside the
HTTP request, Render's 5 s health check on /health timed out
while the job ran, Render restarted the service mid-job, and
the job was left orphaned "running" — never failed, never
resumed. Since the fix, /run and /resume return 202
immediately and the job runs in a background thread; /health
answers from the request path; and startup fails any job left
"running" cleanly so it can be resumed from its checkpoints.
"""
import threading
import time

import pytest
from sqlalchemy.orm import sessionmaker

from app.database.session import get_engine
from app.orchestrator.jobs import JobService
from app.orchestrator.runner import RUNNER
from app.schemas.schemas import JobCreate


def _wait_for_terminal(client, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        got = client.get(f"/jobs/{job_id}")
        assert got.status_code == 200, got.text
        last = got.json()
        if last["status"] in ("awaiting_approval", "approved",
                              "failed", "cancelled"):
            return last
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s: {last}")


def _set_status(engine, job_id: str, status: str, stage: str = "VIDEO_ANALYSIS"):
    from app.models.models import Job

    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        job = session.query(Job).filter(Job.id == job_id).first()
        job.status = status
        job.current_stage = stage
        session.commit()


def test_run_returns_202_and_job_completes_in_background(engine):
    """POST /run answers 202 at once; the job still reaches its
    terminal state, observable by polling GET /jobs/{id}."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        created = client.post("/jobs", json={"topic": "TOP 5 FUNNIEST CAT VIDEOS"})
        job_id = created.json()["id"]
        run = client.post(f"/jobs/{job_id}/run?use_test_doubles=true")
        assert run.status_code == 202, run.text
        finished = _wait_for_terminal(client, job_id)
        assert finished["status"] == "awaiting_approval"


def test_resume_returns_202_and_job_completes_in_background(engine):
    """POST /resume on a created job also starts in the
    background and completes."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        created = client.post("/jobs", json={"topic": "TOP 5 FUNNIEST CAT VIDEOS"})
        job_id = created.json()["id"]
        resumed = client.post(f"/jobs/{job_id}/resume?use_test_doubles=true")
        assert resumed.status_code == 202, resumed.text
        finished = _wait_for_terminal(client, job_id)
        assert finished["status"] == "awaiting_approval"


def test_health_answers_while_a_job_runs_in_background(engine):
    """While a background job is running, GET /health answers
    fast (200) — the exact call Render's health check makes,
    and the one that used to time out after 5 s."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        created = client.post("/jobs", json={"topic": "slow job"})
        job_id = created.json()["id"]
        started = threading.Event()

        def _slow_target():
            started.set()
            time.sleep(3.0)

        assert RUNNER.start(job_id, _slow_target,
                            lambda message: None) is True
        try:
            assert started.wait(timeout=5), "background job never started"
            assert RUNNER.is_running(job_id)
            begun = time.monotonic()
            health = client.get("/health")
            elapsed = time.monotonic() - begun
            assert health.status_code == 200, health.text
            assert elapsed < 2.0, f"/health took {elapsed:.1f}s during a job"
        finally:
            # The slow target finishes on its own; make sure the
            # runner slot is free before the test ends.
            deadline = time.monotonic() + 10
            while RUNNER.is_running(job_id) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not RUNNER.is_running(job_id)


def test_second_run_for_running_job_is_409(engine):
    """A second POST /run for a job that is already running is
    refused with 409 instead of running the same job twice."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        created = client.post("/jobs", json={"topic": "double run"})
        job_id = created.json()["id"]
        started = threading.Event()

        def _slow_target():
            started.set()
            time.sleep(2.0)

        assert RUNNER.start(job_id, _slow_target,
                            lambda message: None) is True
        try:
            assert started.wait(timeout=5)
            again = client.post(f"/jobs/{job_id}/run")
            assert again.status_code == 409, again.text
        finally:
            deadline = time.monotonic() + 10
            while RUNNER.is_running(job_id) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not RUNNER.is_running(job_id)


def test_fail_interrupted_jobs(engine):
    """A job left "running" is failed cleanly with the cause;
    a merely "created" job is untouched."""
    service = JobService(engine)
    stuck = service.create_job(JobCreate(topic="stuck job"))
    fresh = service.create_job(JobCreate(topic="fresh job"))
    _set_status(engine, stuck.id, "running")
    failed = service.fail_interrupted_jobs(
        "interrupted by a service restart — resume to continue "
        "from the last completed stage")
    assert failed == 1
    assert service.get_job(stuck.id).status == "failed"
    assert "interrupted by a service restart" in (
        service.get_job(stuck.id).error_message or "")
    assert service.get_job(fresh.id).status == "created"


def test_startup_fails_interrupted_jobs(engine):
    """Entering the app (lifespan) fails a job left "running"
    by a restart, so it shows FAILED with a resume hint
    instead of sitting "running" forever."""
    from fastapi.testclient import TestClient

    from main import app

    service = JobService(engine)
    job = service.create_job(JobCreate(topic="orphaned by restart"))
    _set_status(engine, job.id, "running")
    with TestClient(app) as client:
        got = client.get(f"/jobs/{job.id}")
        assert got.status_code == 200
        assert got.json()["status"] == "failed"
        assert "interrupted by a service restart" in (
            got.json()["error_message"] or "")


def test_get_engine_is_shared_per_url(engine, tmp_path):
    """The request-path engine is cached per database URL (the
    health-check fix) — repeated calls return the same engine,
    a different URL gets a different one."""
    from app.core.config import Settings

    first = get_engine()
    assert get_engine() is first
    other = get_engine(Settings(
        database_url=f"sqlite:///{tmp_path / 'other.db'}",
        storage_root=str(tmp_path / "storage")))
    assert other is not first
    assert get_engine(Settings(
        database_url=f"sqlite:///{tmp_path / 'other.db'}",
        storage_root=str(tmp_path / "storage"))) is other
