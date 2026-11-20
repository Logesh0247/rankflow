"""Demo: run the Research Agent for one topic (Prompt 03).

Creates a job, runs research (real Internet Archive provider by
default), and prints the search plan, the candidates with their
scores, provider outcomes, metrics and limitations. No other agent
runs — rights, ranking and the rest do not exist yet.

Usage:
    .venv/bin/python scripts/run_research.py "TOP 5 CRAZIEST PARKOUR FAILS"
    .venv/bin/python scripts/run_research.py "funny cats" --provider test
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from app.agents.research.service import ResearchService
from app.core.config import Settings
from app.database.session import init_db, make_engine
from app.orchestrator.jobs import JobService
from app.schemas.schemas import JobCreate


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Research Agent for one topic.")
    parser.add_argument("topic", nargs="?", default="TOP 5 CRAZIEST PARKOUR FAILS")
    parser.add_argument("--provider", default=None, help="internet_archive | youtube | test")
    args = parser.parse_args()

    settings = Settings(research_provider=args.provider) if args.provider else Settings()
    engine = make_engine(settings)
    init_db(engine)
    service = JobService(engine)
    job = service.create_job(JobCreate(topic=args.topic))
    print(f"job {job.id} topic={job.topic!r}\n")

    report = ResearchService(engine, settings).run(job.id)
    plan = report.plan
    print(f"category: {plan.category}  content_type: {plan.content_type}")
    print(f"keywords: {plan.keywords}")
    print(f"expanded: {plan.expanded_keywords}")
    print(f"related:  {plan.related_concepts}  events: {plan.events}")
    print(f"queries:  {[q.text for q in plan.queries]}\n")
    for cand in report.candidates:
        print(f"- {cand.title}  (relevance {cand.relevance_score})")
        print(f"  creator={cand.creator}  duration={cand.duration_seconds}s  "
              f"views={cand.view_count}  licence={cand.licence}")
        print(f"  {cand.url}")
    print(f"\nmetrics: {report.metrics}")
    print(f"duplicates removed: {report.duplicates_removed}")
    print(f"provider results: {[(r.provider, r.status, r.returned) for r in report.provider_results]}")
    if report.limitations:
        print("limitations:")
        for limitation in report.limitations:
            print(f"  - {limitation}")


if __name__ == "__main__":
    main()
