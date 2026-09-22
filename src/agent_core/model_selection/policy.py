"""Pure admission rules: no network, mutable application state, or task snapshots."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Candidate(BaseModel):
    """A uniquely bound runtime model/configuration and its quality evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    identity: str = Field(pattern=r"^[^/\s]+/[^\s]+$")
    score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False, strict=True)
    benchmark_id: str | None = None
    effort: str = ""
    variant: str = ""
    capability_approved: bool = False
    # Published blended list price (USD per 1M tokens) and the price actually
    # paid through this provider, after its operator-configured discount.
    # ``None`` means unknown, never free: unknown prices rank after known ones.
    list_price: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)
    price: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)


class ModelPolicy(BaseModel):
    """An application's restrictions, independent of provider credentials/health."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    application_id: str = Field(min_length=1)
    minimum_coding_score: float = Field(default=70, ge=0, le=100, allow_inf_nan=False, strict=True)
    ranking_strategy: Literal["best-score", "price-efficient"] = "price-efficient"
    allowlist: tuple[str, ...] | None = None
    denylist: tuple[str, ...] = ()
    unscored_approvals: tuple[str, ...] = ()

    @field_validator("ranking_strategy", mode="before")
    @classmethod
    def accept_legacy_strategy(cls, value: object) -> object:
        # Configs predating provider pricing named this same ranking
        # "best-efficient"; price is now its first key, effort the second.
        return "price-efficient" if value == "best-efficient" else value


@dataclass(frozen=True)
class SelectionDecision:
    identity: str
    eligible: bool
    reason: str


@dataclass(frozen=True)
class ResolvedSelection:
    """Ephemeral policy-ranked candidates and bounded per-model explanations."""

    candidates: tuple[Candidate, ...]
    decisions: tuple[SelectionDecision, ...]

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(candidate.identity for candidate in self.candidates)


def _matches(identity: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(identity, pattern) for pattern in patterns)


# Max/xhigh are never admitted. Empty/default is max when a score exists.
_PROHIBITED_EFFORTS = frozenset({"xhigh", "max"})


def _reason(candidate: Candidate, policy: ModelPolicy, shared: tuple[str, ...]) -> str:
    identity = candidate.identity
    if _matches(identity, shared):
        return "shared_denied"
    if _matches(identity, policy.denylist):
        return "application_denied"
    if policy.allowlist is not None and not _matches(identity, policy.allowlist):
        return "not_allowlisted"
    if not candidate.capability_approved:
        return "capability_unapproved"
    if candidate.effort in _PROHIBITED_EFFORTS or (
            candidate.effort == "" and candidate.score is not None):
        return "prohibited_effort"
    if candidate.score is None:
        return "eligible" if identity in policy.unscored_approvals else "score_unknown"
    if not candidate.benchmark_id:
        return "benchmark_mapping_required"
    if candidate.score < policy.minimum_coding_score:
        return "below_threshold"
    return "eligible"


def resolve_selection(
    candidates: Iterable[Candidate],
    policy: ModelPolicy,
    *,
    shared_denylist: tuple[str, ...] = (),
    admission_check: Callable[[Candidate], str | None] | None = None,
    effort_strategy: str = "default",
) -> ResolvedSelection:
    """Admit exact variants, rank them, then retain one winner per runtime model."""
    if effort_strategy not in {"default", "higher"}:
        raise ValueError("unknown effort strategy")
    variants: dict[tuple[str, str, str], Candidate] = {}
    for candidate in candidates:
        key = (candidate.identity, candidate.effort, candidate.variant)
        if key in variants:
            raise ValueError(f"duplicate runtime model variant: {candidate.identity}")
        variants[key] = candidate
    winners: dict[str, Candidate] = {}
    decisions: dict[str, SelectionDecision] = {}
    admitted: list[Candidate] = []
    for candidate in sorted(variants.values(), key=lambda c: _ranking_key(c, policy.ranking_strategy)):
        reason = _reason(candidate, policy, shared_denylist)
        if reason == "eligible" and admission_check is not None:
            reason = admission_check(candidate) or "eligible"
        identity = candidate.identity
        if reason == "eligible":
            admitted.append(candidate)
        if reason == "eligible" and identity not in winners:
            winners[identity] = candidate
            decisions[identity] = SelectionDecision(identity, True, reason)
        elif identity not in decisions:
            decisions[identity] = SelectionDecision(identity, False, reason)
    if effort_strategy == "higher":
        # Upgrade within a model, never use effort escalation to bypass provider
        # ordering, admission, or to opt into max/xhigh/default. Unspecified is max.
        levels = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4}
        for identity, current in winners.items():
            higher = [c for c in admitted if c.identity == identity
                      and c.effort in levels
                      and levels[c.effort] > levels.get(current.effort, 4)]
            if higher:
                winners[identity] = min(higher, key=lambda c: (
                    levels[c.effort], -(c.score or 0), c.variant))
    return ResolvedSelection(tuple(winners.values()), tuple(decisions[k] for k in sorted(decisions)))


def _ranking_key(candidate: Candidate, strategy: str) -> tuple:
    # Empty/default effort ranks as max ONLY. Never rewrite the bound effort or
    # reuse another variant's benchmark score/provider options.
    effort_order = {"none": 0, "minimal": 1, "low": 2, "medium": 3,
                    "high": 4, "xhigh": 5, "max": 6, "": 6}
    priced = strategy == "price-efficient" and candidate.score is not None
    effort = effort_order.get(candidate.effort, 7) if priced else 0
    # Cheapest first; an equal price keeps the original efficiency ordering. A
    # zero-discount provider prices every model at 0, so its models rank by
    # effort and score alone. An unknown price is never ranked as free.
    price = round(candidate.price, 9) if priced and candidate.price is not None else 0.0
    # Explicitly approved unscored models remain after every scored model.
    return (candidate.score is None, priced and candidate.price is None, price, effort,
            -(candidate.score or 0), candidate.identity, candidate.effort, candidate.variant)
