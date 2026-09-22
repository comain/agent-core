"""RDC's wire format, which two products had implemented separately."""

from __future__ import annotations

import json

import pytest

from agent_core.integrations import Outcome, ReportResult, Trigger
from agent_core.integrations.rdc import (
    STATE_FAILED,
    STATE_PASSED,
    RdcProtocol,
    build_rdc_ack,
    parse_rdc_payload,
)

NESTED = {
    "attribute": {
        "appName": "demo",
        "gitRepositoryPath": "git@e/r.git",
        "branch": "main",
        "taskId": 100,
        "recordId": 200,
        "parentId": 300,
        "taskTemplateId": 400,
    },
    "data": {"extra": 1},
    "operator": "someone",
}


# -- inbound ---------------------------------------------------------------------


def test_the_request_is_read_from_the_nested_envelope():
    trigger = parse_rdc_payload(NESTED)

    assert trigger.repo_url == "git@e/r.git"
    assert trigger.branch == "main"
    assert trigger.app_name == "demo"
    assert trigger.source == "rdc"
    assert trigger.metadata == {"extra": 1}


def test_the_repository_has_two_accepted_spellings():
    """Callers send either; handling one means reviews that never start."""
    by_url = parse_rdc_payload({"attribute": {"gitUrl": "git@e/r.git", "branch": "b"}})
    by_path = parse_rdc_payload({"attribute": {"gitRepositoryPath": "git@e/r.git", "branch": "b"}})
    assert by_url.repo_url == by_path.repo_url == "git@e/r.git"


def test_fields_are_accepted_at_the_top_level_too():
    flat = parse_rdc_payload({"gitUrl": "git@e/r.git", "branch": "main", "taskId": 7})
    assert flat.repo_url == "git@e/r.git"
    assert flat.reply_to["task_id"] == "7"


def test_identifiers_are_strings_however_they_arrive():
    """RDC sends them as numbers; they are matched as text on the way back."""
    trigger = parse_rdc_payload(NESTED)
    assert trigger.reply_to["task_id"] == "100"
    assert trigger.reply_to["record_id"] == "200"


def test_what_the_ack_will_need_travels_with_the_trigger():
    assert parse_rdc_payload(NESTED).reply_to == {
        "task_id": "100",
        "record_id": "200",
        "task_template_id": "400",
        "parent_id": "300",
        "operator": "someone",
    }


def test_a_request_without_a_repository_is_refused():
    with pytest.raises(ValueError, match="repository and a branch"):
        parse_rdc_payload({"attribute": {"branch": "main"}})


def test_a_malformed_attribute_is_refused():
    with pytest.raises(ValueError, match="attribute must be an object"):
        parse_rdc_payload({"attribute": "not an object"})


# -- outbound --------------------------------------------------------------------


def test_the_ack_reports_pass_and_fail_with_rdcs_own_codes():
    reply_to = parse_rdc_payload(NESTED).reply_to
    assert build_rdc_ack(reply_to, Outcome(passed=True))["state"] == STATE_PASSED
    assert build_rdc_ack(reply_to, Outcome(passed=False))["state"] == STATE_FAILED


def test_the_ack_carries_the_identifiers_back():
    ack = build_rdc_ack(parse_rdc_payload(NESTED).reply_to,
                        Outcome(passed=False, report_url="http://r/t1"))

    assert ack["attribute"] == {
        "taskId": "100",
        "recordId": "200",
        "taskTemplateId": "400",
        "parentId": "300",
        "url": "http://r/t1",
        "reportUrl": "http://r/t1",
        "operator": "someone",
    }


def test_what_a_product_reports_goes_in_the_data_bag():
    """A review sends a score; a test generator sends coverage."""
    ack = build_rdc_ack({}, Outcome(passed=True, details={"score": "42", "passed": "true"}))
    assert ack["data"] == {"score": "42", "passed": "true"}


def test_reporting_needs_both_identifiers():
    """RDC matches an ack to a pipeline step by the pair."""
    protocol = RdcProtocol(ack_url="http://rdc/ack")
    assert not protocol.can_report({"task_id": "1"})
    assert not protocol.can_report({"record_id": "2"})
    assert protocol.can_report({"task_id": "1", "record_id": "2"})


def test_reporting_without_a_configured_url_is_refused_not_attempted():
    result = RdcProtocol().report({"task_id": "1", "record_id": "2"}, Outcome(passed=True))
    assert not result.delivered
    assert "no RDC ack target" in result.error


def test_the_ack_is_posted_to_the_configured_url():
    sent = {}

    def send(url, body):
        sent["url"], sent["body"] = url, body
        return ReportResult(delivered=True)

    protocol = RdcProtocol(ack_url="http://rdc/ack", send=send)
    result = protocol.report(parse_rdc_payload(NESTED).reply_to, Outcome(passed=True))

    assert result.delivered
    assert sent["url"] == "http://rdc/ack"
    assert sent["body"]["state"] == STATE_PASSED


# -- synchronous replies ---------------------------------------------------------


def test_a_reply_is_always_http_200_with_the_outcome_in_the_body():
    """RDC reads the body and ignores the status code."""
    reply = RdcProtocol().failed(ValueError("bad payload"))
    assert reply.status_code == 200
    assert reply.body["status"] == -1
    assert reply.body["msg"] == "bad payload"


def test_the_duplicate_keys_are_kept():
    """Different callers read `errcode` or `message`; dropping either is silent."""
    body = RdcProtocol().result_body(0, "处理中", {})
    assert body["errcode"] == body["status"]
    assert body["message"] == body["msg"]


def test_an_accepted_trigger_answers_with_the_task_and_its_links():
    reply = RdcProtocol().accepted(
        parse_rdc_payload(NESTED), task_id="t1", task_url="http://s/t1", report_url="http://r/t1"
    )
    assert reply.body["status"] == 0
    assert reply.body["data"]["taskId"] == "t1"
    assert reply.body["data"]["reportUrl"] == "http://r/t1"
