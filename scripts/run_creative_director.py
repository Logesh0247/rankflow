"""Demo: run the Creative Director Agent for an existing job (Prompt 07).

Builds the countdown storyboard, hook, narration, caption plan,
visual direction and Edit Decision List from the job's five ranked
moments. Fails honestly when fewer than five moments are ranked,
or a ranked candidate is no longer Rights-approved — no moment is
invented and rights approval is never bypassed.

Usage:
    .venv/bin/python scripts/run_creative_director.py <job_id>
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from app.agents.creative_director.service import CreativeDirectorService
from app.core.config import Settings
from app.core.errors import ExternalServiceError, RankflowError
from app.database.session import init_db, make_engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Creative Director Agent for a job.")
    parser.add_argument("job_id", help="job id with five ranked moments (see scripts/run_ranking.py)")
    args = parser.parse_args()
    settings = Settings()
    engine = make_engine(settings)
    init_db(engine)
    service = CreativeDirectorService(engine, settings)
    try:
        report = service.run(args.job_id)
    except (ExternalServiceError, RankflowError) as exc:
        print(f"creative director failed (honest failure):\n  {exc}")
        raise SystemExit(1) from exc
    print(f"job {report.job_id}: generator={report.generator} "
          f"storyboard={report.storyboard_id}\n")
    print(f"title: {report.title}")
    print(f"hook ({report.hook['total']:.1f}): {report.hook['text']}")
    print(f"  {report.hook['explanation']}\n")
    print(f"narration ({report.narration_estimated_seconds:.1f}s estimated, "
          f"timeline {report.total_duration_seconds:.1f}s):")
    print(f"  {report.narration_script}\n")
    for scene in report.scenes:
        rank = f"#{scene['rank']}" if scene.get("rank") else scene["kind"]
        print(f"scene {scene['scene_number']} [{rank}] "
              f"{scene['duration_seconds']:.1f}s: {scene['narration']}")
    print("\nEDL clips (#5 -> #1):")
    for clip in report.edl["clips"]:
        print(f"  clip {clip['clip_number']} rank #{clip['rank']}: "
              f"{clip['moment_id']} {clip['start_seconds']}–{clip['end_seconds']}s "
              f"from {clip['candidate_video_id']} ({clip['transition_in']})")
    if report.warnings:
        print("\nwarnings:")
        for warning in report.warnings:
            print(f"  - {warning}")
    if report.limitations:
        print("\nlimitations:")
        for limitation in report.limitations:
            print(f"  - {limitation}")


if __name__ == "__main__":
    main()
