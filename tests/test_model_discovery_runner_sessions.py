"""Discovery contracts for harness.runner and harness.sessions execution."""

from contextlib import contextmanager

import pytest

from agent_core.harness.cost import CostAdmissionError, PaidAttempts
from agent_core.harness.process import TurnResult
from agent_core.harness.runner import run_turn_with_fallback
from agent_core.harness.sessions import FallbackHarnessSession, SessionSnapshot


MODELS = ("a/high", "a/low", "b/medium")


class Health:
    def __init__(self, blocked=()):
        self.blocked = set(blocked)
        self.events = []

    def is_healthy(self, model):
        return model not in self.blocked

    def mark_unhealthy(self, model, *, reason):
        self.events.append((model, reason))
        self.blocked.add(model)

    def mark_success(self, model, *, observed_at):
        self.events.append((model, "success"))


class Process:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def run_turn(self, message=None, **kwargs):
        self.calls.append({"message": message, **kwargs})
        result = self.results.pop(0)
        return result() if callable(result) else result


def failure(reason="rate_limit", **kwargs):
    return TurnResult(type="error", fallback_eligible=True, fallback_reason=reason, **kwargs)


def run(process, health, models=MODELS, **kwargs):
    return run_turn_with_fallback(
        process, repo_path="/repo", models=models, discovery_policy=health, **kwargs
    )


@pytest.fixture(autouse=True)
def no_legacy_health(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("discovery consulted or wrote legacy routing state")

    for name in (
        "is_model_healthy", "is_model_permanently_unhealthy", "last_successful_model",
        "mark_model_success", "mark_model_unhealthy", "available_provider_candidates",
        "provider_candidates",
    ):
        monkeypatch.setattr(f"agent_core.harness.runner.{name}", forbidden)


def test_discovery_order_options_and_billing():
    health = Health()
    ledger = PaidAttempts()
    process = Process(failure(cost_usd=0.2), TurnResult(type="completed", cost_usd=0.3))
    options = {MODELS[0]: {"variant": "high"}, MODELS[1]: {"variant": "low"}}
    updates = []
    result = run(process, health, preferred_model="outside/model", paid_attempts=ledger,
                 candidate_options=options.__getitem__, variant="wrong", on_update=updates.append)
    assert [call["model_id"] for call in process.calls] == list(MODELS[:2])
    assert [call["variant"] for call in process.calls] == ["high", "low"]
    assert all("discovery_policy" not in call and "candidate_options" not in call
               for call in process.calls)
    assert health.events == [(MODELS[0], "rate_limit"), (MODELS[1], "success")]
    assert ledger.count == 2
    assert ledger.aggregate().provider_cost_usd == pytest.approx(0.5)
    assert len(result.model_attempts) == 2
    assert updates == [f"model-fallback: {MODELS[0]} -> {MODELS[1]} reason=rate_limit"]


def test_model_rejection_does_not_quarantine_other_discovery_candidates():
    from agent_core.harness.process import classify_provider_model_error

    reason = classify_provider_model_error({"data": {
        "statusCode": 401, "message": "Your model id does not exist, recognized as k3.",
    }})
    health = Health()
    process = Process(failure(reason), TurnResult(type="completed"))
    assert run(process, health).type == "completed"
    assert [call["model_id"] for call in process.calls] == list(MODELS[:2])
    assert health.events == [(MODELS[0], "model_not_found"), (MODELS[1], "success")]
    assert health.blocked == {MODELS[0]}


@pytest.mark.parametrize("models", [(), MODELS])
def test_unavailable_is_zero_submissions(models):
    ledger = PaidAttempts()
    process = Process()
    result = run(process, Health(MODELS), models=models, paid_attempts=ledger,
                 preferred_model="outside/model")
    assert result.type == "error"
    assert not process.calls
    assert ledger.count == 0


def test_discovery_requires_explicit_models():
    with pytest.raises(ValueError, match="models"):
        run(Process(), Health(), models=None)


def test_terminal_failure_recorded_and_duplicate_models_not_retried():
    process = Process(failure())
    health = Health()
    run(process, health, models=(MODELS[0], MODELS[0]))
    assert len(process.calls) == 1
    assert health.events == [(MODELS[0], "rate_limit")]


def test_health_rechecked_after_workspace_preparation():
    health = Health()
    ledger = PaidAttempts()

    @contextmanager
    def workspace(model):
        health.blocked.add(model)
        yield "/repo"

    process = Process()
    result = run(process, health, workspace_factory=workspace, paid_attempts=ledger)
    assert result.type == "error"
    assert not process.calls
    assert ledger.count == 0
    assert health.events == []


@pytest.mark.parametrize("kind,eligible,reason", [
    ("cancelled", True, "rate_limit"),
    ("error", False, "tool_contract_error"),
    ("stalled", False, None),
])
def test_terminal_non_provider_results_do_not_quarantine(kind, eligible, reason):
    health = Health()
    process = Process(TurnResult(type=kind, fallback_eligible=eligible, fallback_reason=reason))
    assert run(process, health).type == kind
    assert len(process.calls) == 1
    assert health.events == []


class Session:
    session_id = "session"

    def __init__(self, result):
        self.result = result
        self.closed = False
        self.calls = 0

    def run_turn(self, **kwargs):
        self.calls += 1
        return self.result

    def snapshot(self):
        return SessionSnapshot(session_id=self.session_id)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("elapsed,count", [(6, 2), (11, 1)])
def test_session_fallback_shares_timeout(monkeypatch, elapsed, count):
    clock = [0.0]
    monkeypatch.setattr("agent_core.harness.sessions.time.monotonic", lambda: clock[0])
    timeouts = []

    class TimedSession(Session):
        def run_turn(self, **kwargs):
            timeouts.append(kwargs["timeout_seconds"])
            clock[0] += elapsed
            return super().run_turn(**kwargs)

    def opener(model):
        return TimedSession(failure(cost_usd=0.25) if model == MODELS[0] else TurnResult(type="completed"))

    session = FallbackHarnessSession(opener, models=MODELS, discovery_policy=Health())
    result = session.run_turn(timeout_seconds=10)
    assert len(timeouts) == count
    if count == 2:
        assert timeouts == [10, 4]
        assert result.type == "completed"
    else:
        assert result.type == "timeout"
        assert result.cost_usd == 0.25
    session.close()


def test_session_opening_counts_against_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("agent_core.harness.sessions.time.monotonic", lambda: clock[0])
    opened = Session(TurnResult(type="completed"))

    def opener(model):
        clock[0] = 11
        return opened

    session = FallbackHarnessSession(opener, models=MODELS, discovery_policy=Health())
    result = session.run_turn(timeout_seconds=10)
    assert result.type == "timeout"
    assert opened.calls == 0
    assert opened.closed


def fallback(health, results, models=MODELS):
    opened = []

    def open_session(model):
        session = Session(results[len(opened)])
        opened.append((model, session))
        return session

    return FallbackHarnessSession(open_session, models=models, discovery_policy=health), opened


def test_session_all_unhealthy_never_opens_or_bills():
    session, opened = fallback(Health(MODELS), [])
    ledger = PaidAttempts()
    assert session.run_turn(paid_attempts=ledger) is None
    assert not opened
    assert ledger.count == 0


def test_session_reports_terminal_failure_and_success():
    health = Health()
    session, opened = fallback(health, [failure(), TurnResult(type="completed")])
    ledger = PaidAttempts()
    assert session.run_turn(paid_attempts=ledger).type == "completed"
    assert [model for model, _ in opened] == list(MODELS[:2])
    assert opened[0][1].closed
    assert health.events == [(MODELS[0], "rate_limit"), (MODELS[1], "success")]
    assert ledger.count == 2
    last_health = Health()
    last, _ = fallback(last_health, [failure()], models=(MODELS[0],))
    assert not last.run_turn().fallback_eligible
    assert last_health.events == [(MODELS[0], "rate_limit")]


def test_session_rechecks_active_health_between_turns():
    health = Health()
    session, opened = fallback(health, [TurnResult(type="completed"), TurnResult(type="completed")])
    session.run_turn()
    health.blocked.add(MODELS[0])
    session.run_turn()
    assert [model for model, _ in opened] == list(MODELS[:2])
    assert opened[0][1].calls == 1
    assert opened[0][1].closed


@pytest.mark.parametrize("reason,error", [
    ("provider_auth_failed", {"data": {"httpStatus": 401, "errorCode": "invalid_api_key"}}),
    ("provider_transport_error", {"message": "ECONNREFUSED"}),
])
@pytest.mark.parametrize("entrypoint", ["process", "session"])
def test_provider_wide_failures_skip_siblings(entrypoint, reason, error):
    health = Health()
    results = [failure(reason, error=error), TurnResult(type="completed")]
    if entrypoint == "process":
        process = Process(*results)
        run(process, health)
        submitted = [call["model_id"] for call in process.calls]
    else:
        session, opened = fallback(health, results)
        session.run_turn()
        submitted = [model for model, _ in opened]
    assert submitted == [MODELS[0], MODELS[2]]
    assert MODELS[1] in health.blocked
    sibling_reason = "provider_auth_failed" if reason == "provider_auth_failed" else "provider_error"
    assert (MODELS[1], sibling_reason) in health.events


def test_session_cancelled_never_falls_back_or_quarantines():
    health = Health()
    session, opened = fallback(health, [TurnResult(type="cancelled", fallback_eligible=True)])
    assert session.run_turn().type == "cancelled"
    assert len(opened) == 1
    assert health.events == []


def test_empty_discovery_session_never_opens():
    session, opened = fallback(Health(), [], models=())
    assert session.run_turn() is None
    assert opened == []


def test_cancelled_empty_stream_is_not_retried(monkeypatch):
    monkeypatch.setattr("agent_core.harness.runner.time.sleep", lambda _: None)
    health = Health()
    process = Process(TurnResult(type="cancelled", fallback_eligible=True,
                                 fallback_reason="provider_transport_error",
                                 error={"message": "empty_stream"}))
    assert run(process, health).type == "cancelled"
    assert len(process.calls) == 1
    assert health.events == []


def test_health_changed_by_another_invocation_skips_next_candidate():
    health = Health()

    def fail_and_block_next():
        health.blocked.add(MODELS[1])
        return failure()

    process = Process(fail_and_block_next, TurnResult(type="completed"))
    run(process, health)
    assert [call["model_id"] for call in process.calls] == [MODELS[0], MODELS[2]]


def test_session_health_changed_while_opening_does_not_bill():
    health = Health()
    opened = []

    def open_session(model):
        health.blocked.add(model)
        candidate = Session(TurnResult(type="completed"))
        opened.append(candidate)
        return candidate

    session = FallbackHarnessSession(open_session, models=MODELS, discovery_policy=health)
    ledger = PaidAttempts()
    assert session.run_turn(paid_attempts=ledger) is None
    assert all(candidate.closed and candidate.calls == 0 for candidate in opened)
    assert ledger.count == 0


def test_session_all_remaining_unhealthy_preserves_last_failure():
    health = Health(MODELS[1:])
    failed = failure()
    session, opened = fallback(health, [failed])
    assert session.run_turn() is failed
    assert failed.fallback_eligible is False
    assert len(opened) == 1


def test_option_errors_happen_before_billing_without_quarantine():
    health = Health()
    ledger = PaidAttempts()
    process = Process()

    def options(model):
        raise ValueError("unsupported variant")

    with pytest.raises(ValueError, match="unsupported variant"):
        run(process, health, candidate_options=options, paid_attempts=ledger)
    assert ledger.count == 0
    assert not process.calls
    assert health.events == []


def test_continuation_retains_variant_and_bills_each_submission():
    health = Health()
    ledger = PaidAttempts()
    process = Process(TurnResult(type="incomplete", session_id="tool", cost_usd=0.1),
                      TurnResult(type="completed", cost_usd=0.2))
    result = run(process, health, candidate_options=lambda _: {"variant": "high"},
                 paid_attempts=ledger)
    assert [call["variant"] for call in process.calls] == ["high", "high"]
    assert process.calls[1]["session_id"] == "tool"
    assert ledger.count == 2
    assert result.cost_usd == pytest.approx(0.3)
    assert health.events == [(MODELS[0], "success")]


def test_empty_stream_retry_preserves_options_and_billing(monkeypatch):
    monkeypatch.setattr("agent_core.harness.runner.time.sleep", lambda _: None)
    health = Health()
    ledger = PaidAttempts()
    process = Process(failure("provider_transport_error", error={"message": "empty_stream"}),
                      TurnResult(type="completed"))
    run(process, health, candidate_options=lambda _: {"variant": "low"}, paid_attempts=ledger)
    assert [call["model_id"] for call in process.calls] == [MODELS[0], MODELS[0]]
    assert [call["variant"] for call in process.calls] == ["low", "low"]
    assert ledger.count == 2
    assert health.events == [(MODELS[0], "success")]


@pytest.mark.parametrize("entrypoint", ["process", "session"])
def test_cost_refusal_does_not_fallback_or_quarantine(entrypoint):
    class Refuse:
        def before_paid_attempt(self, **kwargs):
            raise CostAdmissionError("no spend")

    health = Health()
    ledger = PaidAttempts(Refuse())
    process = Process()
    session, opened = fallback(health, [TurnResult(type="completed")])
    with pytest.raises(CostAdmissionError):
        if entrypoint == "process":
            run(process, health, paid_attempts=ledger)
        else:
            session.run_turn(paid_attempts=ledger)
    assert not process.calls
    assert all(candidate.calls == 0 for _, candidate in opened)
    assert health.events == []


@pytest.mark.parametrize("entrypoint", ["process", "session"])
def test_success_records_start_before_health_check_not_completion(monkeypatch, entrypoint):
    now = [100.0]
    monkeypatch.setattr("time.time", lambda: now[0])

    class ObservedHealth(Health):
        def __init__(self):
            super().__init__()
            self.observations = []

        def is_healthy(self, model):
            # A failure arriving during the final availability read must still
            # be newer than this attempt's observation.
            now[0] += 1.0
            return True

        def mark_success(self, model, *, observed_at):
            self.observations.append((model, observed_at))

    health = ObservedHealth()
    started = []

    def complete():
        started.append(now[0])
        now[0] = 200.0
        return TurnResult(type="completed")

    if entrypoint == "process":
        run(Process(complete), health, models=(MODELS[0],))
    else:
        class CompletingSession(Session):
            def run_turn(self, **kwargs):
                return complete()

        session = FallbackHarnessSession(
            lambda _: CompletingSession(None), models=(MODELS[0],), discovery_policy=health
        )
        session.run_turn()
        session.close()
    assert health.observations == [(MODELS[0], started[0] - 1.0)]


@pytest.mark.parametrize("transition", ["retry", "continuation", "fallback"])
def test_process_observation_is_per_submission(monkeypatch, transition):
    now = [100.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    monkeypatch.setattr("agent_core.harness.runner.time.sleep", lambda _: None)
    observations = []
    health = Health()
    health.mark_success = lambda model, *, observed_at: observations.append((model, observed_at))

    def first():
        now[0] = 110.0
        if transition == "continuation":
            return TurnResult(type="incomplete", session_id="tool-session")
        if transition == "retry":
            return failure("provider_transport_error", error={"message": "empty_stream"})
        return failure()

    def second():
        now[0] = 120.0
        return TurnResult(type="completed")

    process = Process(first, second)
    run(process, health)
    expected_model = MODELS[1] if transition == "fallback" else MODELS[0]
    assert observations == [(expected_model, 110.0)]
    assert len(process.calls) == 2


def test_reused_session_observes_each_turn_separately(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    observations = []
    health = Health()
    health.mark_success = lambda model, *, observed_at: observations.append((model, observed_at))
    session, opened = fallback(health, [TurnResult(type="completed")])
    session.run_turn()
    now[0] = 110.0
    session.run_turn()
    session.close()
    assert len(opened) == 1
    assert observations == [(MODELS[0], 100.0), (MODELS[0], 110.0)]


def test_health_loss_before_continuation_preserves_paid_work():
    health = Health()
    def first():
        health.blocked.add(MODELS[0])
        return TurnResult(type="incomplete", session_id="work", cost_usd=0.25)
    process = Process(first)
    ledger = PaidAttempts()
    result = run(process, health, models=(MODELS[0],), paid_attempts=ledger)
    assert result.type == "error"
    assert result.session_id == "work"
    assert result.error["message"] == "model unavailable before continuation"
    assert ledger.count == 1
    assert result.cost_usd == pytest.approx(0.25)
    assert len(process.calls) == 1


@pytest.mark.parametrize("patches", [0, 1])
def test_health_loss_before_continuation_falls_back_without_patches(patches):
    health = Health()

    def first():
        health.blocked.add(MODELS[0])
        return TurnResult(type="incomplete", session_id="work", cost_usd=0.25, patch_count=patches)

    process = Process(first, TurnResult(type="completed"))
    result = run(process, health, bootstrap_message="self-contained request")
    if patches:
        assert result.type == "error"
        assert not result.fallback_eligible
        assert result.patch_count == 1
        assert len(process.calls) == 1
        return
    assert result.type == "completed"
    assert [call["model_id"] for call in process.calls] == list(MODELS[:2])
    assert process.calls[1]["message"] == "self-contained request"


def test_manual_mode_keeps_legacy_health_and_provider_skip(monkeypatch):
    from agent_core.harness import runner

    events = []
    monkeypatch.setattr(runner, "is_model_healthy", lambda _: True)
    monkeypatch.setattr(runner, "is_model_permanently_unhealthy", lambda _: False)
    monkeypatch.setattr(runner, "last_successful_model", lambda: None)
    monkeypatch.setattr(runner, "mark_model_success", lambda model: events.append((model, "success")))
    monkeypatch.setattr(runner, "mark_model_unhealthy", lambda model, *, reason: events.append((model, reason)))
    process = Process(failure("provider_transport_error", error={"message": "ECONNREFUSED"}),
                      TurnResult(type="completed"))
    result = run_turn_with_fallback(process, repo_path="/repo", models=MODELS)
    assert result.type == "completed"
    assert [call["model_id"] for call in process.calls] == [MODELS[0], MODELS[2]]
    assert events == [(MODELS[0], "provider_transport_error"), (MODELS[1], "provider_error"),
                      (MODELS[2], "success")]


def test_unhealthy_model_never_prepares_workspace():
    health = Health([MODELS[0]])
    entered = []
    @contextmanager
    def workspace(model):
        entered.append(model)
        yield "/repo"
    run(Process(TurnResult(type="completed")), health, workspace_factory=workspace)
    assert entered == [MODELS[1]]


def test_repeated_empty_stream_retries_only_once(monkeypatch):
    monkeypatch.setattr("agent_core.harness.runner.time.sleep", lambda _: None)
    health = Health()
    failed = lambda: failure("provider_transport_error", error={"message": "empty_stream"})
    process = Process(failed(), failed(), TurnResult(type="completed"))
    run(process, health)
    assert [call["model_id"] for call in process.calls] == [MODELS[0], MODELS[0], MODELS[1]]


def test_health_loss_during_preparation_advances_to_next_candidate():
    health = Health()
    @contextmanager
    def workspace(model):
        if model == MODELS[0]:
            health.blocked.add(model)
        yield "/repo"
    process = Process(TurnResult(type="completed"))
    result = run(process, health, workspace_factory=workspace)
    assert result.type == "completed"
    assert [call["model_id"] for call in process.calls] == [MODELS[1]]
