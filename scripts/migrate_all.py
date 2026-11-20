"""Run every migration in order (Prompt 12, Phase 6).

Migrations are idempotent — each one checks whether its change
already exists — so this script is safe to run on every deploy.
It runs scripts/migrate_*.py in numeric order and stops on the
first failure (non-zero exit), so a half-migrated database is
visible immediately instead of silently continuing.

Usage:
    .venv/bin/python scripts/migrate_all.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Numbered migrations only — migrate_all.py itself matches
# "migrate_*.py" and must not be run as a migration.
SCRIPTS = sorted(p for p in (ROOT / "scripts").glob("migrate_*.py")
                 if p.name != "migrate_all.py")


def main() -> int:
    if not SCRIPTS:
        print("no migrations found")
        return 0
    for script in SCRIPTS:
        print(f"== {script.name} ==")
        proc = subprocess.run(
            [sys.executable, str(script)], cwd=str(ROOT),
            capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print(f"FAILED: {script.name} (exit {proc.returncode}) — stopping")
            return proc.returncode
    print(f"all {len(SCRIPTS)} migration(s) applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
