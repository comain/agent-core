"""Asking a model again when the first answer will not do.

Four places across three products wrote this loop, and each got a different
part of it wrong. What they disagree about is not the mechanics but the
policy, so that is what is parameterised here:

**Why retry.** One caller retries an answer it could not use. Another retries
only a *transient* failure and re-raises anything else, because retrying a
request the provider has already refused just spends money twice. A third
retries because an external check failed -- generated code that did not
compile.

**What to send next.** The first two resend the same prompt. A repair loop
sends a new one built from the failure, which is the whole point of it; a
fixed prompt would ask the model to make the same mistake again.

**What "good" means.** Parsing as JSON with the required keys, or compiling,
or passing a coverage gate. Only the caller knows.

`run_until_accepted` is the general loop. `run_structured_turn` is the common
case sitting on top of it: one prompt, retried while the answer is unusable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: The attempt produced something the caller accepted.
ACCEPTED = "accepted"
#: Attempts ran out while the answer was still unacceptable.
REJECTED = "rejected"
#: Every attempt failed before an answer existed at all.
UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class TurnLoop:
    """How a bounded series of attempts ended."""

    status: str
    result: Any = None
    attempts: int = 0
    reason: Optional[str] = None
    exception: Optional[BaseException] = None
    #: Whether the accepted answer came from the one in-session nudge rather
    #: than from a turn that answered on its own. A recovered run cost two
    #: prompts and succeeded for a different reason; without this the two are
    #: indistinguishable in a report, so a recovery mechanism that has quietly
    #: stopped working looks exactly like normal operation.
    #:
    #: Defaulted, because every existing construction site predates it.
    recovered: bool = False

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED

    def raise_if_failed(self) -> Any:
        """For callers whose contract is to raise rather than return a status.

        Re-raises the original exception where there was one, so the traceback
        still points at the failure rather than at this loop.
        """
        if self.accepted:
            return self.result
        if self.exception is not None:
            raise self.exception
        raise RuntimeError(self.reason or "no acceptable answer after retrying")


def _notify_recover(hook: Optional[Callable[..., None]], attempt: int, reason: Optional[str]) -> None:
    if hook is None:
        return
    try:
        hook(attempt=attempt, reason=reason)
    except Exception:  # noqa: BLE001 - a reporting hook must not fail the turn
        logger.exception("on_recover hook raised")


def run_until_accepted(
    run: Callable[..., Any],
    *,
    accept: Callable[[Any], Optional[str]],
    attempts: int = 1,
    retryable: Optional[Callable[[BaseException], bool]] = None,
    on_retry: Optional[Callable[..., None]] = None,
    recover: Optional[Callable[..., Any]] = None,
    recoverable: Optional[Callable[[Any], bool]] = None,
    on_recover: Optional[Callable[..., None]] = None,
) -> TurnLoop:
    """Run, check, and run again while attempts remain.

    ``run(attempt=…, feedback=…)`` performs one attempt. ``feedback`` is the
    reason the previous attempt was rejected, or None on the first -- which is
    what lets a repair loop send a prompt built from the last failure.

    ``accept(result)`` returns None to accept, or a short reason to reject.

    ``retryable(exc)`` decides whether an exception is worth another attempt.
    The default retries everything. Returning False stops immediately and puts
    the exception on the result, for callers that must not retry a request the
    model has already refused.

    ``on_retry(attempt=…, max_attempts=…, reason=…)`` is called before each
    further attempt, and not after the last one, because nothing is being
    retried then.

    ## Recovering an attempt instead of replacing it

    Retrying runs ``run`` again from nothing. For some rejections that is the
    wrong move by a wide margin: a turn that has already spent several hundred
    thousand tokens exploring a repository and then stalled does not need to
    start over, it needs one nudge to carry on. Starting over pays for the
    exploration a second time and usually reaches the same place.

    ``recoverable(result)`` says whether a rejected result is that kind --
    a stall rather than a wrong answer. ``recover(result=…, reason=…)`` then
    gets one chance to salvage it in place and return a new result, or None to
    give up and let the ordinary retry happen.

    Recovery is attempted **at most once per attempt**, and does not consume
    one: a model that stalls every time still gets its full complement of
    fresh attempts, and cannot loop forever between stalling and being nudged.
    A recovered result goes back through ``accept`` like any other -- being
    salvaged is not the same as being right.

    ``on_recover(attempt=…, reason=…)`` reports that it is being tried.
    """
    attempts = max(1, int(attempts))
    feedback: Optional[str] = None
    result: Any = None
    reason: Optional[str] = None

    for attempt in range(1, attempts + 1):
        try:
            result = run(attempt=attempt, feedback=feedback)
        except BaseException as exc:  # noqa: BLE001 - handed back, never swallowed
            if retryable is not None and not retryable(exc):
                logger.info("attempt %s failed and is not retryable: %s", attempt, exc)
                return TurnLoop(
                    status=UNREACHABLE, attempts=attempt, reason=str(exc), exception=exc
                )
            reason = str(exc)
            logger.warning("attempt %s/%s failed: %s", attempt, attempts, reason)
            if attempt < attempts:
                _notify(on_retry, attempt, attempts, reason)
                feedback = reason
                continue
            return TurnLoop(
                status=UNREACHABLE, attempts=attempt, reason=reason, exception=exc
            )

        rejection = accept(result)
        if rejection is None:
            return TurnLoop(status=ACCEPTED, result=result, attempts=attempt)

        # Try to salvage this attempt before spending another one. Once only,
        # and the salvaged result has to pass `accept` on its own merits.
        if recover is not None and (recoverable is None or recoverable(result)):
            logger.info("attempt %s stalled (%s); recovering in place", attempt, rejection)
            _notify_recover(on_recover, attempt, rejection)
            try:
                recovered = recover(result=result, reason=rejection)
            except BaseException as exc:  # noqa: BLE001 - recovery is best-effort
                # A failed rescue must not lose the attempt that produced the
                # result: fall through and retry as though it never happened.
                logger.warning("recovery of attempt %s failed: %s", attempt, exc)
                recovered = None
            if recovered is not None:
                result = recovered
                rejection = accept(result)
                if rejection is None:
                    # Only here: the nudge produced the answer that was
                    # accepted. A recovery that returned nothing, or one whose
                    # result `accept` rejected, did not save this turn and must
                    # not be credited with it.
                    return TurnLoop(
                        status=ACCEPTED, result=result, attempts=attempt, recovered=True
                    )

        reason = rejection
        if attempt < attempts:
            _notify(on_retry, attempt, attempts, reason)
            feedback = reason
            continue

    return TurnLoop(status=REJECTED, result=result, attempts=attempts, reason=reason)


@dataclass(frozen=True)
class StructuredTurn:
    """The common case: one prompt, an answer that has to parse."""

    status: str
    result: Any = None
    payload: Dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    error: Optional[str] = None
    parse_error: Optional[str] = None

    @property
    def answered(self) -> bool:
        return self.status == ACCEPTED


def run_structured_turn(
    runner: Any,
    *,
    prompt_file: Path,
    repo_path: Path,
    attempts: int = 1,
    parse: Optional[Callable[[str], Dict[str, Any]]] = None,
    retryable: Optional[Callable[[BaseException], bool]] = None,
    on_retry: Optional[Callable[..., None]] = None,
    **turn_kwargs: Any,
) -> StructuredTurn:
    """Run one turn, retrying while the answer cannot be used.

    An answer is usable when the turn completed and, if ``parse`` is given,
    the text parsed. A turn that never completed and one whose answer would
    not parse are both retried, and are distinguished on the way out:
    ``parse_error`` is set only for the second.
    """
    payload: Dict[str, Any] = {}
    parse_error: Optional[str] = None

    def once(attempt: int, feedback: Optional[str]) -> Any:
        return runner.run_turn(prompt_file=prompt_file, repo_path=repo_path, **turn_kwargs)

    def acceptable(result: Any) -> Optional[str]:
        nonlocal payload, parse_error
        payload, parse_error = {}, None
        text = getattr(result, "result", "") or ""
        if text and parse is not None:
            try:
                payload = parse(text)
            except Exception as exc:  # noqa: BLE001 - the caller's parser defines valid
                parse_error = str(exc)
                return f"the answer could not be parsed: {exc}"
        if getattr(result, "type", None) != "completed":
            return "the turn did not complete"
        if parse is not None and not payload:
            return "the answer was empty"
        return None

    loop = run_until_accepted(
        once, accept=acceptable, attempts=attempts, retryable=retryable, on_retry=on_retry
    )
    return StructuredTurn(
        status=loop.status,
        result=loop.result,
        payload=payload,
        attempts=loop.attempts,
        error=loop.reason,
        parse_error=parse_error,
    )


def _notify(
    on_retry: Optional[Callable[..., None]], attempt: int, attempts: int, reason: str
) -> None:
    if on_retry is None:
        return
    on_retry(attempt=attempt, max_attempts=attempts, error=reason)
