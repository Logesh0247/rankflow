"""RANKFLOW AI — cost monitoring (Prompt 12, Phase 3).

Every unit of work a job consumes is recorded as a CostEvent and
priced with the explicit rates below. Nothing is assumed free
except what is actually free:

* Compute — wall-clock seconds each orchestrator stage runs, priced
  at COMPUTE_USD_PER_CPU_HOUR. The rate is a documented planning
  assumption for a small cloud host (see docs/DEPLOYMENT.md); the
  seconds are measured, the dollars are an estimate.
* External API calls — one event per provider search. Internet
  Archive searches are free ($0.00); YouTube Data API calls are
  free inside the daily quota ($0.00, quota-limited, not
  unlimited). A provider that starts charging must set its
  unit cost here — a call is never silently priced at zero.
* TTS / narration audio — $0.00 because no TTS provider is
  configured and no narration audio is generated. This is an
  absent feature, not a free one: the moment a TTS provider is
  configured, its per-character rate goes in TTS_USD_PER_1000_CHARS
  and events are recorded by the narration step.
* Licensed music / SFX — $0.00, same reason: no licensed library
  is configured, so no licensed audio is used.
* Storage — measured live from the files a job actually owns
  (rendered MP4 + thumbnails + segments), priced at
  STORAGE_USD_PER_GB_MONTH. Storage is reported, not stored as
  events, because files come and go.

Cost recording must never break a job: every record() call is
guarded by the caller (engine / research service) with
try/except, and record() itself swallows storage errors.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.models import CostEvent

logger = get_logger("cost")

# --- Rates (documented planning assumptions; see docs/DEPLOYMENT.md) ---
COMPUTE_USD_PER_CPU_HOUR = 0.10  # small 2-vCPU cloud host, ~$0.05/vCPU-hour
STORAGE_USD_PER_GB_MONTH = 0.023  # object-storage list price
TTS_USD_PER_1000_CHARS = 0.0  # NO TTS PROVIDER CONFIGURED — see module docstring
MUSIC_SFX_USD_PER_JOB = 0.0  # NO LICENSED AUDIO LIBRARY CONFIGURED

# External API unit costs ($ per call). Internet Archive is free;
# YouTube Data API is free inside its daily quota.
API_UNIT_COST_USD = {
    "internet_archive": 0.0,
    "youtube": 0.0,
    "test": 0.0,
}


class CostEventRead(BaseModel):
    id: str
    job_id: str
    category: str
    label: str
    quantity: float
    unit: str
    unit_cost_usd: float
    amount_usd: float
    created_at: str


class JobCostSummary(BaseModel):
    job_id: str
    compute_seconds: float = 0.0
    compute_usd: float = 0.0
    api_calls: int = 0
    api_usd: float = 0.0
    tts_chars: int = 0
    tts_usd: float = 0.0
    storage_bytes: int = 0
    storage_usd_per_month: float = 0.0
    total_usd: float = 0.0
    events: list[CostEventRead] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CostService:
    def __init__(self, engine: Engine, settings: Settings | None = None):
        self._engine = engine
        self.settings = settings or get_settings()
        self._sessions = sessionmaker(bind=engine)

    def record(self, job_id: str, *, category: str, label: str,
               quantity: float, unit: str, unit_cost_usd: float) -> None:
        """Persist one cost event. Never raises (cost must not break jobs)."""
        try:
            with self._sessions() as session:
                session.add(CostEvent(
                    job_id=job_id, category=category, label=label,
                    quantity=float(quantity), unit=unit,
                    unit_cost_usd=float(unit_cost_usd),
                    amount_usd=round(float(quantity) * float(unit_cost_usd), 6)))
                session.commit()
        except Exception as exc:  # noqa: BLE001 — cost recording is best-effort
            logger.warning("cost event not recorded for job %s: %s", job_id, exc)

    def record_compute(self, job_id: str, stage: str, seconds: float) -> None:
        self.record(job_id, category="compute",
                    label=f"stage {stage}",
                    quantity=round(float(seconds), 3), unit="cpu_seconds",
                    unit_cost_usd=COMPUTE_USD_PER_CPU_HOUR / 3600.0)

    def record_api_call(self, job_id: str, provider: str, query: str) -> None:
        self.record(job_id, category="external_api",
                    label=f"{provider} search: {query[:80]}",
                    quantity=1.0, unit="calls",
                    unit_cost_usd=API_UNIT_COST_USD.get(provider, 0.0))

    def job_cost(self, job_id: str) -> JobCostSummary:
        """Cost of one job from its recorded events + live storage size."""
        from app.models.models import RenderedVideo

        with self._sessions() as session:
            rows = (session.query(CostEvent)
                    .filter(CostEvent.job_id == job_id)
                    .order_by(CostEvent.created_at.asc()).all())
            events = [CostEventRead(
                id=r.id, job_id=r.job_id, category=r.category, label=r.label,
                quantity=r.quantity, unit=r.unit,
                unit_cost_usd=r.unit_cost_usd, amount_usd=r.amount_usd,
                created_at=r.created_at.isoformat()) for r in rows]
            rendered_paths = [r.file_path for r in session.query(RenderedVideo)
                              .filter(RenderedVideo.job_id == job_id).all()
                              if r.file_path]
        compute_seconds = sum(e.quantity for e in events if e.category == "compute")
        compute_usd = sum(e.amount_usd for e in events if e.category == "compute")
        api_calls = sum(int(e.quantity) for e in events if e.category == "external_api")
        api_usd = sum(e.amount_usd for e in events if e.category == "external_api")
        tts_chars = sum(int(e.quantity) for e in events if e.category == "tts")
        tts_usd = sum(e.amount_usd for e in events if e.category == "tts")
        storage_bytes = 0
        job_dir = Path(self.settings.storage_root) / "jobs" / job_id
        if job_dir.is_dir():
            # Everything the job owns: segments, thumbnails, render.
            for path in job_dir.rglob("*"):
                try:
                    if path.is_file():
                        storage_bytes += path.stat().st_size
                except OSError:
                    continue
        else:
            # Fall back to the rendered video rows when the job
            # directory is gone (e.g. storage was pruned).
            for p in rendered_paths:
                try:
                    path = Path(p)
                    if path.is_file():
                        storage_bytes += path.stat().st_size
                except OSError:
                    continue
        storage_usd = storage_bytes / (1024 ** 3) * STORAGE_USD_PER_GB_MONTH
        notes = [
            "Compute dollars are an estimate: seconds are measured, "
            f"priced at ${COMPUTE_USD_PER_CPU_HOUR:.2f}/CPU-hour (see docs/DEPLOYMENT.md).",
        ]
        if tts_chars == 0:
            notes.append("TTS cost is $0 because no TTS provider is configured — "
                         "narration audio is not generated, not free.")
        return JobCostSummary(
            job_id=job_id, compute_seconds=round(compute_seconds, 2),
            compute_usd=round(compute_usd, 4), api_calls=api_calls,
            api_usd=round(api_usd, 4), tts_chars=tts_chars,
            tts_usd=round(tts_usd, 4), storage_bytes=storage_bytes,
            storage_usd_per_month=round(storage_usd, 4),
            total_usd=round(compute_usd + api_usd + tts_usd, 4),
            events=events, notes=notes)

    def summary(self) -> dict:
        """Totals across all jobs (events only; storage is per-job)."""
        with self._sessions() as session:
            rows = session.query(CostEvent).all()
        total_compute = sum(r.quantity for r in rows if r.category == "compute")
        return {
            "jobs_with_cost_events": len({r.job_id for r in rows}),
            "total_events": len(rows),
            "total_compute_seconds": round(total_compute, 2),
            "total_compute_usd": round(sum(r.amount_usd for r in rows if r.category == "compute"), 4),
            "total_api_calls": sum(int(r.quantity) for r in rows if r.category == "external_api"),
            "total_api_usd": round(sum(r.amount_usd for r in rows if r.category == "external_api"), 4),
            "total_tts_usd": round(sum(r.amount_usd for r in rows if r.category == "tts"), 4),
            "total_usd": round(sum(r.amount_usd for r in rows), 4),
            "rates": {
                "compute_usd_per_cpu_hour": COMPUTE_USD_PER_CPU_HOUR,
                "storage_usd_per_gb_month": STORAGE_USD_PER_GB_MONTH,
                "tts_usd_per_1000_chars": TTS_USD_PER_1000_CHARS,
            },
            "notes": [
                "No external API spend has been recorded: Internet Archive "
                "searches are free and no paid provider is configured.",
                "TTS and licensed audio cost $0 because those features are "
                "absent (no provider / no library), not because they are free.",
            ],
        }
