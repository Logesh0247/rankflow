"""RANKFLOW AI — Video Intelligence Agent, Phases 1-6: service (Prompt 05).

Flow for one job:
  approved candidates (from the Rights gate — server-side, fail
  closed) -> for each one, resolve its local footage file ->
  ffprobe -> sampled frames -> heuristic analysis -> timestamp
  verification -> persist AnalyzedMoment rows -> update the
  ApprovedFootage row and the candidate status -> return a
  VideoAnalysisReport; the executor hands only the moment ids to
  the orchestrator as a validated VideoAnalysisOutput.

Failure handling (never silent, never fabricated):
* no approved candidates -> ExternalServiceError;
* an approved candidate with no local footage file -> skipped
  with the reason recorded (a download step is not configured in
  this prompt; the file must be supplied via ApprovedFootage
  storage_path or the `footage_paths` argument);
* a corrupt / unreadable / no-video-stream file -> skipped with
  the probe error recorded;
* zero usable moments across all candidates -> ExternalServiceError
  (the orchestrator retries, then fails the job honestly).
  An empty VideoAnalysisOutput is never returned (its schema
  requires >= 1 moment id).

Re-running the agent for a job replaces that candidate's moments
(delete + re-insert), so a re-run never duplicates moments.
"""
from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.agents.rights.gate import approved_candidate_ids, require_approved
from app.agents.video_intelligence.analysis import ANALYSER, detect_moments
from app.agents.video_intelligence.frames import extract_samples, save_frame_jpeg
from app.agents.video_intelligence.probe import VideoProbe, probe_video
from app.agents.video_intelligence.verify import verify_moments
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError, NotFoundError, StorageError, ValidationError
from app.database.session import init_db
from app.models.models import AnalyzedMoment, ApprovedFootage, CandidateVideo
from app.orchestrator.jobs import JobService
from app.schemas.schemas import VideoMomentRead
from app.storage.local import get_storage


class CandidateAnalysis(BaseModel):
    """What happened to one approved candidate."""

    candidate_video_id: str
    status: str  # "analyzed", "skipped"
    probe: VideoProbe | None = None
    moment_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class VideoAnalysisReport(BaseModel):
    """Full result of one analysis run (the executor returns only its ids)."""

    job_id: str
    analyser: str = ANALYSER
    candidates: list[CandidateAnalysis] = Field(default_factory=list)
    moment_ids: list[str] = Field(default_factory=list)
    candidate_video_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)


class VideoIntelligenceService:
    def __init__(self, engine: Engine, settings: Settings | None = None):
        self._engine = engine
        self.settings = settings or get_settings()
        self._sessions = sessionmaker(bind=engine)
        self.jobs = JobService(engine)
        self.storage = get_storage(self.settings)

    # ------------------------------------------------------------ main entry
    def run(self, job_id: str,
            footage_paths: dict[str, str | Path] | None = None) -> VideoAnalysisReport:
        job = self.jobs.get_job(job_id)  # raises NotFoundError for unknown jobs
        footage_paths = {k: Path(v) for k, v in (footage_paths or {}).items()}
        started = time.monotonic()
        with self._sessions() as session:
            approved = approved_candidate_ids(session, job_id)
            if not approved:
                raise ExternalServiceError(
                    f"Video Intelligence found no approved footage for job {job_id!r}: "
                    "the Rights gate approved no candidate, and unapproved footage "
                    "is never analysed")
            results: list[CandidateAnalysis] = []
            all_moment_ids: list[str] = []
            analysed_ids: list[str] = []
            for candidate_id in approved:
                # RIGHTS GATE FIRST — before the file is even looked at.
                require_approved(session, job_id, [candidate_id],
                                 purpose="video_intelligence")
                result = self._analyse_candidate(session, job_id, candidate_id,
                                                 footage_paths.get(candidate_id))
                results.append(result)
                all_moment_ids.extend(result.moment_ids)
                if result.status == "analyzed":
                    analysed_ids.append(candidate_id)
            session.commit()
        if not all_moment_ids:
            raise ExternalServiceError(
                f"Video Intelligence produced no usable moments for job {job_id!r}: "
                + "; ".join(
                    f"{r.candidate_video_id}: {r.reason or 'no moments'}"
                    for r in results))
        return VideoAnalysisReport(
            job_id=job.id,
            candidates=results,
            moment_ids=all_moment_ids,
            candidate_video_ids=analysed_ids,
            limitations=self._limitations(),
            metrics={
                "approved_candidates": len(approved),
                "analyzed_candidates": len(analysed_ids),
                "skipped_candidates": len(approved) - len(analysed_ids),
                "moments": len(all_moment_ids),
                "duration_ms": int((time.monotonic() - started) * 1000),
            })

    # ------------------------------------------------------------ one candidate
    def _analyse_candidate(self, session, job_id: str, candidate_id: str,
                           supplied_path: Path | None) -> CandidateAnalysis:
        candidate = (session.query(CandidateVideo)
                     .filter(CandidateVideo.id == candidate_id,
                             CandidateVideo.job_id == job_id).first())
        if candidate is None:
            raise NotFoundError(f"candidate {candidate_id} not found for job {job_id}")
        path = supplied_path
        if path is None:
            footage = (session.query(ApprovedFootage)
                       .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
            if footage is not None and footage.storage_path:
                path = Path(footage.storage_path)
        if path is None:
            return CandidateAnalysis(
                candidate_video_id=candidate_id, status="skipped",
                reason="no local footage file: ApprovedFootage.storage_path is empty "
                       "and no footage path was supplied (downloading source videos "
                       "is not configured in Prompt 05)")
        try:
            probe = probe_video(path, ffprobe_path=self.settings.ffprobe_path)
        except (StorageError, ValidationError) as exc:
            return CandidateAnalysis(candidate_video_id=candidate_id, status="skipped",
                                     reason=f"ingestion rejected the file: {exc}")
        s = self.settings
        extraction = extract_samples(
            path, sample_fps=s.vi_sample_fps, max_frames=s.vi_max_sampled_frames,
            scene_threshold=s.vi_scene_threshold)
        detected = detect_moments(
            probe, extraction, min_moment_seconds=s.vi_min_moment_seconds,
            max_moment_seconds=s.vi_max_moment_seconds,
            max_moments_per_video=s.vi_max_moments_per_video,
            motion_threshold=s.vi_motion_threshold)
        verified = verify_moments(
            str(path), probe, detected,
            lead_in_seconds=s.vi_lead_in_seconds,
            aftermath_seconds=s.vi_aftermath_seconds)
        kept = [v for v in verified if v.verified]
        if not kept:
            return CandidateAnalysis(
                candidate_video_id=candidate_id, status="skipped", probe=probe,
                reason="no usable moments survived timestamp verification "
                       f"({len(verified)} dropped: "
                       + "; ".join(sorted({i for v in verified for i in v.issues})[:3])
                       + ")")
        # Replace this candidate's moments on re-run (no duplicates).
        session.query(AnalyzedMoment).filter(
            AnalyzedMoment.job_id == job_id,
            AnalyzedMoment.candidate_video_id == candidate_id).delete()
        session.flush()
        moment_ids: list[str] = []
        for item in kept:
            moment = item.moment
            frame_refs = [f"t{t.timestamp:.1f}" for t in extraction.samples
                          if moment.start_seconds - 0.6 <= t.timestamp <= moment.end_seconds + 0.6]
            row = AnalyzedMoment(
                job_id=job_id, candidate_video_id=candidate_id,
                start_seconds=moment.start_seconds, end_seconds=moment.end_seconds,
                description=moment.description, score=moment.confidence,
                labels={
                    "analyser": ANALYSER,
                    "event_type": moment.event_type,
                    "confidence": moment.confidence,
                    "visual_quality": moment.visual_quality,
                    "context_clarity": moment.context_clarity,
                    "mean_motion": moment.mean_motion,
                    "mean_sharpness": moment.mean_sharpness,
                    "mean_brightness": moment.mean_brightness,
                    "frame_refs": frame_refs,
                    "verification": {"verified": True, "issues": item.issues},
                    "probe": {"duration_seconds": probe.duration_seconds,
                              "width": probe.width, "height": probe.height,
                              "fps": probe.fps, "video_codec": probe.video_codec,
                              "has_audio": probe.has_audio,
                              "audio_codec": probe.audio_codec},
                })
            session.add(row)
            session.flush()
            moment_ids.append(row.id)
            self._save_thumbnail(path, row.id, job_id, candidate_id, moment, row)
        footage = (session.query(ApprovedFootage)
                   .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
        if footage is not None:
            footage.storage_path = str(path)
            footage.duration_seconds = probe.duration_seconds
        candidate.status = "analyzed"
        session.flush()
        return CandidateAnalysis(candidate_video_id=candidate_id, status="analyzed",
                                 probe=probe, moment_ids=moment_ids)

    def _save_thumbnail(self, path: Path, moment_id: str, job_id: str,
                        candidate_id: str, moment, row: AnalyzedMoment) -> None:
        """Save the middle frame of a moment as its thumbnail and record the path."""
        import os
        import tempfile

        rel = f"jobs/{job_id}/moments/{moment_id}/thumb.jpg"
        fd, tmp = tempfile.mkstemp(prefix=f"rankflow-thumb-{moment_id}-", suffix=".jpg")
        os.close(fd)
        try:
            save_frame_jpeg(path, (moment.start_seconds + moment.end_seconds) / 2,
                            Path(tmp))
            self.storage.put_file(rel, Path(tmp))
            labels = dict(row.labels or {})
            labels["thumbnail"] = rel
            row.labels = labels
        except (StorageError, ValidationError):
            # A missing thumbnail never blocks the moment itself.
            pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ------------------------------------------------------------ reads
    def list_moments(self, job_id: str) -> list[VideoMomentRead]:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            rows = (session.query(AnalyzedMoment)
                    .filter(AnalyzedMoment.job_id == job_id)
                    .order_by(AnalyzedMoment.candidate_video_id.asc(),
                              AnalyzedMoment.start_seconds.asc()).all())
            return [self._read(row) for row in rows]

    def get_moment(self, job_id: str, moment_id: str) -> VideoMomentRead:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            row = (session.query(AnalyzedMoment)
                   .filter(AnalyzedMoment.id == moment_id,
                           AnalyzedMoment.job_id == job_id).first())
            if row is None:
                raise NotFoundError(f"moment {moment_id} not found for job {job_id}")
            return self._read(row)

    @staticmethod
    def _read(row: AnalyzedMoment) -> VideoMomentRead:
        return VideoMomentRead(
            id=row.id, job_id=row.job_id, candidate_video_id=row.candidate_video_id,
            start_seconds=row.start_seconds, end_seconds=row.end_seconds,
            description=row.description, score=row.score,
            labels=dict(row.labels or {}), created_at=row.created_at)

    @staticmethod
    def _limitations() -> list[str]:
        return [
            f"Analyser is {ANALYSER}: deterministic frame signals only "
            "(scene changes, motion, brightness, sharpness). No "
            "vision-language model is configured, so no semantic event "
            "is inferred — descriptions are signal-based by design.",
            "Moments come from scene segments; a long unchanging shot "
            "yields one moment at most, and a cut-heavy video yields "
            "many short ones.",
            "Only approved footage with a local file is analysed. "
            "Downloading source videos is not configured in Prompt 05.",
            "Timestamps are measured from sampled frames (1 fps by "
            "default); boundaries are accurate to about ±1 frame at "
            "the sampling rate, then verified by re-reading the file.",
        ]
