#!/usr/bin/env python3
"""Initialise the RANKFLOW AI database (creates all tables)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database.session import check_db, init_db

if __name__ == "__main__":
    engine = init_db()
    print(f"database initialised: {engine.url}")
    print(f"connection check: {'ok' if check_db(engine) else 'FAILED'}")
