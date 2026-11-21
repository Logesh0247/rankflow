"""Momentrix AI — Research Agent, Phases 5-6: persistence + orchestration (Prompt 03).

Flow for one job:
  topic -> SearchPlan (topic.py) -> providers (providers.py, in order,
  with fallback) -> RawCandidates -> score + deduplicate (scoring.py)
  -> duration cap (known-long videos excluded, short ones boosted;
  re-applied after Archive duration enrichment, with backfill) ->
  persist Source + CandidateVideo rows linked to the job -> return
  a ResearchReport; the executor (agent.py) hands only the candidate
  ids to the orchestrator as a validated ResearchOutput.

Persistence is idempotent: re-running the agent for the same job
upserts by (job, source, external_video_id) instead of creating
duplicate rows, so a resumed job does not double its candidates.

Failure handling (brief, verbatim in behaviour):
* provider unavailable -> retry inside the provider, then try the
  next authorised provider in the chain;
* no relevant results -> try the next query in the search strategy
  (exact -> expanded -> broad);
* duplicates -> removed by deduplicate(), counted in the report;
* missing metadata -> candidate kept, completeness score lowered,
  licence left as None (never invented);
* no eligible candidates at all -> ExternalServiceError with a clear
  limitation message. The orchestrator retries the stage (its normal
  retry policy) and then fails the job with that message stored —
  a silent empty result is never returned, because ResearchOutput
  requires at least one candidate id.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import ExternalServiceError
from app.models.models import CandidateVideo, Source
from app.agents.research.providers import (
    InternetArchiveProvider,
    ProviderUnavailable,
    RawCandidate,
    build_provider,
)
from app.agents.research.scoring import (
    ScoredCandidate,
    deduplicate,
)
from app.agents.research.topic import SearchPlan, understand_topic
from app.orchestrator.jobs import JobService
from app.schemas.schemas import CandidateVideoRead


def _filter_by_duration(items: list[ScoredCandidate]) -> tuple[list[ScoredCandidate], int]:
    """Drop candidates the duration factor excludes (factor 0 =
    known duration over the cap) and count them. Unknown durations
    (factor 1.0) and short videos (factor 1.15) are always kept."""
    kept = [item for item in items if item.duration_factor > 0]
    return kept, len(items) - len(kept)


class ProviderResult(BaseModel):
    provider: str
    query: str
    status: str  # "ok" | "unavailable" | "error"
    returned: int = 0
    error: str | None = None


class ResearchReport(BaseModel):
    """Full result of one research run (the executor returns only its ids)."""

    job_id: str
    topic: str
    plan: SearchPlan
    candidates: list[CandidateVideoRead] = Field(default_factory=list)
    provider_results: list[ProviderResult] = Field(default_factory=list)
    duplicates_removed: int = 0
    queries_used: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)


class ResearchService:
    def __init__(self, engine: Engine, settings: Settings | None = None):
        self._engine = engine
        self.settings = settings or get_settings()
        self._sessions = sessionmaker(bind=engine)
        self.jobs = JobService(engine)

    # ------------------------------------------------------------ main entry
    def run(self, job_id: str, topic: str | None = None) -> ResearchReport:
        job = self.jobs.get_job(job_id)  # raises NotFoundError for unknown jobs
        topic = (topic or job.topic).strip()
        if not topic:
            raise ValueError("topic must not be empty")
        plan = understand_topic(topic)
        max_candidates = max(1, self.settings.research_max_candidates)
        max_minutes = self.settings.research_max_candidate_minutes

        raw, provider_results, queries_used = self._discover(job_id, plan, max_candidates)
        if not raw:
            detail = "; ".join(
                f"{r.provider} ({r.query!r}): {r.error or r.status}"
                for r in provider_results) or "no provider was tried"
            raise ExternalServiceError(
                f"Research found no eligible candidates for topic {topic!r}. "
                f"Queries tried: {queries_used}. Provider outcomes: {detail}. "
                f"Try a broader topic or add a discovery provider (YOUTUBE_API_KEY).")

        scored = [ScoredCandidate(c, plan.keywords,
                                  max_candidate_minutes=max_minutes)
                  for c in raw]
        kept, duplicates_removed = deduplicate(scored)
        # Duration cap, first pass: candidates whose duration is
        # already known (YouTube, Pexels, Pixabay always publish
        # one; Archive usually does not yet) and exceeds the cap
        # are excluded — a feature film is never the right source
        # for a Short. Unknown durations are kept, never excluded.
        kept, excluded_known = _filter_by_duration(kept)
        kept = sorted(kept, key=lambda s: s.combined, reverse=True)
        # Enrich durations for a pool larger than the final cut, so
        # a movie whose duration only becomes known here can be
        # excluded with a short replacement still available.
        self._enrich_durations(kept[:max_candidates + 5])
        # Re-score after enrichment: duration changes completeness
        # and the duration factor — then re-apply the cap, because
        # an Archive movie's length is often only known now.
        for item in kept:
            item.rescore_duration(max_minutes)
        kept, excluded_late = _filter_by_duration(kept)
        kept = sorted(kept, key=lambda s: s.combined, reverse=True)[:max_candidates]
        duration_excluded = excluded_known + excluded_late
        if not kept:
            raise ExternalServiceError(
                f"Research found {len(raw)} candidates for topic {topic!r}, "
                f"but all were excluded: {duration_excluded} exceed the "
                f"{max_minutes:.0f}-minute duration cap "
                f"(RESEARCH_MAX_CANDIDATE_MINUTES) — a Short is built "
                f"from short clips. Try a broader topic or raise the cap.")

        candidates = self._persist(job_id, kept)
        limitations = self._limitations(plan, kept, provider_results,
                                        duration_excluded, max_minutes)
        metrics = self._metrics(raw, kept, duplicates_removed,
                                duration_excluded)
        return ResearchReport(
            job_id=job_id, topic=topic, plan=plan, candidates=candidates,
            provider_results=provider_results, duplicates_removed=duplicates_removed,
            queries_used=queries_used, limitations=limitations, metrics=metrics)

    # ------------------------------------------------------------ discovery
    def _discover(self, job_id: str, plan: SearchPlan, max_candidates: int):
        """Try queries in strategy order, providers in chain order.

        Stops early once twice the needed raw candidates are in hand;
        otherwise keeps expanding (exact -> expanded -> broad) so a
        narrow first query does not end the run with nothing.
        """
        providers = self._provider_chain()
        raw: list[RawCandidate] = []
        provider_results: list[ProviderResult] = []
        queries_used: list[str] = []
        for query in plan.queries:
            queries_used.append(query.text)
            for provider in providers:
                try:
                    found = provider.search(query.text, limit=25)
                except ProviderUnavailable as exc:
                    provider_results.append(ProviderResult(
                        provider=provider.name, query=query.text,
                        status="unavailable", error=str(exc)))
                    continue
                except Exception as exc:  # noqa: BLE001 — one provider must not kill the run
                    provider_results.append(ProviderResult(
                        provider=provider.name, query=query.text,
                        status="error", error=str(exc)))
                    continue
                provider_results.append(ProviderResult(
                    provider=provider.name, query=query.text,
                    status="ok", returned=len(found)))
                # Prompt 12, Phase 3: price the provider call (IA and
                # YouTube are $0 inside quota — recorded, not assumed).
                try:
                    from app.core.cost import CostService
                    CostService(self._engine, self.settings).record_api_call(
                        job_id, provider.name, query.text)
                except Exception:  # noqa: BLE001 — best-effort only
                    pass
                raw.extend(found)
            if len(raw) >= max_candidates * 2:
                break
        return raw, provider_results, queries_used

    def _provider_chain(self) -> list:
        """Discovery providers in search order.

        `RESEARCH_PROVIDERS` (comma-separated, e.g.
        "youtube,internet_archive") wins when set: the named
        providers are tried in that order, duplicates removed. The
        test provider is never mixed with real ones — if "test" is
        listed, the chain is test-only, so its results stay
        deterministic. When RESEARCH_PROVIDERS is empty, the legacy
        single `research_provider` is used: the primary provider
        first, Internet Archive as fallback (except for "test",
        which never gets a fallback). An unknown name is a
        configuration error, raised honestly instead of silently
        dropping a provider.
        """
        configured = [p.strip() for p in
                      (self.settings.research_providers or "").split(",")]
        configured = [p for p in configured if p]
        if configured:
            names = list(dict.fromkeys(configured))  # dedupe, keep order
            if "test" in names:
                return [build_provider("test", self.settings)]
            return [build_provider(name, self.settings) for name in names]
        primary = self.settings.research_provider
        chain = [build_provider(primary, self.settings)]
        if primary != "test" and primary != "internet_archive":
            chain.append(build_provider("internet_archive", self.settings))
        return chain

    def _enrich_durations(self, items: list[ScoredCandidate]) -> None:
        """Fill in durations from the Internet Archive metadata API.

        Only for internet_archive candidates, and only when enabled.
        A failed enrichment leaves duration as None. The caller
        passes a pool larger than the final cut, so a movie whose
        duration only becomes known here can be excluded by the
        duration cap with a short replacement still available.
        """
        if not self.settings.research_enrich_duration:
            return
        ia = InternetArchiveProvider(timeout=10.0, max_attempts=1)
        for item in items:
            cand = item.candidate
            if cand.platform != "internet_archive" or cand.duration_seconds:
                continue
            cand.duration_seconds = ia.enrich_duration(cand.external_id)

    # ------------------------------------------------------------ persistence
    def _persist(self, job_id: str, kept: list[ScoredCandidate]) -> list[CandidateVideoRead]:
        out: list[CandidateVideoRead] = []
        with self._sessions() as session:
            for item in kept:
                cand = item.candidate
                source = (session.query(Source)
                          .filter(Source.platform == cand.platform,
                                  Source.external_id == cand.external_id).first())
                if source is None:
                    source = Source(platform=cand.platform, external_id=cand.external_id,
                                    title=cand.title, url=cand.url)
                    session.add(source)
                    session.flush()
                existing = (session.query(CandidateVideo)
                            .filter(CandidateVideo.job_id == job_id,
                                    CandidateVideo.source_id == source.id,
                                    CandidateVideo.external_video_id == cand.external_id).first())
                if existing is None:
                    existing = CandidateVideo(
                        job_id=job_id, source_id=source.id,
                        external_video_id=cand.external_id)
                    session.add(existing)
                existing.title = cand.title
                existing.url = cand.url
                existing.duration_seconds = cand.duration_seconds
                existing.view_count = cand.view_count
                existing.published_at = cand.published_at
                existing.relevance_score = item.relevance
                existing.creator = cand.creator
                existing.description = cand.description
                existing.licence = cand.licence
                existing.status = "discovered"
                session.flush()
                out.append(CandidateVideoRead(
                    id=existing.id, job_id=job_id, source_id=source.id,
                    external_video_id=cand.external_id, title=cand.title,
                    url=cand.url, duration_seconds=cand.duration_seconds,
                    view_count=cand.view_count, relevance_score=item.relevance,
                    status="discovered", published_at=cand.published_at,
                    created_at=existing.created_at,
                    creator=cand.creator, description=cand.description,
                    licence=cand.licence))
            session.commit()
        return out

    # ------------------------------------------------------------ reporting
    @staticmethod
    def _limitations(plan: SearchPlan, kept: list[ScoredCandidate],
                     provider_results: list[ProviderResult],
                     duration_excluded: int = 0,
                     max_minutes: float = 15.0) -> list[str]:
        limitations: list[str] = []
        if duration_excluded:
            limitations.append(
                f"{duration_excluded} candidate(s) were excluded for "
                f"exceeding the {max_minutes:.0f}-minute duration cap "
                f"(RESEARCH_MAX_CANDIDATE_MINUTES) — a Short is built "
                f"from short clips, not feature films.")
        if kept:
            mean_relevance = sum(i.relevance for i in kept) / len(kept)
            if mean_relevance < 0.10:
                limitations.append(
                    f"Low mean relevance ({mean_relevance:.2f}): the discovery "
                    f"providers returned items whose titles/descriptions barely "
                    f"mention the topic keywords {plan.keywords}. Treat these "
                    f"candidates as weak and expand the topic or add a provider.")
            if any(i.candidate.licence is None for i in kept):
                limitations.append(
                    "Some candidates have no licence metadata from their provider; "
                    "reuse permission is NOT established by this agent — "
                    "the Rights Verification Agent (Prompt 04) must check them.")
            if any(i.candidate.duration_seconds is None for i in kept):
                limitations.append(
                    "Some candidates have unknown duration (metadata enrichment "
                    "unavailable); they are marked incomplete, not dropped.")
        for result in provider_results:
            if result.status != "ok":
                limitations.append(
                    f"Provider {result.provider} was {result.status} for query "
                    f"{result.query!r}: {result.error or 'no detail'}.")
        return limitations

    @staticmethod
    def _metrics(raw: list[RawCandidate], kept: list[ScoredCandidate],
                 duplicates_removed: int, duration_excluded: int = 0) -> dict:
        total = len(raw)
        return {
            "raw_candidates": total,
            "kept_candidates": len(kept),
            "duplicate_rate": round(duplicates_removed / total, 4) if total else 0.0,
            "duration_excluded": duration_excluded,
            "mean_relevance": round(sum(i.relevance for i in kept) / len(kept), 4) if kept else 0.0,
            "mean_completeness": round(sum(i.completeness for i in kept) / len(kept), 4) if kept else 0.0,
            "mean_reliability": round(sum(i.reliability for i in kept) / len(kept), 4) if kept else 0.0,
            # Traceability: every kept candidate must carry its source
            # identifier and a URL that contains it.
            "traceable": sum(1 for i in kept
                             if i.candidate.external_id and i.candidate.url
                             and i.candidate.external_id in i.candidate.url),
        }
