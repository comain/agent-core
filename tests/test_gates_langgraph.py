"""Tests for the LangGraph human-gate bridge.

The re-execution behaviour is the thing worth pinning: LangGraph re-runs a gated
node from the top when a run resumes, so anything before the suspension point
executes more than once per gate.
"""

from __future__ import annotations

import sqlite3
from typing import Any, TypedDict

import pytest

from agent_core.gates.langgraph import (
    gate_id_for,
    human_gate,
    pending_gate_for_thread,
    resume_answered_gates,
)
from agent_core.identity import Principal
from agent_core.runtime import RuntimeStore

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(tmp_path / "rt.db")
    s.init()
    return s


class FlowState(TypedDict, total=False):
    task_ref: str
    design: str
    decision: str
    comments: str
    built: str


def make_graph(store: RuntimeStore, db_path, *, counter=None):
    """design -> design_review (gate) -> build"""

    def design(state):
        return {"design": "an SSE progress endpoint"}

    def design_review(state):
        if counter is not None:
            counter["before_gate"] = counter.get("before_gate", 0) + 1
        answer = human_gate(
            store,
            task_ref=state["task_ref"],
            node="design_review",
            kind="input",
            prompt={"design": state.get("design")},
            response_schema={"decision": "approve|reject", "comments": "string"},
            config={"configurable": {"thread_id": state["task_ref"]}},
        )
        if counter is not None:
            counter["after_gate"] = counter.get("after_gate", 0) + 1
        return {"decision": answer.get("decision"), "comments": answer.get("comments", "")}

    def build(state):
        if state.get("decision") != "approve":
            return {"built": "halted"}
        return {"built": f"built ({state.get('comments')})"}

    b = StateGraph(FlowState)
    b.add_node("design", design)
    b.add_node("design_review", design_review)
    b.add_node("build", build)
    b.add_edge(START, "design")
    b.add_edge("design", "design_review")
    b.add_edge("design_review", "build")
    b.add_edge("build", END)
    return b.compile(checkpointer=SqliteSaver(sqlite3.connect(str(db_path), check_same_thread=False)))


def _run(graph, task_ref):
    cfg = {"configurable": {"thread_id": task_ref}}
    graph.invoke({"task_ref": task_ref}, config=cfg)
    return cfg


# -- gate ids ------------------------------------------------------------------


def test_gate_id_is_deterministic():
    a = gate_id_for(thread_id="t", node="review")
    assert a == gate_id_for(thread_id="t", node="review")
    assert a != gate_id_for(thread_id="t", node="other")
    assert a != gate_id_for(thread_id="other", node="review")


def test_attempt_distinguishes_repeat_visits():
    """A revision cycle asking for review twice needs two inbox entries."""
    assert gate_id_for(thread_id="t", node="r", attempt="1") != gate_id_for(thread_id="t", node="r", attempt="2")


# -- the gate lifecycle --------------------------------------------------------


def test_invoke_none_on_an_interrupted_snapshot_does_not_apply_the_answer(store, tmp_path):
    """ADR-005 rejected path: invoke(None) re-enters interrupt without the human."""
    graph = make_graph(store, tmp_path / "wf.db")
    cfg = _run(graph, "task-1")
    graph.invoke(None, config=cfg)
    state = graph.get_state(cfg)
    assert list(state.next) == ["design_review"]
    assert "built" not in (state.values or {})


def test_gate_is_recorded_and_run_suspends(store, tmp_path):
    graph = make_graph(store, tmp_path / "wf.db")
    cfg = _run(graph, "task-1")

    assert list(graph.get_state(cfg).next) == ["design_review"]
    inbox = store.pending_gates()
    assert len(inbox) == 1
    gate = inbox[0]
    assert gate.node == "design_review" and gate.kind == "input"
    assert gate.thread_id == "task-1"
    assert gate.prompt["design"] == "an SSE progress endpoint"
    assert gate.response_schema == {"decision": "approve|reject", "comments": "string"}


def test_gate_never_expires_by_default(store, tmp_path):
    """Policy: wait indefinitely rather than auto-deciding on a timer."""
    graph = make_graph(store, tmp_path / "wf.db")
    _run(graph, "task-1")
    assert store.pending_gates()[0].expires_at is None
    assert store.expire_gates() == []


def test_answer_then_resume_completes_the_run(store, tmp_path):
    graph = make_graph(store, tmp_path / "wf.db")
    cfg = _run(graph, "task-1")
    gate = store.pending_gates()[0]

    store.answer_gate(
        gate_id=gate.gate_id,
        response={"decision": "approve", "comments": "ship it"},
        principal=Principal(subject="alice", kind="user"),
    )
    outcomes = resume_answered_gates(graph, store)
    assert outcomes == {gate.gate_id: "resumed"}

    state = graph.get_state(cfg)
    assert list(state.next) == []
    assert state.values["built"] == "built (ship it)"


def test_rejection_flows_through_to_the_next_node(store, tmp_path):
    graph = make_graph(store, tmp_path / "wf.db")
    cfg = _run(graph, "task-1")
    gate = store.pending_gates()[0]
    store.answer_gate(gate_id=gate.gate_id, response={"decision": "reject", "comments": "no"}, answered_by="x")
    resume_answered_gates(graph, store)
    assert graph.get_state(cfg).values["built"] == "halted"


# -- the re-execution hazard ---------------------------------------------------


def test_node_reexecutes_but_gate_is_not_duplicated(store, tmp_path):
    """The measured behaviour: pre-gate code runs twice, post-gate code once.

    Without an idempotent gate id this produces a duplicate inbox entry for
    every gate ever answered.
    """
    counter: dict = {}
    graph = make_graph(store, tmp_path / "wf.db", counter=counter)
    _run(graph, "task-1")
    assert counter == {"before_gate": 1}

    gate = store.pending_gates()[0]
    store.answer_gate(gate_id=gate.gate_id, response={"decision": "approve"}, answered_by="x")
    resume_answered_gates(graph, store)

    assert counter["before_gate"] == 2, "pre-gate code should re-execute on resume"
    assert counter["after_gate"] == 1

    conn = store.connect()
    total = conn.execute("SELECT COUNT(*) FROM ac_human_gates").fetchone()[0]
    conn.close()
    assert total == 1, "re-execution must not create a second gate"


# -- resume driver -------------------------------------------------------------


def test_resume_is_claimed_once_under_concurrency(store, tmp_path):
    """Two daemons ticking at once must not both resume the same run."""
    graph = make_graph(store, tmp_path / "wf.db")
    _run(graph, "task-1")
    gate = store.pending_gates()[0]
    store.answer_gate(gate_id=gate.gate_id, response={"decision": "approve"}, answered_by="x")

    first = resume_answered_gates(graph, store)
    second = resume_answered_gates(graph, store)
    assert first == {gate.gate_id: "resumed"}
    assert second == {}, "an already-resumed gate must not be picked up again"


def test_resume_failure_is_recorded_and_does_not_stop_the_tick(store, tmp_path):
    graph = make_graph(store, tmp_path / "wf.db")
    _run(graph, "task-1")
    gate = store.pending_gates()[0]
    store.answer_gate(gate_id=gate.gate_id, response={"decision": "approve"}, answered_by="x")

    class Exploding:
        def invoke(self, *a, **k):
            raise RuntimeError("checkpoint unavailable")

    outcomes = resume_answered_gates(Exploding(), store)
    assert "error: checkpoint unavailable" in outcomes[gate.gate_id]
    events = [r["event_type"] for r in store.events_since(task_ref="task-1")]
    assert "gate_resume_failed" in events


def test_gate_lifecycle_is_visible_in_the_event_log(store, tmp_path):
    """A live progress view should show the wait and the resume."""
    graph = make_graph(store, tmp_path / "wf.db")
    _run(graph, "task-1")
    gate = store.pending_gates()[0]
    store.answer_gate(gate_id=gate.gate_id, response={"decision": "approve"}, answered_by="alice")
    resume_answered_gates(graph, store)

    events = [r["event_type"] for r in store.events_since(task_ref="task-1")]
    assert "gate_opened" in events and "gate_resumed" in events


def test_pending_gate_lookup_by_thread(store, tmp_path):
    graph = make_graph(store, tmp_path / "wf.db")
    _run(graph, "task-1")
    assert pending_gate_for_thread(store, thread_id="task-1").node == "design_review"
    assert pending_gate_for_thread(store, thread_id="nope") is None


def test_thread_id_is_required(store):
    with pytest.raises(ValueError, match="thread_id"):
        human_gate(store, task_ref="t", node="n", kind="approve", prompt={}, config={})


def test_two_tasks_produce_independent_gates(store, tmp_path):
    graph = make_graph(store, tmp_path / "wf.db")
    _run(graph, "task-1")
    _run(graph, "task-2")
    inbox = store.pending_gates()
    assert {g.task_ref for g in inbox} == {"task-1", "task-2"}
    assert len({g.gate_id for g in inbox}) == 2
