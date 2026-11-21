"""Footage download tests (post-Prompt 12 fix for the live failure).

A real user job failed at VIDEO_ANALYSIS because an approved
candidate had no local file: Research and Rights had worked, but
nothing downloaded the source video. These tests prove the
downloader now fills that gap — for approved Internet Archive
footage only, with size caps, fail-soft per file, and a resume
path for the failed job.

No test touches the live Archive. Metadata and files come either
from a local HTTP server (real httpx streaming + ffprobe) or
from httpx.MockTransport responses. The video bytes are real
OpenCV-generated clips with known scenes, so ffprobe and the
moment detector see a genuine file.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.rights.service import RightsService
from app.agents.video_intelligence.download import (
    FootageDownloader,
    select_archive_file,
)
from app.agents.video_intelligence.service import VideoIntelligenceService
from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.models.models import (
    AnalyzedMoment,
    ApprovedFootage,
    CandidateVideo,
    RightsEvidence,
    Source,
)
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.orchestrator.states import WorkflowStage
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
            "footage_download_enabled": True}
    base.update(overrides)
    return Settings(**base)


def _new_job(engine, topic="TOP 5 CRAZIEST PARKOUR FAILS"):
    return JobService(engine).create_job(JobCreate(topic=topic))


def _seed_candidate(engine, job_id, external_id, *, licence=None, platform="internet_archive"):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        source = (session.query(Source)
                  .filter(Source.platform == platform,
                          Source.external_id == external_id).first())
        if source is None:
            source = Source(platform=platform, external_id=external_id,
                            title="Parkour Fails",
                            url=f"https://archive.org/details/{external_id}")
            session.add(source)
            session.flush()
        candidate = CandidateVideo(
            job_id=job_id, source_id=source.id, external_video_id=external_id,
            title="Parkour Fails", url=f"https://archive.org/details/{external_id}",
            duration_seconds=30.0, creator="MACSD", licence=licence)
        session.add(candidate)
        session.commit()
        return candidate.id


def _licence_url(variant: str = "by", version: str = "4.0") -> str:
    return f"https://creativecommons.org/licenses/{variant}/{version}/"


def _approve(engine, job_id, candidate_id):
    """Approve via the real Rights service (CC BY 4.0)."""
    RightsService(engine, _settings()).run(job_id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage is not None, "rights did not approve the candidate"


# ------------------------------------------------------------------ local Archive server
class _ArchiveHandler(BaseHTTPRequestHandler):
    """Serves Archive-style metadata + one video file from disk.

    Class attributes are set per test: `files` maps an identifier
    to {"metadata": {...}, "content": bytes, "fail": "metadata"|"file"|None}.
    `calls` records every path requested, so tests can prove a
    download was (or was not) attempted.
    """
    files: dict = {}
    calls: list = []

    def log_message(self, *args):  # silence the server
        pass

    def do_GET(self):
        type(self).calls.append(self.path)
        if self.path.startswith("/metadata/"):
            identifier = self.path.split("/metadata/", 1)[1].split("?")[0]
            entry = type(self).files.get(identifier)
            if entry is None or entry.get("fail") == "metadata":
                self._send(404, b"not found", "text/plain")
                return
            body = json.dumps(entry["metadata"]).encode()
            self._send(200, body, "application/json")
            return
        if self.path.startswith("/download/"):
            parts = self.path.split("/download/", 1)[1].split("/", 1)
            identifier = parts[0]
            entry = type(self).files.get(identifier)
            if entry is None or entry.get("fail") == "file":
                self._send(404, b"not found", "text/plain")
                return
            content = entry["content"]
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        self._send(404, b"not found", "text/plain")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def archive_server(tmp_path):
    """A local Archive stand-in. Yields (base_url, register_fn).

    `register(identifier, content, size=None, name=None, fail=None)`
    makes the server answer metadata + file for that identifier.
    """
    _ArchiveHandler.files = {}
    _ArchiveHandler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def register(identifier, content, *, size=None, name=None, fail=None,
                 extra_files=None):
        name = name or f"{identifier}.mp4"
        metadata = {"identifier": identifier,
                    "files": [{"name": name, "size": size if size is not None else len(content),
                               "format": "MPEG4", "length": "30"}
                              ] + (extra_files or [])}
        _ArchiveHandler.files[identifier] = {
            "metadata": metadata, "content": content, "fail": fail}

    try:
        yield base_url, register
    finally:
        server.shutdown()
        server.server_close()


def _downloader_settings(base_url, **overrides):
    return _settings(footage_archive_base_url=base_url, **overrides)


# ------------------------------------------------------------------ file selection (pure)
def test_select_prefers_mp4_over_other_containers():
    files = [
        {"name": "clip.webm", "size": 1000},
        {"name": "clip.mov", "size": 1000},
        {"name": "clip.mp4", "size": 1000},
        {"name": "clip.txt", "size": 1000},
        {"name": "clip_files.xml", "size": 1000},
    ]
    entry, reason = select_archive_file(files, 10 ** 9, 10 ** 9)
    assert reason is None
    assert entry["name"] == "clip.mp4"


def test_select_falls_back_to_webm_when_no_mp4():
    files = [{"name": "clip.webm", "size": 1000}, {"name": "clip.avi", "size": 1000}]
    entry, reason = select_archive_file(files, 10 ** 9, 10 ** 9)
    assert entry["name"] == "clip.webm"


def test_select_prefers_largest_within_cap_and_skips_oversize():
    files = [
        {"name": "big.mp4", "size": 900_000_000},
        {"name": "mid.mp4", "size": 400_000_000},
        {"name": "small.mp4", "size": 100_000_000},
    ]
    entry, reason = select_archive_file(files, 500_000_000, 10 ** 9)
    assert entry["name"] == "mid.mp4"


def test_select_reports_when_everything_is_over_cap():
    files = [{"name": "huge.mp4", "size": 2_000_000_000}]
    entry, reason = select_archive_file(files, 500_000_000, 10 ** 9)
    assert entry is None
    assert "exceed the size cap" in reason
    assert "huge.mp4" in reason


def test_select_reports_when_no_playable_file():
    files = [{"name": "clip.txt", "size": 10}, {"name": "clip_files.xml", "size": 10}]
    entry, reason = select_archive_file(files, 10 ** 9, 10 ** 9)
    assert entry is None
    assert "no playable video file" in reason


def test_select_honours_the_remaining_job_budget():
    files = [{"name": "clip.mp4", "size": 800_000}]
    entry, reason = select_archive_file(files, 500_000_000, 100_000)
    assert entry is None
    assert "exceed the size cap" in reason


# ------------------------------------------------------------------ downloader, MockTransport
def _mock_client_factory(handler):
    transport = httpx.MockTransport(handler)

    def factory(timeout, _base_url):
        return httpx.Client(transport=transport, timeout=timeout)

    return factory


def _metadata_handler(content: bytes, *, fail_metadata=False, fail_file=False,
                      metadata_files=None):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        path = request.url.path
        if path.startswith("/metadata/"):
            if fail_metadata:
                return httpx.Response(404, content=b"not found")
            files = metadata_files if metadata_files is not None else [
                {"name": "clip.mp4", "size": len(content), "format": "MPEG4",
                 "length": "30"}]
            return httpx.Response(200, json={"identifier": "cc-clip", "files": files})
        if path.startswith("/download/"):
            if fail_file:
                return httpx.Response(404, content=b"not found")
            return httpx.Response(200, content=content,
                                  headers={"Content-Length": str(len(content))})
        return httpx.Response(404, content=b"not found")

    handler.calls = calls
    return handler


def _downloader_with_mock(engine, handler, **overrides):
    settings = _settings(**overrides)
    return FootageDownloader(settings, client_factory=_mock_client_factory(handler))


def test_downloader_downloads_and_records_footage(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url())
    _approve(engine, job.id, candidate_id)
    handler = _metadata_handler(content)
    downloader = _downloader_with_mock(engine, handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "downloaded"
    assert result.bytes_downloaded == len(content)
    assert result.local_path and Path(result.local_path).is_file()
    assert result.local_path.startswith(str(Path(downloader.storage.root)))
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage.storage_path == result.local_path
        assert footage.duration_seconds == pytest.approx(30.0, abs=0.5)
    assert handler.calls["count"] == 2  # metadata + file, nothing else


def test_downloader_never_downloads_unapproved_footage(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "unlicensed-clip")
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        session.add(RightsEvidence(candidate_video_id=candidate_id,
                                   verdict="needs_review", checked_by="rights_agent"))
        session.commit()
    handler = _metadata_handler(content)
    downloader = _downloader_with_mock(engine, handler)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "rights gate" in result.reason
    assert result.local_path is None
    assert handler.calls["count"] == 0, "no HTTP request may be made for unapproved footage"


def test_downloader_skips_file_over_per_file_cap(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url())
    _approve(engine, job.id, candidate_id)
    handler = _metadata_handler(content)
    downloader = _downloader_with_mock(engine, handler, footage_max_mb=0.0001)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "skipped"
    assert "size cap" in result.reason
    assert handler.calls["count"] == 1, "only the metadata fetch; the file must not be fetched"
    assert result.local_path is None


def test_downloader_skips_when_job_budget_is_exhausted(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url())
    _approve(engine, job.id, candidate_id)
    handler = _metadata_handler(content)
    downloader = _downloader_with_mock(engine, handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=100)  # 100 bytes left
        session.commit()
    assert result.status == "skipped"
    assert "size cap" in result.reason


def test_downloader_failed_download_leaves_no_file(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url())
    _approve(engine, job.id, candidate_id)
    handler = _metadata_handler(content, fail_file=True)
    downloader = _downloader_with_mock(engine, handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "download failed" in result.reason
    assert result.local_path is None
    storage = Path(downloader.storage.root)
    assert not list(storage.glob(f"jobs/{job.id}/footage/{candidate_id}/*")), \
        "a failed download must not leave a partial file behind"


def test_downloader_failed_metadata_leaves_no_file(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url())
    _approve(engine, job.id, candidate_id)
    handler = _metadata_handler(content, fail_metadata=True)
    downloader = _downloader_with_mock(engine, handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "metadata fetch failed" in result.reason


def test_downloader_rejects_corrupt_download(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url())
    _approve(engine, job.id, candidate_id)
    handler = _metadata_handler(b"this is not a video" * 100)
    downloader = _downloader_with_mock(engine, handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "failed"
    assert "ffprobe" in result.reason
    storage = Path(downloader.storage.root)
    assert not list(storage.glob(f"jobs/{job.id}/footage/{candidate_id}/*")), \
        "a file that fails ffprobe must be deleted, never analysed"


def test_downloader_skips_non_archive_platform(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "yt-123",
                                   licence=_licence_url(), platform="youtube")
    _approve(engine, job.id, candidate_id)
    handler = _metadata_handler(content)
    downloader = _downloader_with_mock(engine, handler)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "skipped"
    assert "YouTube" in result.reason or "youtube" in result.reason
    assert handler.calls["count"] == 0


def test_downloader_reuses_existing_local_file(engine, tmp_path):
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url())
    _approve(engine, job.id, candidate_id)
    path = tmp_path / "already-there.mp4"
    path.write_bytes(content)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        footage.storage_path = str(path)
        session.commit()
    handler = _metadata_handler(content)
    downloader = _downloader_with_mock(engine, handler)
    with sessions() as session:
        result = downloader.download_candidate(session, job.id, candidate_id,
                                               budget_bytes=10 ** 9)
        session.commit()
    assert result.status == "reused"
    assert result.local_path == str(path)
    assert handler.calls["count"] == 0, "a present file must not be re-downloaded"


# ------------------------------------------------------------------ service level
def test_service_run_downloads_approved_archive_footage(engine, tmp_path, archive_server):
    base_url, register = archive_server
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    register("cc-clip", content)
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url())
    _approve(engine, job.id, candidate_id)
    service = VideoIntelligenceService(engine, _downloader_settings(base_url))
    report = service.run(job.id)
    assert report.metrics["moments"] == 5
    assert report.metrics["downloaded_candidates"] == 1
    assert report.metrics["downloaded_bytes"] == len(content)
    assert report.candidates[0].download is not None
    assert report.candidates[0].download.status == "downloaded"
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage.storage_path and Path(footage.storage_path).is_file()
        assert footage.storage_path.startswith(
            str(Path(service.storage.root / f"jobs/{job.id}/footage")))


def test_service_run_stale_storage_path_re_downloads(engine, tmp_path, archive_server):
    """A recorded storage_path whose file is gone (storage cleaned,
    database kept) falls through to the download instead of
    failing on the stale path."""
    base_url, register = archive_server
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    register("cc-clip", content)
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url())
    _approve(engine, job.id, candidate_id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        footage.storage_path = str(tmp_path / "gone.mp4")  # recorded, file absent
        session.commit()
    service = VideoIntelligenceService(engine, _downloader_settings(base_url))
    report = service.run(job.id)
    assert report.metrics["moments"] == 5
    assert report.metrics["downloaded_candidates"] == 1


def test_service_run_download_failure_fails_honestly(engine, tmp_path, archive_server):
    base_url, register = archive_server
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    register("cc-clip", content, fail="metadata")
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url())
    _approve(engine, job.id, candidate_id)
    service = VideoIntelligenceService(engine, _downloader_settings(base_url))
    with pytest.raises(ExternalServiceError, match="no usable moments"):
        service.run(job.id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(AnalyzedMoment).filter(
            AnalyzedMoment.job_id == job.id).count() == 0


def test_service_run_one_bad_download_does_not_block_the_next(engine, tmp_path, archive_server):
    base_url, register = archive_server
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    register("cc-bad", content, fail="file")
    register("cc-good", content)
    job = _new_job(engine)
    bad_id = _seed_candidate(engine, job.id, "cc-bad", licence=_licence_url())
    good_id = _seed_candidate(engine, job.id, "cc-good", licence=_licence_url())
    _approve(engine, job.id, bad_id)
    _approve(engine, job.id, good_id)
    service = VideoIntelligenceService(engine, _downloader_settings(base_url))
    report = service.run(job.id)
    assert report.metrics["analyzed_candidates"] == 1
    assert report.candidate_video_ids == [good_id]
    assert report.metrics["downloaded_candidates"] == 1
    by_id = {c.candidate_video_id: c for c in report.candidates}
    assert by_id[bad_id].status == "skipped"
    assert by_id[bad_id].download is not None
    assert by_id[bad_id].download.status == "failed"


def test_service_run_total_cap_skips_later_candidates(engine, tmp_path, archive_server):
    base_url, register = archive_server
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    register("cc-clip-1", content)
    register("cc-clip-2", content)
    job = _new_job(engine)
    first_id = _seed_candidate(engine, job.id, "cc-clip-1", licence=_licence_url())
    second_id = _seed_candidate(engine, job.id, "cc-clip-2", licence=_licence_url())
    _approve(engine, job.id, first_id)
    _approve(engine, job.id, second_id)
    # Total budget fits one clip (1.5x its size) but not two.
    total_mb = 1.5 * len(content) / 1024 / 1024
    service = VideoIntelligenceService(
        engine, _downloader_settings(base_url, footage_total_max_mb=total_mb))
    report = service.run(job.id)
    assert report.metrics["analyzed_candidates"] == 1
    assert report.metrics["downloaded_candidates"] == 1
    by_id = {c.candidate_video_id: c for c in report.candidates}
    assert by_id[second_id].status == "skipped"
    assert "size cap" in (by_id[second_id].reason or "")


# ------------------------------------------------------------------ resume of a failed job
def test_failed_job_resumes_from_video_analysis_after_fix(engine, tmp_path, archive_server):
    """The live failure, replayed: a job fails at VIDEO_ANALYSIS
    with no footage, then resumes from that stage once downloads
    are available — it does not start over."""
    base_url, register = archive_server
    content = _write_footage(tmp_path / "clip.mp4").read_bytes()
    register("cc-clip", content)
    job = _new_job(engine)
    _seed_candidate(engine, job.id, "cc-clip", licence=_licence_url())
    # First run: downloads disabled — the old failure, honestly.
    off_settings = _settings(research_provider="test", footage_download_enabled=False)
    runner = WorkflowEngine(engine, off_settings,
                            build_default_registry(engine, off_settings))
    failed = runner.run_job(job.id)
    assert failed.status == "failed"
    assert "VIDEO_ANALYSIS" in (failed.error_message or "")
    assert JobService(engine).get_stage(job.id) == WorkflowStage.FAILED
    # Second run: downloads enabled against the local Archive —
    # resume from the failed stage, as the dashboard does.
    on_settings = _settings(research_provider="test",
                            footage_archive_base_url=base_url)
    resumed = WorkflowEngine(engine, on_settings,
                             build_default_registry(engine, on_settings))
    result = resumed.resume_job(job.id)
    assert WorkflowStage.VIDEO_ANALYSIS in JobService(engine).completed_stages(job.id)
    moments = VideoIntelligenceService(engine, on_settings).list_moments(job.id)
    assert len(moments) == 5
    # The job then fails honestly at RANKING (one source cannot
    # supply five moments from five different sources) — that is
    # a different, expected failure, not the download failure.
    assert result.status == "failed"
    assert "RANKING" in (result.error_message or "")
