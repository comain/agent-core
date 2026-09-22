"""The canonical ordering of one agent turn.

The order is the contract. It had been written twice — once in the LangGraph
node, once in each product's direct call path — and the two drifted. These
tests freeze it as an ordered trace, because that is the only form in which a
reordering is visible: every individual step still "works" if a result is
persisted before the guard accepts, and the failure shows up months later as a
row describing a diff that was never allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.harness.cost import AttemptNotAdmitted, TurnCost
from agent_core.harness.execution import (
    AgentTurnContext,
    AgentTurnExecutionError,
    AgentTurnRequest,
    HarnessBinding,
    ResultCommitError,
    execute_agent_turn,
)
from agent_core.harness.sessions import AgentSessionRef, SessionSnapshot


class Turn:
    def __init__(self, type="completed", session_id="s1", cost_usd=0.10, refs=()):
        self.type = type
        self.result = "done"
        self.session_id = session_id
        self.cost_usd = cost_usd
        self.session_refs = tuple(refs)


class Harness:
    def __init__(self, trace, result=None):
        self.trace = trace
        self.result = result or Turn()

    def run_turn(self, **kwargs):
        self.trace.append("turn")
        return self.result


class Session:
    def __init__(self, trace, harness):
        self.trace = trace
        self._harness = harness
        self.session_id = "s1"

    def run_turn(self, **kwargs):
        return self._harness.run_turn(**kwargs)

    def snapshot(self):
        self.trace.append("snapshot")
        return SessionSnapshot(session_id="s1", provider_cost_usd=0.10)

    def close(self):
        self.trace.append("close")


class Ports:
    """Every port, all recording into one trace."""

    def __init__(self, trace, *, cancelled=False):
        self.trace = trace
        self.cancelled = cancelled
        self.committed: list = []
        self.rejected: list = []

    # cancellation
    def is_cancelled(self):
        return self.cancelled

    # sessions
    def open_session(self, *, harness, repo_path, model_id):
        self.trace.append("open_session")
        return Session(self.trace, harness)

    # guard
    def before(self, request):
        self.trace.append("guard_before")
        return "token"

    def after(self, request, token):
        self.trace.append(f"guard_after:{token}")

    # cost
    def before_paid_attempt(self, *, operation_id, paid_attempt_ordinal):
        self.trace.append(f"admit:{paid_attempt_ordinal}")

    def after_paid_attempt(self, *, operation_id, paid_attempt_ordinal, cost):
        self.trace.append(f"charge:{paid_attempt_ordinal}:{cost.provider_cost_usd}")

    # progress
    def callback(self, *, request, session_id):
        self.trace.append(f"progress_callback:{request.name}:{session_id}")
        return None

    def flush(self):
        self.trace.append("flush")
        return Flush()

    # results
    def commit(self, request, execution):
        self.trace.append(f"commit:{execution.result.status}")
        self.committed.append(execution)

    def reject(self, request, execution, *, reason):
        self.trace.append(f"reject:{reason}")
        self.rejected.append((execution, reason))

    # observer
    def check_before_attempt(self, *, operation_id, attempt):
        self.trace.append(f"observe:{attempt}")


class Flush:
    configured = True
    failures: dict = {}
    dropped_count = 0


def request_for(tmp_path, **over):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the thing")
    fields = dict(
        name="generate",
        repo_path=tmp_path,
        prompt=lambda attempt, feedback: prompt,
        operation_id="op-1",
        session_scope="phase",
    )
    fields.update(over)
    return AgentTurnRequest(**fields)


def context_for(trace, harness, ports):
    return AgentTurnContext(
        binding=HarnessBinding(name="opencode", harness=harness),
        cost=ports,
        cancellation=ports,
        sessions=ports,
        guard=ports,
        progress=ports,
        results=ports,
        observer=ports,
    )


# -- the order -------------------------------------------------------------

def test_a_string_prompt_is_passed_as_message(tmp_path):
    captured: dict = {}

    class Capturing:
        def run_turn(self, **kwargs):
            captured.update(kwargs)
            return Turn()

    ports = Ports([])
    execute_agent_turn(
        request_for(
            tmp_path,
            prompt=lambda attempt, feedback: "hello",
            session_scope="none",
            delivery="stdin",
            title="spec_docs_plan",
        ),
        context_for([], Capturing(), ports),
    )
    assert captured["message"] == "hello"
    assert captured.get("prompt_file") is None
    assert captured["delivery"] == "stdin"
    assert captured["title"] == "spec_docs_plan"


def test_a_successful_turn_runs_the_steps_in_order(tmp_path):
    trace: list = []
    ports = Ports(trace)
    harness = Harness(trace)

    execution = execute_agent_turn(
        request_for(tmp_path), context_for(trace, harness, ports)
    )

    assert execution.accepted
    assert trace == [
        "open_session",
        "guard_before",
        "progress_callback:generate:s1",
        "observe:1",
        "admit:1",
        "turn",
        "charge:1:0.1",
        "snapshot",
        "guard_after:token",
        "flush",
        "close",
        "commit:completed",
    ]


def test_the_result_is_durable_only_after_the_guard_and_the_session(tmp_path):
    """Persisting earlier lets a product commit an answer built from a
    workspace that was about to be rejected, and there is no undo for that."""
    trace: list = []
    ports = Ports(trace)

    execute_agent_turn(request_for(tmp_path), context_for(trace, Harness(trace), ports))

    assert trace.index("commit:completed") > trace.index("guard_after:token")
    assert trace.index("commit:completed") > trace.index("close")


def test_a_failed_turn_is_committed_too(tmp_path):
    """A failure that produced no row is indistinguishable from a crash."""
    trace: list = []
    ports = Ports(trace)
    harness = Harness(trace, result=Turn(type="error", session_id=None))

    execution = execute_agent_turn(
        request_for(tmp_path), context_for(trace, harness, ports)
    )

    assert not execution.accepted
    assert trace[-1] == "commit:failed"


def test_cancellation_commits_without_touching_anything(tmp_path):
    """No guard taken and no session opened, so there is nothing whose
    acceptance this envelope could be jumping ahead of."""
    trace: list = []
    ports = Ports(trace, cancelled=True)

    execution = execute_agent_turn(
        request_for(tmp_path), context_for(trace, Harness(trace), ports)
    )

    assert execution.result.status == "cancelled"
    assert trace == ["commit:cancelled"]


def test_result_port_receives_operation_identity_even_on_cancellation(tmp_path):
    trace: list = []
    seen = []

    class Capturing(Ports):
        def commit(self, request, execution):
            seen.append((request.operation_id, request.name, execution.result.status))

    execute_agent_turn(
        request_for(tmp_path),
        context_for(trace, Harness(trace), Capturing(trace, cancelled=True)),
    )

    assert seen == [("op-1", "generate", "cancelled")]


def test_active_turn_receives_the_typed_cancellation_source(tmp_path):
    trace: list = []
    ports = Ports(trace)
    seen = []

    class CapturingHarness(Harness):
        def run_turn(self, **kwargs):
            seen.append(kwargs["is_cancelled"])
            return super().run_turn(**kwargs)

    execute_agent_turn(
        request_for(tmp_path), context_for(trace, CapturingHarness(trace), ports)
    )

    assert len(seen) == 1
    assert seen[0]() is False


# -- the guard is authoritative --------------------------------------------

def test_a_rejected_guard_prevents_the_commit_and_re_raises(tmp_path):
    trace: list = []
    ports = Ports(trace)

    def reject(request, token):
        trace.append("guard_after:refused")
        raise RuntimeError("the workspace changed files it may not touch")

    context = context_for(trace, Harness(trace), ports)
    object.__setattr__(context, "guard", _GuardThatRejects(ports, reject))

    with pytest.raises(RuntimeError, match="may not touch"):
        execute_agent_turn(request_for(tmp_path), context)

    assert ports.committed == []
    assert [reason for _, reason in ports.rejected] == [
        "the workspace changed files it may not touch"
    ]


def test_a_rejected_guard_still_flushes_and_closes_before_raising(tmp_path):
    """Raising at the point of rejection would leak the session — a paid
    conversation nobody will ever close."""
    trace: list = []
    ports = Ports(trace)

    def reject(request, token):
        trace.append("guard_after:refused")
        raise RuntimeError("refused")

    context = context_for(trace, Harness(trace), ports)
    object.__setattr__(context, "guard", _GuardThatRejects(ports, reject))

    with pytest.raises(RuntimeError):
        execute_agent_turn(request_for(tmp_path), context)

    assert trace[-4:] == ["guard_after:refused", "flush", "close", "reject:refused"]


class _GuardThatRejects:
    def __init__(self, ports, after):
        self._ports = ports
        self._after = after

    def before(self, request):
        return self._ports.before(request)

    def after(self, request, token):
        return self._after(request, token)


# -- the ports are optional ------------------------------------------------

def test_the_minimum_context_is_a_harness_and_a_cost_port(tmp_path):
    trace: list = []
    ports = Ports(trace)

    execution = execute_agent_turn(
        request_for(tmp_path, session_scope="none"),
        AgentTurnContext(
            binding=HarnessBinding(name="opencode", harness=Harness(trace)),
            cost=ports,
        ),
    )

    assert execution.accepted
    assert trace == ["admit:1", "turn", "charge:1:0.1"]


def test_a_missing_capability_is_refused_before_anything_is_spent(tmp_path):
    """Otherwise it surfaces as an AttributeError inside a turn already paid
    for."""
    trace: list = []
    with pytest.raises(AgentTurnExecutionError, match="session factory"):
        execute_agent_turn(
            request_for(tmp_path, session_scope="phase"),
            AgentTurnContext(
                binding=HarnessBinding(name="opencode", harness=Harness(trace)),
                cost=Ports(trace),
            ),
        )
    assert trace == []


@pytest.mark.parametrize(
    "over, match",
    [
        ({"attempts": 0}, "at least 1"),
        ({"on_failure": "explode"}, "on_failure"),
        ({"session_scope": "forever"}, "session_scope"),
    ],
)
def test_an_impossible_request_is_refused(tmp_path, over, match):
    trace: list = []
    ports = Ports(trace)
    with pytest.raises(AgentTurnExecutionError, match=match):
        execute_agent_turn(
            request_for(tmp_path, **over), context_for(trace, Harness(trace), ports)
        )
    assert trace == []


def test_a_binding_carries_the_configured_name_not_the_class(tmp_path):
    """Attribution that reads `Harness` breaks the day a class is renamed, and
    the rows written before then disagree with the rows written after."""
    trace: list = []
    with pytest.raises(AgentTurnExecutionError, match="neutral name"):
        execute_agent_turn(
            request_for(tmp_path, session_scope="none"),
            AgentTurnContext(
                binding=HarnessBinding(name="", harness=Harness(trace)),
                cost=Ports(trace),
            ),
        )


# -- both answers ----------------------------------------------------------

def test_it_returns_the_exact_outcome_and_the_normalized_result(tmp_path):
    """A direct caller needs its parser's payload; the graph needs JSON. Making
    one derive the other is what produced two normalizations."""
    trace: list = []
    ports = Ports(trace)

    execution = execute_agent_turn(
        request_for(tmp_path, parse=lambda text: {"answer": text}),
        context_for(trace, Harness(trace), ports),
    )

    assert execution.outcome.payload == {"answer": "done"}
    assert execution.result.status == "completed"
    assert execution.result.as_dict()["status"] == "completed"


def test_the_aggregate_cost_and_attempt_count_come_back(tmp_path):
    trace: list = []
    ports = Ports(trace)

    execution = execute_agent_turn(
        request_for(tmp_path), context_for(trace, Harness(trace), ports)
    )

    assert execution.paid_attempts == 1
    assert execution.cost == TurnCost.from_provider(0.10)


def test_an_unknown_charge_leaves_the_aggregate_unknown(tmp_path):
    trace: list = []
    ports = Ports(trace)
    harness = Harness(trace, result=Turn(cost_usd=None))

    execution = execute_agent_turn(
        request_for(tmp_path), context_for(trace, harness, ports)
    )

    assert execution.cost == TurnCost.unavailable()


# -- refusals and faults ---------------------------------------------------

def test_an_observer_refusal_stops_without_another_submission(tmp_path):
    trace: list = []
    ports = Ports(trace)

    class Stopped(Ports):
        def check_before_attempt(self, *, operation_id, attempt):
            trace.append(f"observe:{attempt}")
            raise RuntimeError("the task was stopped")

    stopped = Stopped(trace)
    context = context_for(trace, Harness(trace), ports)
    object.__setattr__(context, "observer", stopped)

    execution = execute_agent_turn(request_for(tmp_path, attempts=3), context)

    assert not execution.accepted
    assert "turn" not in trace
    assert trace.count("observe:1") == 1, "a refusal was retried"


def test_a_commit_failure_is_raised_not_logged(tmp_path):
    """A turn whose answer was not persisted has not finished; letting the
    graph checkpoint past it is how a workflow resumes believing work is done
    that no product row records."""
    trace: list = []

    class Broken(Ports):
        def commit(self, request, execution):
            raise RuntimeError("the operations table is gone")

    ports = Broken(trace)
    with pytest.raises(ResultCommitError):
        execute_agent_turn(request_for(tmp_path), context_for(trace, Harness(trace), ports))


def test_a_broken_flush_cannot_fail_the_turn(tmp_path):
    trace: list = []

    class Broken(Ports):
        def flush(self):
            raise RuntimeError("the sink is gone")

    ports = Broken(trace)
    execution = execute_agent_turn(
        request_for(tmp_path), context_for(trace, Harness(trace), ports)
    )

    assert execution.accepted


def test_a_flush_that_dropped_events_is_a_diagnostic_not_a_failure(tmp_path):
    trace: list = []

    class Dropping(Ports):
        def flush(self):
            trace.append("flush")

            class Result:
                configured = True
                failures = {"delivery": 1}
                dropped_count = 3

            return Result()

    ports = Dropping(trace)
    execution = execute_agent_turn(
        request_for(tmp_path), context_for(trace, Harness(trace), ports)
    )

    assert execution.accepted
    assert execution.result.diagnostics["progress_flush_failed"] is True
    assert execution.result.diagnostics["progress_dropped"] == 3


def test_a_session_that_will_not_close_prevents_the_commit(tmp_path):
    """The close is the last thing that can report the conversation ended in a
    state anyone understood."""
    trace: list = []

    class Stuck(Ports):
        def open_session(self, *, harness, repo_path, model_id):
            session = Session(trace, harness)
            session.close = _raise
            return session

    ports = Stuck(trace)
    with pytest.raises(RuntimeError, match="the client is gone"):
        execute_agent_turn(request_for(tmp_path), context_for(trace, Harness(trace), ports))

    assert ports.committed == []


def test_a_guard_rejection_wins_over_a_failed_close(tmp_path):
    """A verdict on the work, not a fault in the cleanup — and the product
    needs to hear the verdict."""
    trace: list = []

    class Stuck(Ports):
        def open_session(self, *, harness, repo_path, model_id):
            session = Session(trace, harness)
            session.close = _raise
            return session

        def after(self, request, token):
            raise RuntimeError("the workspace changed files it may not touch")

    ports = Stuck(trace)
    with pytest.raises(RuntimeError, match="may not touch"):
        execute_agent_turn(request_for(tmp_path), context_for(trace, Harness(trace), ports))

    assert [reason for _, reason in ports.rejected] == [
        "the workspace changed files it may not touch"
    ]


def _raise():
    raise RuntimeError("the client is gone")


def test_the_guard_is_released_when_the_provider_dies(tmp_path):
    """A dead provider is an unreachable turn, not an escaping exception — but
    the guard is released either way, or a lease outlives the run that took
    it."""
    trace: list = []
    ports = Ports(trace)

    class Exploding:
        def run_turn(self, **kwargs):
            trace.append("turn")
            raise RuntimeError("the provider process died")

    execution = execute_agent_turn(
        request_for(tmp_path), context_for(trace, Exploding(), ports)
    )

    assert execution.outcome.unreachable
    assert "guard_after:token" in trace and "close" in trace


def test_the_guard_is_released_when_the_commit_raises(tmp_path):
    """The guard is released before the commit is even attempted, so a failing
    durability port cannot strand it."""
    trace: list = []

    class Broken(Ports):
        def commit(self, request, execution):
            raise RuntimeError("the operations table is gone")

    ports = Broken(trace)
    with pytest.raises(ResultCommitError):
        execute_agent_turn(request_for(tmp_path), context_for(trace, Harness(trace), ports))

    assert "guard_after:token" in trace


def test_a_guard_that_never_started_is_not_released(tmp_path):
    """Releasing a guard that was never taken is how a product double-frees a
    lease held by someone else."""
    trace: list = []

    class Refusing(Ports):
        def before(self, request):
            trace.append("guard_before")
            raise RuntimeError("another worker holds the workspace")

    ports = Refusing(trace)
    with pytest.raises(RuntimeError, match="another worker"):
        execute_agent_turn(request_for(tmp_path), context_for(trace, Harness(trace), ports))

    assert not any(event.startswith("guard_after") for event in trace)


def test_the_turns_conversations_reach_the_normalized_result(tmp_path):
    """The record and the result are built from different places — the record
    from the turn, the result from the turn *and* the snapshot — so the turn's
    own conversations have to survive into both."""
    trace: list = []
    ports = Ports(trace)
    ref = AgentSessionRef("opencode", "ses-01")
    harness = Harness(trace, result=Turn(refs=(ref,)))

    execution = execute_agent_turn(
        request_for(tmp_path), context_for(trace, harness, ports)
    )

    assert ref in execution.result.session_refs
    assert ref in execution.outcome.record.session_refs


def test_a_record_naming_a_conversation_the_result_does_not_is_refused():
    """An invariant, checked rather than assumed: today normalization merges
    the turn's refs so the two cannot disagree, and if a future change breaks
    that, the product is about to store a row attributing cost to a session its
    own result never mentions. Checked directly because the construction that
    would produce it does not exist yet."""
    from agent_core.harness.execution import _assert_sessions_agree
    from agent_core.harness.node import NodeOutcome
    from agent_core.harness.records import TurnRecord
    from agent_core.harness.turn_result import AgentTurnResult

    ghost = AgentSessionRef("opencode", "never-seen")
    outcome = NodeOutcome(
        status="accepted", record=TurnRecord(status="success", session_refs=(ghost,))
    )

    with pytest.raises(AgentTurnExecutionError, match="never-seen"):
        _assert_sessions_agree(outcome, AgentTurnResult(status="completed"))

    _assert_sessions_agree(
        outcome, AgentTurnResult(status="completed", session_refs=(ghost,))
    )
