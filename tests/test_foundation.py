"""Config loading, database, schemas, storage, security, API and
no-fake-agents tests for Prompt 01."""
import os
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

ROOT = Path(__file__).resolve().parents[1]


def test_config_defaults():
    from app.core.config import Settings

    s = Settings(database_url="sqlite:////tmp/x.db")
    assert s.app_name == "RANKFLOW AI"
    assert s.is_sqlite is True
    assert s.is_postgres is False
    assert s.storage_backend == "local"


def test_config_postgres_detection():
    from app.core.config import Settings

    s = Settings(database_url="postgresql+psycopg://u:p@localhost:5432/rankflow")
    assert s.is_postgres is True
    assert s.is_sqlite is False


def test_config_empty_database_url_rejected():
    from app.core.config import Settings

    with pytest.raises(Exception):
        Settings(database_url="")


def test_logging_configures():
    from app.core.logging import configure_logging, get_logger

    configure_logging("INFO")
    assert get_logger("test").name == "rankflow.test"


def test_security_safe_join_rejects_traversal(tmp_path):
    from app.core.security import safe_join
    from app.core.errors import ValidationError

    with pytest.raises(ValidationError):
        safe_join(tmp_path, "..", "..", "etc", "passwd")
    assert safe_join(tmp_path, "a", "b.txt").parent.name == "a"


def test_security_clean_text():
    from app.core.security import clean_text
    from app.core.errors import ValidationError

    assert clean_text("  parkour fails  ") == "parkour fails"
    with pytest.raises(ValidationError):
        clean_text("   ")
    with pytest.raises(ValidationError):
        clean_text("x" * 201)


def test_schemas_job_create():
    from app.schemas.schemas import JobCreate

    assert JobCreate(topic=" funniest parkour fails ").topic == "funniest parkour fails"
    with pytest.raises(PydanticValidationError):
        JobCreate(topic="   ")
    with pytest.raises(PydanticValidationError):
        JobCreate(topic="x" * 201)


def test_schemas_moment_end_after_start():
    from app.schemas.schemas import VideoMomentCreate

    ok = VideoMomentCreate(job_id="j", candidate_video_id="c", start_seconds=1.0, end_seconds=5.0)
    assert ok.end_seconds == 5.0
    with pytest.raises(PydanticValidationError):
        VideoMomentCreate(job_id="j", candidate_video_id="c", start_seconds=5.0, end_seconds=5.0)


def test_schemas_ranking_bounds():
    from app.schemas.schemas import RankingResultCreate

    assert RankingResultCreate(job_id="j", moment_id="m", candidate_video_id="c", rank=5, score=0.9).rank == 5
    with pytest.raises(PydanticValidationError):
        RankingResultCreate(job_id="j", moment_id="m", candidate_video_id="c", rank=6, score=0.9)


def test_schemas_edl_clip_count():
    from app.schemas.schemas import EditDecisionList, EDLClip

    clips = [EDLClip(clip_number=i + 1, moment_id=f"m{i}", source_path=f"/tmp/{i}.mp4", start_seconds=0, end_seconds=3) for i in range(5)]
    assert len(EditDecisionList(job_id="j", clips=clips).clips) == 5
    with pytest.raises(PydanticValidationError):
        EditDecisionList(job_id="j", clips=clips + [EDLClip(clip_number=6, moment_id="m5", source_path="/tmp/5.mp4", start_seconds=0, end_seconds=3)])


def test_schemas_agent_task_name_pattern():
    from app.schemas.schemas import AgentTask

    assert AgentTask(task_id="t", job_id="j", agent_name="research").agent_name == "research"
    with pytest.raises(PydanticValidationError):
        AgentTask(task_id="t", job_id="j", agent_name="Research Agent")


def test_database_tables_created(engine):
    from sqlalchemy import inspect

    tables = set(inspect(engine).get_table_names())
    expected = {"jobs", "sources", "candidate_videos", "rights_evidence", "approved_footage",
                "analyzed_moments", "ranking_results", "storyboards", "narrations",
                "edit_instructions", "rendered_videos", "qa_reports", "agent_execution_logs",
                "cost_events"}  # Prompt 12: cost monitoring
    assert expected.issubset(tables), f"missing: {expected - tables}"
    assert len(tables) == 14


def test_database_job_roundtrip(engine):
    from sqlalchemy.orm import sessionmaker
    from app.models.models import Job, Source, CandidateVideo

    session = sessionmaker(bind=engine)()
    job = Job(topic="test topic")
    session.add(job)
    session.commit()
    source = Source(platform="youtube", external_id="ch1")
    session.add(source)
    session.commit()
    cand = CandidateVideo(job_id=job.id, source_id=source.id, external_video_id="v1", title="t")
    session.add(cand)
    session.commit()
    found = session.query(Job).filter(Job.id == job.id).first()
    assert found is not None and found.status == "created"
    assert found.candidates[0].external_video_id == "v1"


def test_database_check(engine):
    from app.database.session import check_db

    assert check_db(engine) is True


def test_storage_put_get_delete(tmp_path):
    from app.storage.local import LocalStorage

    store = LocalStorage(tmp_path / "store")
    store.put("jobs/abc/depth.png", b"hello")
    assert store.exists("jobs/abc/depth.png")
    assert store.get_path("jobs/abc/depth.png").read_bytes() == b"hello"
    store.delete("jobs/abc/depth.png")
    assert not store.exists("jobs/abc/depth.png")


def test_storage_rejects_traversal(tmp_path):
    from app.storage.local import LocalStorage
    from app.core.errors import ValidationError

    store = LocalStorage(tmp_path / "store")
    with pytest.raises(ValidationError):
        store.put("../escape.txt", b"x")


def test_ffmpeg_present():
    from app.utils.paths import which_ffmpeg

    tools = which_ffmpeg()
    assert tools["ffmpeg"], "ffmpeg not found"
    assert "ffmpeg version" in tools["ffmpeg"].lower()


def test_no_fake_agents():
    """Prompt 01 rule, updated in Prompts 03-09: agent folders must
    contain no implementation code — except the agents actually
    built by their own prompt. All seven agents are built
    (Prompts 03-09); none is left unbuilt."""
    agents_dir = ROOT / "app" / "agents"
    for agent in agents_dir.iterdir():
        if not agent.is_dir() or agent.name.startswith("_"):
            continue
        py_files = [p for p in agent.iterdir() if p.suffix == ".py" and p.name != "__init__.py"]
        assert (agent / "README.md").exists(), f"{agent.name} missing README"
        if agent.name in ("research", "rights", "video_intelligence", "ranking",
                          "creative_director", "production", "quality_assurance"):
            prompt = {"research": "03", "rights": "04",
                      "video_intelligence": "05", "ranking": "06",
                      "creative_director": "07", "production": "08",
                      "quality_assurance": "09"}[agent.name]
            assert py_files, f"{agent.name} agent must have implementation files after Prompt {prompt}"
            assert f"BUILT (Prompt {prompt})" in (agent / "README.md").read_text()
            continue
        assert py_files == [], f"{agent.name} has implementation files before its prompt: {py_files}"
        assert "NOT BUILT" in (agent / "README.md").read_text()


def test_api_health_and_jobs():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["app"] == "RANKFLOW AI"
        assert body["database_ok"] is True
        assert body["ffmpeg_ok"] is True

        created = client.post("/jobs", json={"topic": "funniest parkour fails"})
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]
        assert created.json()["status"] == "created"

        listed = client.get("/jobs")
        assert listed.status_code == 200
        assert any(j["id"] == job_id for j in listed.json())

        one = client.get(f"/jobs/{job_id}")
        assert one.status_code == 200
        assert one.json()["topic"] == "funniest parkour fails"

        assert client.get("/jobs/does-not-exist").status_code == 404
        assert client.post("/jobs", json={"topic": ""}).status_code == 422
