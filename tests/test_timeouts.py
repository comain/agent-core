"""Scaling a turn budget by what the model is known to need.

The consumer decides what a phase is worth -- ten minutes to fix a compile
error. How much longer one provider takes to deliver the same answer is a
fact about the provider, and belongs with the provider chain, not in a
predicate named after a vendor in a test-generation tool.
"""

from __future__ import annotations

import pytest

from agent_core.config import HarnessConfig, current_config, set_default_config
from agent_core.harness.timeouts import (
    effective_timeout,
    parse_provider_timeout_multipliers,
    provider_of,
    timeout_multiplier_for,
)


@pytest.fixture(autouse=True)
def restore_default():
    original = current_config()
    yield
    set_default_config(original)


def configure(**kwargs):
    set_default_config(HarnessConfig(**kwargs))


# -- parsing ---------------------------------------------------------------

def test_it_parses_provider_entries():
    assert parse_provider_timeout_multipliers("deepseek=2.0;openrouter=1.5") == {
        "deepseek": 2.0,
        "openrouter": 1.5,
    }


def test_it_is_case_insensitive_and_tolerates_spacing():
    assert parse_provider_timeout_multipliers(" DeepSeek = 2.0 ; ") == {"deepseek": 2.0}


@pytest.mark.parametrize(
    "raw",
    ["", "deepseek", "deepseek=", "deepseek=abc", "=2.0", "deepseek=0", "deepseek=-1"],
)
def test_malformed_entries_are_skipped_rather_than_raising(raw):
    """A typo in one multiplier must not stop a service from starting."""
    assert parse_provider_timeout_multipliers(raw) == {}


def test_one_bad_entry_does_not_discard_the_good_ones():
    assert parse_provider_timeout_multipliers("deepseek=oops;openrouter=1.5") == {
        "openrouter": 1.5
    }


# -- provider resolution ---------------------------------------------------

def test_a_qualified_model_names_its_provider():
    configure(opencode_provider="token-pool")
    assert provider_of("deepseek/deepseek-v4-pro") == "deepseek"


def test_a_bare_model_falls_back_to_the_configured_provider():
    """Which is where it would have been routed."""
    configure(opencode_provider="deepseek")
    assert provider_of("gpt-5.5") == "deepseek"


def test_no_model_falls_back_to_the_configured_provider():
    configure(opencode_provider="token-pool")
    assert provider_of(None) == "token-pool"


# -- the budget ------------------------------------------------------------

def test_the_default_leaves_a_budget_alone():
    configure()
    assert effective_timeout(600) == 600


def test_a_global_multiplier_applies_to_every_model():
    configure(opencode_timeout_multiplier=1.5)
    assert effective_timeout(600) == 900


def test_a_slow_provider_gets_longer():
    configure(opencode_provider_timeout_multipliers="deepseek=2.0")
    assert effective_timeout(600, "deepseek/deepseek-v4-pro") == 1200


def test_another_provider_is_unaffected():
    configure(opencode_provider_timeout_multipliers="deepseek=2.0")
    assert effective_timeout(600, "openai/gpt-5.5") == 600


def test_global_and_provider_multipliers_compound():
    configure(
        opencode_timeout_multiplier=1.5,
        opencode_provider_timeout_multipliers="deepseek=2.0",
    )
    assert effective_timeout(600, "deepseek/deepseek-v4-pro") == 1800


def test_a_bare_model_on_a_slow_configured_provider_gets_longer():
    configure(
        opencode_provider="deepseek",
        opencode_provider_timeout_multipliers="deepseek=2.0",
    )
    assert effective_timeout(600, "deepseek-v4-pro") == 1200


def test_a_second_slow_provider_needs_no_code():
    """The reason this is data: adding one is a config change."""
    configure(opencode_provider_timeout_multipliers="deepseek=2.0;openrouter=1.5")
    assert effective_timeout(600, "openrouter/z-ai/glm-5.1") == 900


def test_a_budget_never_collapses_below_a_second():
    configure(opencode_timeout_multiplier=0.0001)
    assert effective_timeout(1) == 1


def test_a_zero_or_negative_base_still_yields_a_usable_budget():
    configure()
    assert effective_timeout(0) == 1
    assert effective_timeout(-30) == 1


def test_the_multiplier_is_reportable_on_its_own():
    """Operators ask why a turn was given twice as long."""
    configure(opencode_provider_timeout_multipliers="deepseek=2.0")
    assert timeout_multiplier_for("deepseek/deepseek-v4-pro") == 2.0
