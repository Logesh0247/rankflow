"""Vid Kraken YouTube downloader tests (2026-11-21).

Vid Kraken is a managed YouTube download API: POST
/api/v2/download {"url", "format": "1080"} returns a jobId, poll
GET /api/v2/download/{jobId} until COMPLETED, then stream the
returned downloadUrl. Auth is `Authorization: Bearer <key>`
(vidkraken.com/llms.txt). It is used ONLY for approved CC / owned
YouTube videos — Rights still approves first, and Vid Kraken
never bypasses Rights. When a key is configured it is tried
BEFORE yt-dlp; yt-dlp is the fallback when no key is set or the
Vid Kraken service fails / times out. A Vid Kraken job that
FAILED with a machine-readable errorCode (AGE_RESTRICTED,
MEMBERS_ONLY, …) is a definitive failure and is NOT retried
with yt-dlp.

No test touches the live API: the key is a test value, submit /
poll / file bytes are stubbed with MockTransport, and yt-dlp is
stubbed with a fake runner that copies a real OpenCV-generated
clip into place. Live verification must happen on the deploy
host, via a Render-side job, once VIDKRAKEN_API_KEY is set.
"""
import subprocess
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.research.service import ResearchService
from app.agents.rights.service import RightsService
from app.agents.video_intelligence.download import FootageDownloader
from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.models.models import ApprovedFootage, CandidateVideo, Source
from app.orchestrator.jobs import JobService
from app.schemas.schemas import JobCreate

FPS, W, H = 15, 160, 120
SCENES = [
    (0, 5, (120, 40, 20), 0, 0),
    (5, 11, (40, 160, 40), 7, 50),
    (11, 20, (40, 40, 180), 1, 50),
    (20, 24, (40, 200, 220), 7, 50),
    (24, 30, (140, 140, 140), 0, 0),
]


def _pingpong(frame_index: int, speed: int, span: int) -> int:
    p = (frame_index * speed) % (2 * span)
    return p if p <= span else 2 * span - p


def _write_footage(path: Path, duration: float = 30.0) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for i in range(int(duration * FPS)):
        t = i / FPS
        seg = next(s for s in SCENES if s[0] <= t < s[1] or (s[1] == duration and t >= s[0]))
        _, _, colour, speed, size = seg
        frame = np.full((H, W, 3), colour, dtype=np.uint8)
        if size:
            x = _pingpong(i, speed, W - size)
            cv2.rectangle(frame, (x, H // 2 - size // 2), (x + size, H // 2 + size // 2),
                          (0, 0, 0), -1)
        writer.write(frame)
    writer.release()
    return path


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 300.0,
            "rights_fetch_ia_metadata": False,
            "footage_download_enabled": True,
            "vidkraken_api_key": "test-vidkraken",
            "vidkraken_poll_interval_seconds": 0.0,
            "vidkraken_poll_timeout_seconds": 30.0}
    base.update(overrides)
    return Settings(**base)


def _new_job(engine, topic="TOP 5 FUNNIEST CAT VIDEOS"):
    return JobService(engine).create_job(JobCreate(topic=topic))


def _cc_url(variant: str = "by", version: str = "3.0") -> str:
    return f"https://creativecommons.org/licenses/{variant}/{version}/"


def _seed_candidate(engine, job_id, external_id, *, licence=None,
                    platform="youtube", title="Funny Cat Fails",
                    creator="CatChannel", url=None):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        source = (session.query(Source)
                  .filter(Source.platform == platform,
                          Source.external_id == external_id).first())
        if source is None:
            source = Source(platform=platform, external_id=external_id,
                            title=title,
                            url=url or f"https://www.youtube.com/watch?v={external_id}")
            session.add(source)
            session.flush()
        candidate = CandidateVideo(
            job_id=job_id, source_id=source.id, external_video_id=external_id,
            title=title,
            url=url or f"https://www.youtube.com/watch?v={external_id}",
            duration_seconds=30.0, creator=creator, licence=licence)
        session.add(candidate)
        session.commit()
        return candidate.id


def _approve(engine, job_id, candidate_id):
    RightsService(engine, _settings()).run(job_id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage is not None, "rights did not approve the candidate"


# ------------------------------------------------------------------ Vid Kraken mock
def _vidkraken_handler(content: bytes, *, poll_states=None,
                       submit_status=200, job_id="vk-123"):
    """MockTransport handler for the Vid Kraken API + CDN file.

    `poll_states` is the sequence of poll responses (each a dict
    with at least "status"); the last one repeats. Records every
    request so tests can prove the Bearer header, the 1080 format
    and the Idempotency-Key were actually sent.
    """
    calls = {"submit": [], "poll": [], "file": 0}
    states = list(poll_states) if poll_states is not None else [
        {"status": "COMPLETED",
         "downloadUrl": "https://cdn.example.invalid/vk-123.mp4",
         "title": "Funny Cat Fails", "duration": 30}]

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "vidkraken.com":
            if request.method == "POST":
                import json
                calls["submit"].append({
                    "headers": dict(request.headers),
                    "json": json.loads(request.content.decode()),
                })
                if submit_status != 200:
                    return httpx.Response(submit_status, content=b"error")
                return httpx.Response(200, json={
                    "jobId": job_id, "title": "Funny Cat Fails",
                    "duration": 30, "status": "IN_QUEUE"})
            calls["poll"].append(dict(request.headers))
            state = states.pop(0) if len(states) > 1 else states[0]
            return httpx.Response(200, json=state)
        if host == "cdn.example.invalid":
            calls["file"] += 1
            return httpx.Response(200, content=content,
                                  headers={"Content-Length": str(len(content))})
        return httpx.Response(404, content=b"not found")

    handler.calls = calls
    return handler


def _mock_client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(timeout, _base_url):
        return httpx.Client(transport=transport, timeout=timeout)

    return factory


class _StubRunner:
    """Fake yt-dlp: copies a real clip into place. Records calls."""

    def __init__(self, content=None):
        self.content = content
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout):
        self.calls.append(list(cmd))
        out_idx = cmd.index("--output") + 1
        target = Path(cmd[out_idx])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.content or b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _downloader(handler, runner, **overrides):
    settings = _settings(**overrides)
    return FootageDownloader(settings,
                             client_factory=_mock_client_factory(handler),
                             ytdlp_runner=runner)


def _run_download(engine, downloader, job_id, candidate_id):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job_id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    return result


def test_vidkraken_submit_poll_download_records_footage(engine, tmp_path):
    """Submit returns a job id + title/duration, poll returns
    COMPLETED with a downloadUrl, the file streams and records."""
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    handler = _vidkraken_handler(content)
    runner = _StubRunner(content)
    downloader = _downloader(handler, runner)
    result = _run_download(engine, downloader, job.id, candidate_id)
    assert result.status == "downloaded"
    assert result.bytes_downloaded == len(content)
    assert result.local_path and Path(result.local_path).is_file()
    assert result.local_path.endswith("vk-123.mp4")
    assert result.source_url == "https://cdn.example.invalid/vk-123.mp4"
    assert result.vidkraken_job_id == "vk-123"
    assert "via Vid Kraken" in result.reason
    # Submit carried Bearer auth, format 1080 and an Idempotency-Key.
    submit = handler.calls["submit"][0]
    assert submit["headers"]["authorization"] == "Bearer test-vidkraken"
    assert submit["json"] == {"url": "https://www.youtube.com/watch?v=cc-vid-1",
                              "format": "1080"}
    assert submit["headers"]["idempotency-key"]
    assert handler.calls["poll"], "the job was polled"
    # yt-dlp was never run — Vid Kraken was tried first and won.
    assert runner.calls == []
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage.storage_path == result.local_path
        assert footage.duration_seconds == pytest.approx(30.0, abs=0.5)


def test_vidkraken_poll_in_progress_then_completed(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    handler = _vidkraken_handler(content, poll_states=[
        {"status": "IN_QUEUE"},
        {"status": "IN_PROGRESS", "progress": {"percent": 50}},
        {"status": "COMPLETED",
         "downloadUrl": "https://cdn.example.invalid/vk-123.mp4"}])
    downloader = _downloader(handler, _StubRunner(content))
    result = _run_download(engine, downloader, job.id, candidate_id)
    assert result.status == "downloaded"
    assert len(handler.calls["poll"]) == 3


def test_vidkraken_error_code_is_a_failure_not_a_success(engine, tmp_path):
    """Poll returns FAILED with AGE_RESTRICTED: the download fails
    honestly, and yt-dlp is NOT tried to get around it."""
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    handler = _vidkraken_handler(b"", poll_states=[
        {"status": "FAILED", "errorCode": "AGE_RESTRICTED",
         "message": "This video is age-restricted"}])
    runner = _StubRunner(b"")
    downloader = _downloader(handler, runner)
    result = _run_download(engine, downloader, job.id, candidate_id)
    assert result.status == "failed"
    assert "AGE_RESTRICTED" in result.reason
    assert result.vidkraken_job_id == "vk-123"
    assert result.local_path is None
    assert runner.calls == [], "yt-dlp must not retry a definitive Vid Kraken rejection"


@pytest.mark.parametrize("error_code", [
    "MEMBERS_ONLY", "GEO_RESTRICTED", "PRIVATE_VIDEO",
    "DRM_PROTECTED", "VIDEO_UNAVAILABLE",
])
def test_vidkraken_other_error_codes_are_failures(engine, tmp_path, error_code):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    handler = _vidkraken_handler(b"", poll_states=[
        {"status": "FAILED", "errorCode": error_code}])
    runner = _StubRunner(b"")
    downloader = _downloader(handler, runner)
    result = _run_download(engine, downloader, job.id, candidate_id)
    assert result.status == "failed"
    assert error_code in result.reason
    assert runner.calls == []


def test_vidkraken_no_key_falls_back_to_ytdlp(engine, tmp_path):
    """No key: Vid Kraken is not attempted (0 calls) and yt-dlp
    downloads the video as before."""
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    handler = _vidkraken_handler(content)
    runner = _StubRunner(content)
    downloader = _downloader(handler, runner, vidkraken_api_key="")
    result = _run_download(engine, downloader, job.id, candidate_id)
    assert result.status == "downloaded"
    assert result.vidkraken_job_id is None
    assert handler.calls["submit"] == [] and handler.calls["poll"] == []
    assert len(runner.calls) == 1, "yt-dlp is the fallback with no key"
    assert "https://www.youtube.com/watch?v=cc-vid-1" in runner.calls[0]


def test_vidkraken_service_error_falls_back_to_ytdlp(engine, tmp_path):
    """Submit returns 500: the service failed, so yt-dlp is tried."""
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    handler = _vidkraken_handler(content, submit_status=500)
    runner = _StubRunner(content)
    downloader = _downloader(handler, runner)
    result = _run_download(engine, downloader, job.id, candidate_id)
    assert result.status == "downloaded"
    assert result.vidkraken_job_id is None
    assert len(runner.calls) == 1


def test_vidkraken_poll_timeout_falls_back_to_ytdlp(engine, tmp_path):
    """Poll never completes within the timeout: fall back to yt-dlp."""
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-vid-1",
                                   licence=_cc_url())
    _approve(engine, job.id, candidate_id)
    handler = _vidkraken_handler(content, poll_states=[{"status": "IN_PROGRESS"}])
    runner = _StubRunner(content)
    downloader = _downloader(handler, runner,
                             vidkraken_poll_timeout_seconds=0.01)
    result = _run_download(engine, downloader, job.id, candidate_id)
    assert result.status == "downloaded"
    assert result.vidkraken_job_id is None
    assert len(runner.calls) == 1


def test_vidkraken_never_used_for_non_youtube_candidate(engine, tmp_path):
    """A Vimeo candidate never goes through Vid Kraken."""
    from app.agents.research.providers import VimeoProvider  # noqa: F401

    job = _new_job(engine)
    candidate_id = _seed_candidate(
        engine, job.id, "123456789",
        licence="https://creativecommons.org/licenses/by/4.0/",
        platform="vimeo", url="https://vimeo.com/123456789")
    _approve(engine, job.id, candidate_id)
    handler = _vidkraken_handler(b"")
    downloader = _downloader(handler, _StubRunner(b""))
    result = _run_download(engine, downloader, job.id, candidate_id)
    # The Vimeo detail lookup is not stubbed here, so the download
    # fails — but the point is Vid Kraken was never contacted.
    assert handler.calls["submit"] == [] and handler.calls["poll"] == []
    assert result.status == "failed"


def test_vidkraken_never_downloads_unapproved_footage(engine, tmp_path):
    """A standard-licence YouTube video is rejected by Rights, so
    the gate fails and Vid Kraken is never called — 0 calls."""
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "std-vid-1",
                                   licence="standard YouTube licence")
    with pytest.raises(ExternalServiceError, match="approved 0 of 1"):
        RightsService(engine, _settings()).run(job.id)
    handler = _vidkraken_handler(b"")
    runner = _StubRunner(b"")
    downloader = _downloader(handler, runner)
    result = _run_download(engine, downloader, job.id, candidate_id)
    assert result.status == "failed"
    assert "rights gate" in result.reason
    assert handler.calls["submit"] == [] and handler.calls["poll"] == []
    assert runner.calls == []


def test_settings_status_reports_vidkraken_flag():
    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as client:
        status = client.get("/settings/status").json()
        assert status["vidkraken_configured"] is False
