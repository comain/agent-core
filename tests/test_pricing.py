"""Tests for token pricing.

Rates are load-bearing: the provider these products use reports no usage, so a
derived figure is the only cost number that exists.
"""

from __future__ import annotations

import pytest

from agent_core.pricing import (
    DEFAULT_PRICE_TABLE,
    DEFAULT_RATES,
    ModelRates,
    cost_for_turn,
    cost_from_provider_or_tokens,
    estimate_cost,
    is_priced,
    rates_for,
)
from agent_core.harness import TurnResult


# -- the ordering hazard this replaces -------------------------------------------


def test_specific_pattern_beats_general_regardless_of_order():
    """The bug in the implementation this replaces.

    A general 'gpt-5.4' rule had to be written *after* 'gpt-5.4-mini' or it
    shadowed it. Longest match wins, so ordering carries no meaning.
    """
    assert rates_for("openai/gpt-5.4-mini").input == 0.75
    assert rates_for("openai/gpt-5.4-nano").input == 0.20
    assert rates_for("openai/gpt-5.4").input == 2.50


def test_a_new_general_entry_cannot_shadow_a_specific_one():
    table = dict(DEFAULT_PRICE_TABLE)
    table["gpt"] = ModelRates(input=99.0, output=99.0, cache_read=99.0)  # appended late
    assert rates_for("openai/gpt-5.4-mini", table=table).input == 0.75


def test_matching_is_case_insensitive():
    assert rates_for("Provider/GPT-5.5").input == 5.00


def test_provider_prefix_does_not_matter():
    assert rates_for("token-pool/gpt-5.5") == rates_for("openai/gpt-5.5")


# -- unknown models --------------------------------------------------------------


def test_unknown_model_falls_back_rather_than_failing():
    """A missing rate must not break cost reporting."""
    assert rates_for("brand/new-model") is DEFAULT_RATES


def test_unknown_model_is_reported_as_unpriced():
    """Previously indistinguishable from a priced one."""
    assert is_priced("openai/gpt-5.5") is True
    assert is_priced("brand/new-model") is False
    assert is_priced(None) is False


def test_unknown_model_warns_once(caplog):
    import agent_core.pricing as pricing
    pricing._warned.clear()
    with caplog.at_level("WARNING"):
        rates_for("brand/unseen-model")
        rates_for("brand/unseen-model")
    assert sum("no pricing for model" in r.message for r in caplog.records) == 1


# -- arithmetic ------------------------------------------------------------------


def test_cost_is_per_million_tokens():
    cost = estimate_cost(model="openai/gpt-5.4", input_tokens=1_000_000, output_tokens=0)
    assert cost == pytest.approx(2.50)


def test_output_and_reasoning_are_billed_at_the_output_rate():
    """Reasoning tokens are generated, not read."""
    a = estimate_cost(model="openai/gpt-5.4", output_tokens=1_000_000)
    b = estimate_cost(model="openai/gpt-5.4", reasoning_tokens=1_000_000)
    assert a == b == pytest.approx(15.0)


def test_cached_input_is_cheaper_than_fresh_input():
    fresh = estimate_cost(model="openai/gpt-5.5", input_tokens=1_000_000)
    cached = estimate_cost(model="openai/gpt-5.5", cache_read_tokens=1_000_000)
    assert cached < fresh


def test_components_sum():
    cost = estimate_cost(
        model="openai/gpt-5.4",
        input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000,
    )
    assert cost == pytest.approx(2.50 + 15.0 + 0.25)


def test_zero_tokens_costs_nothing():
    assert estimate_cost(model="openai/gpt-5.5") == 0.0


# -- provider figure vs derived --------------------------------------------------


def test_provider_cost_wins_when_reported():
    assert cost_from_provider_or_tokens(
        provider_cost_usd=0.42, model="openai/gpt-5.5", input_tokens=1_000_000
    ) == 0.42


def test_zero_provider_cost_is_treated_as_not_reported():
    """Which is what a provider omitting usage actually produces."""
    derived = cost_from_provider_or_tokens(
        provider_cost_usd=0.0, model="openai/gpt-5.4", input_tokens=1_000_000
    )
    assert derived == pytest.approx(2.50)


def test_no_tokens_and_no_provider_cost_is_zero():
    assert cost_from_provider_or_tokens(provider_cost_usd=None, model="x") == 0.0


# -- behaviour preserved from the implementation this replaces --------------------


@pytest.mark.parametrize(
    "model,input_rate,output_rate",
    [
        ("kimi-k2.6", 0.7448, 4.655),
        ("gpt-5.5", 5.00, 30.0),
        ("gpt-5.4-mini", 0.75, 4.50),
        ("gpt-5.4-nano", 0.20, 1.25),
        ("gpt-5.4", 2.50, 15.0),
        ("gpt-5.3-codex", 1.75, 14.0),
        ("gpt-5.3", 1.75, 14.0),
    ],
)
def test_rates_match_the_previous_table(model, input_rate, output_rate):
    r = rates_for(model)
    assert (r.input, r.output) == (input_rate, output_rate)


# -- turn integration ------------------------------------------------------------


def test_cost_for_turn_reads_the_parser_token_shape():
    turn = TurnResult(
        type="completed",
        model_id="openai/gpt-5.4",
        tokens={"input": 1_000_000, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    )
    assert cost_for_turn(turn) == pytest.approx(2.50)


def test_cost_for_turn_prefers_a_reported_cost():
    turn = TurnResult(type="completed", model_id="openai/gpt-5.4", cost_usd=0.99,
                      tokens={"input": 1_000_000})
    assert cost_for_turn(turn) == 0.99


def test_cost_for_turn_handles_a_turn_with_no_usage():
    """The live case: this provider reports neither cost nor tokens."""
    assert cost_for_turn(TurnResult(type="completed", model_id="token-pool/gpt-5.5")) == 0.0
