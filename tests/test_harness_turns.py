"""A bounded turn that has to come back with something usable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.harness.turns import (
    ACCEPTED,
    REJECTED,
    UNREACHABLE,
    run_structured_turn,
)


class Turn:
    def __init__(self, type="completed", result='{"ok": true}'):
        self.type = type
        self.result = result


class Runner:
    """Returns each scripted outcome in order; an exception is raised."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def run_turn(self, **kwargs):
        self.calls += 1
        outcome = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def parse_json(text):
    return json.loads(text)


def _run(runner, **kw):
    return run_structured_turn(
        runner, prompt_file=Path("/tmp/p.md"), repo_path=Path("/tmp"), parse=parse_json, **kw
    )


def test_a_good_answer_is_returned_on_the_first_attempt():
    runner = Runner(Turn())
    outcome = _run(runner, attempts=3)

    assert outcome.status == ACCEPTED
    assert outcome.answered
    assert outcome.payload == {"ok": True}
    assert outcome.attempts == 1
    assert runner.calls == 1, "a good answer must not be asked for twice"


def test_an_unparseable_answer_is_retried():
    runner = Runner(Turn(result="not json"), Turn())
    outcome = _run(runner, attempts=3)

    assert outcome.answered
    assert outcome.attempts == 2


def test_a_turn_that_never_completed_is_retried():
    runner = Runner(Turn(type="stalled", result=""), Turn())
    assert _run(runner, attempts=2).answered


def test_retries_stop_at_the_bound():
    runner = Runner(Turn(result="not json"))
    outcome = _run(runner, attempts=3)

    assert outcome.status == REJECTED
    assert outcome.attempts == 3
    assert runner.calls == 3
    assert "could not be parsed" in outcome.error
    assert outcome.parse_error


def test_a_runner_that_raises_every_time_is_unreachable_not_unanswered():
    """Different endings: never reached, versus answered badly.

    The first usually deserves an alert and the second usually does not, so
    they must not arrive as the same status.
    """
    runner = Runner(RuntimeError("connection refused"))
    outcome = _run(runner, attempts=2)

    assert outcome.status == UNREACHABLE
    assert outcome.error == "connection refused"
    assert outcome.result is None


def test_a_transport_failure_is_retried_and_can_still_succeed():
    runner = Runner(RuntimeError("reset"), Turn())
    outcome = _run(runner, attempts=2)

    assert outcome.answered
    assert outcome.attempts == 2


def test_on_retry_is_told_what_is_being_retried():
    seen = []
    runner = Runner(Turn(result="not json"), Turn())
    _run(runner, attempts=2, on_retry=lambda **kw: seen.append(kw))

    assert seen == [{"attempt": 1, "max_attempts": 2, "error": seen[0]["error"]}]
    assert "could not be parsed" in seen[0]["error"]


def test_on_retry_is_not_called_after_the_last_attempt():
    """Nothing is being retried at that point; a caller logging it would lie."""
    seen = []
    runner = Runner(Turn(result="not json"))
    _run(runner, attempts=2, on_retry=lambda **kw: seen.append(kw))

    assert [k["attempt"] for k in seen] == [1]


def test_attempts_below_one_still_runs_once():
    runner = Runner(Turn())
    assert _run(runner, attempts=0).answered
    assert runner.calls == 1


def test_without_a_parser_a_completed_turn_is_enough():
    """Not every turn is asked for JSON."""
    runner = Runner(Turn(result="prose"))
    outcome = run_structured_turn(
        runner, prompt_file=Path("/tmp/p.md"), repo_path=Path("/tmp"), attempts=1
    )
    assert outcome.answered
    assert outcome.payload == {}


def test_turn_arguments_are_passed_through():
    class Recorder:
        def __init__(self):
            self.kwargs = None

        def run_turn(self, **kwargs):
            self.kwargs = kwargs
            return Turn()

    runner = Recorder()
    run_structured_turn(
        runner, prompt_file=Path("/tmp/p.md"), repo_path=Path("/tmp"),
        model_id="m", timeout_seconds=30,
    )
    assert runner.kwargs["model_id"] == "m"
    assert runner.kwargs["timeout_seconds"] == 30


# -- the general loop, against the shape each consumer actually has --------------


from agent_core.harness.turns import TurnLoop, run_until_accepted


def test_a_repair_loop_gets_the_previous_failure_as_feedback():
    """UTA's shape: the tests did not compile, so tell the model what broke.

    A fixed prompt would ask for the same mistake again, so the reason a
    result was rejected has to reach the next attempt.
    """
    seen_feedback = []
    compiles_on = 3

    def run(attempt, feedback):
        seen_feedback.append(feedback)
        return {"attempt": attempt, "compiles": attempt >= compiles_on}

    def accept(result):
        return None if result["compiles"] else "compile error: cannot find symbol Foo"

    loop = run_until_accepted(run, accept=accept, attempts=4)

    assert loop.accepted
    assert loop.attempts == 3
    assert seen_feedback == [None, "compile error: cannot find symbol Foo",
                             "compile error: cannot find symbol Foo"]


def test_a_repair_loop_that_never_compiles_reports_the_last_reason():
    def run(attempt, feedback):
        return {"compiles": False}

    loop = run_until_accepted(
        run, accept=lambda r: "still broken", attempts=2
    )
    assert loop.status == REJECTED
    assert loop.reason == "still broken"
    assert loop.attempts == 2


def test_a_non_transient_error_is_not_retried():
    """The spec generator's shape: retrying a refused request spends money twice."""
    calls = []

    def run(attempt, feedback):
        calls.append(attempt)
        raise RuntimeError("invalid request: context too long")

    loop = run_until_accepted(
        run,
        accept=lambda r: None,
        attempts=5,
        retryable=lambda exc: "timeout" in str(exc),
    )

    assert calls == [1], "a permanent failure must not be tried again"
    assert loop.status == UNREACHABLE
    assert isinstance(loop.exception, RuntimeError)


def test_a_transient_error_is_retried():
    attempts_seen = []

    def run(attempt, feedback):
        attempts_seen.append(attempt)
        if attempt == 1:
            raise RuntimeError("timeout talking to provider")
        return "fine"

    loop = run_until_accepted(
        run, accept=lambda r: None, attempts=3, retryable=lambda exc: "timeout" in str(exc)
    )
    assert loop.accepted
    assert attempts_seen == [1, 2]


def test_raise_if_failed_reraises_the_original_exception():
    """So the traceback points at the failure, not at this loop."""
    boom = RuntimeError("invalid request")

    def run(attempt, feedback):
        raise boom

    loop = run_until_accepted(run, accept=lambda r: None, attempts=1)
    with pytest.raises(RuntimeError) as caught:
        loop.raise_if_failed()
    assert caught.value is boom


def test_raise_if_failed_returns_the_result_when_accepted():
    loop = run_until_accepted(lambda attempt, feedback: "ok", accept=lambda r: None)
    assert loop.raise_if_failed() == "ok"


def test_an_exception_with_no_retries_left_is_unreachable_not_rejected():
    """Never got an answer, versus got one and did not like it."""
    def run(attempt, feedback):
        raise RuntimeError("connection refused")

    assert run_until_accepted(run, accept=lambda r: None, attempts=2).status == UNREACHABLE


def test_the_structured_turn_is_the_general_loop_underneath():
    """The common case must not be a second implementation of the same thing."""
    import inspect

    from agent_core.harness.turns import run_structured_turn

    assert "run_until_accepted" in inspect.getsource(run_structured_turn)
