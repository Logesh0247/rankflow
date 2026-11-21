"""User Interface tests (Prompt 11).

Covers the backend surface the UI depends on, served by the real
FastAPI app on a temp SQLite DB:
* GET /ui/ serves the Creator Studio page (all five pages present);
* GET /settings/status reports config status and never leaks a
  secret or API key value;
* JobRead exposes current_stage (the dashboard progress bar reads it);
* POST /jobs/{id}/approve approves only awaiting_approval jobs;
* POST /jobs/{id}/reject refuses non-awaiting jobs, and on a real
  completed job it re-renders from the approved EDL and returns the
  job to awaiting_approval — without consuming a QA repair round.
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.models.models import Job  # noqa: E402
from app.orchestrator.engine import WorkflowEngine  # noqa: E402
from app.orchestrator.jobs import JobService  # noqa: E402
from app.orchestrator.registry import build_default_registry  # noqa: E402
from app.schemas.schemas import JobCreate  # noqa: E402
from tests.test_integration import _run_real, _seed_five_sources, _settings  # noqa: E402


def _client():
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


def _set_status(engine, job_id, status, stage="COMPLETED"):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        job = session.query(Job).filter(Job.id == job_id).first()
        job.status = status
        job.current_stage = stage
        session.commit()


def test_ui_serves_creator_studio_page(engine):
    with _client() as client:
        r = client.get("/ui/")
        assert r.status_code == 200
        html = r.text
        assert "MOMENTRIX" in html
        assert "AI-powered creative production" in html
        assert "Generate video" in html
        for label in ("Overview", "Projects", "Campaigns", "Content Library",
                      "Templates", "Brand Kit", "Avatars", "Workflows",
                      "Analytics", "Team", "Settings"):
            assert label in html
        for step in ("BRIEF & PLAN", "CREATE & GENERATE", "REVIEW & APPROVE",
                     "PUBLISH & DISTRIBUTE", "MEASURE & OPTIMISE"):
            assert step in html
        # The real demo poster frame is served with the UI.
        poster = client.get("/ui/poster.jpg")
        assert poster.status_code == 200
        assert len(poster.content) > 10_000


def test_settings_status_never_leaks_secrets(engine):
    with _client() as client:
        r = client.get("/settings/status")
        assert r.status_code == 200
        body = r.json()
        # Edge TTS is configured by default (free, no API key needed).
        assert body["tts_configured"] is True
        assert body["tts_provider"] == "edge-tts"
        assert body["tts_voice"] == "en-US-GuyNeural"
        assert body["tts_enabled"] is True
        assert body["youtube_configured"] is False
        assert body["music_library_configured"] is False
        assert body["video_output"]["width"] == 1080
        assert body["video_output"]["height"] == 1920
        assert len(body["agents"]) == 7
        assert all(a["status"] == "ready" for a in body["agents"])
        text = r.text.lower()
        for forbidden in ("api_key", "secret", "youtube_api", "tts_api"):
            assert forbidden not in text


def test_job_read_exposes_current_stage(engine):
    with _client() as client:
        created = client.post("/jobs", json={"topic": "UI stage test"})
        assert created.status_code == 201
        assert created.json()["current_stage"] == "INITIALIZED"
        listed = client.get("/jobs")
        assert listed.status_code == 200
        assert listed.json()[0]["current_stage"] == "INITIALIZED"


def test_approve_only_from_awaiting_approval(engine):
    job = JobService(engine).create_job(JobCreate(topic="approve test"))
    with _client() as client:
        assert client.post(f"/jobs/{job.id}/approve").status_code == 422
        _set_status(engine, job.id, "awaiting_approval")
        ok = client.post(f"/jobs/{job.id}/approve")
        assert ok.status_code == 200
        assert ok.json()["status"] == "approved"
        assert client.post(f"/jobs/{job.id}/approve").status_code == 422
        assert client.post("/jobs/no-such-job/approve").status_code == 404


def test_reject_refuses_non_awaiting_job(engine):
    job = JobService(engine).create_job(JobCreate(topic="reject test"))
    with _client() as client:
        assert client.post(f"/jobs/{job.id}/reject").status_code == 422
        assert client.post("/jobs/no-such-job/reject").status_code == 404


def test_reject_regenerates_real_completed_job(engine, tmp_path):
    """Full pipeline, then Reject & Regenerate through the real
    engine: the job returns to awaiting_approval with a fresh
    render, and the rejection did not consume a QA repair round."""
    job = JobService(engine).create_job(JobCreate(topic="TOP 5 CRAZIEST PARKOUR FAILS"))
    _seed_five_sources(engine, job, tmp_path)
    finished = _run_real(engine, job.id)
    assert finished.status == "awaiting_approval", finished.error_message
    service = JobService(engine)
    assert service.repair_rounds_used(job.id) == 0
    engine_obj = WorkflowEngine(engine, _settings(), build_default_registry(engine, _settings()))
    regenerated = engine_obj.regenerate_job(job.id)
    assert regenerated.status == "awaiting_approval", regenerated.error_message
    assert regenerated.current_stage == "COMPLETED"
    # A user rejection is not a QA repair round.
    assert service.repair_rounds_used(job.id) == 0
    # The new render exists and is a real 1080x1920 MP4.
    from app.agents.production.service import ProductionService

    rendered = ProductionService(engine, _settings()).get_rendered_video(job.id)
    assert rendered.status == "ready"
    assert rendered.width == 1080 and rendered.height == 1920
    assert Path(rendered.file_path).is_file()
