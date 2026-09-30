"""Trigger endpoints, so enabling a protocol is a line of configuration."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_core.integrations import Trigger, VerificationFailed
from agent_core.integrations.gitlab_webhook import GitLabProtocol
from agent_core.integrations.rdc import RdcProtocol
from agent_core.integrations.router import Accepted, TriggerMount, create_trigger_router

RDC_BODY = {
    "attribute": {"appName": "demo", "gitUrl": "git@e/r.git", "branch": "main",
                  "taskId": 100, "recordId": 200},
}


def build(mounts, submit=None):
    submitted = []

    def default_submit(trigger):
        submitted.append(trigger)
        return Accepted(task_id="t1", task_url="http://s/t1", report_url="http://r/t1")

    app = FastAPI()
    app.include_router(create_trigger_router(mounts, submit=submit or default_submit))
    return TestClient(app, raise_server_exceptions=False), submitted


def test_a_trigger_starts_work_and_is_answered_in_the_callers_format():
    client, submitted = build([TriggerMount(RdcProtocol(), "/api/v1/rdc/trigger")])

    response = client.post("/api/v1/rdc/trigger", json=RDC_BODY)

    assert response.status_code == 200
    assert response.json()["status"] == 0
    assert response.json()["data"]["taskId"] == "t1"
    assert submitted[0].repo_url == "git@e/r.git"


def test_an_ignorable_event_is_not_an_error_and_starts_nothing():
    """Answering red would fill a pipeline's UI with failures that are not."""
    client, submitted = build([TriggerMount(GitLabProtocol(secret="s"), "/hook")])

    response = client.post(
        "/hook",
        headers={"X-Gitlab-Token": "s"},
        json={"object_kind": "note", "project": {}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert submitted == []


def test_an_unverified_request_never_reaches_the_product():
    """The check is the endpoint's job, not something each product remembers."""
    client, submitted = build([TriggerMount(GitLabProtocol(secret="s3cret"), "/hook")])

    response = client.post("/hook", headers={"X-Gitlab-Token": "wrong"}, json={})

    assert response.status_code == 401
    assert submitted == []


def test_a_malformed_body_is_answered_in_the_callers_format():
    client, _ = build([TriggerMount(RdcProtocol(), "/api/v1/rdc/trigger")])

    response = client.post(
        "/api/v1/rdc/trigger", content=b"{not json", headers={"content-type": "application/json"}
    )

    # RDC reads the outcome from the body, so a rejection is still HTTP 200.
    assert response.status_code == 200
    assert response.json()["status"] == -1


def test_a_failure_starting_the_work_is_reported_to_the_caller():
    """A caller that cannot read the error cannot act on it."""

    def explode(trigger):
        raise RuntimeError("queue is down")

    client, _ = build([TriggerMount(RdcProtocol(), "/api/v1/rdc/trigger")], submit=explode)

    response = client.post("/api/v1/rdc/trigger", json=RDC_BODY)

    assert response.status_code == 200
    assert response.json()["status"] == -1
    assert "queue is down" in response.json()["msg"]


def test_each_protocol_answers_in_its_own_way_on_one_app():
    """The differences between callers live in the protocol, not the route."""
    client, _ = build([
        TriggerMount(RdcProtocol(), "/api/v1/rdc/trigger"),
        TriggerMount(GitLabProtocol(secret="s"), "/hook"),
    ])

    rdc = client.post("/api/v1/rdc/trigger", json=RDC_BODY)
    gitlab = client.post(
        "/hook",
        headers={"X-Gitlab-Token": "s"},
        json={"object_kind": "push", "ref": "refs/heads/main", "checkout_sha": "abc",
              "project": {"id": 1, "git_ssh_url": "git@e/r.git"}},
    )

    assert rdc.status_code == 200 and rdc.json()["status"] == 0
    assert gitlab.status_code == 202 and gitlab.json()["status"] == "accepted"


def test_the_product_may_ask_for_the_request_as_well():
    """For building absolute URLs from the host the call arrived on."""
    seen = {}

    def submit(trigger, request):
        seen["host"] = request.headers.get("host")
        return Accepted(task_id="t1")

    client, _ = build([TriggerMount(RdcProtocol(), "/api/v1/rdc/trigger")], submit=submit)
    client.post("/api/v1/rdc/trigger", json=RDC_BODY)

    assert seen["host"]


def test_a_type_error_from_the_product_is_not_retried():
    """Catching TypeError to detect the signature would call it twice."""
    calls = []

    def submit(trigger):
        calls.append(1)
        raise TypeError("product bug: unsupported operand")

    client, _ = build([TriggerMount(RdcProtocol(), "/api/v1/rdc/trigger")], submit=submit)
    response = client.post("/api/v1/rdc/trigger", json=RDC_BODY)

    assert calls == [1], "the product's own TypeError must not look like a signature mismatch"
    assert response.json()["status"] == -1


def test_two_mounts_of_one_protocol_are_distinguishable():
    client, _ = build([
        TriggerMount(RdcProtocol(), "/api/v1/rdc/trigger", name="rdc-primary"),
        TriggerMount(RdcProtocol(), "/api/v1/legacy/rdc", name="rdc-legacy"),
    ])

    assert client.post("/api/v1/rdc/trigger", json=RDC_BODY).status_code == 200
    assert client.post("/api/v1/legacy/rdc", json=RDC_BODY).status_code == 200
