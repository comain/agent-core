"""Tests for provider fallback orchestration."""

from __future__ import annotations

from typing import List

import pytest

from agent_core.harness import TurnResult, run_turn_with_fallback, should_skip_provider
from agent_core.harness.tiered_router import reset_model_health


@pytest.fixture(autouse=True)
def _reset_health():
    reset_model_health()
    yield
    reset_model_health()


class FakeProcess:
    """Records which models were attempted and returns scripted results."""

    def __init__(self, results):
        self.results = list(results)
        self.attempts: List[str] = []
        self.calls: List[dict] = []

    def run_turn(self, message=None, **kw):
        self.attempts.append(kw["model_id"])
        self.calls.append({"message": message, **kw})
        return self.results.pop(0) if self.results else TurnResult(type="completed")


def ok(model=None):
    return TurnResult(type="completed", result="done", model_id=model)


def failed(reason="provider_error", error=None):
    return TurnResult(type="error", fallback_eligible=True, fallback_reason=reason, error=error or {})


CHAIN = ["a/m1", "a/m2", "b/m1"]


def test_model_rejection_keeps_same_provider_fallback_available():
    from agent_core.harness.process import classify_provider_model_error

    error = {"data": {"statusCode": 401, "message": "Your model id does not exist, recognized as k3."}}
    rejection = failed(classify_provider_model_error(error), error)
    assert not should_skip_provider(rejection)
    proc = FakeProcess([rejection, ok()])
    assert run(proc).type == "completed"
    assert proc.attempts == ["a/m1", "a/m2"]


def test_partial_usage_timeout_advances_to_next_model():
    proc = FakeProcess([
        TurnResult(type="timeout", fallback_eligible=True, fallback_reason="timeout",
                   tokens={"input": 100, "output": 0}),
        ok(),
    ])
    assert run(proc).type == "completed"
    assert proc.attempts == ["a/m1", "a/m2"]


@pytest.mark.parametrize("elapsed,expected_calls", [(6, 2), (11, 1)])
def test_fallback_candidates_share_deadline(monkeypatch, elapsed, expected_calls):
    clock = [0.0]
    monkeypatch.setattr("agent_core.harness.runner.time.monotonic", lambda: clock[0])

    class TimedProcess(FakeProcess):
        def run_turn(self, message=None, **kwargs):
            result = super().run_turn(message, **kwargs)
            clock[0] += elapsed
            return result

    prior = failed("rate_limit")
    prior.tokens = {"input": 100}
    prior.cost_usd = 0.25
    prior.model_id = "a/m1"
    prior.session_id = "paid-session"
    proc = TimedProcess([prior, ok()])
    result = run(proc, timeout=10)
    assert len(proc.calls) == expected_calls
    if expected_calls == 2:
        assert proc.calls[1]["timeout"] == 4
        assert result.type == "completed"
    else:
        assert result.type == "timeout"
        assert not result.fallback_eligible
        assert result.tokens == {"input": 100}
        assert result.cost_usd == 0.25
        assert result.model_id == "a/m1"
        assert result.session_id == "paid-session"


def run(proc, **kw):
    return run_turn_with_fallback(proc, repo_path="/repo", message="hi", models=CHAIN, **kw)


# -- the happy path ------------------------------------------------------------


def test_first_success_stops_immediately():
    proc = FakeProcess([ok()])
    assert run(proc).type == "completed"
    assert proc.attempts == ["a/m1"]


def test_tool_call_boundary_resumes_same_model_and_session():
    proc = FakeProcess(
        [
            TurnResult(
                type="incomplete",
                result="I will inspect the target first.",
                session_id="ses-tool-turn",
                model_id="a/m1",
                tokens={"input": 10, "output": 2, "total": 12},
            ),
            TurnResult(
                type="completed",
                result="Implemented and verified.",
                session_id="ses-tool-turn",
                model_id="a/m1",
                patch_count=1,
                tokens={"input": 20, "output": 3, "total": 23},
            ),
        ]
    )

    result = run(proc)

    assert result.type == "completed"
    assert result.patch_count == 1
    assert result.tokens == {"input": 30, "output": 5, "total": 35}
    assert proc.attempts == ["a/m1", "a/m1"]
    assert proc.calls[1]["session_id"] == "ses-tool-turn"
    assert proc.calls[1]["message"] == "Continue the current turn and complete the requested work."


def test_falls_through_to_the_next_model():
    proc = FakeProcess([failed(), ok()])
    result = run(proc)
    assert result.type == "completed"
    assert proc.attempts == ["a/m1", "a/m2"]
    assert result.model_attempts == (
        {
            "model": "a/m1",
            "outcome": "error",
            "fallback_reason": "provider_error",
        },
        {"model": "a/m2", "outcome": "completed", "fallback_reason": ""},
    )


def test_fallback_transition_is_reported_with_models_and_reason():
    updates = []
    proc = FakeProcess([failed(reason="rate_limit"), ok()])

    result = run(proc, on_update=updates.append)

    assert result.type == "completed"
    assert updates == ["model-fallback: a/m1 -> a/m2 reason=rate_limit"]


def test_empty_stream_retries_same_model_before_fallback(monkeypatch):
    sleeps = []
    monkeypatch.setattr("agent_core.harness.runner.random.uniform", lambda _a, _b: 1.5)
    monkeypatch.setattr("agent_core.harness.runner.time.sleep", sleeps.append)
    empty_stream = failed(
        reason="provider_transport_error",
        error={"data": {"message": "empty_stream: upstream stream closed"}},
    )
    proc = FakeProcess([empty_stream, ok()])

    result = run(proc, session_id="ses-old", bootstrap_message="bootstrap")

    assert result.type == "completed"
    assert proc.attempts == ["a/m1", "a/m1"]
    assert sleeps == [1.5]
    assert [item["model"] for item in result.model_attempts] == ["a/m1", "a/m1"]
    assert [call["session_id"] for call in proc.calls] == ["ses-old", None]
    assert [call["message"] for call in proc.calls] == ["hi", "bootstrap"]


def test_empty_stream_retries_only_once_before_next_model(monkeypatch):
    monkeypatch.setattr("agent_core.harness.runner.time.sleep", lambda _seconds: None)
    empty_stream = failed(
        reason="provider_transport_error",
        error={"message": "empty_stream"},
    )
    proc = FakeProcess([empty_stream, empty_stream, ok()])

    result = run(proc)

    assert result.type == "completed"
    assert proc.attempts == ["a/m1", "a/m1", "a/m2"]


def test_model_id_is_recorded_on_the_result():
    """Which model actually answered, after a chain retry."""
    proc = FakeProcess([failed(), ok()])
    assert run(proc).model_id == "a/m2"


def test_exhausting_the_chain_returns_the_last_failure():
    proc = FakeProcess([failed(), failed(), failed()])
    result = run(proc)
    assert result.type == "error"
    assert proc.attempts == CHAIN
    assert [attempt["model"] for attempt in result.model_attempts] == CHAIN


# -- cancellation --------------------------------------------------------------


def test_cancelled_turn_never_falls_back():
    """An operator pressing stop must not move the spend to the next provider."""
    proc = FakeProcess([TurnResult(type="cancelled", fallback_eligible=True)])
    result = run(proc)
    assert result.type == "cancelled"
    assert proc.attempts == ["a/m1"], "cancellation must stop the chain dead"


# -- a non-eligible failure is final -------------------------------------------


def test_non_eligible_failure_does_not_retry():
    proc = FakeProcess([TurnResult(type="error", fallback_eligible=False)])
    run(proc)
    assert proc.attempts == ["a/m1"]


def test_tool_contract_failure_does_not_quarantine_model():
    tool_error = TurnResult(
        type="error",
        fallback_eligible=False,
        fallback_reason="tool_contract_error",
    )
    proc = FakeProcess([tool_error, ok()])

    assert run(proc).type == "error"
    assert run(proc).type == "completed"
    assert proc.attempts == ["a/m1", "a/m1"]


# -- unreachable providers -----------------------------------------------------


REFUSED = {"name": "ConnectionRefused", "data": {"message": "ECONNREFUSED"}}


def test_unreachable_provider_is_abandoned_not_retried_per_model():
    """Ten models behind one refused connection is ten pointless spawns."""
    proc = FakeProcess([failed(error=REFUSED), ok()])
    result = run(proc)
    assert result.type == "completed"
    # a/m2 is skipped without a process spawn; the chain jumps to provider b
    assert proc.attempts == ["a/m1", "b/m1"]


def test_evidence_backed_auth_failure_quarantines_provider_across_calls():
    proc = FakeProcess(
        [
            failed(
                reason="provider_auth_failed",
                error={"data": {"httpStatus": 401, "errorCode": "invalid_api_key"}},
            ),
            ok(),
            ok(),
        ]
    )

    first = run(proc)
    second = run(proc)

    assert first.type == "completed"
    assert second.type == "completed"
    assert proc.attempts == ["a/m1", "b/m1", "b/m1"]


def test_unsubstantiated_auth_failure_does_not_quarantine_provider():
    """A classifier label alone cannot create an indefinite provider outage."""
    proc = FakeProcess(
        [
            failed(
                reason="provider_auth_failed",
                error={"data": {"message": "authentication failed"}},
            ),
            ok(),
        ]
    )

    result = run(proc)

    assert result.type == "completed"
    assert proc.attempts == ["a/m1", "a/m2"]


def test_model_attempt_carries_bounded_http_failure_evidence():
    proc = FakeProcess(
        [
            failed(
                reason="provider_auth_failed",
                error={"data": {
                    "message": "provider_auth_failed",
                    "httpStatus": 401,
                    "errorCode": "invalid_api_key",
                    "errorDetail": "Missing API key [redacted]",
                }},
            ),
            ok(),
        ]
    )

    result = run(proc)

    assert result.model_attempts[0] == {
        "model": "a/m1",
        "outcome": "error",
        "fallback_reason": "provider_auth_failed",
        "http_status": 401,
        "error_code": "invalid_api_key",
        "error_detail": "Missing API key [redacted]",
    }


def test_all_auth_failed_models_are_not_retried_until_reset():
    from agent_core.harness.tiered_router import mark_model_unhealthy

    for model in CHAIN:
        mark_model_unhealthy(model, reason="provider_auth_failed")
    proc = FakeProcess([ok()])

    result = run(proc)

    assert result.type == "error"
    assert proc.attempts == []

    reset_model_health()
    assert run(proc).type == "completed"
    assert proc.attempts == ["a/m1"]


def test_terminal_auth_failure_is_quarantined():
    proc = FakeProcess([failed(
        reason="provider_auth_failed",
        error={"data": {"httpStatus": 401, "errorCode": "invalid_api_key"}},
    )])

    first = run_turn_with_fallback(
        proc,
        repo_path="/repo",
        message="hi",
        models=["a/m1"],
    )
    second = run_turn_with_fallback(
        proc,
        repo_path="/repo",
        message="hi",
        models=["a/m1"],
    )

    assert first.type == "error"
    assert second.type == "error"
    assert proc.attempts == ["a/m1"]


def test_ordinary_model_failure_still_tries_the_same_provider():
    proc = FakeProcess([failed(error={"name": "BadRequest"}), ok()])
    run(proc)
    assert proc.attempts == ["a/m1", "a/m2"]


def test_should_skip_provider_is_not_keyed_on_a_provider_name():
    """The original keyed this on one deployment's provider; that cannot ship."""
    assert should_skip_provider(failed(error=REFUSED)) is True
    assert should_skip_provider(failed(error={"name": "Timeout"})) is False
    assert should_skip_provider(TurnResult(type="error", fallback_reason="rate_limited",
                                           error=REFUSED)) is False


# -- candidate selection -------------------------------------------------------


def test_preferred_model_goes_first():
    proc = FakeProcess([ok()])
    run_turn_with_fallback(proc, repo_path="/r", message="hi", models=CHAIN, preferred_model="b/m1")
    assert proc.attempts == ["b/m1"]


def test_preferred_model_is_not_duplicated():
    proc = FakeProcess([failed(), failed(), failed()])
    run_turn_with_fallback(proc, repo_path="/r", message="hi", models=CHAIN, preferred_model="a/m2")
    assert proc.attempts == ["a/m2", "a/m1", "b/m1"]


def test_empty_chain_is_an_error_not_a_crash():
    result = run_turn_with_fallback(FakeProcess([]), repo_path="/r", message="hi", models=[])
    assert result.type == "error" and "no opencode model candidate" in result.error["message"]


def test_prompt_file_is_passed_through():
    captured = {}

    class P:
        def run_turn(self, message=None, **kw):
            captured.update(kw)
            return ok()

    run_turn_with_fallback(P(), repo_path="/r", prompt_file="/tmp/p.md", models=["a/m1"])
    assert captured["prompt_file"] == "/tmp/p.md"


def test_session_id_is_only_on_the_first_candidate():
    proc = FakeProcess([failed(), ok()])
    run(
        proc,
        session_id="ses_bound",
        bootstrap_message="full bootstrap",
    )
    assert proc.calls[0]["session_id"] == "ses_bound"
    assert proc.calls[0]["message"] == "hi"
    assert proc.calls[1].get("session_id") in (None, "")
    assert proc.calls[1]["message"] == "full bootstrap"


def test_unhealthy_model_is_skipped_on_the_next_call_even_with_models():
    reset_model_health()
    proc = FakeProcess([failed(), ok(), ok()])
    run(proc)
    run(proc)
    assert proc.attempts == ["a/m1", "a/m2", "a/m2"]


def test_all_cooling_models_are_still_attempted():
    reset_model_health()
    proc = FakeProcess([failed(), failed(), failed(), ok()])
    run(proc)
    run(proc)
    assert proc.attempts[:3] == CHAIN
    assert proc.attempts[3] in CHAIN


def test_later_candidate_keeps_original_prompt_without_bootstrap():
    proc = FakeProcess([failed(), ok()])
    run(proc, session_id="ses_bound")
    assert proc.calls[0]["session_id"] == "ses_bound"
    assert proc.calls[1].get("session_id") in (None, "")
    assert proc.calls[1]["message"] == "hi"


def test_skipping_an_unhealthy_first_model_is_failover_not_continue():
    """A cooling bound model must not hand --continue to the next candidate."""
    from agent_core.harness.tiered_router import mark_model_unhealthy

    mark_model_unhealthy("a/m1", reason="rate_limit")
    proc = FakeProcess([ok()])
    run(proc, session_id="ses_bound", bootstrap_message="full bootstrap")
    assert proc.attempts == ["a/m2"]
    assert proc.calls[0].get("session_id") in (None, "")
    assert proc.calls[0]["message"] == "full bootstrap"


def test_last_success_does_not_inject_a_model_outside_models():
    """models= is an allowlist; last-success only reorders inside it."""
    from agent_core.harness.tiered_router import mark_model_success

    mark_model_success("z/outsider")
    proc = FakeProcess([ok()])
    run(proc)
    assert proc.attempts == ["a/m1"]


def test_all_cooling_default_chain_is_still_attempted(monkeypatch):
    """available_provider_candidates already drops cooling models; the walker
    must still try the configured chain when that list is empty."""
    from agent_core.harness.tiered_router import ProviderCandidate, mark_model_unhealthy

    chain = [
        ProviderCandidate(provider="a", model="m1", index=0),
        ProviderCandidate(provider="b", model="m1", index=1),
    ]
    monkeypatch.setattr(
        "agent_core.harness.runner.available_provider_candidates", lambda **k: []
    )
    monkeypatch.setattr(
        "agent_core.harness.runner.provider_candidates", lambda **k: chain
    )
    mark_model_unhealthy("a/m1", reason="rate_limit")
    mark_model_unhealthy("b/m1", reason="rate_limit")
    proc = FakeProcess([ok()])
    run_turn_with_fallback(proc, repo_path="/r", message="hi")
    assert proc.attempts == ["a/m1"]
