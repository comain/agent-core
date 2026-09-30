"""Whether a turn needed its one in-session nudge.

A run that stalled and was recovered cost two prompts rather than one, and
succeeded for a different reason than a run that answered first time. Without
a flag, the two are indistinguishable in a report -- so a recovery mechanism
that has quietly stopped working, or one firing on every turn, both look
exactly like normal operation.

Additive and defaulted, because existing construction sites must not change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.harness.node import NodeOutcome, run_harness_node
from agent_core.harness.turns import ACCEPTED, TurnLoop, run_until_accepted


# -- the flag is additive --------------------------------------------------

def test_turn_loop_defaults_to_not_recovered():
    assert TurnLoop(status=ACCEPTED).recovered is False


def test_node_outcome_defaults_to_not_recovered():
    assert NodeOutcome(status=ACCEPTED).recovered is False


def test_existing_construction_is_unaffected():
    """Every current call site passes no `recovered`; none should change."""
    assert TurnLoop(status=ACCEPTED, result="x", attempts=2).accepted
    assert NodeOutcome(status=ACCEPTED, attempts=1).accepted


# -- it is set only when a recovery was actually accepted -------------------

def _run(*results):
    seq = list(results)
    calls = []

    def run(*, attempt, feedback=None):
        calls.append(attempt)
        return seq[min(attempt, len(seq)) - 1]

    run.calls = calls
    return run


def accept_ok(result):
    return None if result == "ok" else result


def test_a_first_time_answer_is_not_recovered():
    loop = run_until_accepted(_run("ok"), accept=accept_ok, attempts=2)

    assert loop.accepted
    assert loop.recovered is False


def test_a_recovered_answer_is_marked():
    loop = run_until_accepted(
        _run("stalled"), accept=accept_ok, attempts=2,
        recover=lambda result, reason: "ok",
    )

    assert loop.accepted
    assert loop.recovered is True


def test_a_recovery_that_did_not_help_is_not_marked_recovered():
    """Recovery ran, the answer still came from a fresh attempt. Marking that
    recovered would credit the nudge with a success it did not produce."""
    run = _run("stalled", "ok")
    loop = run_until_accepted(
        run, accept=accept_ok, attempts=2,
        recover=lambda result, reason: None,
    )

    assert loop.accepted
    assert loop.recovered is False
    assert run.calls == [1, 2]


def test_a_rejected_recovery_result_is_not_marked():
    """The salvaged value went back through accept and failed."""
    run = _run("stalled", "ok")
    loop = run_until_accepted(
        run, accept=accept_ok, attempts=2,
        recover=lambda result, reason: "still wrong",
    )

    assert loop.recovered is False


def test_the_node_projects_the_loops_flag(tmp_path):
    """`AgentTurnResult.recovered` sources from here, so the projection has to
    survive the hop from loop to outcome."""
    (tmp_path / "p.md").write_text("hi")
    calls = {"n": 0}

    class Runner:
        def run_turn(self, *, prompt_file, repo_path, **kw):
            calls["n"] += 1
            class R:
                # "stalled" is not a stall to `is_stall`; the vocabulary is
                # stalled_no_progress / stalled_after_recovery.
                type = "completed" if calls["n"] > 1 else "stalled_no_progress"
                result = "ok" if calls["n"] > 1 else ""
                session_id = "s"; tokens = {}; cost_usd = 0.0
                model_id = "m"; patch_count = 0; raw_log_path = None; error = None
            return R()

    outcome = run_harness_node(
        Runner(), name="probe", repo_path=tmp_path,
        prompt=lambda attempt, feedback: tmp_path / "p.md",
        recovery_prompt=lambda result, reason: tmp_path / "p.md",
        attempts=1,
    )

    assert outcome.recovered is True
