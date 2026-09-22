"""Bounding progress so a talkative model cannot become a write loop.

A streaming provider emits progress far faster than a database wants to be
written to, and a harness calls the publish path *inline* while polling that
provider. Two consequences shape everything here:

* **Nothing in this module may raise into its caller.** An exception on the
  progress path would abort or reclassify a model turn that actually
  succeeded -- trading a cosmetic problem for a real one. Failures are
  latched and reported, never propagated.
* **Progress is never authoritative.** A product's own ledger records what
  happened. Losing progress costs a person some visibility; it must never
  cost correctness.

Capacity is a two-phase reservation because the real admission decision
belongs to the product's `append_batch`, possibly in another process. The
in-memory count is provisional until that call reports what it took.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Deque, Dict, List, Mapping, Optional, Protocol,
)

from agent_core.runtime.progress import AgentProgressEvent, project_turn_progress

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProgressReservation:
    """Provisional capacity, held until the product says what it admitted.

    Identity matters, not just the numbers: a reservation is settled exactly
    once, and the budget rejects a second settlement of the same token. A
    commit followed by a release would otherwise refund capacity that was
    genuinely spent, which is the double-count bug in its simplest form.
    """

    events: int
    serialized_bytes: int


@dataclass(frozen=True)
class AppendBatchResult:
    """What the product's append port actually admitted.

    ``truncated`` says the remainder was rejected on purpose rather than lost,
    so the caller can write one truncation marker instead of retrying forever.
    """

    admitted_events: int
    admitted_serialized_bytes: int
    truncated: bool


@dataclass
class ProgressBudget:
    """A task's remaining room for ordinary progress.

    Not frozen: it is the mutable accounting. It is deliberately *not* the
    cross-process authority -- the product's transactional counters are, and
    survive a restart. This exists so a single task in a single process stops
    generating writes long before it reaches that authority.

    ``used_*`` is constructor-visible so a restarting worker starts from what
    it already persisted rather than being handed a fresh full budget.
    """

    max_events: int
    max_serialized_bytes: int
    used_events: int = 0
    used_serialized_bytes: int = 0
    #: Outstanding reservations, by identity. Not part of equality or repr:
    #: it is bookkeeping, not state a caller compares.
    _outstanding: list = field(default_factory=list, repr=False, compare=False)

    @property
    def exhausted(self) -> bool:
        """True when no further ordinary progress will ever be admitted.

        Distinct from a refused reservation, which may only mean "too big".
        The caller uses this to write its truncation marker exactly once.
        """
        return (
            self.used_events >= self.max_events
            or self.used_serialized_bytes >= self.max_serialized_bytes
        )

    def reserve(self, *, events: int, serialized_bytes: int) -> Optional[ProgressReservation]:
        """Hold capacity for a batch, or return None if it will not fit.

        All-or-nothing on purpose. A partial charge on refusal would make the
        used counters disagree with what was actually written, and that
        disagreement only ever grows.
        """
        if self.used_events + events > self.max_events:
            return None
        if self.used_serialized_bytes + serialized_bytes > self.max_serialized_bytes:
            return None

        reservation = ProgressReservation(events=events, serialized_bytes=serialized_bytes)
        self.used_events += events
        self.used_serialized_bytes += serialized_bytes
        self._outstanding.append(reservation)
        return reservation

    def commit(self, reservation: ProgressReservation, admitted: AppendBatchResult) -> None:
        """Charge what the product admitted and refund the rest.

        The product is the authority on what it took. Charging the full
        reservation when it admitted less would shrink the budget by the size
        of every rejection; charging more than was reserved would let one
        batch consume an unbounded amount, so the reservation is the cap.
        """
        self._settle(reservation)
        charged_events = min(max(admitted.admitted_events, 0), reservation.events)
        charged_bytes = min(
            max(admitted.admitted_serialized_bytes, 0), reservation.serialized_bytes
        )
        self.used_events -= reservation.events - charged_events
        self.used_serialized_bytes -= reservation.serialized_bytes - charged_bytes

    def release(self, reservation: ProgressReservation) -> None:
        """Return capacity for a batch that was never admitted.

        The retry path. Without it a long run drains its budget through
        failures alone and progress stops for no visible reason.
        """
        self._settle(reservation)
        self.used_events -= reservation.events
        self.used_serialized_bytes -= reservation.serialized_bytes

    def _settle(self, reservation: ProgressReservation) -> None:
        """Consume the token, refusing a second settlement or a foreign one.

        Matched by identity, not equality: two batches of the same size make
        equal reservations, and `list.remove` would drop whichever came first.
        """
        for index, held in enumerate(self._outstanding):
            if held is reservation:
                del self._outstanding[index]
                return
        raise ValueError("reservation is not outstanding on this budget")


#: Kinds a person needs from a flooded task. They hold reserved per-session
#: slots that ordinary chatter cannot evict, and coalesce onto themselves so
#: "reserved" does not become "unbounded".
CRITICAL_KINDS = frozenset({"error", "rate_limit", "final"})

#: The marker written once per session when its cap was reached, so a reader
#: knows the timeline is incomplete rather than assuming the task went quiet.
TRUNCATION_KIND = "progress_truncated"


@dataclass(frozen=True)
class ProgressFlushResult:
    """What the last delivery attempt managed, for the turn's diagnostics.

    Reported rather than raised. It is attached to `AgentTurnResult`
    diagnostics, where it can explain a missing timeline without ever
    implying anything about whether the turn itself succeeded.

    ``failures`` counts by stage only. A driver or provider exception can
    quote a password, a query, or repository content, so no error text
    crosses this boundary.
    """

    configured: bool = True
    delivered_events: int = 0
    dropped_count: int = 0
    truncated: bool = False
    failures: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def not_configured(cls) -> "ProgressFlushResult":
        """No sink was supplied.

        Distinct from a clean flush so a diagnostic can say "no progress
        configured" rather than "progress delivered nothing", which would look
        like a fault.
        """
        return cls(configured=False)


class AgentProgressSink(Protocol):
    """Where projected progress goes. Implementations must never raise."""

    def publish(self, event: AgentProgressEvent) -> None: ...
    def flush(self, *, timeout_seconds: float = 5) -> ProgressFlushResult: ...
    def record_failure(self, *, stage: str) -> None: ...


@dataclass
class _SessionBuffer:
    """One session's pending events, with the critical ones held apart.

    Ordinary events live in a bounded deque that drops from the left under
    pressure. Critical events live in a dict keyed by kind, so a flood of
    ordinary chatter can never evict the error a person is waiting to see,
    and a repeated error coalesces onto the latest rather than accumulating.
    """

    ordinary: Deque[AgentProgressEvent]
    critical: Dict[str, AgentProgressEvent] = field(default_factory=dict)
    delivered: int = 0
    truncation_marked: bool = False

    def pending(self) -> List[AgentProgressEvent]:
        return list(self.ordinary) + list(self.critical.values())


class ProgressBatcher:
    """The default task-scoped sink: buffer, coalesce, sample, deliver.

    Delivery is driven from `publish` and `flush` rather than a background
    timer. A thread would need its own lifecycle, its own failure path, and a
    guarantee it is joined before the process exits -- for a component whose
    entire contract is that it must never disturb the turn. Opportunistic
    sending has no such surface, and `flush` before the session closes is what
    makes the last events land.

    All state is under one lock: `publish` is called from provider polling
    while `flush` may be called from the node, and the truncation-marker
    decision must happen exactly once.
    """

    def __init__(
        self,
        append_batch: Callable[..., Any],
        *,
        budget: Optional[ProgressBudget] = None,
        max_pending_per_session: int = 256,
        max_events_per_session: int = 2000,
        samples_per_second: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._append_batch = append_batch
        self._budget = budget
        self._max_pending = max_pending_per_session
        self._max_events = max_events_per_session
        self._interval = 1.0 / samples_per_second if samples_per_second > 0 else 0.0
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: Dict[str, _SessionBuffer] = {}
        # The window starts now, not at negative infinity: otherwise the
        # first event of every burst escapes alone before anything can be
        # coalesced with it, and "batch" means "one write plus a batch".
        # The cost is that first progress waits one interval.
        self._last_send = clock()
        self._dropped = 0
        self._failures: Dict[str, int] = {}
        self._delivered = 0
        self._truncated = False
        self._closed = False

    # -- the inline path -----------------------------------------------------

    def publish(self, event: AgentProgressEvent) -> None:
        """Queue an event and maybe deliver. Never raises.

        Called inline while a harness polls a provider. An exception here
        would abort or reclassify a turn that actually succeeded, trading a
        cosmetic failure for a real one.
        """
        try:
            with self._lock:
                if self._closed:
                    # A provider callback arriving after cleanup is ordinary;
                    # a harness cannot guarantee otherwise.
                    return
                self._enqueue(event)
                due = self._clock() - self._last_send >= self._interval
            if due:
                self._deliver(attempts=1)
        except Exception:  # noqa: BLE001 - the whole point of this method
            self.record_failure(stage="delivery")

    def record_failure(self, *, stage: str) -> None:
        """Latch a sanitized diagnostic. Never raises, and takes no error.

        The signature refuses an exception object deliberately: the caller
        cannot accidentally pass one through to storage, where it could carry
        a credential or repository content.
        """
        try:
            with self._lock:
                self._failures[stage] = self._failures.get(stage, 0) + 1
        except Exception:  # noqa: BLE001 - even a broken latch is swallowed
            pass

    # -- the bounded path ----------------------------------------------------

    def flush(self, *, timeout_seconds: float = 5) -> ProgressFlushResult:
        """Deliver what is pending, with one bounded retry. Never raises.

        Bounded because this runs on the turn's critical path, before the
        phase session closes. An unbounded retry would hold a turn open
        against a dead database, which is precisely the correctness cost this
        component exists to avoid.
        """
        deadline = self._clock() + max(float(timeout_seconds), 0.0)
        try:
            self._deliver(attempts=2, deadline=deadline)
        except Exception:  # noqa: BLE001
            self.record_failure(stage="delivery")
        with self._lock:
            return ProgressFlushResult(
                delivered_events=self._delivered,
                dropped_count=self._dropped,
                truncated=self._truncated,
                failures=dict(self._failures),
            )

    def close(self) -> None:
        """Stop accepting events. Never raises."""
        try:
            self.flush()
        finally:
            with self._lock:
                self._closed = True

    def pending_count(self, session_id: str) -> int:
        with self._lock:
            buffer = self._sessions.get(session_id)
            return len(buffer.pending()) if buffer else 0

    # -- internals -----------------------------------------------------------

    def _enqueue(self, event: AgentProgressEvent) -> None:
        session_id = str(event.session_id or "")
        buffer = self._sessions.get(session_id)
        if buffer is None:
            buffer = _SessionBuffer(ordinary=deque())
            self._sessions[session_id] = buffer

        if event.kind in CRITICAL_KINDS:
            # Keyed by kind: the latest error replaces the previous one rather
            # than queueing behind it.
            buffer.critical[event.kind] = event
            return

        if buffer.ordinary and _coalesces(buffer.ordinary[-1], event):
            buffer.ordinary[-1] = event
            return

        buffer.ordinary.append(event)
        while len(buffer.ordinary) > self._max_pending:
            buffer.ordinary.popleft()
            self._dropped += 1
            self._truncated = True

    def _deliver(self, *, attempts: int, deadline: Optional[float] = None) -> None:
        for attempt in range(attempts):
            with self._lock:
                batches = [
                    (session_id, buffer, buffer.pending())
                    for session_id, buffer in self._sessions.items()
                    if buffer.pending()
                ]
            if not batches:
                return
            if deadline is not None and attempt and self._clock() > deadline:
                return

            failed = False
            for session_id, buffer, events in batches:
                if not self._send(session_id, buffer, events):
                    failed = True
            if not failed:
                return

    def _send(self, session_id: str, buffer: _SessionBuffer, events: List) -> bool:
        """Deliver one session's batch. Returns False when it should be retried.

        Retry means "the storage call failed and might succeed". Running out
        of per-session or budget capacity is *not* retryable -- capacity does
        not come back within a task -- so those paths retire the buffer and
        report success, having emitted one truncation marker so the gap is
        visible rather than looking like a task that went quiet.
        """
        with self._lock:
            allowance = self._max_events - buffer.delivered
            capped = allowance <= 0
            admitted_scope: List[AgentProgressEvent] = []
            reservation = None

            if not capped:
                admitted_scope = events[:allowance]
                if len(admitted_scope) < len(events):
                    self._dropped += len(events) - len(admitted_scope)
                    self._truncated = True
                if self._budget is not None:
                    reservation = self._budget.reserve(
                        events=len(admitted_scope),
                        serialized_bytes=sum(_size_of(e) for e in admitted_scope),
                    )
                    if reservation is None:
                        capped = True
                        admitted_scope = []

            if capped:
                self._retire(buffer, events, marked=True)
                if buffer.truncation_marked:
                    return True
                buffer.truncation_marked = True
                payload = [_truncation_marker(session_id, events)]
            else:
                payload = list(admitted_scope)
                if self._truncated and not buffer.truncation_marked:
                    buffer.truncation_marked = True
                    payload = payload + [_truncation_marker(session_id, events)]
            self._last_send = self._clock()

        try:
            admitted = self._append_batch(session_id=session_id, events=payload)
        except Exception:  # noqa: BLE001 - a product's storage is not our problem
            with self._lock:
                if reservation is not None and self._budget is not None:
                    # Released, not committed: nothing was written, and a
                    # budget that only ever charges drains through failures.
                    self._budget.release(reservation)
                # The marker was not delivered either, so it is still owed.
                buffer.truncation_marked = False
                self._failures["delivery"] = self._failures.get("delivery", 0) + 1
            return False

        with self._lock:
            result = _as_batch_result(admitted, payload)
            if reservation is not None and self._budget is not None:
                self._budget.commit(reservation, result)
            buffer.delivered += result.admitted_events
            self._delivered += result.admitted_events
            if result.truncated:
                self._truncated = True
            self._retire(buffer, admitted_scope, marked=False)
        return True

    def _retire(self, buffer: _SessionBuffer, events: List, *, marked: bool) -> None:
        """Drop delivered (or undeliverable) events from the buffers."""
        sent = {id(event) for event in events}
        remaining = deque(e for e in buffer.ordinary if id(e) not in sent)
        if marked:
            self._dropped += len(buffer.ordinary) - len(remaining)
            self._truncated = True
        buffer.ordinary = remaining
        for kind, event in list(buffer.critical.items()):
            if id(event) in sent:
                del buffer.critical[kind]


def _coalesces(previous: AgentProgressEvent, event: AgentProgressEvent) -> bool:
    """Same activity, said again. Only adjacent updates collapse."""
    return (
        previous.kind == event.kind
        and previous.tool == event.tool
        and previous.status == event.status
        and previous.detail == event.detail
        and previous.summary == event.summary
    )


def _size_of(event: AgentProgressEvent) -> int:
    return len(event.summary or "") + len(event.detail or "") + len(event.phase or "") + 32


def _truncation_marker(session_id: str, events: List) -> AgentProgressEvent:
    """One marker, so a reader knows the timeline is incomplete.

    Without it a capped session is indistinguishable from a task that went
    quiet, which is the more alarming reading of the same silence.
    """
    return AgentProgressEvent(
        sequence=events[-1].sequence if events else 0,
        session_id=session_id,
        phase=events[-1].phase if events else "",
        kind=TRUNCATION_KIND,
        summary="Some progress updates were omitted",
    )


def _as_batch_result(admitted: Any, payload: List) -> AppendBatchResult:
    """Accept a product's report, or assume it took everything.

    A port that returns nothing is the common case for a simple product; it
    would be worse to treat that as "admitted zero" and retry forever.
    """
    if isinstance(admitted, AppendBatchResult):
        return admitted
    return AppendBatchResult(len(payload), sum(_size_of(e) for e in payload), False)


class SinkProgressPort:
    """A progress sink, as the turn executor's narrow progress port.

    The projection — sanitising a harness update into a publishable event,
    numbering it, applying the node's detail policy — is agent-core's, not a
    product's. Without this adapter every consumer would rewrite it, and the
    one that got the sanitisation subtly wrong would publish a prompt.

    Phase and detail policy come off the request rather than the constructor:
    one port serves a whole run, and those two differ per node.
    """

    def __init__(self, sink: Optional[AgentProgressSink]):
        self._sink = sink

    def callback(self, *, request: Any, session_id: Optional[str]):
        if self._sink is None:
            return None
        phase = str(getattr(request, "name", "") or "agent_turn")
        policy = str(getattr(request, "progress_detail", "") or "summary")
        counter = {"sequence": 0}
        sink = self._sink

        def publish(progress: Any) -> None:
            try:
                counter["sequence"] += 1
                event = project_turn_progress(
                    progress,
                    phase=phase,
                    session_id=session_id,
                    sequence=counter["sequence"],
                    detail_policy=policy,
                )
                if event is not None:
                    sink.publish(event)
            except Exception:  # noqa: BLE001 - progress must never fail a turn
                logger.debug("progress projection failed for %s", phase, exc_info=True)
                try:
                    sink.record_failure(stage="projection")
                except Exception:  # noqa: BLE001 - even a broken latch is swallowed
                    pass

        return publish

    def flush(self) -> ProgressFlushResult:
        """Bounded, and never raising: delivery cannot change operation truth."""
        if self._sink is None:
            return ProgressFlushResult.not_configured()
        try:
            return self._sink.flush()
        except Exception:  # noqa: BLE001
            logger.warning("progress flush failed", exc_info=True)
            return ProgressFlushResult(failures={"delivery": 1})
