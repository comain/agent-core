"""Tests for the canonical runtime layer (ADR-004)."""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_core.runtime import Gate, GateAlreadyAnswered, RuntimeStore


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(tmp_path / "runtime.db")
    s.init()
    return s


def test_init_is_idempotent(store):
    store.init()
    store.init()


def test_core_tables_are_namespaced(store):
    conn = store.connect()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"ac_task_events", "ac_task_controls", "ac_runner_heartbeats", "ac_human_gates"} <= names
    # Namespacing is what lets these share a file with a product's own tables.
    assert all(n.startswith("ac_") or n.startswith("sqlite") for n in names)


def test_coexists_with_a_product_table(tmp_path):
    """The core must not collide with tables a product already owns."""
    db = tmp_path / "shared.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cr_tasks (task_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT)")
    conn.commit()
    conn.close()

    RuntimeStore(db).init()  # must not raise

    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "task_events" in names and "ac_task_events" in names


# -- events --------------------------------------------------------------------


def test_events_stream_by_cursor(store):
    for i in range(5):
        store.append_event(task_ref="t1", event_type="progress", message=f"step {i}")
    first = store.events_since(task_ref="t1")
    assert [r["message"] for r in first] == [f"step {i}" for i in range(5)]

    tail = store.events_since(task_ref="t1", after_id=first[2]["id"])
    assert [r["message"] for r in tail] == ["step 3", "step 4"]


def test_events_are_isolated_per_task(store):
    store.append_event(task_ref="a", event_type="e", message="for-a")
    store.append_event(task_ref="b", event_type="e", message="for-b")
    assert [r["message"] for r in store.events_since(task_ref="a")] == ["for-a"]


def test_cursor_survives_same_second_events(store):
    """Timestamps here have one-second resolution, so the cursor must be the id.

    A created_at cursor would drop or repeat events written in the same second,
    which is the common case during a burst of tool activity.
    """
    ids = [store.append_event(task_ref="t", event_type="e", message=str(i)) for i in range(20)]
    assert ids == sorted(ids) and len(set(ids)) == 20
    rows = store.events_since(task_ref="t", after_id=ids[9])
    assert len(rows) == 10


def test_event_payload_roundtrips(store):
    store.append_event(task_ref="t", event_type="tool", message="m", payload={"tool": "bash", "n": 3})
    import json
    assert json.loads(store.events_since(task_ref="t")[0]["payload_json"]) == {"tool": "bash", "n": 3}


# -- controls ------------------------------------------------------------------


def test_controls_are_a_log_not_a_slot(store):
    """Two stop requests must both be visible.

    One consuming product models control as a single row per task, which cannot
    express 'requested twice, acknowledged once'. The core keeps the log.
    """
    store.request_control(task_ref="t", action="stop", reason="first")
    store.request_control(task_ref="t", action="stop", reason="second")
    pending = store.pending_controls(task_ref="t")
    assert [c["reason"] for c in pending] == ["first", "second"]


def test_acknowledge_control_is_once_only(store):
    cid = store.request_control(task_ref="t", action="cancel")
    assert store.acknowledge_control(cid) is True
    assert store.acknowledge_control(cid) is False
    assert store.pending_controls(task_ref="t") == []


# -- heartbeats ----------------------------------------------------------------


def test_heartbeat_upserts(store):
    store.heartbeat(runner_id="r1", status="running", task_ref="t", pid=1)
    store.heartbeat(runner_id="r1", status="idle", task_ref=None, pid=1)
    conn = store.connect()
    rows = list(conn.execute("SELECT * FROM ac_runner_heartbeats"))
    conn.close()
    assert len(rows) == 1 and rows[0]["status"] == "idle"


def test_stale_runners_detected(store):
    store.heartbeat(runner_id="fresh", status="running")
    conn = store.connect()
    conn.execute("UPDATE ac_runner_heartbeats SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE runner_id='fresh'")
    conn.commit()
    conn.close()
    assert [r["runner_id"] for r in store.stale_runners(older_than_seconds=60)] == ["fresh"]


# -- human gates ---------------------------------------------------------------


def test_open_and_answer_an_input_gate(store):
    gate = store.open_gate(
        task_ref="t1", node="design_review", kind="input", thread_id="thread-1",
        prompt={"question": "Approve?", "design": "..."},
        response_schema={"decision": "approve|reject", "comments": "string"},
    )
    assert isinstance(gate, Gate) and gate.state == "pending"
    assert gate.thread_id == "thread-1"

    answered = store.answer_gate(
        gate_id=gate.gate_id, response={"decision": "approve", "comments": "ship it"}, answered_by="alice"
    )
    assert answered.state == "answered"
    assert answered.response == {"decision": "approve", "comments": "ship it"}
    assert answered.answered_by == "alice"


def test_gate_kind_is_constrained(store):
    """approve vs input is the ACP distinction; a third kind is a mistake."""
    with pytest.raises(ValueError):
        store.open_gate(task_ref="t", node="n", kind="maybe", prompt={})


def test_thread_id_lets_a_responder_resume_without_knowing_the_engine(store):
    gate = store.open_gate(task_ref="t", node="n", kind="approve", prompt={}, thread_id="wf-42")
    assert store.get_gate(gate.gate_id).thread_id == "wf-42"


def test_double_answer_is_refused(store):
    """A second answer would resume the suspended workflow twice."""
    gate = store.open_gate(task_ref="t", node="n", kind="approve", prompt={})
    store.answer_gate(gate_id=gate.gate_id, response=True)
    with pytest.raises(GateAlreadyAnswered):
        store.answer_gate(gate_id=gate.gate_id, response=False)
    assert store.get_gate(gate.gate_id).response is True


def test_answering_a_missing_gate_raises_keyerror(store):
    with pytest.raises(KeyError):
        store.answer_gate(gate_id="nope", response=True)


def test_concurrent_answers_only_one_wins(store):
    """Two reviewers acting on the same inbox entry at once."""
    gate = store.open_gate(task_ref="t", node="n", kind="approve", prompt={})

    def attempt(value):
        try:
            store.answer_gate(gate_id=gate.gate_id, response=value)
            return "won"
        except GateAlreadyAnswered:
            return "lost"
        except sqlite3.OperationalError:
            return "locked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [True, False]))
    assert results.count("won") == 1, results


def test_pending_gates_is_the_inbox_across_tasks(store):
    a = store.open_gate(task_ref="task-a", node="design_review", kind="input", prompt={})
    time.sleep(1)  # timestamps are second-resolution; force a distinct ordering
    b = store.open_gate(task_ref="task-b", node="ship", kind="approve", prompt={})
    store.open_gate(task_ref="task-c", node="n", kind="approve", prompt={})

    store.answer_gate(gate_id=b.gate_id, response=True)
    inbox = store.pending_gates()
    refs = [g.task_ref for g in inbox]
    assert "task-b" not in refs
    assert refs[0] == "task-a"  # oldest first


def test_cancel_gate(store):
    gate = store.open_gate(task_ref="t", node="n", kind="approve", prompt={})
    assert store.cancel_gate(gate.gate_id) is True
    assert store.get_gate(gate.gate_id).state == "cancelled"
    assert store.cancel_gate(gate.gate_id) is False


def test_expire_only_affects_past_due_pending_gates(store):
    soon = store.open_gate(task_ref="t", node="n", kind="approve", prompt={}, expires_in_seconds=-1)
    later = store.open_gate(task_ref="t", node="n", kind="approve", prompt={}, expires_in_seconds=3600)
    never = store.open_gate(task_ref="t", node="n", kind="approve", prompt={})

    expired = store.expire_gates()
    assert expired == [soon.gate_id]
    assert store.get_gate(later.gate_id).state == "pending"
    assert store.get_gate(never.gate_id).state == "pending"


def test_expired_gate_cannot_then_be_answered(store):
    gate = store.open_gate(task_ref="t", node="n", kind="approve", prompt={}, expires_in_seconds=-1)
    store.expire_gates()
    with pytest.raises(GateAlreadyAnswered):
        store.answer_gate(gate_id=gate.gate_id, response=True)


def test_latest_event_id_is_the_live_cursor(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    store.init()
    assert store.latest_event_id(task_ref="t") == 0
    first = store.append_event(task_ref="t", event_type="task_failed", message="old run")
    assert store.latest_event_id(task_ref="t") == first
    second = store.append_event(task_ref="t", event_type="gate_opened", message="new run")
    assert store.latest_event_id(task_ref="t") == second
    assert store.latest_event_id(task_ref="other") == 0
    # The point of the cursor: a stale terminal event is not replayed.
    assert store.events_since(task_ref="t", after_id=first) == [
        r for r in store.events_since(task_ref="t", after_id=0) if r["id"] > first
    ]


def test_a_cancelled_gate_reopens_rather_than_poisoning_its_id(store):
    """The gate id is derived from thread and node, so it repeats every time a
    run reaches that gate. Returning a *cancelled* record for it meant the run
    suspended on a gate that no inbox lists and nobody can answer -- the task
    waits for a human forever, and the page shows it waiting on nothing."""
    first = store.open_gate(
        gate_id="gate-fixed", task_ref="t", node="escalate", kind="approve",
        prompt={"round": 1}, thread_id="wf-1",
    )
    assert first.state == "pending"

    # While it stands, the id is idempotent: a resumed run re-executes the node
    # and must not fill the inbox with duplicates.
    again = store.open_gate(
        gate_id="gate-fixed", task_ref="t", node="escalate", kind="approve",
        prompt={"round": 2}, thread_id="wf-1",
    )
    assert again.state == "pending"
    assert again.prompt == {"round": 1}

    store.cancel_gate("gate-fixed")
    assert store.get_gate("gate-fixed").state == "cancelled"

    reopened = store.open_gate(
        gate_id="gate-fixed", task_ref="t", node="escalate", kind="approve",
        prompt={"round": 3}, thread_id="wf-1",
    )
    assert reopened.state == "pending"
    assert reopened.prompt == {"round": 3}
    assert [g.gate_id for g in store.pending_gates()] == ["gate-fixed"]
