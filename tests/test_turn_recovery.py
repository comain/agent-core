"""Salvaging a stalled turn instead of paying for it twice.

Retrying runs the turn again from nothing. For a stall that is the wrong move
by a wide margin: a turn that has spent several hundred thousand tokens
exploring a repository and then went quiet needs one nudge, not a fresh start
that pays for the exploration again and usually arrives at the same place.

One consumer had this and the other lost it during extraction, silently --
the retry path still worked, it just cost a full turn every time.
"""

from __future__ import annotations

import pytest

from agent_core.harness.turns import ACCEPTED, REJECTED, run_until_accepted


def runner(*results):
    """A `run` that returns each result in turn."""
    seq = list(results)
    calls = []

    def run(*, attempt, feedback=None):
        calls.append({"attempt": attempt, "feedback": feedback})
        return seq[min(attempt, len(seq)) - 1]

    run.calls = calls
    return run


def accept_ok(result):
    return None if result == "ok" else result


def test_a_stall_is_recovered_without_a_fresh_turn():
    run = runner("stalled")

    loop = run_until_accepted(
        run,
        accept=accept_ok,
        attempts=3,
        recover=lambda result, reason: "ok",
    )

    assert loop.status == ACCEPTED
    assert loop.result == "ok"
    assert len(run.calls) == 1, "recovery must not spend an attempt"


def test_recovery_does_not_consume_an_attempt():
    """A model that stalls every time still gets all its fresh attempts."""
    run = runner("stalled", "stalled", "ok")

    loop = run_until_accepted(
        run,
        accept=accept_ok,
        attempts=3,
        recover=lambda result, reason: None,
    )

    assert loop.status == ACCEPTED
    assert len(run.calls) == 3


def test_a_recovered_result_still_has_to_be_accepted():
    """Being salvaged is not the same as being right."""
    run = runner("stalled", "ok")

    loop = run_until_accepted(
        run,
        accept=accept_ok,
        attempts=2,
        recover=lambda result, reason: "still wrong",
    )

    assert loop.status == ACCEPTED
    assert len(run.calls) == 2, "the unaccepted recovery fell through to a retry"


def test_recovery_is_tried_at_most_once_per_attempt():
    """Otherwise a permanently stalled model loops forever being nudged."""
    recoveries = []
    run = runner("stalled")

    def recover(*, result, reason):
        recoveries.append(reason)
        return "stalled"

    loop = run_until_accepted(run, accept=accept_ok, attempts=1, recover=recover)

    assert loop.status == REJECTED
    assert len(recoveries) == 1


def test_only_recoverable_rejections_are_recovered():
    """A wrong answer needs a new turn; a stall does not."""
    recoveries = []
    run = runner("wrong", "ok")

    loop = run_until_accepted(
        run,
        accept=accept_ok,
        attempts=2,
        recoverable=lambda result: result == "stalled",
        recover=lambda result, reason: recoveries.append(result) or "ok",
    )

    assert loop.status == ACCEPTED
    assert recoveries == [], "a wrong answer must not be nudged"
    assert len(run.calls) == 2


def test_the_rejection_reason_reaches_the_recovery():
    seen = {}
    run = runner("stalled")

    def recover(*, result, reason):
        seen["reason"] = reason
        seen["result"] = result
        return "ok"

    run_until_accepted(run, accept=accept_ok, attempts=1, recover=recover)

    assert seen == {"reason": "stalled", "result": "stalled"}


def test_a_failed_recovery_does_not_lose_the_attempt():
    """A rescue that raises must fall through to the ordinary retry."""
    run = runner("stalled", "ok")

    def recover(*, result, reason):
        raise RuntimeError("the session was already gone")

    loop = run_until_accepted(run, accept=accept_ok, attempts=2, recover=recover)

    assert loop.status == ACCEPTED
    assert len(run.calls) == 2


def test_recovery_is_reported():
    """Operators ask why a turn took two prompts."""
    events = []
    run = runner("stalled")

    run_until_accepted(
        run,
        accept=accept_ok,
        attempts=1,
        recover=lambda result, reason: "ok",
        on_recover=lambda attempt, reason: events.append((attempt, reason)),
    )

    assert events == [(1, "stalled")]


def test_a_reporting_hook_that_raises_does_not_fail_the_turn():
    run = runner("stalled")

    loop = run_until_accepted(
        run,
        accept=accept_ok,
        attempts=1,
        recover=lambda result, reason: "ok",
        on_recover=lambda attempt, reason: (_ for _ in ()).throw(ValueError("boom")),
    )

    assert loop.status == ACCEPTED


def test_nothing_changes_when_no_recovery_is_offered():
    """The existing contract, unaltered."""
    run = runner("stalled", "ok")

    loop = run_until_accepted(run, accept=accept_ok, attempts=2)

    assert loop.status == ACCEPTED
    assert len(run.calls) == 2
