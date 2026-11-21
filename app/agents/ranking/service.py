"""Momentrix AI — Ranking Agent, Phases 4-6: service (Prompt 06).

Flow for one job:
  AnalyzedMoment rows (from Video Intelligence)
  -> skip moments whose candidate is not Rights-approved
     (the gate is re-checked here, server-side, before any
     moment is ranked — a candidate whose approval expired or
     was revoked after analysis is never selected)
  -> score every remaining moment from its measured signals
  -> drop duplicates (same moment, overlapping same-source
     moments, same-event re-uploads)
  -> drop moments below the configured minimum score
  -> keep the best moment per source video
  -> select exactly five, from five different sources, ranked
     #1 (best) to #5; persist RankingResult rows with the full
     component breakdown and the explanation.

Failure handling (never silent, never fabricated):
* no moments at all for the job -> ExternalServiceError;
* no approved moments -> ExternalServiceError (rights, not ranking);
* fewer than five eligible sources after dedup / floor ->
  ExternalServiceError naming the counts and the recovery:
  request more candidates from Research (a broader topic, more
  approved footage) and re-run ranking. No moment is invented
  to fill the five, and rights approval is never bypassed to
  reach five. The orchestrator retries the stage, then fails
  the job honestly with the reason stored.
* re-running replaces the job's ranking rows, so a re-run never
  duplicates ranks (the DB also enforces one row per rank and
  one row per moment per job).

The executor hands only the five ranked entries to the
orchestrator, validated against RankingOutput; the full report
(scores, duplicates dropped, counts, limitations) is persisted
in the database rows and the orchestrator log detail.
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.agents.ranking.scoring import (
    SCORER, MomentSignals, ScoredMoment, deduplicate, rank_key, score_moment,
)
from app.agents.rights.gate import is_approved, latest_verdict
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError, NotFoundError
from app.models.models import AnalyzedMoment, CandidateVideo, RankingResult
from app.orchestrator.jobs import JobService
from app.schemas.schemas import RankingResultRead

REQUIRED_RANKS = 5


class RankedEntry(BaseModel):
    """One selected moment, in the final ranking."""

    rank: int = Field(..., ge=1, le=5)
    moment_id: str
    candidate_video_id: str
    score: float = Field(..., ge=0, le=1)  # total / 100, the shared contract scale
    score_100: float = Field(..., ge=0, le=100)
    components: dict[str, float] = Field(default_factory=dict)
    explanation: str


class RankingReport(BaseModel):
    """Full result of one ranking run (the executor returns only its ranks)."""

    job_id: str
    scorer: str = SCORER
    ranks: list[RankedEntry] = Field(default_factory=list)
    dropped_duplicates: list[dict] = Field(default_factory=list)
    below_floor: list[str] = Field(default_factory=list)
    unapproved_skipped: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)


class RankingService:
    def __init__(self, engine: Engine, settings: Settings | None = None):
        self._engine = engine
        self.settings = settings or get_settings()
        self._sessions = sessionmaker(bind=engine)
        self.jobs = JobService(engine)

    # ------------------------------------------------------------ main entry
    def run(self, job_id: str) -> RankingReport:
        job = self.jobs.get_job(job_id)  # raises NotFoundError for unknown jobs
        started = time.monotonic()
        with self._sessions() as session:
            rows = (session.query(AnalyzedMoment)
                    .filter(AnalyzedMoment.job_id == job_id)
                    .order_by(AnalyzedMoment.candidate_video_id.asc(),
                              AnalyzedMoment.start_seconds.asc()).all())
            if not rows:
                raise ExternalServiceError(
                    f"Ranking found no analysed moments for job {job_id!r}: "
                    "Video Intelligence produced nothing to rank — run "
                    "analysis first, do not invent moments")
            scored, unapproved = self._score_approved(session, job_id, rows)
            if not scored:
                raise ExternalServiceError(
                    f"Ranking found no approved moments for job {job_id!r}: "
                    f"{len(unapproved)} moment(s) were skipped because their "
                    "candidate is not Rights-approved — approve more footage "
                    "or request more candidates; rights approval is never bypassed")
            kept, dropped = deduplicate(
                scored, iou_threshold=self.settings.ranking_duplicate_iou,
                fingerprint_tolerance=self.settings.ranking_fingerprint_tolerance)
            eligible, below_floor = self._apply_floor(kept)
            by_source = self._best_per_source(eligible)
            if len(by_source) < REQUIRED_RANKS:
                raise ExternalServiceError(
                    self._insufficient_message(job_id, rows, scored, dropped,
                                               below_floor, unapproved, by_source))
            selected = sorted(by_source, key=rank_key)[:REQUIRED_RANKS]
            entries = self._persist(session, job_id, selected)
            session.commit()
        return RankingReport(
            job_id=job.id, ranks=entries, dropped_duplicates=dropped,
            below_floor=[m.moment_id for m in below_floor],
            unapproved_skipped=unapproved,
            limitations=self._limitations(),
            metrics={
                "moments_seen": len(rows),
                "approved_moments": len(scored),
                "unapproved_skipped": len(unapproved),
                "duplicates_dropped": len(dropped),
                "below_floor": len(below_floor),
                "eligible_sources": len(by_source),
                "ranked": len(entries),
                "duration_ms": int((time.monotonic() - started) * 1000),
            })

    # ------------------------------------------------------------ scoring
    def _score_approved(self, session, job_id: str, rows: list[AnalyzedMoment]
                        ) -> tuple[list[ScoredMoment], list[dict]]:
        """Score moments whose candidate is still Rights-approved.

        The gate is re-checked per candidate, server-side: an
        approval that expired or was revoked after Video
        Intelligence ran must not leak into the ranking."""
        scored: list[ScoredMoment] = []
        unapproved: list[dict] = []
        candidates: dict[str, CandidateVideo] = {}
        for row in rows:
            candidate = candidates.get(row.candidate_video_id)
            if candidate is None:
                candidate = (session.query(CandidateVideo)
                             .filter(CandidateVideo.id == row.candidate_video_id).first())
                candidates[row.candidate_video_id] = candidate
            if candidate is None or candidate.job_id != job_id:
                unapproved.append({"moment_id": row.id,
                                   "reason": "candidate not found for this job"})
                continue
            if not is_approved(session, row.candidate_video_id):
                verdict = latest_verdict(session, row.candidate_video_id) or "not_checked"
                unapproved.append({"moment_id": row.id,
                                   "candidate_video_id": row.candidate_video_id,
                                   "reason": f"candidate not Rights-approved (verdict: {verdict})"})
                continue
            scored.append(score_moment(self._signals(row, candidate)))
        return scored, unapproved

    @staticmethod
    def _signals(row: AnalyzedMoment, candidate: CandidateVideo) -> MomentSignals:
        labels = dict(row.labels or {})
        probe = labels.get("probe") or {}
        verification = labels.get("verification") or {}
        return MomentSignals(
            moment_id=row.id, candidate_video_id=row.candidate_video_id,
            start_seconds=row.start_seconds, end_seconds=row.end_seconds,
            confidence=float(labels.get("confidence") or row.score or 0.0),
            visual_quality=float(labels.get("visual_quality") or 0.0),
            context_clarity=float(labels.get("context_clarity") or 0.0),
            event_type=str(labels.get("event_type") or "scene_segment"),
            mean_motion=float(labels.get("mean_motion") or 0.0),
            mean_sharpness=float(labels.get("mean_sharpness") or 0.0),
            mean_brightness=float(labels.get("mean_brightness") or 0.0),
            width=int(probe.get("width") or 0), height=int(probe.get("height") or 0),
            fps=float(probe.get("fps") or 0.0),
            has_audio=bool(probe.get("has_audio") or False),
            verification_issues=list(verification.get("issues") or []),
            candidate_title=candidate.title, candidate_creator=candidate.creator)

    # ------------------------------------------------------------ selection
    def _apply_floor(self, kept: list[ScoredMoment]
                     ) -> tuple[list[ScoredMoment], list[ScoredMoment]]:
        """Drop moments below the configured minimum total score."""
        floor = self.settings.ranking_min_score
        eligible = [m for m in kept if m.total >= floor]
        below = [m for m in kept if m.total < floor]
        return eligible, below

    @staticmethod
    def _best_per_source(eligible: list[ScoredMoment]) -> list[ScoredMoment]:
        """One moment per source video: the best-scoring one.
        A source with two good moments never contributes twice."""
        best: dict[str, ScoredMoment] = {}
        for moment in eligible:
            current = best.get(moment.candidate_video_id)
            if current is None or rank_key(moment) < rank_key(current):
                best[moment.candidate_video_id] = moment
        return list(best.values())

    def _persist(self, session, job_id: str, selected: list[ScoredMoment]
                 ) -> list[RankedEntry]:
        """Replace the job's ranking with the five selected moments,
        rank #1 first (best score)."""
        session.query(RankingResult).filter(RankingResult.job_id == job_id).delete()
        session.flush()
        entries: list[RankedEntry] = []
        for rank, moment in enumerate(selected, start=1):
            explanation = (
                f"#{rank} of 5 — {moment.explanation} "
                f"Source: {moment.candidate_video_id}.")
            row = RankingResult(
                job_id=job_id, moment_id=moment.moment_id,
                candidate_video_id=moment.candidate_video_id, rank=rank,
                score=moment.score, reason=explanation,
                score_breakdown=moment.components())
            session.add(row)
            session.flush()
            entries.append(RankedEntry(
                rank=rank, moment_id=moment.moment_id,
                candidate_video_id=moment.candidate_video_id,
                score=moment.score, score_100=moment.total,
                components=moment.components(), explanation=explanation))
        # The five chosen candidates are the selected ones.
        for entry in entries:
            candidate = (session.query(CandidateVideo)
                         .filter(CandidateVideo.id == entry.candidate_video_id).first())
            if candidate is not None:
                candidate.status = "selected"
        session.flush()
        return entries

    @staticmethod
    def _insufficient_message(job_id: str, rows, scored, dropped, below_floor,
                              unapproved, by_source) -> str:
        return (
            f"Ranking cannot select five moments from five different sources "
            f"for job {job_id!r}: only {len(by_source)} eligible source(s) "
            f"after scoring {len(scored)} approved moment(s) from {len(rows)} "
            f"analysed moment(s) ({len(dropped)} duplicate(s) dropped, "
            f"{len(below_floor)} below the minimum score, "
            f"{len(unapproved)} skipped as not Rights-approved). "
            "Recovery: request more candidates from Research (a broader "
            "topic, or more approved footage) and re-run ranking — do not "
            "invent missing moments and do not bypass rights approval.")

    # ------------------------------------------------------------ reads
    def list_ranking(self, job_id: str) -> list[RankedEntry]:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            rows = (session.query(RankingResult)
                    .filter(RankingResult.job_id == job_id)
                    .order_by(RankingResult.rank.asc()).all())
            return [self._read(row) for row in rows]

    def list_ranking_reads(self, job_id: str) -> list[RankingResultRead]:
        """Ranking rows as shared-schema reads (real ids + timestamps)."""
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            rows = (session.query(RankingResult)
                    .filter(RankingResult.job_id == job_id)
                    .order_by(RankingResult.rank.asc()).all())
            return [self._read_schema(row) for row in rows]

    def get_rank(self, job_id: str, rank: int) -> RankedEntry:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            row = (session.query(RankingResult)
                   .filter(RankingResult.job_id == job_id,
                           RankingResult.rank == rank).first())
            if row is None:
                raise NotFoundError(f"no rank {rank} for job {job_id}")
            return self._read(row)

    def get_rank_read(self, job_id: str, rank: int) -> RankingResultRead:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            row = (session.query(RankingResult)
                   .filter(RankingResult.job_id == job_id,
                           RankingResult.rank == rank).first())
            if row is None:
                raise NotFoundError(f"no rank {rank} for job {job_id}")
            return self._read_schema(row)

    @staticmethod
    def _read_schema(row: RankingResult) -> RankingResultRead:
        return RankingResultRead(
            id=row.id, job_id=row.job_id, moment_id=row.moment_id,
            candidate_video_id=row.candidate_video_id, rank=row.rank,
            score=row.score, reason=row.reason,
            score_breakdown=dict(row.score_breakdown or {}),
            created_at=row.created_at)

    @staticmethod
    def _read(row: RankingResult) -> RankedEntry:
        breakdown = dict(row.score_breakdown or {})
        total = float(breakdown.get("total") or row.score * 100.0)
        return RankedEntry(
            rank=row.rank, moment_id=row.moment_id,
            candidate_video_id=row.candidate_video_id, score=row.score,
            score_100=round(total, 1), components=breakdown,
            explanation=row.reason or "")

    @staticmethod
    def _limitations() -> list[str]:
        return [
            f"Scorer is {SCORER}: editorial heuristics from measured "
            "frame signals only (motion, sharpness, brightness, visual "
            "quality, context clarity, resolution, audio). Scores are "
            "NOT viral-performance probabilities, and no semantic event "
            "is claimed — Video Intelligence never named what happens "
            "in a scene, so ranking cannot score it either.",
            "Duplicate detection across uploads is a metadata + signal "
            "fingerprint (same title/creator, agreeing signals); a "
            "re-upload with different metadata is not recognised as a "
            "duplicate.",
            "Fewer than five eligible sources fails the job honestly: "
            "the orchestrator has no loop back to Research, so recovery "
            "is to supply more approved candidates and re-run ranking.",
        ]
