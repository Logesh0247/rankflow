"""Momentrix AI — Rights Verification Agent, Phase 4: enforcement (Prompt 04).

Server-side permission checks. This is the ONLY way downstream
agents (Video Intelligence, Video Production, and anything later)
may decide a source is usable: they call `require_approved`
before touching a candidate, and it raises unless the candidate's
LATEST rights evidence says ``approved`` and is not expired.

It is not a UI warning and it cannot be bypassed by passing a
different status string: the verdict is read from the database,
and "approved" is the only passing value. ``needs_review``,
``rejected``, ``expired`` and "never checked" all fail closed.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.models.models import CandidateVideo, RightsEvidence


def _now() -> datetime:
    return datetime.now(timezone.utc)


def latest_evidence(session: Session, candidate_id: str) -> RightsEvidence | None:
    """The newest RightsEvidence row for a candidate, or None."""
    return (session.query(RightsEvidence)
            .filter(RightsEvidence.candidate_video_id == candidate_id)
            .order_by(RightsEvidence.created_at.desc(),
                      RightsEvidence.id.desc())
            .first())


def latest_verdict(session: Session, candidate_id: str) -> str | None:
    evidence = latest_evidence(session, candidate_id)
    return evidence.verdict if evidence else None


def is_approved(session: Session, candidate_id: str) -> bool:
    """True only when the latest evidence is ``approved`` and any
    expiry on it is still in the future. Everything else fails closed."""
    evidence = latest_evidence(session, candidate_id)
    if evidence is None or evidence.verdict != "approved":
        return False
    expires_at = evidence.expires_at
    if expires_at is not None:
        aware = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        if aware <= _now():
            return False
    return True


def approved_candidate_ids(session: Session, job_id: str) -> list[str]:
    """All candidates of a job whose latest evidence is approved."""
    candidates = (session.query(CandidateVideo)
                  .filter(CandidateVideo.job_id == job_id).all())
    return [c.id for c in candidates if is_approved(session, c.id)]


def require_approved(session: Session, job_id: str, candidate_ids: list[str],
                     *, purpose: str) -> None:
    """Server-side gate for downstream agents.

    Raises ValidationError (HTTP 422) listing every candidate that
    is not approved, with its verdict. Call before analysing or
    rendering any candidate — never after.
    """
    if not candidate_ids:
        raise ValidationError(
            f"rights gate ({purpose}): no candidate ids supplied — nothing is approved")
    blocked: list[str] = []
    for candidate_id in candidate_ids:
        candidate = session.query(CandidateVideo).filter(
            CandidateVideo.id == candidate_id).first()
        if candidate is None:
            blocked.append(f"{candidate_id} (unknown candidate)")
            continue
        if candidate.job_id != job_id:
            blocked.append(f"{candidate_id} (belongs to another job)")
            continue
        if not is_approved(session, candidate_id):
            verdict = latest_verdict(session, candidate_id) or "not_checked"
            blocked.append(f"{candidate_id} (verdict: {verdict})")
    if blocked:
        raise ValidationError(
            f"rights gate ({purpose}): {len(blocked)} candidate(s) are not approved "
            f"for the intended use and must not be analysed or produced: "
            + "; ".join(blocked))
