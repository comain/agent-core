"""Counting what was actually submitted to a provider.

One `run_turn` is not one charge. A chain that fails over twice submitted three
times; a stalled turn nudged back to life submitted twice; a rejected answer
retried submitted again. These tests pin the three properties that make the
count usable: one monotonic sequence across all three mechanisms, the charge
reported before the next admission, and an unknown charge reported rather than
skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.harness.cost import (
    CostAdmissionError,
    PaidAttempts,
    TurnCost,
    aggregate_turn_costs,
)
from agent_core.harness.node import run_harness_node
from agent_core.harness.sessions import FallbackHarnessSession, SessionSnapshot


class Recorder:
    """A product's cost policy: records everything, refuses on demand."""

    def __init__(self, *, refuse_from: int | None = None, budget: float | None = None):
        self.trace: list = []
        self.refuse_from = refuse_from
        self.budget = budget
        self.spent = 0.0

    def before_paid_attempt(self, *, operation_id, paid_attempt_ordinal):
        self.trace.append(("before", paid_attempt_ordinal))
        if self.refuse_from is not None and paid_attempt_ordinal >= self.refuse_from:
            raise CostAdmissionError(f"attempt {paid_attempt_ordinal} is over the cap")
        if self.budget is not None and self.spent >= self.budget:
            raise CostAdmissionError("the budget is spent")

    def after_paid_attempt(self, *, operation_id, paid_attempt_ordinal, cost):
        self.trace.append(("after", paid_attempt_ordinal, cost.provider_cost_usd))
        if cost.known:
            self.spent += float(cost.provider_cost_usd)


class Turn:
    def __init__(self, type="completed", session_id="s1", cost_usd=None):
        self.type = type
        self.result = "done"
        self.session_id = session_id
        self.cost_usd = cost_usd
        self.session_refs = ()


def prompt_file(tmp_path):
    path = tmp_path / "prompt.md"
    path.write_text("do the thing")
    return path


# -- the ledger ------------------------------------------------------------

def test_every_submission_is_admitted_then_reported():
    port = Recorder()
    ledger = PaidAttempts(port, operation_id="op")

    with ledger.submission() as attempt:
        attempt.record_provider(0.10)
    with ledger.submission() as attempt:
        attempt.record_provider(0.25)

    assert port.trace == [
        ("before", 1), ("after", 1, 0.10),
        ("before", 2), ("after", 2, 0.25),
    ], "a charge that arrives after the next admission is not a cap"


def test_a_submission_that_raises_still_reports_an_unknown_charge():
    """The request reached the provider; saying nothing lets the next
    admission believe the budget is intact."""
    port = Recorder()
    ledger = PaidAttempts(port, operation_id="op")

    with pytest.raises(RuntimeError):
        with ledger.submission():
            raise RuntimeError("the gateway hung up mid-stream")

    assert port.trace == [("before", 1), ("after", 1, None)]


def test_an_unknown_charge_poisons_the_aggregate():
    """A partial total looks authoritative, is always an underestimate, and a
    cap compared against it approves spend that already happened."""
    assert aggregate_turn_costs([TurnCost.from_provider(0.1), TurnCost.unavailable()]) == (
        TurnCost.unavailable()
    )


def test_a_reported_zero_is_a_number_not_a_missing_value():
    total = aggregate_turn_costs([TurnCost.from_provider(0.0), TurnCost.from_provider(0.5)])

    assert total.known and total.provider_cost_usd == 0.5


def test_nothing_submitted_is_not_a_zero_bill():
    assert aggregate_turn_costs([]) == TurnCost.unavailable()


def test_a_recording_failure_during_an_error_does_not_mask_it():
    """The turn's own failure is the real cause. The bookkeeping error closes
    the ledger instead, so the next spend is what fails."""
    class Broken(Recorder):
        def after_paid_attempt(self, **kwargs):
            raise RuntimeError("the ledger table is gone")

    ledger = PaidAttempts(Broken(), operation_id="op")

    with pytest.raises(RuntimeError, match="the gateway hung up"):
        with ledger.submission():
            raise RuntimeError("the gateway hung up")

    with pytest.raises(CostAdmissionError, match="could not be recorded"):
        with ledger.submission():
            pass


# -- through the node ------------------------------------------------------

def test_retries_draw_from_one_sequence(tmp_path):
    """Numbering per mechanism would let three separate "attempt 1"s spend
    three times a cap meant to allow one."""
    port = Recorder()

    class Rejecting:
        def run_turn(self, **kwargs):
            return Turn(type="error", session_id=None, cost_usd=0.10)

    outcome = run_harness_node(
        Rejecting(),
        name="generate",
        repo_path=tmp_path,
        prompt=lambda attempt, feedback: prompt_file(tmp_path),
        attempts=3,
        paid_attempts=PaidAttempts(port, operation_id="op"),
    )

    assert not outcome.accepted
    assert [entry[1] for entry in port.trace] == [1, 1, 2, 2, 3, 3]


def test_an_in_place_recovery_is_a_paid_attempt_of_its_own(tmp_path):
    """A nudge is cheaper than a fresh attempt but it is not free, and a
    stall-prone model must not spend past a cap through a mechanism that
    numbers its submissions separately."""
    port = Recorder()
    turns = iter(
        [
            Turn(type="stalled", session_id=None, cost_usd=0.10),
            Turn(type="completed", session_id="s1", cost_usd=0.05),
        ]
    )

    class Stalling:
        def run_turn(self, **kwargs):
            return next(turns)

    outcome = run_harness_node(
        Stalling(),
        name="generate",
        repo_path=tmp_path,
        prompt=lambda attempt, feedback: prompt_file(tmp_path),
        attempts=2,
        recovery_prompt=lambda result, reason: prompt_file(tmp_path),
        recoverable=lambda result: True,
        paid_attempts=PaidAttempts(port, operation_id="op"),
    )

    assert outcome.accepted and outcome.recovered
    assert port.trace == [
        ("before", 1), ("after", 1, 0.10),
        ("before", 2), ("after", 2, 0.05),
    ]


def test_the_first_attempt_can_consume_the_budget_and_block_the_second(tmp_path):
    """The property a cap exists for: attempt one's charge is known before
    attempt two is admitted, so the second submission never happens."""
    port = Recorder(budget=0.20)
    submitted: list = []

    class Rejecting:
        def run_turn(self, **kwargs):
            submitted.append(1)
            return Turn(type="error", session_id=None, cost_usd=0.25)

    outcome = run_harness_node(
        Rejecting(),
        name="generate",
        repo_path=tmp_path,
        prompt=lambda attempt, feedback: prompt_file(tmp_path),
        attempts=2,
        paid_attempts=PaidAttempts(port, operation_id="op"),
    )

    assert submitted == [1], "the second attempt was submitted anyway"
    assert outcome.status == "unreachable"
    assert port.trace == [("before", 1), ("after", 1, 0.25), ("before", 2)]


def test_a_refused_admission_is_never_retried(tmp_path):
    """Retrying is exactly how a cap's "no" turns into the spend it was there
    to prevent."""
    port = Recorder(refuse_from=1)
    submitted: list = []

    class Anything:
        def run_turn(self, **kwargs):
            submitted.append(1)
            return Turn()

    outcome = run_harness_node(
        Anything(),
        name="generate",
        repo_path=tmp_path,
        prompt=lambda attempt, feedback: prompt_file(tmp_path),
        attempts=3,
        paid_attempts=PaidAttempts(port, operation_id="op"),
    )

    assert submitted == []
    assert outcome.status == "unreachable"
    assert [entry[1] for entry in port.trace] == [1]


# -- who does the counting -------------------------------------------------

def test_a_chain_walking_harness_counts_its_own_candidates(tmp_path):
    """Counted once outside and once inside is worse than not counted: the
    number still looks plausible."""
    port = Recorder()

    class Candidate:
        def __init__(self, name, *, fallback, cost):
            self.name = name
            self.session_id = f"session-{name}"
            self.fallback = fallback
            self.cost = cost

        def run_turn(self, **kwargs):
            result = Turn(
                type="error" if self.fallback else "completed",
                session_id=self.session_id,
                cost_usd=self.cost,
            )
            result.fallback_eligible = self.fallback
            return result

        def snapshot(self):
            return SessionSnapshot(session_id=self.session_id, provider_cost_usd=self.cost)

        def close(self):
            pass

    candidates = iter(
        (Candidate("first", fallback=True, cost=0.10),
         Candidate("second", fallback=False, cost=0.25))
    )
    session = FallbackHarnessSession(lambda model: next(candidates), models=("a", "b"))

    outcome = run_harness_node(
        session,
        name="generate",
        repo_path=tmp_path,
        prompt=lambda attempt, feedback: prompt_file(tmp_path),
        paid_attempts=PaidAttempts(port, operation_id="op"),
    )

    assert outcome.accepted
    assert port.trace == [
        ("before", 1), ("after", 1, 0.10),
        ("before", 2), ("after", 2, 0.25),
    ], "the chain was counted as one attempt, or as three"
