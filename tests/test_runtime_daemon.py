"""Tests for the shared task daemon."""

from __future__ import annotations

import threading

import pytest

from agent_core.runtime import (
    DaemonConfig,
    DaemonPorts,
    RuntimeStore,
    TaskDaemon,
    TaskOutcome,
)


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(tmp_path / "rt.db")
    s.init()
    return s


class Queue:
    """A minimal stand-in for a product's task table."""

    def __init__(self, refs=()):
        self.refs = list(refs)
        self.claimed = []
        self.executed = []
        self.released = []
        self.renewals = 0
        self.outcome = None
        self.raises = None

    def claim(self, *, daemon_id, lease_seconds):
        if not self.refs:
            return None
        ref = self.refs.pop(0)
        self.claimed.append(ref)
        return ref

    def execute(self, ref):
        self.executed.append(ref)
        if self.raises:
            raise self.raises
        return self.outcome

    def renew(self, ref, *, daemon_id, lease_seconds):
        self.renewals += 1
        return True

    def release(self, ref, outcome):
        self.released.append((ref, outcome))


def make(store, queue, **cfg):
    return TaskDaemon(
        store,
        DaemonPorts(claim=queue.claim, execute=queue.execute, renew_lease=queue.renew, release=queue.release),
        config=DaemonConfig(**cfg),
        daemon_id="d1",
        _sleep=lambda s: None,
    )


# -- claiming and execution ----------------------------------------------------


def test_claims_and_executes(store):
    q = Queue(["t1"])
    assert make(store, q).once() is TaskOutcome.COMPLETED
    assert q.claimed == ["t1"] and q.executed == ["t1"]


def test_empty_queue_returns_none(store):
    assert make(store, Queue()).once() is None


def test_idle_records_a_heartbeat(store):
    make(store, Queue()).once()
    conn = store.connect()
    row = conn.execute("SELECT status, message FROM ac_runner_heartbeats WHERE runner_id='d1'").fetchone()
    conn.close()
    assert row["status"] == "IDLE" and "no queued task" in row["message"]


def test_idle_heartbeat_can_be_disabled(store):
    make(store, Queue(), idle_heartbeat=False).once()
    conn = store.connect()
    assert conn.execute("SELECT COUNT(*) FROM ac_runner_heartbeats").fetchone()[0] == 0
    conn.close()


# -- suspension is not failure -------------------------------------------------


def test_suspended_task_is_released_without_failure(store):
    """A run waiting on a human has not finished and has not crashed.

    Conflating the two either re-runs work a reviewer is still looking at, or
    pins a worker for the whole wait.
    """
    q = Queue(["t1"])
    q.outcome = TaskOutcome.SUSPENDED
    assert make(store, q).once() is TaskOutcome.SUSPENDED
    assert q.released == [("t1", TaskOutcome.SUSPENDED)]
    types = [r["event_type"] for r in store.events_since(task_ref="t1")]
    assert "task_failed" not in types


def test_outcome_accepts_a_plain_string(store):
    q = Queue(["t1"])
    q.outcome = "suspended"
    assert make(store, q).once() is TaskOutcome.SUSPENDED


def test_no_return_value_means_completed(store):
    q = Queue(["t1"])
    q.outcome = None
    assert make(store, q).once() is TaskOutcome.COMPLETED


# -- failure isolation ---------------------------------------------------------


def test_execution_failure_is_recorded_and_does_not_propagate(store):
    q = Queue(["t1"])
    q.raises = RuntimeError("workflow exploded")
    assert make(store, q).once() is TaskOutcome.FAILED
    assert [r["event_type"] for r in store.events_since(task_ref="t1")] == ["task_failed"]
    assert q.released == [("t1", TaskOutcome.FAILED)]


def test_claim_failure_does_not_stop_the_daemon(store):
    def bad_claim(**kw):
        raise RuntimeError("db locked")

    d = TaskDaemon(store, DaemonPorts(claim=bad_claim, execute=lambda r: None),
                   daemon_id="d1", _sleep=lambda s: None)
    assert d.once() is None  # survived


def test_release_failure_does_not_propagate(store):
    q = Queue(["t1"])
    def bad_release(ref, outcome):
        raise RuntimeError("nope")
    d = TaskDaemon(store, DaemonPorts(claim=q.claim, execute=q.execute, release=bad_release),
                   daemon_id="d1", _sleep=lambda s: None)
    assert d.once() is TaskOutcome.COMPLETED


# -- lease renewal -------------------------------------------------------------


def test_lease_is_renewed_while_the_task_runs(store):
    q = Queue(["t1"])
    started = threading.Event()

    def slow(ref):
        started.set()
        # long enough for at least one renewal at the clamped interval
        threading.Event().wait(0.5)
        return TaskOutcome.COMPLETED

    d = TaskDaemon(
        store,
        DaemonPorts(claim=q.claim, execute=slow, renew_lease=q.renew),
        config=DaemonConfig(lease_seconds=1),
        daemon_id="d1",
        _sleep=lambda s: None,
    )
    d.once()
    assert started.is_set()
    assert q.renewals >= 1


def test_renewal_interval_is_clamped():
    assert DaemonConfig(lease_seconds=300).lease_renew_interval == 60.0   # upper clamp
    assert DaemonConfig(lease_seconds=3).lease_renew_interval == 1.0      # lease/3
    assert DaemonConfig(lease_seconds=0).lease_renew_interval >= 0.2      # lower clamp


def test_daemon_works_without_a_renew_port(store):
    q = Queue(["t1"])
    d = TaskDaemon(store, DaemonPorts(claim=q.claim, execute=q.execute), daemon_id="d1", _sleep=lambda s: None)
    assert d.once() is TaskOutcome.COMPLETED


# -- the loop ------------------------------------------------------------------


def test_run_forever_drains_the_queue_without_sleeping(store):
    """Sleeping after a completed task drains a full queue one item per tick."""
    q = Queue(["a", "b", "c"])
    slept = []
    d = TaskDaemon(
        store,
        DaemonPorts(claim=q.claim, execute=q.execute),
        daemon_id="d1",
        _sleep=slept.append,
    )
    d.run_forever(max_ticks=4)
    assert q.executed == ["a", "b", "c"]
    assert len(slept) == 1  # only the final empty tick sleeps


def test_stop_ends_the_loop(store):
    q = Queue(["a"] * 100)
    d = make(store, q)

    original = q.execute
    def execute_then_stop(ref):
        d.stop()
        return original(ref)
    d.ports.execute = execute_then_stop

    assert d.run_forever() == 1


def test_maintenance_runs_and_isolates_failures(store):
    calls = []
    def good():
        calls.append("good")
    def bad():
        raise RuntimeError("hook failed")

    d = TaskDaemon(
        store,
        DaemonPorts(claim=lambda **kw: None, execute=lambda r: None, maintenance=(bad, good)),
        daemon_id="d1",
        _sleep=lambda s: None,
    )
    d.run_forever(max_ticks=1)
    assert calls == ["good"], "a failing hook must not skip the ones after it"


def test_maintenance_can_run_less_often_than_every_tick(store):
    calls = []
    d = TaskDaemon(
        store,
        DaemonPorts(claim=lambda **kw: None, execute=lambda r: None, maintenance=(lambda: calls.append(1),)),
        config=DaemonConfig(maintenance_every_ticks=3),
        daemon_id="d1",
        _sleep=lambda s: None,
    )
    d.run_forever(max_ticks=6)
    assert len(calls) == 2


# -- adopting the loop without adopting the schema -------------------------------


def test_daemon_runs_without_a_runtime_store():
    """ADR-004 promises per-capability adoption.

    A product with its own heartbeat table and event log must be able to take
    this loop without also taking the canonical schema -- otherwise 'adopt the
    daemon' silently means 'migrate your storage too'.
    """
    beats = []
    events = []
    q = Queue(["t1"])

    d = TaskDaemon(
        None,
        DaemonPorts(
            claim=q.claim,
            execute=q.execute,
            heartbeat=lambda **kw: beats.append(kw),
            record_event=lambda **kw: events.append(kw),
        ),
        daemon_id="d1",
        _sleep=lambda s: None,
    )
    assert d.once() is TaskOutcome.COMPLETED
    assert [b["status"] for b in beats] == ["RUNNING", "IDLE"]
    assert beats[0]["task_ref"] == "t1"
    assert events == []


def test_injected_event_sink_receives_failures():
    events = []
    q = Queue(["t1"])
    q.raises = RuntimeError("boom")
    d = TaskDaemon(
        None,
        DaemonPorts(claim=q.claim, execute=q.execute, record_event=lambda **kw: events.append(kw)),
        daemon_id="d1",
        _sleep=lambda s: None,
    )
    assert d.once() is TaskOutcome.FAILED
    assert events[0]["event_type"] == "task_failed" and events[0]["severity"] == "error"


def test_daemon_with_no_store_and_no_sinks_is_silent_not_broken():
    q = Queue(["t1"])
    d = TaskDaemon(None, DaemonPorts(claim=q.claim, execute=q.execute),
                   daemon_id="d1", _sleep=lambda s: None)
    assert d.once() is TaskOutcome.COMPLETED


def test_loop_stops_claiming_once_shutdown_is_requested():
    """A deploy should not start new work while the service is going down."""
    from agent_core.harness import shutdown as sd

    sd.reset_for_tests()
    try:
        q = Queue(["a", "b", "c"])
        d = TaskDaemon(None, DaemonPorts(claim=q.claim, execute=q.execute),
                       daemon_id="d1", _sleep=lambda s: None)
        original = q.execute

        def execute_then_signal(ref):
            sd._shutting_down.set()
            return original(ref)

        d.ports.execute = execute_then_signal
        d.run_forever()
        assert q.executed == ["a"], "no work claimed after the shutdown signal"
    finally:
        sd.reset_for_tests()
