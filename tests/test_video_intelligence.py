"""Video Intelligence Agent tests (Prompt 05): ingestion, frame
extraction, heuristic analysis, timestamp verification, the
rights gate, persistence, orchestrator integration and the API.

Known footage is generated here with OpenCV: the scene boundaries
are ground truth because the test chose them when it drew the
frames. Timestamps are therefore measured against values the test
knows, never against values the agent reported about itself.

The strict rules under test throughout: unapproved footage is
never analysed, and no timestamp is ever fabricated — every
moment boundary comes from a frame that was actually read.
"""
import json
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.rights.gate import require_approved
from app.agents.rights.service import RightsService
from app.agents.video_intelligence.analysis import ANALYSER, detect_moments
from app.agents.video_intelligence.frames import extract_samples
from app.agents.video_intelligence.probe import probe_video
from app.agents.video_intelligence.service import VideoIntelligenceService
from app.agents.video_intelligence.verify import verify_moments
from app.core.config import Settings
from app.core.errors import ExternalServiceError, NotFoundError, StorageError, ValidationError
from app.models.models import (
    AnalyzedMoment, ApprovedFootage, CandidateVideo, RightsEvidence, Source,
)
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.schemas.schemas import JobCreate

FPS, W, H = 15, 160, 120
# (start, end, BGR colour, speed px/frame, square size) — same shape as
# scripts/make_known_footage.py, smaller for test speed.
SCENES = [
    (0, 5, (120, 40, 20), 0, 0),
    (5, 11, (40, 160, 40), 7, 50),
    (11, 20, (40, 40, 180), 1, 50),
    (20, 24, (40, 200, 220), 7, 50),
    (24, 30, (140, 140, 140), 0, 0),
]
BOUNDARIES = [5, 11, 20, 24]


def _pingpong(frame_index: int, speed: int, span: int) -> int:
    """Bounce position: 0..span..0. The square is black (uniform
    contrast on every background) and ping-pongs instead of
    wrapping, so motion measured between 1 fps samples is
    proportional to speed and does not alias: fast segments read
    above the 6.0 motion threshold, the slow one below it, static
    reads 0."""
    p = (frame_index * speed) % (2 * span)
    return p if p <= span else 2 * span - p


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 30.0,
            "rights_fetch_ia_metadata": False,
            # Downloads stay off here: the pre-download tests use
            # seeded local files, and the no-footage tests must fail
            # without touching the live Archive. Download behaviour
            # is covered by tests/test_footage_download.py.
            "footage_download_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _write_footage(path: Path, segments, duration: float) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for i in range(int(duration * FPS)):
        t = i / FPS
        seg = next(s for s in segments if s[0] <= t < s[1] or (s[1] == duration and t >= s[0]))
        _, _, colour, speed, size = seg
        frame = np.full((H, W, 3), colour, dtype=np.uint8)
        if size:
            x = _pingpong(i, speed, W - size)
            cv2.rectangle(frame, (x, H // 2 - size // 2), (x + size, H // 2 + size // 2),
                          (0, 0, 0), -1)
        writer.write(frame)
    writer.release()
    return path


def _known_scenes(tmp_path: Path) -> Path:
    return _write_footage(tmp_path / "known_scenes.mp4", SCENES, 30)


def _known_black(tmp_path: Path) -> Path:
    return _write_footage(tmp_path / "known_black.mp4",
                          [(0, 4, (0, 0, 0), 0, 0), (4, 12, (40, 160, 40), 7, 50)], 12)


def _new_job(engine, topic="TOP 5 CRAZIEST PARKOUR FAILS"):
    return JobService(engine).create_job(JobCreate(topic=topic))


def _seed_candidate(engine, job_id, external_id, *, licence=None):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        source = (session.query(Source)
                  .filter(Source.platform == "internet_archive",
                          Source.external_id == external_id).first())
        if source is None:
            source = Source(platform="internet_archive", external_id=external_id,
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


def _approve(engine, job_id, candidate_id):
    """Approve via the real Rights service (CC BY 4.0)."""
    RightsService(engine, _settings()).run(job_id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage is not None, "rights did not approve the candidate"


def _licence_url(variant: str, version: str = "4.0") -> str:
    return f"https://creativecommons.org/licenses/{variant}/{version}/"


def _iou(a_start, a_end, b_start, b_end) -> float:
    overlap = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return overlap / union if union > 0 else 0.0


# ---------------------------------------------------------------- Phase 1: ingestion
def test_probe_valid_video(tmp_path):
    path = _known_scenes(tmp_path)
    probe = probe_video(path)
    assert probe.duration_seconds == pytest.approx(30.0, abs=0.5)
    assert (probe.width, probe.height) == (W, H)
    assert probe.fps == pytest.approx(FPS, abs=0.5)
    assert probe.video_codec in ("mpeg4", "mp4v")  # ffprobe names the mp4v fourcc "mpeg4"
    assert probe.has_audio is False


def test_probe_missing_file_raises(tmp_path):
    with pytest.raises(StorageError, match="not found"):
        probe_video(tmp_path / "nope.mp4")


def test_probe_corrupt_file_raises(tmp_path):
    path = tmp_path / "corrupt.mp4"
    path.write_bytes(b"this is not a video" * 100)
    with pytest.raises(ValidationError):
        probe_video(path)


def test_probe_non_video_file_raises(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello\n" * 50)
    with pytest.raises(ValidationError, match="no video stream|not a readable video"):
        probe_video(path)


# ---------------------------------------------------------------- Phase 2: frame extraction
def test_frame_extraction_samples_and_scene_boundaries(tmp_path):
    path = _known_scenes(tmp_path)
    extraction = extract_samples(path, sample_fps=1.0, max_frames=600)
    assert 25 <= len(extraction.samples) <= 31  # ~1 fps over 30 s
    timestamps = [s.timestamp for s in extraction.samples]
    assert timestamps == sorted(timestamps)
    assert extraction.effective_fps == pytest.approx(1.0, abs=0.01)
    # Timestamp accuracy: every ground-truth boundary is detected within 1.5 s.
    for boundary in BOUNDARIES:
        assert any(abs(b - boundary) <= 1.5 for b in extraction.scene_boundaries), \
            f"boundary {boundary}s not detected: {extraction.scene_boundaries}"
    assert len(extraction.scene_boundaries) <= len(BOUNDARIES) + 1


def test_frame_extraction_performance(tmp_path):
    path = _known_scenes(tmp_path)
    started = time.monotonic()
    extraction = extract_samples(path, sample_fps=1.0, max_frames=600)
    elapsed = time.monotonic() - started
    assert extraction.sampled_frame_count >= 25
    assert elapsed < 10.0, f"frame extraction too slow: {elapsed:.1f}s for a 30 s clip"


def test_frame_extraction_unreadable_file_raises(tmp_path):
    path = tmp_path / "corrupt.mp4"
    path.write_bytes(b"garbage" * 100)
    with pytest.raises((StorageError, ValidationError)):
        extract_samples(path)


# ---------------------------------------------------------------- Phases 3-4: analysis / moment detection
def test_detect_moments_known_footage(tmp_path):
    path = _known_scenes(tmp_path)
    probe = probe_video(path)
    extraction = extract_samples(path, sample_fps=1.0, max_frames=600)
    moments = detect_moments(probe, extraction)
    assert 1 <= len(moments) <= 5
    for moment in moments:
        assert 0 <= moment.start_seconds < moment.end_seconds <= probe.duration_seconds
        assert 0 <= moment.confidence <= 1
        assert 0 <= moment.visual_quality <= 1
        assert 0 <= moment.context_clarity <= 1
        # No semantic event is ever claimed: descriptions are signal-based.
        assert "no vision-language model" in moment.description
        assert "falls" not in moment.description and "jumps" not in moment.description
    # Event identification: both high-motion segments are found (IoU >= 0.5).
    for start, end in ((5, 11), (20, 24)):
        assert any(_iou(m.start_seconds, m.end_seconds, start, end) >= 0.5
                   for m in moments), f"high-motion segment {start}–{end}s not detected"
    high_motion = [m for m in moments if m.event_type == "high_motion_segment"]
    assert len(high_motion) >= 2, \
        "both fast segments (5–11 s and 20–24 s) must be high-motion; " \
        f"got {[ (m.start_seconds, m.end_seconds) for m in high_motion ]}"


def test_detect_moments_drops_black_segment(tmp_path):
    path = _known_black(tmp_path)
    probe = probe_video(path)
    extraction = extract_samples(path, sample_fps=1.0, max_frames=600)
    moments = detect_moments(probe, extraction)
    assert moments, "the moving 4–12 s segment should yield a moment"
    assert all(m.start_seconds >= 4.0 - 0.01 for m in moments), \
        "a moment must not start inside the black segment"


def test_detect_moments_long_video_sample_cap(tmp_path):
    """A long video is sampled adaptively: at most max_frames samples."""
    path = _known_scenes(tmp_path)
    extraction = extract_samples(path, sample_fps=10.0, max_frames=20)
    assert len(extraction.samples) <= 20
    assert extraction.effective_fps < 10.0


# ---------------------------------------------------------------- Phase 5: timestamp verification
def test_verify_adds_lead_in_and_aftermath_and_flags_truncation(tmp_path):
    path = _known_scenes(tmp_path)
    probe = probe_video(path)
    extraction = extract_samples(path, sample_fps=1.0, max_frames=600)
    moments = detect_moments(probe, extraction)
    verified = verify_moments(str(path), probe, moments)
    kept = [v for v in verified if v.verified]
    assert kept, "verification must keep the known moments"
    for item in kept:
        assert 0 <= item.moment.start_seconds < item.moment.end_seconds <= probe.duration_seconds
    first = min(kept, key=lambda v: v.moment.start_seconds)
    assert any("truncated" in issue for issue in first.issues), \
        "a moment starting at the video start must be flagged truncated"


def test_verify_drops_black_moment(tmp_path):
    from app.agents.video_intelligence.analysis import DetectedMoment

    path = _known_black(tmp_path)
    probe = probe_video(path)
    moment = DetectedMoment(start_seconds=0.0, end_seconds=4.0, confidence=0.5,
                            visual_quality=0.1, context_clarity=0.5,
                            description="black segment", event_type="scene_segment",
                            mean_motion=0.0, mean_sharpness=0.0, mean_brightness=0.0)
    verified = verify_moments(str(path), probe, [moment])
    assert len(verified) == 1 and verified[0].verified is False
    assert any("black" in issue for issue in verified[0].issues)


def test_verify_deduplicates_overlapping_moments(tmp_path):
    from app.agents.video_intelligence.analysis import DetectedMoment

    path = _known_scenes(tmp_path)
    probe = probe_video(path)
    moments = [
        DetectedMoment(start_seconds=5.0, end_seconds=11.0, confidence=0.9,
                       visual_quality=0.8, context_clarity=1.0, description="a",
                       event_type="high_motion_segment", mean_motion=20.0,
                       mean_sharpness=100.0, mean_brightness=120.0),
        DetectedMoment(start_seconds=6.0, end_seconds=10.0, confidence=0.5,
                       visual_quality=0.8, context_clarity=1.0, description="b",
                       event_type="high_motion_segment", mean_motion=20.0,
                       mean_sharpness=100.0, mean_brightness=120.0),
    ]
    verified = verify_moments(str(path), probe, moments)
    kept = [v for v in verified if v.verified]
    assert len(kept) == 1
    assert kept[0].moment.confidence == 0.9
    assert any("duplicate" in issue for v in verified if not v.verified for issue in v.issues)


# ---------------------------------------------------------------- gate + service
def test_gate_blocks_unapproved_footage(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "unlicensed-clip")
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        session.add(RightsEvidence(candidate_video_id=candidate_id,
                                   verdict="needs_review", checked_by="rights_agent",
                                   reasons=["no licence published"]))
        session.commit()
        with pytest.raises(ValidationError, match="not approved"):
            require_approved(session, job.id, [candidate_id], purpose="video_intelligence")
    # The service fails closed too: no approved footage at all.
    with pytest.raises(ExternalServiceError, match="no approved footage"):
        VideoIntelligenceService(engine, _settings()).run(job.id)


def test_service_run_end_to_end(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url("by", "4.0"))
    _approve(engine, job.id, candidate_id)
    path = _known_scenes(tmp_path)
    service = VideoIntelligenceService(engine, _settings())
    report = service.run(job.id, footage_paths={candidate_id: path})
    assert report.analyser == ANALYSER
    assert report.metrics["moments"] == 5
    assert report.metrics["analyzed_candidates"] == 1
    assert len(report.moment_ids) == 5
    assert report.candidate_video_ids == [candidate_id]
    assert any("vision-language" in limitation for limitation in report.limitations)
    moments = service.list_moments(job.id)
    assert len(moments) == 5
    assert all(m.labels["analyser"] == ANALYSER for m in moments)
    assert all("no vision-language model" in (m.description or "") for m in moments)
    # Candidate status + footage row updated; thumbnails stored.
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        candidate = session.query(CandidateVideo).filter(CandidateVideo.id == candidate_id).first()
        assert candidate.status == "analyzed"
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        assert footage is not None and footage.storage_path == str(path)
        assert footage.duration_seconds == pytest.approx(30.0, abs=0.5)
    from app.core.config import get_settings
    storage_root = Path(get_settings().storage_root)
    for moment in moments:
        thumb = moment.labels.get("thumbnail")
        assert thumb and (storage_root / thumb).exists(), f"thumbnail missing: {thumb}"
    # Single-moment read works and unknown ids are 404s.
    assert service.get_moment(job.id, moments[0].id).id == moments[0].id
    with pytest.raises(NotFoundError):
        service.get_moment(job.id, "no-such-moment")


def test_service_run_is_idempotent(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url("by", "4.0"))
    _approve(engine, job.id, candidate_id)
    path = _known_scenes(tmp_path)
    service = VideoIntelligenceService(engine, _settings())
    first = service.run(job.id, footage_paths={candidate_id: path})
    second = service.run(job.id, footage_paths={candidate_id: path})
    assert len(first.moment_ids) == len(second.moment_ids) == 5
    # A re-run replaces the moments with fresh rows (new ids) — the
    # stored count must stay 5, never 10.
    assert not set(first.moment_ids) & set(second.moment_ids), \
        "a re-run must replace moments, not keep the old rows"
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(AnalyzedMoment).filter(
            AnalyzedMoment.job_id == job.id).count() == 5


def test_service_run_no_footage_fails_honestly(engine):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url("by", "4.0"))
    _approve(engine, job.id, candidate_id)
    with pytest.raises(ExternalServiceError, match="no usable moments|no local footage"):
        VideoIntelligenceService(engine, _settings()).run(job.id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(AnalyzedMoment).filter(
            AnalyzedMoment.job_id == job.id).count() == 0


def test_service_run_corrupt_footage_fails_honestly(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url("by", "4.0"))
    _approve(engine, job.id, candidate_id)
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"not a video" * 100)
    with pytest.raises(ExternalServiceError, match="no usable moments"):
        VideoIntelligenceService(engine, _settings()).run(
            job.id, footage_paths={candidate_id: corrupt})
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(AnalyzedMoment).filter(
            AnalyzedMoment.job_id == job.id).count() == 0


def test_service_run_all_black_footage_fails_honestly(engine, tmp_path):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url("by", "4.0"))
    _approve(engine, job.id, candidate_id)
    black = _write_footage(tmp_path / "black.mp4", [(0, 10, (0, 0, 0), 0, 0)], 10)
    with pytest.raises(ExternalServiceError, match="no usable moments"):
        VideoIntelligenceService(engine, _settings()).run(
            job.id, footage_paths={candidate_id: black})


def test_service_run_unknown_job_raises(engine):
    with pytest.raises(NotFoundError):
        VideoIntelligenceService(engine, _settings()).run("no-such-job")


def test_service_run_uses_storage_path_without_footage_arg(engine, tmp_path):
    """The orchestrator path: footage resolved from ApprovedFootage.storage_path."""
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url("by", "4.0"))
    _approve(engine, job.id, candidate_id)
    path = _known_scenes(tmp_path)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        footage.storage_path = str(path)
        session.commit()
    report = VideoIntelligenceService(engine, _settings()).run(job.id)
    assert len(report.moment_ids) == 5


# ---------------------------------------------------------------- registry + orchestrator
def test_registry_video_intelligence_ready_with_engine(engine):
    registry = build_default_registry(engine, _settings())
    assert registry.get("video_intelligence").status == "ready"
    assert registry.health("video_intelligence").status == "ready"


def test_orchestrator_runs_real_video_intelligence_then_fails_ranking_honestly(engine, tmp_path):
    """Prompt 05's 501-at-ranking expectation is now a honest
    RANKING failure: the agent exists, but one candidate cannot
    supply five moments from five different sources."""
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url("by", "4.0"))
    path = _known_scenes(tmp_path)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        # Pre-seed the footage path: Rights keeps an existing
        # ApprovedFootage row (and its storage_path) when it approves.
        session.add(ApprovedFootage(job_id=job.id, candidate_video_id=candidate_id,
                                    storage_path=str(path)))
        session.commit()
    settings = _settings(research_provider="test")
    registry = build_default_registry(engine, settings)
    assert registry.get("video_intelligence").status == "ready"
    assert registry.get("ranking").status == "ready"
    runner = WorkflowEngine(engine, settings, registry)
    result = runner.run_job(job.id)
    from app.orchestrator.states import WorkflowStage
    assert WorkflowStage.VIDEO_ANALYSIS in JobService(engine).completed_stages(job.id)
    assert result.status == "failed"
    assert result.error_message is not None
    assert "RANKING" in result.error_message
    assert "five" in result.error_message.lower()
    moments = VideoIntelligenceService(engine, _settings()).list_moments(job.id)
    assert len(moments) == 5


def test_orchestrator_fails_job_when_video_intelligence_has_no_footage(engine):
    """Prompt 04's 501-at-video-analysis expectation is now a
    honest VIDEO_ANALYSIS failure: the agent exists, but no
    approved candidate has a local footage file."""
    job = _new_job(engine)
    _seed_candidate(engine, job.id, "cc-clip", licence=_licence_url("by", "4.0"))
    settings = _settings(research_provider="test")
    registry = build_default_registry(engine, settings)
    runner = WorkflowEngine(engine, settings, registry)
    result = runner.run_job(job.id)
    assert result.status == "failed"
    assert result.error_message is not None
    assert "VIDEO_ANALYSIS" in result.error_message


# ---------------------------------------------------------------- API
def test_api_video_intelligence_endpoints():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        agents = client.get("/agents").json()
        by_id = {a["agent_id"]: a for a in agents}
        assert by_id["video_intelligence"]["status"] == "ready"
        assert client.get("/agents/video_intelligence/health").json()["status"] == "ready"

        created = client.post("/jobs", json={"topic": "TOP 5 CRAZIEST PARKOUR FAILS"})
        job_id = created.json()["id"]
        assert client.get(f"/jobs/{job_id}/moments").json() == []
        assert client.get(f"/jobs/{job_id}/moments/no-such-moment").status_code == 404
        assert client.get("/jobs/no-such-job/moments").status_code == 404


def test_known_footage_script_manifest(tmp_path):
    """The demo footage script produces the ground truth it claims."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, str(root / "scripts" / "make_known_footage.py")],
                          capture_output=True, text=True, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((root / "data" / "known_footage" / "manifest.json").read_text())
    assert manifest["known_scenes.mp4"]["scene_boundaries_seconds"] == BOUNDARIES
    assert manifest["known_scenes.mp4"]["expected_high_motion_moments"] == [[5, 11], [20, 24]]
