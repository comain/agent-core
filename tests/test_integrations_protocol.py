"""Accepting work from an outside system, and reporting back to it."""

from __future__ import annotations

import json

import pytest

from agent_core.integrations import (
    Outcome,
    Reply,
    ReportResult,
    Trigger,
    TriggerProtocol,
    UnknownProtocolError,
    VerificationFailed,
    available_protocols,
    create_protocol,
    protocol_for,
    register_protocol,
    unregister_protocol,
)


class PipelineProtocol(TriggerProtocol):
    """An orchestrator that nests the request and wants a receipt back."""

    name = "pipeline"

    def __init__(self, secret: str = ""):
        self.secret = secret
        self.reported = []

    def verify(self, body, headers):
        if self.secret and headers.get("x-signature") != self.secret:
            raise VerificationFailed("bad signature")

    def parse(self, body, headers):
        payload = json.loads(body)
        job = payload.get("job") or {}
        if payload.get("event") == "ping":
            return None
        return Trigger(
            repo_url=job["repo"],
            branch=job["branch"],
            app_name=job.get("app", ""),
            source="pipeline",
            reply_to={"job_id": job["id"]},
        )

    def accepted(self, trigger, *, task_id, task_url, report_url):
        return Reply(status_code=200, body={"code": 0, "jobId": trigger.reply_to["job_id"],
                                           "taskId": task_id, "url": report_url})

    def failed(self, exc):
        return Reply(status_code=200, body={"code": -1, "message": str(exc)})

    def report(self, reply_to, outcome):
        self.reported.append((reply_to["job_id"], outcome.passed))
        return ReportResult(delivered=True)

    def recognises(self, headers):
        return "x-pipeline-event" in headers


@pytest.fixture(autouse=True)
def _clean():
    yield
    for name in ("pipeline", "other"):
        unregister_protocol(name)


def test_a_trigger_is_parsed_into_terms_every_product_shares():
    body = json.dumps({"job": {"id": "j1", "repo": "git@x/y.git", "branch": "main", "app": "demo"}}).encode()
    trigger = PipelineProtocol().parse(body, {})

    assert trigger.repo_url == "git@x/y.git"
    assert trigger.branch == "main"
    assert trigger.source == "pipeline"


def test_what_is_needed_to_answer_later_travels_with_the_trigger():
    """Reporting must not depend on the original request still being around.

    It happens minutes or hours later, in another process.
    """
    body = json.dumps({"job": {"id": "j1", "repo": "r", "branch": "b"}}).encode()
    trigger = PipelineProtocol().parse(body, {})
    assert trigger.reply_to == {"job_id": "j1"}


def test_an_event_worth_ignoring_is_not_a_failure():
    """A webhook fires for many things and most of them are not work."""
    assert PipelineProtocol().parse(json.dumps({"event": "ping"}).encode(), {}) is None


def test_ignoring_has_a_default_answer():
    assert PipelineProtocol().ignored().status_code == 200


def test_a_forged_request_is_refused():
    protocol = PipelineProtocol(secret="s3cret")
    with pytest.raises(VerificationFailed):
        protocol.verify(b"{}", {"x-signature": "wrong"})
    protocol.verify(b"{}", {"x-signature": "s3cret"})


def test_verification_is_off_unless_a_protocol_opts_in():
    """Honest for an internal orchestrator, wrong for anything public."""

    class Trusting(PipelineProtocol):
        pass

    Trusting().verify(b"{}", {})


def test_a_failure_is_answered_in_the_callers_format():
    """Some callers read the outcome from the body and ignore the status."""
    reply = PipelineProtocol().failed(ValueError("nope"))
    assert reply.status_code == 200
    assert reply.body["code"] == -1


def test_the_outcome_goes_home_through_the_same_protocol():
    protocol = PipelineProtocol()
    result = protocol.report({"job_id": "j1"}, Outcome(passed=False, summary="two blockers"))

    assert result.delivered
    assert protocol.reported == [("j1", False)]


def test_a_task_with_nowhere_to_report_is_recognised():
    assert not PipelineProtocol().can_report({})
    assert PipelineProtocol().can_report({"job_id": "j1"})


# -- registration ----------------------------------------------------------------


def test_a_protocol_is_asked_for_by_name():
    register_protocol("pipeline", PipelineProtocol)
    assert isinstance(create_protocol("pipeline"), PipelineProtocol)
    assert "pipeline" in available_protocols()


def test_an_unknown_protocol_says_what_is_available():
    register_protocol("pipeline", PipelineProtocol)
    with pytest.raises(UnknownProtocolError, match="available: "):
        create_protocol("nope")


def test_registering_twice_is_refused():
    register_protocol("pipeline", PipelineProtocol)
    with pytest.raises(ValueError, match="already registered"):
        register_protocol("pipeline", PipelineProtocol)


def test_a_shared_endpoint_can_pick_the_protocol_from_the_headers():
    pipeline = PipelineProtocol()

    class Other(PipelineProtocol):
        def recognises(self, headers):
            return "x-other-event" in headers

    assert protocol_for({"x-pipeline-event": "run"}, [pipeline, Other()]) is pipeline
    assert protocol_for({"x-nothing": "1"}, [pipeline, Other()]) is None


def test_a_protocol_with_its_own_route_need_not_recognise_headers():
    class RouteOnly(PipelineProtocol):
        recognises = None

    assert protocol_for({"x-pipeline-event": "1"}, [RouteOnly()]) is None
