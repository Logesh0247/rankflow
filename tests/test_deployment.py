"""Deployment tests (Prompt 12): cost monitoring, rate limiting,
secret redaction, backups, storage validation, notifications and
the migration runner.

Nothing here renders video or calls a live provider — the slow
render/QA paths are covered by their own test files.
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.cost import CostService
from app.core.logging import _redact
from app.core.notifications import notify_job_failed, recent_notifications
from app.core.ratelimit import RateLimiter, RateLimitMiddleware
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.test_doubles import build_test_registry
from app.schemas.schemas import JobCreate
from app.storage.validate import validate_storage

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "TOP 5 CRAZIEST PARKOUR FAILS"


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 5.0}
    base.update(overrides)
    return Settings(**base)


def _new_job(engine, topic=TOPIC):
    return JobService(engine).create_job(JobCreate(topic=topic))


# ---------------------------------------------------------------- cost
def test_cost_service_prices_compute_and_api(engine):
    job = _new_job(engine)
    cost = CostService(engine)
    cost.record_compute(job.id, "RESEARCH", 60.0)  # 60 CPU-seconds
    cost.record_compute(job.id, "VIDEO_PRODUCTION", 120.0)
    cost.record_api_call(job.id, "internet_archive", "parkour fails")
    summary = cost.job_cost(job.id)
    assert summary.compute_seconds == pytest.approx(180.0)
    # 180 s at $0.10/CPU-hour = $0.005
    assert summary.compute_usd == pytest.approx(0.005, abs=1e-4)
    assert summary.api_calls == 1
    assert summary.api_usd == 0.0  # Internet Archive is genuinely free
    assert summary.tts_chars == 0
    assert summary.tts_usd == 0.0
    assert summary.total_usd == pytest.approx(0.005, abs=1e-4)
    assert len(summary.events) == 3
    assert any("No TTS synthesis ran" in n for n in summary.notes)


def test_cost_summary_totals(engine):
    job = _new_job(engine)
    CostService(engine).record_compute(job.id, "RANKING", 30.0)
    total = CostService(engine).summary()
    assert total["total_compute_seconds"] >= 30.0
    assert total["total_api_usd"] == 0.0
    assert total["rates"]["compute_usd_per_cpu_hour"] == 0.10


def test_engine_run_records_compute_cost(engine):
    """A simulated job run prices every stage it completes."""
    job = _new_job(engine)
    finished = WorkflowEngine(
        engine, _settings(), build_test_registry()).run_job(job.id)
    assert finished.status == "awaiting_approval"
    summary = CostService(engine).job_cost(job.id)
    assert len(summary.events) == 7  # one compute event per stage
    assert summary.compute_seconds >= 0
    assert summary.total_usd >= 0


def test_cost_endpoints(engine):
    from main import app

    job = _new_job(engine)
    CostService(engine).record_compute(job.id, "RESEARCH", 10.0)
    with TestClient(app) as client:
        res = client.get(f"/jobs/{job.id}/cost")
        assert res.status_code == 200
        assert res.json()["job_id"] == job.id
        assert res.json()["compute_seconds"] == pytest.approx(10.0)
        assert res.json()["api_calls"] == 0
        missing = client.get("/jobs/does-not-exist/cost")
        assert missing.status_code == 404
        summary = client.get("/cost/summary")
        assert summary.status_code == 200
        assert summary.json()["total_api_usd"] == 0.0


# ---------------------------------------------------------------- rate limiting
def test_rate_limiter_allows_then_blocks():
    limiter = RateLimiter(2)
    assert limiter.allow("1.2.3.4")[0] is True
    assert limiter.allow("1.2.3.4")[0] is True
    allowed, retry_after = limiter.allow("1.2.3.4")
    assert allowed is False
    assert retry_after > 0
    # A different client is unaffected.
    assert limiter.allow("5.6.7.8")[0] is True


def test_rate_limit_middleware_returns_429():
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, limiter=RateLimiter(2))

    @app.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
        assert client.get("/ping").status_code == 200
        blocked = client.get("/ping")
        assert blocked.status_code == 429
        assert blocked.headers.get("Retry-After")
        assert blocked.json()["code"] == "rate_limited"


# ---------------------------------------------------------------- secret redaction
def test_log_redaction_masks_keys():
    assert _redact("api_key=sk-live-abcdef") == "api_key=***"
    assert _redact('token: "mytoken123"') == 'token=***"'
    assert _redact("Authorization: Bearer abc.def.ghi") == "Authorization: Bearer ***"
    assert "hook" in _redact("https://hooks.slack.com/services/T123/ABC/xyz")
    assert "xyz" not in _redact("https://hooks.slack.com/services/T123/ABC/xyz")
    # Ordinary text is untouched.
    assert _redact("job abc finished stage RESEARCH") == "job abc finished stage RESEARCH"


def test_settings_status_never_leaks_secrets(engine):
    from main import app

    with TestClient(app) as client:
        res = client.get("/settings/status")
        assert res.status_code == 200
        body = res.text
        for key in ("YOUTUBE_API_KEY", "TTS_API_KEY", "S3_SECRET"):
            assert key not in body
        # The response must not contain any configured secret value.
        settings = Settings()
        for value in (settings.youtube_api_key, settings.tts_api_key,
                      settings.s3_secret_access_key):
            if value:
                assert value not in body


# ---------------------------------------------------------------- reliability
def test_backup_script_creates_verified_backup(tmp_path):
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES ('hello')")
    conn.commit()
    conn.close()
    out_dir = tmp_path / "backups"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "backup_db.py"),
         "--out", str(out_dir)],
        cwd=str(ROOT), capture_output=True, text=True,
        env={"DATABASE_URL": f"sqlite:///{src}",
             "STORAGE_ROOT": str(tmp_path / "storage"),
             "PATH": "/usr/bin:/bin:/usr/local/bin"})
    assert proc.returncode == 0, proc.stderr
    backups = list(out_dir.glob("rankflow-*.db"))
    assert len(backups) == 1
    check = sqlite3.connect(str(backups[0]))
    assert check.execute("SELECT x FROM t").fetchone()[0] == "hello"
    check.close()


def test_storage_validation_ok(engine):
    # The test storage root is a small tmpfs — lower the free-space
    # floor so the check measures writability/DB/files, not /tmp size.
    report = validate_storage(engine, _settings(storage_min_free_mb=10))
    assert report["ok"] is True
    assert report["checks"]["storage_root_writable"] is True
    assert report["checks"]["rendered_videos_missing"] == 0


def test_storage_validation_flags_missing_render(engine):
    from app.models.models import RenderedVideo

    job = _new_job(engine)
    sessions = __import__("sqlalchemy.orm", fromlist=["sessionmaker"]).sessionmaker(bind=engine)
    with sessions() as session:
        session.add(RenderedVideo(
            job_id=job.id, file_path="/nonexistent/rankflow-short.mp4",
            status="ready"))
        session.commit()
    report = validate_storage(engine, _settings(storage_min_free_mb=10))
    assert report["ok"] is False
    assert report["checks"]["rendered_videos_missing"] == 1
    assert any("missing from disk" in i for i in report["issues"])


def test_failure_notification_is_recorded(engine, tmp_path, monkeypatch):
    settings = _settings(storage_root=str(tmp_path / "storage"))
    monkeypatch.setattr("app.core.notifications.get_settings", lambda: settings)
    event = notify_job_failed("job-1", "RANKING", "boom", settings)
    assert event["event"] == "job_failed"
    assert event["job_id"] == "job-1"
    log = tmp_path / "notifications.log"
    assert log.is_file()
    assert json.loads(log.read_text().splitlines()[0])["stage"] == "RANKING"
    assert recent_notifications(settings)[0]["job_id"] == "job-1"


def test_migrate_all_runs_clean():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "migrate_all.py")],
        cwd=str(ROOT), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "migration(s) applied" in proc.stdout


def test_health_detailed_endpoint(engine):
    from main import app

    with TestClient(app) as client:
        res = client.get("/health/detailed")
        assert res.status_code == 200
        body = res.json()
        assert body["database_ok"] is True
        assert body["ffmpeg_ok"] is True
        assert "storage" in body
        assert "recent_failures" in body
        assert "api_key" not in res.text.lower()
