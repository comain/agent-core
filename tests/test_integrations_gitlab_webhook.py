"""GitLab webhooks: mostly deciding what is not work."""

from __future__ import annotations

import json

import pytest

from agent_core.integrations import Outcome, ReportResult, VerificationFailed
from agent_core.integrations.gitlab_webhook import (
    GitLabProtocol,
    build_commit_status,
    parse_gitlab_event,
)

PROJECT = {
    "id": 42,
    "git_ssh_url": "git@gitlab/group/app.git",
    "git_http_url": "https://gitlab/group/app.git",
    "path_with_namespace": "group/app",
}

PUSH = {
    "object_kind": "push",
    "ref": "refs/heads/feature/x",
    "checkout_sha": "abc123",
    "user_username": "someone",
    "project": PROJECT,
}

MERGE_REQUEST = {
    "object_kind": "merge_request",
    "user": {"username": "someone"},
    "project": PROJECT,
    "object_attributes": {
        "action": "open",
        "iid": 7,
        "source_branch": "feature/x",
        "target_branch": "main",
        "last_commit": {"id": "abc123"},
    },
}


# -- what is work ----------------------------------------------------------------


def test_a_push_is_work():
    trigger = parse_gitlab_event(PUSH)

    assert trigger.repo_url == "git@gitlab/group/app.git"
    assert trigger.branch == "feature/x"
    assert trigger.commit_id == "abc123"
    assert trigger.app_name == "group/app"
    assert trigger.source == "gitlab-push"


def test_a_branch_name_with_slashes_survives():
    """`refs/heads/feature/x` is the branch `feature/x`, not `feature`."""
    assert parse_gitlab_event(PUSH).branch == "feature/x"


def test_an_opened_merge_request_is_work():
    trigger = parse_gitlab_event(MERGE_REQUEST)

    assert trigger.branch == "feature/x"
    assert trigger.commit_id == "abc123"
    assert trigger.source == "gitlab-merge-request"
    assert trigger.metadata["merge_request_iid"] == 7
    assert trigger.metadata["target_branch"] == "main"


@pytest.mark.parametrize("action", ["open", "reopen", "update"])
def test_actions_that_change_the_code_are_work(action):
    payload = json.loads(json.dumps(MERGE_REQUEST))
    payload["object_attributes"]["action"] = action
    assert parse_gitlab_event(payload) is not None


# -- what is not ------------------------------------------------------------------


@pytest.mark.parametrize("action", ["close", "merge", "approved", "unapproved"])
def test_actions_that_do_not_change_the_code_are_ignored(action):
    """Reviewing on these repeats work that already ran."""
    payload = json.loads(json.dumps(MERGE_REQUEST))
    payload["object_attributes"]["action"] = action
    assert parse_gitlab_event(payload) is None


def test_a_deleted_branch_is_ignored():
    """GitLab reports an all-zero sha, and there is nothing left to review."""
    payload = dict(PUSH, checkout_sha="0000000000000000000000000000000000000000")
    assert parse_gitlab_event(payload) is None


def test_a_tag_is_not_a_branch():
    assert parse_gitlab_event(dict(PUSH, ref="refs/tags/v1.0")) is None


def test_other_event_kinds_are_ignored():
    """A webhook fires for comments, pipelines, wiki edits, releases."""
    for kind in ("note", "pipeline", "wiki_page", "release", "issue"):
        assert parse_gitlab_event({"object_kind": kind, "project": PROJECT}) is None


def test_an_ssh_url_is_preferred_but_http_will_do():
    payload = json.loads(json.dumps(PUSH))
    del payload["project"]["git_ssh_url"]
    assert parse_gitlab_event(payload).repo_url == "https://gitlab/group/app.git"


# -- verification -----------------------------------------------------------------


def test_a_correct_token_passes():
    GitLabProtocol(secret="s3cret").verify(b"{}", {"X-Gitlab-Token": "s3cret"})


def test_a_wrong_token_is_refused():
    with pytest.raises(VerificationFailed, match="invalid webhook token"):
        GitLabProtocol(secret="s3cret").verify(b"{}", {"X-Gitlab-Token": "wrong"})


def test_a_missing_token_is_refused():
    with pytest.raises(VerificationFailed, match="missing X-Gitlab-Token"):
        GitLabProtocol(secret="s3cret").verify(b"{}", {})


def test_headers_are_matched_case_insensitively():
    """HTTP headers are case-insensitive; a plain dict is not."""
    GitLabProtocol(secret="s3cret").verify(b"{}", {"x-gitlab-token": "s3cret"})


def test_without_a_secret_every_request_is_refused():
    """An unverified public endpoint that starts work is worse than one that is off.

    The endpoint is reachable by anyone who learns the URL, so an
    unconfigured secret must not mean "trust everyone".
    """
    with pytest.raises(VerificationFailed, match="no webhook secret"):
        GitLabProtocol().verify(b"{}", {"X-Gitlab-Token": "anything"})


def test_a_rejected_webhook_answers_with_a_status_code_gitlab_shows():
    """GitLab records failed deliveries; that is where a bad secret is noticed."""
    protocol = GitLabProtocol(secret="s")
    assert protocol.failed(VerificationFailed("bad token")).status_code == 401
    assert protocol.failed(ValueError("bad json")).status_code == 400


def test_the_protocol_recognises_its_own_deliveries():
    assert GitLabProtocol().recognises({"X-Gitlab-Event": "Push Hook"})
    assert not GitLabProtocol().recognises({"X-Other": "1"})


# -- reporting --------------------------------------------------------------------


def test_the_result_is_posted_as_a_commit_status():
    sent = {}

    def send(path, body):
        sent["path"], sent["body"] = path, body
        return ReportResult(delivered=True)

    protocol = GitLabProtocol(secret="s", send=send)
    result = protocol.report(
        parse_gitlab_event(PUSH).reply_to,
        Outcome(passed=False, summary="two blockers", report_url="http://r/t1"),
    )

    assert result.delivered
    assert sent["path"] == "/projects/42/statuses/abc123"
    assert sent["body"]["state"] == "failed"
    assert sent["body"]["target_url"] == "http://r/t1"
    assert sent["body"]["ref"] == "feature/x"


def test_a_passing_review_reports_success():
    assert build_commit_status(Outcome(passed=True))["state"] == "success"


def test_a_long_summary_is_trimmed():
    """GitLab truncates the description, which would hide the link."""
    body = build_commit_status(Outcome(passed=True, summary="x" * 500))
    assert len(body["description"]) == 140


def test_reporting_without_a_commit_is_refused_not_attempted():
    result = GitLabProtocol(secret="s").report({}, Outcome(passed=True))
    assert not result.delivered
    assert "no GitLab commit" in result.error
