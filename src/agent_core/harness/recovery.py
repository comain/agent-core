"""Resuming a session that went quiet, rather than starting it again.

`run_until_accepted` takes a `recover` hook, which is the general shape: on a
rejection that is worth salvaging, get one chance to salvage it. What it does
not do is know how to salvage an *agent session*, so every consumer wrote that
itself -- poll, notice a stall, send one continue prompt into the live session,
poll again with a shorter budget.

That is the same procedure every time, and the parts that genuinely differ are
small: which stall types are worth resuming, what to say, and how long to allow
the second poll. So it lives here, and a consumer enables it and supplies a
prompt.

## Why one attempt, into the live session

The point is that the session already holds the work. A turn that has spent
several hundred thousand tokens reading a repository and then stopped emitting
does not need to read it again -- it needs to be told to carry on. Starting a
fresh turn pays for the exploration twice and usually arrives back where it
was.

One nudge, though, not a loop. A model that has genuinely stopped will stop
again, and a second continue prompt buys nothing but another timeout; the
attempt budget in `run_until_accepted` is the right place for further tries,
because those start clean.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

#: Event types that mean "went quiet", as opposed to "answered badly". Only
#: these are worth a continue prompt: an answer that failed to parse needs a
#: different prompt, not the same one repeated.
STALL_TYPES = frozenset({"stalled_after_recovery", "stalled_no_progress"})

#: Shortest and longest the resumed poll may wait. The floor stops a tight
#: original budget giving the continue prompt no room to produce anything; the
#: ceiling stops a generous one doubling the cost of a turn that has already
#: shown it is not producing.
MIN_RESUME_SECONDS = 120
MAX_RESUME_SECONDS = 600


def is_stall(event: Any, stall_types=STALL_TYPES) -> bool:
    """Whether a native event or neutral turn result is worth resuming."""
    if isinstance(event, Mapping):
        event_type = event.get("type")
    else:
        event_type = getattr(event, "type", None)
    return event_type in stall_types


def resume_timeout(original: int) -> int:
    """How long to allow the resumed poll, given the original budget."""
    return max(MIN_RESUME_SECONDS, min(int(original or 0), MAX_RESUME_SECONDS))


def continue_session(
    client: Any,
    session_id: str,
    *,
    prompt: str,
    timeout: int,
    model_id: Optional[str] = None,
    on_update: Optional[Callable[[str], None]] = None,
    before_continue: Optional[Callable[[], None]] = None,
    poll_kwargs: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Send one continue prompt into a live session and poll again.

    ``before_continue`` runs after the decision to resume and before the prompt
    is sent, for a consumer that materialises a resume artifact the agent is
    expected to read.

    Returns the second poll's event, or None if the session could not be
    resumed at all -- a client without the surface, or one that raised while
    being nudged. None means "recovery did not happen", which lets
    `run_until_accepted` fall through to an ordinary retry rather than treating
    a failed rescue as a result.
    """
    send = getattr(client, "send_message", None)
    poll = getattr(client, "poll_completion", None)
    if not callable(send) or not callable(poll):
        logger.debug("client cannot resume a session; leaving the stall to the retry path")
        return None

    if on_update:
        on_update(f"recovery: session stalled; sending one guarded continue prompt")
    if before_continue is not None:
        before_continue()

    try:
        send(session_id, prompt, model_id=model_id)
        return poll(
            session_id,
            timeout=resume_timeout(timeout),
            on_update=on_update,
            **(poll_kwargs or {}),
        )
    except Exception as exc:  # noqa: BLE001 - a failed rescue is not a failed turn
        logger.warning("could not resume session %s: %s", session_id, exc)
        return None


def session_recovery(
    client: Any,
    *,
    session_id: str,
    prompt: str,
    timeout: int,
    model_id: Optional[str] = None,
    on_update: Optional[Callable[[str], None]] = None,
    before_continue: Optional[Callable[[], None]] = None,
    poll_kwargs: Optional[Dict[str, Any]] = None,
) -> Callable[..., Any]:
    """A ``recover`` callable for `run_until_accepted`, ready to pass in.

    The whole point of this module::

        loop = run_until_accepted(
            run,
            accept=accept,
            attempts=3,
            recoverable=is_stall,
            recover=session_recovery(client, session_id=sid, prompt=CONTINUE, timeout=900),
        )

    The consumer supplies the prompt, because what to say to resume depends on
    what the turn was doing -- "continue the review" and "continue generating
    tests for this class" are not interchangeable, and a generic instruction
    invites the model to start over, which is the cost this exists to avoid.
    """

    def recover(*, result: Any = None, reason: Any = None) -> Optional[Dict[str, Any]]:
        return continue_session(
            client,
            session_id,
            prompt=prompt,
            timeout=timeout,
            model_id=model_id,
            on_update=on_update,
            before_continue=before_continue,
            poll_kwargs=poll_kwargs,
        )

    return recover
