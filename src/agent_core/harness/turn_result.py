"""One neutral answer to "what happened in that turn?".

Without this, every product reads the harness's own result object to decide
what to do next. That has two costs. Each product independently learns that
one provider says ``timeout`` while another says ``timed_out``, and the
vocabulary drifts. And the provider object itself ends up on graph state,
where LangGraph will try to checkpoint it -- unserializable at best, and at
worst a payload holding an API key or a prompt written to disk.

`AgentTurnResult` is the boundary. It is frozen, JSON-safe, and contains no
provider object, exception, or error text.

It lives beside the harness rather than beside the workflow because it is the
answer to "what happened in that turn?", and a product that runs turns directly
— no graph, no LangGraph installed — needs that answer just as much as one that
runs them through a node. `agent_core.workflow` re-exports it for the graph
path.

## Why status is a precedence, not a mapping

Several things can be true of one turn: it can be *skipped* -- meaning the
workflow chose to tolerate the failure -- and also have *timed out*. A mapping
would have to pick arbitrarily. The order is

    cancelled → timed_out → rate_limited → stalled → failed → skipped → completed

so the most specific cause wins. "Skipped" describes the workflow's tolerance,
not the fault; an operator asking why a unit produced nothing needs the fault.
The order also fails closed at the bottom: anything not recognized as an
outright success is reported as `failed`, never as `completed`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from agent_core.harness.lifecycle import sanitize_detail
from agent_core.harness.sessions import AgentSessionRef, merge_session_refs

#: What a harness result's `type` means in neutral terms. Only causes that a
#: product would branch on differently are distinguished.
_TURN_TYPE_STATUS = {
    "cancelled": "cancelled",
    "timeout": "timed_out",
    "timed_out": "timed_out",
    "rate_limited": "rate_limited",
    "stalled": "stalled",
    "error": "failed",
}

#: A fallback reason is a classification token, not prose. Anything else is
#: replaced wholesale rather than trimmed -- a partially-redacted provider
#: message is still a provider message.
_FALLBACK_REASON = re.compile(r"[a-z0-9_.:-]{1,64}")

#: Most specific first. Used to choose between a cause read off the turn and
#: the node's own verdict.
_PRECEDENCE = (
    "cancelled",
    "timed_out",
    "rate_limited",
    "stalled",
    "failed",
    "skipped",
    "completed",
)


@dataclass(frozen=True)
class AgentTurnResult:
    """What one agent turn produced, in terms any product can act on.

    Frozen and JSON-safe because it is returned as graph state and therefore
    checkpointed. Missing usage, retrospective and patch data normalize to
    empty maps and zero rather than to a provider's own sentinel, so a product
    summing across phases never has to check for `None`, a string, or
    `"unknown"`.
    """

    status: str
    text: str = ""
    #: Every conversation the turn used, in the order they were used. Plural
    #: rather than one `session_id`: a fallback chain is several conversations
    #: reported as one outcome, and the singular field could only ever name the
    #: last one tried — so the earlier, already-paid-for candidates were
    #: impossible to attribute in exactly the runs where that mattered most.
    session_refs: Tuple[AgentSessionRef, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    provider_cost_usd: Optional[float] = None
    retrospective: Mapping[str, Any] = field(default_factory=dict)
    patch_count: int = 0
    recovered: bool = False
    attempts: int = 0
    elapsed_seconds: float = 0.0
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    raw_log_path: Optional[str] = None

    @classmethod
    def cancelled(cls, **overrides: Any) -> "AgentTurnResult":
        """A turn that never started because the task was already cancelled.

        There is no outcome to normalize, but a product still needs a
        persistable envelope: the operation row was already opened, and
        leaving it open would make a deliberate cancellation look like a crash.
        """
        return cls(status="cancelled", **overrides)

    def as_dict(self) -> Dict[str, Any]:
        """JSON-safe, because this is checkpointed as graph state.

        `asdict` would leave `SessionLocatorScope` members in place, and an
        enum is not JSON — it survives a `dict()` and fails at the checkpoint.
        """
        data = asdict(self)
        data["session_refs"] = [ref.as_dict() for ref in self.session_refs]
        return data

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def _mapping(value: Any) -> Dict[str, Any]:
    """A JSON-safe mapping, whatever the harness actually supplied."""
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _json_safe(item) for key, item in value.items()}


def _turn_status(turn: Any) -> Optional[str]:
    """The neutral status implied by the harness result, if it implies one."""
    turn_type = str(getattr(turn, "type", "") or "")
    return _TURN_TYPE_STATUS.get(turn_type)


def _node_status(outcome: Any) -> str:
    """The node's own verdict, failing closed.

    Only an explicit `accepted` reads as a completion. A status added to the
    harness later, or one this does not recognize, must not be reported as
    success -- the expensive direction of a wrong guess here is a product
    committing a result that was never produced.
    """
    status = str(getattr(outcome, "status", "") or "")
    if status == "accepted":
        return "completed"
    if status == "skipped":
        return "skipped"
    return "failed"


def normalize_turn_outcome(
    outcome: Any,
    *,
    snapshot: Any = None,
    elapsed_seconds: float = 0.0,
    raw_log_path: Optional[str] = None,
) -> AgentTurnResult:
    """Turn a `NodeOutcome` and its pre-close `SessionSnapshot` into a result.

    The snapshot must be captured *before* the session closes: usage,
    retrospective and patch count exist only on the live session.
    """
    turn = getattr(outcome, "result", None)

    node_verdict = _node_status(outcome)
    from_turn = _turn_status(turn)
    if node_verdict == "completed" and from_turn not in (None, "cancelled"):
        # The node accepted the step, so a cause still showing on the turn
        # object -- a stall that in-session recovery went on to fix -- is
        # history, not the outcome. Downgrading here would make a product redo
        # an expensive phase whose answer it already has. The cause stays in
        # diagnostics. Cancellation is the exception: a cancelled task must
        # never report a completion, whatever arrived.
        from_turn = None
    candidates = [node_verdict] + ([from_turn] if from_turn else [])
    status = min(candidates, key=_PRECEDENCE.index)

    diagnostics: Dict[str, Any] = {}
    if turn_type := str(getattr(turn, "type", "") or ""):
        # The provider's own word for it, kept as a short label for an
        # operator. Never the error text: a message can quote a prompt, a
        # path, or a credential.
        diagnostics["turn_type"] = turn_type
    if getattr(outcome, "error", None):
        diagnostics["error"] = True
    node_status = str(getattr(outcome, "status", "") or "")
    if node_status:
        diagnostics["node_status"] = node_status
    if getattr(turn, "fallback_eligible", False):
        # The harness judged this failure worth retrying on another provider --
        # a gateway resetting a long stream, a rate limit. A product cannot act
        # on that if normalization drops it: the run just ends having produced
        # nothing, and the recoverable fault looks like an ordinary failure.
        diagnostics["fallback_eligible"] = True
        reason = str(getattr(turn, "fallback_reason", "") or "").strip()
        if reason:
            # A short classification only. The provider's own message can quote
            # a request, a path or a credential, so anything that is not a
            # bare token is refused rather than trimmed.
            diagnostics["fallback_reason"] = (
                reason if _FALLBACK_REASON.fullmatch(reason) else "unclassified"
            )
    model_attempts = _safe_model_attempts(getattr(turn, "model_attempts", ()))
    if model_attempts:
        diagnostics["model_attempts"] = model_attempts

    text = getattr(turn, "result", None)

    return AgentTurnResult(
        status=status,
        text=str(text) if text else "",
        session_refs=merge_session_refs(
            getattr(turn, "session_refs", ()),
            getattr(snapshot, "session_refs", ()),
        ),
        usage=_mapping(getattr(snapshot, "usage", None)),
        provider_cost_usd=getattr(snapshot, "provider_cost_usd", None),
        retrospective=_mapping(getattr(snapshot, "retrospect", None)),
        patch_count=max(int(getattr(snapshot, "patch_count", 0) or 0), 0),
        recovered=bool(getattr(outcome, "recovered", False)),
        attempts=int(getattr(outcome, "attempts", 0) or 0),
        elapsed_seconds=float(elapsed_seconds or 0.0),
        diagnostics=diagnostics,
        raw_log_path=str(raw_log_path) if raw_log_path else None,
    )


def _safe_model_attempts(value: Any) -> list[Dict[str, Any]]:
    """Keep only bounded classifications and sanitized HTTP evidence."""

    attempts: list[Dict[str, Any]] = []
    for item in value or ():
        if not isinstance(item, Mapping):
            continue
        model = str(item.get("model") or "").strip()
        outcome = str(item.get("outcome") or "").strip()
        reason = str(item.get("fallback_reason") or "").strip()
        if not model:
            continue
        attempt: Dict[str, Any] = {
                "model": model[:256],
                "outcome": outcome[:64] or "unknown",
                "fallback_reason": (
                    reason if _FALLBACK_REASON.fullmatch(reason) else "unclassified"
                )
                if reason
                else "",
            }
        try:
            status = int(item.get("http_status"))
        except (TypeError, ValueError):
            status = 0
        if 100 <= status <= 599:
            attempt["http_status"] = status
        code = str(item.get("error_code") or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_.+-]{1,96}", code):
            attempt["error_code"] = code
        detail = sanitize_detail(item.get("error_detail"))
        if detail:
            attempt["error_detail"] = detail
        attempts.append(attempt)
    return attempts
