"""Complete System Integration tests (Prompt 10).

End-to-end proof that the seven real agents work as one autonomous
workflow under the orchestrator — not as seven separately tested
parts. Uses real agents throughout (research runs on the TEST-ONLY
provider so no live search is needed; the five sources are seeded
CC-licensed clips with local footage, exactly as the QA
orchestrator test does).

Covered here:
* all seven agents are real, ready, and wired — no test stubs in
  the default registry, output schemas cover every stage;
* a topic-only job runs end to end with no manual selection;
* a deterministic QA failure is attempted exactly once (no blind
  3x retry) and the repair loop heals it on resume;
* an insufficient-candidates failure recovers after human
  intervention (more approved sources) via resume from a named
  stage;
* resume refuses once the QA repair rounds are exhausted.
"""
import os
import subprocess

import cv2
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.production.service import ProductionService
from app.agents.quality_assurance.service import QAService
from app.agents.quality_assurance.technical import validate_file
from app.core.config import Settings
from app.core.errors import AgentNotImplementedError, ValidationError
from app.models.models import ApprovedFootage, CandidateVideo, Source
from app.orchestrator.contracts import STAGE_OUTPUT_SCHEMAS
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import AGENT_SPECS, build_default_registry
from app.orchestrator.states import AGENT_STAGES, WorkflowStage
from app.schemas.schemas import JobCreate

FPS, W, H = 15, 160, 120
TOPIC = "TOP 5 CRAZIEST PARKOUR FAILS"


def _settings(**overrides) -> Settings:
    # TTS is disabled by default in these tests: the no-voice path
    # must stay covered, and a live Edge TTS attempt would make
    # verdicts depend on whether speech.platform.bing.com is
    # reachable from the machine running the suite (it is blocked
    # on this VM). The generated-voice path is covered by the
    # stubbed-narration test in test_production.py.
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 300.0,
            "rights_fetch_ia_metadata": False,
            "research_provider": "test",
            "tts_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _new_job(engine, topic=TOPIC):
    return JobService(engine).create_job(JobCreate(topic=topic))


def _licence_url() -> str:
    return "https://creativecommons.org/licenses/by/4.0/"


def _seed_candidate(engine, job_id, external_id, *, title, creator):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        source = (session.query(Source)
                  .filter(Source.platform == "internet_archive",
                          Source.external_id == external_id).first())
        if source is None:
            source = Source(platform="internet_archive", external_id=external_id,
                            title=title, url=f"https://archive.org/details/{external_id}")
            session.add(source)
            session.flush()
        candidate = CandidateVideo(
            job_id=job_id, source_id=source.id, external_video_id=external_id,
            title=title, url=f"https://archive.org/details/{external_id}",
            duration_seconds=30.0, creator=creator, licence=_licence_url())
        session.add(candidate)
        session.commit()
        return candidate.id


def _pingpong(frame_index: int, speed: int, span: int) -> int:
    p = (frame_index * speed) % (2 * span)
    return p if p <= span else 2 * span - p


def _write_footage(path, speed: int, colour, duration: float = 20.0):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for i in range(int(duration * FPS)):
        frame = np.full((H, W, 3), colour, dtype=np.uint8)
        x = _pingpong(i, speed, W - 50)
        cv2.rectangle(frame, (x, H // 2 - 25), (x + 50, H // 2 + 25), (0, 0, 0), -1)
        writer.write(frame)
    writer.release()
    return path


def _seed_five_sources(engine, job, tmp_path):
    """Five CC BY candidates with local footage — the minimum
    honest input for a full end-to-end run."""
    colours = [(40, 160, 40), (40, 40, 180), (40, 200, 220), (120, 40, 20), (140, 140, 140)]
    sessions = sessionmaker(bind=engine)
    for i, (speed, colour) in enumerate(zip((7, 6, 5, 4, 3), colours), start=1):
        candidate_id = _seed_candidate(
            engine, job.id, f"cc-clip-{i}", title=f"Parkour Fails {i}",
            creator=f"Creator {i}")
        path = _write_footage(tmp_path / f"clip-{i}.mp4", speed, colour)
        with sessions() as session:
            session.add(ApprovedFootage(job_id=job.id, candidate_video_id=candidate_id,
                                        storage_path=str(path)))
            session.commit()


def _run_real(engine, job_id, **overrides):
    settings = _settings(**overrides)
    runner = WorkflowEngine(engine, settings, build_default_registry(engine, settings))
    return runner.run_job(job_id)


def _qa_failed_logs(engine, job_id):
    return [e for e in JobService(engine).job_history(job_id)
            if e.event == "agent_failed" and e.stage == "quality_assurance"]


def _succeeded(engine, job_id, agent):
    return [e for e in JobService(engine).job_history(job_id)
            if e.event == "agent_succeeded" and e.stage == agent]


# ------------------------------------------------------------------ Phase 1: compatibility
def test_all_seven_agents_are_real_and_ready(engine):
    """No test stubs in the default registry: every agent has a
    real executor, reports ready, and has a validated output
    schema for its stage."""
    registry = build_default_registry(engine, _settings())
    assert [a.agent_id for a in registry.list()] == list(AGENT_SPECS)
    for agent_id in AGENT_SPECS:
        entry = registry.get(agent_id)
        assert entry.status == "ready", agent_id
        assert entry.is_test_double is False, agent_id
        assert registry.health(agent_id).status == "ready", agent_id
        assert registry.executor_for(agent_id) is not None, agent_id
    assert sorted(STAGE_OUTPUT_SCHEMAS) == sorted(s.value for s in AGENT_STAGES)


# ------------------------------------------------------------------ Phases 3 + 5: end to end, topic only
def test_end_to_end_topic_only_run(engine, tmp_path):
    """Autonomous operation: the user submits a topic and nothing
    else — no video, timestamp or ranking is chosen by hand. The
    job runs research -> rights -> video intelligence -> ranking
    -> creative director -> production -> quality assurance and
    ends awaiting_approval with a real 1080x1920 render and a QA
    report (needs_review: TTS is disabled for this test, so no
    narration voice exists yet)."""
    job = _new_job(engine)
    _seed_five_sources(engine, job, tmp_path)
    finished = _run_real(engine, job.id)
    assert finished.status == "awaiting_approval"
    assert JobService(engine).get_stage(job.id) == WorkflowStage.COMPLETED

    report = QAService(engine, _settings()).get_report(job.id)
    assert report.verdict == "needs_review"
    assert report.rendered_video_id
    rendered = ProductionService(engine, _settings()).get_rendered_video(job.id)
    assert rendered.width == 1080 and rendered.height == 1920
    assert os.path.isfile(rendered.file_path)
    technical = validate_file(rendered.file_path)
    assert technical.valid is True

    # The final video really contains five different sources.
    from app.models.models import RankingResult
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        rows = session.query(RankingResult).filter(
            RankingResult.job_id == job.id).all()
    assert len(rows) == 5
    assert len({r.candidate_video_id for r in rows}) == 5

    # The same state is visible through the API.
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as client:
        agents = client.get("/agents")
        assert agents.status_code == 200
        assert [a["agent_id"] for a in agents.json()] == list(AGENT_SPECS)
        assert all(a["status"] == "ready" for a in agents.json())
        qa = client.get(f"/jobs/{job.id}/qa")
        assert qa.status_code == 200
        assert qa.json()["verdict"] == "needs_review"


# ------------------------------------------------------------------ Phase 4: deterministic QA failure + repair loop
def _run_pipeline_stopped_before_qa(engine, job):
    """Run the real pipeline with the QA executor unregistered,
    so the job stops after VIDEO_PRODUCTION with a real render
    on disk and no QA checkpoint: `executor_for` raises
    AgentNotImplementedError, which the engine surfaces as 501
    and leaves the job untouched (a raising executor would be
    retried 3x and wrapped instead)."""
    settings = _settings()
    registry = build_default_registry(engine, settings)
    registry.get("quality_assurance").executor = None
    runner = WorkflowEngine(engine, settings, registry)
    with pytest.raises(AgentNotImplementedError):
        runner.run_job(job.id)
    assert JobService(engine).get_stage(job.id) == WorkflowStage.VIDEO_PRODUCTION


def _tamper_render_to_720p(file_path: str, tmp_path) -> None:
    """Replace the rendered MP4 with a 720x1280 transcode of the
    same duration — a genuine production defect QA must catch."""
    tmp = str(tmp_path / "tampered.mp4")
    subprocess.run(["ffmpeg", "-y", "-i", file_path,
                    "-vf", "scale=720:1280",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
                    "-c:a", "aac", tmp],
                   check=True, capture_output=True, timeout=300)
    os.replace(tmp, file_path)


def test_qa_failure_is_deterministic_and_repair_loop_heals(engine, tmp_path):
    """Prompt 09's known issue, fixed: a QA `fail` is attempted
    exactly once (no 3x blind retry). Resume then applies the
    repair request — re-render from the approved EDL — and QA
    re-inspects the corrected render."""
    job = _new_job(engine)
    _seed_five_sources(engine, job, tmp_path)
    _run_pipeline_stopped_before_qa(engine, job)
    rendered = ProductionService(engine, _settings()).get_rendered_video(job.id)
    _tamper_render_to_720p(rendered.file_path, tmp_path)

    failed = _run_real(engine, job.id)
    assert failed.status == "failed"
    assert "QUALITY_ASSURANCE" in (failed.error_message or "")
    assert len(_qa_failed_logs(engine, job.id)) == 1, (
        "deterministic QA failure must be attempted once, not retried")

    report = QAService(engine, _settings()).get_report(job.id)
    assert report.verdict == "fail"
    assert any("resolution" in i.lower() for i in report.issues)
    assert any(r.get("target_stage") == "VIDEO_PRODUCTION"
               for r in report.repair_requests)

    resumed = WorkflowEngine(engine, _settings(),
                             build_default_registry(engine, _settings())).resume_job(job.id)
    assert resumed.status == "awaiting_approval"
    assert JobService(engine).repair_rounds_used(job.id) == 1
    # The old production success log was invalidated (deleted) by
    # the repair, so exactly one production success remains — the
    # re-render — at a new file path.
    assert len(_succeeded(engine, job.id, "production")) == 1
    assert len(_succeeded(engine, job.id, "quality_assurance")) == 1

    fixed = ProductionService(engine, _settings()).get_rendered_video(job.id)
    assert fixed.file_path != rendered.file_path
    assert fixed.width == 1080 and fixed.height == 1920
    assert QAService(engine, _settings()).get_report(job.id).verdict == "needs_review"


# ------------------------------------------------------------------ Phase 4: insufficient candidates, then human approval
def test_recovery_from_insufficient_candidates_after_approval(engine, tmp_path):
    """A job with one approved source fails honestly at RANKING
    and stops safely. After a human supplies four more approved
    sources, resume from RIGHTS_VERIFICATION re-checks them all
    and the job completes — no moment is invented, no rights
    check is skipped."""
    job = _new_job(engine)
    sessions = sessionmaker(bind=engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip-1",
                                   title="Parkour Fails 1", creator="Creator 1")
    path = _write_footage(tmp_path / "clip-1.mp4", 7, (40, 160, 40))
    with sessions() as session:
        session.add(ApprovedFootage(job_id=job.id, candidate_video_id=candidate_id,
                                    storage_path=str(path)))
        session.commit()

    failed = _run_real(engine, job.id)
    assert failed.status == "failed"
    assert "RANKING" in (failed.error_message or "")

    colours = [(40, 40, 180), (40, 200, 220), (120, 40, 20), (140, 140, 140)]
    for i, (speed, colour) in enumerate(zip((6, 5, 4, 3), colours), start=2):
        cid = _seed_candidate(engine, job.id, f"cc-clip-{i}",
                              title=f"Parkour Fails {i}", creator=f"Creator {i}")
        p = _write_footage(tmp_path / f"clip-{i}.mp4", speed, colour)
        with sessions() as session:
            session.add(ApprovedFootage(job_id=job.id, candidate_video_id=cid,
                                        storage_path=str(p)))
            session.commit()

    resumed = WorkflowEngine(engine, _settings(),
                             build_default_registry(engine, _settings())).resume_job(
        job.id, rerun_from_stage="RIGHTS_VERIFICATION")
    assert resumed.status == "awaiting_approval"
    from app.models.models import RankingResult
    with sessions() as session:
        rows = session.query(RankingResult).filter(
            RankingResult.job_id == job.id).all()
    assert len(rows) == 5
    assert len({r.candidate_video_id for r in rows}) == 5


# ------------------------------------------------------------------ Phase 4: repair rounds are capped
def test_resume_refuses_when_repair_rounds_exhausted(engine, tmp_path):
    """The job-level repair cap (qa_max_repair_attempts = 2) is
    enforced on resume: after two repair rounds the job refuses
    another automatic repair instead of looping forever."""
    job = _new_job(engine)
    _seed_five_sources(engine, job, tmp_path)
    _run_pipeline_stopped_before_qa(engine, job)
    rendered = ProductionService(engine, _settings()).get_rendered_video(job.id)
    _tamper_render_to_720p(rendered.file_path, tmp_path)
    failed = _run_real(engine, job.id)
    assert failed.status == "failed"

    service = JobService(engine)
    assert service.repair_rounds_used(job.id) == 0
    service.invalidate_checkpoints(job.id, WorkflowStage.VIDEO_PRODUCTION)
    service.invalidate_checkpoints(job.id, WorkflowStage.VIDEO_PRODUCTION)
    assert service.repair_rounds_used(job.id) == 2

    runner = WorkflowEngine(engine, _settings(),
                            build_default_registry(engine, _settings()))
    with pytest.raises(ValidationError, match="repair attempts exhausted"):
        runner.resume_job(job.id)
    assert JobService(engine).get_job(job.id).status == "failed"
