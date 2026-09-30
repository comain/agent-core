"""Normalizing a turn into one status a product can act on.

A product needs to branch on what happened. Today it would have to read the
provider's own result object, which means every product independently learns
that this provider says `timeout` and that one says `timed_out`, and a
provider exception object ends up on a workflow's state and then in a
checkpoint. `AgentTurnResult` is the boundary that stops both.

The status precedence is the design's, and it is a precedence rather than a
mapping because several things can be true at once: a turn can be skipped
*and* have timed out. The more specific cause wins, because "skipped" tells an
operator nothing about why.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from agent_core.harness.node import NodeOutcome
from agent_core.harness.sessions import AgentSessionRef, SessionSnapshot
from agent_core.harness.turn_result import AgentTurnResult, normalize_turn_outcome


class FakeTurn:
    """The shape a harness result has: a `type`, and provider-specific extras."""

    def __init__(self, type="completed", result="answer", **extra):
        self.type = type
        self.result = result
        for key, value in extra.items():
            setattr(self, key, value)


def outcome(status="accepted", *, turn=None, **kw):
    return NodeOutcome(status=status, result=turn if turn is not None else FakeTurn(), **kw)


# -- status precedence -------------------------------------------------------

def test_an_accepted_turn_is_completed():
    assert normalize_turn_outcome(outcome()).status == "completed"


@pytest.mark.parametrize("turn_type,expected", [
    ("cancelled", "cancelled"),
    ("timeout", "timed_out"),
    ("rate_limited", "rate_limited"),
    ("stalled", "stalled"),
    ("error", "failed"),
])
def test_a_provider_type_maps_to_a_neutral_status(turn_type, expected):
    """Providers disagree on spelling; a product should not have to learn each."""
    result = normalize_turn_outcome(outcome("rejected", turn=FakeTurn(type=turn_type)))

    assert result.status == expected


def test_a_specific_cause_outranks_being_skipped():
    """`skipped` says the workflow tolerated it, not what went wrong. An
    operator asking why a unit produced nothing needs the cause."""
    result = normalize_turn_outcome(outcome("skipped", turn=FakeTurn(type="timeout")))

    assert result.status == "timed_out"


def test_a_skip_with_no_specific_cause_is_skipped():
    result = normalize_turn_outcome(outcome("skipped", turn=FakeTurn(type="completed")))

    assert result.status == "skipped"


def test_cancellation_outranks_everything():
    result = normalize_turn_outcome(outcome("skipped", turn=FakeTurn(type="cancelled")))

    assert result.status == "cancelled"


def test_an_unreachable_turn_is_failed_not_completed():
    """No answer ever arrived. The dangerous normalization is the one that
    lets that look like success."""
    result = normalize_turn_outcome(outcome("unreachable", turn=None))

    assert result.status == "failed"


def test_a_rejected_answer_is_failed():
    """An answer arrived and was not good enough -- still not a completion."""
    assert normalize_turn_outcome(outcome("rejected")).status == "failed"


def test_an_unknown_node_status_is_not_reported_as_success():
    """Fail closed: a status added later must not read as completed."""
    assert normalize_turn_outcome(outcome("something_new")).status == "failed"


# -- what crosses the boundary -----------------------------------------------

def test_no_provider_object_survives_normalization():
    """The DTO ends up on graph state and therefore in a checkpoint. A
    provider object there is both unserializable and a data-leak surface."""
    turn = FakeTurn(raw_provider_payload={"api_key": "sk-secret"}, exception=RuntimeError("x"))
    result = normalize_turn_outcome(outcome(turn=turn))

    serialized = json.dumps(dataclasses.asdict(result))
    assert "sk-secret" not in serialized
    assert "RuntimeError" not in serialized


def test_the_result_is_json_safe():
    """`as_dict`, not `dataclasses.asdict`: the scope is an enum, and an enum
    survives `asdict` only to fail at the checkpoint that tries to store it."""
    result = normalize_turn_outcome(
        outcome(turn=FakeTurn()),
        snapshot=SessionSnapshot(
            session_id="s1",
            usage={"input_tokens": 5},
            retrospect={"note": "ok"},
            patch_count=2,
            session_refs=(AgentSessionRef("opencode", "s1"),),
        ),
    )

    assert json.loads(json.dumps(result.as_dict()))["session_refs"] == [
        {"harness": "opencode", "locator": "s1", "scope": "durable"}
    ]


def test_the_result_is_frozen():
    result = normalize_turn_outcome(outcome())
    with pytest.raises(Exception):
        result.status = "completed"  # type: ignore[misc]


def test_an_error_becomes_a_diagnostic_without_its_text():
    """An exception message can quote a prompt, a path, or a credential."""
    result = normalize_turn_outcome(
        outcome("rejected", error="connect failed: password=hunter2 at /Users/me/repo")
    )

    assert "hunter2" not in json.dumps(dataclasses.asdict(result))
    assert result.diagnostics


# -- the session snapshot ----------------------------------------------------

def test_snapshot_fields_are_carried():
    ref = AgentSessionRef("opencode", "sess-1")
    result = normalize_turn_outcome(
        outcome(),
        snapshot=SessionSnapshot(session_id="sess-1", usage={"input_tokens": 10},
                                 retrospect={"stalls": 1}, patch_count=3,
                                 session_refs=(ref,)),
    )

    assert result.session_refs == (ref,)
    assert result.usage == {"input_tokens": 10}
    assert result.retrospective == {"stalls": 1}
    assert result.patch_count == 3


def test_missing_snapshot_data_normalizes_to_empty_not_a_sentinel():
    """A product summing usage across phases must not have to check for None,
    a string, or a provider's own 'unknown' marker."""
    result = normalize_turn_outcome(outcome(), snapshot=None)

    assert result.usage == {}
    assert result.retrospective == {}
    assert result.patch_count == 0
    assert result.session_refs == ()


def test_a_snapshot_with_junk_usage_still_normalizes():
    snapshot = SessionSnapshot(session_id="s", usage={"input_tokens": "lots"}, patch_count=-4)
    result = normalize_turn_outcome(outcome(), snapshot=snapshot)

    json.dumps(dataclasses.asdict(result))  # must not raise
    assert result.patch_count == 0, "a negative patch count is not a count"


# -- the rest ----------------------------------------------------------------

def test_recovered_comes_from_the_outcome():
    """True only when the in-session nudge produced the accepted answer, not
    merely when recovery was attempted."""
    assert normalize_turn_outcome(outcome(recovered=True)).recovered is True
    assert normalize_turn_outcome(outcome()).recovered is False


def test_attempts_are_carried():
    assert normalize_turn_outcome(outcome(attempts=3)).attempts == 3


def test_the_text_is_the_turn_answer():
    result = normalize_turn_outcome(outcome(turn=FakeTurn(result="the answer")))

    assert result.text == "the answer"


def test_a_missing_text_is_empty_not_none():
    result = normalize_turn_outcome(outcome(turn=FakeTurn(result=None)))

    assert result.text == ""


def test_a_cancelled_result_can_be_built_without_a_turn():
    """Pre-turn cancellation has no outcome to normalize, but still needs a
    persistable envelope."""
    result = AgentTurnResult.cancelled()

    assert result.status == "cancelled"
    assert result.attempts == 0
    json.dumps(dataclasses.asdict(result))


def test_an_accepted_answer_is_not_downgraded_by_a_stale_turn_type():
    """Recovery succeeded: the nudge produced an answer the node accepted.

    A literal reading of the precedence list would report `stalled` here,
    because the underlying turn object still says so. That would make a
    product redo an expensive phase whose answer it already has. Acceptance is
    the node's verdict on the whole step, and it wins.
    """
    result = normalize_turn_outcome(
        outcome("accepted", turn=FakeTurn(type="stalled"), recovered=True)
    )

    assert result.status == "completed"
    assert result.recovered is True
    assert result.diagnostics["turn_type"] == "stalled", "the cause stays visible"


def test_cancellation_still_outranks_acceptance():
    """A cancelled task must never report a completion, whatever arrived."""
    result = normalize_turn_outcome(outcome("accepted", turn=FakeTurn(type="cancelled")))

    assert result.status == "cancelled"


def test_a_recoverable_provider_failure_is_reported_as_such():
    """A product cannot retry on another provider if it never learns it could.

    Harnesses classify some failures as worth retrying elsewhere -- a gateway
    resetting a long stream, a rate limit -- and expose that as
    `fallback_eligible`. Normalization dropped it, so the durable path saw only
    "failed" and skipped the phase, where the legacy path would have queued a
    resume on the next provider in the chain. Losing this turns a recoverable
    upstream fault into a run that silently produces nothing.

    Only the flag and a short reason cross; never the provider's error text.
    """
    turn = FakeTurn(type="error")
    turn.fallback_eligible = True
    turn.fallback_reason = "provider_transport_error"

    result = normalize_turn_outcome(outcome("skipped", turn=turn))

    assert result.diagnostics["fallback_eligible"] is True
    assert result.diagnostics["fallback_reason"] == "provider_transport_error"


def test_an_ordinary_failure_is_not_marked_recoverable():
    result = normalize_turn_outcome(outcome("rejected", turn=FakeTurn(type="error")))

    assert "fallback_eligible" not in result.diagnostics


def test_a_fallback_reason_carries_no_provider_error_text():
    """The reason is a short classification, not the underlying message, which
    can quote a request, a path, or a credential."""
    turn = FakeTurn(type="error")
    turn.fallback_eligible = True
    turn.fallback_reason = "connect failed: password=hunter2 host=internal.db"

    result = normalize_turn_outcome(outcome("skipped", turn=turn))

    assert "hunter2" not in json.dumps(dataclasses.asdict(result))
    assert "internal.db" not in json.dumps(dataclasses.asdict(result))


def test_model_attempts_are_normalized_without_provider_error_text():
    turn = FakeTurn(type="completed")
    turn.model_attempts = (
        {
            "model": "token-pool/primary",
            "outcome": "error",
            "fallback_reason": "rate_limit",
            "error": "password=hunter2",
        },
        {
            "model": "token-pool/secondary",
            "outcome": "completed",
            "fallback_reason": "",
        },
    )

    result = normalize_turn_outcome(outcome(turn=turn))

    assert result.diagnostics["model_attempts"] == [
        {
            "model": "token-pool/primary",
            "outcome": "error",
            "fallback_reason": "rate_limit",
        },
        {
            "model": "token-pool/secondary",
            "outcome": "completed",
            "fallback_reason": "",
        },
    ]
    assert "hunter2" not in json.dumps(dataclasses.asdict(result))


def test_model_attempts_keep_only_sanitized_http_evidence():
    turn = FakeTurn(type="completed")
    turn.model_attempts = ({
        "model": "token-pool/primary",
        "outcome": "error",
        "fallback_reason": "provider_auth_failed",
        "http_status": 401,
        "error_code": "invalid_api_key",
        "error_detail": "Missing API key [redacted]",
        "raw_error": "Bearer secret-token",
    },)

    result = normalize_turn_outcome(outcome(turn=turn))

    assert result.diagnostics["model_attempts"] == [{
        "model": "token-pool/primary",
        "outcome": "error",
        "fallback_reason": "provider_auth_failed",
        "http_status": 401,
        "error_code": "invalid_api_key",
        "error_detail": "Missing API key [redacted]",
    }]
    assert "secret-token" not in json.dumps(dataclasses.asdict(result))
