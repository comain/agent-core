"""Session affinity: keep related turns in one OpenCode session.

`run_turn` accepts a ``session_id`` but nothing decides what it should be, so by
default every turn starts fresh and the model re-reads context it produced
moments earlier.

Affinity binds a caller-chosen key to a session id. Turns sharing a key continue
the same conversation, which matters whenever a later turn should remember an
earlier one -- a design proposal and the revision that answers review comments,
or a fix applied after the finding that prompted it.

    affinity = SessionAffinity()

    first = affinity.run(harness, "review-42", message="propose a design",
                         repo_path=repo)
    # ... a human answers a gate ...
    second = affinity.run(harness, "review-42", message="address these comments",
                          repo_path=repo)   # same session; sees its own proposal

Thread-safe: consumers fan turns out across a pool, and two workers sharing a
key must not each start a session.

Harnesses that isolate every attempt in a disposable workspace retain model
affinity but start a fresh session for each turn. Continuing such a session can
make the model reuse absolute paths into a workspace that no longer exists.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional


def _isolated_turn_message(message: Any, bootstrap_message: Any) -> Any:
    """Make a continuation turn self-contained in its new workspace."""
    if not bootstrap_message or bootstrap_message == message:
        return message
    if not message:
        return bootstrap_message
    return (
        f"{str(bootstrap_message).rstrip()}\n\n"
        "Current continuation instruction:\n"
        f"{str(message).strip()}"
    )


class SessionAffinity:
    """Maps an affinity key to the OpenCode session serving it.

    ``max_turns`` bounds how long one session is reused. Context grows with every
    turn, and an unbounded session eventually costs more in re-sent history than
    it saves; reaching the limit starts a fresh session under the same key.
    """

    def __init__(self, *, max_turns: Optional[int] = None):
        self._sessions: Dict[str, tuple[str, Optional[str]]] = {}
        self._turns: Dict[str, int] = {}
        self._lock = threading.Lock()
        self.max_turns = max_turns

    # -- inspection --------------------------------------------------------

    def has_session(self, key: str) -> bool:
        with self._lock:
            return bool(self._sessions.get(key))

    def session_id(self, key: str) -> Optional[str]:
        with self._lock:
            bound = self._sessions.get(key)
            return bound[0] if bound else None

    def model_id(self, key: str) -> Optional[str]:
        with self._lock:
            bound = self._sessions.get(key)
            return bound[1] if bound else None

    def turn_count(self, key: str) -> int:
        with self._lock:
            return self._turns.get(key, 0)

    def reset(self, key: str) -> None:
        """Forget a key, so the next turn starts a new session."""
        with self._lock:
            self._sessions.pop(key, None)
            self._turns.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._turns.clear()

    # -- running -----------------------------------------------------------

    def _claim(self, key: str) -> Optional[tuple[str, Optional[str]]]:
        """Session to continue, or None to start a new one."""
        with self._lock:
            if self.max_turns is not None and self._turns.get(key, 0) >= self.max_turns:
                self._sessions.pop(key, None)
                self._turns.pop(key, None)
            return self._sessions.get(key)

    def _record(self, key: str, session_id: Optional[str], model_id: Optional[str]) -> None:
        if not session_id:
            # A turn that produced no session id cannot be continued. Leave any
            # existing binding alone rather than clearing it: one malformed
            # response should not discard a working conversation.
            return
        with self._lock:
            self._sessions[key] = (session_id, model_id)
            self._turns[key] = self._turns.get(key, 0) + 1

    def run(self, harness: Any, key: str, **kwargs: Any) -> Any:
        """Run a turn bound to ``key``, continuing its session when one exists.

        ``harness`` is anything with ``run_turn`` (the ``Harness`` protocol).
        Any ``session_id`` in ``kwargs`` wins -- an explicit choice by the
        caller should not be silently overridden by affinity bookkeeping.
        """
        if "session_id" not in kwargs:
            bound = self._claim(key)
            bound_sid, bound_model = bound if bound else (None, None)
            turn_model = kwargs.get("model_id")
            model_mismatch = bool(
                bound_sid and bound_model and turn_model and bound_model != turn_model
            )
            isolated_workspace = bool(
                bound_sid and getattr(harness, "isolate_attempts", False)
            )
            if bound_sid and not model_mismatch and not isolated_workspace:
                kwargs["session_id"] = bound_sid
                if turn_model is None and bound_model:
                    kwargs["model_id"] = bound_model
            else:
                kwargs["session_id"] = None
                bootstrap = kwargs.get("bootstrap_message")
                if isolated_workspace:
                    kwargs["message"] = _isolated_turn_message(
                        kwargs.get("message"), bootstrap
                    )
                    if turn_model is None and bound_model:
                        kwargs["model_id"] = bound_model
                elif bound_sid and bootstrap is not None:
                    kwargs["message"] = bootstrap
        result = harness.run_turn(**kwargs)
        self._record(
            key,
            getattr(result, "session_id", None),
            getattr(result, "model_id", None) or kwargs.get("model_id"),
        )
        return result
