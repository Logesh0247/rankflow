"""Demo: run the Video Intelligence Agent for an existing job (Prompt 05).

Analyses every Rights-approved candidate of the job that has a
local footage file. Footage is resolved from ApprovedFootage
storage_path, or supplied here as candidate_id=path pairs.

Usage:
    .venv/bin/python scripts/run_video_intelligence.py <job_id>
    .venv/bin/python scripts/run_video_intelligence.py <job_id> \\
        --footage <candidate_id>=/path/to/video.mp4
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from app.agents.video_intelligence.service import VideoIntelligenceService
from app.core.config import Settings
from app.core.errors import ExternalServiceError
from app.database.session import init_db, make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Video Intelligence Agent for a job.")
    parser.add_argument("job_id", help="job id with approved candidates (see scripts/run_rights.py)")
    parser.add_argument("--footage", action="append", default=[],
                        help="candidate_id=/path/to/video.mp4 (repeatable)")
    args = parser.parse_args()
    footage = {}
    for item in args.footage:
        candidate_id, _, path = item.partition("=")
        if not candidate_id or not path:
            parser.error(f"--footage must be candidate_id=path, got {item!r}")
        footage[candidate_id] = Path(path)
    settings = Settings()
    engine = make_engine(settings)
    init_db(engine)
    service = VideoIntelligenceService(engine, settings)
    try:
        report = service.run(args.job_id, footage_paths=footage or None)
    except ExternalServiceError as exc:
        print(f"video intelligence failed (honest failure — no usable moments):\n  {exc}")
        raise SystemExit(1) from exc
    print(f"job {report.job_id}: analyser={report.analyser} metrics={report.metrics}\n")
    for cand in report.candidates:
        print(f"[{cand.status}] {cand.candidate_video_id}")
        if cand.probe:
            p = cand.probe
            print(f"  probe: {p.duration_seconds:.1f}s {p.width}x{p.height} "
                  f"{p.fps:.1f}fps {p.video_codec} audio={p.has_audio}")
        for moment_id in cand.moment_ids:
            moment = service.get_moment(report.job_id, moment_id)
            print(f"  moment {moment.start_seconds:.1f}–{moment.end_seconds:.1f}s "
                  f"confidence={moment.score} "
                  f"visual={moment.labels.get('visual_quality')} "
                  f"context={moment.labels.get('context_clarity')}")
            print(f"    {moment.description}")
            print(f"    thumbnail: {moment.labels.get('thumbnail')}")
        if cand.reason:
            print(f"  skipped: {cand.reason}")
    if report.limitations:
        print("\nlimitations:")
        for limitation in report.limitations:
            print(f"  - {limitation}")


if __name__ == "__main__":
    main()
