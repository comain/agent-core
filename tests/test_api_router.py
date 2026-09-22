"""Tests for the shared task-service router, driven through a real ASGI client."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_core.api.router import TaskServicePorts, create_task_router
from agent_core.identity import IdentityResolver, Policy, ServiceTokenResolver
from agent_core.runtime import RuntimeStore

USER = {"X-Forwarded-User": "alice"}


@pytest.fixture
def store(tmp_path):
    s = RuntimeStore(tmp_path / "rt.db")
    s.init()
    return s


@pytest.fixture
def tasks():
    return {"t1": {"task_id": "t1", "status": "running"}}


def build(store, tasks=None, **kw):
    ports = TaskServicePorts(
        get_task=(lambda tid: (tasks or {}).get(tid)),
        list_tasks=(lambda limit, status: list((tasks or {}).values())),
        health=lambda: {"version": "test"},
    ) if tasks is not None else TaskServicePorts()
    app = FastAPI()
    app.include_router(create_task_router(store, ports=ports, **kw))
    return TestClient(app)


# -- health --------------------------------------------------------------------


def test_health(store, tasks):
    r = build(store, tasks).get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["version"] == "test"


def test_health_reports_degraded_rather_than_500(store):
    """A 500 tells a load balancer nothing useful."""
    def boom():
        raise RuntimeError("db gone")
    app = FastAPI()
    app.include_router(create_task_router(store, ports=TaskServicePorts(health=boom)))
    r = TestClient(app).get("/health")
    assert r.status_code == 200 and r.json()["status"] == "degraded"


def test_healthcheck_html(store, tasks):
    r = build(store, tasks).get("/healthcheck.html")
    assert r.status_code == 200 and "ok" in r.text


# -- tasks ---------------------------------------------------------------------


def test_get_task(store, tasks):
    assert build(store, tasks).get("/api/v1/tasks/t1").json()["status"] == "running"


def test_unknown_task_is_404(store, tasks):
    assert build(store, tasks).get("/api/v1/tasks/nope").status_code == 404


def test_missing_port_is_501_not_404(store):
    """'Not implemented here' must stay distinguishable from 'wrong URL'."""
    r = build(store).get("/api/v1/tasks/t1")
    assert r.status_code == 501


def test_list_tasks(store, tasks):
    assert len(build(store, tasks).get("/api/v1/tasks").json()) == 1


@pytest.mark.parametrize("limit", [0, -1, 501])
def test_list_tasks_rejects_unbounded_or_nonpositive_limits(store, tasks, limit):
    assert build(store, tasks).get(f"/api/v1/tasks?limit={limit}").status_code == 422


# -- control mutations ---------------------------------------------------------


def test_control_requires_authentication(store, tasks):
    r = build(store, tasks).post("/api/v1/tasks/t1/stop")
    assert r.status_code == 401


def test_control_records_and_attributes(store, tasks):
    r = build(store, tasks).post("/api/v1/tasks/t1/cancel?reason=oops", headers=USER)
    assert r.status_code == 200
    assert r.json()["requested_by"] == "alice"
    pending = store.pending_controls(task_ref="t1")
    assert len(pending) == 1
    assert pending[0]["action"] == "cancel" and pending[0]["requested_by"] == "alice"
    assert "task_cancel_requested" in [e["event_type"] for e in store.events_since(task_ref="t1")]


def test_unknown_control_action_is_404(store, tasks):
    assert build(store, tasks).post("/api/v1/tasks/t1/explode", headers=USER).status_code == 404


def test_service_principal_may_mutate_tasks(store, tasks):
    client = build(
        store, tasks,
        resolver=IdentityResolver(service=ServiceTokenResolver({"rdc": "tok"})),
    )
    r = client.post("/api/v1/tasks/t1/requeue", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.json()["requested_by"] == "rdc"


# -- events stream -------------------------------------------------------------


def test_event_stream_emits_and_terminates(store, tasks):
    store.append_event(task_ref="t1", event_type="progress", message="working")
    store.append_event(task_ref="t1", event_type="task_completed", message="done")
    r = build(store, tasks).get("/api/v1/tasks/t1/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no"   # or nginx buffers it into looking frozen
    assert "working" in r.text and "event: task_completed" in r.text


def test_event_stream_rejects_unknown_task(store, tasks):
    assert build(store, tasks).get("/api/v1/tasks/missing/events").status_code == 404


def test_last_event_id_header_resumes(store, tasks):
    first = store.append_event(task_ref="t1", event_type="progress", message="ALPHA")
    store.append_event(task_ref="t1", event_type="task_completed", message="OMEGA")
    r = build(store, tasks).get("/api/v1/tasks/t1/events", headers={"Last-Event-ID": str(first)})
    assert "ALPHA" not in r.text, "events at or before the cursor must not be resent"
    assert "OMEGA" in r.text


def test_malformed_last_event_id_is_ignored(store, tasks):
    store.append_event(task_ref="t1", event_type="task_completed", message="OMEGA")
    r = build(store, tasks).get("/api/v1/tasks/t1/events", headers={"Last-Event-ID": "garbage"})
    assert "OMEGA" in r.text


# -- approval inbox ------------------------------------------------------------


def test_inbox_page_lists_gates(store, tasks):
    store.open_gate(task_ref="t1", node="design_review", kind="input", prompt={"design": "x"})
    r = build(store, tasks).get("/gates")
    assert r.status_code == 200 and "design_review" in r.text


def test_inbox_data_is_json(store, tasks):
    store.open_gate(task_ref="t1", node="design_review", kind="input", prompt={"design": "x"})
    body = build(store, tasks).get("/gates/data").json()
    assert body[0]["node"] == "design_review" and "waited" in body[0]


def test_answer_gate_requires_authentication(store, tasks):
    gate = store.open_gate(task_ref="t1", node="n", kind="approve", prompt={})
    r = build(store, tasks).post(f"/gates/{gate.gate_id}/answer", json={"decision": "approve"})
    assert r.status_code == 401
    assert store.get_gate(gate.gate_id).state == "pending"


def test_answer_gate_via_json(store, tasks):
    gate = store.open_gate(task_ref="t1", node="design_review", kind="input", prompt={})
    r = build(store, tasks).post(
        f"/gates/{gate.gate_id}/answer",
        json={"decision": "approve", "comments": "ship"},
        headers=USER,
    )
    assert r.status_code == 200
    answered = store.get_gate(gate.gate_id)
    assert answered.state == "answered" and answered.answered_by == "alice"
    assert answered.response == {"decision": "approve", "comments": "ship"}


def test_answer_gate_via_form_redirects_back_to_the_inbox(store, tasks):
    gate = store.open_gate(task_ref="t1", node="design_review", kind="input", prompt={})
    r = build(store, tasks).post(
        f"/gates/{gate.gate_id}/answer",
        data={"gate_id": gate.gate_id, "decision": "approve", "comments": "ok"},
        headers=USER,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/gates"
    assert store.get_gate(gate.gate_id).response == {"decision": "approve", "comments": "ok"}


def test_double_answer_is_409_not_400(store, tasks):
    """The request was well formed; it lost a race with another reviewer."""
    gate = store.open_gate(task_ref="t1", node="n", kind="approve", prompt={})
    client = build(store, tasks)
    first = client.post(f"/gates/{gate.gate_id}/answer", json={"decision": "approve"}, headers=USER)
    second = client.post(f"/gates/{gate.gate_id}/answer", json={"decision": "reject"}, headers=USER)
    assert first.status_code == 200 and second.status_code == 409


def test_answer_unknown_gate_is_404(store, tasks):
    r = build(store, tasks).post("/gates/nope/answer", json={"decision": "approve"}, headers=USER)
    assert r.status_code == 404


def test_service_principal_cannot_answer_a_gate(store, tasks):
    """A pipeline approving its own gate defeats the gate."""
    gate = store.open_gate(task_ref="t1", node="n", kind="approve", prompt={})
    client = build(store, tasks, resolver=IdentityResolver(service=ServiceTokenResolver({"rdc": "tok"})))
    r = client.post(f"/gates/{gate.gate_id}/answer", json={"decision": "approve"},
                    headers={"Authorization": "Bearer tok"})
    assert r.status_code == 403
    assert store.get_gate(gate.gate_id).state == "pending"


def test_required_groups_enforced_through_http(store, tasks):
    gate = store.open_gate(task_ref="t1", node="n", kind="approve", prompt={})
    client = build(store, tasks, policy=Policy(required_groups=frozenset({"reviewers"})))
    denied = client.post(f"/gates/{gate.gate_id}/answer", json={"decision": "approve"},
                         headers={"X-Forwarded-User": "dave", "X-Forwarded-Groups": "eng"})
    assert denied.status_code == 403
    allowed = client.post(f"/gates/{gate.gate_id}/answer", json={"decision": "approve"},
                          headers={"X-Forwarded-User": "erin", "X-Forwarded-Groups": "eng,reviewers"})
    assert allowed.status_code == 200


def test_cancel_gate(store, tasks):
    gate = store.open_gate(task_ref="t1", node="n", kind="approve", prompt={})
    r = build(store, tasks).post(f"/gates/{gate.gate_id}/cancel", headers=USER)
    assert r.json()["cancelled"] is True
    assert store.get_gate(gate.gate_id).state == "cancelled"


# -- operations ----------------------------------------------------------------


def test_running_reports_runners_and_staleness(store, tasks):
    store.heartbeat(runner_id="r1", status="RUNNING", task_ref="t1")
    body = build(store, tasks).get("/api/v1/admin/running").json()
    assert body["runners"][0]["runner_id"] == "r1"
    assert body["stale_count"] == 0

    conn = store.connect()
    conn.execute("UPDATE ac_runner_heartbeats SET heartbeat_at='2000-01-01T00:00:00+00:00'")
    conn.commit(); conn.close()
    body = build(store, tasks).get("/api/v1/admin/running").json()
    assert body["stale_count"] == 1 and body["runners"][0]["stale"] is True


def test_running_rejects_nonpositive_staleness_window(store, tasks):
    assert build(store, tasks).get(
        "/api/v1/admin/running?stale_after_seconds=0"
    ).status_code == 422


# -- mounting ------------------------------------------------------------------


def test_prefix_lets_a_product_keep_its_base_path(store, tasks):
    app = FastAPI()
    app.include_router(create_task_router(store, prefix="/cr-agent"))
    client = TestClient(app)
    assert client.get("/cr-agent/healthz").status_code == 200
    assert client.get("/healthz").status_code == 404


def test_inbox_redirect_respects_the_prefix(store):
    app = FastAPI()
    app.include_router(create_task_router(store, prefix="/dfa"))
    client = TestClient(app)
    gate = store.open_gate(task_ref="t1", node="n", kind="approve", prompt={})
    r = client.post(f"/dfa/gates/{gate.gate_id}/answer",
                    data={"decision": "approve"}, headers=USER, follow_redirects=False)
    assert r.headers["location"] == "/dfa/gates"


def test_the_public_names_are_importable_from_the_package():
    """A consumer must not have to reach into `agent_core.api.router`.

    The package advertised a lazy export that was never implemented, so this
    import failed and the private submodule path was the only one that worked.
    """
    from agent_core.api import TaskServicePorts, create_task_router

    assert callable(create_task_router)
    assert TaskServicePorts().get_task is None


def test_an_unknown_attribute_still_raises_attribute_error():
    import agent_core.api as api

    with pytest.raises(AttributeError, match="no attribute"):
        api.does_not_exist


def test_controlling_an_unknown_task_is_a_404_and_records_nothing(tmp_path):
    """A typo in a task id used to 500 and leave an audit record behind.

    The control was written before anything checked the task existed, so the
    store ended up describing a task that never was.
    """
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    store.init()
    ports = TaskServicePorts(get_task=lambda task_id: None, request_control=lambda **kw: None)
    app = FastAPI()
    app.include_router(create_task_router(store, ports=ports, policy=Policy(allow_anonymous=True)))

    response = TestClient(app).post("/api/v1/tasks/nope/stop")

    assert response.status_code == 404
    conn = store.connect()
    try:
        assert list(conn.execute("SELECT * FROM ac_task_controls")) == []
        assert list(conn.execute("SELECT * FROM ac_task_events")) == []
    finally:
        conn.close()


def test_a_port_may_signal_an_unknown_task_when_there_is_no_get_task():
    """Not every product can answer get_task cheaply; KeyError is the fallback."""

    def refuse(**kwargs):
        raise KeyError("unknown task: nope")

    store_path = tempfile.mkdtemp()
    store = RuntimeStore(Path(store_path) / "runtime.sqlite3")
    store.init()
    app = FastAPI()
    app.include_router(
        create_task_router(
            store,
            ports=TaskServicePorts(request_control=refuse),
            policy=Policy(allow_anonymous=True),
        )
    )

    assert TestClient(app).post("/api/v1/tasks/nope/stop").status_code == 404
