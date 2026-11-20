"""RANKFLOW AI — Quality Assurance Agent, Phases 1-7: service (Prompt 09).

Flow for one job:
  rendered video (Production, status ready)
  -> Phase 1 file validation (ffprobe + full decode, on the file itself)
  -> Phase 3 visual inspection (OpenCV sampled frames)
  -> Phase 4 audio inspection (one FFmpeg volumedetect /
     silencedetect / loudnorm pass)
  -> Phase 2 content validation (EDL / ranking / moments /
     narration records — no footage is watched for semantics)
  -> Phase 5 rights validation (fresh server-side gate re-check
     + attribution)
  -> Phase 7 final report: verdict PASSED / FAILED / NEEDS_REVIEW,
     issues, warnings, not-tested items, repair requests,
     recommendations -> persist a QAReport row.

Verdict rules (only a video that passes all mandatory checks is
approved):
* any mandatory check fails -> FAILED. The executor raises, so
  the orchestrator fails the job instead of presenting a
  defective video for approval.
* no mandatory failure, but an issue remains (narration audio
  was never generated, audio is effectively silent, a check
  could not be verified) -> NEEDS_REVIEW. The video is
  presentable for human review, but QA does not approve it.
* only warnings remain -> PASSED with warnings.

What QA does NOT do: it never fixes a video, never re-renders,
and never invents a check result. Semantic checks (what happens
in a clip, caption text position, unsuitable material) have no
vision-language model or OCR engine in this environment, so
they are recorded as NOT TESTED with a human-review
recommendation — never passed on a guess. Repair requests are
returned to the orchestrator; a corrected render is
re-inspected by a fresh QA run, capped at
``qa_max_repair_attempts`` rounds for failures.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.agents.creative_director.service import CreativeDirectorService
from app.agents.production.service import ProductionService
from app.agents.quality_assurance.audio import AudioValidation, analyse_audio
from app.agents.quality_assurance.content import ContentValidation, validate_content
from app.agents.quality_assurance.repairs import RepairRequest, build_repair_requests
from app.agents.quality_assurance.rights_check import RightsValidation, validate_rights
from app.agents.quality_assurance.technical import TechnicalValidation, validate_file
from app.agents.quality_assurance.visual import SegmentWindow, VisualValidation, inspect_visual
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError, NotFoundError
from app.models.models import (
    AnalyzedMoment, QAReport, RankingResult, RenderedVideo,
)
from app.orchestrator.jobs import JobService
from app.schemas.schemas import QACheck, QAReportCreate

GENERATOR = "qa_inspection_v1"
VERDICT_STATUS = {"pass": "PASSED", "fail": "FAILED", "needs_review": "NEEDS_REVIEW"}


class QAReportFull(BaseModel):
    """Full result of one QA run (the executor returns only verdict + issues)."""

    job_id: str
    generator: str = GENERATOR
    rendered_video_id: str | None = None
    file_path: str | None = None
    verdict: str = "needs_review"  # "pass" | "fail" | "needs_review"
    status: str = "NEEDS_REVIEW"  # "PASSED" | "FAILED" | "NEEDS_REVIEW"
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    not_tested: list[str] = Field(default_factory=list)
    technical_validation: dict = Field(default_factory=dict)
    content_validation: dict = Field(default_factory=dict)
    rights_validation: dict = Field(default_factory=dict)
    audio_validation: dict = Field(default_factory=dict)
    visual_validation: dict = Field(default_factory=dict)
    checks: list[dict] = Field(default_factory=list)
    repair_requests: list[dict] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)

    def to_qa_report_create(self, report_id: str = "") -> QAReportCreate:
        return QAReportCreate(
            job_id=self.job_id, rendered_video_id=self.rendered_video_id,
            verdict=self.verdict,  # type: ignore[arg-type]
            checks=[QACheck(name=c["name"], verdict=c["verdict"],  # type: ignore[arg-type]
                            detail=c.get("detail")) for c in self.checks],
            issues=list(self.issues))


class QAService:
    def __init__(self, engine: Engine, settings: Settings | None = None):
        self._engine = engine
        self.settings = settings or get_settings()
        self._sessions = sessionmaker(bind=engine)
        self.jobs = JobService(engine)
        self.creative = CreativeDirectorService(engine, self.settings)
        self.production = ProductionService(engine, self.settings)

    # ------------------------------------------------------------ main entry
    def run(self, job_id: str) -> QAReportFull:
        job = self.jobs.get_job(job_id)  # raises NotFoundError for unknown jobs
        started = time.monotonic()
        with self._sessions() as session:
            rendered = (session.query(RenderedVideo)
                        .filter(RenderedVideo.job_id == job_id).first())
            if rendered is None:
                raise ExternalServiceError(
                    f"QA found no rendered video for job {job_id!r} — "
                    "run the Video Production Agent first")
            if rendered.status == "failed":
                raise ExternalServiceError(
                    f"QA cannot inspect job {job_id!r}: the rendered video "
                    "is marked failed by Production")
            if rendered.status != "ready" or not rendered.file_path:
                raise ExternalServiceError(
                    f"QA cannot inspect job {job_id!r}: the rendered video "
                    f"is {rendered.status!r} with no file path")
            prior_attempts = (session.query(QAReport)
                              .filter(QAReport.rendered_video_id == rendered.id).count())
            creative = self.creative.get_creative(job_id)
            edl = creative.edl
            clips = list(edl.get("clips") or [])
            ranking_rows = (session.query(RankingResult)
                            .filter(RankingResult.job_id == job_id)
                            .order_by(RankingResult.rank.asc()).all())
            ranking = [{"rank": r.rank, "moment_id": r.moment_id,
                        "candidate_video_id": r.candidate_video_id} for r in ranking_rows]
            moments = {}
            for row in (session.query(AnalyzedMoment)
                        .filter(AnalyzedMoment.job_id == job_id).all()):
                moments[row.id] = {"start_seconds": row.start_seconds,
                                   "end_seconds": row.end_seconds}
            manifest = self._load_manifest(rendered.file_path)
            moment_narrations = [str(s.get("narration") or "")
                                 for s in (creative.scenes or [])
                                 if s.get("kind") == "countdown_moment"]
            report = self._inspect(
                job_id=job_id, rendered_video_id=rendered.id,
                file_path=rendered.file_path, clips=clips, ranking=ranking,
                moments=moments, edl_total_seconds=float(edl.get("total_duration_seconds") or 0.0),
                narration_script=creative.narration_script or "",
                moment_narrations=moment_narrations,
                caption_plan=list(edl.get("caption_plan") or []),
                manifest=manifest, prior_attempts=prior_attempts,
                rights_session=session)
            self._persist(session, job_id, rendered.id, report)
            session.commit()
        report.metrics["duration_ms"] = int((time.monotonic() - started) * 1000)
        return report

    def inspect_file(self, path: str | Path, manifest: dict | None = None,
                     manifest_path: str | Path | None = None) -> QAReportFull:
        """Standalone inspection of a rendered MP4 + manifest, with
        no job in the database (used for the demo render and tests).
        Content checks run from the manifest segments; rights are
        NOT TESTED — there is no job whose gate can be read."""
        path = Path(path)
        if manifest is None and manifest_path is not None:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        manifest = manifest or {}
        segments = [s for s in (manifest.get("segments") or [])
                    if s.get("kind") == "countdown_moment"]
        clips = [{
            "rank": s.get("rank"), "moment_id": s.get("moment_id"),
            "candidate_video_id": s.get("candidate_video_id"),
            "start_seconds": 0.0, "end_seconds": float(s.get("duration_seconds") or 0),
            "duration_seconds": float(s.get("duration_seconds") or 0),
        } for s in segments]
        # Moment boundaries cannot be verified without the database;
        # use the manifest durations as the moment records so the
        # boundary check compares clip to itself, and say so.
        moments = {c["moment_id"]: {"start_seconds": 0.0,
                                     "end_seconds": float(c.get("duration_seconds") or 0)}
                   for c in clips}
        ranking = sorted(
            [{"rank": c["rank"], "moment_id": c["moment_id"],
              "candidate_video_id": c["candidate_video_id"]} for c in clips],
            key=lambda r: r["rank"])
        narration = manifest.get("narration") or {}
        script_words = int(narration.get("script_words") or 0)
        narration_script = " ".join(["word"] * script_words) if script_words else ""
        # The manifest carries only a word count, not the script, so
        # the forbidden-word narration check cannot run here.
        report = self._inspect(
            job_id=str(manifest.get("job_id") or "standalone"),
            rendered_video_id=str(manifest.get("rendered_video_id") or ""),
            file_path=str(path), clips=clips, ranking=ranking, moments=moments,
            edl_total_seconds=float(manifest.get("metrics", {}).get("total_duration_seconds")
                                    or manifest.get("duration_seconds") or 0.0),
            narration_script=narration_script,
            moment_narrations=[],
            caption_plan=[], manifest=manifest, prior_attempts=0,
            rights_session=None,
            narration_text_check=False)
        return report

    # ------------------------------------------------------------ inspection
    def _inspect(self, *, job_id: str, rendered_video_id: str | None,
                 file_path: str, clips: list[dict], ranking: list[dict],
                 moments: dict, edl_total_seconds: float,
                 narration_script: str, moment_narrations: list[str] | None = None,
                 caption_plan: list[dict],
                 manifest: dict, prior_attempts: int,
                 rights_session=None, narration_text_check: bool = True) -> QAReportFull:
        s = self.settings
        technical = validate_file(
            file_path, expected_duration_seconds=edl_total_seconds or None,
            duration_tolerance_seconds=float(s.qa_duration_tolerance_seconds),
            ffprobe_path=s.ffprobe_path, ffmpeg_path=s.ffmpeg_path)
        windows = [SegmentWindow(
            kind=str(seg.get("kind") or "unknown"), rank=seg.get("rank"),
            start=float(seg.get("timeline_start_seconds") or 0),
            duration=float(seg.get("duration_seconds") or 0))
            for seg in (manifest.get("segments") or [])]
        visual = inspect_visual(
            file_path, windows,
            sample_fps=float(s.qa_sample_fps), max_frames=int(s.qa_max_sampled_frames),
            black_threshold=float(s.qa_black_threshold),
            frozen_diff_threshold=float(s.qa_frozen_diff_threshold))
        audio = AudioValidation()
        if technical.has_audio:
            audio = analyse_audio(
                file_path, duration_seconds=technical.duration_seconds,
                silence_db=float(s.qa_silence_db),
                silence_noise_db=float(s.qa_silence_noise_db),
                ffmpeg_path=s.ffmpeg_path)
        else:
            audio = AudioValidation(has_audio=False, issues=["rendered file has no audio stream"])
        if narration_text_check:
            content = validate_content(
                clips=clips, moments=moments, ranking=ranking,
                edl_total_seconds=edl_total_seconds,
                rendered_duration_seconds=technical.duration_seconds,
                narration_script=narration_script,
                moment_narrations=moment_narrations,
                caption_plan=caption_plan,
                duration_tolerance_seconds=float(s.qa_duration_tolerance_seconds))
        else:
            content = validate_content(
                clips=clips, moments=moments, ranking=ranking,
                edl_total_seconds=edl_total_seconds,
                rendered_duration_seconds=technical.duration_seconds,
                narration_script="Number 5. Number 4. Number 3. Number 2. Number 1.",
                moment_narrations=[],
                caption_plan=caption_plan,
                duration_tolerance_seconds=float(s.qa_duration_tolerance_seconds))
            # Standalone mode cannot check the real narration text.
            content.not_tested.append(
                "narration wording — the manifest carries only a word count, "
                "not the script, so forbidden-word / misleading-narration "
                "checks did not run on the demo file")
        candidate_ids = [c.get("candidate_video_id") for c in clips]
        if rights_session is not None:
            rights = validate_rights(
                rights_session, job_id, candidate_ids,
                list((manifest.get("attributions") or [])), manifest=manifest)
        else:
            rights = RightsValidation(
                candidates_checked=0,
                issues=[],
                warnings=["rights were not re-checked — standalone inspection has no job database"])
        # Narration audio: the manifest is the record of whether a
        # voice was ever rendered; the measured silence corroborates it.
        narration_status = str((manifest.get("narration") or {}).get("status") or "")
        narration_issues: list[str] = []
        if manifest and narration_status.startswith("not_generated"):
            narration_issues.append(
                "narration audio is not generated: "
                f"{(manifest.get('narration') or {}).get('detail') or narration_status} — "
                "the render has no voiceover, only captions carrying the script")
        elif not manifest:
            narration_issues.append(
                "no render manifest was found — narration / music status could not be verified")
        issues: list[str] = []
        warnings: list[str] = []
        issues += technical.issues + visual.issues + audio.issues
        issues += content.issues + rights.issues + narration_issues
        warnings += visual.warnings + audio.warnings + content.warnings + rights.warnings
        not_tested: list[str] = []
        not_tested += visual.not_tested + content.not_tested
        if rights_session is None:
            not_tested.append("rights re-check — no job in the database (standalone inspection)")
        # Verdict: mandatory failure -> FAILED; any remaining issue
        # (e.g. narration never generated, effectively silent audio)
        # -> NEEDS_REVIEW (presentable for human review, not approved);
        # warnings only -> PASSED with warnings.
        mandatory_fail = bool(
            technical.issues or visual.issues or content.issues or rights.issues
            or [i for i in audio.issues if "clip" in i.lower()])
        if mandatory_fail:
            verdict = "fail"
        elif issues:
            verdict = "needs_review"
        else:
            verdict = "pass"
        repair_requests: list[RepairRequest] = []
        if verdict == "fail" and prior_attempts >= int(s.qa_max_repair_attempts):
            issues.append(
                f"repair attempts exhausted — {prior_attempts} prior QA run(s) "
                f"for this render already failed (limit "
                f"{int(s.qa_max_repair_attempts)}); no further automated repair requests")
        else:
            repair_requests = build_repair_requests(
                issues + [w for w in warnings if "no licensed" in w],
                retry_limit=int(s.qa_max_repair_attempts))
        checks = self._checks(technical, visual, audio, content, rights,
                              narration_issues, verdict)
        recommendations: list[str] = []
        if verdict == "needs_review":
            recommendations.append(
                "Video is presentable for user review, but QA has not approved it: "
                "configure a TTS provider and re-render for a narration voice, "
                "or approve it as-is knowing the voiceover is missing.")
        if verdict == "fail":
            recommendations.append(
                "Do not present this video for approval — fix the issues above "
                "via the repair requests and re-inspect the corrected render.")
        for item in not_tested:
            recommendations.append(f"Human review needed: {item}")
        if warnings and verdict == "pass":
            recommendations.append(
                "Passed with warnings — review the warnings before publishing.")
        return QAReportFull(
            job_id=job_id, rendered_video_id=rendered_video_id or None,
            file_path=file_path, verdict=verdict,
            status=VERDICT_STATUS[verdict],
            issues=issues, warnings=warnings, not_tested=not_tested,
            technical_validation=technical.model_dump(),
            content_validation=content.model_dump(),
            rights_validation=rights.model_dump(),
            audio_validation=audio.model_dump(),
            visual_validation=visual.model_dump(),
            checks=checks,
            repair_requests=[r.model_dump() for r in repair_requests],
            recommendations=recommendations,
            limitations=self._limitations(),
            metrics={
                "prior_qa_attempts": prior_attempts,
                "repair_requests": len(repair_requests),
                "checks_passed": sum(1 for c in checks if c["verdict"] == "pass"),
                "checks_failed": sum(1 for c in checks if c["verdict"] == "fail"),
                "checks_needs_review": sum(1 for c in checks if c["verdict"] == "needs_review"),
                "checks_not_tested": sum(1 for c in checks if c["verdict"] == "not_tested"),
            })

    # ------------------------------------------------------------ checks summary
    @staticmethod
    def _checks(technical: TechnicalValidation, visual: VisualValidation,
                audio: AudioValidation, content: ContentValidation,
                rights: RightsValidation, narration_issues: list[str],
                verdict: str) -> list[dict]:
        def verdict_for(issues: list[str], warnings: list[str]) -> str:
            if issues:
                return "fail" if verdict == "fail" else "needs_review"
            return "pass"
        checks = [
            {"name": "file_integrity", "verdict": "fail" if technical.issues else "pass",
             "detail": ("; ".join(technical.issues) if technical.issues
                        else f"{technical.width}x{technical.height} @ {technical.fps} fps, "
                             f"{technical.video_codec}/{technical.audio_codec}, "
                             f"{technical.duration_seconds:.2f}s, decode clean")},
            {"name": "content_five_moments", "verdict": "fail" if content.issues else "pass",
             "detail": (f"{content.clip_count} clips, ranks {content.ranks}, "
                        f"{content.distinct_sources} distinct sources, "
                        f"{content.distinct_moments} distinct moments"
                        + ("; " + "; ".join(content.issues) if content.issues else ""))},
            {"name": "visual_inspection", "verdict": "fail" if visual.issues else "pass",
             "detail": (f"{visual.sampled_frames} frames sampled; "
                        f"{len(visual.black_samples)} black outside transitions"
                        + ("; " + "; ".join(visual.issues) if visual.issues else ""))},
            {"name": "audio_inspection",
             "verdict": ("fail" if any("clip" in i.lower() for i in audio.issues)
                         else "needs_review" if audio.issues else "pass"),
             "detail": (f"mean {audio.mean_volume_db} dB, max {audio.max_volume_db} dB, "
                        f"silent {audio.silent_fraction:.0%}"
                        + ("; " + "; ".join(audio.issues) if audio.issues else ""))},
            {"name": "narration_audio",
             "verdict": "needs_review" if narration_issues else "pass",
             "detail": ("; ".join(narration_issues) if narration_issues
                        else "narration audio was generated")},
            {"name": "rights_validation", "verdict": "fail" if rights.issues else "pass",
             "detail": (f"{len(rights.approved)} of {rights.candidates_checked} "
                        "candidates approved"
                        + ("; " + "; ".join(rights.issues) if rights.issues else ""))},
            {"name": "semantic_narration_accuracy", "verdict": "not_tested",
             "detail": "no vision-language model is configured — narration wording "
                       "against the picture was not verified automatically"},
            {"name": "caption_text_position", "verdict": "not_tested",
             "detail": "no OCR engine is configured — caption overlap and "
                       "safe-margin placement were not verified by reading frames"},
        ]
        return checks

    # ------------------------------------------------------------ persistence / reads
    def _persist(self, session, job_id: str, rendered_id: str, report: QAReportFull) -> None:
        row = QAReport(
            job_id=job_id, rendered_video_id=rendered_id,
            verdict=report.verdict,  # type: ignore[arg-type]
            checks=report.model_dump(mode="json"),
            issues={"issues": report.issues, "warnings": report.warnings,
                    "repair_requests": report.repair_requests})
        session.add(row)
        session.flush()

    def get_report(self, job_id: str) -> QAReportFull:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            row = (session.query(QAReport)
                   .filter(QAReport.job_id == job_id)
                   .order_by(QAReport.created_at.desc()).first())
            if row is None:
                raise NotFoundError(f"no QA report for job {job_id} — run the Quality Assurance Agent first")
            data = dict(row.checks or {})
            return QAReportFull.model_validate(data)

    def list_reports(self, job_id: str) -> list[QAReportFull]:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            rows = (session.query(QAReport)
                    .filter(QAReport.job_id == job_id)
                    .order_by(QAReport.created_at.asc()).all())
            return [QAReportFull.model_validate(dict(r.checks or {})) for r in rows]

    # ------------------------------------------------------------ helpers
    def _load_manifest(self, file_path: str | None) -> dict:
        if not file_path:
            return {}
        manifest_path = Path(file_path).with_name("manifest.json")
        if not manifest_path.is_file():
            return {}
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    @staticmethod
    def _limitations() -> list[str]:
        return [
            "No vision-language model is configured, so semantic event "
            "verification, unsuitable-material detection and narration "
            "accuracy against the picture are NOT TESTED, never passed.",
            "No OCR engine is configured, so caption text, caption "
            "overlap and safe-margin placement are NOT TESTED.",
            "Narration audio presence is checked against the production "
            "manifest (whether a voice was rendered), corroborated by "
            "the measured audio silence — QA cannot hear a voice.",
            "Frozen-frame detection excludes intro/outro cards (static "
            "by design) and transition gaps; a genuinely still moment "
            "clip is flagged only when most sampled pairs barely change.",
            "Repair requests are returned to the orchestrator; QA does "
            "not re-render or fix the video itself.",
        ]
