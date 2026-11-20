"""Rights Verification Agent tests (Prompt 04): licence catalogue,
evaluation, evidence persistence, manual review, the server-side
gate, attribution, orchestrator integration and the rights API.

The strict rule under test throughout: an unverified source is
never approved merely to keep production moving. Missing evidence
is NEEDS_REVIEW, a restrictive licence is REJECTED, an expired
permission is EXPIRED — approval needs evidence that covers
commercial use, modification and redistribution.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.agents.rights.attribution import build_attribution
from app.agents.rights.evidence import ManualEvidence, evaluate
from app.agents.rights.gate import (
    approved_candidate_ids,
    is_approved,
    latest_verdict,
    require_approved,
)
from app.agents.rights.licenses import parse_licence, rejection_reason
from app.agents.rights.service import RightsService
from app.core.config import Settings
from app.core.errors import ExternalServiceError, NotFoundError, ValidationError
from app.models.models import ApprovedFootage, CandidateVideo, RightsEvidence, Source
from app.orchestrator.engine import WorkflowEngine
from app.orchestrator.jobs import JobService
from app.orchestrator.registry import build_default_registry
from app.schemas.schemas import JobCreate


def _settings(**overrides) -> Settings:
    base = {"orchestrator_backoff_base_seconds": 0.01,
            "orchestrator_timeout_seconds": 10.0,
            "rights_fetch_ia_metadata": False}
    base.update(overrides)
    return Settings(**base)


def _new_job(engine, topic="TOP 5 CRAZIEST PARKOUR FAILS"):
    return JobService(engine).create_job(JobCreate(topic=topic))


def _seed_candidate(engine, job_id, external_id, *, licence=None, platform="internet_archive",
                    title="Parkour Fails", creator="MACSD"):
    """A candidate exactly as Research would have persisted it."""
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        source = (session.query(Source)
                  .filter(Source.platform == platform, Source.external_id == external_id).first())
        if source is None:
            source = Source(platform=platform, external_id=external_id, title=title,
                            url=f"https://archive.org/details/{external_id}")
            session.add(source)
            session.flush()
        candidate = CandidateVideo(
            job_id=job_id, source_id=source.id, external_video_id=external_id,
            title=title, url=f"https://archive.org/details/{external_id}",
            duration_seconds=120.0, creator=creator, licence=licence)
        session.add(candidate)
        session.commit()
        return candidate.id


def _licence_url(variant: str, version: str = "4.0") -> str:
    return f"https://creativecommons.org/licenses/{variant}/{version}/"


# ---------------------------------------------------------------- Phase 1: licence catalogue
@pytest.mark.parametrize("raw,commercial,modification", [
    (_licence_url("by", "4.0"), True, True),
    (_licence_url("by", "3.0"), True, True),
    (_licence_url("by-sa", "4.0"), True, True),
    ("CC BY 4.0", True, True),
    ("CC-BY-SA 3.0", True, True),
    ("https://creativecommons.org/publicdomain/zero/1.0/", True, True),
])
def test_permissive_licences_allow_intended_use(raw, commercial, modification):
    info = parse_licence(raw)
    assert info is not None
    assert info.commercial_use is commercial
    assert info.modification is modification
    assert info.redistribution is True
    assert rejection_reason(info) is None


@pytest.mark.parametrize("raw", [
    _licence_url("by-nc", "4.0"),
    _licence_url("by-nd", "4.0"),
    _licence_url("by-nc-sa", "3.0"),
    _licence_url("by-nc-nd", "4.0"),
    "CC BY-NC 4.0",
    "standard YouTube licence",
    "All Rights Reserved",
    "test-only — not a real source",
])
def test_restrictive_licences_forbid_intended_use(raw):
    info = parse_licence(raw)
    assert info is not None
    assert rejection_reason(info), f"{raw} must carry a rejection reason"
    assert not (info.commercial_use and info.modification and info.redistribution)


@pytest.mark.parametrize("raw", [None, "", "   ", "https://example.com/some-licence",
                                 "Creative Commons Attribution-NonCommercial"])
def test_unknown_or_missing_licence_is_not_parsed(raw):
    assert parse_licence(raw) is None


def test_public_domain_mark_is_known_but_not_a_licence():
    info = parse_licence("https://creativecommons.org/publicdomain/mark/1.0/")
    assert info is not None
    assert info.is_licence_grant is False
    assert info.key == "public-domain-mark"


def test_catalogue_urls_are_canonical():
    assert parse_licence(_licence_url("by", "3.0")).url == _licence_url("by", "3.0")
    assert parse_licence(_licence_url("by-sa", "4.0")).url == _licence_url("by-sa", "4.0")


# ---------------------------------------------------------------- Phases 1-3: evaluation
def test_evaluate_approves_cc_by_with_attribution():
    decision = evaluate(licence_raw=_licence_url("by", "3.0"), title="Parkour Fails",
                        creator="MACSD", url="https://archive.org/details/x",
                        platform="internet_archive")
    assert decision.verdict == "approved"
    assert decision.commercial_use and decision.modification and decision.redistribution
    assert decision.attribution_required is True
    assert "MACSD" in (decision.attribution_text or "")
    assert "by/3.0" in (decision.attribution_text or "")


def test_evaluate_approves_cc0_without_attribution_requirement():
    decision = evaluate(licence_raw="https://creativecommons.org/publicdomain/zero/1.0/",
                        title="Clip", creator=None, url=None, platform="internet_archive")
    assert decision.verdict == "approved"
    assert decision.attribution_required is False


def test_evaluate_rejects_noncommercial_and_noderivatives():
    for raw in (_licence_url("by-nc", "4.0"), _licence_url("by-nd", "4.0")):
        decision = evaluate(licence_raw=raw, title="T", creator="C", url=None,
                            platform="internet_archive")
        assert decision.verdict == "rejected"
        assert decision.reasons


def test_evaluate_missing_licence_needs_review_not_approval():
    decision = evaluate(licence_raw=None, title="Parkour Fails", creator="FailArmy",
                        url="https://archive.org/details/x", platform="internet_archive")
    assert decision.verdict == "needs_review"
    assert "not proof of permission" in " ".join(decision.reasons)


def test_evaluate_public_domain_mark_needs_human_review():
    decision = evaluate(licence_raw="https://creativecommons.org/publicdomain/mark/1.0/",
                        title="T", creator="C", url=None, platform="internet_archive")
    assert decision.verdict == "needs_review"


def test_evaluate_expired_permission_beats_permissive_licence():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    decision = evaluate(licence_raw=_licence_url("by", "4.0"), title="T", creator="C",
                        url=None, platform="internet_archive",
                        manual=ManualEvidence(expires_at=past))
    assert decision.verdict == "expired"


def test_evaluate_future_expiry_does_not_block_approval():
    future = datetime.now(timezone.utc) + timedelta(days=30)
    decision = evaluate(licence_raw=_licence_url("by", "4.0"), title="T", creator="C",
                        url=None, platform="internet_archive",
                        manual=ManualEvidence(expires_at=future))
    assert decision.verdict == "approved"
    assert decision.expires_at == future


def test_manual_permission_grant_needs_evidence():
    with pytest.raises(ValueError, match="permission_evidence"):
        evaluate(licence_raw=None, title="T", creator="C", url=None,
                 platform="internet_archive",
                 manual=ManualEvidence(permission_granted=True, reviewed_by="logesh"))


def test_manual_permission_grant_with_evidence_approves():
    decision = evaluate(
        licence_raw=None, title="T", creator="C", url=None, platform="internet_archive",
        manual=ManualEvidence(permission_granted=True,
                              permission_evidence="Creator email 2026-11-20 granting commercial use",
                              reviewed_by="logesh"))
    assert decision.verdict == "approved"
    assert decision.verification_method == "manual_review"
    assert decision.reviewer_status == "reviewed"


def test_manual_evidence_cannot_override_restrictive_licence():
    decision = evaluate(
        licence_raw=_licence_url("by-nc", "4.0"), title="T", creator="C", url=None,
        platform="internet_archive",
        manual=ManualEvidence(permission_granted=True,
                              permission_evidence="reviewer says it is fine",
                              reviewed_by="logesh"))
    assert decision.verdict == "rejected"


def test_attribution_without_creator_does_not_invent_one():
    text = build_attribution(title="Clip", creator=None, url="https://x",
                             licence=parse_licence(_licence_url("by", "4.0")))
    assert text is not None and "Unknown creator" in text


# ---------------------------------------------------------------- Phase 2/5: service + persistence
def test_service_verifies_mixed_candidates(engine):
    job = _new_job(engine)
    approved_id = _seed_candidate(engine, job.id, "cc-clip", licence=_licence_url("by", "3.0"))
    nc_id = _seed_candidate(engine, job.id, "nc-clip", licence=_licence_url("by-nc", "4.0"))
    none_id = _seed_candidate(engine, job.id, "no-licence-clip", licence=None)
    report = RightsService(engine, _settings()).run(job.id)
    assert report.metrics == {"total": 3, "approved": 1, "rejected": 1,
                              "needs_review": 1, "expired": 0}
    assert [v.candidate_video_id for v in report.approved] == [approved_id]
    assert [v.candidate_video_id for v in report.rejected] == [nc_id]
    assert [v.candidate_video_id for v in report.needs_review] == [none_id]
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        statuses = {c.external_video_id: c.status for c in
                    session.query(CandidateVideo).filter(CandidateVideo.job_id == job.id)}
        assert statuses == {"cc-clip": "rights_approved", "nc-clip": "rights_rejected",
                            "no-licence-clip": "rights_pending"}
        footage = session.query(ApprovedFootage).filter(
            ApprovedFootage.job_id == job.id).all()
        assert len(footage) == 1 and footage[0].candidate_video_id == approved_id
        assert footage[0].rights_evidence_id is not None


def test_service_fails_honestly_when_nothing_is_approved(engine):
    job = _new_job(engine)
    _seed_candidate(engine, job.id, "no-licence-1", licence=None)
    _seed_candidate(engine, job.id, "nc-1", licence=_licence_url("by-nc", "4.0"))
    with pytest.raises(ExternalServiceError, match="approved 0 of 2"):
        RightsService(engine, _settings()).run(job.id)
    # ...but the evidence was still persisted, and nothing was approved.
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert session.query(ApprovedFootage).filter(
            ApprovedFootage.job_id == job.id).count() == 0
        candidate_ids = [c.id for c in session.query(CandidateVideo).filter(
            CandidateVideo.job_id == job.id)]
        verdicts = {r.verdict for r in session.query(RightsEvidence).filter(
            RightsEvidence.candidate_video_id.in_(candidate_ids))}
        assert verdicts == {"needs_review", "rejected"}


def test_service_rejects_job_with_no_candidates(engine):
    job = _new_job(engine)
    with pytest.raises(ExternalServiceError, match="no candidates"):
        RightsService(engine, _settings()).run(job.id)


def test_service_unknown_job(engine):
    with pytest.raises(NotFoundError):
        RightsService(engine, _settings()).run("does-not-exist")


def test_service_rerun_is_idempotent(engine):
    job = _new_job(engine)
    _seed_candidate(engine, job.id, "cc-clip", licence=_licence_url("by", "4.0"))
    service = RightsService(engine, _settings())
    service.run(job.id)
    service.run(job.id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        candidate_ids = [c.id for c in session.query(CandidateVideo).filter(
            CandidateVideo.job_id == job.id)]
        assert session.query(RightsEvidence).filter(
            RightsEvidence.candidate_video_id.in_(candidate_ids)).count() == 1
        assert session.query(ApprovedFootage).filter(
            ApprovedFootage.job_id == job.id).count() == 1


def test_live_ia_metadata_lookup_finds_cc_licence(engine):
    """Live check: the Archive metadata API publishes a CC BY licence
    for youtube-8q8wKM7mHyA and none for macsdca-Parkour_Fails."""
    job = _new_job(engine)
    cc_id = _seed_candidate(engine, job.id, "youtube-8q8wKM7mHyA", licence=None)
    none_id = _seed_candidate(engine, job.id, "macsdca-Parkour_Fails", licence=None)
    report = RightsService(engine, _settings(rights_fetch_ia_metadata=True)).run(job.id)
    assert [v.candidate_video_id for v in report.approved] == [cc_id]
    assert [v.candidate_video_id for v in report.needs_review] == [none_id]


# ---------------------------------------------------------------- Phase 3: manual review
def test_manual_review_approves_with_permission_grant(engine):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "no-licence-clip", licence=None)
    reviewed = RightsService(engine, _settings()).review_candidate(
        job.id, candidate_id,
        ManualEvidence(permission_granted=True,
                       permission_evidence="Creator DM 2026-11-20: yes, use it commercially",
                       reviewed_by="logesh"))
    assert reviewed.verdict == "approved"
    assert reviewed.reviewer_status == "reviewed"
    assert reviewed.reviewed_by == "logesh"
    assert reviewed.verification_method == "manual_review"


def test_manual_review_cannot_force_approval_of_nc_licence(engine):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "nc-clip",
                                   licence=_licence_url("by-nc", "4.0"))
    reviewed = RightsService(engine, _settings()).review_candidate(
        job.id, candidate_id,
        ManualEvidence(permission_granted=True,
                       permission_evidence="reviewer says it is fine",
                       reviewed_by="logesh"))
    assert reviewed.verdict == "rejected"


def test_manual_review_with_past_expiry_is_expired(engine):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url("by", "4.0"))
    reviewed = RightsService(engine, _settings()).review_candidate(
        job.id, candidate_id,
        ManualEvidence(expires_at=datetime.now(timezone.utc) - timedelta(days=1),
                       reviewed_by="logesh"))
    assert reviewed.verdict == "expired"


def test_manual_review_unknown_candidate(engine):
    job = _new_job(engine)
    with pytest.raises(NotFoundError):
        RightsService(engine, _settings()).review_candidate(
            job.id, "no-such-candidate", ManualEvidence(reviewed_by="logesh"))


def test_attribution_lists_only_approved_candidates(engine):
    job = _new_job(engine)
    approved_id = _seed_candidate(engine, job.id, "cc-clip",
                                  licence=_licence_url("by", "3.0"), creator="Allie Kitaguchi")
    _seed_candidate(engine, job.id, "no-licence-clip", licence=None)
    service = RightsService(engine, _settings())
    service.run(job.id)
    attribution = service.list_attribution(job.id)
    assert len(attribution) == 1
    assert attribution[0].candidate_video_id == approved_id
    assert attribution[0].attribution_required is True
    assert "Allie Kitaguchi" in (attribution[0].attribution_text or "")


# ---------------------------------------------------------------- Phase 4: gate
def test_gate_allows_only_approved(engine):
    job = _new_job(engine)
    approved_id = _seed_candidate(engine, job.id, "cc-clip", licence=_licence_url("by", "4.0"))
    blocked_id = _seed_candidate(engine, job.id, "no-licence-clip", licence=None)
    RightsService(engine, _settings()).run(job.id)
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert is_approved(session, approved_id) is True
        assert is_approved(session, blocked_id) is False
        assert latest_verdict(session, blocked_id) == "needs_review"
        assert approved_candidate_ids(session, job.id) == [approved_id]
        require_approved(session, job.id, [approved_id], purpose="video_analysis")
        with pytest.raises(ValidationError, match="not approved"):
            require_approved(session, job.id, [approved_id, blocked_id],
                             purpose="video_production")
        with pytest.raises(ValidationError, match="not approved"):
            require_approved(session, job.id, ["unknown-id"], purpose="video_analysis")


def test_gate_expired_approval_fails_closed(engine):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip",
                                   licence=_licence_url("by", "4.0"))
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        session.add(RightsEvidence(
            candidate_video_id=candidate_id, licence=_licence_url("by", "4.0"),
            verdict="approved", checked_by="rights_agent",
            expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
        session.commit()
        assert is_approved(session, candidate_id) is False
        with pytest.raises(ValidationError, match="not approved"):
            require_approved(session, job.id, [candidate_id],
                             purpose="video_production")


def test_gate_never_checked_fails_closed(engine):
    job = _new_job(engine)
    candidate_id = _seed_candidate(engine, job.id, "cc-clip", licence=_licence_url("by", "4.0"))
    sessions = sessionmaker(bind=engine)
    with sessions() as session:
        assert is_approved(session, candidate_id) is False
        assert latest_verdict(session, candidate_id) is None


# ---------------------------------------------------------------- orchestrator integration
def test_orchestrator_runs_real_rights_then_video_analysis_needs_footage(engine):
    job = _new_job(engine)
    _seed_candidate(engine, job.id, "cc-clip", licence=_licence_url("by", "4.0"))
    settings = _settings(research_provider="test")
    registry = build_default_registry(engine, settings)
    assert registry.get("research").status == "ready"
    assert registry.get("rights").status == "ready"
    # Research (test provider) adds 5 test-only candidates, all
    # rejected; the seeded CC BY candidate is the only approval, so
    # rights passes. Prompt 05 built the video agent, so the run no
    # longer stops with 501 at VIDEO_ANALYSIS: it runs the agent,
    # which fails honestly because the approved candidate has no
    # local footage file (downloading is not configured).
    runner = WorkflowEngine(engine, settings, registry)
    result = runner.run_job(job.id)
    assert result.status == "failed"
    assert result.error_message is not None
    assert "VIDEO_ANALYSIS" in result.error_message
    from app.orchestrator.states import WorkflowStage
    assert WorkflowStage.RIGHTS_VERIFICATION in JobService(engine).completed_stages(job.id)
    assert JobService(engine).get_stage(job.id).value == "FAILED"


def test_orchestrator_fails_job_when_nothing_approved(engine):
    job = _new_job(engine)
    registry = build_default_registry(engine, _settings(research_provider="test"))
    runner = WorkflowEngine(engine, _settings(research_provider="test"), registry)
    result = runner.run_job(job.id)
    assert result.status == "failed"
    assert result.error_message is not None
    assert "RIGHTS_VERIFICATION" in result.error_message
    assert "approved 0 of" in result.error_message


# ---------------------------------------------------------------- API
def test_api_rights_endpoints():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        agents = client.get("/agents").json()
        by_id = {a["agent_id"]: a for a in agents}
        assert by_id["rights"]["status"] == "ready"
        assert client.get("/agents/rights/health").json()["status"] == "ready"

        created = client.post("/jobs", json={"topic": "TOP 5 CRAZIEST PARKOUR FAILS"})
        job_id = created.json()["id"]
        assert client.get(f"/jobs/{job_id}/rights").json() == []
        assert client.get(f"/jobs/{job_id}/attribution").json() == []
        assert client.get(f"/jobs/{job_id}/rights/no-such-candidate").status_code == 404
        assert client.get("/jobs/no-such-job/rights").status_code == 404

        # Manual review of an unknown candidate is 404, not a silent pass.
        review = client.post(
            f"/jobs/{job_id}/rights/no-such-candidate/review",
            json={"reviewer": "logesh", "permission_granted": True,
                  "permission_evidence": "creator said yes"})
        assert review.status_code == 404
