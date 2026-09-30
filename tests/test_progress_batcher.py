"""The default sink: buffering, sampling, and refusing to ever raise.

A harness calls `publish` *inline* while polling a provider. Everything here
follows from that one fact. If this code raises, a model turn that actually
succeeded gets aborted or reclassified — a real failure traded for a cosmetic
one. So the central tests are the ones where the product's database is broken
and progress still cannot break anything.

The second theme is bounds. A streaming model emits progress far faster than a
database wants writes, so events are coalesced, sampled, and capped. Under
pressure something must be dropped; what must *not* be dropped is the handful
of updates a person actually needs — the error, the rate limit, the final
state — so those hold reserved slots that ordinary chatter cannot evict.
"""

from __future__ import annotations

import pytest

from agent_core.runtime.progress import AgentProgressEvent
from agent_core.runtime.progress_sink import (
    AppendBatchResult,
    ProgressBatcher,
    ProgressBudget,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingPort:
    """A product's append port, scriptable into every way it can misbehave."""

    def __init__(self, *, admits=None, raises=None) -> None:
        self.batches: list = []
        self.admits = admits
        self.raises = raises
        self.calls = 0

    def __call__(self, *, session_id, events):
        self.calls += 1
        if self.raises:
            raise self.raises
        self.batches.append((session_id, list(events)))
        if self.admits is not None:
            return self.admits
        return AppendBatchResult(len(events), sum(len(e.summary) for e in events), False)

    @property
    def delivered(self) -> list:
        return [event for _, events in self.batches for event in events]


def event(sequence=1, *, kind="text", summary="Agent update", session="s1", **kw):
    return AgentProgressEvent(
        sequence=sequence, session_id=session, phase="generate",
        kind=kind, summary=summary, **kw,
    )


def batcher(port, clock=None, **kw):
    return ProgressBatcher(port, clock=clock or FakeClock(), **kw)


# -- the no-throw boundary ---------------------------------------------------

def test_publish_swallows_an_append_failure():
    """The database is down. The turn is not."""
    sink = batcher(RecordingPort(raises=RuntimeError("db is gone")))

    sink.publish(event())
    sink.publish(event(2))  # no raise


def test_flush_swallows_an_append_failure_and_reports_it():
    port = RecordingPort(raises=RuntimeError("db is gone"))
    sink = batcher(port)
    sink.publish(event())

    result = sink.flush()

    assert result.failures  # latched, not raised
    assert result.delivered_events == 0


def test_a_failure_diagnostic_carries_no_raw_error_text():
    """A provider or driver exception can quote credentials, a query, or
    repository content. Only the stage crosses."""
    secret = RuntimeError("connect failed: password=hunter2 host=internal.db")
    sink = batcher(RecordingPort(raises=secret))
    sink.publish(event())

    result = sink.flush()

    assert "hunter2" not in repr(result)
    assert "internal.db" not in repr(result)


def test_record_failure_never_raises_and_accepts_no_error_object():
    sink = batcher(RecordingPort())

    sink.record_failure(stage="projection")
    sink.record_failure(stage="delivery")

    assert sink.flush().failures["projection"] == 1


def test_close_swallows_a_failing_port():
    sink = batcher(RecordingPort(raises=RuntimeError("gone")))
    sink.publish(event())

    sink.close()  # no raise


def test_publishing_after_close_is_ignored_not_an_error():
    """Ordering between a late provider callback and cleanup is not something
    a harness can guarantee."""
    port = RecordingPort()
    sink = batcher(port)
    sink.close()

    sink.publish(event())

    assert port.delivered == []


# -- sampling ----------------------------------------------------------------

def test_a_burst_within_one_window_becomes_one_batch():
    """The whole point: a streaming model must not become a write loop."""
    port, clock = RecordingPort(), FakeClock()
    sink = batcher(port, clock)

    for i in range(50):
        sink.publish(event(i, summary=f"update {i}"))

    assert port.calls <= 1


def test_the_sampling_window_admits_two_sends_per_second():
    port, clock = RecordingPort(), FakeClock()
    sink = batcher(port, clock)

    for i in range(10):
        sink.publish(event(i, summary=f"a{i}"))
        clock.advance(0.5)

    # 10 events over 5 seconds at 2/second
    assert 8 <= port.calls <= 11


def test_flush_delivers_regardless_of_the_window():
    """A flush is the last chance; the sampling window must not withhold."""
    port, clock = RecordingPort(), FakeClock()
    sink = batcher(port, clock)
    sink.publish(event())

    result = sink.flush()

    assert result.delivered_events == 1
    assert len(port.delivered) == 1


# -- coalescing and separation -----------------------------------------------

def test_repeated_identical_updates_coalesce():
    port = RecordingPort()
    sink = batcher(port)

    for i in range(20):
        sink.publish(event(i, kind="tool", tool="bash", status="running", summary="Ran repository checks"))
    sink.flush()

    assert len(port.delivered) == 1


def test_a_changed_field_is_not_coalesced_away():
    port = RecordingPort()
    sink = batcher(port)

    sink.publish(event(1, kind="tool", tool="bash", status="running", summary="s"))
    sink.publish(event(2, kind="tool", tool="bash", status="completed", summary="s"))
    sink.flush()

    assert len(port.delivered) == 2


def test_parallel_sessions_stay_separate():
    """Two units generating at once must not merge into one timeline."""
    port = RecordingPort()
    sink = batcher(port)

    sink.publish(event(1, session="unit-a"))
    sink.publish(event(2, session="unit-b"))
    sink.flush()

    assert {session for session, _ in port.batches} == {"unit-a", "unit-b"}


# -- pressure ----------------------------------------------------------------

def test_pending_events_are_bounded_per_session():
    port = RecordingPort(raises=RuntimeError("down"))  # nothing drains
    sink = batcher(port, max_pending_per_session=8)

    for i in range(100):
        sink.publish(event(i, summary=f"u{i}"))

    assert sink.pending_count("s1") <= 8


def test_pressure_drops_the_oldest_and_counts_it():
    port = RecordingPort(raises=RuntimeError("down"))
    sink = batcher(port, max_pending_per_session=4)

    for i in range(20):
        sink.publish(event(i, summary=f"u{i}"))

    assert sink.flush().dropped_count > 0


def test_a_critical_update_is_never_evicted_by_ordinary_traffic():
    """The one thing a person needs from a flooded task is the error."""
    port = RecordingPort(raises=RuntimeError("down"))
    sink = batcher(port, max_pending_per_session=4)

    sink.publish(event(0, kind="error", summary="Agent needs attention"))
    for i in range(1, 200):
        sink.publish(event(i, summary=f"chatter {i}"))

    port.raises = None
    sink.flush()

    assert any(e.kind == "error" for e in port.delivered), "the error was evicted by chatter"


def test_a_later_critical_update_coalesces_onto_the_reserved_slot():
    """Reserved does not mean unbounded: repeated errors keep the latest."""
    port = RecordingPort(raises=RuntimeError("down"))
    sink = batcher(port, max_pending_per_session=4)

    for i in range(50):
        sink.publish(event(i, kind="error", summary=f"error {i}"))

    port.raises = None
    sink.flush()

    errors = [e for e in port.delivered if e.kind == "error"]
    assert len(errors) == 1
    assert errors[0].summary == "error 49"


def test_ordinary_progress_is_capped_per_session():
    """The cap bounds progress, not the marker that explains the cap."""
    port = RecordingPort()
    sink = batcher(port, max_events_per_session=10)

    for i in range(100):
        sink.publish(event(i, summary=f"u{i}"))
        sink.flush()

    ordinary = [e for e in port.delivered if e.kind != "progress_truncated"]
    assert len(ordinary) <= 10


def test_the_truncation_marker_is_written_once():
    port = RecordingPort()
    sink = batcher(port, max_events_per_session=5)

    for i in range(100):
        sink.publish(event(i, summary=f"u{i}"))
        sink.flush()

    markers = [e for e in port.delivered if e.kind == "progress_truncated"]
    assert len(markers) == 1


# -- budget integration ------------------------------------------------------

def test_a_failed_append_releases_its_reservation():
    """Otherwise a long run drains its budget through failures alone and
    progress stops with nothing to show for it."""
    budget = ProgressBudget(max_events=100, max_serialized_bytes=100_000)
    port = RecordingPort(raises=RuntimeError("down"))
    sink = batcher(port, budget=budget)

    for i in range(20):
        sink.publish(event(i, summary=f"u{i}"))
        sink.flush()

    assert budget.used_events == 0


def test_a_partial_admission_charges_only_what_was_admitted():
    budget = ProgressBudget(max_events=100, max_serialized_bytes=100_000)
    port = RecordingPort(admits=AppendBatchResult(1, 10, truncated=True))
    sink = batcher(port, budget=budget)

    sink.publish(event(1, summary="a"))
    sink.publish(event(2, kind="tool", tool="read", status="completed", summary="b"))
    sink.flush()

    assert budget.used_events == 1


def test_an_exhausted_budget_stops_delivery_without_error():
    budget = ProgressBudget(max_events=2, max_serialized_bytes=100_000)
    port = RecordingPort()
    sink = batcher(port, budget=budget)

    for i in range(50):
        sink.publish(event(i, summary=f"u{i}"))
        sink.flush()

    assert len(port.delivered) <= 3  # the admitted events plus one marker


# -- flush -------------------------------------------------------------------

def test_flush_retries_once_and_no_more():
    """Bounded: a flush happens on the turn's critical path, before the phase
    session closes. An unbounded retry would hold a turn open on a dead
    database."""
    class FlakyPort(RecordingPort):
        def __call__(self, **kw):
            self.calls += 1
            raise RuntimeError("down")

    port = FlakyPort()
    sink = batcher(port)
    sink.publish(event())

    sink.flush()

    assert port.calls == 2


def test_a_flush_with_nothing_pending_is_a_clean_result():
    sink = batcher(RecordingPort())

    result = sink.flush()

    assert result.delivered_events == 0
    assert not result.failures


def test_flush_is_idempotent():
    port = RecordingPort()
    sink = batcher(port)
    sink.publish(event())

    sink.flush()
    sink.flush()

    assert len(port.delivered) == 1


def test_not_configured_is_a_distinct_clean_result():
    """`agent_turn` uses this when no sink was supplied, so the diagnostic can
    say "no progress configured" rather than "progress delivered nothing"."""
    from agent_core.runtime.progress_sink import ProgressFlushResult

    result = ProgressFlushResult.not_configured()

    assert result.configured is False
    assert not result.failures
