"""Demo: run the Ranking Agent for an existing job (Prompt 06).

Ranks the job's analysed moments and prints the five selected
moments (#1 best first) with their component scores and
explanations. Fails honestly when fewer than five eligible
sources exist — no moment is invented.

Usage:
    .venv/bin/python scripts/run_ranking.py <job_id>
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from app.agents.ranking.service import RankingService
from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.database.session import init_db, make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Ranking Agent for a job.")
    parser.add_argument("job_id", help="job id with analysed moments (see scripts/run_video_intelligence.py)")
    args = parser.parse_args()
    settings = Settings()
    engine = make_engine(settings)
    init_db(engine)
    service = RankingService(engine, settings)
    try:
        report = service.run(args.job_id)
    except ExternalServiceError as exc:
        print(f"ranking failed (honest failure — cannot select five):\n  {exc}")
        raise SystemExit(1) from exc
    print(f"job {report.job_id}: scorer={report.scorer} metrics={report.metrics}\n")
    for entry in report.ranks:
        print(f"#{entry.rank}  {entry.moment_id}  from {entry.candidate_video_id}  "
              f"score={entry.score_100:.1f}/100")
        print(f"    components: {entry.components}")
        print(f"    {entry.explanation}\n")
    if report.dropped_duplicates:
        print("duplicates dropped:")
        for drop in report.dropped_duplicates:
            print(f"  - {drop['moment_id']}: {drop['reason']}")
    if report.unapproved_skipped:
        print("unapproved moments skipped:")
        for skip in report.unapproved_skipped:
            print(f"  - {skip['moment_id']}: {skip['reason']}")
    if report.limitations:
        print("\nlimitations:")
        for limitation in report.limitations:
            print(f"  - {limitation}")


if __name__ == "__main__":
    main()
