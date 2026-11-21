"""Momentrix AI — Rights Verification Agent, Phases 2, 5, 6: service (Prompt 04).

Flow for one job:
  candidates (from Research) -> for each candidate:
      licence evidence (provider-published, plus an Internet Archive
      metadata lookup when the provider published none) -> evaluate()
      -> persist a RightsEvidence row -> update the candidate status
      -> approved candidates get an ApprovedFootage row
  -> return a RightsReport; the executor hands only the approved /
  rejected ids to the orchestrator as a validated RightsOutput.

Persistence is append-only with an identical-evidence guard:
re-running the agent for the same job does not create a duplicate
evidence row when nothing changed, and never duplicates an
ApprovedFootage row. A changed verdict appends a new row, so the
history of a candidate's rights checks is preserved; the gate
always reads the LATEST row.

Failure handling:
* no candidates for the job -> clear error (nothing to verify);
* zero approved candidates -> ExternalServiceError with the counts
  and the reasons — the job cannot proceed, and the agent never
  approves an unverified source merely to keep production moving.
  The orchestrator retries the stage, then fails the job with that
  message stored. An empty RightsOutput is never returned (its
  schema requires >= 1 approved id).
* Internet Archive metadata lookup fails -> the candidate stays
  NEEDS_REVIEW; the failure is recorded, never treated as a licence.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.agents.rights.evidence import ManualEvidence, RightsDecision, evaluate
from app.agents.rights.gate import approved_candidate_ids, is_approved
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError, NotFoundError, ValidationError
from app.models.models import ApprovedFootage, CandidateVideo, RightsEvidence
from app.orchestrator.jobs import JobService
from app.schemas.schemas import AttributionRead, RightsVerificationRead


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RightsReport(BaseModel):
    """Full result of one rights run (the executor returns only its ids)."""

    job_id: str
    approved: list[RightsVerificationRead] = Field(default_factory=list)
    rejected: list[RightsVerificationRead] = Field(default_factory=list)
    needs_review: list[RightsVerificationRead] = Field(default_factory=list)
    expired: list[RightsVerificationRead] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)


class RightsService:
    def __init__(self, engine: Engine, settings: Settings | None = None):
        self._engine = engine
        self.settings = settings or get_settings()
        self._sessions = sessionmaker(bind=engine)
        self.jobs = JobService(engine)

    # ------------------------------------------------------------ main entry
    def run(self, job_id: str) -> RightsReport:
        job = self.jobs.get_job(job_id)  # raises NotFoundError for unknown jobs
        with self._sessions() as session:
            candidates = (session.query(CandidateVideo)
                          .filter(CandidateVideo.job_id == job_id)
                          .order_by(CandidateVideo.created_at.asc()).all())
            if not candidates:
                raise ExternalServiceError(
                    f"Rights verification found no candidates for job {job_id!r} "
                    f"(topic {job.topic!r}). Run the Research Agent first.")
            verified: list[RightsVerificationRead] = []
            for candidate in candidates:
                verified.append(self._verify_in_session(session, candidate, ManualEvidence()))
            session.commit()
        approved = [v for v in verified if v.verdict == "approved"]
        rejected = [v for v in verified if v.verdict == "rejected"]
        needs_review = [v for v in verified if v.verdict == "needs_review"]
        expired = [v for v in verified if v.verdict == "expired"]
        if not approved:
            raise ExternalServiceError(
                f"Rights verification approved 0 of {len(verified)} candidates for "
                f"job {job_id} (topic {job.topic!r}): {len(rejected)} rejected, "
                f"{len(needs_review)} need review, {len(expired)} expired. "
                f"No candidate has evidence permitting commercial use, modification "
                f"and redistribution, so the job cannot proceed. Supply licence "
                f"evidence or a manual review (POST /jobs/{job_id}/rights/{{candidate_id}}/review) "
                f"and resume the job.")
        return RightsReport(
            job_id=job_id, approved=approved, rejected=rejected,
            needs_review=needs_review, expired=expired,
            limitations=self._limitations(verified),
            metrics={"total": len(verified), "approved": len(approved),
                     "rejected": len(rejected), "needs_review": len(needs_review),
                     "expired": len(expired)})

    # ------------------------------------------------------------ manual review
    def review_candidate(self, job_id: str, candidate_id: str,
                         manual: ManualEvidence) -> RightsVerificationRead:
        """Human review: supply evidence, the agent re-evaluates.

        The reviewer can add evidence (a licence URL, or a permission
        grant with where it is recorded) — but cannot force an
        approval: a restrictive licence still rejects, and a bare
        assertion without evidence is a validation error. A past
        expiry still expires.
        """
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            candidate = self._candidate_for_job(session, job_id, candidate_id)
            decision = evaluate(
                licence_raw=candidate.licence, title=candidate.title,
                creator=candidate.creator, url=candidate.url,
                platform=self._platform(session, candidate),
                manual=manual)
            # A reviewer-supplied licence URL is itself the evidence
            # string; the catalogue still decides what it permits.
            row = self._persist(session, candidate, decision, checked_by=manual.reviewed_by)
            self._sync_candidate(session, candidate, decision)
            session.commit()
            return self._read(row)

    # ------------------------------------------------------------ reads
    def list_rights(self, job_id: str) -> list[RightsVerificationRead]:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            candidates = (session.query(CandidateVideo)
                          .filter(CandidateVideo.job_id == job_id)
                          .order_by(CandidateVideo.created_at.asc()).all())
            out: list[RightsVerificationRead] = []
            for candidate in candidates:
                row = self._latest(session, candidate.id)
                if row is not None:
                    out.append(self._read(row))
            return out

    def get_rights(self, job_id: str, candidate_id: str) -> RightsVerificationRead:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            candidate = self._candidate_for_job(session, job_id, candidate_id)
            row = self._latest(session, candidate.id)
            if row is None:
                raise NotFoundError(
                    f"no rights evidence for candidate {candidate_id} — "
                    "run the Rights Verification Agent first")
            return self._read(row)

    def list_attribution(self, job_id: str) -> list[AttributionRead]:
        """Attribution for the job's APPROVED candidates only — the
        text the Creative Director / Production agents must render
        when it is required (and may render when only recommended)."""
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            out: list[AttributionRead] = []
            for candidate_id in approved_candidate_ids(session, job_id):
                row = self._latest(session, candidate_id)
                candidate = session.query(CandidateVideo).filter(
                    CandidateVideo.id == candidate_id).first()
                if row is None or candidate is None:
                    continue
                out.append(AttributionRead(
                    candidate_video_id=candidate_id, title=candidate.title,
                    attribution_text=row.attribution_text,
                    attribution_required=bool(row.attribution_required),
                    licence_name=row.license_name))
            return out

    # ------------------------------------------------------------ verification
    def _verify_in_session(self, session, candidate: CandidateVideo,
                           manual: ManualEvidence) -> RightsVerificationRead:
        licence_raw = candidate.licence
        if licence_raw is None:
            # Second-chance evidence: the Internet Archive metadata API
            # sometimes publishes a licenseurl the search response did
            # not carry. This is provider-published metadata, fetched
            # live — never invented. A failed lookup leaves the
            # candidate NEEDS_REVIEW.
            fetched = self._fetch_ia_licence(candidate)
            if fetched:
                licence_raw = fetched
                candidate.licence = fetched
        decision = evaluate(
            licence_raw=licence_raw, title=candidate.title,
            creator=candidate.creator, url=candidate.url,
            platform=self._platform(session, candidate), manual=manual)
        row = self._persist(session, candidate, decision, checked_by="rights_agent")
        self._sync_candidate(session, candidate, decision)
        return self._read(row)

    def _fetch_ia_licence(self, candidate: CandidateVideo) -> str | None:
        if not self.settings.rights_fetch_ia_metadata:
            return None
        source = candidate.source
        if source is None or source.platform != "internet_archive":
            return None
        try:
            from app.agents.research.providers import _http_client

            with _http_client(self.settings.rights_ia_metadata_timeout_seconds) as client:
                response = client.get(f"https://archive.org/metadata/{candidate.external_video_id}")
            response.raise_for_status()
            metadata = response.json().get("metadata") or {}
            value = metadata.get("licenseurl")
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list) and value:
                first = str(value[0]).strip()
                return first or None
        except Exception:
            return None
        return None

    # ------------------------------------------------------------ persistence
    @staticmethod
    def _latest(session, candidate_id: str) -> RightsEvidence | None:
        return (session.query(RightsEvidence)
                .filter(RightsEvidence.candidate_video_id == candidate_id)
                .order_by(RightsEvidence.created_at.desc(),
                          RightsEvidence.id.desc()).first())

    def _persist(self, session, candidate: CandidateVideo, decision: RightsDecision,
                 *, checked_by: str) -> RightsEvidence:
        """Append a new evidence row, unless the latest row is
        identical (a re-run with nothing changed creates no duplicate)."""
        latest = self._latest(session, candidate.id)
        if latest is not None and self._same_as(latest, decision, checked_by):
            return latest
        row = RightsEvidence(
            candidate_video_id=candidate.id,
            licence=candidate.licence,
            verdict=decision.verdict,
            evidence_url=decision.licence_url or decision.permission_evidence,
            notes="; ".join(decision.reasons) if decision.reasons else None,
            checked_by=checked_by,
            license_name=decision.licence_name,
            license_url=decision.licence_url,
            permission_evidence=decision.permission_evidence,
            verification_date=_utcnow(),
            commercial_use=decision.commercial_use,
            modification_allowed=decision.modification,
            redistribution_allowed=decision.redistribution,
            attribution_required=decision.attribution_required,
            attribution_text=decision.attribution_text,
            geographic_restrictions=decision.geographic_restrictions,
            time_restrictions=decision.time_restrictions,
            expires_at=decision.expires_at,
            verification_method=decision.verification_method,
            reviewer_status=decision.reviewer_status,
            reviewed_by=checked_by if decision.reviewer_status == "reviewed" else None,
            reviewed_at=_utcnow() if decision.reviewer_status == "reviewed" else None,
            reasons=list(decision.reasons),
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def _same_as(row: RightsEvidence, decision: RightsDecision, checked_by: str) -> bool:
        return (
            row.verdict == decision.verdict
            and (row.license_name or None) == (decision.licence_name or None)
            and (row.license_url or None) == (decision.licence_url or None)
            and (row.permission_evidence or None) == (decision.permission_evidence or None)
            and row.commercial_use == decision.commercial_use
            and row.verification_method == decision.verification_method
            and row.checked_by == checked_by
        )

    def _sync_candidate(self, session, candidate: CandidateVideo,
                        decision: RightsDecision) -> None:
        """Candidate status follows the latest verdict, and approved
        candidates get (exactly one) ApprovedFootage row."""
        candidate.status = {
            "approved": "rights_approved",
            "rejected": "rights_rejected",
            "needs_review": "rights_pending",
            "expired": "rights_pending",
        }[decision.verdict]
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate.id).first())
        if decision.verdict == "approved":
            if footage is None:
                session.add(ApprovedFootage(
                    job_id=candidate.job_id, candidate_video_id=candidate.id,
                    rights_evidence_id=None, duration_seconds=candidate.duration_seconds))
            # Link the footage row to the latest evidence row.
            latest = self._latest(session, candidate.id)
            if latest is not None:
                row = (session.query(ApprovedFootage)
                       .filter(ApprovedFootage.candidate_video_id == candidate.id).first())
                if row is not None:
                    row.rights_evidence_id = latest.id
        elif footage is not None:
            session.delete(footage)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _candidate_for_job(session, job_id: str, candidate_id: str) -> CandidateVideo:
        candidate = (session.query(CandidateVideo)
                     .filter(CandidateVideo.id == candidate_id,
                             CandidateVideo.job_id == job_id).first())
        if candidate is None:
            raise NotFoundError(
                f"candidate {candidate_id} not found for job {job_id}")
        return candidate

    @staticmethod
    def _platform(session, candidate: CandidateVideo) -> str:
        return candidate.source.platform if candidate.source else ""

    @staticmethod
    def _read(row: RightsEvidence) -> RightsVerificationRead:
        reasons: list[str] = []
        if isinstance(row.reasons, list):
            reasons = [str(r) for r in row.reasons]
        elif isinstance(row.reasons, str):
            # Rows written before the JSON column was used directly.
            try:
                parsed = json.loads(row.reasons)
                if isinstance(parsed, list):
                    reasons = [str(r) for r in parsed]
                else:
                    reasons = [row.reasons]
            except (ValueError, TypeError):
                reasons = [row.reasons]
        return RightsVerificationRead(
            id=row.id, candidate_video_id=row.candidate_video_id,
            licence=row.licence, verdict=row.verdict,  # type: ignore[arg-type]
            evidence_url=row.evidence_url, notes=row.notes,
            checked_by=row.checked_by, created_at=row.created_at,
            license_name=row.license_name, license_url=row.license_url,
            permission_evidence=row.permission_evidence,
            verification_date=row.verification_date,
            commercial_use=bool(row.commercial_use),
            modification_allowed=bool(row.modification_allowed),
            redistribution_allowed=bool(row.redistribution_allowed),
            attribution_required=bool(row.attribution_required),
            attribution_text=row.attribution_text,
            geographic_restrictions=row.geographic_restrictions,
            time_restrictions=row.time_restrictions,
            expires_at=row.expires_at,
            verification_method=row.verification_method,
            reviewer_status=row.reviewer_status,
            reviewed_by=row.reviewed_by, reviewed_at=row.reviewed_at,
            reasons=reasons)

    @staticmethod
    def _limitations(verified: list[RightsVerificationRead]) -> list[str]:
        limitations: list[str] = []
        if any(v.verdict == "needs_review" for v in verified):
            limitations.append(
                "Candidates marked needs_review have no usable licence evidence — "
                "a public URL is not proof of permission. Supply licence evidence "
                "or a manual review before they can be used.")
        if any(v.verdict == "rejected" for v in verified):
            limitations.append(
                "Rejected candidates carry licences that forbid commercial use, "
                "modification or redistribution (or a standard platform licence); "
                "they must not enter the pipeline.")
        if any(v.verdict == "expired" for v in verified):
            limitations.append(
                "Expired candidates need renewed permission evidence before use.")
        return limitations

    # Re-export for tests / downstream agents that need the gate.
    def is_candidate_approved(self, job_id: str, candidate_id: str) -> bool:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            return is_approved(session, candidate_id)
