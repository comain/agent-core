"""One ordering for an agent turn, whoever is asking for it.

A turn is not just "call the harness". Around the call sit a cancellation
check, a session that may have to outlive several attempts, a workspace guard
that decides whether the diff is allowed, a cost ledger, a progress sink, a
recorder, and a result port that makes the answer durable. The *order* of those
is the whole contract, and it had been written twice: once in the LangGraph
node and once in each product's direct call path, which is how the two drifted.

`execute_agent_turn` owns the order. The graph node projects state into a
request and projects the normalized result back; a product calling directly
passes the same request with its own parse and accept callbacks. Both get the
same twelve steps.

## Why the order is what it is

**A result is durable only after the guard has accepted and the session has
closed.** Persisting earlier lets a product commit an answer built from a
workspace that was about to be rejected, and there is no undo for that: the row
is written, the report is sent, and the diff it describes was never allowed.

**The guard's rejection is captured, not swallowed, and not raised before
cleanup.** Flushing progress and closing the session still have to happen —
a leaked session is a paid conversation nobody will ever close — so the
rejection travels as a value until cleanup is done, then goes to the result
port as a `reject` and is re-raised.

**A session that will not close prevents the commit.** Preserved from the
behaviour this replaced, and worth keeping: the close is the last thing that
can tell you the conversation was in a state anyone understood. A guard
rejection still wins over it, because that is a verdict on the work rather
than a fault in the cleanup, and the product needs to hear the verdict.

**Cancellation still produces a result.** The operation row was already opened.
Returning nothing leaves it open, which makes a deliberate stop indistinguishable
from a crash.

**The ports are narrow on purpose.** One "service" object with a dozen methods
would make every consumer implement — or stub — capabilities it does not have.
Eight small protocols let a product bring the three it needs, and `validate`
says so before any money is spent rather than at the first missing attribute.

## What it does not do

It does not decide what a turn *means*: parsing, acceptance, retry policy and
what to persist stay with the product. And it does not touch graph state; the
context holds live objects and never crosses a checkpoint.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from agent_core.harness.cost import (
    AttemptNotAdmitted,
    PaidAttempts,
    TurnCost,
    TurnCostPort,
)
from agent_core.harness.node import NodeOutcome, run_harness_node
from agent_core.harness.sessions import AgentSessionRef
from agent_core.harness.turn_result import AgentTurnResult, normalize_turn_outcome

logger = logging.getLogger(__name__)

#: The turn never started, because the task was already stopped.
CANCELLED = "cancelled"


class AgentTurnExecutionError(RuntimeError):
    """The turn cannot be trusted, so nothing is committed."""


class ObserverRefusedAttempt(AttemptNotAdmitted):
    """A product observer stopped the operation before another paid attempt."""


class ResultCommitError(RuntimeError):
    """The product could not make the result durable.

    Raised rather than logged: a turn whose answer was not persisted has not
    finished, and letting the graph checkpoint past it is how a workflow
    resumes believing work is done that no product row records.
    """


# -- ports -----------------------------------------------------------------

@runtime_checkable
class CancellationSource(Protocol):
    def is_cancelled(self) -> bool: ...


@runtime_checkable
class SessionFactory(Protocol):
    def open_session(self, *, harness: Any, repo_path: Path, model_id: Optional[str], effort_strategy: str = "default") -> Any: ...


@runtime_checkable
class TurnGuard(Protocol):
    """Whatever must hold before and after the turn — a workspace diff policy,
    a lease, a lock. `after` is authoritative: raising prevents durability."""

    def before(self, request: "AgentTurnRequest") -> Any: ...
    def after(self, request: "AgentTurnRequest", token: Any) -> None: ...


@runtime_checkable
class ProgressFlush(Protocol):
    """What a flush reports. Diagnostics only: delivery never changes truth."""

    @property
    def configured(self) -> bool: ...
    @property
    def failures(self) -> Mapping[str, int]: ...
    @property
    def dropped_count(self) -> int: ...


@runtime_checkable
class TurnProgressPort(Protocol):
    def callback(
        self, *, request: "AgentTurnRequest", session_id: Optional[str]
    ) -> Optional[Callable[[Any], None]]: ...
    def flush(self) -> ProgressFlush: ...


@runtime_checkable
class TurnResultPort(Protocol):
    def commit(
        self, request: "AgentTurnRequest", execution: "AgentTurnExecutionResult"
    ) -> None: ...
    def reject(
        self,
        request: "AgentTurnRequest",
        execution: "AgentTurnExecutionResult",
        *,
        reason: str,
    ) -> None: ...


@runtime_checkable
class ExecutionObserver(Protocol):
    """The product's own check that this operation should still be running."""

    def check_before_attempt(self, *, operation_id: str, attempt: int) -> None: ...


# -- what a turn is --------------------------------------------------------

@dataclass(frozen=True)
class HarnessBinding:
    """A harness and the neutral name it was configured under.

    The name is carried rather than derived from the class: attribution that
    reads `OpenCodeHarness` breaks the day a class is renamed or wrapped, and
    the rows already written then disagree with the rows written after.
    """

    name: str
    harness: Any


@dataclass(frozen=True)
class AgentTurnRequest:
    """What to run. Serializable in spirit: no live ports, no clients."""

    name: str
    repo_path: Path
    prompt: Callable[[int, Optional[str]], Path | str]
    operation_id: str = ""
    model_id: Optional[str] = None
    effort_strategy: str = "default"
    attempts: int = 1
    timeout_seconds: Optional[int] = None
    on_failure: str = "fail"
    session_scope: str = "none"
    progress_detail: str = "summary"
    parse: Optional[Callable[[str], Dict[str, Any]]] = None
    accept: Optional[Callable[[Any, Dict[str, Any]], Optional[str]]] = None
    retryable: Optional[Callable[[BaseException], bool]] = None
    recovery_prompt: Optional[Callable[[Any, Optional[str]], Path | str]] = None
    recoverable: Optional[Callable[[Any], bool]] = None
    turn_kwargs: Mapping[str, Any] = field(default_factory=dict)
    delivery: Optional[str] = None
    title: Optional[str] = None
    pure: Optional[bool] = None

    @property
    def wants_session(self) -> bool:
        return self.session_scope == "phase"


@dataclass(frozen=True)
class AgentTurnContext:
    """The live objects a turn needs. Application-owned, never checkpointed."""

    binding: HarnessBinding
    cost: TurnCostPort
    cancellation: Optional[CancellationSource] = None
    sessions: Optional[SessionFactory] = None
    guard: Optional[TurnGuard] = None
    progress: Optional[TurnProgressPort] = None
    results: Optional[TurnResultPort] = None
    recorder: Optional[Any] = None
    observer: Optional[ExecutionObserver] = None

    def validate(self, request: AgentTurnRequest) -> None:
        """Refuse an impossible turn before it spends anything.

        Every check here would otherwise surface as an `AttributeError` or a
        `None` deep inside a turn that has already been paid for.
        """
        if not self.binding.name:
            raise AgentTurnExecutionError("the harness binding has no neutral name")
        if self.binding.harness is None:
            raise AgentTurnExecutionError(f"binding {self.binding.name!r} has no harness")
        if self.cost is None:
            raise AgentTurnExecutionError(
                "a cost port is required; a product with no cap supplies a "
                "no-op admission that still records"
            )
        if request.attempts < 1:
            raise AgentTurnExecutionError(f"attempts must be at least 1, got {request.attempts}")
        if request.on_failure not in ("fail", "skip"):
            raise AgentTurnExecutionError(
                f"on_failure must be 'fail' or 'skip', got {request.on_failure!r}"
            )
        if request.session_scope not in ("none", "phase"):
            raise AgentTurnExecutionError(
                f"session_scope must be 'none' or 'phase', got {request.session_scope!r}"
            )
        if request.wants_session and self.sessions is None:
            raise AgentTurnExecutionError(
                f"{request.name!r} asks for a phase session but no session factory was given"
            )
        if self.observer is not None and not request.operation_id:
            raise AgentTurnExecutionError(
                f"{request.name!r} has an observer but no operation_id to check against"
            )


@dataclass(frozen=True)
class AgentTurnExecutionResult:
    """Both answers: the exact one and the neutral one.

    A direct caller needs `outcome` — the payload its parser produced, the
    attempt record, the precise status. The graph needs `result`, which is
    JSON-safe and can be checkpointed. Returning one and making callers derive
    the other is what produced two different normalizations in the first place.
    """

    outcome: NodeOutcome
    result: AgentTurnResult
    cost: TurnCost = field(default_factory=TurnCost.unavailable)
    paid_attempts: int = 0

    @property
    def accepted(self) -> bool:
        return self.outcome.accepted


# -- the order -------------------------------------------------------------

def execute_agent_turn(
    request: AgentTurnRequest, context: AgentTurnContext
) -> AgentTurnExecutionResult:
    """Run one agent turn through the canonical ordering."""
    context.validate(request)

    if context.cancellation is not None and context.cancellation.is_cancelled():
        return _cancelled_result(request, context)

    ledger = PaidAttempts(context.cost, operation_id=request.operation_id)
    session = None
    if request.wants_session:
        session = context.sessions.open_session(
            harness=context.binding.harness,
            repo_path=request.repo_path,
            model_id=request.model_id,
            **({"effort_strategy": request.effort_strategy} if request.effort_strategy != "default" else {}),
        )

    runner = session if session is not None else context.binding.harness
    guard_token = None
    guard_taken = False
    started = time.monotonic()
    rejection: Optional[BaseException] = None
    cleanup_fault: Optional[BaseException] = None
    execution: Optional[AgentTurnExecutionResult] = None

    try:
        if context.guard is not None:
            guard_token = context.guard.before(request)
            guard_taken = True

        turn_kwargs = dict(request.turn_kwargs)
        if request.delivery is not None:
            turn_kwargs.setdefault("delivery", request.delivery)
        if request.title is not None:
            turn_kwargs.setdefault("title", request.title)
        if request.pure is not None:
            turn_kwargs.setdefault("pure", request.pure)
        if context.cancellation is not None:
            turn_kwargs.setdefault("is_cancelled", context.cancellation.is_cancelled)
        outcome = run_harness_node(
            runner,
            name=request.name,
            repo_path=request.repo_path,
            prompt=request.prompt,
            attempts=request.attempts,
            parse=request.parse,
            accept=request.accept,
            recorder=context.recorder,
            on_failure=request.on_failure,
            retryable=request.retryable,
            recovery_prompt=request.recovery_prompt,
            recoverable=request.recoverable,
            default_model=request.model_id,
            model_id=request.model_id,
            timeout_seconds=request.timeout_seconds,
            paid_attempts=ledger,
            before_attempt=_observer_check(request, context),
            **_progress_kwargs(request, context, session),
            **turn_kwargs,
        )
        # Before close, deliberately: usage, retrospective and patch count
        # exist only on a live session.
        snapshot = session.snapshot() if session is not None else None
        result = normalize_turn_outcome(
            outcome, snapshot=snapshot, elapsed_seconds=time.monotonic() - started
        )
        _assert_sessions_agree(outcome, result)
        execution = AgentTurnExecutionResult(
            outcome=outcome,
            result=result,
            cost=ledger.aggregate(),
            paid_attempts=ledger.count,
        )
    finally:
        # Cleanup runs whatever happened, and a guard rejection is carried out
        # as a value: raising it here would skip the flush and leak the session.
        if guard_taken and context.guard is not None:
            try:
                context.guard.after(request, guard_token)
            except BaseException as exc:  # noqa: BLE001 - captured, then re-raised
                rejection = exc
        flush = _flush(context)
        if session is not None:
            try:
                session.close()
            except BaseException as exc:  # noqa: BLE001 - captured, then raised
                cleanup_fault = exc

    if execution is not None:
        execution = _with_flush_diagnostic(execution, flush)

    if rejection is None and cleanup_fault is not None:
        # Nothing is committed: the close is the last thing that could report
        # the conversation ended in a state anyone understood.
        raise cleanup_fault

    if rejection is not None:
        if cleanup_fault is not None:
            logger.warning(
                "the session for %s also failed to close: %s", request.name, cleanup_fault
            )
        if execution is not None and context.results is not None:
            # The product may want the attempt and session audit even though
            # nothing reusable was produced. `reject`, never `commit`: a
            # rejected guard means the answer must not be read back as done.
            context.results.reject(request, execution, reason=str(rejection))
        raise rejection

    if execution is None:  # the turn itself raised; nothing to commit
        raise AgentTurnExecutionError(f"{request.name} produced no result")

    _commit(request, context, execution)
    return execution


def _cancelled_result(
    request: AgentTurnRequest, context: AgentTurnContext
) -> AgentTurnExecutionResult:
    """No guard taken, no session opened, nothing paid for — but the operation
    row is already open, and leaving it open makes a stop look like a crash."""
    execution = AgentTurnExecutionResult(
        outcome=NodeOutcome(status=CANCELLED),
        result=AgentTurnResult.cancelled(),
    )
    _commit(request, context, execution)
    return execution


def _commit(
    request: AgentTurnRequest,
    context: AgentTurnContext,
    execution: AgentTurnExecutionResult,
) -> None:
    if context.results is None:
        return
    try:
        context.results.commit(request, execution)
    except Exception as exc:  # noqa: BLE001
        raise ResultCommitError(
            f"the product could not persist the result of {execution.result.status} turn"
        ) from exc


def _observer_check(
    request: AgentTurnRequest, context: AgentTurnContext
) -> Optional[Callable[[int], None]]:
    if context.observer is None:
        return None

    def check(attempt: int) -> None:
        try:
            context.observer.check_before_attempt(
                operation_id=request.operation_id, attempt=attempt
            )
        except AttemptNotAdmitted:
            raise
        except Exception as exc:  # noqa: BLE001 - a refusal, however it is spelt
            raise ObserverRefusedAttempt(
                f"attempt {attempt} of {request.name} was stopped: {exc}"
            ) from exc

    return check


def _progress_kwargs(
    request: AgentTurnRequest, context: AgentTurnContext, session: Any
) -> Dict[str, Any]:
    """Omitted rather than passed as None: a harness that does not take the
    argument should not be handed one it must ignore.

    The port is built once for a run but the phase name and detail policy are
    per node, so it reads them off the request rather than being rebuilt for
    every turn.
    """
    if context.progress is None:
        return {}
    callback = context.progress.callback(
        request=request, session_id=getattr(session, "session_id", None)
    )
    return {"on_progress": callback} if callback else {}


def _flush(context: AgentTurnContext) -> Optional[ProgressFlush]:
    if context.progress is None:
        return None
    try:
        return context.progress.flush()
    except Exception:  # noqa: BLE001 - delivery cannot change operation truth
        logger.warning("progress flush failed", exc_info=True)
        return None


def _with_flush_diagnostic(
    execution: AgentTurnExecutionResult, flush: Optional[ProgressFlush]
) -> AgentTurnExecutionResult:
    """Record a progress problem without implying anything about the turn."""
    if flush is None or not flush.configured:
        return execution
    if not (flush.failures or flush.dropped_count):
        return execution
    diagnostics = dict(execution.result.diagnostics)
    if flush.failures:
        diagnostics["progress_flush_failed"] = True
    if flush.dropped_count:
        diagnostics["progress_dropped"] = flush.dropped_count
    return dataclasses.replace(
        execution, result=dataclasses.replace(execution.result, diagnostics=diagnostics)
    )


def _assert_sessions_agree(outcome: NodeOutcome, result: AgentTurnResult) -> None:
    """The attempt record and the normalized result must name the same
    conversations.

    They are built from different places — the record from the turn, the
    normalized result from the turn *and* the session snapshot — and a
    disagreement means one of them is describing a conversation the other never
    saw. That is not a reporting nuisance: the product is about to store a row
    attributing cost to a session that its own result does not mention.
    """
    record = getattr(outcome, "record", None)
    recorded: Tuple[AgentSessionRef, ...] = tuple(getattr(record, "session_refs", ()) or ())
    if not recorded:
        return
    missing = [ref for ref in recorded if ref not in result.session_refs]
    if missing:
        raise AgentTurnExecutionError(
            f"the attempt record names conversations the result does not: "
            f"{[ref.locator for ref in missing]}"
        )
