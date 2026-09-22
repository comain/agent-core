"""Adding up what a run cost, across more than one turn.

`token_usage_from_turn` gives one turn's usage in the column names a table
stores. Almost nothing consists of one turn: a review fans out across
reviewers and a judge, a generation run walks a batch of classes. Every
consumer therefore has to add these up, and every consumer invented its own
way of doing it -- one in SQL::

    COALESCE(SUM(input_tokens), 0), COALESCE(SUM(cache_read_tokens), 0), ...

one in a comprehension over rows, and one as a set of nested bucket helpers.
Three spellings of the same sum, each able to drift from the column names
independently. The SQL version is the dangerous one: name a column that is not
there and it does not fail, it returns zero, which is precisely the silent
under-reporting `token_usage_from_turn` exists to prevent.

## Two vocabularies, on purpose

Turn usage is stored under `input_tokens`, `cache_read_tokens` and so on,
because that is what a schema wants. Providers report `input`, `output`,
`cache.read`. Both appear in real data -- a row read back from a database
speaks the first, a payload straight off a session speaks the second -- so
`add_usage` accepts either and always emits the storage names. A consumer that
had to normalise before summing would be back to writing this itself.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

#: The token counts a turn is measured in, in storage vocabulary.
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)

#: What providers call the same numbers, mapped to the storage name.
_PROVIDER_ALIASES = {
    "input": "input_tokens",
    "output": "output_tokens",
    "reasoning": "reasoning_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write": "cache_write_tokens",
    "total": "total_tokens",
}


def empty_usage() -> Dict[str, Any]:
    """A zeroed total, safe to accumulate into."""
    usage: Dict[str, Any] = {field: 0 for field in TOKEN_FIELDS}
    usage["cost_usd"] = 0.0
    return usage


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        # A provider that reports "1,024" or None should not abort a run over
        # accounting; under-counting a turn beats losing it.
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def add_usage(total: Dict[str, Any], usage: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Add one turn's usage into a running total, in place.

    Accepts either vocabulary. A nested ``cache`` mapping is read too, since
    that is the shape a provider payload arrives in.
    """
    if not usage:
        return total

    for field in TOKEN_FIELDS:
        if field in usage:
            total[field] = _as_int(total.get(field)) + _as_int(usage.get(field))

    for alias, field in _PROVIDER_ALIASES.items():
        if alias in usage:
            total[field] = _as_int(total.get(field)) + _as_int(usage.get(alias))

    cache = usage.get("cache")
    if isinstance(cache, Mapping):
        total["cache_read_tokens"] = _as_int(total.get("cache_read_tokens")) + _as_int(cache.get("read"))
        total["cache_write_tokens"] = _as_int(total.get("cache_write_tokens")) + _as_int(cache.get("write"))

    if "cost_usd" in usage:
        total["cost_usd"] = round(_as_float(total.get("cost_usd")) + _as_float(usage.get("cost_usd")), 6)

    return total


def sum_usage(usages: Iterable[Optional[Mapping[str, Any]]]) -> Dict[str, Any]:
    """Total of many turns, in storage vocabulary.

    ``total_tokens`` is summed as reported rather than recomputed: providers
    do not always make it the sum of the parts -- cached reads are billed
    differently and some report a total that already accounts for that -- and
    a consumer comparing this against an invoice wants what was reported.
    """
    total = empty_usage()
    for usage in usages:
        add_usage(total, usage)
    return total
