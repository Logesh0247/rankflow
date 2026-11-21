"""Momentrix AI — Quality Assurance Agent demo (Prompt 09).

Usage:
    .venv/bin/python scripts/run_qa.py <job_id>
    .venv/bin/python scripts/run_qa.py --file data/outputs/rankflow-short-demo.mp4

The first form inspects the job's rendered video against its
EDL / ranking / narration / rights records. The second form
inspects a standalone MP4 + manifest (the demo render) — content
checks run from the manifest segments and rights are NOT TESTED,
because there is no job database to read the gate from.

Exit code is 0 for PASSED / NEEDS_REVIEW, 1 for FAILED.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.quality_assurance.service import QAService  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.database.session import init_db, make_engine  # noqa: E402


def _print(report) -> None:
    print(f"verdict: {report.verdict} ({report.status})")
    print(f"file: {report.file_path}")
    for check in report.checks:
        print(f"  [{check['verdict']}] {check['name']}: {check.get('detail')}")
    for issue in report.issues:
        print(f"  ISSUE: {issue}")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    for item in report.not_tested:
        print(f"  NOT TESTED: {item}")
    for request in report.repair_requests:
        print(f"  REPAIR -> {request['responsible_component']} "
              f"({request['target_stage']}): {request['correction']}")
    for rec in report.recommendations:
        print(f"  recommendation: {rec}")


def main(argv: list[str]) -> int:
    settings = get_settings()
    if len(argv) == 3 and argv[1] == "--file":
        engine = make_engine(settings)
        init_db(engine)
        service = QAService(engine, settings)
        path = Path(argv[2])
        manifest_path = path.with_name("rankflow-short-demo-manifest.json")
        if not manifest_path.is_file():
            manifest_path = path.with_suffix(".json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) \
            if manifest_path.is_file() else {}
        report = service.inspect_file(path, manifest=manifest)
        _print(report)
        return 0 if report.verdict in ("pass", "needs_review") else 1
    if len(argv) != 2:
        print(__doc__)
        return 2
    engine = make_engine(settings)
    init_db(engine)
    report = QAService(engine, settings).run(argv[1])
    _print(report)
    return 0 if report.verdict in ("pass", "needs_review") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
