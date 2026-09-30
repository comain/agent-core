"""What a product writes down about a turn.

Every consumer keeps its own table -- the columns differ, and so do the names
-- but they all answer the same questions first, and each answered them
slightly differently:

- did this turn succeed? A turn that completed without a session id is not a
  success, however it looks: there is no session to attribute cost or
  transcript to, and treating it as one produces rows nothing can be traced
  back to.
- if it failed, what does the operator read? The provider's own message when
  there is one, the stop reason when there is not.
- which model gets the cost? What the turn reports, falling back to what the
  caller would have selected -- never a name written into code.
- how many tokens, in the column names a table stores rather than the ones
  the agent emits.

`describe_turn` answers all four in one place. The SQL stays with the product;
what goes into it does not have to be derived four times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agent_core.harness.sessions import merge_session_refs
from agent_core.pricing import cost_from_provider_or_tokens

SUCCESS = "success"
FAILED = "failed"


@dataclass(frozen=True)
class TurnRecord:
    """One turn, reduced to what a row needs."""

    status: str
    session_id: Optional[str] = None
    model_id: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    raw_log_path: Optional[str] = None
    #: Every conversation the turn used, in order. `session_id` stays the
    #: single column a product's existing row already has; this is the full
    #: story a fallback chain leaves behind, for the products that want it.
    session_refs: tuple = ()

    @property
    def succeeded(self) -> bool:
        return self.status == SUCCESS


def token_usage_from_turn(result: Any, default_model: Optional[str] = None) -> Dict[str, Any]:
    """One turn's usage, in the column names a database stores.

    The agent reports `input`/`output`/`total` with a nested `cache`; tables
    store `input_tokens`, `cache_read_tokens` and so on. A consumer that
    skipped the translation stored the agent's names and read back zeroes.

    `default_model` prices a turn the provider did not attribute. Callers pass
    the model they would have selected rather than naming one, so a deployment
    that reorders its provider chain does not silently cost turns at another
    model's rates.
    """
    tokens = getattr(result, "tokens", None) or {}
    cache = tokens.get("cache") or {}
    input_tokens = int(tokens.get("input") or 0)
    output_tokens = int(tokens.get("output") or 0)
    reasoning_tokens = int(tokens.get("reasoning") or 0)
    cache_read_tokens = int(cache.get("read") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": int(cache.get("write") or 0),
        "total_tokens": int(tokens.get("total") or 0),
        "cost_usd": cost_from_provider_or_tokens(
            provider_cost_usd=getattr(result, "cost_usd", None),
            model=getattr(result, "model_id", None) or default_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            reasoning_tokens=reasoning_tokens,
        ),
    }


def turn_failure_message(result: Any, *, missing_session_message: str = "missing session id") -> str:
    """Why a turn is not a success, in words an operator can act on.

    Prefers what the provider said. `stalled` on its own tells an operator
    nothing; `stalled: OpenCodeNoOutputStall` tells them where to look.
    """
    error = getattr(result, "error", None)
    error = error if isinstance(error, dict) else {}
    detail = error.get("message") or error.get("name")
    if detail:
        return f"{getattr(result, 'type', 'error')}: {detail}"
    if getattr(result, "type", None) != "completed":
        return str(getattr(result, "type", None) or "error")
    return missing_session_message


def describe_turn(
    result: Any,
    *,
    default_model: Optional[str] = None,
    missing_session_message: str = "missing session id",
) -> TurnRecord:
    """Reduce a finished turn to the row a product will store."""
    completed = getattr(result, "type", None) == "completed"
    session_id = getattr(result, "session_id", None)
    succeeded = bool(completed and session_id)
    return TurnRecord(
        status=SUCCESS if succeeded else FAILED,
        session_id=session_id,
        model_id=getattr(result, "model_id", None) or default_model,
        usage=token_usage_from_turn(result, default_model),
        error=None
        if succeeded
        else turn_failure_message(result, missing_session_message=missing_session_message),
        raw_log_path=getattr(result, "raw_log_path", None),
        session_refs=merge_session_refs(getattr(result, "session_refs", ())),
    )
