
import json

import pytest
from test_model_configuration import write_config

from agent_core.model_selection.cache import CatalogCache
from agent_core.model_selection.configuration import load_selection_config
from agent_core.model_selection.runtime import DiscoveryRuntime, NoAvailableModels
from agent_core.model_selection.sources import parse_benchmarks, parse_pricing


def runtime(tmp_path, environ=None):
    path = write_config(tmp_path, policy={"application_id": "uta"}, bindings={
        "pool/a": {"benchmark_id": "a", "effort": "high", "variant": "high", "capability_approved": True},
        "pool/b": {"benchmark_id": "b", "effort": "high", "variant": "high", "capability_approved": True},
    }, variant_options={"pool/a": {"reasoningEffort": "high"},
                        "pool/b": {"reasoningEffort": "high"}})
    config = load_selection_config(path, environ={})
    records = parse_benchmarks({"data": [
        {"id": name, "name": f"{name} (high)", "slug": name,
         "evaluations": {"artificial_analysis_coding_index": score}}
        for name, score in [("a", 80), ("b", 75)]]})
    CatalogCache(config).publish(inventory={"pool": ["a", "b"]},
                                 benchmarks={"records": [r.model_dump() for r in records]})
    return DiscoveryRuntime(config, environ=environ or {"POOL_KEY": "synthetic"})


def test_resolve_fresh_and_persist_health_by_identity(tmp_path):
    r = runtime(tmp_path)
    assert r.resolve().model_ids == ("pool/a", "pool/b")
    r.mark_unhealthy("pool/a", reason="rate_limit")
    r2 = DiscoveryRuntime(r.config, environ={"POOL_KEY": "synthetic"})
    assert r2.resolve().model_ids == ("pool/b",)
    r2.mark_unhealthy("pool/b", reason="rate_limit")
    with pytest.raises(NoAvailableModels):
        r2.resolve()


@pytest.mark.parametrize("strategy", ["price-efficient", "best-score"])
def test_provider_order_precedes_score_effort_and_alphabetical_identity(tmp_path, strategy):
    r = runtime(tmp_path)
    catalog = CatalogCache(r.config).load()
    primary = r.config.providers[0].model_copy(update={"id": "z-primary"})
    backup = r.config.providers[0].model_copy(update={"id": "a-backup"})
    bindings = {
        "z-primary/b": r.config.bindings["pool/b"],
        "a-backup/a": {**r.config.bindings["pool/a"], "effort": "low", "variant": "low"},
    }
    records = catalog["benchmarks"]["records"]
    next(record for record in records if record["benchmark_id"] == "a")["effort"] = "low"
    config = r.config.model_copy(update={
        "providers": (primary, backup), "bindings": bindings,
        "variant_options": {"z-primary/b": {"reasoningEffort": "high"},
                            "a-backup/a": {"reasoningEffort": "low"}},
        "policy": r.config.policy.model_copy(update={"ranking_strategy": strategy}),
    })
    CatalogCache(config).publish(inventory={"a-backup": ["a"], "z-primary": ["b"]},
                                 benchmarks={"records": records})
    r = DiscoveryRuntime(config, environ={"POOL_KEY": "synthetic"})
    assert r.resolve().model_ids == ("z-primary/b", "a-backup/a")
    assert r.config_updates(r.resolve(), "z-primary/b")["opencode_provider_chain"] == (
        "z-primary:z-primary/b;a-backup:a-backup/a")
    r.mark_unhealthy("z-primary/b", reason="provider_auth_failed")
    assert r.resolve().model_ids == ("a-backup/a",)


@pytest.mark.parametrize("strategy", ["price-efficient", "best-score"])
def test_ranking_reaches_provider_chain_without_changing_effort(tmp_path, strategy):
    r = runtime(tmp_path)
    catalog = CatalogCache(r.config).load()
    records = catalog["benchmarks"]["records"]
    for record in records:
        if record["benchmark_id"] == "b":
            record["effort"] = ""
    bindings = dict(r.config.bindings)
    bindings["pool/b"] = dict(bindings["pool/b"], effort="", variant="")
    config = r.config.model_copy(update={
        "bindings": bindings, "variant_options": {"pool/a": {"reasoningEffort": "high"}},
        "policy": r.config.policy.model_copy(update={"ranking_strategy": strategy}),
    })
    CatalogCache(config).publish(inventory=catalog["inventory"], benchmarks={"records": records})
    r = DiscoveryRuntime(config, environ={"POOL_KEY": "synthetic"})
    selected = r.resolve()
    assert selected.model_ids == ("pool/a",)
    updates = r.config_updates(selected, "pool/a")
    assert updates["opencode_provider_chain"] == "pool:pool/a"
    assert "pool/b" not in updates["opencode_discovery_variants"]
    r.mark_unhealthy("pool/a", reason="rate_limit")
    with pytest.raises(NoAvailableModels):
        r.resolve()


@pytest.mark.parametrize("strategy,effort,score", [
    ("price-efficient", "low", 75.7), ("best-score", "high", 80.0),
])
@pytest.mark.parametrize("low_problem", [None, "below_threshold", "score_unknown", "unapproved", "missing_options", "wrong_options", "missing_benchmark"])
def test_multiple_bindings_select_and_wire_exact_effort(tmp_path, strategy, effort, score, low_problem):
    r = runtime(tmp_path)
    catalog = CatalogCache(r.config).load()
    records = catalog["benchmarks"]["records"]
    records.append(dict(records[0], benchmark_id="a-low", effort="low", score=75.7))
    bindings = dict(r.config.bindings)
    bindings["pool/a"] = [bindings["pool/a"], {
        "benchmark_id": "a-low", "effort": "low", "variant": "quick", "capability_approved": True}]
    options = {"pool/a": {"high": {"reasoningEffort": "high"}, "quick": {"reasoningEffort": "low"}},
               "pool/b": {"reasoningEffort": "high"}}
    if low_problem == "below_threshold":
        records[-1]["score"] = 69.9
    elif low_problem == "score_unknown":
        records[-1]["score"] = None
    elif low_problem == "unapproved":
        bindings["pool/a"][1]["capability_approved"] = False
    elif low_problem == "missing_options":
        del options["pool/a"]["quick"]
    elif low_problem == "wrong_options":
        options["pool/a"]["quick"]["reasoningEffort"] = "high"
    elif low_problem == "missing_benchmark":
        bindings["pool/a"][1]["benchmark_id"] = "absent"
    if low_problem:
        effort, score = "high", 80.0
    path = write_config(tmp_path, policy={"application_id": "uta", "ranking_strategy": strategy},
                        bindings=bindings, variant_options=options)
    config = load_selection_config(path, environ={})
    CatalogCache(config).publish(inventory=catalog["inventory"], benchmarks={"records": records})
    r = DiscoveryRuntime(config, environ={"POOL_KEY": "synthetic"})
    selection = r.resolve()
    chosen = selection.candidates[0]
    assert (chosen.identity, chosen.effort, chosen.score) == ("pool/a", effort, score)
    updates = r.config_updates(selection, chosen.identity)
    assert updates["opencode_discovery_variants"][chosen.identity] == {
        chosen.variant: {"reasoningEffort": effort}}
    assert selection.model_ids.count("pool/a") == 1


def test_config_overrides_model_effort_and_never_uses_legacy_chain(tmp_path):
    r = runtime(tmp_path)
    result = r.resolve()
    updates = r.config_updates(result, "pool/b")
    assert updates["opencode_model"] == "pool/b"
    assert updates["opencode_variant"] == "high"
    assert updates["opencode_selection_mode"] == "discovery"
    assert updates["opencode_discovery_variants"]["pool/b"] == {"high": {"reasoningEffort": "high"}}
    assert "synthetic" not in repr(result)
    with pytest.raises(ValueError):
        r.config_updates(result, "other/excluded")


def test_execution_config_is_exact_and_contains_only_resolved_variants(tmp_path):
    r = runtime(tmp_path)
    result = r.resolve()
    assert r.config_updates(result, "pool/b") == {
        "opencode_selection_mode": "discovery",
        "opencode_model": "pool/b", "opencode_small_model": "pool/b",
        "opencode_provider": "pool", "opencode_variant": "high",
        "opencode_discovery_variants": {
            "pool/a": {"high": {"reasoningEffort": "high"}},
            "pool/b": {"high": {"reasoningEffort": "high"}},
        },
        "opencode_provider_chain": "pool:pool/a,pool/b",
        "opencode_provider_tokens": "pool.token={env:POOL_KEY}",
        "opencode_credential_env": {"POOL_KEY": "synthetic"},
        "opencode_provider_base_urls": "pool.base_url=https://provider.example/v1",
    }


@pytest.mark.parametrize("model", ["a", "other/a"])
def test_health_rejects_models_outside_provider_scope(tmp_path, model):
    r = runtime(tmp_path)
    with pytest.raises(ValueError, match="outside configured provider scopes"):
        r.is_healthy(model)


def test_scope_preserves_endpoint_and_credential_identity(tmp_path):
    from agent_core.model_selection.runtime import credential_scope

    r = runtime(tmp_path)
    scope = credential_scope(r.config.providers[0])
    assert scope.provider_id == "pool"
    assert scope.normalized_endpoint == "https://provider.example/v1"
    assert scope.credential_scope_id == "team"
    assert scope.credential_generation == "1"


def test_unavailable_explanation_replaces_admission_without_duplicates(tmp_path):
    r = runtime(tmp_path)
    r.mark_unhealthy("pool/a", reason="rate_limit")
    result = r.resolve(require_available=False)
    assert result.model_ids == ("pool/b",)
    assert [(d.identity, d.eligible, d.reason) for d in result.decisions if d.identity == "pool/a"] == [
        ("pool/a", False, "model_unavailable")]
    assert [d.identity for d in result.decisions] == ["pool/a", "pool/b"]
    r.mark_unhealthy("pool/b", reason="rate_limit")
    assert r.resolve(require_available=False).model_ids == ()
    with pytest.raises(NoAvailableModels, match="^no_available_models: model_unavailable$"):
        r.resolve()


@pytest.mark.parametrize("options,reason", [({}, "variant_mapping_required"),
    ({"pool/a": {"reasoningEffort": "low"}, "pool/b": {"reasoningEffort": "low"}},
     "variant_effort_mismatch")])
def test_variant_exclusion_evidence(tmp_path, options, reason):
    r = runtime(tmp_path)
    r = DiscoveryRuntime(r.config.model_copy(update={"variant_options": options}), environ={"POOL_KEY": "synthetic"})
    result = r.resolve(require_available=False)
    assert result.model_ids == ()
    assert [(d.identity, d.eligible, d.reason) for d in result.decisions] == [
        ("pool/a", False, reason), ("pool/b", False, reason)]


def test_benchmark_auth_marker_blocks_before_catalog_load(tmp_path):
    from agent_core.model_selection.cache import CatalogUnavailable

    r = runtime(tmp_path)
    (r.config.cache_root / "benchmark-auth-invalid.json").write_text("{}")
    CatalogCache(r.config).path.unlink()
    with pytest.raises(CatalogUnavailable, match="^benchmark_auth_failed; authenticated refresh required$"):
        r.resolve()


def test_missing_provider_key_fails_before_execution(tmp_path):
    r = runtime(tmp_path)
    r = DiscoveryRuntime(r.config, environ={})
    with pytest.raises(NoAvailableModels, match="credential"):
        r.resolve()


def test_catalog_refresh_changes_next_resolution(tmp_path):
    r = runtime(tmp_path)
    old = r.resolve()
    data = CatalogCache(r.config).load()
    data["benchmarks"]["records"][1]["score"] = 90
    CatalogCache(r.config).publish(inventory=data["inventory"], benchmarks=data["benchmarks"])
    assert old.model_ids == ("pool/a", "pool/b")
    assert r.resolve().model_ids == ("pool/b", "pool/a")


def test_missing_variant_mapping_fails_before_invocation(tmp_path):
    r = runtime(tmp_path)
    config = r.config.model_copy(update={"variant_options": {}})
    r = DiscoveryRuntime(config, environ={"POOL_KEY": "synthetic"})
    with pytest.raises(NoAvailableModels, match="variant"):
        r.resolve()


def test_conflicting_effort_cannot_claim_higher_benchmark_score(tmp_path):
    r = runtime(tmp_path)
    config = r.config.model_copy(update={"variant_options": {"pool/a": {"reasoningEffort": "low"},
                                                            "pool/b": {"reasoningEffort": "low"}}})
    with pytest.raises(NoAvailableModels, match="variant"):
        DiscoveryRuntime(config, environ={"POOL_KEY": "synthetic"}).resolve()


@pytest.mark.parametrize("started_at,healthy", [(99.0, False), (100.0, False), (101.0, True)])
def test_runtime_success_uses_attempt_observation_not_completion_time(tmp_path, monkeypatch, started_at, healthy):
    r = runtime(tmp_path)
    other = DiscoveryRuntime(r.config, environ={"POOL_KEY": "synthetic"})
    now = [100.0]
    monkeypatch.setattr(r.store, "_clock", lambda: now[0])
    monkeypatch.setattr(other.store, "_clock", lambda: now[0])
    other.mark_unhealthy("pool/a", reason="rate_limit")
    now[0] = 110.0
    r.mark_success("pool/a", observed_at=started_at)
    assert other.is_healthy("pool/a") is healthy


@pytest.mark.parametrize("entrypoint", ["process", "session"])
def test_inflight_success_cannot_clear_another_runtime_newer_failure(tmp_path, monkeypatch, entrypoint):
    from agent_core.harness.cost import PaidAttempts
    from agent_core.harness.process import TurnResult
    from agent_core.harness.runner import run_turn_with_fallback
    from agent_core.harness.sessions import FallbackHarnessSession, SessionSnapshot

    r = runtime(tmp_path)
    other = DiscoveryRuntime(r.config, environ={"POOL_KEY": "synthetic"})
    now = [100.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    for store in (r.store, other.store):
        monkeypatch.setattr(store, "_clock", lambda: now[0])

    class Inflight:
        session_id = "test-session"

        def run_turn(self, message=None, **kwargs):
            now[0] = 101.0
            other.mark_unhealthy("pool/a", reason="rate_limit")
            now[0] = 102.0
            return TurnResult(type="completed", cost_usd=0.25)

        def snapshot(self):
            return SessionSnapshot(session_id=self.session_id)

        def close(self):
            pass

    ledger = PaidAttempts()
    if entrypoint == "process":
        result = run_turn_with_fallback(
            Inflight(), repo_path=str(tmp_path), message="work", models=("pool/a",),
            discovery_policy=r, paid_attempts=ledger,
        )
    else:
        session = FallbackHarnessSession(lambda _: Inflight(), models=("pool/a",), discovery_policy=r)
        result = session.run_turn(paid_attempts=ledger)
        session.close()
    assert result.type == "completed"
    assert ledger.count == 1
    assert ledger.aggregate().provider_cost_usd == 0.25
    assert not other.is_healthy("pool/a")


def _pool_config(tmp_path, discounts=(("token-pool", 0), ("openai", 1))):
    """Two endpoints for the same models: an internal free pool, then OpenAI."""
    providers = [{"id": name, "base_url": f"https://{name}.example/v1",
                  "credential_scope_id": name, "credential_generation": "1",
                  "api_key_env": "POOL_KEY", "pricing_discount": discount}
                 for name, discount in discounts]
    bindings, options = {}, {}
    for provider, _ in discounts:
        bindings[f"{provider}/gpt-6-astra"] = [
            {"benchmark_id": "astra-low", "effort": "low", "variant": "low",
             "capability_approved": True},
            {"benchmark_id": "astra-high", "effort": "high", "variant": "high",
             "capability_approved": True}]
        bindings[f"{provider}/gpt-5.5"] = {"benchmark_id": "gpt55-low", "effort": "low",
                                           "variant": "low", "capability_approved": True}
        options[f"{provider}/gpt-6-astra"] = {"low": {"reasoningEffort": "low"},
                                              "high": {"reasoningEffort": "high"}}
        options[f"{provider}/gpt-5.5"] = {"reasoningEffort": "low"}
    path = write_config(tmp_path, providers=providers, bindings=bindings, variant_options=options,
                        policy={"application_id": "uta"})
    config = load_selection_config(path, environ={})
    records = parse_benchmarks({"data": [
        {"id": "astra-low", "name": "GPT-6 Astra (low)", "slug": "gpt-6-astra-low",
         "evaluations": {"artificial_analysis_coding_index": 84}},
        {"id": "astra-high", "name": "GPT-6 Astra (high)", "slug": "gpt-6-astra-high",
         "evaluations": {"artificial_analysis_coding_index": 90}},
        {"id": "gpt55-low", "name": "GPT-5.5 (low)", "slug": "gpt-5.5-low",
         "evaluations": {"artificial_analysis_coding_index": 76}}]})
    # Astra: 5 in / 15 out, GPT-5.5: 0.5 in / 2.5 out (USD per 1M tokens).
    pricing = parse_pricing({"data": [
        {"id": "openai/gpt-6-astra", "pricing": {"prompt": "0.000005", "completion": "0.000015"}},
        {"id": "openai/gpt-5.5", "pricing": {"prompt": "0.0000005", "completion": "0.0000025"}}]})
    inventory = {provider: ["gpt-5.5", "gpt-6-astra"] for provider, _ in discounts}
    CatalogCache(config).publish(
        inventory=inventory, benchmarks={"records": [r.model_dump() for r in records]},
        pricing={"records": [r.model_dump() for r in pricing]})
    return DiscoveryRuntime(config, environ={"POOL_KEY": "synthetic"})


def test_free_pool_ranks_by_efficiency_while_paid_provider_ranks_by_price(tmp_path):
    selection = _pool_config(tmp_path).resolve()
    ranked = [(c.identity, c.effort, c.price) for c in selection.candidates]
    # The zero-discount pool costs nothing, so the most efficient model leads.
    assert ranked[:2] == [("token-pool/gpt-6-astra", "low", 0.0),
                          ("token-pool/gpt-5.5", "low", 0.0)]
    # Full-price OpenAI leads with the cheapest model instead, not Astra.
    assert ranked[2:] == [("openai/gpt-5.5", "low", 7.5),
                          ("openai/gpt-6-astra", "low", 65.0)]


def test_discount_changes_only_the_paid_price_not_the_published_one(tmp_path):
    selection = _pool_config(tmp_path, discounts=(("openai", 0.25),)).resolve()
    assert [(c.identity, c.list_price, c.price) for c in selection.candidates] == [
        ("openai/gpt-5.5", 7.5, 1.875), ("openai/gpt-6-astra", 65.0, 16.25)]


def test_default_discount_is_full_price(tmp_path):
    r = _pool_config(tmp_path)
    assert [p.pricing_discount for p in r.config.providers] == [0, 1]
    config = json.loads((tmp_path / "selection.json").read_text())
    for provider in config["providers"]:
        del provider["pricing_discount"]
    (tmp_path / "selection.json").write_text(json.dumps(config))
    reloaded = load_selection_config(tmp_path / "selection.json", environ={})
    assert [p.pricing_discount for p in reloaded.providers] == [1.0, 1.0]
