"""Reusable conversations behind the same neutral harness interface.

Some workflows need several turns in one agent conversation: generate, inspect
the compiler result, then repair with the context from the first turn.  The
product should still see a harness, not an implementation's create/send/poll
protocol.  A :class:`HarnessSession` therefore is itself a harness runner and
can be handed directly to :func:`agent_core.harness.run_harness_node`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from agent_core.harness.registry import Harness
from agent_core.harness.usage import sum_usage


class SessionUnsupportedError(TypeError):
    """The selected harness can run turns, but cannot resume a conversation."""


class SessionLocatorScope(Enum):
    """How long a session locator stays meaningful.

    The distinction is the whole reason this type exists. An OpenCode client
    conversation is addressed by a UUID this package minted, which means
    nothing to anyone once the process that minted it is gone; the id OpenCode
    writes into its own storage survives a restart and can be inspected
    tomorrow. Storing both under one column and hoping produced exactly the
    failure it sounds like: a diagnostic tool asked to explain yesterday's run
    from a locator that only ever existed in yesterday's memory.
    """

    #: Survives process death; a later process can still resolve it.
    DURABLE = "durable"
    #: Meaningful only inside the process that produced it.
    PROCESS = "process"


#: Neutral harness names, not class names: attribution must survive a rename.
_HARNESS_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
#: A locator is an identifier, never prose. Whitespace and control characters
#: are refused rather than stripped, because a locator that needed cleaning is
#: one nobody should be storing. Bounded in *bytes*, not characters: it is
#: compared byte for byte and stored as UTF-8, and a 512-character limit would
#: be a 2 KiB one for anything non-ASCII.
_LOCATOR = re.compile(r"[^\s\x00-\x1f\x7f]+")
_LOCATOR_MAX_BYTES = 512


@dataclass(frozen=True)
class AgentSessionRef:
    """Which conversation a turn happened in, in neutral terms.

    Validated for *syntax only*. It is deliberately not checked against the
    live harness registry: these get persisted and read back long after the
    process that made them, and a durable record that cannot be constructed
    because a harness has since been unregistered would be a record lost at
    exactly the moment it is needed.
    """

    harness: str
    locator: str
    scope: SessionLocatorScope = SessionLocatorScope.DURABLE

    def __post_init__(self) -> None:
        if not _HARNESS_NAME.fullmatch(self.harness or ""):
            raise ValueError(f"not a neutral harness name: {self.harness!r}")
        if not _LOCATOR.fullmatch(self.locator or ""):
            raise ValueError(f"not a session locator: {self.locator!r}")
        if len(self.locator.encode("utf-8")) > _LOCATOR_MAX_BYTES:
            raise ValueError(
                f"session locator is longer than {_LOCATOR_MAX_BYTES} bytes"
            )
        if isinstance(self.scope, str):
            # Rehydrated from JSON. Coerced here so a checkpoint round trip
            # produces the same type it stored, rather than a string that
            # compares unequal to every scope in the code.
            object.__setattr__(self, "scope", SessionLocatorScope(self.scope))
        if not isinstance(self.scope, SessionLocatorScope):
            raise ValueError(f"not a locator scope: {self.scope!r}")

    @property
    def durable(self) -> bool:
        return self.scope is SessionLocatorScope.DURABLE

    def as_dict(self) -> Dict[str, str]:
        return {
            "harness": self.harness,
            "locator": self.locator,
            "scope": self.scope.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgentSessionRef":
        return cls(
            harness=str(value.get("harness") or ""),
            locator=str(value.get("locator") or ""),
            scope=SessionLocatorScope(str(value.get("scope") or "durable")),
        )


def merge_session_refs(*groups: Iterable[Any]) -> Tuple[AgentSessionRef, ...]:
    """Every ref, in the order first seen, with exact duplicates dropped.

    Order is the record of what was tried: the first candidate, then the one
    that took over after it failed. Sorting or de-duplicating by locator alone
    would lose that, and an operator reading the row could no longer tell which
    conversation produced the answer that was kept.

    Duplicates are ordinary rather than exceptional: a retry and an in-session
    recovery both happen inside the conversation that is already listed.
    """
    merged = []
    seen = set()
    for group in groups:
        for ref in group or ():
            if isinstance(ref, Mapping):
                ref = AgentSessionRef.from_dict(ref)
            key = (ref.harness, ref.locator, ref.scope)
            if key in seen:
                continue
            seen.add(key)
            merged.append(ref)
    return tuple(merged)


# Cost is per *submission*, not per session, so it lives with the paid-attempt
# ledger. Re-exported here because every consumer imports it from this module.
from agent_core.harness.cost import (  # noqa: E402
    CostAdmissionError,
    PaidAttempts,
    TurnCost,
    TurnCostPort,
)


@runtime_checkable
class CostGate(Protocol):
    """Neutral product policy called before a potentially paid attempt.

    Superseded by `TurnCostPort`, which is called around *every* submission
    rather than once per operation. Kept because products implement it today.
    """

    def allow_attempt(self, *, operation_id: str, paid_attempt_ordinal: int) -> None:
        """Raise to prevent the provider call."""


class DiscoveryExecutionPolicy(Protocol):
    """Scoped shared availability; supplying this opts into discovery execution.

    Callers supply current, explicitly ordered model IDs, not a saved task
    selection. Implementations own persistence and concurrent success/failure
    reconciliation. No legacy health or preferred-model state is consulted.
    """

    def is_healthy(self, model: str) -> bool:
        """Read current availability immediately before submission."""

    def mark_unhealthy(self, model: str, *, reason: str) -> None:
        """Record an existing harness provider-failure classification."""

    def mark_success(self, model: str, *, observed_at: float) -> None:
        """Record success using the submission's Unix start time, before its health read.

        Keep this observation local to each submission, not shared on a policy
        instance. Failures still use their detection time for cooldown expiry.
        """


class FallbackHarnessSession:
    """Reusable session that isolates each provider fallback candidate.

    One `run_turn` here can submit to a provider several times, so it accounts
    for its own paid attempts rather than letting a caller count the whole
    chain as one.

    ``discovery_policy`` uses the same shared health port as the process runner.
    ``models`` is an explicit ordered list for this conversation; only in-memory
    traversal state is retained. An empty/unavailable discovery list returns
    ``None`` without opening a provider session or recording a paid attempt.
    """

    #: Read by `run_harness_node`: this harness takes the ledger as a turn
    #: argument and reports each candidate itself.
    accepts_paid_attempts = True

    def __init__(
        self,
        open_session: Callable[[str], "HarnessSession"],
        *,
        models: tuple[str, ...],
        discovery_policy: Optional[DiscoveryExecutionPolicy] = None,
    ) -> None:
        if not models and discovery_policy is None:
            raise ValueError("fallback session needs at least one model")
        self._open_session = open_session
        self._models = tuple(dict.fromkeys(models)) if discovery_policy is not None else models
        self._discovery_policy = discovery_policy
        self._next_index = 0
        self._active: Optional[HarnessSession] = None
        self._closed_snapshots: list[SessionSnapshot] = []
        self._seen_refs: Tuple[AgentSessionRef, ...] = ()
        self._closed = False

    @property
    def session_id(self) -> str:
        if self._active is not None:
            return self._active.session_id
        if self._closed_snapshots:
            return self._closed_snapshots[-1].session_id
        return ""

    def run_turn(self, **kwargs: Any) -> Any:
        if self._closed:
            raise RuntimeError("cannot run a turn on a closed fallback session")
        paid_attempts = kwargs.pop("paid_attempts", None) or PaidAttempts()
        timeout = kwargs.get("timeout_seconds")
        deadline = time.monotonic() + float(timeout) if timeout is not None else None
        last = None
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    from agent_core.harness.process import TurnResult
                    from agent_core.harness.runner import _merge_continuation

                    result = TurnResult(type="timeout", fallback_reason="timeout",
                        model_id=getattr(last, "model_id", None),
                        session_id=getattr(last, "session_id", None),
                        error={"message": "turn deadline exhausted"})
                    return self._with_seen_refs(_merge_continuation(last, result))
                kwargs["timeout_seconds"] = remaining
            if self._active is None:
                if self._next_index >= len(self._models):
                    if last is not None:
                        last.fallback_eligible = False
                    return self._with_seen_refs(last)
                model = self._models[self._next_index]
                self._next_index += 1
                if (
                    self._discovery_policy is not None
                    and not self._discovery_policy.is_healthy(model)
                ):
                    continue
                self._active = self._open_session(model)
            # The active session always belongs to the most recently opened index.
            model = self._models[self._next_index - 1]
            observed_at = time.time() if self._discovery_policy is not None else 0.0
            if (
                self._discovery_policy is not None
                and not self._discovery_policy.is_healthy(model)
            ):
                self._retire_active()
                continue
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._retire_active()
                    continue
                kwargs["timeout_seconds"] = remaining
            with paid_attempts.submission() as attempt:
                result = self._active.run_turn(**kwargs)
                attempt.record_provider(getattr(result, "cost_usd", None))
            if self._discovery_policy is not None:
                # Imported lazily: the process runner also uses session refs.
                from agent_core.harness.runner import _record_discovery_result

                _record_discovery_result(
                    self._discovery_policy, model, self._models, result,
                    observed_at=observed_at,
                )
            if (
                getattr(result, "type", None) == "cancelled"
                or not getattr(result, "fallback_eligible", False)
            ):
                return self._with_seen_refs(result)
            last = self._with_seen_refs(result)
            self._retire_active()
            if self._next_index >= len(self._models):
                # The fallback chain is exhausted inside agent-core.  Products
                # must see one final failure, not a request to requeue the
                # whole task and repeat already-paid provider attempts.
                result.fallback_eligible = False
                return self._with_seen_refs(result)

    def _with_seen_refs(self, result: Any) -> Any:
        """Give the surviving result every candidate's ref, not just its own.

        A chain that failed over twice produced three conversations, and the
        one that answered is the only one the result would otherwise name. The
        two that were paid for and abandoned are exactly what an operator is
        looking for when the bill does not match the transcript.
        """
        refs = merge_session_refs(self._seen_refs, getattr(result, "session_refs", ()))
        try:
            result.session_refs = refs
        except (AttributeError, TypeError):  # a frozen or foreign result
            return result
        return result

    def _retire_active(self) -> None:
        if self._active is None:
            return
        try:
            snapshot = self._active.snapshot()
            self._closed_snapshots.append(snapshot)
            self._seen_refs = merge_session_refs(self._seen_refs, snapshot.session_refs)
        finally:
            self._active.close()
            self._active = None

    def snapshot(self) -> "SessionSnapshot":
        snapshots = list(self._closed_snapshots)
        if self._active is not None:
            snapshots.append(self._active.snapshot())
        costs = [snapshot.provider_cost_usd for snapshot in snapshots]
        provider_cost = None if any(cost is None for cost in costs) else sum(costs)
        current = snapshots[-1] if snapshots else SessionSnapshot(session_id="")
        usage = (
            current.usage
            if len(snapshots) <= 1
            else sum_usage(snapshot.usage for snapshot in snapshots)
        )
        return SessionSnapshot(
            session_id=current.session_id,
            usage=usage,
            retrospect=current.retrospect,
            patch_count=sum(snapshot.patch_count for snapshot in snapshots),
            provider_cost_usd=provider_cost,
            session_refs=merge_session_refs(
                *(snapshot.session_refs for snapshot in snapshots)
            ),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._retire_active()


@dataclass(frozen=True)
class SessionSnapshot:
    """Implementation-neutral diagnostics accumulated by one conversation."""

    session_id: str
    usage: Mapping[str, Any] = field(default_factory=dict)
    retrospect: Mapping[str, Any] = field(default_factory=dict)
    patch_count: int = 0
    #: Every conversation this snapshot covers, in the order they were used.
    #: Plural because a fallback chain is several conversations reported as
    #: one outcome, and the singular id can only name the last of them.
    session_refs: Tuple[AgentSessionRef, ...] = ()
    # Raw provider charge only. ``None`` means the provider did not report a
    # charge; callers must never replace it with a model-price estimate.
    provider_cost_usd: float | None = None


@runtime_checkable
class HarnessSession(Harness, Protocol):
    """A reusable conversation that runs through the ordinary harness API."""

    @property
    def session_id(self) -> str:
        """Stable agent-core identifier for the conversation."""

    def snapshot(self) -> SessionSnapshot:
        """Return usage and diagnostics accumulated so far."""

    def close(self) -> None:
        """Release the underlying conversation; safe to call more than once."""


@runtime_checkable
class ResumableHarness(Harness, Protocol):
    """Optional capability implemented by harnesses that support conversations."""

    def open_session(self, *, repo_path: Path, **kwargs: Any) -> HarnessSession:
        """Open a reusable conversation rooted in ``repo_path``."""


def open_harness_session(
    harness: Harness,
    *,
    repo_path: Path,
    **kwargs: Any,
) -> HarnessSession:
    """Open a conversation without exposing which agent implements it."""
    if not isinstance(harness, ResumableHarness):
        raise SessionUnsupportedError(
            f"{type(harness).__name__} does not support reusable sessions"
        )
    return harness.open_session(repo_path=Path(repo_path).resolve(), **kwargs)
