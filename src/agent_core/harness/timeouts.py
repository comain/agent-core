"""How long to give a turn, given which model is running it.

A consumer knows how much work a phase is worth -- ten minutes to fix a
compile error, fifteen to repair a test. It does not know, and should not
have to, that one provider's models take roughly twice as long to produce
the same answer. That is a fact about the provider, and the provider chain
lives here.

The code this replaces had the consumer asking ``is_deepseek_model(model_id)``
and multiplying. Two things were wrong with that. A test-generation tool had
a function whose name was a vendor's; and adding a second slow provider meant
a second predicate, a second setting, and a second branch at every call site.

So the multipliers are data, keyed by provider::

    opencode_provider_timeout_multipliers = "deepseek=2.0;openrouter=1.5"

and a consumer asks only ``effective_timeout(600, model_id=...)``.
"""

from __future__ import annotations

from typing import Dict, Optional

from agent_core.config import settings


def parse_provider_timeout_multipliers(raw: str) -> Dict[str, float]:
    """Parse ``provider=multiplier`` entries separated by semicolons.

    Entries that are malformed, non-numeric or non-positive are skipped rather
    than raising: a typo in one provider's multiplier should slow nothing down
    and stop nothing, and the alternative is a service that will not start.
    """
    multipliers: Dict[str, float] = {}
    if not isinstance(raw, str):
        return multipliers
    for raw_entry in (raw or "").split(";"):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            continue
        provider, _, value = entry.partition("=")
        provider = provider.strip().lower()
        if not provider:
            continue
        try:
            multiplier = float(value.strip())
        except (TypeError, ValueError):
            continue
        if multiplier > 0:
            multipliers[provider] = multiplier
    return multipliers


def provider_of(model_id: Optional[str]) -> str:
    """The provider a model id belongs to.

    ``deepseek/deepseek-v4-pro`` names its provider; a bare ``gpt-5.5`` does
    not, and falls back to the configured provider -- which is what the caller
    would have been routed to.
    """
    model = (model_id or "").strip().lower()
    if "/" in model:
        return model.split("/", 1)[0]
    return (getattr(settings, "opencode_provider", "") or "").strip().lower()


def timeout_multiplier_for(model_id: Optional[str] = None) -> float:
    """The combined multiplier applied to a turn budget for this model."""
    multiplier = float(getattr(settings, "opencode_timeout_multiplier", 1.0) or 1.0)
    by_provider = parse_provider_timeout_multipliers(
        getattr(settings, "opencode_provider_timeout_multipliers", "") or ""
    )
    provider = provider_of(model_id)
    if provider and provider in by_provider:
        multiplier *= by_provider[provider]
    return multiplier


def effective_timeout(base_seconds: int, model_id: Optional[str] = None) -> int:
    """Scale a caller's budget by what this model is known to need.

    ``base_seconds`` is the consumer's judgement about the work; everything
    applied to it here is a fact about the provider. Never returns less than a
    second, so a misconfigured multiplier cannot produce a turn that times out
    before it starts.
    """
    base = max(1, int(base_seconds))
    return max(1, int(base * timeout_multiplier_for(model_id)))
