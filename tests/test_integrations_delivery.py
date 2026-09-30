"""Delivering a result to a system that may not answer the first time."""

from __future__ import annotations

import pytest

from agent_core.integrations.delivery import deliver_json


class Response:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class Client:
    """Stands in for httpx.Client, scripted per attempt."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.posts = []

    def __call__(self, **kwargs):
        self.timeout = kwargs.get("timeout")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self.posts.append({"url": url, "headers": headers, "json": json})
        outcome = self.outcomes[min(len(self.posts), len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _deliver(client, **kwargs):
    slept = []
    result = deliver_json(
        "http://target/ack", {"state": 0},
        client_factory=client, sleep=slept.append, **kwargs,
    )
    return result, slept


def test_a_delivery_that_lands_first_time_does_not_retry():
    client = Client(Response())
    result, slept = _deliver(client, attempts=3)

    assert result.delivered
    assert len(client.posts) == 1
    assert slept == []


def test_a_failure_is_retried_up_to_the_bound():
    client = Client(Response(status_code=500))
    result, _ = _deliver(client, attempts=3)

    assert not result.delivered
    assert len(client.posts) == 3
    assert "after 3 attempt(s)" in result.error


def test_attempts_is_the_total_not_the_retries():
    """Off by one here means one more call than intended at every endpoint."""
    client = Client(Response(status_code=500))
    _deliver(client, attempts=1)
    assert len(client.posts) == 1


def test_a_later_attempt_can_succeed():
    client = Client(Response(status_code=502), Response())
    result, _ = _deliver(client, attempts=3)

    assert result.delivered
    assert len(client.posts) == 2
    assert len(result.attempts) == 2


def test_a_transport_error_is_recorded_and_retried():
    client = Client(RuntimeError("connection reset"), Response())
    result, _ = _deliver(client, attempts=2)

    assert result.delivered
    assert result.attempts[0]["error"] == "connection reset"
    assert "status_code" not in result.attempts[0]


def test_every_attempt_is_recorded_with_what_happened():
    """Persisted and read back when someone asks why a pipeline never heard."""
    client = Client(Response(status_code=500, text="boom"))
    result, _ = _deliver(client, attempts=2)

    assert [a["attempt"] for a in result.attempts] == [1, 2]
    for entry in result.attempts:
        assert entry["status_code"] == 500
        assert entry["body"] == "boom"
        assert isinstance(entry["elapsed_ms"], int)


def test_a_large_response_body_is_bounded():
    """A failing endpoint returning a page of HTML must not fill every task row."""
    client = Client(Response(status_code=500, text="x" * 10_000))
    result, _ = _deliver(client, attempts=1)
    assert len(result.attempts[0]["body"]) == 1000


def test_backoff_grows_but_is_capped():
    client = Client(Response(status_code=500))
    _, slept = _deliver(client, attempts=6)
    assert slept == [1, 2, 3, 3, 3]


def test_there_is_no_pause_after_the_last_attempt():
    """Nothing is left to wait for, and the caller is blocked until this returns."""
    client = Client(Response(status_code=500))
    _, slept = _deliver(client, attempts=3)
    assert len(slept) == 2


def test_a_failed_delivery_is_reported_not_raised():
    """What a failure means -- retry later, or give up -- is the caller's."""
    client = Client(RuntimeError("down"))
    result, _ = _deliver(client, attempts=1)
    assert not result.delivered


def test_extra_headers_are_sent_alongside_the_content_type():
    client = Client(Response())
    _deliver(client, attempts=1, headers={"X-Task-Token": "s3cret"})

    sent = client.posts[0]["headers"]
    assert sent["X-Task-Token"] == "s3cret"
    assert sent["Content-Type"] == "application/json"
