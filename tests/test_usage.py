"""Adding up what a run cost across more than one turn.

Every consumer had to do this and each invented its own way: a SUM() over
columns in SQL, a comprehension over rows, and a set of nested bucket
helpers. The SQL one is the reason this exists -- name a column that is not
there and it returns zero rather than failing, which is the silent
under-reporting `token_usage_from_turn` was written to prevent.
"""

from __future__ import annotations

import pytest

from agent_core.harness.usage import (
    TOKEN_FIELDS,
    add_usage,
    empty_usage,
    sum_usage,
)


def test_an_empty_total_is_zero_everywhere():
    usage = empty_usage()
    assert all(usage[f] == 0 for f in TOKEN_FIELDS)
    assert usage["cost_usd"] == 0.0


def test_it_sums_storage_vocabulary():
    """What a row read back from a table looks like."""
    total = sum_usage([
        {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cost_usd": 0.5},
        {"input_tokens": 200, "output_tokens": 20, "total_tokens": 220, "cost_usd": 0.25},
    ])

    assert total["input_tokens"] == 300
    assert total["output_tokens"] == 30
    assert total["total_tokens"] == 330
    assert total["cost_usd"] == 0.75


def test_it_sums_provider_vocabulary():
    """What a payload straight off a session looks like."""
    total = sum_usage([
        {"input": 100, "output": 10, "total": 110},
        {"input": 200, "output": 20, "total": 220},
    ])

    assert total["input_tokens"] == 300
    assert total["total_tokens"] == 330


def test_the_two_vocabularies_mix():
    """Both appear in real data, often in the same aggregation."""
    total = sum_usage([
        {"input_tokens": 100},
        {"input": 50},
    ])

    assert total["input_tokens"] == 150


def test_a_nested_cache_mapping_is_read():
    total = sum_usage([{"cache": {"read": 40, "write": 5}}])

    assert total["cache_read_tokens"] == 40
    assert total["cache_write_tokens"] == 5


def test_nothing_at_all_totals_zero():
    assert sum_usage([]) == empty_usage()
    assert sum_usage([None, {}, None])["total_tokens"] == 0


def test_a_missing_field_contributes_nothing_rather_than_erroring():
    total = sum_usage([{"input_tokens": 10}, {"output_tokens": 5}])

    assert total["input_tokens"] == 10
    assert total["output_tokens"] == 5
    assert total["reasoning_tokens"] == 0


@pytest.mark.parametrize("bad", ["1,024", "abc", None, object()])
def test_an_unparseable_count_does_not_abort_the_run(bad):
    """Under-counting one turn beats losing the whole aggregate to accounting."""
    total = sum_usage([{"input_tokens": bad}, {"input_tokens": 7}])

    assert total["input_tokens"] == 7


def test_total_is_taken_as_reported_not_recomputed():
    """Providers bill cached reads differently; a consumer reconciling against
    an invoice wants what was reported, not our arithmetic."""
    total = sum_usage([{"input_tokens": 100, "output_tokens": 10, "total_tokens": 60}])

    assert total["total_tokens"] == 60


def test_add_usage_accumulates_in_place():
    total = empty_usage()

    add_usage(total, {"input_tokens": 5})
    add_usage(total, {"input_tokens": 5})

    assert total["input_tokens"] == 10


def test_it_totals_what_the_turn_helper_produces():
    """The two halves have to fit: one turn in, many turns summed out."""
    from agent_core.harness.records import token_usage_from_turn

    class Turn:
        tokens = {"input": 10, "output": 2, "total": 12, "cache": {"read": 3, "write": 1}}
        cost_usd = 0.01
        model_id = "token-pool/gpt-5.5"

    one = token_usage_from_turn(Turn())
    total = sum_usage([one, one])

    assert total["input_tokens"] == 20
    assert total["cache_read_tokens"] == 6
    assert total["total_tokens"] == 24
