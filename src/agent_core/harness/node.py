"""A harness node: everything one model-driven step of a workflow does.

A reviewer, a judge, a feedback re-review, a repair attempt — they are the
same process with different content:

    build the prompt  ->  run the turn  ->  record the attempt
          ^                                        |
          +--------- rejected, with the reason ----+
                                                   |
                                     accepted ->  outcome

Extracting only the retry loop left each consumer still writing the prompt
handling, the attempt bookkeeping, the status derivation and the parsing
around it — which is where they had actually drifted apart. This owns the
whole shape, and takes what differs as parameters:

``prompt``    how to build the prompt for an attempt. Given the reason the
              previous attempt was rejected, so a repair step can send
              something new rather than asking for the same mistake again.
``parse``     what the answer has to be. Raising rejects the attempt.
``accept``    anything else that must hold — a compile, a coverage gate.
``recorder``  where the attempt is written down. The product keeps its own
              table; this only says when to write and what the row contains.
``retryable`` whether an exception is worth another attempt.

Nothing here knows what a reviewer is, what a finding is, or what table any
of it goes in.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

from agent_core.harness.cost import (
    AttemptNotAdmitted,
    PaidAttempts,
    accounts_for_paid_attempts,
)
from agent_core.harness.records import TurnRecord, describe_turn
from agent_core.harness.recovery import is_stall, resume_timeout
from agent_core.harness.turns import ACCEPTED, REJECTED, UNREACHABLE, run_until_accepted

#: The step failed and that is tolerable: it contributes nothing and the
#: workflow carries on. An optional reviewer whose answer will not parse is
#: not a broken review, it is a review with one fewer opinion in it.
SKIPPED = "skipped"

logger = logging.getLogger(__name__)


@runtime_checkable
class TurnRecorder(Protocol):
    """Where a product writes attempts down.

    Every method is optional in practice -- `NullRecorder` implements them as
    no-ops -- so a caller with nothing to persist supplies nothing.
    """

    def start(self, *, node: str, attempt: int, max_attempts: int, model_id: Optional[str]) -> Any:
        """Called before the turn. Whatever it returns identifies the attempt."""

    def finish(self, handle: Any, record: TurnRecord) -> None:
        """Called after the turn, with the row's contents already derived."""

    def retry_scheduled(self, handle: Any, *, attempt: int, max_attempts: int, error: str) -> None:
        """Called before another attempt, never after the last one."""

    def gave_up(self, handle: Any, *, attempt: int, error: str) -> None:
        """Called once when no attempt produced an acceptable answer."""

    def skipped(self, handle: Any, *, attempt: int, error: str) -> None:
        """Called instead of `gave_up` when the step's failure is tolerable.

        Separate because a product usually records the two differently: a
        skipped step is not an incident, and a run that logs it as one trains
        its operators to ignore the log.
        """


class NullRecorder:
    """Records nothing. The default, so a caller opts in to bookkeeping."""

    def start(self, **kwargs: Any) -> Any:
        return None

    def finish(self, handle: Any, record: TurnRecord) -> None:
        return None

    def retry_scheduled(self, handle: Any, **kwargs: Any) -> None:
        return None

    def gave_up(self, handle: Any, **kwargs: Any) -> None:
        return None

    def skipped(self, handle: Any, **kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class NodeOutcome:
    """What the step produced, and enough to decide what happens next."""

    status: str
    payload: Dict[str, Any] = field(default_factory=dict)
    result: Any = None
    record: Optional[TurnRecord] = None
    attempts: int = 0
    error: Optional[str] = None
    #: Projected from `TurnLoop.recovered`. Additive and defaulted.
    recovered: bool = False

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED

    @property
    def unreachable(self) -> bool:
        """No answer ever arrived, as opposed to one that was not good enough."""
        return self.status == UNREACHABLE

    @property
    def skipped(self) -> bool:
        """Failed, tolerably. The workflow continues without this step."""
        return self.status == SKIPPED


def run_harness_node(
    runner: Any,
    *,
    name: str,
    repo_path: Path,
    prompt: Callable[[int, Optional[str]], Path | str],
    attempts: int = 1,
    parse: Optional[Callable[[str], Dict[str, Any]]] = None,
    accept: Optional[Callable[[Any, Dict[str, Any]], Optional[str]]] = None,
    recorder: Optional[TurnRecorder] = None,
    on_failure: str = "fail",
    retryable: Optional[Callable[[BaseException], bool]] = None,
    recover: Optional[Callable[..., Any]] = None,
    recoverable: Optional[Callable[[Any], bool]] = None,
    recovery_prompt: Optional[Callable[[Any, Optional[str]], Path]] = None,
    default_model: Optional[str] = None,
    missing_session_message: str = "missing session id",
    paid_attempts: Optional[PaidAttempts] = None,
    before_attempt: Optional[Callable[[int], None]] = None,
    **turn_kwargs: Any,
) -> NodeOutcome:
    """Run one model-driven step to an acceptable answer, or give up.

    ``prompt(attempt, feedback)`` returns the file to send. A step whose
    prompt never changes ignores both arguments; a repair step uses
    ``feedback``, which is why the prompt is a callable rather than a path.

    An attempt is acceptable when the turn completed with a session id, the
    answer parsed, and ``accept`` -- if given -- returned None. Anything else
    is a rejection carrying the reason, which is recorded, reported to the
    recorder, and handed to the next prompt.

    ``on_failure`` says what a step that never succeeds means. The default,
    ``"fail"``, reports it and lets the caller decide to stop. ``"skip"`` is
    for a step the workflow can do without -- an optional reviewer, a
    best-effort summary -- and reports SKIPPED, so a caller does not have to
    tell "this failed and matters" from "this failed and does not" by
    inspecting the error text.

    ``paid_attempts`` is the operation's cost ledger. Every submission this
    node causes -- each attempt and the in-place recovery turn -- is admitted
    and reported through it, in one monotonic sequence, so a cap sees the
    third submission as the third and not as another "attempt 1". A harness
    that walks a provider chain inside one `run_turn` is handed the ledger
    instead and reports each candidate itself; wrapping such a harness here as
    well would count one chain twice.

    ``before_attempt(attempt)`` runs immediately before each *fresh* attempt is
    admitted -- the product's own check that this operation should still be
    spending. Raising `AttemptNotAdmitted` stops without another submission.
    An in-place recovery does not call it: it consumes no attempt, and the
    check has already passed for the attempt it is salvaging.

    ``recovery_prompt(result, reason)`` enables the standard reusable-session
    recovery: a stalled turn receives one bounded continue turn through the
    same neutral runner. The product supplies the prompt because only it knows
    what work should continue; agent-core owns the recovery mechanics. A
    custom ``recover`` hook remains available for non-session recovery, but
    the two forms cannot be combined.
    """
    if on_failure not in ("fail", "skip"):
        raise ValueError(f"on_failure must be 'fail' or 'skip', got {on_failure!r}")
    if recovery_prompt is not None and recover is not None:
        raise ValueError("pass recovery_prompt or recover, not both")
    recorder = recorder or NullRecorder()
    ledger = paid_attempts or PaidAttempts()
    harness_counts_itself = accounts_for_paid_attempts(runner)
    if harness_counts_itself:
        turn_kwargs = {**turn_kwargs, "paid_attempts": ledger}

    @contextmanager
    def paid() -> Any:
        """One submission, unless the harness is counting them itself."""
        if harness_counts_itself:
            yield None
            return
        with ledger.submission() as attempt:
            yield attempt

    payload: Dict[str, Any] = {}
    record: Optional[TurnRecord] = None
    handle: Any = None

    def attempt_once(attempt: int, feedback: Optional[str]) -> Any:
        nonlocal handle
        handle = recorder.start(
            node=name, attempt=attempt, max_attempts=attempts, model_id=default_model
        )
        if before_attempt is not None:
            before_attempt(attempt)
        rendered = prompt(attempt, feedback)
        turn = dict(turn_kwargs)
        if isinstance(rendered, Path):
            turn["prompt_file"] = rendered
        else:
            turn["message"] = str(rendered)
        with paid() as submission:
            result = runner.run_turn(repo_path=repo_path, **turn)
            if submission is not None:
                submission.record_provider(getattr(result, "cost_usd", None))
            return result

    def acceptable(result: Any) -> Optional[str]:
        nonlocal payload, record
        payload = {}
        record = describe_turn(
            result, default_model=default_model, missing_session_message=missing_session_message
        )
        if not record.succeeded:
            recorder.finish(handle, record)
            return record.error

        if parse is not None:
            try:
                payload = parse(getattr(result, "result", "") or "")
            except Exception as exc:  # noqa: BLE001 - the product's parser defines valid
                rejected = TurnRecord(
                    status="failed",
                    session_id=record.session_id,
                    model_id=record.model_id,
                    usage=record.usage,
                    error=f"unusable answer: {exc}",
                    raw_log_path=record.raw_log_path,
                )
                record = rejected
                recorder.finish(handle, rejected)
                return rejected.error

        if accept is not None:
            rejection = accept(result, payload)
            if rejection is not None:
                rejected = TurnRecord(
                    status="failed",
                    session_id=record.session_id,
                    model_id=record.model_id,
                    usage=record.usage,
                    error=rejection,
                    raw_log_path=record.raw_log_path,
                )
                record = rejected
                recorder.finish(handle, rejected)
                return rejection

        recorder.finish(handle, record)
        return None

    if recovery_prompt is not None:
        original_timeout = int(turn_kwargs.get("timeout_seconds") or 0)

        def recover_in_place(*, result: Any, reason: Optional[str]) -> Any:
            rendered = recovery_prompt(result, reason)
            recovery_kwargs = dict(turn_kwargs)
            recovery_kwargs["timeout_seconds"] = resume_timeout(original_timeout)
            if isinstance(rendered, Path):
                recovery_kwargs["prompt_file"] = rendered
            else:
                recovery_kwargs["message"] = str(rendered)
            # A nudge is cheaper than a fresh attempt but it is not free, and
            # it draws the next ordinal from the same sequence: a stall-prone
            # model must not be able to spend past a cap through a mechanism
            # that numbers its submissions separately.
            with paid() as submission:
                result = runner.run_turn(
                    repo_path=repo_path,
                    **recovery_kwargs,
                )
                if submission is not None:
                    submission.record_provider(getattr(result, "cost_usd", None))
                return result

        recover = recover_in_place
        recoverable = recoverable or is_stall

    def worth_another_attempt(exc: BaseException) -> bool:
        # A refused admission is never retried: retrying is exactly how a cap's
        # "no" turns into the spend it was there to prevent.
        if isinstance(exc, AttemptNotAdmitted):
            return False
        return True if retryable is None else retryable(exc)

    loop = run_until_accepted(
        attempt_once,
        accept=acceptable,
        attempts=attempts,
        retryable=worth_another_attempt,
        on_retry=lambda attempt, max_attempts, error: recorder.retry_scheduled(
            handle, attempt=attempt, max_attempts=max_attempts, error=error
        ),
        # A stalled turn is nudged in place before a fresh one is paid for.
        # Passed straight through: what counts as recoverable, and how to
        # recover it, depends on the harness and the step, not on this loop.
        recover=recover,
        recoverable=recoverable,
        on_recover=lambda attempt, reason: recorder.retry_scheduled(
            handle, attempt=attempt, max_attempts=attempts, error=f"recovering in place: {reason}"
        ),
    )

    if loop.status == UNREACHABLE and loop.exception is not None:
        # The turn never returned, so there is no result to describe -- but the
        # attempt row is already open and has to be closed.
        record = TurnRecord(status="failed", model_id=default_model, error=loop.reason)
        recorder.finish(handle, record)

    reason = loop.reason or "no acceptable answer"
    if loop.status != ACCEPTED:
        if on_failure == "skip":
            recorder.skipped(handle, attempt=loop.attempts, error=reason)
        else:
            recorder.gave_up(handle, attempt=loop.attempts, error=reason)

    status = loop.status
    if status != ACCEPTED and on_failure == "skip":
        status = SKIPPED

    return NodeOutcome(
        status=status,
        payload=payload if loop.status == ACCEPTED else {},
        result=loop.result,
        record=record,
        attempts=loop.attempts,
        error=loop.reason,
        recovered=loop.recovered,
    )
