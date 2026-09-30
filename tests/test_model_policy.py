"""Target-specific tests for model_selection.policy."""

import math

import pytest

from agent_core.model_selection import Candidate, ModelPolicy, resolve_selection


def model(name, score=75, **kwargs):
    return Candidate(identity=f"pool/{name}", score=score, benchmark_id=name,
                     effort="high", variant="high", capability_approved=True, **kwargs)


def test_scores_rank_descending_without_input_order_or_application_leakage():
    candidates = [model("z", 75), model("a", 75), model("best", 80)]
    unrestricted = ModelPolicy(application_id="uta")
    restricted = ModelPolicy(application_id="cr", denylist=("pool/best",))
    assert resolve_selection(candidates, restricted).model_ids == ("pool/a", "pool/z")
    assert resolve_selection(reversed(candidates), unrestricted).model_ids == (
        "pool/best", "pool/a", "pool/z")


@pytest.mark.parametrize("score,eligible", [(69.999, False), (70, True), (70.001, True)])
def test_inclusive_unrounded_threshold(score, eligible):
    result = resolve_selection([model("a", score)], ModelPolicy(application_id="uta"))
    assert bool(result.candidates) == eligible


def test_empty_allowlist_and_denials_cannot_be_overridden():
    candidates = [model("a")]
    assert not resolve_selection(candidates, ModelPolicy(application_id="uta", allowlist=())).candidates
    policy = ModelPolicy(application_id="uta", allowlist=("pool/*",), denylist=("pool/a",))
    assert resolve_selection(candidates, policy).decisions[0].reason == "application_denied"
    assert resolve_selection(candidates, ModelPolicy(application_id="uta"),
                             shared_denylist=("pool/*",)).decisions[0].reason == "shared_denied"


def test_unscored_approval_does_not_override_known_low_score():
    candidates = [model("unknown", None), model("low", 20), model("ok", 75)]
    policy = ModelPolicy(application_id="uta", unscored_approvals=("pool/unknown", "pool/low"))
    result = resolve_selection(candidates, policy)
    assert result.model_ids == ("pool/ok", "pool/unknown")
    assert resolve_selection(candidates, ModelPolicy(application_id="cr")).model_ids == ("pool/ok",)


@pytest.mark.parametrize("score", [math.nan, math.inf, -1, 101, True, "75"])
def test_invalid_scores_rejected(score):
    with pytest.raises(ValueError):
        model("invalid", score)


def test_duplicate_identities_rejected_and_unknown_capability_pending():
    with pytest.raises(ValueError, match="duplicate"):
        resolve_selection([model("a"), model("a", 80)], ModelPolicy(application_id="uta"))
    candidate = model("a").model_copy(update={"capability_approved": False})
    assert resolve_selection([candidate], ModelPolicy(application_id="uta")).decisions[0].reason == "capability_unapproved"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 101, True, ""])
def test_invalid_policy_threshold(value):
    with pytest.raises(ValueError):
        ModelPolicy(application_id="uta", minimum_coding_score=value)


def test_policy_zero_and_case_sensitive_patterns():
    policy = ModelPolicy(application_id="uta", minimum_coding_score=0, allowlist=("pool/A",))
    assert resolve_selection([model("a", 0)], policy).model_ids == ()
    assert resolve_selection([model("A", 0)], policy).model_ids == ("pool/A",)


def test_admission_explanations_preserve_model_identity_and_reason():
    cases = [
        (model("low", 69), ModelPolicy(application_id="test"), "below_threshold"),
        (model("unknown", None), ModelPolicy(application_id="test"), "score_unknown"),
        (model("missing").model_copy(update={"benchmark_id": None}),
         ModelPolicy(application_id="test"), "benchmark_mapping_required"),
        (model("excluded"), ModelPolicy(application_id="test", allowlist=()), "not_allowlisted"),
        (ranked_model("qwen", 73.1, ""), ModelPolicy(application_id="test"), "prohibited_effort"),
        (ranked_model("max", 80, "max"), ModelPolicy(application_id="test"), "prohibited_effort"),
        (ranked_model("xhigh", 80, "xhigh"), ModelPolicy(application_id="test"), "prohibited_effort"),
    ]
    for candidate, policy, expected in cases:
        result = resolve_selection([candidate], policy)
        assert result.model_ids == ()
        assert len(result.decisions) == 1
        assert (result.decisions[0].identity, result.decisions[0].eligible,
                result.decisions[0].reason) == (candidate.identity, False, expected)


def test_accepted_decisions_and_empty_catalog_are_explicit():
    empty = resolve_selection([], ModelPolicy(application_id="test"))
    assert empty.candidates == empty.decisions == ()
    candidate = model("accepted")
    result = resolve_selection([candidate], ModelPolicy(application_id="test"))
    assert result.candidates == (candidate,)
    assert [(d.identity, d.eligible, d.reason) for d in result.decisions] == [
        ("pool/accepted", True, "eligible")]


def ranked_model(name, score, effort):
    return model(name, score).model_copy(update={"effort": effort, "variant": effort})


@pytest.mark.parametrize("strategy,expected", [
    ("price-efficient", ("low", "medium", "high")),
    ("best-score", ("high", "medium", "low")),
])
def test_configurable_rankers_filter_before_ranking(strategy, expected):
    candidates = [ranked_model("low", 70, "low"), ranked_model("high", 99, "high"),
                  ranked_model("default", 80, ""), ranked_model("medium", 75, "medium"),
                  ranked_model("below", 69.99, "low"), ranked_model("denied", 100, "low")]
    policy = ModelPolicy(application_id="uta", ranking_strategy=strategy, denylist=("pool/denied",))
    result = resolve_selection(reversed(candidates), policy)
    assert result.model_ids == tuple("pool/" + name for name in expected)
    assert next(d.reason for d in result.decisions if d.identity == "pool/default") == "prohibited_effort"


def test_default_ranker_is_efficient_and_ties_are_stable():
    policy = ModelPolicy(application_id="uta")
    assert policy.ranking_strategy == "price-efficient"
    candidates = [ranked_model("z", 75, "medium"), ranked_model("a", 75, ""),
                  ranked_model("low", 70, "low"), ranked_model("unknown", None, "low")]
    policy = policy.model_copy(update={"unscored_approvals": ("pool/unknown",)})
    for ordered in (candidates, list(reversed(candidates))):
        assert resolve_selection(ordered, policy).model_ids == ("pool/low", "pool/z", "pool/unknown")


def test_invalid_ranking_strategy_rejected():
    with pytest.raises(ValueError):
        ModelPolicy(application_id="uta", ranking_strategy="fastest")


def test_efficient_effort_order_and_unrounded_scores():
    efforts = ["none", "minimal", "low", "medium", "high", "other"]
    candidates = [ranked_model(effort, 75, effort) for effort in reversed(efforts)]
    assert resolve_selection(candidates, ModelPolicy(application_id="uta")).model_ids == tuple(
        "pool/" + effort for effort in efforts)
    candidates = [ranked_model("a", 75.001, "low"), ranked_model("z", 75.002, "low")]
    assert resolve_selection(candidates, ModelPolicy(application_id="uta")).model_ids == ("pool/z", "pool/a")


@pytest.mark.parametrize("strategy,expected", [
    ("price-efficient", [("pool/astra", "low", 75.7), ("pool/kimi", "low", 72.0),
                        ("pool/sol", "medium", 76.3)]),
    ("best-score", [("pool/sol", "high", 77.2), ("pool/astra", "high", 77.1),
                    ("pool/kimi", "low", 72.0)]),
])
def test_ranker_selects_best_eligible_variant_before_deduplicating_model(strategy, expected):
    candidates = [ranked_model("astra", 77.1, "high"), ranked_model("astra", 75.7, "low"),
                  ranked_model("kimi", 72.0, "low"), ranked_model("sol", 69.7, "low"),
                  ranked_model("sol", 76.3, "medium"), ranked_model("sol", 77.2, "high")]
    policy = ModelPolicy(application_id="uta", ranking_strategy=strategy)
    for rows in (candidates, list(reversed(candidates))):
        result = resolve_selection(rows, policy)
        assert [(c.identity, c.effort, c.score) for c in result.candidates] == expected
        assert len(result.decisions) == 3


def test_unsuffixed_default_effort_is_prohibited_with_max_and_xhigh():
    candidates = [
        ranked_model("qwen", 73.1, ""),
        ranked_model("gpt55", 71.6, "high"),
        ranked_model("sol", 76.3, "medium"),
        ranked_model("kimi", 72.0, "low"),
        ranked_model("explicit-max", 74.0, "max"),
        ranked_model("xhigh", 75.0, "xhigh"),
    ]
    policy = ModelPolicy(application_id="uta")
    result = resolve_selection(reversed(candidates), policy)
    assert [(c.identity, c.effort) for c in result.candidates] == [
        ("pool/kimi", "low"),
        ("pool/sol", "medium"),
        ("pool/gpt55", "high"),
    ]
    prohibited = {d.identity: d.reason for d in result.decisions if not d.eligible}
    assert prohibited == {
        "pool/qwen": "prohibited_effort",
        "pool/explicit-max": "prohibited_effort",
        "pool/xhigh": "prohibited_effort",
    }


def priced_model(name, score, effort, price):
    return ranked_model(name, score, effort).model_copy(
        update={"list_price": price, "price": price})


def test_price_ranks_before_effort_and_equal_prices_keep_efficiency_order():
    candidates = [priced_model("pricey-low", 99, "low", 12.0),
                  priced_model("cheap-high", 71, "high", 3.0),
                  priced_model("tie-medium", 75, "medium", 3.0),
                  priced_model("tie-low", 72, "low", 3.0)]
    policy = ModelPolicy(application_id="uta")
    for ordered in (candidates, list(reversed(candidates))):
        assert resolve_selection(ordered, policy).model_ids == (
            "pool/tie-low", "pool/tie-medium", "pool/cheap-high", "pool/pricey-low")


def test_free_provider_falls_back_to_efficiency_and_score():
    candidates = [priced_model("astra-high", 99, "high", 0.0),
                  priced_model("astra-low", 75, "low", 0.0),
                  priced_model("kimi-low", 72, "low", 0.0)]
    assert resolve_selection(candidates, ModelPolicy(application_id="uta")).model_ids == (
        "pool/astra-low", "pool/kimi-low", "pool/astra-high")


def test_unknown_price_is_never_ranked_as_free():
    candidates = [priced_model("known", 71, "high", 9.0), priced_model("unknown", 99, "low", None)]
    policy = ModelPolicy(application_id="uta")
    assert resolve_selection(candidates, policy).model_ids == ("pool/known", "pool/unknown")
    assert resolve_selection(candidates, policy.model_copy(
        update={"ranking_strategy": "best-score"})).model_ids == ("pool/unknown", "pool/known")


def test_best_score_ignores_price_entirely():
    candidates = [priced_model("cheap", 71, "low", 0.0), priced_model("costly", 99, "high", 90.0)]
    policy = ModelPolicy(application_id="uta", ranking_strategy="best-score")
    assert resolve_selection(candidates, policy).model_ids == ("pool/costly", "pool/cheap")


def test_price_chooses_between_variants_of_one_model_before_deduplicating():
    # Both variants of a model share its price, so effort still decides there.
    candidates = [priced_model("a", 99, "high", 1.0), priced_model("a", 71, "low", 1.0),
                  priced_model("b", 99, "low", 2.0)]
    result = resolve_selection(candidates, ModelPolicy(application_id="uta"))
    assert [(c.identity, c.effort) for c in result.candidates] == [("pool/a", "low"), ("pool/b", "low")]


def test_legacy_strategy_name_still_selects_the_price_ranker():
    policy = ModelPolicy(application_id="uta", ranking_strategy="best-efficient")
    assert policy.ranking_strategy == "price-efficient"
