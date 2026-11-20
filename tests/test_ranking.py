"""Ranking Agent tests (Prompt 06): scoring formula, component
caps, duplicate detection, unique-source enforcement, ranking
order, insufficient candidates, the rights gate, idempotency,
the API and orchestrator integration.

Moments are seeded directly with Video-Intelligence-style
labels for the unit/service tests (ranking never reads video
files — it scores the persisted signals), and the orchestrator
test runs real footage through research -> rights -> video
intelligence -> ranking, like the Prompt 05 tests.
"""
import cv2
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.ranking.scoring import (
    SCORER, MomentSignals, deduplicate, rank_key, score_moment,
)
from app.agents.ranking.service import RankingService
from app.agents.rights.service import RightsService
from app.core.config import Settings
from app.core.errors import ExternalServiceError, NotFoundError
from app.models.models import (
    AnalyzedMoment, ApprovedFootage, CandidateVideo, RankingResult, Source,
)
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.orchestrator.states import WorkflowStage
from app.schemas.schemas import JobCreate

FPS, W, H = 15, 160, 120


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 30.0,
            "rights_fetch_ia_metadata": False}
    base.update(overrides)
    return Settings(**base)


def _new_job(engine, topic="TOP 5 CRAZIEST PARKOUR FAILS"):
    return JobService(engine).create_job(JobCreate(topic=topic))


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


def _approve(engine, job_id):
    """Approve via the real Rights service (CC BY 4.0 candidates)."""
    RightsService(engine, _settings()).run(job_id)


def _licence_url(variant: str = "by", version: str = "4.0") -> str:
    return f"https://creativecommons.org/licenses/{variant}/{version}/"


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


def _signals(**overrides) -> MomentSignals:
    base = {"moment_id": "m1", "candidate_video_id": "c1",
            "start_seconds": 0.0, "end_seconds": 6.0, "confidence": 0.8,
            "visual_quality": 0.7, "context_clarity": 1.0,
            "event_type": "high_motion_segment", "mean_motion": 20.0,
            "mean_sharpness": 200.0, "mean_brightness": 120.0,
            "width": 1280, "height": 720, "fps": 30.0, "has_audio": True}
    base.update(overrides)
    return MomentSignals(**base)


def _seed_five(engine, job_id, *, moments_per_candidate=1):
    """Five approved candidates, each with `moments_per_candidate`
    moments. Returns {candidate_id: [moment_ids]}. Motion varies
    per candidate so the ranking order is deterministic."""
    out = {}
    for i, motion in enumerate((30.0, 25.0, 20.0, 15.0, 10.0), start=1):
        candidate_id = _seed_candidate(
            engine, job_id, f"cc-clip-{i}", title=f"Parkour Fails {i}",
            creator=f"Creator {i}", licence=_licence_url())
        out[candidate_id] = []
        for j in range(moments_per_candidate):
            labels = _labels(motion=motion - j * 5.0)
            out[candidate_id].append(_seed_moment(
                engine, job_id, candidate_id, j * 6.0, j * 6.0 + 6.0, labels))
    _approve(engine, job_id)
    return out


# ---------------------------------------------------------------- Phase 1: scoring
def test_score_formula_is_the_weighted_sum():
    scored = score_moment(_signals())
    expected = round(0.25 * scored.entertainment + 0.25 * scored.unexpectedness
                     + 0.20 * scored.visual_impact + 0.15 * scored.context_clarity
                     + 0.15 * scored.technical_quality, 1)
    assert scored.total == pytest.approx(expected, abs=0.1)
    assert scored.score == pytest.approx(scored.total / 100.0, abs=0.001)
    for value in (scored.entertainment, scored.unexpectedness, scored.visual_impact,
                  scored.context_clarity, scored.technical_quality, scored.total):
        assert 0.0 <= value <= 100.0
    # The explanation names the score and says it is not a viral prediction.
    assert "not a viral-performance prediction" in scored.explanation
    assert "heuristic editorial score" in scored.explanation


def test_high_motion_beats_static():
    fast = score_moment(_signals(mean_motion=30.0, event_type="high_motion_segment"))
    static = score_moment(_signals(mean_motion=0.0, event_type="scene_segment"))
    assert fast.total > static.total
    assert fast.entertainment > static.entertainment
    assert fast.unexpectedness > static.unexpectedness


def test_sharp_bright_beats_blurry_dark():
    sharp = score_moment(_signals(visual_quality=0.9))
    blurry = score_moment(_signals(visual_quality=0.1))
    assert sharp.total > blurry.total
    assert sharp.visual_impact > blurry.visual_impact
    assert sharp.technical_quality > blurry.technical_quality


def test_short_burst_beats_long_unchanging():
    short = score_moment(_signals(start_seconds=0.0, end_seconds=2.0))
    long_ = score_moment(_signals(start_seconds=0.0, end_seconds=12.0))
    assert short.unexpectedness > long_.unexpectedness


def test_truncated_moment_caps_context_clarity():
    clean = score_moment(_signals())
    truncated = score_moment(_signals(
        verification_issues=["truncated: moment starts at the video start, no lead-in available"]))
    assert clean.context_clarity == 100.0
    assert truncated.context_clarity == 70.0
    assert truncated.total < clean.total


def test_frozen_moment_caps_context_clarity_harder():
    frozen = score_moment(_signals(
        verification_issues=["frames are frozen (identical) across the moment"]))
    assert frozen.context_clarity == 50.0


def test_silent_low_resolution_scores_lower_technically():
    good = score_moment(_signals())
    weak = score_moment(_signals(width=160, height=120, fps=15.0, has_audio=False))
    assert weak.technical_quality < good.technical_quality


# ---------------------------------------------------------------- Phase 3: duplicates
def test_deduplicate_overlapping_same_source_keeps_higher():
    high = score_moment(_signals(moment_id="m-high", start_seconds=5.0, end_seconds=11.0,
                                 mean_motion=30.0))
    low = score_moment(_signals(moment_id="m-low", start_seconds=6.0, end_seconds=10.0,
                                mean_motion=10.0))
    kept, dropped = deduplicate([low, high])
    assert [m.moment_id for m in kept] == ["m-high"]
    assert len(dropped) == 1 and dropped[0]["moment_id"] == "m-low"
    assert "same source video" in dropped[0]["reason"]


def test_deduplicate_non_overlapping_same_source_keeps_both():
    a = score_moment(_signals(moment_id="m-a", start_seconds=0.0, end_seconds=5.0))
    b = score_moment(_signals(moment_id="m-b", start_seconds=10.0, end_seconds=15.0))
    kept, dropped = deduplicate([a, b])
    assert len(kept) == 2 and dropped == []


def test_fingerprint_duplicate_across_uploads():
    a = score_moment(_signals(moment_id="m-a", candidate_video_id="c-a",
                              candidate_title="Parkour Fails", candidate_creator="MACSD",
                              mean_motion=20.0))
    b = score_moment(_signals(moment_id="m-b", candidate_video_id="c-b",
                              candidate_title="Parkour Fails", candidate_creator="MACSD",
                              mean_motion=21.0, mean_sharpness=205.0,
                              mean_brightness=118.0, start_seconds=0.0, end_seconds=6.0))
    kept, dropped = deduplicate([a, b])
    assert len(kept) == 1 and len(dropped) == 1
    assert "different upload" in dropped[0]["reason"]


def test_fingerprint_same_title_different_signals_not_duplicate():
    a = score_moment(_signals(moment_id="m-a", candidate_video_id="c-a",
                              candidate_title="Parkour Fails", candidate_creator="MACSD",
                              mean_motion=30.0))
    b = score_moment(_signals(moment_id="m-b", candidate_video_id="c-b",
                              candidate_title="Parkour Fails", candidate_creator="MACSD",
                              mean_motion=2.0, mean_sharpness=20.0))
    kept, dropped = deduplicate([a, b])
    assert len(kept) == 2 and dropped == []


def test_fingerprint_needs_same_creator():
    a = score_moment(_signals(moment_id="m-a", candidate_video_id="c-a",
                              candidate_title="Parkour Fails", candidate_creator="MACSD"))
    b = score_moment(_signals(moment_id="m-b", candidate_video_id="c-b",
                              candidate_title="Parkour Fails", candidate_creator="Someone Else"))
    kept, _ = deduplicate([a, b])
    assert len(kept) == 2


def test_tied_scores_rank_deterministically():
    a = score_moment(_signals(moment_id="m-b", start_seconds=10.0, end_seconds=16.0))
    b = score_moment(_signals(moment_id="m-a", start_seconds=0.0, end_seconds=6.0))
    first = sorted([a, b], key=rank_key)
    second = sorted([b, a], key=rank_key)
    assert first == second  # insertion order must not matter
    assert first[0].moment_id == "m-a"  # earlier start wins the tie


# ---------------------------------------------------------------- service: selection
def test_service_selects_five_unique_sources(engine):
    job = _new_job(engine)
    seeded = _seed_five(engine, job.id)
    service = RankingService(engine, _settings())
    report = service.run(job.id)
    assert report.scorer == SCORER
    assert len(report.ranks) == 5
    assert [r.rank for r in report.ranks] == [1, 2, 3, 4, 5]
    assert len({r.candidate_video_id for r in report.ranks}) == 5
    assert len({r.moment_id for r in report.ranks}) == 5
    scores = [r.score_100 for r in report.ranks]
    assert scores == sorted(scores, reverse=True), "rank #1 must be the best score"
    # The fastest-motion candidate wins; the slowest is last.
    first_candidate = next(iter(seeded))  # motion 30
    assert report.ranks[0].candidate_video_id == first_candidate
    assert all("heuristic editorial score" in r.explanation for r in report.ranks)
    assert report.metrics["ranked"] == 5
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        rows = session.query(RankingResult).filter(RankingResult.job_id == job.id).all()
        assert len(rows) == 5
        assert all(row.score_breakdown and row.score_breakdown["total"] > 0 for row in rows)
        assert all(row.reason for row in rows)
        selected = session.query(CandidateVideo).filter(
            CandidateVideo.job_id == job.id, CandidateVideo.status == "selected").count()
        assert selected == 5


def test_service_best_moment_per_source_only(engine):
    job = _new_job(engine)
    seeded = _seed_five(engine, job.id, moments_per_candidate=2)
    report = RankingService(engine, _settings()).run(job.id)
    assert len(report.ranks) == 5
    assert len({r.candidate_video_id for r in report.ranks}) == 5
    # Each candidate's second moment (motion - 5) is never selected.
    second_moments = {ids[1] for ids in seeded.values()}
    assert not second_moments & {r.moment_id for r in report.ranks}


def test_service_below_floor_moment_dropped(engine):
    job = _new_job(engine)
    seeded = _seed_five(engine, job.id)
    weak_candidate = _seed_candidate(
        engine, job.id, "cc-weak", title="Weak Clip", creator="Weak",
        licence=_licence_url())
    weak_labels = _labels(motion=0.0, visual_quality=0.05, context_clarity=0.1,
                          event_type="scene_segment", width=160, height=120,
                          fps=15.0, has_audio=False)
    weak_id = _seed_moment(engine, job.id, weak_candidate, 0.0, 12.0, weak_labels)
    _approve(engine, job.id)
    report = RankingService(engine, _settings()).run(job.id)
    assert len(report.ranks) == 5
    assert weak_id in report.below_floor
    assert weak_id not in {r.moment_id for r in report.ranks}
    assert report.metrics["below_floor"] == 1


def test_service_insufficient_sources_fails_honestly(engine):
    job = _new_job(engine)
    for i in range(3):
        candidate_id = _seed_candidate(
            engine, job.id, f"cc-clip-{i}", title=f"Clip {i}", creator=f"C {i}",
            licence=_licence_url())
        _seed_moment(engine, job.id, candidate_id, 0.0, 6.0, _labels())
    _approve(engine, job.id)
    with pytest.raises(ExternalServiceError, match="five|more candidates"):
        RankingService(engine, _settings()).run(job.id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(RankingResult).filter(
            RankingResult.job_id == job.id).count() == 0, \
            "no partial ranking may be persisted"


def test_service_no_moments_fails_honestly(engine):
    job = _new_job(engine)
    with pytest.raises(ExternalServiceError, match="no analysed moments"):
        RankingService(engine, _settings()).run(job.id)


def test_service_unknown_job_raises(engine):
    with pytest.raises(NotFoundError):
        RankingService(engine, _settings()).run("no-such-job")


def test_service_unapproved_moment_never_ranked(engine):
    job = _new_job(engine)
    seeded = _seed_five(engine, job.id)
    # A sixth candidate with no licence (needs_review) and the
    # strongest signals of all — it must still never be selected.
    sneaky = _seed_candidate(engine, job.id, "unlicensed-clip",
                             title="Sneaky Clip", creator="Sneaky", licence=None)
    sneaky_labels = _labels(motion=40.0, visual_quality=1.0, context_clarity=1.0)
    sneaky_id = _seed_moment(engine, job.id, sneaky, 0.0, 2.0, sneaky_labels)
    _approve(engine, job.id)
    report = RankingService(engine, _settings()).run(job.id)
    assert sneaky_id not in {r.moment_id for r in report.ranks}
    assert any(s["moment_id"] == sneaky_id for s in report.unapproved_skipped)
    assert len(report.ranks) == 5


def test_service_all_unapproved_fails_honestly(engine):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "unlicensed-clip",
                                   title="Clip", creator="C", licence=None)
    _seed_moment(engine, job.id, candidate_id, 0.0, 6.0, _labels())
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        from app.models.models import RightsEvidence
        session.add(RightsEvidence(candidate_video_id=candidate_id,
                                   verdict="needs_review", checked_by="rights_agent",
                                   reasons=["no licence published"]))
        session.commit()
    with pytest.raises(ExternalServiceError, match="no approved moments|not Rights-approved"):
        RankingService(engine, _settings()).run(job.id)


def test_service_run_is_idempotent(engine):
    job = _new_job(engine)
    _seed_five(engine, job.id)
    service = RankingService(engine, _settings())
    first = service.run(job.id)
    second = service.run(job.id)
    assert [r.moment_id for r in first.ranks] == [r.moment_id for r in second.ranks]
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(RankingResult).filter(
            RankingResult.job_id == job.id).count() == 5


def test_service_floor_can_make_insufficient(engine):
    job = _new_job(engine)
    _seed_five(engine, job.id)
    settings = _settings(ranking_min_score=90.0)
    with pytest.raises(ExternalServiceError, match="five|below the minimum"):
        RankingService(engine, settings).run(job.id)


# ---------------------------------------------------------------- registry + orchestrator
def test_registry_ranking_ready_with_engine(engine):
    registry = build_default_registry(engine, _settings())
    assert registry.get("ranking").status == "ready"
    assert registry.health("ranking").status == "ready"


def _pingpong(frame_index: int, speed: int, span: int) -> int:
    p = (frame_index * speed) % (2 * span)
    return p if p <= span else 2 * span - p


def _write_footage(path, speed: int, colour, duration: float = 20.0):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for i in range(int(duration * FPS)):
        t = i / FPS
        frame = np.full((H, W, 3), colour, dtype=np.uint8)
        x = _pingpong(i, speed, W - 50)
        cv2.rectangle(frame, (x, H // 2 - 25), (x + 50, H // 2 + 25), (0, 0, 0), -1)
        writer.write(frame)
    writer.release()
    return path


def test_orchestrator_runs_real_ranking_then_production_then_qa(engine, tmp_path):
    """Full real pipeline: research (test provider) -> rights ->
    video intelligence (local footage) -> ranking -> creative
    director -> production (Prompt 08, built) -> quality
    assurance (Prompt 09, built). QA verdict is needs_review,
    not pass: no TTS provider is configured, so the render has
    no narration voice and effectively silent audio — flagged
    honestly, never passed as present."""
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
    assert registry.get("ranking").status == "ready"
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
    assert any("narration" in issue.lower() for issue in qa.issues)
    ranked = RankingService(engine, _settings()).list_ranking(job.id)
    assert len(ranked) == 5
    assert [r.rank for r in ranked] == [1, 2, 3, 4, 5]
    assert len({r.candidate_video_id for r in ranked}) == 5
    assert ranked[0].score_100 >= ranked[-1].score_100
    # Creative Director ran before production: storyboard + EDL exist.
    from app.agents.creative_director.service import CreativeDirectorService
    creative = CreativeDirectorService(engine, _settings())
    storyboard = creative.get_storyboard(job.id)
    assert storyboard.hook
    assert len(storyboard.scenes) == 7  # intro + 5 moments + outro
    edl = creative.get_edl(job.id)
    assert len(edl.edl["clips"]) == 5
    assert [c["rank"] for c in edl.edl["clips"]] == [5, 4, 3, 2, 1]
    # Production also ran: a real rendered video exists.
    from app.agents.production.service import ProductionService
    from app.agents.video_intelligence.probe import probe_video
    rendered = ProductionService(engine, _settings()).get_rendered_video(job.id)
    assert rendered.status == "ready"
    assert rendered.width == 1080 and rendered.height == 1920
    probe = probe_video(rendered.file_path)
    assert probe.video_codec == "h264" and probe.audio_codec == "aac"


# ---------------------------------------------------------------- API
def test_api_ranking_endpoints(engine):
    from fastapi.testclient import TestClient

    from main import app

    job = _new_job(engine)
    _seed_five(engine, job.id)
    RankingService(engine, _settings()).run(job.id)
    with TestClient(app) as client:
        agents = client.get("/agents").json()
        by_id = {a["agent_id"]: a for a in agents}
        assert by_id["ranking"]["status"] == "ready"
        assert client.get("/agents/ranking/health").json()["status"] == "ready"
        ranking = client.get(f"/jobs/{job.id}/ranking").json()
        assert len(ranking) == 5
        assert [r["rank"] for r in ranking] == [1, 2, 3, 4, 5]
        assert all(r["score_breakdown"] and "total" in r["score_breakdown"] for r in ranking)
        first = client.get(f"/jobs/{job.id}/ranking/1").json()
        assert first["rank"] == 1 and first["score"] == ranking[0]["score"]
        assert client.get(f"/jobs/{job.id}/ranking/9").status_code == 404
        assert client.get("/jobs/no-such-job/ranking").status_code == 404
        assert client.get("/jobs/no-such-job/ranking/1").status_code == 404
