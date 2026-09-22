"""The event envelope: one shape, and a reader that ignores what it cannot read."""

from __future__ import annotations

import pytest

from agent_core.runtime import (
    Event,
    RuntimeStore,
    group_by_call,
    parse_event,
    parse_events,
    register_kinds,
)


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(tmp_path / "r.db")
    s.init()
    return s


def test_an_event_carries_its_attribution_and_correlation(store):
    store.append_event(
        task_ref="t",
        event_type="agent_progress",
        message="read a file",
        stage="build",
        source="agent",
        call_id="call-1",
    )
    event = parse_event(store.events_since(task_ref="t")[0])
    assert event.kind == "agent_progress"
    assert event.source == "agent" and event.call_id == "call-1"
    assert event.stage == "build" and event.known


def test_an_unknown_kind_is_delivered_not_refused(store):
    """A page rendered by yesterday's deploy must survive today's server."""
    store.append_event(task_ref="t", event_type="something_new", message="hello")
    event = parse_event(store.events_since(task_ref="t")[0])
    assert event.kind == "something_new"
    assert event.known is False
    assert event.message == "hello"


def test_a_product_declares_its_own_kinds(store):
    register_kinds(["plan_task"])
    store.append_event(task_ref="t", event_type="plan_task", message="task 1: done")
    assert parse_event(store.events_since(task_ref="t")[0]).known is True


def test_a_malformed_payload_costs_that_payload_not_the_stream(store):
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO ac_task_events (task_ref, event_type, severity, message,"
            " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("t", "agent_progress", "info", "broken", "{not json", "2026-01-01T00:00:00Z"),
        )
    event = parse_event(store.events_since(task_ref="t")[0])
    assert event.data == {}
    assert event.message == "broken"


def test_an_unattributed_source_falls_back_rather_than_lying(store):
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO ac_task_events (task_ref, event_type, severity, message,"
            " payload_json, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("t", "agent_progress", "info", "x", "{}", "martian", "2026-01-01T00:00:00Z"),
        )
    assert parse_event(store.events_since(task_ref="t")[0]).source == "agent"


def test_work_is_grouped_by_the_call_it_belongs_to(store):
    for call, message in (("c1", "run tests"), ("c2", "read file"), ("c1", "tests failed")):
        store.append_event(task_ref="t", event_type="agent_progress", message=message, call_id=call)
    store.append_event(task_ref="t", event_type="run_started", message="not part of a call")

    grouped = group_by_call(parse_events(store.events_since(task_ref="t")))
    assert set(grouped) == {"c1", "c2"}
    assert [e.message for e in grouped["c1"]] == ["run tests", "tests failed"]


def test_events_can_be_asked_for_by_kind_stage_and_call(store):
    store.append_event(task_ref="t", event_type="agent_progress", message="a", stage="build")
    store.append_event(task_ref="t", event_type="agent_progress", message="b", stage="review")
    store.append_event(task_ref="t", event_type="gate_opened", message="c", stage="review")
    store.append_event(task_ref="t", event_type="agent_progress", message="d", call_id="c9")

    by_kind = store.events_since(task_ref="t", kinds=["gate_opened"])
    assert [r["message"] for r in by_kind] == ["c"]
    by_stage = store.events_since(task_ref="t", stages=["build"])
    assert [r["message"] for r in by_stage] == ["a"]
    by_call = store.events_since(task_ref="t", call_id="c9")
    assert [r["message"] for r in by_call] == ["d"]


def test_the_tail_of_a_long_run_is_reachable_without_paging_to_it(store):
    for n in range(50):
        store.append_event(task_ref="t", event_type="agent_progress", message=f"e{n}")
    newest = store.events_since(task_ref="t", limit=3, newest_first=True)
    assert [r["message"] for r in newest] == ["e49", "e48", "e47"]
