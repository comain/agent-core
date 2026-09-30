-- Generated from the live schema; do not edit.
-- Regenerate with: python scripts/dump_schema.py

CREATE TABLE ac_human_gates (
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
    );

CREATE TABLE ac_runner_heartbeats (
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
    );

CREATE TABLE ac_schema_migrations (
            id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

CREATE TABLE ac_task_controls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_ref TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT,
        requested_by TEXT,
        requested_at TEXT NOT NULL,
        acknowledged_at TEXT
    );

CREATE TABLE ac_task_events (
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
    );

CREATE INDEX ix_ac_controls_pending ON ac_task_controls(task_ref, acknowledged_at);

CREATE INDEX ix_ac_events_call ON ac_task_events(task_ref, call_id);

CREATE INDEX ix_ac_events_ref_id ON ac_task_events(task_ref, id);

CREATE INDEX ix_ac_gates_pending
        ON ac_human_gates(requested_at) WHERE state = 'pending';

CREATE INDEX ix_ac_gates_ref ON ac_human_gates(task_ref);
