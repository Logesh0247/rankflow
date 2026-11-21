"""RANKFLOW AI — Video Production Agent, Phases 1-9: service (Prompt 08).

Flow for one job:
  approved Edit Decision List (Creative Director)
  -> re-check the Rights gate per candidate, server-side
  -> resolve each clip's local footage file (ApprovedFootage
     storage_path, or an explicit footage map — a source URL is
     NOT downloaded: no download step is configured)
  -> probe every source, measure the motion centroid for
     subject-aware reframing, extract each clip at its verified
     timestamps, reframe to 1080x1920 full-bleed, burn in the
     title bar, numbered list, captions and attribution, then
     join the five clips with straight cuts (no intro/outro
     cards, no black transition gaps)
  -> validate the render with ffprobe + a full decode check
  -> persist the RenderedVideo row and a render manifest.

Full-bleed style (2026-11-21, replacing the Rankter style):
the clip fills the whole 1080x1920 frame; the title bar, the
1-5 numbers down the left edge and the captions are drawn on
top of the footage. The voiceover is OFF for this style — the
reference videos carry the text on screen, not as narration —
so no TTS synthesis runs (the Edge TTS code is intact, just
not called in this style path); the narration script exists
and the captions carry it, but no voice is claimed.

What is NOT in the render, recorded honestly in the manifest:
* narration audio — voiceover is off by design in the
  full-bleed style (see above); the manifest records status
  ``not_generated_voiceover_off_fullbleed_style``;
* licensed music — only mixed in when PROD_MUSIC_DIR holds a
  licensed file that probes as audio; otherwise none, and the
  manifest says so;
* sound effects — detected when configured, but SFX mixing is
  not implemented in this prompt, so none are mixed in; the
  manifest says so.

Attribution: there is no end-credits screen in the video
itself. A clip whose licence requires attribution gets a small
attribution line in the lower third for the last 2 seconds of
its segment, and the full attribution for every clip is
recorded in the manifest and in the report's
``description_attribution`` (paste it into the video
description when publishing).

Failure handling (never silent, never fabricated):
* no EDL / fewer than five clips / wrong order / timestamps that
  do not match the verified AnalyzedMoment rows -> ExternalServiceError;
* a clip's candidate no longer Rights-approved -> ValidationError
  from the gate (fail closed);
* a clip with no local footage file, or a source URL only ->
  ExternalServiceError naming the candidate (Production does not
  download videos);
* a corrupt source, a clip that runs past its source duration,
  or a rendered segment whose measured duration is wrong ->
  ExternalServiceError;
* a final render that fails validation -> ExternalServiceError
  and the RenderedVideo row is left "failed" — never "ready".

Re-running replaces the job's rendered video (one row, one
output directory per render id), so a re-run never duplicates it.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.agents.creative_director.service import CreativeDirectorService
from app.agents.production.audio import (
    AudioAsset, ducking_status, find_music, find_sfx,
)
from app.agents.production.captions import ass_filter, build_ass
from app.agents.production.extract import ExtractedClip, extract_clip
from app.agents.production.graphics import (
    TARGET_WIDTH, clean_clip_caption, clip_graphics_filters,
)
from app.agents.production.narration import (
    NarrationAudio, NarrationProvider, detect_narration_provider,
    narration_estimated_seconds,
)
from app.agents.production.reframe import motion_centroid_x
from app.agents.production.render import (
    RenderValidation, concat_segments, validate_render,
)
from app.agents.rights.gate import require_approved
from app.agents.video_intelligence.probe import probe_video
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError, NotFoundError, ValidationError
from app.models.models import (
    AnalyzedMoment, ApprovedFootage, EditInstruction, RenderedVideo,
)
from app.orchestrator.jobs import JobService
from app.schemas.schemas import RenderedVideoRead
from app.storage.local import get_storage

GENERATOR = "ffmpeg_production_v1"
REQUIRED_CLIPS = 5


class SegmentReport(BaseModel):
    kind: str  # "intro" | "countdown_moment" | "outro"
    rank: int | None = None
    moment_id: str | None = None
    candidate_video_id: str | None = None
    duration_seconds: float
    timeline_start_seconds: float
    file_name: str
    subject_aware: bool = False
    transition_in: str | None = None
    transition_rendered: str = ""


class ProductionReport(BaseModel):
    """Full result of one production run (the executor returns only its ids)."""

    job_id: str
    generator: str = GENERATOR
    rendered_video_id: str
    file_path: str
    manifest_path: str
    duration_seconds: float
    width: int = 1080
    height: int = 1920
    fps: float = 30.0
    video_codec: str = "h264"
    audio_codec: str = "aac"
    segments: list[SegmentReport] = Field(default_factory=list)
    narration: dict = Field(default_factory=dict)
    music: AudioAsset = Field(default_factory=lambda: AudioAsset(kind="music", status="not_included_no_licensed_library"))
    sfx_chime: AudioAsset = Field(default_factory=lambda: AudioAsset(kind="sfx", status="not_included_no_licensed_library"))
    sfx_whoosh: AudioAsset = Field(default_factory=lambda: AudioAsset(kind="sfx", status="not_included_no_licensed_library"))
    sfx_mixed: bool = False
    ducking: str = "not_applicable"
    captions: dict = Field(default_factory=dict)
    attributions: list[dict] = Field(default_factory=list)
    # Full attribution text for the video description (the video
    # itself has no end-credits screen): one line per clip whose
    # licence requires attribution, joined with "; ".
    description_attribution: str = ""
    validation: dict = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)


class ProductionService:
    def __init__(self, engine: Engine, settings: Settings | None = None):
        self._engine = engine
        self.settings = settings or get_settings()
        self._sessions = sessionmaker(bind=engine)
        self.jobs = JobService(engine)
        self.creative = CreativeDirectorService(engine, self.settings)
        self.storage = get_storage(self.settings)

    # ------------------------------------------------------------ main entry
    def run(self, job_id: str,
            footage_paths: dict[str, str | Path] | None = None) -> ProductionReport:
        job = self.jobs.get_job(job_id)  # raises NotFoundError for unknown jobs
        footage_paths = {k: Path(v) for k, v in (footage_paths or {}).items()}
        started = time.monotonic()
        render_id = uuid.uuid4().hex[:12]
        work_dir = Path(self.settings.storage_root) / "jobs" / job_id / "production" / render_id
        work_dir.mkdir(parents=True, exist_ok=True)
        creative = self.creative.get_creative(job_id)  # raises NotFoundError without an EDL
        edl = creative.edl
        clips = list(edl.get("clips") or [])
        self._validate_edl(job_id, clips)
        total_duration = float(edl.get("total_duration_seconds") or 0.0)
        intro_s = float(edl.get("intro_seconds") or 0.0)
        outro_s = float(edl.get("outro_seconds") or 0.0)
        transition_s = float(edl.get("transition_seconds") or 0.0)
        caption_plan = list(edl.get("caption_plan") or [])
        visual = dict(edl.get("visual_direction") or {})
        per_moment = list(visual.get("per_moment") or [])
        narration_script = creative.narration_script or ""
        narration_provider = detect_narration_provider(self.settings)
        narration_seconds = narration_estimated_seconds(
            narration_script, words_per_minute=float(self.settings.cd_narration_wpm))
        # Full-bleed style: the voiceover is OFF by design — the
        # reference videos carry the text on screen, not as
        # narration. No TTS synthesis runs at all (the Edge TTS
        # code is intact, just not called in this style path), so
        # there is no fit check against the timeline and no
        # synthesis failure to fall back from. The narration
        # script exists and the captions carry it; QA will flag
        # the missing voice as needs_review, which is the honest
        # verdict for a voiceless render.
        narration_audio: NarrationAudio | None = None
        narration_failure: str | None = None
        synthesis_warnings: list[str] = [
            "voiceover is off in the full-bleed style — rendering "
            "with the clips' own audio only; the narration script "
            "exists and the captions carry it"]

        with self._sessions() as session:
            # RIGHTS GATE FIRST — before any footage file is touched.
            candidate_ids = [c["candidate_video_id"] for c in clips]
            require_approved(session, job_id, candidate_ids, purpose="production")
            moments = self._load_moments(session, job_id, clips)
            sources = self._resolve_sources(session, job_id, clips, footage_paths)
            # Replace any previous render for this job (no duplicates).
            session.query(RenderedVideo).filter(RenderedVideo.job_id == job_id).delete()
            session.flush()
            edit_instruction = (session.query(EditInstruction)
                                .filter(EditInstruction.job_id == job_id).first())
            rendered_row = RenderedVideo(
                job_id=job_id,
                edit_instruction_id=edit_instruction.id if edit_instruction else None,
                status="rendering")
            session.add(rendered_row)
            session.flush()
            rendered_id = rendered_row.id
            session.commit()

        try:
            segments, warnings = self._render_timeline(
                job, clips, moments, sources, caption_plan, per_moment,
                intro_s, outro_s, transition_s, total_duration, work_dir)
            warnings = synthesis_warnings + warnings
            music = find_music(self.settings, ffprobe_path=self.settings.ffprobe_path)
            chime = find_sfx(self.settings, name_contains="chime",
                             ffprobe_path=self.settings.ffprobe_path)
            whoosh = find_sfx(self.settings, name_contains="whoosh",
                              ffprobe_path=self.settings.ffprobe_path)
            output_path = work_dir / "rankflow-short.mp4"
            concat_segments(
                [s["path"] for s in segments], output_path,
                ffmpeg_path=self.settings.ffmpeg_path,
                ffprobe_path=self.settings.ffprobe_path,
                total_duration_seconds=total_duration,
                narration_path=None,
                music_path=music.file_path if music.status == "included" else None,
                timeout_seconds=float(self.settings.prod_render_timeout_seconds))
            validation = validate_render(
                output_path, expected_duration_seconds=total_duration,
                ffprobe_path=self.settings.ffprobe_path,
                ffmpeg_path=self.settings.ffmpeg_path)
            if not validation.valid:
                raise ExternalServiceError(
                    f"render validation failed for job {job_id!r}: "
                    + "; ".join(validation.issues))
        except Exception:
            with self._sessions() as session:
                row = session.query(RenderedVideo).filter(RenderedVideo.id == rendered_id).first()
                if row is not None:
                    row.status = "failed"
                session.commit()
            raise

        narration_report = {
            "script_words": len(narration_script.split()),
            "estimated_seconds": narration_seconds,
            "provider": narration_provider.provider,
            "voice": narration_provider.voice,
            "status": "not_generated_voiceover_off_fullbleed_style",
            "detail": ("voiceover is off by design in the full-bleed style — "
                       "the reference videos carry the text on screen, not as "
                       "narration; the Edge TTS code is intact but was not "
                       "called, so no voice was synthesised"),
            "audio_path": None,
        }
        report = ProductionReport(
            job_id=job.id, rendered_video_id=rendered_id,
            file_path=str(output_path),
            manifest_path=str(work_dir / "manifest.json"),
            duration_seconds=validation.duration_seconds,
            width=validation.width, height=validation.height, fps=validation.fps,
            video_codec=validation.video_codec,
            audio_codec=validation.audio_codec or "aac",
            segments=[SegmentReport(**s["report"]) for s in segments],
            narration=narration_report,
            music=music, sfx_chime=chime, sfx_whoosh=whoosh, sfx_mixed=False,
            ducking=ducking_status(narration_generated=narration_audio is not None,
                                   music_included=music.status == "included"),
            captions={
                "lines": len(caption_plan),
                "ass_files": [s["ass_file"] for s in segments if s.get("ass_file")],
                "max_chars_per_line": 40,
                "placement": "raised above the big bottom caption "
                             "(MarginV 380), centred, inside the 10% safe margins",
            },
            attributions=[{
                "candidate_video_id": c["candidate_video_id"],
                "attribution_text": (per_moment[i].get("attribution_display")
                                     if i < len(per_moment) else None),
                "attribution_required": bool(per_moment[i].get("attribution_required"))
                                       if i < len(per_moment) else False,
                "licence_name": per_moment[i].get("licence_name") if i < len(per_moment) else None,
            } for i, c in enumerate(clips)],
            description_attribution="; ".join(
                (per_moment[i].get("attribution_display") or "")
                for i in range(len(clips))
                if i < len(per_moment)
                and per_moment[i].get("attribution_required")
                and per_moment[i].get("attribution_display")),
            validation=validation.model_dump(),
            limitations=self._limitations(narration_provider, narration_audio,
                                          narration_failure, music, chime, whoosh),
            warnings=warnings,
            metrics={
                "segments": len(segments),
                "clip_duration_seconds": round(sum(c["duration_seconds"] for c in clips), 2),
                "total_duration_seconds": total_duration,
                "render_id": render_id,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "graphics_style": "fullbleed",
                "graphics_layout": {
                    "title": "Full-bleed title bar (solid black, y 0..200): "
                             "'RANKING' white + qualifier phrase red #ff2d2d, "
                             "subject words yellow #ffd400",
                    "ranked_list": "numbers 1. to 5. down the left edge, "
                                   "active rank yellow #ffd400, per-clip "
                                   "caption next to its number once played",
                    "video_area": "clip fills the whole 1080x1920 frame, "
                                  "no separate video zone, no dark background",
                    "bottom_caption": "clip caption uppercased, 64px white "
                                      "with 8px black stroke, centred at the bottom",
                    "transitions": "straight cuts, no black gaps, no intro/outro cards",
                    "voiceover": "off by design in the full-bleed style",
                },
            })
        manifest = report.model_dump(mode="json")
        (work_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        with self._sessions() as session:
            row = session.query(RenderedVideo).filter(RenderedVideo.id == rendered_id).first()
            row.file_path = str(output_path)
            row.duration_seconds = validation.duration_seconds
            row.width = validation.width
            row.height = validation.height
            row.status = "ready"
            session.commit()
        return report

    # ------------------------------------------------------------ validation
    def _validate_edl(self, job_id: str, clips: list[dict]) -> None:
        if len(clips) != REQUIRED_CLIPS:
            raise ExternalServiceError(
                f"Production found {len(clips)} EDL clip(s) for job {job_id!r}, "
                f"expected {REQUIRED_CLIPS}: run the Creative Director Agent first — "
                "do not invent a clip to fill the countdown")
        if [c.get("rank") for c in clips] != [5, 4, 3, 2, 1]:
            raise ExternalServiceError(
                f"Production EDL for job {job_id!r} is not in #5 -> #1 order: "
                f"{[c.get('rank') for c in clips]}")
        if len({c.get("moment_id") for c in clips}) != REQUIRED_CLIPS:
            raise ExternalServiceError(
                f"Production EDL for job {job_id!r} contains a duplicate moment")
        if len({c.get("candidate_video_id") for c in clips}) != REQUIRED_CLIPS:
            raise ExternalServiceError(
                f"Production EDL for job {job_id!r} does not use five different "
                "source videos")
        for clip in clips:
            for key in ("moment_id", "candidate_video_id", "source_path",
                        "start_seconds", "end_seconds", "timeline_start_seconds"):
                if clip.get(key) in (None, ""):
                    raise ExternalServiceError(
                        f"Production EDL clip rank #{clip.get('rank')} for job "
                        f"{job_id!r} is missing {key}")

    def _load_moments(self, session, job_id: str, clips: list[dict]) -> dict[str, AnalyzedMoment]:
        moments: dict[str, AnalyzedMoment] = {}
        for clip in clips:
            moment = (session.query(AnalyzedMoment)
                      .filter(AnalyzedMoment.id == clip["moment_id"],
                              AnalyzedMoment.job_id == job_id).first())
            if moment is None:
                raise ExternalServiceError(
                    f"Production EDL for job {job_id!r} references a moment that "
                    f"no longer exists (rank #{clip['rank']})")
            if (abs(moment.start_seconds - float(clip["start_seconds"])) > 0.001
                    or abs(moment.end_seconds - float(clip["end_seconds"])) > 0.001):
                raise ExternalServiceError(
                    f"Production EDL clip rank #{clip['rank']} timestamps "
                    f"{clip['start_seconds']}–{clip['end_seconds']}s do not match "
                    f"the verified moment {moment.start_seconds}–"
                    f"{moment.end_seconds}s — refusing to render from stale timestamps")
            moments[clip["moment_id"]] = moment
        return moments

    def _resolve_sources(self, session, job_id: str, clips: list[dict],
                         footage_paths: dict[str, Path]) -> dict[str, Path]:
        """Local footage file per candidate — a URL is never downloaded."""
        sources: dict[str, Path] = {}
        for clip in clips:
            candidate_id = clip["candidate_video_id"]
            path = footage_paths.get(candidate_id)
            if path is None:
                footage = (session.query(ApprovedFootage)
                           .filter(ApprovedFootage.candidate_video_id == candidate_id).first())
                if footage is not None and footage.storage_path:
                    path = Path(footage.storage_path)
            if path is None:
                raise ExternalServiceError(
                    f"Production has no local footage file for candidate "
                    f"{candidate_id} (rank #{clip['rank']}): ApprovedFootage."
                    "storage_path is empty and no footage path was supplied — "
                    "downloading source videos is not configured")
            if str(path).startswith(("http://", "https://")):
                raise ExternalServiceError(
                    f"Production cannot render candidate {candidate_id} "
                    f"(rank #{clip['rank']}) from a URL {path}: no download "
                    "step is configured — supply a local footage file")
            if not path.is_file():
                raise ExternalServiceError(
                    f"Production footage file does not exist for candidate "
                    f"{candidate_id} (rank #{clip['rank']}): {path}")
            sources[candidate_id] = path
        return sources

    # ------------------------------------------------------------ rendering
    def _render_timeline(self, job, clips, moments, sources, caption_plan,
                         per_moment, intro_s, outro_s, transition_s,
                         total_duration, work_dir: Path) -> tuple[list[dict], list[str]]:
        """Full-bleed timeline: the five clips, cut straight together.

        `intro_s`, `outro_s` and `transition_s` are accepted for
        call compatibility — the full-bleed EDL sets them all to
        0.0, and this method renders no intro/outro cards and no
        transition gaps. Each clip is reframed to the full
        1080x1920 frame and carries the title bar, the left-side
        numbers, its bottom caption and (when required) the
        licence attribution. The returned segments are exactly
        the five countdown clips, in #5 -> #1 order, with
        timeline starts measured from the start of the video
        (the EDL's own timeline_start_seconds are the same
        values in the full-bleed EDL, since there are no cards
        or gaps before them).
        """
        s = self.settings
        warnings: list[str] = []
        segments: list[dict] = []
        # Per-clip short captions, built once and drawn on every
        # segment. Labels reuse the Creative Director's own
        # caption text (the first eight words of the moment
        # narration, signal-grounded, never a description of what
        # happens in a clip) — nothing is invented here.
        list_items: list[tuple[int, str]] = []
        for i, clip in enumerate(clips):
            caption_text = clip.get("caption") or ""
            list_items.append((
                int(clip["rank"]),
                clean_clip_caption(caption_text, int(clip["rank"]))))
        cursor = 0.0
        for i, clip in enumerate(clips):
            moment = moments[clip["moment_id"]]
            source_path = sources[clip["candidate_video_id"]]
            probe = probe_video(source_path, ffprobe_path=s.ffprobe_path)
            centroid = motion_centroid_x(
                source_path, float(clip["start_seconds"]), float(clip["end_seconds"]))
            graphic = per_moment[i] if i < len(per_moment) else {}
            source_label = graphic.get("source_label") or "Untitled video"
            cue_start = float(clip["timeline_start_seconds"])
            clip_cues = [c for c in caption_plan if c.get("moment_id") == clip["moment_id"]]
            # ASS captions are raised (MarginV 380) so they sit
            # above the big bottom caption instead of colliding
            # with it.
            ass_path = self._ass_for_segment(
                work_dir, f"clip-{i + 1}", clip_cues, cue_start, margin_v=380)
            caption = dict(list_items).get(int(clip["rank"])) or ""
            gfx, _ = clip_graphics_filters(
                rank=int(clip["rank"]), topic=job.topic.strip(),
                list_items=list_items,
                source_label=source_label,
                attribution_text=graphic.get("attribution_display"),
                attribution_required=bool(graphic.get("attribution_required")),
                duration_seconds=float(clip["duration_seconds"]),
                work_dir=work_dir, font_file=s.prod_font_file,
                bottom_caption=caption)
            gfx = self._with_ass(gfx, ass_path)
            extracted = extract_clip(
                source_path, float(clip["start_seconds"]), float(clip["end_seconds"]),
                work_dir / f"seg-{i + 1:02d}-clip.mp4",
                ffmpeg_path=s.ffmpeg_path, ffprobe_path=s.ffprobe_path,
                extra_video_filters=gfx, subject_x=centroid,
                target_width=TARGET_WIDTH,
                target_height=1920,
                timeout_seconds=float(s.prod_render_timeout_seconds))
            if centroid is None:
                warnings.append(
                    f"rank #{clip['rank']}: no motion detected in the clip, "
                    "centre crop used instead of subject-aware reframing")
            segments.append({"path": str(extracted.output_path),
                             "ass_file": str(ass_path) if ass_path else None,
                             "report": {"kind": "countdown_moment", "rank": int(clip["rank"]),
                                        "moment_id": clip["moment_id"],
                                        "candidate_video_id": clip["candidate_video_id"],
                                        "duration_seconds": extracted.duration_seconds,
                                        "timeline_start_seconds": round(cursor, 2),
                                        "file_name": f"seg-{i + 1:02d}-clip.mp4",
                                        "subject_aware": extracted.subject_aware,
                                        "transition_in": graphic.get("transition_in"),
                                        "transition_rendered": "straight cut"}})
            cursor += extracted.duration_seconds
        rendered_total = sum(seg["report"]["duration_seconds"] for seg in segments)
        if abs(rendered_total - total_duration) > 0.01:
            raise ExternalServiceError(
                f"segment durations sum to {rendered_total:.2f}s but the EDL "
                f"total is {total_duration:.2f}s — refusing to render a "
                "timeline that does not match the plan")
        return segments, warnings

    @staticmethod
    def _with_ass(filters: str, ass_path: Path | None) -> str:
        if ass_path is None:
            return filters
        return f"{filters},{ass_filter(ass_path)}" if filters else ass_filter(ass_path)

    def _ass_for_segment(self, work_dir: Path, name: str, cues: list[dict],
                         segment_start: float, margin_v: int = 200) -> Path | None:
        if not cues:
            return None
        return build_ass(cues, work_dir / f"{name}.ass",
                         font_name="DejaVu Sans",
                         font_size=int(self.settings.prod_caption_font_size),
                         time_offset_seconds=segment_start,
                         margin_v=margin_v)

    # ------------------------------------------------------------ reads
    def get_rendered_video(self, job_id: str) -> RenderedVideoRead:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            row = (session.query(RenderedVideo)
                   .filter(RenderedVideo.job_id == job_id).first())
            if row is None:
                raise NotFoundError(f"no rendered video for job {job_id} — run the Video Production Agent first")
            return RenderedVideoRead(
                id=row.id, job_id=row.job_id, edit_instruction_id=row.edit_instruction_id,
                file_path=row.file_path, duration_seconds=row.duration_seconds,
                width=row.width, height=row.height, status=row.status,  # type: ignore[arg-type]
                created_at=row.created_at)

    def get_production(self, job_id: str) -> ProductionReport:
        """Rebuild the report from the persisted manifest file."""
        rendered = self.get_rendered_video(job_id)
        if not rendered.file_path:
            raise NotFoundError(f"no rendered video file for job {job_id}")
        manifest_path = Path(rendered.file_path).with_name("manifest.json")
        if not manifest_path.is_file():
            raise NotFoundError(f"no render manifest for job {job_id}: {manifest_path}")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return ProductionReport.model_validate(data)

    def get_manifest(self, job_id: str) -> dict:
        return self.get_production(job_id).model_dump(mode="json")

    @staticmethod
    def _limitations(narration_provider: NarrationProvider,
                     narration_audio: NarrationAudio | None,
                     narration_failure: str | None,
                     music: AudioAsset,
                     chime: AudioAsset, whoosh: AudioAsset) -> list[str]:
        narration_limitation = (
            "The voiceover is off by design in the full-bleed style — "
            "the reference videos carry the text on screen, not as "
            "narration — so no TTS synthesis was attempted. The "
            "render's audio track is the source clips' own audio; "
            "the narration script exists and the captions carry it, "
            "but no voice is claimed. The Edge TTS code is intact "
            "and can be re-enabled by a future style that wants a "
            "voiceover; QA flags this render needs_review.")
        limitations = [
            narration_limitation,
            "No licensed music library is configured: the render has no "
            "background music unless PROD_MUSIC_DIR holds a licensed "
            "file that probes as audio.",
            "Sound-effect mixing is not implemented: SFX files are "
            "detected when configured but never mixed into the render.",
            "Clips are joined with straight cuts — no black gaps, no "
            "intro/outro cards and no cross-dissolve transitions, "
            "to match the reference videos.",
            "Graphics use the full-bleed layout (clip fills the whole "
            "1080x1920 frame; solid black title bar with 'RANKING' "
            "white + qualifier phrase red and subject words yellow; "
            "numbers 1. to 5. down the left edge with the active "
            "rank yellow; per-clip caption next to its number; big "
            "uppercased bottom caption). There is no end-credits "
            "screen: a clip whose licence requires attribution gets "
            "a small attribution line in the lower third for the "
            "last 2 seconds of its segment, and the full attribution "
            "for every clip is in the manifest and the report's "
            "description_attribution — paste it into the video "
            "description when publishing. The clips are "
            "auto-selected by Momentrix from licensed sources — "
            "unlike the reference videos, the user does not hand-pick "
            "or order them — and the list labels reuse the Creative "
            "Director's signal-grounded narration text, not "
            "descriptions of what happens in a clip.",
            "Subject-aware reframing tracks the motion centroid measured "
            "by frame differencing; a static shot falls back to centre crop.",
        ]
        return limitations
