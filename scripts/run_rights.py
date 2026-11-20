"""Demo: run the Rights Verification Agent for an existing job (Prompt 04).

Verifies every candidate the Research Agent persisted for a job
and prints the verdicts with reasons, the candidate statuses and
the attribution for approved candidates. Does NOT run any other
agent — a job with zero approved candidates fails honestly here
(the service raises), which is the correct outcome: no unverified
source is approved merely to keep production moving.

Usage:
    .venv/bin/python scripts/run_rights.py <job_id>
    .venv/bin/python scripts/run_rights.py <job_id> --no-ia-fetch
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from app.agents.rights.service import RightsService
from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.database.session import init_db, make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Rights Verification Agent for a job.")
    parser.add_argument("job_id", help="job id with Research candidates (see scripts/run_research.py)")
    parser.add_argument("--no-ia-fetch", action="store_true",
                        help="do not re-check the Internet Archive metadata API for missing licences")
    args = parser.parse_args()

    settings = Settings(rights_fetch_ia_metadata=not args.no_ia_fetch)
    engine = make_engine(settings)
    init_db(engine)
    service = RightsService(engine, settings)
    try:
        report = service.run(args.job_id)
    except ExternalServiceError as exc:
        print(f"rights failed (honest failure — nothing was approved):\n  {exc}")
        raise SystemExit(1) from exc
    print(f"job {report.job_id}: {report.metrics}\n")
    for group in (report.approved, report.rejected, report.needs_review, report.expired):
        for item in group:
            print(f"[{item.verdict}] {item.candidate_video_id}")
            print(f"  licence: {item.license_name or item.licence or 'none'}")
            for reason in item.reasons:
                print(f"  - {reason}")
            if item.attribution_text:
                print(f"  attribution: {item.attribution_text}")
    print("\nattribution for approved candidates (for Creative Director / Production):")
    for entry in service.list_attribution(args.job_id):
        print(f"  - {entry.candidate_video_id}: required={entry.attribution_required} "
              f"{entry.attribution_text or ''}")
    if report.limitations:
        print("limitations:")
        for limitation in report.limitations:
            print(f"  - {limitation}")


if __name__ == "__main__":
    main()
