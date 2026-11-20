"""RANKFLOW AI — Video Production demo (Prompt 08).

Renders the approved Edit Decision List for one job to a real
1080x1920 MP4 and prints the render report + manifest path.

Usage:
  .venv/bin/python scripts/run_production.py <job_id>
  .venv/bin/python scripts/run_production.py <job_id> \
      --footage <candidate_id>=<path> --footage <candidate_id>=<path>

Footage defaults to each candidate's ApprovedFootage.storage_path;
use --footage to supply a local file instead. A job needs a
Creative Director plan first (scripts/run_creative_director.py),
and every clip's candidate must still be Rights-approved.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agents.production.service import ProductionService  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.database.session import init_db, make_engine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a job's EDL to a 1080x1920 MP4")
    parser.add_argument("job_id")
    parser.add_argument("--footage", action="append", default=[],
                        help="candidate_id=path (repeatable)")
    args = parser.parse_args()
    footage = {}
    for item in args.footage:
        candidate_id, _, path = item.partition("=")
        if not candidate_id or not path:
            parser.error(f"--footage must be candidate_id=path, got {item!r}")
        footage[candidate_id] = path
    settings = get_settings()
    engine = make_engine(settings)
    init_db(engine)
    report = ProductionService(engine, settings).run(args.job_id, footage_paths=footage)
    print(f"job: {report.job_id}")
    print(f"rendered video: {report.rendered_video_id}")
    print(f"file: {report.file_path}")
    print(f"duration: {report.duration_seconds:.2f}s  {report.width}x{report.height} "
          f"@{report.fps}fps  {report.video_codec}/{report.audio_codec}")
    print(f"segments: {len(report.segments)} "
          f"({', '.join(s.kind for s in report.segments)})")
    print(f"narration: {report.narration['status']} "
          f"({report.narration['estimated_seconds']:.1f}s estimated, "
          f"{report.narration['script_words']} words)")
    print(f"music: {report.music.status}"
          + (f" ({report.music.file_name})" if report.music.file_name else ""))
    print(f"sfx: chime={report.sfx_chime.status}, whoosh={report.sfx_whoosh.status} "
          f"(mixed: {report.sfx_mixed})")
    print(f"captions: {report.captions['lines']} lines")
    print(f"validation: {'valid' if report.validation.get('valid') else 'INVALID'}")
    print(f"manifest: {report.manifest_path}")
    for warning in report.warnings:
        print(f"warning: {warning}")
    for limitation in report.limitations:
        print(f"limitation: {limitation}")
    print(json.dumps(report.metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
