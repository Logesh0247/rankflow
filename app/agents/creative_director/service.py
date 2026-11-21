"""Momentrix AI — Creative Director Agent, Phases 1-7: service (Prompt 07).

Flow for one job:
  RankingResult rows (exactly five, ranks 1-5, five sources)
  -> re-check the Rights gate per candidate, server-side (an
     approval that expired after ranking must not leak into the
     story — the gate is the same one Video Intelligence and
     Ranking used)
  -> hook candidates, scored deterministically, strongest wins
  -> countdown storyboard #5 -> #1, signal-grounded narration
     (narration.py), visual direction, caption plan
  -> structured Edit Decision List, validated against the shared
     EditDecisionList contract (exactly five clips, 1080x1920 @ 30fps)
  -> Phase 7 validation (all five moments, narration matches
     footage, realistic timing, coherent story, appropriate
     duration) -> persist Storyboard + Narration + EditInstruction.

Failure handling (never silent, never fabricated):
* no ranking / fewer than five ranks -> ExternalServiceError
  (run Ranking first; do not invent a moment);
* a ranked candidate no longer Rights-approved -> the gate
  raises ValidationError naming it (fail closed, never bypassed);
* validation finds a forbidden word, a missing moment, a
  timestamp mismatch or narration longer than the timeline ->
  ExternalServiceError listing the issues (the plan is rejected,
  not quietly edited);
* re-running replaces the job's storyboard / narration / EDL, so
  a re-run never duplicates them.

The executor hands only the storyboard id, scene count and the
five ranked moment ids to the orchestrator as a validated
CreativeOutput; the full plan is persisted in the database rows.
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.agents.creative_director.hooks import GENERATOR, HookCandidate, select_hook
from app.agents.creative_director.narration import (
    build_moment_narration, closing_text, estimate_seconds,
    narration_contains_forbidden_word, split_captions,
)
from app.agents.creative_director.signals import MomentPlanInput, moment_plan_input
from app.agents.creative_director.visual import overall_visual_direction
from app.agents.rights.gate import require_approved
from app.agents.rights.service import RightsService
from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError, NotFoundError
from app.models.models import (
    AnalyzedMoment, ApprovedFootage, CandidateVideo, EditInstruction,
    Narration, RankingResult, Storyboard,
)
from app.orchestrator.jobs import JobService
from app.schemas.schemas import (
    EditDecisionList, EditInstructionRead, EDLClip, NarrationRead,
    StoryboardCreate, StoryboardRead, StoryboardScene,
)

REQUIRED_RANKS = 5


class CreativeReport(BaseModel):
    """Full result of one creative run (the executor returns only its ids)."""

    job_id: str
    generator: str = GENERATOR
    storyboard_id: str
    narration_id: str
    edit_instruction_id: str
    title: str
    concept: str
    hook: dict
    scenes: list[dict] = Field(default_factory=list)
    narration_script: str
    narration_estimated_seconds: float
    total_duration_seconds: float
    edl: dict = Field(default_factory=dict)
    caption_plan: list[dict] = Field(default_factory=list)
    visual_direction: dict = Field(default_factory=dict)
    attributions: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)


def validate_plan(*, moments: list[MomentPlanInput], hook_text: str,
                  moment_narrations: dict[str, str], closing: str,
                  edl_clips: list[dict], total_duration_seconds: float,
                  narration_estimated_seconds: float) -> tuple[list[str], list[str]]:
    """Phase 7 validation. Returns (issues, warnings); issues fail the plan.

    Checks: all five ranked moments represented exactly once in
    the EDL, in #5 -> #1 order, from five different sources;
    EDL timestamps equal the verified moment timestamps; no
    forbidden (semantic-event) word in any moment narration or
    the closing; narration is not longer than the timeline
    (continuous voiceover must fit the video); total duration is
    a Short (15-90s); hook is present and makes no footage claim
    (it may quote the user's topic, never a clip event).
    """
    issues: list[str] = []
    warnings: list[str] = []
    if len(moments) != REQUIRED_RANKS:
        issues.append(f"expected {REQUIRED_RANKS} ranked moments, got {len(moments)}")
    ranks = sorted(m.rank for m in moments)
    if ranks != [1, 2, 3, 4, 5]:
        issues.append(f"ranked moments must be ranks 1-5, got {ranks}")
    if len({m.moment_id for m in moments}) != len(moments):
        issues.append("ranked moments contain a duplicate moment id")
    if len({m.candidate_video_id for m in moments}) != len(moments):
        issues.append("ranked moments must come from five different source videos")
    countdown = sorted(moments, key=lambda m: -m.rank)
    if [c["moment_id"] for c in edl_clips] != [m.moment_id for m in countdown]:
        issues.append("EDL clips must be the five ranked moments, in #5 -> #1 order")
    for clip, moment in zip(edl_clips, countdown):
        if (abs(clip["start_seconds"] - moment.start_seconds) > 0.001
                or abs(clip["end_seconds"] - moment.end_seconds) > 0.001):
            issues.append(
                f"EDL clip for moment {moment.moment_id} timestamps "
                f"{clip['start_seconds']}–{clip['end_seconds']}s do not match the "
                f"verified moment {moment.start_seconds}–{moment.end_seconds}s")
    for moment in moments:
        text = moment_narrations.get(moment.moment_id, "")
        if not text.strip():
            issues.append(f"moment {moment.moment_id} (rank #{moment.rank}) has no narration")
        bad = narration_contains_forbidden_word(text)
        if bad:
            issues.append(
                f"moment {moment.moment_id} narration contains semantic-event "
                f"word(s) not supported by the footage signals: {', '.join(bad)}")
        if f"Number {moment.rank}." not in text:
            issues.append(f"moment {moment.moment_id} narration does not state its rank")
    bad_closing = narration_contains_forbidden_word(closing)
    if bad_closing:
        issues.append(f"closing narration contains forbidden word(s): {', '.join(bad_closing)}")
    if not hook_text.strip():
        issues.append("no hook was selected")
    if narration_estimated_seconds > total_duration_seconds + 2.0:
        issues.append(
            f"narration is {narration_estimated_seconds:.1f}s but the timeline is only "
            f"{total_duration_seconds:.1f}s — the voiceover cannot fit the video")
    if not 15.0 <= total_duration_seconds <= 90.0:
        warnings.append(
            f"total duration {total_duration_seconds:.1f}s is outside the usual "
            "15-90s Short range; the plan is still usable, but check the target length")
    # Per-scene overrun is a warning, not a failure: narration is a
    # continuous voiceover cued at each clip's start, not a hard
    # per-clip limit — but a large overrun means the cue will still
    # be speaking well into the next clip.
    for moment in moments:
        words = len(moment_narrations.get(moment.moment_id, "").split())
        est = words / 150.0 * 60.0
        if est > moment.duration + 2.0:
            warnings.append(
                f"rank #{moment.rank} narration ({est:.1f}s) runs "
                f"{est - moment.duration:.1f}s past its {moment.duration:.1f}s clip — "
                "it will carry over the transition into the next clip")
    return issues, warnings


class CreativeDirectorService:
    def __init__(self, engine: Engine, settings: Settings | None = None):
        self._engine = engine
        self.settings = settings or get_settings()
        self._sessions = sessionmaker(bind=engine)
        self.jobs = JobService(engine)
        self.rights = RightsService(engine, self.settings)

    # ------------------------------------------------------------ main entry
    def run(self, job_id: str) -> CreativeReport:
        job = self.jobs.get_job(job_id)  # raises NotFoundError for unknown jobs
        started = time.monotonic()
        with self._sessions() as session:
            moments = self._load_ranked_moments(session, job_id)
            # RIGHTS GATE, again — before any ranked candidate is used.
            require_approved(session, job_id,
                             [m.candidate_video_id for m in moments],
                             purpose="creative_director")
            attributions = {a.candidate_video_id: a for a in self.rights.list_attribution(job_id)}
            for moment in moments:
                attr = attributions.get(moment.candidate_video_id)
                moment.attribution_text = attr.attribution_text if attr else None
                moment.attribution_required = bool(attr and attr.attribution_required)
                moment.licence_name = attr.licence_name if attr else None
                footage = (session.query(ApprovedFootage)
                           .filter(ApprovedFootage.candidate_video_id == moment.candidate_video_id).first())
                moment.footage_path = footage.storage_path if footage and footage.storage_path else None
            report = self._build(job, moments)
            issues, warnings = validate_plan(
                moments=moments, hook_text=report.hook["text"],
                moment_narrations={m.moment_id: next(
                    s["narration"] for s in report.scenes if s.get("rank") == m.rank)
                    for m in moments},
                closing=closing_text(), edl_clips=report.edl["clips"],
                total_duration_seconds=report.total_duration_seconds,
                narration_estimated_seconds=report.narration_estimated_seconds)
            if issues:
                raise ExternalServiceError(
                    f"Creative Director validation failed for job {job_id!r}: "
                    + "; ".join(issues))
            report.warnings = warnings
            self._persist(session, job_id, report)
            session.commit()
        report.metrics["duration_ms"] = int((time.monotonic() - started) * 1000)
        return report

    # ------------------------------------------------------------ loading
    def _load_ranked_moments(self, session, job_id: str) -> list[MomentPlanInput]:
        rows = (session.query(RankingResult)
                .filter(RankingResult.job_id == job_id)
                .order_by(RankingResult.rank.asc()).all())
        if len(rows) != REQUIRED_RANKS:
            raise ExternalServiceError(
                f"Creative Director found {len(rows)} ranked moment(s) for job {job_id!r}, "
                f"expected {REQUIRED_RANKS}: run the Ranking Agent first — "
                "do not invent a moment to fill the countdown")
        if [r.rank for r in rows] != [1, 2, 3, 4, 5]:
            raise ExternalServiceError(
                f"Creative Director ranking for job {job_id!r} is not ranks 1-5: "
                f"{[r.rank for r in rows]}")
        if len({r.candidate_video_id for r in rows}) != REQUIRED_RANKS:
            raise ExternalServiceError(
                f"Creative Director ranking for job {job_id!r} does not use five "
                "different source videos — re-run Ranking")
        moments: list[MomentPlanInput] = []
        for row in rows:
            moment = (session.query(AnalyzedMoment)
                      .filter(AnalyzedMoment.id == row.moment_id,
                              AnalyzedMoment.job_id == job_id).first())
            candidate = (session.query(CandidateVideo)
                         .filter(CandidateVideo.id == row.candidate_video_id,
                                 CandidateVideo.job_id == job_id).first())
            if moment is None or candidate is None:
                raise ExternalServiceError(
                    f"Creative Director ranking for job {job_id!r} references a "
                    f"moment/candidate that no longer exists (rank #{row.rank})")
            moments.append(moment_plan_input(moment, candidate, row))
        return moments

    # ------------------------------------------------------------ building
    def _build(self, job, moments: list[MomentPlanInput]) -> CreativeReport:
        s = self.settings
        countdown = sorted(moments, key=lambda m: -m.rank)  # #5 first
        # Full-bleed style (2026-11-21): no intro card, no outro
        # card and no transition gaps — the title bar and the
        # numbered list are overlaid on the clips, and the five
        # clips are cut straight together. The timeline is exactly
        # the five clip durations. cd_intro_seconds /
        # cd_outro_seconds / cd_transition_seconds are retained in
        # config for the old layout and are not used by this EDL.
        intro_s = 0.0
        outro_s = 0.0
        transition_s = 0.0
        wpm = float(s.cd_narration_wpm)
        clip_total = sum(m.duration for m in countdown)
        total_duration = round(clip_total, 2)

        hook = select_hook(job.topic)
        closing = closing_text()

        # Narration word budget: the whole voiceover must fit the
        # timeline (continuous voiceover, 95% of the timeline at
        # the configured words-per-minute). Hook and closing take
        # their words first; the rest is shared between the five
        # moments in proportion to their clip lengths. The floor
        # is 2 words per moment — the rank intro ("Number N."),
        # which build_moment_narration always keeps — never 4:
        # on a short timeline (five 2s clips = 10s) a 4-word
        # floor would force the script past the budget and fail
        # validation, as the full-bleed timeline (clips only, no
        # intro/outro/gaps to pad it) exposed.
        total_words = int(total_duration * wpm / 60.0 * 0.95)
        hook_words = len(hook.text.split())
        closing_words = len(closing.split())
        remaining = max(5 * 2, total_words - hook_words - closing_words)
        budgets = self._allocate_words(countdown, remaining)

        narrations: dict[int, str] = {}
        narration_bases: dict[int, str] = {}
        for moment in countdown:
            built = build_moment_narration(moment, word_budget=budgets[moment.rank],
                                           words_per_minute=wpm)
            narrations[moment.rank] = built.text
            narration_bases[moment.rank] = built.basis

        script_parts = [hook.text] + [narrations[m.rank] for m in countdown] + [closing]
        narration_script = " ".join(script_parts)
        narration_seconds = estimate_seconds(narration_script, words_per_minute=wpm)

        title = f"{job.topic.strip()} — Top 5 Countdown"
        concept = (
            "A countdown of the five highest-scoring measured moments, "
            "presented #5 to #1, with short signal-based narration and the "
            "source shown on screen for each clip. Narration describes only "
            "what Video Intelligence measured (motion, sharpness, brightness, "
            "clarity, duration); no semantic event is claimed.")
        visual = overall_visual_direction(countdown)

        # Timeline offsets (seconds from the start of the video).
        offsets: dict[str, float] = {}
        cursor = 0.0
        offsets["intro"] = cursor
        cursor += intro_s + transition_s
        for moment in countdown:
            offsets[moment.moment_id] = round(cursor, 2)
            cursor += moment.duration + transition_s
        offsets["outro"] = round(cursor, 2)

        scenes: list[dict] = [{
            "scene_number": 1, "rank": None, "moment_id": None,
            "kind": "intro", "narration": hook.text,
            "duration_seconds": None,
            "caption_lines": split_captions(hook.text),
            "graphic": ("No intro card in the full-bleed style — the "
                        "title bar and numbers are overlaid on the first clip."),
        }]
        for i, moment in enumerate(countdown, start=2):
            scenes.append({
                "scene_number": i, "rank": moment.rank, "moment_id": moment.moment_id,
                "kind": "countdown_moment", "narration": narrations[moment.rank],
                "duration_seconds": moment.duration,
                "caption_lines": split_captions(narrations[moment.rank]),
                "graphic": (f"Countdown #{moment.rank} overlay; rank label "
                            f"'No. {moment.rank} of 5'; source lower third."),
                "narration_basis": narration_bases[moment.rank],
                "transition_in": visual["per_moment"][i - 2]["transition_in"],
            })
        scenes.append({
            "scene_number": len(countdown) + 2, "rank": None, "moment_id": None,
            "kind": "outro", "narration": closing,
            "duration_seconds": None,
            "caption_lines": split_captions(closing),
            "graphic": ("No outro card in the full-bleed style — "
                        "attribution is in the manifest/description, not a "
                        "credits screen."),
        })

        caption_plan = self._caption_plan(scenes, offsets, wpm)
        edl = self._build_edl(job, countdown, narrations, offsets, visual,
                               caption_plan, intro_s, outro_s, transition_s,
                               total_duration)
        attributions = [{
            "candidate_video_id": m.candidate_video_id,
            "title": m.candidate_title,
            "attribution_text": m.attribution_text,
            "attribution_required": m.attribution_required,
            "licence_name": m.licence_name,
        } for m in countdown]
        limitations = self._limitations()
        # Note in the report when any clip has no local footage file:
        # Production will need to resolve the source URL first.
        return CreativeReport(
            job_id=job.id, storyboard_id="", narration_id="", edit_instruction_id="",
            title=title, concept=concept,
            hook={"text": hook.text, "attention": hook.attention,
                  "relevance": hook.relevance, "curiosity": hook.curiosity,
                  "clarity": hook.clarity, "natural_delivery": hook.natural_delivery,
                  "total": hook.total, "explanation": hook.explanation},
            scenes=scenes, narration_script=narration_script,
            narration_estimated_seconds=narration_seconds,
            total_duration_seconds=total_duration, edl=edl,
            caption_plan=caption_plan, visual_direction=visual,
            attributions=attributions, limitations=limitations,
            metrics={
                "ranked_moments": len(moments),
                "clip_duration_seconds": round(clip_total, 2),
                "narration_words": len(narration_script.split()),
                "hook_score": hook.total,
                "target_duration_seconds": float(s.cd_target_duration_seconds),
            })

    @staticmethod
    def _allocate_words(countdown: list[MomentPlanInput], total: int) -> dict[int, int]:
        """Share a word budget between moments by clip length (min 2 each).

        The minimum is the 2-word rank intro ("Number N.") —
        build_moment_narration always keeps it, and a higher floor
        would overshoot the budget on short timelines (see _build).
        """
        weights = [max(m.duration, 0.5) for m in countdown]
        total_weight = sum(weights)
        budgets: dict[int, int] = {}
        used = 0
        for moment, weight in zip(countdown, weights):
            share = max(2, int(total * weight / total_weight))
            budgets[moment.rank] = share
            used += share
        # Trim the longest moments first if the shares overshoot.
        while used > total:
            longest = max(countdown, key=lambda m: budgets[m.rank])
            if budgets[longest.rank] <= 2:
                break
            budgets[longest.rank] -= 1
            used -= 1
        return budgets

    def _caption_plan(self, scenes: list[dict], offsets: dict[str, float],
                      wpm: float) -> list[dict]:
        """Caption cues for every scene, timed inside its segment.

        Each caption line gets a slice of its scene's duration,
        proportional to its word count, so captions never run past
        the scene they belong to.
        """
        plan: list[dict] = []
        for scene in scenes:
            # Scenes with no duration (intro/outro in the full-bleed
            # style) carry storyboard text only — they get no
            # caption cues, because a zero-length cue would fail
            # validation in Production and in QA.
            if not scene.get("duration_seconds"):
                continue
            key = scene["moment_id"] or scene["kind"]
            start = offsets[key]
            duration = float(scene["duration_seconds"])
            lines = scene["caption_lines"]
            total_words = sum(len(line.split()) for line in lines) or 1
            cursor = start
            for line in lines:
                share = duration * len(line.split()) / total_words
                plan.append({"scene_number": scene["scene_number"],
                             "rank": scene["rank"], "moment_id": scene["moment_id"],
                             "start_seconds": round(cursor, 2),
                             "end_seconds": round(cursor + share, 2),
                             "text": line})
                cursor += share
        return plan

    def _build_edl(self, job, countdown, narrations, offsets, visual,
                   caption_plan, intro_s, outro_s, transition_s,
                   total_duration) -> dict:
        # Core clips, validated against the shared contract first.
        clips = []
        for i, moment in enumerate(countdown, start=1):
            clips.append(EDLClip(
                clip_number=i, moment_id=moment.moment_id,
                source_path=self._footage_path_for_edl(moment),
                start_seconds=moment.start_seconds, end_seconds=moment.end_seconds,
                caption=" ".join(narrations[moment.rank].split()[:8])))
        EditDecisionList(job_id=job.id, clips=clips, output_width=1080,
                         output_height=1920, fps=30)
        # Extended EDL: the core clips plus the cues Production needs.
        extended = []
        for i, moment in enumerate(countdown, start=1):
            clip = clips[i - 1]
            cue_start = offsets[moment.moment_id]
            extended.append({
                "clip_number": i, "rank": moment.rank,
                "moment_id": moment.moment_id,
                "candidate_video_id": moment.candidate_video_id,
                "source_path": clip.source_path,
                "start_seconds": moment.start_seconds,
                "end_seconds": moment.end_seconds,
                "duration_seconds": moment.duration,
                "timeline_start_seconds": cue_start,
                "caption": clip.caption,
                "narration_cue": {
                    "text": narrations[moment.rank],
                    "start_seconds": cue_start,
                    "estimated_seconds": estimate_seconds(narrations[moment.rank]),
                },
                "graphic_instructions": visual["per_moment"][i - 1],
                "caption_timing": [c for c in caption_plan
                                   if c["moment_id"] == moment.moment_id],
                "audio_cues": [
                    {"cue": "countdown chime on rank reveal",
                     "at_seconds": cue_start},
                    {"cue": "soft whoosh on transition in",
                     "at_seconds": cue_start},
                    {"cue": "duck background music under narration",
                     "at_seconds": cue_start},
                ],
                "transition_in": visual["per_moment"][i - 1]["transition_in"],
                "transition_out": "straight cut (end of video)" if i == len(countdown)
                                  else "straight cut",
            })
        return {
            "job_id": job.id, "clips": extended,
            "output_width": 1080, "output_height": 1920, "fps": 30,
            "intro_seconds": intro_s, "outro_seconds": outro_s,
            "transition_seconds": transition_s,
            "total_duration_seconds": total_duration,
            "caption_plan": caption_plan, "visual_direction": visual,
        }

    def _footage_path_for_edl(self, moment: MomentPlanInput) -> str:
        """Source path for the EDL clip: the local footage file when
        on record (set in run()), else the source URL as the
        retrieval reference. Production must resolve/download
        before rendering when no local file exists."""
        if moment.footage_path:
            return moment.footage_path
        if moment.candidate_url:
            return moment.candidate_url
        raise ExternalServiceError(
            f"Creative Director has no footage reference for candidate "
            f"{moment.candidate_video_id} (rank #{moment.rank}): no local "
            "footage file and no source URL — Production cannot render it")

    # ------------------------------------------------------------ persistence
    def _persist(self, session, job_id: str, report: CreativeReport) -> None:
        """Replace the job's storyboard / narration / EDL on re-run."""
        session.query(EditInstruction).filter(EditInstruction.job_id == job_id).delete()
        session.query(Narration).filter(Narration.job_id == job_id).delete()
        session.query(Storyboard).filter(Storyboard.job_id == job_id).delete()
        session.flush()
        storyboard = Storyboard(
            job_id=job_id, title=report.title, hook=report.hook["text"],
            narration_script=report.narration_script,
            scenes=[{k: v for k, v in scene.items()} for scene in report.scenes])
        session.add(storyboard)
        session.flush()
        narration = Narration(
            job_id=job_id, storyboard_id=storyboard.id,
            script_text=report.narration_script,
            duration_seconds=report.narration_estimated_seconds, status="draft")
        session.add(narration)
        session.flush()
        instruction = EditInstruction(
            job_id=job_id, storyboard_id=storyboard.id, edl=report.edl,
            output_width=1080, output_height=1920, fps=30)
        session.add(instruction)
        session.flush()
        report.storyboard_id = storyboard.id
        report.narration_id = narration.id
        report.edit_instruction_id = instruction.id

    # ------------------------------------------------------------ reads
    def get_storyboard(self, job_id: str) -> StoryboardRead:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            row = (session.query(Storyboard)
                   .filter(Storyboard.job_id == job_id).first())
            if row is None:
                raise NotFoundError(f"no storyboard for job {job_id} — run the Creative Director Agent first")
            return StoryboardRead(
                id=row.id, job_id=row.job_id, title=row.title, hook=row.hook,
                narration_script=row.narration_script,
                scenes=[StoryboardScene.model_validate(s) for s in (row.scenes or [])],
                created_at=row.created_at)

    def get_narration(self, job_id: str) -> NarrationRead:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            row = (session.query(Narration)
                   .filter(Narration.job_id == job_id).first())
            if row is None:
                raise NotFoundError(f"no narration for job {job_id} — run the Creative Director Agent first")
            return NarrationRead(
                id=row.id, job_id=row.job_id, storyboard_id=row.storyboard_id,
                script_text=row.script_text, audio_path=row.audio_path,
                duration_seconds=row.duration_seconds, status=row.status,  # type: ignore[arg-type]
                created_at=row.created_at)

    def get_edl(self, job_id: str) -> EditInstructionRead:
        self.jobs.get_job(job_id)
        with self._sessions() as session:
            row = (session.query(EditInstruction)
                   .filter(EditInstruction.job_id == job_id).first())
            if row is None:
                raise NotFoundError(f"no edit instructions for job {job_id} — run the Creative Director Agent first")
            return EditInstructionRead(
                id=row.id, job_id=row.job_id, storyboard_id=row.storyboard_id,
                edl=dict(row.edl or {}), output_width=row.output_width,
                output_height=row.output_height, fps=row.fps, created_at=row.created_at)

    def get_creative(self, job_id: str) -> CreativeReport:
        """Rebuild the full plan from the persisted rows."""
        storyboard = self.get_storyboard(job_id)
        narration = self.get_narration(job_id)
        edl = self.get_edl(job_id)
        scenes = [dict(s) for s in (storyboard.scenes or [])]
        edl_dict = dict(edl.edl or {})
        return CreativeReport(
            job_id=job_id, storyboard_id=storyboard.id,
            narration_id=narration.id, edit_instruction_id=edl.id,
            title=storyboard.title or "", concept="",
            hook={"text": storyboard.hook or ""},
            scenes=scenes, narration_script=narration.script_text,
            narration_estimated_seconds=narration.duration_seconds or 0.0,
            total_duration_seconds=float(edl_dict.get("total_duration_seconds") or 0.0),
            edl=edl_dict,
            caption_plan=list(edl_dict.get("caption_plan") or []),
            visual_direction=dict(edl_dict.get("visual_direction") or {}),
            attributions=[], limitations=self._limitations())

    @staticmethod
    def _limitations() -> list[str]:
        return [
            f"Generator is {GENERATOR}: deterministic templates, no LLM — "
            "narration is built from measured frame signals only. No "
            "vision-language model is configured, so no semantic event "
            "is claimed in any narration; a VLM could replace the "
            "templates later without changing the storyboard/EDL schema.",
            "Narration is a continuous voiceover cued at each clip's start; "
            "a moment's narration may carry over its transition — the "
            "validator checks the total voiceover against the total timeline.",
            "No licensed music or SFX track is selected: no licensed music / "
            "SFX library is configured. Production must supply licensed assets.",
            "Source paths in the EDL are the approved footage file when on "
            "record, otherwise the source URL as the retrieval reference — "
            "Production must resolve/download before rendering.",
        ]
