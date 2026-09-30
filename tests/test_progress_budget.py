"""Capacity accounting for progress that must never cost correctness.

A streaming model can emit progress far faster than a database wants to be
written to, so progress is bounded. The bound is a two-phase reservation
because the authoritative admission happens in the product's `append_batch`,
possibly in another process: the in-memory number here is provisional until
that call reports what it actually took.

The two failure modes this exists to prevent:

* **double counting** — a retried batch charged twice, so a task silently
  loses its remaining progress budget while well under the real limit;
* **leaking capacity** — a failed batch never released, so the budget drains
  to zero over a long run and progress stops for no visible reason.

Both are invisible in production: progress just quietly stops. Hence tests.
"""

from __future__ import annotations

import pytest

from agent_core.runtime.progress_sink import (
    AppendBatchResult,
    ProgressBudget,
    ProgressReservation,
)


def budget(**kw) -> ProgressBudget:
    return ProgressBudget(max_events=kw.get("events", 100), max_serialized_bytes=kw.get("bytes", 10_000))


# -- reserving ---------------------------------------------------------------

def test_a_reservation_within_the_budget_is_granted():
    reservation = budget().reserve(events=10, serialized_bytes=500)

    assert reservation == ProgressReservation(events=10, serialized_bytes=500)


def test_a_reservation_past_the_event_limit_is_refused():
    b = budget(events=10)
    assert b.reserve(events=10, serialized_bytes=1) is not None
    assert b.reserve(events=1, serialized_bytes=1) is None


def test_a_reservation_past_the_byte_limit_is_refused():
    """Either limit, whichever is reached first."""
    b = budget(events=1_000_000, bytes=100)
    assert b.reserve(events=1, serialized_bytes=100) is not None
    assert b.reserve(events=1, serialized_bytes=1) is None


def test_a_refused_reservation_consumes_nothing():
    """A refusal must not partially charge, or the next caller sees less
    capacity than was actually used."""
    b = budget(events=10)
    b.reserve(events=10, serialized_bytes=10)
    before = (b.used_events, b.used_serialized_bytes)

    assert b.reserve(events=5, serialized_bytes=5) is None
    assert (b.used_events, b.used_serialized_bytes) == before


def test_reserving_nothing_is_not_an_error():
    assert budget().reserve(events=0, serialized_bytes=0) == ProgressReservation(0, 0)


# -- committing what was actually admitted -----------------------------------

def test_committing_the_full_reservation_charges_it_once():
    b = budget()
    reservation = b.reserve(events=10, serialized_bytes=500)

    b.commit(reservation, AppendBatchResult(10, 500, truncated=False))

    assert (b.used_events, b.used_serialized_bytes) == (10, 500)


def test_a_partially_admitted_batch_releases_the_remainder():
    """The product is the authority. If it admitted 4 of 10, the other 6 are
    capacity this task still has -- charging them would shrink the budget by
    the size of every rejection."""
    b = budget()
    reservation = b.reserve(events=10, serialized_bytes=500)

    b.commit(reservation, AppendBatchResult(4, 200, truncated=True))

    assert (b.used_events, b.used_serialized_bytes) == (4, 200)


def test_a_released_reservation_returns_all_capacity():
    b = budget()
    reservation = b.reserve(events=10, serialized_bytes=500)

    b.release(reservation)

    assert (b.used_events, b.used_serialized_bytes) == (0, 0)


def test_reserve_release_reserve_does_not_leak():
    """The retry path: a failed append releases, then the retry reserves again.
    Over many retries this must not drain the budget."""
    b = budget(events=10)
    for _ in range(50):
        reservation = b.reserve(events=10, serialized_bytes=100)
        assert reservation is not None, "capacity leaked across retries"
        b.release(reservation)

    assert b.used_events == 0


def test_committing_more_than_reserved_charges_only_the_reservation():
    """A product reporting a larger admission than was reserved is a bug on
    its side; trusting it would let one batch consume an unbounded budget."""
    b = budget()
    reservation = b.reserve(events=5, serialized_bytes=50)

    b.commit(reservation, AppendBatchResult(500, 5_000, truncated=False))

    assert b.used_events == 5
    assert b.used_serialized_bytes == 50


def test_a_reservation_cannot_be_settled_twice():
    """Double-settling is the double-count bug in its simplest form: a commit
    followed by a release of the same token would refund capacity that was
    genuinely spent."""
    b = budget()
    reservation = b.reserve(events=10, serialized_bytes=100)
    b.commit(reservation, AppendBatchResult(10, 100, truncated=False))

    with pytest.raises(ValueError):
        b.release(reservation)
    with pytest.raises(ValueError):
        b.commit(reservation, AppendBatchResult(10, 100, truncated=False))

    assert b.used_events == 10


def test_settling_a_foreign_reservation_is_refused():
    """A token from another budget carries no capacity here."""
    other = budget().reserve(events=5, serialized_bytes=5)

    with pytest.raises(ValueError):
        budget().commit(other, AppendBatchResult(5, 5, truncated=False))


# -- restart -----------------------------------------------------------------

def test_a_budget_can_start_from_already_persisted_usage():
    """Restart must not hand a long-running task a fresh full budget; the rows
    it already wrote are still in the product's database."""
    b = ProgressBudget(
        max_events=100, max_serialized_bytes=1_000,
        used_events=95, used_serialized_bytes=990,
    )

    assert b.reserve(events=10, serialized_bytes=10) is None
    assert b.reserve(events=5, serialized_bytes=10) is not None


def test_an_exhausted_budget_reports_itself():
    """The caller needs to distinguish "refused, try smaller" from "done", so
    it can write the truncation marker exactly once."""
    b = budget(events=10)
    assert not b.exhausted
    b.commit(b.reserve(events=10, serialized_bytes=1), AppendBatchResult(10, 1, False))
    assert b.exhausted
