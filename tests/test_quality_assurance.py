"""Quality Assurance Agent tests (Prompt 09): file validation,
visual inspection, audio inspection, content validation, rights
re-check, repair requests, the service, the executor, the API
and orchestrator integration.

Valid and defective videos are both tested. Synthetic videos
are generated with FFmpeg/OpenCV; the integration tests use a
real ProductionService render and a crafted passing render
(1080x1920, tone audio, narration marked generated in its
manifest — the only way to get a PASSED verdict without a TTS
provider configured).
"""
import json
import subprocess

import cv2
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.creative_director.service import CreativeDirectorService
from app.agents.production.service import ProductionService
from app.agents.quality_assurance.agent import make_quality_assurance_executor
from app.agents.quality_assurance.audio import analyse_audio
from app.agents.quality_assurance.content import validate_content
from app.agents.quality_assurance.repairs import build_repair_requests
from app.agents.quality_assurance.rights_check import validate_rights
from app.agents.quality_assurance.service import QAService
from app.agents.quality_assurance.technical import validate_file
from app.agents.quality_assurance.visual import SegmentWindow, inspect_visual
from app.orchestrator.contracts import OrchestratorTask
from app.agents.ranking.service import RankingService
from app.agents.rights.service import RightsService
from app.core.config import Settings
from app.core.errors import ExternalServiceError, NotFoundError
from app.models.models import (
    AnalyzedMoment, ApprovedFootage, CandidateVideo, RenderedVideo,
    RightsEvidence, Source,
)
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.schemas.schemas import JobCreate

FPS, W, H = 15, 160, 120
TOPIC = "TOP 5 CRAZIEST PARKOUR FAILS"
ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


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
            "tts_enabled": False}
    base.update(overrides)
    return Settings(**base)


def _new_job(engine, topic=TOPIC):
    return JobService(engine).create_job(JobCreate(topic=topic))


def _licence_url(variant: str = "by", version: str = "4.0") -> str:
    return f"https://creativecommons.org/licenses/{variant}/{version}/"


def _seed_candidate(engine, job_id, external_id, *, title="Parkour Fails",
                    creator="MACSD", licence=None):
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
            duration_seconds=30.0, creator=creator, licence=licence)
        session.add(candidate)
        session.commit()
        return candidate.id


def _labels(*, motion=20.0):
    return {
        "analyser": "heuristic_frame_signals_v1",
        "event_type": "high_motion_segment",
        "confidence": 0.8,
        "visual_quality": 0.7,
        "context_clarity": 1.0,
        "mean_motion": motion,
        "mean_sharpness": 200.0,
        "mean_brightness": 120.0,
        "frame_refs": ["t1.0", "t2.0"],
        "verification": {"verified": True, "issues": []},
        "probe": {"duration_seconds": 30.0, "width": 1280, "height": 720,
                  "fps": 30.0, "video_codec": "mpeg4", "has_audio": True,
                  "audio_codec": "aac"},
    }


def _seed_moment(engine, job_id, candidate_id, start, end, labels):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        row = AnalyzedMoment(
            job_id=job_id, candidate_video_id=candidate_id,
            start_seconds=start, end_seconds=end,
            description="signal-based moment", score=labels.get("confidence", 0.5),
            labels=labels)
        session.add(row)
        session.commit()
        return row.id


def _seed_ranked_five(engine, job_id, *, moment_seconds: float = 6.0):
    seeded = {}
    for i, motion in enumerate((30.0, 25.0, 20.0, 15.0, 10.0), start=1):
        candidate_id = _seed_candidate(
            engine, job_id, f"cc-clip-{i}", title=f"Parkour Fails {i}",
            creator=f"Creator {i}", licence=_licence_url())
        moment_id = _seed_moment(engine, job_id, candidate_id, 0.0, moment_seconds,
                                 _labels(motion=motion))
        seeded[i] = (candidate_id, moment_id)
    RightsService(engine, _settings()).run(job_id)
    RankingService(engine, _settings()).run(job_id)
    CreativeDirectorService(engine, _settings()).run(job_id)
    return seeded


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


def _ffmpeg_video(path, *, width=1080, height=1920, duration=6.0, source="testsrc2",
                  audio="sine", volume=None, audio_file=None):
    """Generate a small synthetic MP4 with FFmpeg lavfi sources."""
    video = f"{source}=size={width}x{height}:rate=30:duration={duration}"
    if source.startswith("color"):
        video = f"color=c={source[5:]}:size={width}x{height}:rate=30:duration={duration}"
    if audio_file is not None:
        audio_args = ["-i", str(audio_file)]
    else:
        if audio == "sine":
            audio_src = f"sine=frequency=440:duration={duration}"
        elif audio == "silent":
            audio_src = f"anullsrc=channel_layout=stereo:sample_rate=48000:duration={duration}"
        else:
            audio_src = audio
        audio_args = ["-f", "lavfi", "-i", audio_src]
    args = ["ffmpeg", "-y", "-f", "lavfi", "-i", video] + audio_args + [
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30"]
    if volume:
        args += ["-af", f"volume={volume}"]
    args += ["-c:a", "aac", "-shortest", str(path)]
    subprocess.run(args, check=True, capture_output=True, timeout=300)
    return path


def _tone_wav(path, duration=5.0):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={duration}",
                    "-c:a", "pcm_s16le", str(path)],
                   check=True, capture_output=True, timeout=60)
    return path


def _clips_and_moments(seeded, *, moment_seconds=6.0):
    """EDL-style clips (#5 first) and matching moment records."""
    clips, moments = [], {}
    for rank in (5, 4, 3, 2, 1):
        candidate_id, moment_id = seeded[rank]
        clips.append({"rank": rank, "moment_id": moment_id,
                      "candidate_video_id": candidate_id,
                      "start_seconds": 0.0, "end_seconds": moment_seconds,
                      "duration_seconds": moment_seconds})
        moments[moment_id] = {"start_seconds": 0.0, "end_seconds": moment_seconds}
    return clips, moments


def _ranking(seeded):
    return [{"rank": r, "moment_id": seeded[r][1], "candidate_video_id": seeded[r][0]}
            for r in (1, 2, 3, 4, 5)]


def _craft_manifest(engine, job_id, edl, *, narration_status="generated",
                    music_file=None, sfx_file=None):
    """A production manifest for a crafted render, built from the job's EDL."""
    clips = list(edl["clips"])
    intro_s = float(edl["intro_seconds"])
    outro_s = float(edl["outro_seconds"])
    transition_s = float(edl["transition_seconds"])
    segments = [{"kind": "intro", "rank": None, "moment_id": None,
                 "candidate_video_id": None, "duration_seconds": intro_s,
                 "timeline_start_seconds": 0.0, "file_name": "seg-00-intro.mp4"}]
    cursor = intro_s
    for clip in clips:
        segments.append({"kind": "transition", "rank": None, "moment_id": None,
                         "candidate_video_id": None, "duration_seconds": transition_s,
                         "timeline_start_seconds": cursor, "file_name": "gap.mp4"})
        cursor += transition_s
        segments.append({"kind": "countdown_moment", "rank": clip["rank"],
                         "moment_id": clip["moment_id"],
                         "candidate_video_id": clip["candidate_video_id"],
                         "duration_seconds": float(clip["duration_seconds"]),
                         "timeline_start_seconds": float(clip["timeline_start_seconds"]),
                         "file_name": "clip.mp4", "subject_aware": True})
        cursor += float(clip["duration_seconds"])
    segments.append({"kind": "transition", "rank": None, "moment_id": None,
                     "candidate_video_id": None, "duration_seconds": transition_s,
                     "timeline_start_seconds": cursor, "file_name": "gap.mp4"})
    cursor += transition_s
    segments.append({"kind": "outro", "rank": None, "moment_id": None,
                     "candidate_video_id": None, "duration_seconds": outro_s,
                     "timeline_start_seconds": cursor, "file_name": "outro.mp4"})
    attributions = [{
        "candidate_video_id": a.candidate_video_id,
        "attribution_text": a.attribution_text,
        "attribution_required": a.attribution_required,
        "licence_name": a.licence_name,
    } for a in RightsService(engine, _settings()).list_attribution(job_id)]
    return {
        "job_id": job_id, "generator": "ffmpeg_production_v1",
        "duration_seconds": float(edl["total_duration_seconds"]),
        "segments": segments,
        "narration": {"script_words": 100, "estimated_seconds": 40.0,
                      "provider": "test", "status": narration_status,
                      "detail": "test fixture voice", "audio_path": None},
        "music": {"kind": "music",
                  "status": "included" if music_file else "not_included_no_licensed_library",
                  "file_path": str(music_file) if music_file else None,
                  "file_name": "tone.wav" if music_file else None},
        "sfx_chime": {"kind": "sfx",
                      "status": "included" if sfx_file else "not_included_no_licensed_library",
                      "file_path": str(sfx_file) if sfx_file else None},
        "sfx_whoosh": {"kind": "sfx",
                       "status": "included" if sfx_file else "not_included_no_licensed_library",
                       "file_path": str(sfx_file) if sfx_file else None},
        "sfx_mixed": bool(sfx_file),
        "captions": {"lines": 23, "ass_files": []},
        "attributions": attributions,
        "validation": {"valid": True},
        "metrics": {"total_duration_seconds": float(edl["total_duration_seconds"])},
    }


def _insert_rendered(engine, job_id, edl_id, file_path, duration):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        row = RenderedVideo(job_id=job_id, edit_instruction_id=edl_id,
                            file_path=str(file_path), duration_seconds=duration,
                            width=1080, height=1920, status="ready")
        session.add(row)
        session.commit()
        return row.id


# ---------------------------------------------------------------- Phase 1: file validation
def test_validate_file_passes_on_valid_video(tmp_path):
    path = _ffmpeg_video(tmp_path / "valid.mp4", duration=6.0)
    result = validate_file(path, expected_duration_seconds=6.0)
    assert result.valid is True
    assert result.width == 1080 and result.height == 1920
    assert result.fps == pytest.approx(30.0, abs=0.1)
    assert result.video_codec == "h264" and result.audio_codec == "aac"
    assert result.has_audio is True
    assert result.decode_errors == []
    assert result.issues == []


def test_validate_file_rejects_wrong_resolution(tmp_path):
    path = _ffmpeg_video(tmp_path / "small.mp4", width=720, height=1280, duration=6.0)
    result = validate_file(path, expected_duration_seconds=6.0)
    assert result.valid is False
    assert any("720x1280" in issue for issue in result.issues)


def test_validate_file_rejects_wrong_duration(tmp_path):
    path = _ffmpeg_video(tmp_path / "short.mp4", duration=3.0)
    result = validate_file(path, expected_duration_seconds=6.0)
    assert result.valid is False
    assert any("duration" in issue for issue in result.issues)


def test_validate_file_rejects_corrupt_file(tmp_path):
    path = tmp_path / "corrupt.mp4"
    path.write_bytes(b"this is not a video at all, just text" * 10)
    result = validate_file(path)
    assert result.valid is False
    assert result.issues


def test_validate_file_missing_file(tmp_path):
    result = validate_file(tmp_path / "nope.mp4")
    assert result.valid is False
    assert any("does not exist" in issue for issue in result.issues)


# ---------------------------------------------------------------- Phase 3: visual inspection
def test_visual_black_frames_detected(tmp_path):
    path = _ffmpeg_video(tmp_path / "black.mp4", source="colorblack", duration=6.0)
    result = inspect_visual(path, [])
    assert result.black_samples, "a black video must produce black samples"
    assert result.issues, "black frames outside transitions must be an issue"


def test_visual_frozen_moment_detected(tmp_path):
    path = _ffmpeg_video(tmp_path / "static.mp4", source="color0x28a028", duration=6.0)
    windows = [SegmentWindow(kind="countdown_moment", rank=5, start=0.0, duration=6.0)]
    result = inspect_visual(path, windows)
    assert result.frozen_segments == ["countdown_moment#5"]
    assert any("frozen" in issue for issue in result.issues)


def test_visual_moving_video_passes(tmp_path):
    path = _write_footage(tmp_path / "moving.mp4", 7, (40, 160, 40), duration=10.0)
    windows = [SegmentWindow(kind="countdown_moment", rank=5, start=0.0, duration=10.0)]
    result = inspect_visual(path, windows)
    assert result.issues == []
    assert result.black_samples == []
    assert result.not_tested, "semantic / OCR checks must be recorded as not tested"


def test_visual_edge_fade_frames_are_not_black_frames(tmp_path):
    """Regression: Production fades the intro in and the outro out
    over 0.5 s, so the first and last sampled frames are legitimately
    near-black and must not fail QA. A 5.4 s video that is black only
    in its first and last second passes the black-frame check."""
    path = tmp_path / "edge-fade.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for i in range(int(5.4 * FPS)):
        if i < FPS or i >= 74:
            # Black only inside the 0.5 s fade zones at both edges:
            # first second (sampled at t=0) and last 0.5 s (sampled
            # at t=5, i.e. 5.0 > 5.4 - 0.5).
            frame = np.zeros((H, W, 3), dtype=np.uint8)
        else:
            frame = np.full((H, W, 3), (40, 160, 40), dtype=np.uint8)
            x = _pingpong(i, 7, W - 50)
            cv2.rectangle(frame, (x, H // 2 - 25), (x + 50, H // 2 + 25), (0, 0, 0), -1)
        writer.write(frame)
    writer.release()
    result = inspect_visual(path, [])
    assert result.black_samples == []
    assert result.issues == []


def test_visual_transition_gaps_are_not_black_frames(tmp_path):
    # A video that is black only inside a known transition window.
    path = _ffmpeg_video(tmp_path / "gap.mp4", source="colorblack", duration=2.0)
    windows = [SegmentWindow(kind="transition", rank=None, start=0.0, duration=2.0)]
    result = inspect_visual(path, windows)
    assert result.black_samples == []


# ---------------------------------------------------------------- Phase 4: audio inspection
def test_audio_silent_track_is_effectively_silent(tmp_path):
    path = _ffmpeg_video(tmp_path / "silent.mp4", duration=6.0, audio="silent")
    result = analyse_audio(path, duration_seconds=6.0)
    assert result.effectively_silent is True
    assert result.mean_volume_db is not None and result.mean_volume_db <= -70.0
    assert any("silent" in issue for issue in result.issues)


def test_audio_tone_is_not_silent_and_no_clipping(tmp_path):
    path = _ffmpeg_video(tmp_path / "tone.mp4", duration=6.0)
    result = analyse_audio(path, duration_seconds=6.0)
    assert result.effectively_silent is False
    assert result.clipping is False
    assert result.issues == []


def test_audio_clipping_detected(tmp_path):
    # Full-scale square wave: hits 0 dBFS, which is what the
    # clipping detector keys on.
    import wave

    wav = tmp_path / "square.wav"
    rate = 48000
    t = np.arange(int(6.0 * rate)) / rate
    samples = (np.sign(np.sin(2 * np.pi * 440 * t)) * 32767).astype("<i2")
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    path = _ffmpeg_video(tmp_path / "loud.mp4", duration=6.0, audio_file=wav)
    result = analyse_audio(path, duration_seconds=6.0)
    assert result.clipping is True
    assert any("clip" in issue for issue in result.issues)


# ---------------------------------------------------------------- Phase 2: content validation
def _content_inputs(engine, job_id, seeded, *, moment_seconds=6.0, total=30.0):
    clips, moments = _clips_and_moments(seeded, moment_seconds=moment_seconds)
    return {"clips": clips, "moments": moments, "ranking": _ranking(seeded),
            "edl_total_seconds": total, "rendered_duration_seconds": total,
            "narration_script": "Number 5. A sharp segment. Number 4. A steady segment. "
                                "Number 3. Clear from start to finish. Number 2. A sharp segment. "
                                "Number 1. The highest-scoring segment.",
            "moment_narrations": ["Number 5. A sharp segment.",
                                  "Number 4. A steady segment.",
                                  "Number 3. Clear from start to finish.",
                                  "Number 2. A sharp segment.",
                                  "Number 1. The highest-scoring segment."],
            "caption_plan": []}


def test_content_validates_five_moments_in_order(engine):
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    result = validate_content(**_content_inputs(engine, job.id, seeded))
    assert result.issues == []
    assert result.clip_count == 5
    assert result.distinct_sources == 5
    assert result.duration_matches_edl is True
    assert result.not_tested, "semantic accuracy must be not tested, never passed"


def test_content_rejects_wrong_order_and_duplicates(engine):
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    inputs = _content_inputs(engine, job.id, seeded)
    inputs["clips"] = list(reversed(inputs["clips"]))
    result = validate_content(**inputs)
    assert any("order" in issue for issue in result.issues)
    inputs = _content_inputs(engine, job.id, seeded)
    inputs["clips"][1]["candidate_video_id"] = inputs["clips"][0]["candidate_video_id"]
    result = validate_content(**inputs)
    assert any("different source" in issue for issue in result.issues)


def test_content_rejects_forbidden_narration_word(engine):
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    inputs = _content_inputs(engine, job.id, seeded)
    inputs["moment_narrations"] = ["Number 5. He falls off the wall."]
    result = validate_content(**inputs)
    assert any("misleading narration" in issue for issue in result.issues)


def test_content_rejects_timestamp_mismatch(engine):
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    inputs = _content_inputs(engine, job.id, seeded)
    inputs["clips"][0]["end_seconds"] = 9.0
    result = validate_content(**inputs)
    assert any("boundaries" in issue for issue in result.issues)


def test_content_hook_may_quote_topic_but_moment_narration_may_not(engine):
    # The hook quoting "parkour fails" is the user's topic, not a
    # claim about a clip — it must not fail the forbidden-word check.
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    inputs = _content_inputs(engine, job.id, seeded)
    inputs["narration_script"] = ("TOP 5 CRAZIEST PARKOUR FAILS. "
                                  + inputs["narration_script"])
    result = validate_content(**inputs)
    assert result.issues == []


# ---------------------------------------------------------------- Phase 5: rights re-check
def test_rights_recheck_fails_unapproved_candidate(engine):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "no-licence-clip", licence=None)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        session.add(RightsEvidence(candidate_video_id=candidate_id,
                                   verdict="needs_review",
                                   licence=None, checked_by="rights_agent"))
        session.commit()
        result = validate_rights(session, job.id, [candidate_id], [])
    assert result.issues
    assert result.not_approved == [candidate_id]
    assert any("needs_review" in issue for issue in result.issues)


def test_rights_recheck_passes_approved_with_attribution(engine):
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        attributions = RightsService(engine, _settings()).list_attribution(job.id)
        manifest_attributions = [{
            "candidate_video_id": a.candidate_video_id,
            "attribution_text": a.attribution_text,
            "attribution_required": a.attribution_required,
            "licence_name": a.licence_name} for a in attributions]
        candidate_ids = [seeded[r][0] for r in (1, 2, 3, 4, 5)]
        result = validate_rights(session, job.id, candidate_ids, manifest_attributions,
                                 manifest={"music": {"status": "not_included_no_licensed_library"},
                                           "sfx_chime": {"status": "not_included_no_licensed_library"}})
    assert result.issues == []
    assert len(result.approved) == 5
    assert any("no licensed background music" in w for w in result.warnings)


# ---------------------------------------------------------------- Phase 6: repair requests
def test_repair_requests_route_to_owning_component():
    requests = build_repair_requests([
        "resolution is 720x1280, expected 1080x1920",
        "candidate abc is not approved for use (latest verdict: needs_review)",
        "narration audio is not generated: no TTS provider is configured",
        "something nobody has a rule for",
    ])
    by_issue = {r.issue: r for r in requests}
    assert by_issue["resolution is 720x1280, expected 1080x1920"].responsible_component == "production"
    assert by_issue["resolution is 720x1280, expected 1080x1920"].target_stage == "VIDEO_PRODUCTION"
    assert by_issue["candidate abc is not approved for use (latest verdict: needs_review)"].responsible_component == "rights"
    assert by_issue["narration audio is not generated: no TTS provider is configured"].responsible_component == "config"
    assert by_issue["something nobody has a rule for"].target_stage == "manual_review"
    assert all(r.retry_limit == 2 for r in requests)


# ---------------------------------------------------------------- Service: crafted passing render
@pytest.fixture(scope="session")
def pass_job(engine, tmp_path_factory):
    """A job with a crafted, valid render: 1080x1920 testsrc2 with
    tone audio, manifest saying narration was generated and music
    is included. The crafted way to reach PASSED; a real render
    with a generated narration voice would reach it the same way,
    but the full-bleed style has the voiceover off by design, so
    no real render passes on narration (see the voiceover-off
    test in test_production.py)."""
    tmp = tmp_path_factory.mktemp("qa-pass")
    job = _new_job(engine)
    _seed_ranked_five(engine, job.id)
    creative = CreativeDirectorService(engine, _settings())
    edl = creative.get_edl(job.id)
    tone = _tone_wav(tmp / "tone.wav")
    video = _ffmpeg_video(tmp / "rankflow-short.mp4", duration=30.0)
    manifest = _craft_manifest(engine, job.id, edl.edl, music_file=tone, sfx_file=tone)
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _insert_rendered(engine, job.id, edl.id, video, 30.0)
    return job.id


def test_service_passes_crafted_render(engine, pass_job):
    report = QAService(engine, _settings()).run(pass_job)
    assert report.verdict == "pass", f"issues: {report.issues}"
    assert report.status == "PASSED"
    assert report.issues == []
    assert report.checks, "report must carry the per-check results"
    assert any(c["name"] == "semantic_narration_accuracy" and c["verdict"] == "not_tested"
               for c in report.checks)
    assert report.repair_requests == []
    stored = QAService(engine, _settings()).get_report(pass_job)
    assert stored.verdict == "pass"


@pytest.fixture(scope="session")
def prod_job(engine, tmp_path_factory):
    """A job rendered by the real ProductionService (3s moments,
    ~15s Short). The voiceover is off by design in the full-bleed
    style, so QA must say needs_review — narration missing, audio
    effectively silent — never pass. (2s moments would make a 10s
    timeline, which cannot fit the 27-word hook + closing alone,
    so the Creative Director rightly refuses the plan.)"""
    tmp = tmp_path_factory.mktemp("qa-prod")
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id, moment_seconds=3.0)
    colours = [(40, 160, 40), (40, 40, 180), (40, 200, 220), (120, 40, 20), (140, 140, 140)]
    footage = {}
    for rank, (candidate_id, _) in seeded.items():
        path = _write_footage(tmp / f"clip-{rank}.mp4", 8 - rank, colours[rank - 1],
                              duration=20.0)
        footage[candidate_id] = str(path)
    ProductionService(engine, _settings()).run(job.id, footage_paths=footage)
    return job.id


def test_service_needs_review_on_real_production_render(engine, prod_job):
    report = QAService(engine, _settings()).run(prod_job)
    assert report.verdict == "needs_review", f"issues: {report.issues}"
    assert report.status == "NEEDS_REVIEW"
    assert any("narration audio is not generated" in issue for issue in report.issues)
    assert any("effectively silent" in issue for issue in report.issues)
    assert report.technical_validation["valid"] is True
    assert report.content_validation["issues"] == []
    assert report.rights_validation["issues"] == []
    assert report.repair_requests, "needs_review must carry repair requests"
    assert any(r["responsible_component"] == "config" for r in report.repair_requests)


def test_executor_returns_needs_review_for_production_render(engine, prod_job):
    executor = make_quality_assurance_executor(engine, _settings())
    task = OrchestratorTask(
        task_id=f"{prod_job}:QUALITY_ASSURANCE:1", job_id=prod_job,
        agent_id="quality_assurance", expected_output_schema="QAOutput",
        deadline=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        input={}).model_dump(mode="json")
    result = executor(task)
    assert result["verdict"] == "needs_review"
    assert result["issues"]


def test_executor_raises_on_failed_qa(engine, tmp_path_factory):
    """A 720x1280 render fails QA, so the executor raises — the
    orchestrator then fails the job instead of presenting it."""
    tmp = tmp_path_factory.mktemp("qa-fail")
    job = _new_job(engine)
    _seed_ranked_five(engine, job.id)
    creative = CreativeDirectorService(engine, _settings())
    edl = creative.get_edl(job.id)
    tone = _tone_wav(tmp / "tone.wav")
    video = _ffmpeg_video(tmp / "small.mp4", width=720, height=1280, duration=30.0)
    manifest = _craft_manifest(engine, job.id, edl.edl, music_file=tone, sfx_file=tone)
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _insert_rendered(engine, job.id, edl.id, video, 30.0)
    executor = make_quality_assurance_executor(engine, _settings())
    from datetime import datetime, timezone
    task = OrchestratorTask(
        task_id=f"{job.id}:QUALITY_ASSURANCE:1", job_id=job.id,
        agent_id="quality_assurance", expected_output_schema="QAOutput",
        deadline=datetime.now(timezone.utc), input={}).model_dump(mode="json")
    with pytest.raises(ExternalServiceError, match="Quality Assurance FAILED"):
        executor(task)
    # The failed run is persisted, with repair requests and the
    # attempts-exhaustion rule: run 2 still gets repair requests
    # (the limit is 2 attempts), but run 3 issues none and says
    # the attempts are exhausted, so a job cannot loop forever.
    service = QAService(engine, _settings())
    first = service.get_report(job.id)
    assert first.verdict == "fail"
    assert first.repair_requests
    second = service.run(job.id)
    assert second.verdict == "fail"
    assert second.repair_requests
    third = service.run(job.id)
    assert third.verdict == "fail"
    assert third.repair_requests == []
    assert any("repair attempts exhausted" in issue for issue in third.issues)


def test_service_unknown_job_raises(engine):
    with pytest.raises(NotFoundError):
        QAService(engine, _settings()).run("no-such-job")


def test_service_no_render_raises(engine):
    job = _new_job(engine)
    with pytest.raises(ExternalServiceError, match="no rendered video"):
        QAService(engine, _settings()).run(job.id)


# ---------------------------------------------------------------- Demo render
def test_demo_render_inspects_needs_review(engine):
    """The shipped demo render (74.07s, H.264 1080x1920 @30fps +
    AAC): technically and content-wise valid, but narration was
    never generated and the audio is effectively silent — so
    NEEDS_REVIEW, never PASSED."""
    service = QAService(engine, _settings())
    manifest = json.loads((ROOT / "data" / "outputs"
                           / "rankflow-short-demo-manifest.json").read_text())
    report = service.inspect_file(
        ROOT / "data" / "outputs" / "rankflow-short-demo.mp4", manifest=manifest)
    assert report.technical_validation["valid"] is True
    assert report.technical_validation["duration_seconds"] == pytest.approx(74.07, abs=0.6)
    assert report.content_validation["issues"] == []
    assert report.visual_validation["sampled_frames"] == 75
    assert report.audio_validation["effectively_silent"] is True
    assert report.audio_validation["mean_volume_db"] is not None
    assert report.audio_validation["mean_volume_db"] <= -70.0
    assert report.verdict == "needs_review"
    assert any("narration audio is not generated" in issue for issue in report.issues)
    assert any("effectively silent" in issue for issue in report.issues)


# ---------------------------------------------------------------- Registry, API, orchestrator
def test_registry_qa_ready_with_engine(engine):
    registry = build_default_registry(engine, _settings())
    assert registry.get("quality_assurance").status == "ready"
    assert registry.health("quality_assurance").status == "ready"


def test_api_qa_endpoints(engine, prod_job):
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        health = client.get("/agents/quality_assurance/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ready"
        report = client.get(f"/jobs/{prod_job}/qa")
        assert report.status_code == 200
        assert report.json()["verdict"] == "needs_review"
        rerun = client.post(f"/jobs/{prod_job}/qa/run")
        assert rerun.status_code == 200
        assert rerun.json()["verdict"] == "needs_review"
        missing = client.get("/jobs/no-such-job/qa")
        assert missing.status_code == 404


def test_orchestrator_completes_full_pipeline_with_qa(engine, tmp_path):
    """A plain job run now completes: research -> rights -> video
    intelligence -> ranking -> creative director -> production ->
    quality assurance -> COMPLETED / awaiting_approval, with the
    QA report saying needs_review (TTS is disabled for this test,
    so there is no narration voice)."""
    job = _new_job(engine)
    colours = [(40, 160, 40), (40, 40, 180), (40, 200, 220), (120, 40, 20), (140, 140, 140)]
    sessions = sessionmaker(bind=engine)
    for i, (speed, colour) in enumerate(zip((7, 6, 5, 4, 3), colours), start=1):
        candidate_id = _seed_candidate(
            engine, job.id, f"cc-clip-{i}", title=f"Parkour Fails {i}",
            creator=f"Creator {i}", licence=_licence_url())
        path = _write_footage(tmp_path / f"clip-{i}.mp4", speed, colour)
        with sessions() as session:
            session.add(ApprovedFootage(job_id=job.id, candidate_video_id=candidate_id,
                                        storage_path=str(path)))
            session.commit()
    settings = _settings(research_provider="test", orchestrator_timeout_seconds=300.0)
    registry = build_default_registry(engine, settings)
    assert registry.get("quality_assurance").status == "ready"
    runner = WorkflowEngine(engine, settings, registry)
    finished = runner.run_job(job.id)
    assert finished.status == "awaiting_approval"
    from app.orchestrator.states import WorkflowStage
    assert WorkflowStage.QUALITY_ASSURANCE in JobService(engine).completed_stages(job.id)
    assert JobService(engine).get_stage(job.id).value == "COMPLETED"
    report = QAService(engine, _settings()).get_report(job.id)
    assert report.verdict == "needs_review"
