"""SQLite schema tooling: ordered migrations, and a readable schema dump.

Both consuming services grew the same two problems. Each created a
`schema_version` table, wrote `1` into it, and never read it again -- a
ledger that recorded nothing -- while the actual migrations were a handful of
`ADD COLUMN IF MISSING` calls and some repair statements re-executed on every
single startup, because nothing remembered they had run.

That works for exactly the migrations they had: additive, idempotent, and
order-independent. It cannot express a backfill that must run once, a column
drop, or a repair that must happen before a constraint is added. The first
migration needing any of those has nowhere to go.

`apply_migrations` gives them somewhere: a list of named steps, applied in
order, recorded once.

## Adopting this on a database that already exists

The ledger starts empty even for a database that is already fully migrated,
so on first adoption every step runs once. That is safe **because every step
written so far is idempotent** -- guarded `ALTER TABLE`, and repairs that are
no-ops once the data is clean. It is deliberately not a "assume an existing
database is current" baseline: guessing that a step has already run is how a
migration gets silently skipped. Steps run, get recorded, and never run again.

A step added later does not have to be idempotent. That is the point.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable, List, Sequence


@dataclass(frozen=True)
class Migration:
    """One schema change, applied at most once per database.

    ``id`` is written to the ledger, so it must never change once released --
    renaming one makes every database run the step a second time.
    """

    id: str
    apply: Callable[[sqlite3.Connection], None]


def apply_migrations(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration],
    *,
    ledger_table: str,
) -> List[str]:
    """Apply every migration not yet recorded, in order. Returns what ran.

    Each step and its ledger entry share a SAVEPOINT: a step that raises
    leaves neither its own changes nor a record claiming it succeeded, so the
    next start retries it rather than skipping it.
    """
    _assert_identifier(ledger_table)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {ledger_table} (
            id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )

    seen = {row[0] for row in conn.execute(f"SELECT id FROM {ledger_table}")}
    duplicates = _duplicates(m.id for m in migrations)
    if duplicates:
        raise ValueError(f"duplicate migration ids: {sorted(duplicates)}")

    applied: List[str] = []
    for migration in migrations:
        if migration.id in seen:
            continue
        conn.execute("SAVEPOINT agent_core_migration")
        try:
            migration.apply(conn)
            conn.execute(
                f"INSERT INTO {ledger_table}(id, applied_at) VALUES (?, datetime('now'))",
                (migration.id,),
            )
        except Exception:
            conn.execute("ROLLBACK TO agent_core_migration")
            conn.execute("RELEASE agent_core_migration")
            raise
        conn.execute("RELEASE agent_core_migration")
        applied.append(migration.id)
    return applied


def applied_migrations(conn: sqlite3.Connection, *, ledger_table: str) -> List[str]:
    """What this database has already run, oldest first."""
    _assert_identifier(ledger_table)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if ledger_table not in tables:
        return []
    return [row[0] for row in conn.execute(f"SELECT id FROM {ledger_table} ORDER BY applied_at, id")]


def dump_schema(conn: sqlite3.Connection, *, prefix: str = "") -> str:
    """The schema as DDL text, ordered so two dumps are comparable.

    This is what makes the schema reviewable: a file that can be diffed
    between releases and read without stepping through the Python that builds
    it. It is generated, never the source of truth -- SQLite is asked what it
    actually has, so the dump cannot drift from the database the code creates.

    ``prefix`` restricts the dump to one namespace, for a file shared between
    a product's tables and the core's.
    """
    rows = conn.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()

    kept = [r for r in rows if not prefix or str(r[2]).startswith(prefix)]
    # Tables first, then their indexes; alphabetical within each so the file
    # only changes when the schema does.
    order = {"table": 0, "view": 1, "trigger": 2, "index": 3}
    kept.sort(key=lambda r: (order.get(str(r[0]), 9), str(r[1])))

    lines = [
        "-- Generated from the live schema; do not edit.",
        "-- Regenerate with: python scripts/dump_schema.py",
        "",
    ]
    for kind, name, _tbl, sql in kept:
        lines.append(f"{_normalize(sql)};")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _normalize(sql: str) -> str:
    """Collapse the incidental whitespace SQLite preserves from the source."""
    return "\n".join(line.rstrip() for line in str(sql).strip().splitlines())


def _duplicates(values: Iterable[str]) -> set:
    seen, dupes = set(), set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return dupes


def _assert_identifier(name: str) -> None:
    """Table names are interpolated, not bound; refuse anything but a plain name."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"not a usable table name: {name!r}")
