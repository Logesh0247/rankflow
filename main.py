"""RANKFLOW AI entrypoint (Prompt 08).

FastAPI app: health check, job creation/listing, orchestrator runs,
the Research Agent (Prompt 03), the Rights Verification Agent
(Prompt 04), the Video Intelligence Agent (Prompt 05), the Ranking
Agent (Prompt 06), the Creative Director Agent (Prompt 07), the
Video Production Agent (Prompt 08) and the Quality Assurance Agent
(Prompt 09) — all seven real agents. A plain job run now runs the
full pipeline to COMPLETED (when five eligible sources with
analysed moments exist — otherwise the ranking stage fails the job
honestly); a QA FAILED verdict fails the job at QUALITY_ASSURANCE,
and a QA NEEDS_REVIEW verdict completes the job to
awaiting_approval with the QA report flagging what is missing.
Run:  .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import creative_director as creative_director_routes
from app.api import cost as cost_routes
from app.api import health as health_routes
from app.api import jobs as jobs_routes
from app.api import orchestrator as orchestrator_routes
from app.api import production as production_routes
from app.api import quality_assurance as quality_assurance_routes
from app.api import ranking as ranking_routes
from app.api import research as research_routes
from app.api import review as review_routes
from app.api import rights as rights_routes
from app.api import system_status as system_status_routes
from app.api import video_intelligence as video_intelligence_routes
from app.core.config import get_settings
from app.core.errors import RankflowError
from app.core.logging import configure_logging, get_logger
from app.core.ratelimit import RateLimiter, RateLimitMiddleware
from app.database.session import init_db

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger("startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Prompt 12, Phase 4: validate storage at startup — warn, never
    # refuse to boot (a missing render file must not take the API down).
    try:
        from app.database.session import make_engine
        from app.storage.validate import validate_storage
        report = validate_storage(make_engine(), settings)
        if not report["ok"]:
            for issue in report["issues"]:
                logger.warning("storage validation: %s", issue)
        else:
            logger.info("storage validation ok: %s", report["checks"])
    except Exception as exc:  # noqa: BLE001 — startup must not fail on this
        logger.warning("storage validation could not run: %s", exc)
    yield


app = FastAPI(title=settings.app_name, version="0.10.0", debug=settings.app_debug, lifespan=lifespan)
# Prompt 12, Phase 5: per-IP rate limiting (600/min by default).
app.add_middleware(RateLimitMiddleware, limiter=RateLimiter(settings.rate_limit_per_minute))
app.include_router(health_routes.router)
app.include_router(jobs_routes.router)
app.include_router(orchestrator_routes.router)
app.include_router(research_routes.router)
app.include_router(rights_routes.router)
app.include_router(video_intelligence_routes.router)
app.include_router(ranking_routes.router)
app.include_router(creative_director_routes.router)
app.include_router(cost_routes.router)
app.include_router(production_routes.router)
app.include_router(quality_assurance_routes.router)
app.include_router(review_routes.router)
app.include_router(system_status_routes.router)

# Prompt 11 UI — the RANKFLOW space, served same-origin so it talks
# to this real backend (no separate host, no simulated data).
from fastapi.staticfiles import StaticFiles
from pathlib import Path

app.mount("/ui", StaticFiles(directory=Path(__file__).resolve().parent / "app" / "ui", html=True), name="ui")


@app.exception_handler(RankflowError)
async def _rankflow_error(request: Request, exc: RankflowError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "message": exc.message})


@app.get("/")
def root() -> dict:
    return {"app": settings.app_name, "version": "0.10.0",
            "status": "all seven agents ready — research + rights + video_intelligence + ranking + creative_director + production + quality_assurance, see README.md"}
