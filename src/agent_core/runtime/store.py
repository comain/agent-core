"""Operations over the canonical runtime schema.

Deliberately not an ORM and not a task queue. Products keep their own task
tables and their own claim logic; what lives here is the cross-cutting
machinery every product ends up needing -- an event log an SSE stream can tail,
a control channel, worker liveness, and human gates.

Concurrency: every mutation runs in an ``IMMEDIATE`` transaction so two daemons
racing on the same gate cannot both win. This matters most for
:meth:`RuntimeStore.answer_gate`, where a double answer would resume a suspended
workflow twice.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence, TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Union

if TYPE_CHECKING:  # avoid a runtime dependency from runtime -> identity
    from agent_core.identity.policy import Policy
    from agent_core.identity.principal import Principal

from agent_core.runtime.schema import apply_schema


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _plus(seconds: int) -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=seconds)).isoformat()


class GateAlreadyAnswered(RuntimeError):
    """Raised when answering a gate that is no longer pending.

    Surfaced rather than swallowed: a second answer usually means two reviewers
    acted on the same inbox entry, and silently accepting one would resume the
    workflow twice.
    """


@dataclass(frozen=True)
class Gate:
    gate_id: str
    task_ref: str
    thread_id: Optional[str]
    node: str
    kind: str
    state: str
    prompt: Dict[str, Any]
    response_schema: Optional[Dict[str, Any]]
    response: Optional[Any]
    requested_at: str
    expires_at: Optional[str]
    answered_at: Optional[str]
    answered_by: Optional[str]
    resumed_at: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Gate":
        return cls(
            gate_id=row["gate_id"],
            task_ref=row["task_ref"],
            thread_id=row["thread_id"],
            node=row["node"],
            kind=row["kind"],
            state=row["state"],
            prompt=json.loads(row["prompt_json"] or "{}"),
            response_schema=json.loads(row["response_schema_json"]) if row["response_schema_json"] else None,
            response=json.loads(row["response_json"]) if row["response_json"] else None,
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            answered_at=row["answered_at"],
            answered_by=row["answered_by"],
            resumed_at=row["resumed_at"] if "resumed_at" in row.keys() else None,
        )


class RuntimeStore:
    def __init__(self, path: Union[str, Path]):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            # IMMEDIATE takes the write lock up front, so a concurrent writer
            # fails fast instead of at COMMIT after doing its work.
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        conn = self.connect()
        try:
            apply_schema(conn)
            conn.commit()
        finally:
            conn.close()

    # -- events ------------------------------------------------------------

    def append_event(
        self,
        *,
        task_ref: str,
        event_type: str,
        message: str,
        stage: Optional[str] = None,
        severity: str = "info",
        payload: Optional[Dict[str, Any]] = None,
        source: str = "agent",
        call_id: Optional[str] = None,
    ) -> int:
        """Append one event. ``source`` attributes it; ``call_id`` groups it.

        Both default to what the common case is -- the agent, working on
        nothing in particular -- so existing callers are unaffected.
        """
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO ac_task_events
                    (task_ref, event_type, severity, stage, message, payload_json,
                     source, call_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_ref, event_type, severity, stage, message,
                    json.dumps(payload or {}), source, call_id, _now(),
                ),
            )
            return int(cur.lastrowid)

    def events_since(
        self,
        *,
        task_ref: str,
        after_id: int = 0,
        limit: int = 500,
        kinds: Optional[Sequence[str]] = None,
        stages: Optional[Sequence[str]] = None,
        call_id: Optional[str] = None,
        newest_first: bool = False,
    ) -> List[sqlite3.Row]:
        """Events with ``id > after_id``, oldest first unless asked otherwise.

        This is the SSE cursor. Ordering by ``id`` rather than ``created_at``
        is deliberate: timestamps have one-second resolution here, so several
        events can share one, and a timestamp cursor would drop or repeat them.

        The filters exist because a caller that wants one kind, one stage or
        one call had to read everything and discard most of it -- and a
        `limit` then answered with the oldest rows rather than the ones
        asked for. ``newest_first`` is for a view that wants the tail of a
        long run without paging the whole log to reach it.
        """
        clauses = ["task_ref = ?", "id > ?"]
        params: List[Any] = [task_ref, after_id]
        if kinds:
            clauses.append(f"event_type IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if stages:
            clauses.append(f"stage IN ({','.join('?' * len(stages))})")
            params.extend(stages)
        if call_id:
            clauses.append("call_id = ?")
            params.append(call_id)
        order = "DESC" if newest_first else "ASC"
        params.append(limit)
        conn = self.connect()
        try:
            return list(
                conn.execute(
                    f"""
                    SELECT * FROM ac_task_events
                    WHERE {' AND '.join(clauses)}
                    ORDER BY id {order} LIMIT ?
                    """,
                    tuple(params),
                )
            )
        finally:
            conn.close()

    def latest_event_id(self, *, task_ref: str) -> int:
        """Highest event id for a task, or 0 when it has none.

        The cursor a *live-only* stream starts from. Tailing from 0 replays the
        log, which is right for a viewer who wants the history and wrong for one
        that reacts to what it reads: a terminal event from a previous, retried
        run is still in the log, and replaying it ends the new stream on its
        first frame.
        """
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT MAX(id) AS max_id FROM ac_task_events WHERE task_ref = ?",
                (task_ref,),
            ).fetchone()
            return int(row["max_id"] or 0) if row else 0
        finally:
            conn.close()

    # -- controls ----------------------------------------------------------

    def request_control(
        self, *, task_ref: str, action: str, reason: Optional[str] = None, requested_by: Optional[str] = None
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO ac_task_controls (task_ref, action, reason, requested_by, requested_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_ref, action, reason, requested_by, _now()),
            )
            return int(cur.lastrowid)

    def pending_controls(self, *, task_ref: str) -> List[sqlite3.Row]:
        conn = self.connect()
        try:
            return list(
                conn.execute(
                    "SELECT * FROM ac_task_controls WHERE task_ref = ? AND acknowledged_at IS NULL ORDER BY id ASC",
                    (task_ref,),
                )
            )
        finally:
            conn.close()

    def acknowledge_control(self, control_id: int) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE ac_task_controls SET acknowledged_at = ? WHERE id = ? AND acknowledged_at IS NULL",
                (_now(), control_id),
            )
            return cur.rowcount > 0

    # -- worker liveness ---------------------------------------------------

    def heartbeat(
        self,
        *,
        runner_id: str,
        status: str,
        task_ref: Optional[str] = None,
        pid: Optional[int] = None,
        hostname: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        now = _now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO ac_runner_heartbeats
                    (runner_id, task_ref, pid, hostname, status, message, started_at, heartbeat_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(runner_id) DO UPDATE SET
                    task_ref=excluded.task_ref, pid=excluded.pid, hostname=excluded.hostname,
                    status=excluded.status, message=excluded.message,
                    heartbeat_at=excluded.heartbeat_at, updated_at=excluded.updated_at
                """,
                (runner_id, task_ref, pid, hostname, status, message, now, now, now, now),
            )

    def stale_runners(self, *, older_than_seconds: int) -> List[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).replace(microsecond=0).isoformat()
        conn = self.connect()
        try:
            return list(
                conn.execute(
                    "SELECT * FROM ac_runner_heartbeats WHERE heartbeat_at < ? ORDER BY heartbeat_at ASC",
                    (cutoff,),
                )
            )
        finally:
            conn.close()

    # -- human gates -------------------------------------------------------

    def open_gate(
        self,
        *,
        task_ref: str,
        node: str,
        kind: str,
        prompt: Dict[str, Any],
        thread_id: Optional[str] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        expires_in_seconds: Optional[int] = None,
        gate_id: Optional[str] = None,
    ) -> Gate:
        """Record that a workflow is waiting on a human.

        ``kind`` follows the Agent Client Protocol split -- ``approve`` for a
        yes/no permission, ``input`` for a structured response. ``design-review``
        is an ``input`` gate: it wants comments, not a boolean.
        """
        if kind not in ("approve", "input"):
            raise ValueError(f"kind must be 'approve' or 'input', got {kind!r}")
        gid = gate_id or f"gate-{uuid.uuid4().hex[:16]}"
        now = _now()

        # Idempotent when an explicit gate_id is supplied. A workflow engine
        # re-executes the code preceding a suspension point when the run
        # resumes, so this is called more than once for a single gate; without
        # this the inbox would fill with duplicates of every gate ever answered.
        if gate_id is not None:
            existing = self.get_gate(gid)
            # ...but only while the record still stands for something. A
            # cancelled or expired gate keeps its id, so returning it means the
            # run suspends on a gate nobody can see or answer: the task waits
            # for a human forever, and the inbox is empty. Reopen it instead.
            if existing is not None and existing.state in ("pending", "answered"):
                return existing
            if existing is not None:
                with self.transaction() as conn:
                    conn.execute(
                        """
                        UPDATE ac_human_gates
                        SET state = 'pending', prompt_json = ?, response_json = NULL,
                            answered_at = NULL, answered_by = NULL, resumed_at = NULL,
                            requested_at = ?, expires_at = ?, updated_at = ?
                        WHERE gate_id = ?
                        """,
                        (
                            json.dumps(prompt),
                            now,
                            _plus(expires_in_seconds) if expires_in_seconds else None,
                            now,
                            gid,
                        ),
                    )
                return self.get_gate(gid)  # type: ignore[return-value]

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO ac_human_gates
                    (gate_id, task_ref, thread_id, node, kind, state, prompt_json,
                     response_schema_json, requested_at, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    gid, task_ref, thread_id, node, kind, json.dumps(prompt),
                    json.dumps(response_schema) if response_schema else None,
                    now,
                    _plus(expires_in_seconds) if expires_in_seconds else None,
                    now, now,
                ),
            )
        return self.get_gate(gid)  # type: ignore[return-value]

    def get_gate(self, gate_id: str) -> Optional[Gate]:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM ac_human_gates WHERE gate_id = ?", (gate_id,)).fetchone()
            return Gate.from_row(row) if row else None
        finally:
            conn.close()

    def answer_gate(
        self,
        *,
        gate_id: str,
        response: Any,
        answered_by: Optional[str] = None,
        principal: Optional["Principal"] = None,
        policy: Optional["Policy"] = None,
    ) -> Gate:
        """Answer a pending gate.

        Conditional on the gate still being ``pending``, so two responders
        racing on the same inbox entry cannot both succeed -- the loser gets
        :class:`GateAlreadyAnswered` rather than silently overwriting, because
        each success would resume the suspended workflow.

        Attribution: pass ``principal`` (and optionally ``policy``) to have the
        answer authorized and attributed to an authenticated actor. The raw
        ``answered_by`` string remains for callers that have not yet adopted
        identity, but the two are mutually exclusive -- accepting both would let
        a caller present one identity and record another.
        """
        if principal is not None:
            if answered_by is not None:
                raise ValueError("pass either principal or answered_by, not both")
            from agent_core.identity.policy import ANSWER_GATE, Policy as _Policy

            (policy or _Policy()).authorize(principal, ANSWER_GATE)
            answered_by = principal.subject
        now = _now()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE ac_human_gates
                SET state='answered', response_json=?, answered_at=?, answered_by=?, updated_at=?
                WHERE gate_id=? AND state='pending'
                """,
                (json.dumps(response), now, answered_by, now, gate_id),
            )
            if cur.rowcount < 1:
                existing = conn.execute(
                    "SELECT state FROM ac_human_gates WHERE gate_id=?", (gate_id,)
                ).fetchone()
                if existing is None:
                    raise KeyError(f"no such gate: {gate_id}")
                raise GateAlreadyAnswered(f"gate {gate_id} is {existing['state']}, not pending")
        return self.get_gate(gate_id)  # type: ignore[return-value]

    def cancel_gate(self, gate_id: str) -> bool:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE ac_human_gates SET state='cancelled', updated_at=? WHERE gate_id=? AND state='pending'",
                (_now(), gate_id),
            )
            return cur.rowcount > 0

    def expire_gates(self) -> List[str]:
        """Mark past-due pending gates expired. Returns the ids affected.

        Whether an expired gate should auto-reject, escalate, or simply be
        visible is a *policy* question that belongs to the caller; this only
        records that the deadline passed.
        """
        now = _now()
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT gate_id FROM ac_human_gates WHERE state='pending' AND expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            ).fetchall()
            ids = [r["gate_id"] for r in rows]
            if ids:
                conn.executemany(
                    "UPDATE ac_human_gates SET state='expired', updated_at=? WHERE gate_id=?",
                    [(now, gid) for gid in ids],
                )
            return ids

    def mark_resumed(self, gate_id: str) -> bool:
        """Record that an answered gate's workflow has been resumed.

        Conditional on not already being resumed, so a driver that runs twice --
        or two drivers running at once -- cannot resume the same workflow twice.
        """
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE ac_human_gates SET resumed_at=?, updated_at=? "
                "WHERE gate_id=? AND state='answered' AND resumed_at IS NULL",
                (_now(), _now(), gate_id),
            )
            return cur.rowcount > 0

    def answered_gates_awaiting_resume(self, *, limit: int = 100) -> List[Gate]:
        """Gates a human has answered whose workflow has not yet been resumed."""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM ac_human_gates WHERE state='answered' AND resumed_at IS NULL "
                "ORDER BY answered_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [Gate.from_row(r) for r in rows]
        finally:
            conn.close()

    def pending_gates(self, *, limit: int = 100) -> List[Gate]:
        """The approval inbox: gates awaiting a human, across all tasks."""
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM ac_human_gates WHERE state='pending' ORDER BY requested_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [Gate.from_row(r) for r in rows]
        finally:
            conn.close()
