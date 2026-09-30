"""Token pricing.

Providers do not reliably report cost. The token-pool endpoint these products
use returns no usage at all, so a cost figure has to be derived from token
counts and a rate table — which means the rate table is load-bearing for every
consumer's cost reporting, not a nicety.

Rates are USD per million tokens.

## Why this is data rather than an if/elif chain

The implementation this replaces matched model substrings in an ordered
if/elif. That has a failure mode worth naming: ``"gpt-5.4"`` had to appear
*after* ``"gpt-5.4-mini"`` and ``"gpt-5.4-nano"``, because the general pattern
shadows the specific ones. Appending a new rule in the obvious place — the end,
or next to its family — silently reprices a model.

Here the table is data and the **longest matching pattern wins**, so ordering
carries no meaning and cannot be got wrong.

## Unknown models are visible

An unrecognised model previously fell through to a silent default, so a newly
adopted model was priced as if it were something else and nothing said so.
:func:`estimate_cost` still falls back — a missing rate should not break cost
reporting — but :func:`is_priced` lets a caller check, and the fallback is
logged once per model.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, Mapping, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelRates:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float

    @classmethod
    def of(cls, input: float, output: float, cache_read: Optional[float] = None) -> "ModelRates":
        # Cached input is conventionally a tenth of input where a provider does
        # not publish it separately.
        return cls(input=input, output=output,
                   cache_read=input * 0.1 if cache_read is None else cache_read)


#: Fallback when no pattern matches. Deliberately the same numbers the previous
#: implementation used as its silent default, so migrating changes nothing for a
#: model that was already unpriced.
DEFAULT_RATES = ModelRates(input=2.50, output=15.0, cache_read=0.25)

#: Pattern -> rates. Patterns are matched case-insensitively as substrings of
#: the model id; the longest match wins, so specific entries beat general ones
#: regardless of the order they appear here.
DEFAULT_PRICE_TABLE: Dict[str, ModelRates] = {
    "kimi-k2.6": ModelRates(input=0.7448, output=4.655, cache_read=0.7448 * 0.25),
    "kimi/k2.6": ModelRates(input=0.7448, output=4.655, cache_read=0.7448 * 0.25),
    "gpt-5.5": ModelRates(input=5.00, output=30.0, cache_read=0.50),
    "gpt-5.4-mini": ModelRates(input=0.75, output=4.50, cache_read=0.075),
    "gpt-5.4-nano": ModelRates(input=0.20, output=1.25, cache_read=0.02),
    "gpt-5.4": ModelRates(input=2.50, output=15.0, cache_read=0.25),
    "gpt-5.3": ModelRates(input=1.75, output=14.0, cache_read=0.175),
}

_warned: set = set()
_warned_lock = threading.Lock()


def _warn_once(model: str) -> None:
    with _warned_lock:
        if model in _warned:
            return
        _warned.add(model)
    logger.warning(
        "no pricing for model %r; using default rates (in=%.2f out=%.2f per 1M). "
        "Add it to the price table for accurate cost reporting.",
        model, DEFAULT_RATES.input, DEFAULT_RATES.output,
    )


def rates_for(model: Optional[str], *, table: Optional[Mapping[str, ModelRates]] = None) -> ModelRates:
    """Rates for a model. Longest matching pattern wins."""
    table = DEFAULT_PRICE_TABLE if table is None else table
    lowered = (model or "").lower()
    best: Optional[str] = None
    for pattern in table:
        if pattern.lower() in lowered and (best is None or len(pattern) > len(best)):
            best = pattern
    if best is None:
        if lowered:
            _warn_once(lowered)
        return DEFAULT_RATES
    return table[best]


def is_priced(model: Optional[str], *, table: Optional[Mapping[str, ModelRates]] = None) -> bool:
    """Whether an explicit rate exists, as opposed to the default fallback."""
    table = DEFAULT_PRICE_TABLE if table is None else table
    lowered = (model or "").lower()
    return any(pattern.lower() in lowered for pattern in table)


def estimate_cost(
    *,
    model: Optional[str],
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    reasoning_tokens: int = 0,
    table: Optional[Mapping[str, ModelRates]] = None,
) -> float:
    """Cost in USD derived from token counts.

    Reasoning tokens are billed at the output rate: they are generated, not read.
    """
    r = rates_for(model, table=table)
    return (
        (input_tokens * r.input) / 1_000_000
        + (cache_read_tokens * r.cache_read) / 1_000_000
        + ((output_tokens + reasoning_tokens) * r.output) / 1_000_000
    )


def cost_from_provider_or_tokens(
    *,
    provider_cost_usd: Optional[float],
    model: Optional[str],
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    reasoning_tokens: int = 0,
    table: Optional[Mapping[str, ModelRates]] = None,
) -> float:
    """Prefer what the provider charged; fall back to deriving it.

    A provider figure is authoritative when present and positive. Zero is
    treated as "not reported" rather than "free", because that is what a
    provider omitting usage actually produces.
    """
    if provider_cost_usd is not None and provider_cost_usd > 0:
        return float(provider_cost_usd)
    if not (input_tokens or output_tokens or cache_read_tokens or reasoning_tokens):
        return 0.0
    return estimate_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        reasoning_tokens=reasoning_tokens,
        table=table,
    )


def cost_for_turn(result, *, table: Optional[Mapping[str, ModelRates]] = None) -> float:
    """Cost of a :class:`~agent_core.harness.process.TurnResult`.

    Reads the token shape the stream parser produces, so a caller does not have
    to know which keys the harness emits.
    """
    tokens = getattr(result, "tokens", None) or {}
    cache = tokens.get("cache") or {}
    return cost_from_provider_or_tokens(
        provider_cost_usd=getattr(result, "cost_usd", None),
        model=getattr(result, "model_id", None),
        input_tokens=int(tokens.get("input") or 0),
        output_tokens=int(tokens.get("output") or 0),
        reasoning_tokens=int(tokens.get("reasoning") or 0),
        cache_read_tokens=int(cache.get("read") or 0) if isinstance(cache, dict) else 0,
        table=table,
    )
