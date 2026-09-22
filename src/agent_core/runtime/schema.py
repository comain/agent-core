"""Canonical runtime schema.

Tables are namespaced ``ac_*`` so they can share a SQLite file with a product's
existing tables without collision, and so "has this product adopted the core
runtime?" is answerable by looking at table names.

Two deliberate absences:

**No task table.** Each consuming product has its own, and they are mutually
incompatible -- one uses a TEXT key, another an INTEGER key, another a parent /
child pair of keys. Work is referenced here by an opaque ``task_ref`` string
which the product supplies. There is consequently no foreign key from these
tables into product tables; an orphaned ``task_ref`` is possible and accepted,
because a foreign key is precisely what cannot be expressed across three
incompatible key types.

**No workflow state.** The workflow engine persists that itself. What lives
here is the *gate record* -- what was asked, of whom, and what came back.

See ADR-004 in the dev-flow-agent repo.
"""

from __future__ import annotations

import sqlite3

from agent_core.db import Migration, apply_migrations


DDL = (
    # ---- events ---------------------------------------------------------
    # Append-only. The `id` is the cursor an SSE stream reads from, which is
    # why it must be monotonic and must never be reused.
    """
    CREATE TABLE IF NOT EXISTS ac_task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_ref TEXT NOT NULL,
        seq INTEGER,
        event_type TEXT NOT NULL,
        severity TEXT NOT NULL DEFAULT 'info',
        stage TEXT,
        message TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        -- Who the event is attributed to: the agent, a person, or the
        -- machinery around them. A reader following a run needs to tell an
        -- agent's work from an operator's decision, and a flat log cannot.
        source TEXT NOT NULL DEFAULT 'agent',
        -- Correlates the events of one piece of work: a tool call and its
        -- result, or the branches of one fanned-out step. Without it a
        -- failure is a line next to the thing that failed rather than part
        -- of it, and concurrent work interleaves with nothing to group by.
        call_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ac_events_ref_id ON ac_task_events(task_ref, id)",
    # ---- controls -------------------------------------------------------
    # An append-only log, not a single command slot. A log can express "stop was
    # requested twice and acknowledged once"; a slot cannot, and that
    # distinction matters when diagnosing a task that would not die.
    """
    CREATE TABLE IF NOT EXISTS ac_task_controls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_ref TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT,
        requested_by TEXT,
        requested_at TEXT NOT NULL,
        acknowledged_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ac_controls_pending ON ac_task_controls(task_ref, acknowledged_at)",
    # ---- worker liveness ------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS ac_runner_heartbeats (
        runner_id TEXT PRIMARY KEY,
        task_ref TEXT,
        pid INTEGER,
        hostname TEXT,
        status TEXT NOT NULL,
        message TEXT,
        started_at TEXT,
        heartbeat_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # ---- human gates ----------------------------------------------------
    # The new capability. `kind` follows the Agent Client Protocol's split:
    #   approve -> session/request_permission  (yes / no)
    #   input   -> elicitation/create          (structured response)
    # design-review is an `input` gate; conflating the two would force a
    # migration the moment someone wants review comments rather than a boolean.
    #
    # `thread_id` is the workflow engine's resume handle. Storing it here is what
    # lets a responder resume a suspended run without the responder knowing
    # anything about the workflow engine.
    """
    CREATE TABLE IF NOT EXISTS ac_human_gates (
        gate_id TEXT PRIMARY KEY,
        task_ref TEXT NOT NULL,
        thread_id TEXT,
        node TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('approve', 'input')),
        state TEXT NOT NULL CHECK(state IN ('pending', 'answered', 'expired', 'cancelled')),
        prompt_json TEXT NOT NULL DEFAULT '{}',
        response_schema_json TEXT,
        response_json TEXT,
        requested_at TEXT NOT NULL,
        expires_at TEXT,
        answered_at TEXT,
        answered_by TEXT,
        resumed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # The approval inbox reads this: pending gates across all tasks, oldest
    # first. Partial index keeps it small as answered gates accumulate.
    """
    CREATE INDEX IF NOT EXISTS ix_ac_gates_pending
        ON ac_human_gates(requested_at) WHERE state = 'pending'
    """,
    "CREATE INDEX IF NOT EXISTS ix_ac_gates_ref ON ac_human_gates(task_ref)",
)

#: Where applied migrations are recorded. Replaces `ac_schema_version`, which
#: held a single row nothing ever read.
MIGRATION_LEDGER = "ac_schema_migrations"


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _add_gate_resumed_at(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "ac_human_gates", "resumed_at", "TEXT")


def _add_event_source_and_call_id(conn: sqlite3.Connection) -> None:
    """Add the columns, then the index that needs them.

    The index cannot live in the DDL: on a database that already has the table,
    `CREATE TABLE IF NOT EXISTS` does nothing, so the DDL would index a column
    that this migration has not added yet -- and DDL runs first.
    """
    _add_column_if_missing(conn, "ac_task_events", "source", "TEXT NOT NULL DEFAULT 'agent'")
    _add_column_if_missing(conn, "ac_task_events", "call_id", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ac_events_call ON ac_task_events(task_ref, call_id)")


def _drop_unread_schema_version(conn: sqlite3.Connection) -> None:
    """Remove the version table the ledger replaces.

    It recorded one row, was never read, and its presence implied a version
    check that did not exist. Dropping it is safe precisely because nothing
    read it.
    """
    conn.execute("DROP TABLE IF EXISTS ac_schema_version")


#: Applied in order, once per database, recorded in MIGRATION_LEDGER. Ids are
#: written to that ledger, so renaming one makes every database re-run it.
MIGRATIONS = (
    Migration("0001_gate_resumed_at", _add_gate_resumed_at),
    Migration("0002_drop_unread_schema_version", _drop_unread_schema_version),
    Migration("0003_event_source_and_call_id", _add_event_source_and_call_id),
)


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create the canonical tables, then bring the database up to date.

    Idempotent and safe alongside a product's own tables. The DDL creates
    what a fresh database needs; MIGRATIONS carries anything that a database
    created by an earlier release also needs, applied once each.
    """
    for statement in DDL:
        conn.execute(statement)
    apply_migrations(conn, MIGRATIONS, ledger_table=MIGRATION_LEDGER)
