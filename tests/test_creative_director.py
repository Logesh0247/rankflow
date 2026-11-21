"""Creative Director Agent tests (Prompt 07): hook generation,
signal-grounded narration, countdown storyboard, caption plan,
visual direction, Edit Decision List, Phase 7 validation, the
rights gate, idempotency, the API and orchestrator integration.

Moments are seeded directly with Video-Intelligence-style labels
(creative direction never reads video files — it plans from the
persisted ranking and signals), except the orchestrator test,
which runs real footage through research -> rights -> video
intelligence -> ranking -> creative director.
"""
import cv2
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.creative_director.hooks import generate_hooks, select_hook
from app.agents.creative_director.narration import (
    build_moment_narration, closing_text, narration_contains_forbidden_word,
    split_captions,
)
from app.agents.creative_director.service import (
    CreativeDirectorService, validate_plan,
)
from app.agents.creative_director.signals import MomentPlanInput
from app.agents.rights.service import RightsService
from app.core.config import Settings
from app.core.errors import ExternalServiceError, ValidationError
from app.models.models import (
    AnalyzedMoment, ApprovedFootage, CandidateVideo, EditInstruction,
    Narration, RankingResult, Source, Storyboard,
)
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.orchestrator.states import WorkflowStage
from app.schemas.schemas import JobCreate

FPS, W, H = 15, 160, 120
TOPIC = "TOP 5 CRAZIEST PARKOUR FAILS"


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 30.0,
            "rights_fetch_ia_metadata": False,
            "tts_enabled": False}  # No-voice path: verdict must not depend on endpoint reachability.
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


def _labels(*, event_type="high_motion_segment", motion=20.0, sharpness=200.0,
            brightness=120.0, visual_quality=0.7, context_clarity=1.0,
            confidence=0.8, width=1280, height=720, fps=30.0, has_audio=True,
            issues=None):
    return {
        "analyser": "heuristic_frame_signals_v1",
        "event_type": event_type,
        "confidence": confidence,
        "visual_quality": visual_quality,
        "context_clarity": context_clarity,
        "mean_motion": motion,
        "mean_sharpness": sharpness,
        "mean_brightness": brightness,
        "frame_refs": ["t1.0", "t2.0"],
        "verification": {"verified": True, "issues": list(issues or [])},
        "probe": {"duration_seconds": 30.0, "width": width, "height": height,
                  "fps": fps, "video_codec": "mpeg4", "has_audio": has_audio,
                  "audio_codec": "aac" if has_audio else None},
    }


def _seed_moment(engine, job_id, candidate_id, start, end, labels, *, score=None):
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        row = AnalyzedMoment(
            job_id=job_id, candidate_video_id=candidate_id,
            start_seconds=start, end_seconds=end,
            description="signal-based moment", score=score if score is not None
            else labels.get("confidence", 0.5), labels=labels)
        session.add(row)
        session.commit()
        return row.id


def _seed_ranked_five(engine, job_id):
    """Five approved candidates, each with one analysed moment, ranked
    #1 (motion 30) to #5 (motion 10) via the real Ranking Agent.
    Returns {rank: (candidate_id, moment_id)}."""
    from app.agents.ranking.service import RankingService

    seeded = {}
    for i, motion in enumerate((30.0, 25.0, 20.0, 15.0, 10.0), start=1):
        candidate_id = _seed_candidate(
            engine, job_id, f"cc-clip-{i}", title=f"Parkour Fails {i}",
            creator=f"Creator {i}", licence=_licence_url())
        moment_id = _seed_moment(engine, job_id, candidate_id, 0.0, 6.0,
                                 _labels(motion=motion))
        seeded[i] = (candidate_id, moment_id)
    RightsService(engine, _settings()).run(job_id)
    report = RankingService(engine, _settings()).run(job_id)
    assert len(report.ranks) == 5
    return seeded


def _plan_input(*, rank=5, motion=20.0, visual_quality=0.7, context_clarity=1.0,
                duration=6.0, issues=None):
    return MomentPlanInput(
        rank=rank, moment_id=f"m{rank}", candidate_video_id=f"c{rank}",
        start_seconds=0.0, end_seconds=duration,
        description="signal-based moment", event_type="high_motion_segment",
        confidence=0.8, visual_quality=visual_quality,
        context_clarity=context_clarity, mean_motion=motion,
        mean_sharpness=200.0, mean_brightness=120.0,
        verification_issues=list(issues or []),
        width=1280, height=720, fps=30.0, has_audio=True,
        score_100=70.0,
        components={"entertainment": 80.0, "unexpectedness": 70.0,
                    "visual_impact": 65.0, "context_clarity": 90.0,
                    "technical_quality": 60.0},
        candidate_title="Parkour Fails 5", candidate_creator="Creator 5",
        candidate_url="https://archive.org/details/cc-clip-5")


# ---------------------------------------------------------------- Phase 2: hooks
def test_generate_hooks_returns_five_scored_candidates():
    candidates = generate_hooks(TOPIC)
    assert len(candidates) == 5
    assert all(0.0 <= c.total <= 100.0 for c in candidates)
    assert all(c.text for c in candidates)
    # No hook promises a viral outcome or describes a clip event.
    for candidate in candidates:
        lowered = candidate.text.lower()
        assert "viral" not in lowered and "guaranteed" not in lowered
        assert "falls" not in lowered or "parkour fails" in lowered.lower()


def test_select_hook_is_the_highest_scoring():
    candidates = generate_hooks(TOPIC)
    selected = select_hook(TOPIC)
    assert selected.total == max(c.total for c in candidates)
    assert selected.text in {c.text for c in candidates}
    # The selected hook is relevant to the topic, not the generic one.
    assert selected.relevance > 0.0
    # Deterministic: same topic, same hook.
    assert select_hook(TOPIC).text == selected.text


def test_hook_scoring_prefers_topic_relevance():
    candidates = generate_hooks(TOPIC)
    generic = next(c for c in candidates if "Parkour" not in c.text)
    topical = next(c for c in candidates if "Parkour" in c.text)
    assert topical.relevance > generic.relevance


# ---------------------------------------------------------------- Phase 4: narration
def test_moment_narration_is_signal_grounded_and_clean():
    moment = _plan_input(rank=5, motion=30.0)
    built = build_moment_narration(moment, word_budget=16)
    assert built.text.startswith("Number 5.")
    assert built.word_count <= 16
    assert narration_contains_forbidden_word(built.text) == []
    # The basis names the measured signals the wording rests on.
    assert "mean motion 30.0" in built.basis
    assert "score 70.0/100" in built.basis
    assert "high_motion_segment" in built.basis
    # No invented event, person or dialogue in the narration.
    for word in ("falls", "jumps", "flips", "parkour", "he ", "she "):
        assert word not in built.text.lower()


def test_moment_narration_respects_a_small_word_budget():
    moment = _plan_input(rank=3, motion=2.0)
    built = build_moment_narration(moment, word_budget=5)
    assert built.text.startswith("Number 3.")
    assert built.word_count <= 5
    assert narration_contains_forbidden_word(built.text) == []


def test_moment_narration_describes_truncated_and_frozen_signals():
    truncated = _plan_input(rank=4, issues=["truncated: moment starts at the video start"])
    built = build_moment_narration(truncated, word_budget=16)
    assert "cut at the edge" in built.text
    frozen = _plan_input(rank=4, issues=["frames are frozen (identical) across the moment"])
    built_frozen = build_moment_narration(frozen, word_budget=16)
    assert "still, unchanging" in built_frozen.text


def test_forbidden_word_guard_catches_semantic_events():
    assert narration_contains_forbidden_word("he falls off the wall") == ["falls"]
    assert narration_contains_forbidden_word("a fast, sharp segment") == []
    assert closing_text()
    assert narration_contains_forbidden_word(closing_text()) == []


def test_split_captions_respects_line_length():
    text = "Number five. A sharp, fast-moving segment. Clear from start to finish."
    lines = split_captions(text, max_chars=40)
    assert all(len(line) <= 40 for line in lines)
    assert " ".join(lines) == text
    assert len(lines) >= 2


# ---------------------------------------------------------------- service: happy path
def test_service_builds_countdown_storyboard_and_edl(engine):
    job = _new_job(engine)
    _seed_ranked_five(engine, job.id)
    report = CreativeDirectorService(engine, _settings()).run(job.id)
    assert report.generator == "deterministic_template_v1"
    assert report.hook["text"]
    assert report.title.startswith(TOPIC)
    # 7 scenes: intro + 5 moments + outro, countdown #5 -> #1.
    assert len(report.scenes) == 7
    assert report.scenes[0]["kind"] == "intro"
    assert report.scenes[-1]["kind"] == "outro"
    assert [s["rank"] for s in report.scenes[1:6]] == [5, 4, 3, 2, 1]
    assert all(s["narration"].startswith(f"Number {s['rank']}.")
               for s in report.scenes[1:6])
    # Narration: hook + five moments + closing, fits the timeline.
    assert report.narration_script.startswith(report.hook["text"])
    assert "That is number one" in report.narration_script
    assert report.narration_estimated_seconds <= report.total_duration_seconds + 2.0
    # Timeline (full-bleed style): five 6s clips only, no intro/outro
    # cards and no transition gaps = 30s.
    assert report.total_duration_seconds == pytest.approx(30)
    # EDL: five clips, #5 first, verified timestamps, 1080x1920 @ 30fps.
    assert len(report.edl["clips"]) == 5
    assert [c["rank"] for c in report.edl["clips"]] == [5, 4, 3, 2, 1]
    assert all(c["start_seconds"] == 0.0 and c["end_seconds"] == 6.0
               for c in report.edl["clips"])
    assert report.edl["output_width"] == 1080 and report.edl["output_height"] == 1920
    assert report.edl["fps"] == 30
    assert all(c["source_path"] for c in report.edl["clips"])
    # Caption plan stays inside the timeline; visual direction per moment.
    assert report.caption_plan
    assert all(0.0 <= c["start_seconds"] < c["end_seconds"] <= report.total_duration_seconds
               for c in report.caption_plan)
    assert all(len(c["text"]) <= 40 for c in report.caption_plan)
    assert len(report.visual_direction["per_moment"]) == 5
    assert all(m["attribution_required"] for m in report.visual_direction["per_moment"])
    # Attributions: all five approved CC BY sources, with licence.
    assert len(report.attributions) == 5
    assert all(a["licence_name"] for a in report.attributions)
    # Persisted rows are readable through the getters.
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(Storyboard).filter(Storyboard.job_id == job.id).count() == 1
        assert session.query(Narration).filter(Narration.job_id == job.id).count() == 1
        assert session.query(EditInstruction).filter(EditInstruction.job_id == job.id).count() == 1
    storyboard = CreativeDirectorService(engine, _settings()).get_storyboard(job.id)
    assert storyboard.hook == report.hook["text"]
    assert len(storyboard.scenes) == 7
    narration = CreativeDirectorService(engine, _settings()).get_narration(job.id)
    assert narration.script_text == report.narration_script
    assert narration.status == "draft"
    edl = CreativeDirectorService(engine, _settings()).get_edl(job.id)
    assert len(edl.edl["clips"]) == 5


def test_service_run_is_idempotent(engine):
    job = _new_job(engine)
    _seed_ranked_five(engine, job.id)
    service = CreativeDirectorService(engine, _settings())
    first = service.run(job.id)
    second = service.run(job.id)
    assert first.narration_script == second.narration_script
    assert [c["moment_id"] for c in first.edl["clips"]] == [c["moment_id"] for c in second.edl["clips"]]
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(Storyboard).filter(Storyboard.job_id == job.id).count() == 1
        assert session.query(Narration).filter(Narration.job_id == job.id).count() == 1
        assert session.query(EditInstruction).filter(EditInstruction.job_id == job.id).count() == 1


def test_service_unknown_job_raises(engine):
    from app.core.errors import NotFoundError
    with pytest.raises(NotFoundError):
        CreativeDirectorService(engine, _settings()).run("no-such-job")


def test_service_no_ranking_fails_honestly(engine):
    job = _new_job(engine)
    with pytest.raises(ExternalServiceError, match="Ranking Agent|ranked moment"):
        CreativeDirectorService(engine, _settings()).run(job.id)


def test_service_fewer_than_five_ranks_fails_honestly(engine):
    job = _new_job(engine)
    for i in range(3):
        candidate_id = _seed_candidate(engine, job.id, f"cc-clip-{i}",
                                       title=f"Clip {i}", creator=f"C {i}",
                                       licence=_licence_url())
        moment_id = _seed_moment(engine, job.id, candidate_id, 0.0, 6.0, _labels())
        sessions = sessionmaker(bind=engine)
        with sessions() as session:
            session.add(RankingResult(job_id=job.id, moment_id=moment_id,
                                      candidate_video_id=candidate_id, rank=i + 1,
                                      score=0.8, reason="seeded"))
            session.commit()
    RightsService(engine, _settings()).run(job.id)
    with pytest.raises(ExternalServiceError, match="expected 5|five"):
        CreativeDirectorService(engine, _settings()).run(job.id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(Storyboard).filter(Storyboard.job_id == job.id).count() == 0


def test_service_unapproved_ranked_candidate_is_rejected(engine):
    from app.models.models import RightsEvidence

    job = _new_job(engine)
    seeded = _seed_ranked_five(engine, job.id)
    # Revoke the approval of the #1 candidate after ranking.
    candidate_id = seeded[1][0]
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        session.add(RightsEvidence(candidate_video_id=candidate_id,
                                   verdict="needs_review", checked_by="rights_agent",
                                   reasons=["licence evidence withdrawn"]))
        session.commit()
    with pytest.raises(ValidationError, match="rights gate|not approved"):
        CreativeDirectorService(engine, _settings()).run(job.id)


# ---------------------------------------------------------------- Phase 7: validation
def test_validate_plan_catches_wrong_edl_order_and_duplicates():
    moments = [_plan_input(rank=r) for r in (1, 2, 3, 4, 5)]
    narrations = {m.moment_id: f"Number {m.rank}. A fast-moving segment."
                  for m in moments}
    clips = [{"moment_id": m.moment_id, "start_seconds": 0.0, "end_seconds": 6.0}
             for m in sorted(moments, key=lambda m: -m.rank)]
    issues, _ = validate_plan(moments=moments, hook_text="Five moments, ranked.",
                              moment_narrations=narrations, closing=closing_text(),
                              edl_clips=clips, total_duration_seconds=42.0,
                              narration_estimated_seconds=30.0)
    assert issues == []
    # Wrong order: #1 first instead of #5 first.
    wrong = list(reversed(clips))
    issues, _ = validate_plan(moments=moments, hook_text="Five moments, ranked.",
                              moment_narrations=narrations, closing=closing_text(),
                              edl_clips=wrong, total_duration_seconds=42.0,
                              narration_estimated_seconds=30.0)
    assert any("#5 -> #1" in issue for issue in issues)
    # Forbidden word in a moment narration.
    bad = dict(narrations)
    bad["m5"] = "Number 5. He falls off the wall."
    issues, _ = validate_plan(moments=moments, hook_text="Five moments, ranked.",
                              moment_narrations=bad, closing=closing_text(),
                              edl_clips=clips, total_duration_seconds=42.0,
                              narration_estimated_seconds=30.0)
    assert any("falls" in issue for issue in issues)
    # Narration longer than the timeline.
    issues, _ = validate_plan(moments=moments, hook_text="Five moments, ranked.",
                              moment_narrations=narrations, closing=closing_text(),
                              edl_clips=clips, total_duration_seconds=42.0,
                              narration_estimated_seconds=99.0)
    assert any("cannot fit" in issue for issue in issues)
    # Timestamp mismatch.
    wrong_time = [dict(c) for c in clips]
    wrong_time[0]["end_seconds"] = 9.0
    issues, _ = validate_plan(moments=moments, hook_text="Five moments, ranked.",
                              moment_narrations=narrations, closing=closing_text(),
                              edl_clips=wrong_time, total_duration_seconds=42.0,
                              narration_estimated_seconds=30.0)
    assert any("do not match" in issue for issue in issues)


# ---------------------------------------------------------------- registry + orchestrator
def test_registry_creative_director_ready_with_engine(engine):
    registry = build_default_registry(engine, _settings())
    assert registry.get("creative_director").status == "ready"
    assert registry.health("creative_director").status == "ready"


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


def test_orchestrator_runs_real_creative_then_production_then_qa(engine, tmp_path):
    """Full real pipeline: research (test provider) -> rights ->
    video intelligence (local footage) -> ranking -> creative
    director -> production (Prompt 08, built) -> quality
    assurance (Prompt 09, built). The orchestrator timeout is
    raised because a real 1080x1920 render takes minutes on this
    VM, far longer than the 30s default. QA verdict is
    needs_review: TTS is disabled for this test, so no narration voice."""
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
    assert registry.get("creative_director").status == "ready"
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
    creative = CreativeDirectorService(engine, _settings())
    storyboard = creative.get_storyboard(job.id)
    assert len(storyboard.scenes) == 7
    assert [s.rank for s in storyboard.scenes[1:6]] == [5, 4, 3, 2, 1]
    edl = creative.get_edl(job.id)
    assert len(edl.edl["clips"]) == 5
    # EDL source paths are the real local footage files.
    assert all(c["source_path"].endswith(".mp4") for c in edl.edl["clips"])
    # Production also ran: a real rendered video exists.
    from app.agents.production.service import ProductionService
    from app.agents.video_intelligence.probe import probe_video
    rendered = ProductionService(engine, _settings()).get_rendered_video(job.id)
    assert rendered.status == "ready"
    assert rendered.width == 1080 and rendered.height == 1920
    probe = probe_video(rendered.file_path)
    assert probe.video_codec == "h264" and probe.audio_codec == "aac"


# ---------------------------------------------------------------- API
def test_api_creative_director_endpoints(engine):
    from fastapi.testclient import TestClient

    from main import app

    job = _new_job(engine)
    _seed_ranked_five(engine, job.id)
    CreativeDirectorService(engine, _settings()).run(job.id)
    with TestClient(app) as client:
        agents = client.get("/agents").json()
        by_id = {a["agent_id"]: a for a in agents}
        assert by_id["creative_director"]["status"] == "ready"
        assert client.get("/agents/creative_director/health").json()["status"] == "ready"
        storyboard = client.get(f"/jobs/{job.id}/storyboard").json()
        assert storyboard["hook"]
        assert len(storyboard["scenes"]) == 7
        narration = client.get(f"/jobs/{job.id}/narration").json()
        assert narration["script_text"].startswith(storyboard["hook"])
        assert narration["status"] == "draft"
        edl = client.get(f"/jobs/{job.id}/edl").json()
        assert len(edl["edl"]["clips"]) == 5
        assert edl["output_width"] == 1080 and edl["output_height"] == 1920
        creative = client.get(f"/jobs/{job.id}/creative").json()
        assert creative["storyboard_id"] == storyboard["id"]
        assert creative["total_duration_seconds"] == pytest.approx(30)
        assert client.get("/jobs/no-such-job/storyboard").status_code == 404
        assert client.get("/jobs/no-such-job/narration").status_code == 404
        assert client.get("/jobs/no-such-job/edl").status_code == 404
        assert client.get("/jobs/no-such-job/creative").status_code == 404
