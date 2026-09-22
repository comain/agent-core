"""Ordered, once-only schema migrations."""

from __future__ import annotations

import sqlite3

import pytest

from agent_core.db import Migration, applied_migrations, apply_migrations, dump_schema

LEDGER = "ac_schema_migrations"


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY)")
    yield connection
    connection.close()


def test_migrations_run_in_the_order_given(conn):
    """Order is the whole point: a repair may have to precede a constraint."""
    order = []
    steps = [
        Migration("first", lambda c: order.append("first")),
        Migration("second", lambda c: order.append("second")),
        Migration("third", lambda c: order.append("third")),
    ]
    apply_migrations(conn, steps, ledger_table=LEDGER)
    assert order == ["first", "second", "third"]


def test_a_migration_runs_once_however_often_the_service_starts(conn):
    runs = []
    steps = [Migration("count", lambda c: runs.append(1))]

    for _ in range(3):
        apply_migrations(conn, steps, ledger_table=LEDGER)

    assert runs == [1], "this is what the old per-startup repair statements could not do"


def test_only_the_new_steps_run_when_a_migration_is_added(conn):
    ran = []
    first = [Migration("a", lambda c: ran.append("a"))]
    apply_migrations(conn, first, ledger_table=LEDGER)

    second = first + [Migration("b", lambda c: ran.append("b"))]
    applied = apply_migrations(conn, second, ledger_table=LEDGER)

    assert applied == ["b"]
    assert ran == ["a", "b"]


def test_adopting_the_ledger_on_an_existing_database_runs_every_step_once(conn):
    """Adoption does not assume an existing database is already current.

    Guessing that a step has already run is how one gets silently skipped, so
    every step runs once and is recorded. The steps written so far are
    idempotent, which is what makes that safe.
    """
    conn.execute("ALTER TABLE widgets ADD COLUMN colour TEXT")  # as an older release left it

    def add_colour(c):
        columns = {r[1] for r in c.execute("PRAGMA table_info(widgets)")}
        if "colour" not in columns:
            c.execute("ALTER TABLE widgets ADD COLUMN colour TEXT")

    applied = apply_migrations(conn, [Migration("add_colour", add_colour)], ledger_table=LEDGER)

    assert applied == ["add_colour"]
    assert applied_migrations(conn, ledger_table=LEDGER) == ["add_colour"]


def test_a_failed_migration_is_not_recorded_and_leaves_nothing_behind(conn):
    def half_finished(c):
        c.execute("ALTER TABLE widgets ADD COLUMN half TEXT")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        apply_migrations(conn, [Migration("half", half_finished)], ledger_table=LEDGER)

    columns = {r[1] for r in conn.execute("PRAGMA table_info(widgets)")}
    assert "half" not in columns, "the partial change must roll back"
    assert applied_migrations(conn, ledger_table=LEDGER) == [], "and must not be recorded as done"


def test_a_failed_migration_is_retried_on_the_next_start(conn):
    attempts = []

    def flaky(c):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("first attempt fails")
        c.execute("ALTER TABLE widgets ADD COLUMN eventually TEXT")

    steps = [Migration("flaky", flaky)]
    with pytest.raises(RuntimeError):
        apply_migrations(conn, steps, ledger_table=LEDGER)
    apply_migrations(conn, steps, ledger_table=LEDGER)

    assert len(attempts) == 2
    assert "eventually" in {r[1] for r in conn.execute("PRAGMA table_info(widgets)")}


def test_a_later_step_still_runs_after_an_earlier_one_failed(conn):
    """A failure must not be recorded as success and skip the rest silently."""
    with pytest.raises(RuntimeError):
        apply_migrations(
            conn,
            [
                Migration("ok", lambda c: c.execute("ALTER TABLE widgets ADD COLUMN one TEXT")),
                Migration("bad", lambda c: (_ for _ in ()).throw(RuntimeError("no"))),
            ],
            ledger_table=LEDGER,
        )
    assert applied_migrations(conn, ledger_table=LEDGER) == ["ok"]


def test_duplicate_migration_ids_are_refused(conn):
    """Two steps sharing an id means one of them silently never runs."""
    with pytest.raises(ValueError, match="duplicate migration ids"):
        apply_migrations(
            conn,
            [Migration("same", lambda c: None), Migration("same", lambda c: None)],
            ledger_table=LEDGER,
        )


def test_a_ledger_name_that_is_not_an_identifier_is_refused(conn):
    """The name is interpolated into DDL, so it cannot be arbitrary text."""
    with pytest.raises(ValueError, match="not a usable table name"):
        apply_migrations(conn, [], ledger_table="x; DROP TABLE widgets")


# -- the schema dump -------------------------------------------------------------


def test_the_dump_is_the_schema_sqlite_actually_has(conn):
    conn.execute("CREATE INDEX ix_widgets ON widgets(id)")
    dump = dump_schema(conn)
    assert "CREATE TABLE widgets" in dump
    assert "CREATE INDEX ix_widgets" in dump


def test_the_dump_is_stable_across_runs(conn):
    """A file that reorders itself is a file nobody can review by diff."""
    conn.execute("CREATE TABLE zebra (id INTEGER)")
    conn.execute("CREATE TABLE alpha (id INTEGER)")
    assert dump_schema(conn) == dump_schema(conn)
    assert dump_schema(conn).index("alpha") < dump_schema(conn).index("zebra")


def test_tables_are_listed_before_their_indexes(conn):
    conn.execute("CREATE INDEX ix_widgets ON widgets(id)")
    dump = dump_schema(conn)
    assert dump.index("CREATE TABLE widgets") < dump.index("CREATE INDEX ix_widgets")


def test_a_prefix_restricts_the_dump_to_one_namespace(conn):
    """One SQLite file holds both a product's tables and the core's."""
    conn.execute("CREATE TABLE ac_thing (id INTEGER)")
    dump = dump_schema(conn, prefix="ac_")
    assert "ac_thing" in dump
    assert "widgets" not in dump


def test_sqlite_internal_tables_are_left_out(conn):
    conn.execute("CREATE TABLE auto (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
    assert "sqlite_" not in dump_schema(conn)


# -- the committed schema.sql ----------------------------------------------------


def test_the_committed_schema_file_matches_the_code():
    """schema.sql is generated; a stale one is worse than none.

    If this fails the schema changed without the artifact being regenerated:
    run `python scripts/dump_schema.py`.
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'scripts'); "
         "from dump_schema import current_schema; print(current_schema(), end='')"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    committed = (root / "schema.sql").read_text(encoding="utf-8")
    assert result.stdout == committed, "schema.sql is stale; run python scripts/dump_schema.py"


def test_the_migration_ledger_is_part_of_the_documented_schema():
    """It is a real table on every deployed database, so it belongs in the file."""
    from pathlib import Path

    schema = (Path(__file__).resolve().parent.parent / "schema.sql").read_text(encoding="utf-8")
    assert "ac_schema_migrations" in schema


def test_schema_applies_to_a_database_created_before_the_columns_existed():
    """The failure this exists for: an index in the DDL referenced a column a
    migration had not added yet, and DDL runs first -- so every deployed
    database refused to open while a fresh one was fine."""
    import sqlite3
    import tempfile
    from pathlib import Path

    from agent_core.runtime.schema import apply_schema

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old.db"
        conn = sqlite3.connect(str(path))
        # The events table as an earlier release created it.
        conn.execute(
            """
            CREATE TABLE ac_task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_ref TEXT NOT NULL,
                seq INTEGER,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                stage TEXT,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO ac_task_events (task_ref, event_type, message, created_at)"
            " VALUES ('t', 'agent_progress', 'before the upgrade', '2026-01-01T00:00:00Z')"
        )
        conn.commit()

        apply_schema(conn)
        conn.commit()

        columns = {r[1] for r in conn.execute("PRAGMA table_info(ac_task_events)")}
        assert {"source", "call_id"} <= columns
        row = conn.execute("SELECT message, source, call_id FROM ac_task_events").fetchone()
        assert row == ("before the upgrade", "agent", None)
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(ac_task_events)")}
        assert "ix_ac_events_call" in indexes
        conn.close()
