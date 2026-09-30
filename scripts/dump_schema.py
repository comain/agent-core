#!/usr/bin/env python3
"""Regenerate schema.sql from the schema the code actually creates.

    python scripts/dump_schema.py

The file is a generated artifact, not the source of truth: it exists so the
schema can be read and diffed between releases without stepping through the
Python that builds it. A test fails if it drifts from the code.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_core.db import dump_schema  # noqa: E402
from agent_core.runtime.schema import apply_schema  # noqa: E402

TARGET = ROOT / "schema.sql"


def current_schema() -> str:
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        return dump_schema(conn, prefix="ac_")
    finally:
        conn.close()


if __name__ == "__main__":
    TARGET.write_text(current_schema(), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
