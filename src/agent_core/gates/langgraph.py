"""LangGraph adapter for human gates.

Bridges two halves that otherwise know nothing about each other: the workflow
engine, which suspends a run and can resume it, and the gate store, which is
what an approval inbox reads and writes.

Requires the optional dependency::

    pip install "agent-core[langgraph]"

LangGraph is *not* a hard dependency of agent-core. Three of the four consumers
do not run graphs today, and they should not have to install a workflow engine
to use the harness.

## The re-execution hazard

When a suspended run resumes, LangGraph **re-executes the gated node from the
top**; ``interrupt()`` then returns the supplied value instead of raising.
Measured directly: the code before ``interrupt()`` ran twice for one gate, the
code after it once.

So everything preceding the suspension point runs at least twice, and a gate
recorded with a fresh identifier each time would fill the inbox with duplicates
of every gate ever answered. The gate id is therefore derived deterministically
from the thread and node, and :meth:`RuntimeStore.open_gate` returns the
existing record when given an id it already holds.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Mapping, Optional

try:
    from langgraph.types import Command, interrupt
except ImportError as exc:  # pragma: no cover - exercised by the import test
    raise ImportError(
        "agent_core.gates.langgraph requires LangGraph. "
        'Install it with: pip install "agent-core[langgraph]"'
    ) from exc

from agent_core.runtime.store import Gate, RuntimeStore


def gate_id_for(*, thread_id: str, node: str, attempt: str = "") -> str:
    """Deterministic gate id.

    Stable across re-execution of the same node in the same run, which is what
    makes gate creation idempotent. ``attempt`` distinguishes legitimate repeat
    visits -- a graph that loops back to the same gate node, or a revision cycle
    that asks for review a second time -- which must be separate inbox entries.
    """
    digest = hashlib.sha256(f"{thread_id}\0{node}\0{attempt}".encode()).hexdigest()
    return f"gate-{digest[:24]}"


def human_gate(
    store: RuntimeStore,
    *,
    task_ref: str,
    node: str,
    kind: str,
    prompt: Dict[str, Any],
    config: Mapping[str, Any],
    response_schema: Optional[Dict[str, Any]] = None,
    attempt: str = "",
    expires_in_seconds: Optional[int] = None,
) -> Any:
    """Suspend the workflow until a human answers, and return their answer.

    Call from inside a graph node::

        def design_review(state):
            answer = human_gate(
                store, task_ref=state["task_ref"], node="design_review",
                kind="input", prompt={"design": state["design"]},
                response_schema={"decision": "approve|reject", "comments": "string"},
                config=config,
            )
            return {"decision": answer["decision"]}

    ``expires_in_seconds`` defaults to ``None`` -- gates wait indefinitely. That
    is a deliberate policy choice: an unanswered design review should block its
    workflow rather than be silently auto-rejected or auto-approved on a timer.
    It is safe only because the run is suspended, holding no worker, and because
    the inbox makes every waiting gate visible.
    """
    thread_id = (config.get("configurable") or {}).get("thread_id")
    if not thread_id:
        raise ValueError("human_gate needs config['configurable']['thread_id'] to resume later")

    gid = gate_id_for(thread_id=str(thread_id), node=node, attempt=attempt)

    # Runs on every re-execution; idempotent by construction.
    store.open_gate(
        gate_id=gid,
        task_ref=task_ref,
        node=node,
        kind=kind,
        prompt=prompt,
        thread_id=str(thread_id),
        response_schema=response_schema,
        expires_in_seconds=expires_in_seconds,
    )
    store.append_event(
        task_ref=task_ref,
        event_type="gate_opened",
        severity="info",
        stage=node,
        message=f"waiting for human {kind} at {node}",
        payload={"gate_id": gid},
    )

    # Raises out of the node on first execution; returns the answer on resume.
    return interrupt({"gate_id": gid, "kind": kind, "node": node, **prompt})


def resume_answered_gates(graph: Any, store: RuntimeStore, *, limit: int = 100) -> Dict[str, str]:
    """Resume workflows whose gates have been answered.

    Intended to be driven by a daemon tick. Returns ``{gate_id: outcome}`` where
    outcome is ``resumed``, ``skipped`` (another worker claimed it first), or an
    ``error: ...`` string.

    Resumption is claimed *before* the graph is invoked, via a conditional
    update. Two daemons ticking simultaneously would otherwise both resume the
    same run, producing duplicate downstream work -- a second model call and a
    second set of edits.
    """
    outcomes: Dict[str, str] = {}
    for gate in store.answered_gates_awaiting_resume(limit=limit):
        if not gate.thread_id:
            outcomes[gate.gate_id] = "error: gate has no thread_id"
            continue
        if not store.mark_resumed(gate.gate_id):
            outcomes[gate.gate_id] = "skipped"
            continue
        try:
            graph.invoke(
                Command(resume=gate.response),
                config={"configurable": {"thread_id": gate.thread_id}},
            )
            store.append_event(
                task_ref=gate.task_ref,
                event_type="gate_resumed",
                severity="info",
                stage=gate.node,
                message=f"resumed after {gate.kind} by {gate.answered_by or 'unknown'}",
                payload={"gate_id": gate.gate_id},
            )
            outcomes[gate.gate_id] = "resumed"
        except Exception as exc:  # noqa: BLE001 - one bad gate must not stop the tick
            store.append_event(
                task_ref=gate.task_ref,
                event_type="gate_resume_failed",
                severity="error",
                stage=gate.node,
                message=f"resume failed: {exc}",
                payload={"gate_id": gate.gate_id},
            )
            outcomes[gate.gate_id] = f"error: {exc}"
    return outcomes


def pending_gate_for_thread(store: RuntimeStore, *, thread_id: str) -> Optional[Gate]:
    """The gate a suspended run is waiting on, if any."""
    for gate in store.pending_gates(limit=1000):
        if gate.thread_id == thread_id:
            return gate
    return None
