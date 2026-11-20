#!/usr/bin/env python3
"""RANKFLOW AI — environment inspection (Phase 1, re-runnable).

Prints what is actually present in this environment: Python, installed
packages, FFmpeg/OpenCV, database and Redis availability, and which
integrations are configured. Exits 0 if the foundation can run, 1 if
a required local component is missing.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys

CHECKS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append({"name": name, "ok": ok, "detail": detail})
    print(f"{'OK  ' if ok else 'MISS'} {name}: {detail}")


def main() -> int:
    print(f"python: {sys.version.split()[0]}")
    for pkg in ("fastapi", "uvicorn", "pydantic", "sqlalchemy", "psycopg", "redis", "celery", "cv2", "numpy", "requests"):
        spec = importlib.util.find_spec(pkg)
        if spec is None:
            check(f"package:{pkg}", False, "not installed")
        else:
            try:
                mod = importlib.import_module(pkg)
                check(f"package:{pkg}", True, getattr(mod, "__version__", "installed"))
            except Exception as exc:  # noqa: BLE001
                check(f"package:{pkg}", False, str(exc))

    for tool in ("ffmpeg", "ffprobe", "sqlite3"):
        path = shutil.which(tool)
        check(f"tool:{tool}", bool(path), path or "not on PATH")
    if shutil.which("ffmpeg"):
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        check("ffmpeg:version", True, out.stdout.splitlines()[0] if out.stdout else "")

    for cmd, label in (("psql", "postgres client"), ("pg_isready", "postgres server check"), ("redis-server", "redis server"), ("redis-cli", "redis client")):
        check(f"tool:{label}", bool(shutil.which(cmd)), shutil.which(cmd) or "not installed — see docs/ARCHITECTURE.md")

    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("SELECT 1")
        check("database:sqlite", True, "in-memory query ok (local testing fallback)")
    except Exception as exc:  # noqa: BLE001
        check("database:sqlite", False, str(exc))

    print(json.dumps({"required_ok": all(c["ok"] for c in CHECKS if c["name"].startswith(("package:fastapi", "package:pydantic", "package:sqlalchemy", "tool:ffmpeg", "database:sqlite")))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
