"""Video Production Agent tests (Prompt 08): reframing, clip
extraction, captions, graphics, narration, audio, the FFmpeg
render pipeline, the rights gate, idempotency, the API and
orchestrator integration.

The full-render tests use real footage written with OpenCV and a
real Creative Director plan (seeded ranking, like the Prompt 07
tests) — the rendered MP4 is verified with ffprobe, not assumed.
"""
import json
import subprocess

import cv2
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.creative_director.service import CreativeDirectorService
from app.agents.production.audio import find_audio_asset, find_music
from app.agents.production.captions import build_ass, emphasise_caption
from app.agents.production.extract import extract_clip, render_silent_segment
from app.agents.production.graphics import (
    clip_graphics_filters, drawtext_filter,
)
from app.agents.production.narration import (
    detect_narration_provider, generate_narration_audio,
)
from app.agents.production.reframe import (
    compute_vertical_crop, motion_centroid_x, reframe_filter,
)
from app.agents.production.render import (
    concat_segments, decode_check, validate_render,
)
from app.agents.production.service import ProductionService
from app.agents.ranking.service import RankingService
from app.agents.rights.service import RightsService
from app.core.config import Settings
from app.core.errors import ExternalServiceError, NotFoundError, ValidationError
from app.models.models import (
    AnalyzedMoment, ApprovedFootage, CandidateVideo, RankingResult,
    RenderedVideo, RightsEvidence, Source,
)
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.orchestrator.states import WorkflowStage
from app.schemas.schemas import JobCreate
from app.agents.video_intelligence.probe import probe_video

FPS, W, H = 15, 160, 120
TOPIC = "TOP 5 CRAZIEST PARKOUR FAILS"
KNOWN_SCENES = "data/known_footage/known_scenes.mp4"


def _settings(**overrides) -> Settings:
    # TTS is disabled by default in these tests: the no-voice path
    # must stay covered, and a live Edge TTS attempt would make
    # verdicts depend on whether speech.platform.bing.com is
    # reachable from the machine running the suite (it is blocked
    # on this VM). The generated-voice path is covered by the
    # stubbed-narration tests below, which enable TTS explicitly.
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 30.0,
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


def _seed_ranked_five(engine, job_id):
    seeded = {}
    for i, motion in enumerate((30.0, 25.0, 20.0, 15.0, 10.0), start=1):
        candidate_id = _seed_candidate(
            engine, job_id, f"cc-clip-{i}", title=f"Parkour Fails {i}",
            creator=f"Creator {i}", licence=_licence_url())
        moment_id = _seed_moment(engine, job_id, candidate_id, 0.0, 6.0,
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


def _write_static_footage(path, colour=(40, 160, 40), duration: float = 10.0):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for _ in range(int(duration * FPS)):
        writer.write(np.full((H, W, 3), colour, dtype=np.uint8))
    writer.release()
    return path


def _footage_map(engine, job_id, seeded, tmp_path):
    """Write one footage file per seeded candidate and return the map."""
    colours = [(40, 160, 40), (40, 40, 180), (40, 200, 220), (120, 40, 20), (140, 140, 140)]
    footage = {}
    for rank, (candidate_id, _) in seeded.items():
        path = _write_footage(tmp_path / f"clip-{rank}.mp4", 8 - rank,
                              colours[rank - 1])
        footage[candidate_id] = str(path)
    return footage


# ---------------------------------------------------------------- Phase 3: reframing
def test_compute_vertical_crop_wide_source_centres():
    window = compute_vertical_crop(1920, 1080)
    assert window.width == 607 or window.width == 606  # 1080 * 9/16, even
    assert window.height == 1080
    # x is rounded down to an even pixel, so it is the centred
    # offset or one pixel left of it.
    assert window.x in ((1920 - window.width) // 2, (1920 - window.width) // 2 - 1)
    assert window.x % 2 == 0


def test_compute_vertical_crop_tall_source():
    window = compute_vertical_crop(720, 1280)
    assert window.width == 720
    assert window.height == 1280  # 720 * 16/9 = 1280
    assert window.y == 0


def test_compute_vertical_crop_subject_is_clamped_inside_frame():
    window = compute_vertical_crop(1920, 1080, subject_x=0.99)
    assert window.subject_aware is True
    assert 0 <= window.x <= 1920 - window.width
    assert window.x + window.width <= 1920
    left = compute_vertical_crop(1920, 1080, subject_x=0.01)
    assert left.x == 0
    assert left.x < window.x  # subject position moves the window


def test_reframe_filter_is_1080x1920_at_30fps():
    chain, window = reframe_filter(320, 240)
    assert "scale=1080:1920" in chain
    assert "fps=30" in chain
    assert chain.startswith("crop=")


def test_motion_centroid_finds_moving_subject(tmp_path):
    path = _write_footage(tmp_path / "moving.mp4", 7, (40, 160, 40), duration=10.0)
    centroid = motion_centroid_x(path, 0.0, 10.0)
    assert centroid is not None
    assert 0.0 <= centroid <= 1.0


def test_motion_centroid_returns_none_for_static_shot(tmp_path):
    path = _write_static_footage(tmp_path / "static.mp4")
    assert motion_centroid_x(path, 0.0, 10.0) is None


# ---------------------------------------------------------------- Phase 2: extraction
def test_extract_clip_reencodes_to_1080x1920(tmp_path):
    out = tmp_path / "clip.mp4"
    extracted = extract_clip(KNOWN_SCENES, 5.0, 11.0, out)
    assert extracted.duration_seconds == pytest.approx(6.0, abs=0.3)
    assert extracted.width == 1080 and extracted.height == 1920
    assert extracted.video_codec == "h264"
    assert extracted.has_audio is True  # silent track when source has none
    assert extracted.audio_codec == "aac"
    probe = probe_video(out)
    assert probe.width == 1080 and probe.height == 1920


def test_extract_clip_rejects_end_past_duration(tmp_path):
    with pytest.raises(ValidationError, match="past the source duration"):
        extract_clip(KNOWN_SCENES, 25.0, 35.0, tmp_path / "clip.mp4")


def test_extract_clip_rejects_bad_range_and_missing_file(tmp_path):
    with pytest.raises(ValidationError):
        extract_clip(KNOWN_SCENES, 5.0, 5.0, tmp_path / "clip.mp4")
    with pytest.raises(ValidationError):
        extract_clip(KNOWN_SCENES, -1.0, 2.0, tmp_path / "clip.mp4")
    from app.core.errors import StorageError
    with pytest.raises(StorageError):
        extract_clip(tmp_path / "nope.mp4", 0.0, 2.0, tmp_path / "clip.mp4")


def test_fullbleed_clip_frame_is_full_bleed_video(tmp_path):
    """A full-bleed clip segment: green footage fills the whole
    1080x1920 frame (no dark top zone, no separate video area),
    with a solid black title bar across the top and the left-side
    numbers + bottom caption drawn on top of the footage."""
    footage = _write_footage(tmp_path / "source.mp4", 5, (40, 160, 40),
                             duration=10.0)
    items = [(5, "A sharp, fast segment"),
             (4, "A steady segment"),
             (3, "A dark segment"),
             (2, "A bright segment"),
             (1, "A sharp segment")]
    gfx, _ = clip_graphics_filters(
        rank=5, topic="TOP 5 CRAZIEST PARKOUR FAILS", list_items=items,
        source_label="Parkour Fails 5 by Creator 5",
        attribution_text=None, attribution_required=False,
        duration_seconds=6.0, work_dir=tmp_path,
        font_file="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        bottom_caption="A sharp, fast segment")
    clip = extract_clip(footage, 0.0, 6.0, tmp_path / "seg.mp4",
                        extra_video_filters=gfx,
                        target_width=1080, target_height=1920)
    assert clip.width == 1080 and clip.height == 1920
    cap = cv2.VideoCapture(str(tmp_path / "seg.mp4"))
    ok, frame = cap.read()
    cap.release()
    assert ok, "could not read the rendered segment"
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    top = gray[:200, :]       # solid black title bar
    assert float(np.mean(top)) < 60
    assert float(np.std(top)) > 2.0  # title text is visible
    mid = gray[300:1500, :]   # footage fills the middle of the frame
    assert float(np.mean(mid)) > 60  # green footage, clearly brighter
    # The numbers area (left edge) has text pixels.
    numbers = gray[260:1400, 0:280]
    assert float(np.std(numbers)) > 2.0
    # The bottom caption area has text pixels.
    bottom = gray[1650:1900, :]
    assert float(np.std(bottom)) > 2.0


def test_service_render_has_fullbleed_layout(engine, tmp_path):
    """Full service render in the full-bleed style: 1080x1920,
    five clips only (30s), manifest records the style, and a
    sampled frame inside clip #5 shows the black title bar on
    top with footage filling the rest of the frame."""
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    footage = _footage_map(engine, job.id, seeded, tmp_path)
    report = ProductionService(engine, _settings()).run(job.id, footage_paths=footage)
    assert report.validation["valid"] is True
    assert report.duration_seconds == pytest.approx(30.0, abs=0.6)
    assert report.metrics["graphics_style"] == "fullbleed"
    assert any("full-bleed" in limitation for limitation in report.limitations)
    assert report.narration["status"] == "not_generated_voiceover_off_fullbleed_style"
    cap = cv2.VideoCapture(report.file_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, 3000)  # inside clip #5 (0s–6s)
    ok, frame = cap.read()
    cap.release()
    assert ok, "could not read a frame from the rendered video"
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    assert float(np.mean(gray[:200, :])) < 60  # black title bar
    assert float(np.std(gray[:200, :])) > 2.0  # title text visible
    assert float(np.mean(gray[300:1500, :])) > 40  # footage fills the frame
    assert float(np.std(gray[260:1400, 0:280])) > 2.0  # numbers visible


def test_render_silent_segment_is_a_valid_card(tmp_path):
    out = tmp_path / "card.mp4"
    rendered = render_silent_segment(out, 3.0)
    assert rendered.duration_seconds == pytest.approx(3.0, abs=0.3)
    assert rendered.width == 1080 and rendered.height == 1920
    assert rendered.has_audio is True


# ---------------------------------------------------------------- Phase 5: captions
def test_build_ass_writes_timed_cues(tmp_path):
    cues = [
        {"start_seconds": 0.0, "end_seconds": 2.0, "text": "Number 5. A sharp segment."},
        {"start_seconds": 2.0, "end_seconds": 4.5, "text": "Score: 70 out of 100."},
    ]
    path = build_ass(cues, tmp_path / "caps.ass")
    text = path.read_text()
    assert "PlayResX: 1080" in text and "PlayResY: 1920" in text
    assert "Number 5." in text
    assert "0:00:02.00" in text and "0:00:04.50" in text


def test_build_ass_rejects_long_lines_and_bad_times(tmp_path):
    with pytest.raises(ValidationError, match="40-character"):
        build_ass([{"start_seconds": 0.0, "end_seconds": 2.0,
                    "text": "x" * 41}], tmp_path / "caps.ass")
    with pytest.raises(ValidationError):
        build_ass([{"start_seconds": 2.0, "end_seconds": 1.0, "text": "hi"}],
                  tmp_path / "caps.ass")
    with pytest.raises(ValidationError):
        build_ass([{"start_seconds": -1.0, "end_seconds": 1.0, "text": "hi"}],
                  tmp_path / "caps.ass", time_offset_seconds=0.0)


def test_emphasise_caption_bolds_rank_and_score():
    out = emphasise_caption("Number 5. A sharp segment. Score: 70 out of 100.")
    assert out.startswith("{\\b1}Number 5.{\\b0}")
    assert "{\\b1}Score: 70 out of 100.{\\b0}" in out


# ---------------------------------------------------------------- Phase 6: graphics
def test_drawtext_filter_uses_textfile_not_inline_text(tmp_path):
    from app.agents.production.graphics import write_text_file
    text_file = write_text_file(tmp_path, "t.txt", "TOP 5: \"Parkour\" & co.")
    filt = drawtext_filter(text_file, x="(w-text_w)/2", y="100", fontsize=64,
                           font_file="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    assert "textfile=" in filt
    assert "TOP 5" not in filt  # text lives in the file, not the filter
    assert "fontsize=64" in filt


def test_clip_graphics_filters_have_rank_source_and_no_fade(tmp_path):
    items = [(5, "A sharp, fast segment"),
             (4, "A steady segment"),
             (3, "A dark segment"),
             (2, "A bright segment"),
             (1, "A sharp segment")]
    filters, files = clip_graphics_filters(
        rank=5, topic="TOP 5 CRAZIEST PARKOUR FAILS", list_items=items,
        source_label="Parkour Fails 5 by Creator 5",
        attribution_text="Parkour Fails 5 by Creator 5 (CC BY 4.0)",
        attribution_required=True, duration_seconds=6.0,
        work_dir=tmp_path,
        font_file="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        bottom_caption="A sharp, fast segment")
    # Full-bleed frame: no pad into a video area, no fades —
    # straight cuts, the clip fills the whole frame.
    assert "pad=" not in filters
    assert "fade=" not in filters
    assert "drawtext=" in filters
    assert "fontcolor=0xffd400" in filters  # PARKOUR/FAILS + active #5 line
    assert "borderw=8" in filters  # stroked title / bottom caption
    assert len(files) >= 4
    hard, _ = clip_graphics_filters(
        rank=1, topic="TOP 5 CRAZIEST PARKOUR FAILS", list_items=items,
        source_label="Clip", attribution_text=None,
        attribution_required=False, duration_seconds=6.0,
        work_dir=tmp_path,
        font_file="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    assert "fade=" not in hard
    assert "pad=" not in hard


# ---------------------------------------------------------------- Phase 4: narration
def test_narration_provider_is_edge_tts_by_default():
    provider = detect_narration_provider(Settings())
    assert provider.provider == "edge-tts"
    assert provider.status == "edge_tts"
    assert provider.voice == "en-US-GuyNeural"


def test_narration_provider_is_none_when_tts_disabled():
    provider = detect_narration_provider(_settings())
    assert provider.provider == "none"
    assert provider.status == "not_configured"
    assert "no TTS provider" in provider.detail or "disabled" in provider.detail


def test_generate_narration_audio_fails_honestly_when_disabled(tmp_path):
    with pytest.raises(ExternalServiceError, match="narration audio cannot be generated"):
        generate_narration_audio("Number five. A sharp segment.",
                                 str(tmp_path / "narr.mp3"), _settings())


def test_generate_narration_audio_failure_is_honest(monkeypatch, tmp_path):
    """A synthesis endpoint failure raises — it never writes a
    fake file. (The stub stands in for the live endpoint, which
    is unreachable from this machine.)"""
    import edge_tts

    class _Failing:
        def __init__(self, *args, **kwargs):
            pass

        def save_sync(self, output_path):
            raise RuntimeError("Cannot connect to host speech.platform.bing.com:443")

    monkeypatch.setattr(edge_tts, "Communicate", _Failing)
    out = tmp_path / "narr.mp3"
    with pytest.raises(ExternalServiceError, match="narration audio cannot be generated"):
        generate_narration_audio("Number five. A sharp segment.",
                                 str(out), _settings(tts_enabled=True))
    assert not out.is_file()


# ---------------------------------------------------------------- Phase 7: audio
def test_find_music_with_no_library_is_honest():
    asset = find_music(_settings())
    assert asset.status == "not_included_no_licensed_library"
    assert asset.file_path is None


def test_find_audio_asset_picks_a_real_audio_file(tmp_path):
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    tone = music_dir / "licensed-track.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=5", "-c:a", "pcm_s16le",
                    str(tone)], check=True)
    asset = find_audio_asset(music_dir, "music")
    assert asset.status == "included"
    assert asset.file_name == "licensed-track.wav"
    assert asset.duration_seconds == pytest.approx(5.0, abs=0.3)
    # A non-audio file is skipped, never used.
    (music_dir / "not-audio.wav").write_bytes(b"this is not audio")
    asset2 = find_audio_asset(music_dir, "music")
    assert asset2.file_name == "licensed-track.wav"


def test_final_audio_filter_has_loudnorm_fades_and_limiter():
    from app.agents.production.audio import final_audio_filter
    filt = final_audio_filter(39.0)
    assert "loudnorm=" in filt and "afade=t=in" in filt
    assert "afade=t=out:st=38.00" in filt and "alimiter=" in filt


# ---------------------------------------------------------------- Phase 8/9: concat + validation
def test_concat_and_validate_two_segments(tmp_path):
    clip = extract_clip(KNOWN_SCENES, 5.0, 11.0, tmp_path / "a.mp4")
    card = render_silent_segment(tmp_path / "b.mp4", 3.0)
    out = tmp_path / "final.mp4"
    concat_segments([clip.output_path, card.output_path], out,
                    total_duration_seconds=9.0)
    validation = validate_render(out, expected_duration_seconds=9.0)
    assert validation.valid, validation.issues
    assert validation.width == 1080 and validation.height == 1920
    assert validation.video_codec == "h264" and validation.audio_codec == "aac"
    assert validation.duration_seconds == pytest.approx(9.0, abs=0.5)
    assert decode_check(out) == []


def test_validate_render_catches_wrong_duration(tmp_path):
    clip = extract_clip(KNOWN_SCENES, 5.0, 11.0, tmp_path / "a.mp4")
    validation = validate_render(clip.output_path, expected_duration_seconds=99.0)
    assert validation.valid is False
    assert any("duration" in issue for issue in validation.issues)


def test_concat_with_music_mixes_it_under(tmp_path):
    clip = extract_clip(KNOWN_SCENES, 5.0, 8.0, tmp_path / "a.mp4")
    music = tmp_path / "music.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=10", "-c:a", "pcm_s16le",
                    str(music)], check=True)
    out = tmp_path / "final.mp4"
    concat_segments([clip.output_path], out, total_duration_seconds=3.0,
                    music_path=str(music))
    validation = validate_render(out, expected_duration_seconds=3.0)
    assert validation.valid, validation.issues


def test_concat_with_narration_mixes_it_over(tmp_path):
    """Narration is the primary track: a tone narration file mixed
    over a silent clip must make the output measurably non-silent,
    at the segments' duration."""
    from app.agents.quality_assurance.audio import analyse_audio

    clip = extract_clip(KNOWN_SCENES, 5.0, 8.0, tmp_path / "a.mp4")
    narration = tmp_path / "narration.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-c:a", "libmp3lame",
                    "-q:a", "4", str(narration)], check=True)
    out = tmp_path / "final.mp4"
    concat_segments([clip.output_path], out, total_duration_seconds=3.0,
                    narration_path=str(narration))
    validation = validate_render(out, expected_duration_seconds=3.0)
    assert validation.valid, validation.issues
    audio = analyse_audio(out, duration_seconds=3.0)
    assert audio.has_audio is True
    assert audio.effectively_silent is False
    assert audio.mean_volume_db is not None and audio.mean_volume_db > -70.0


# The stubbed-narration helper was removed with the voiceover-off
# restyle (2026-11-21): the full-bleed style never calls the
# synthesiser, so there is nothing to stand in for. The TTS
# code itself is still tested in tests/test_tts.py.


# ---------------------------------------------------------------- service: happy path
def test_service_renders_a_real_mp4_from_the_edl(engine, tmp_path):
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    footage = _footage_map(engine, job.id, seeded, tmp_path)
    report = ProductionService(engine, _settings()).run(job.id, footage_paths=footage)
    assert report.width == 1080 and report.height == 1920
    assert report.fps == pytest.approx(30.0, abs=0.1)
    assert report.video_codec == "h264" and report.audio_codec == "aac"
    # Timeline (full-bleed style): five 6s clips only, straight
    # cuts — no intro/outro cards, no transition gaps = 30s.
    assert report.duration_seconds == pytest.approx(30.0, abs=0.6)
    assert len(report.segments) == 5
    assert [s.kind for s in report.segments] == ["countdown_moment"] * 5
    assert [s.rank for s in report.segments if s.kind == "countdown_moment"] == [5, 4, 3, 2, 1]
    assert [s.transition_rendered for s in report.segments] == ["straight cut"] * 5
    assert report.validation["valid"] is True
    assert report.validation["decode_errors"] == []
    # Honest asset labels: voiceover off by design, no music, no SFX mixed.
    assert report.narration["status"] == "not_generated_voiceover_off_fullbleed_style"
    assert report.narration["audio_path"] is None
    assert report.music.status == "not_included_no_licensed_library"
    assert report.sfx_mixed is False
    assert report.ducking == "not_applicable"
    assert report.captions["lines"] > 0
    assert len(report.attributions) == 5
    assert all(a["attribution_required"] for a in report.attributions)
    # Attribution for the description: every required attribution
    # is recorded even though the video has no credits screen.
    assert report.description_attribution, "description attribution must not be empty"
    assert "Creator 5" in report.description_attribution
    # Persisted row + manifest.
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        rows = session.query(RenderedVideo).filter(RenderedVideo.job_id == job.id).all()
        assert len(rows) == 1
        assert rows[0].status == "ready"
        assert rows[0].file_path == report.file_path
    assert json.loads(open(report.manifest_path).read())["rendered_video_id"] == report.rendered_video_id
    probe = probe_video(report.file_path)
    assert probe.width == 1080 and probe.height == 1920
    assert decode_check(report.file_path) == []


def test_service_voiceover_stays_off_even_with_tts_enabled_and_qa_needs_review(
        engine, tmp_path):
    """The voiceover-off path, end to end: even with TTS enabled,
    the full-bleed style never calls the synthesiser — the
    manifest records the voiceover as off by design, no narration
    audio is mixed in, and QA reaches `needs_review`, the honest
    verdict for a voiceless render. The semantic checks stay
    `not_tested`: no VLM/OCR exists to verify wording against the
    picture."""
    from app.agents.quality_assurance.service import QAService

    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    footage = _footage_map(engine, job.id, seeded, tmp_path)
    report = ProductionService(engine, _settings(tts_enabled=True)).run(
        job.id, footage_paths=footage)
    assert report.narration["status"] == "not_generated_voiceover_off_fullbleed_style"
    assert report.narration["provider"] == "edge-tts"
    assert report.narration["voice"] == "en-US-GuyNeural"
    assert report.narration["audio_path"] is None
    assert report.validation["valid"] is True
    qa = QAService(engine, _settings()).run(job.id)
    assert qa.verdict == "needs_review", f"issues: {qa.issues}"
    assert qa.status == "NEEDS_REVIEW"
    assert qa.issues, "needs_review must carry issues"
    assert any("narration" in issue.lower() for issue in qa.issues)
    assert any(c["name"] == "semantic_narration_accuracy" and c["verdict"] == "not_tested"
               for c in qa.checks)


def test_service_run_is_idempotent(engine, tmp_path):
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    footage = _footage_map(engine, job.id, seeded, tmp_path)
    service = ProductionService(engine, _settings())
    first = service.run(job.id, footage_paths=footage)
    second = service.run(job.id, footage_paths=footage)
    assert first.duration_seconds == pytest.approx(second.duration_seconds, abs=0.6)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(RenderedVideo).filter(RenderedVideo.job_id == job.id).count() == 1


def test_service_unknown_job_raises(engine):
    from app.core.errors import NotFoundError
    with pytest.raises(NotFoundError):
        ProductionService(engine, _settings()).run("no-such-job")


def test_service_no_edl_fails_honestly(engine):
    job = _new_job(engine)
    with pytest.raises(NotFoundError, match="Creative Director|storyboard|no .* for job"):
        ProductionService(engine, _settings()).run(job.id)


def test_service_unapproved_candidate_is_rejected(engine, tmp_path):
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    footage = _footage_map(engine, job.id, seeded, tmp_path)
    candidate_id = seeded[1][0]  # rank #1
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        session.add(RightsEvidence(candidate_video_id=candidate_id,
                                   verdict="needs_review", checked_by="rights_agent",
                                   reasons=["licence evidence withdrawn"]))
        session.commit()
    with pytest.raises(ValidationError, match="rights gate|not approved"):
        ProductionService(engine, _settings()).run(job.id, footage_paths=footage)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(RenderedVideo).filter(RenderedVideo.job_id == job.id).count() == 0


def test_service_missing_footage_fails_honestly(engine):
    job = _new_job(engine)
    _seed_ranked_five(engine, job.id)  # no ApprovedFootage rows, EDL has URLs only
    with pytest.raises(ExternalServiceError, match="no local footage|URL"):
        ProductionService(engine, _settings()).run(job.id)


def test_service_stale_edl_timestamps_are_rejected(engine, tmp_path):
    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    footage = _footage_map(engine, job.id, seeded, tmp_path)
    # Move a verified moment after the plan was made.
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        moment = (session.query(AnalyzedMoment)
                  .filter(AnalyzedMoment.job_id == job.id).first())
        moment.end_seconds = 9.0
        session.commit()
    with pytest.raises(ExternalServiceError, match="do not match|stale"):
        ProductionService(engine, _settings()).run(job.id, footage_paths=footage)


# ---------------------------------------------------------------- registry + orchestrator
def test_registry_production_ready_with_engine(engine):
    registry = build_default_registry(engine, _settings())
    assert registry.get("production").status == "ready"
    assert registry.health("production").status == "ready"


def test_orchestrator_runs_real_production_then_qa(engine, tmp_path):
    """Full real pipeline: research (test provider) -> rights ->
    video intelligence (local footage) -> ranking -> creative
    director -> production -> quality assurance (Prompt 09).
    QA verdict is needs_review, not pass: the voiceover is off by
    design in the full-bleed style, so the render has no narration
    voice. (Enabling a voiceover would need a style that calls
    TTS — see the voiceover-off test.)"""
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
    assert registry.get("production").status == "ready"
    assert registry.get("quality_assurance").status == "ready"
    runner = WorkflowEngine(engine, settings, registry)
    finished = runner.run_job(job.id)
    assert finished.status == "awaiting_approval"
    assert WorkflowStage.QUALITY_ASSURANCE in JobService(engine).completed_stages(job.id)
    assert JobService(engine).get_stage(job.id).value == "COMPLETED"
    from app.agents.quality_assurance.service import QAService
    qa = QAService(engine, _settings()).get_report(job.id)
    assert qa.verdict == "needs_review"
    assert qa.status == "NEEDS_REVIEW"
    assert any("narration" in issue.lower() for issue in qa.issues)
    assert qa.repair_requests, "needs_review must carry repair requests"
    rendered = ProductionService(engine, _settings()).get_rendered_video(job.id)
    assert rendered.status == "ready"
    assert rendered.width == 1080 and rendered.height == 1920
    probe = probe_video(rendered.file_path)
    assert probe.video_codec == "h264" and probe.audio_codec == "aac"
    assert decode_check(rendered.file_path) == []


# ---------------------------------------------------------------- API
def test_api_production_endpoints(engine, tmp_path):
    from fastapi.testclient import TestClient

    from main import app

    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    footage = _footage_map(engine, job.id, seeded, tmp_path)
    report = ProductionService(engine, _settings()).run(job.id, footage_paths=footage)
    with TestClient(app) as client:
        agents = client.get("/agents").json()
        by_id = {a["agent_id"]: a for a in agents}
        assert by_id["production"]["status"] == "ready"
        assert client.get("/agents/production/health").json()["status"] == "ready"
        rendered = client.get(f"/jobs/{job.id}/rendered-video").json()
        assert rendered["id"] == report.rendered_video_id
        assert rendered["status"] == "ready"
        assert rendered["width"] == 1080 and rendered["height"] == 1920
        production = client.get(f"/jobs/{job.id}/production").json()
        assert production["file_path"] == report.file_path
        assert production["narration"]["status"] == "not_generated_voiceover_off_fullbleed_style"
        assert production["music"]["status"] == "not_included_no_licensed_library"
        download = client.get(f"/jobs/{job.id}/rendered-video/download")
        assert download.status_code == 200
        assert download.headers["content-type"] == "video/mp4"
        assert len(download.content) > 100_000
        assert client.get("/jobs/no-such-job/rendered-video").status_code == 404
        assert client.get("/jobs/no-such-job/production").status_code == 404
        assert client.get("/jobs/no-such-job/rendered-video/download").status_code == 404
