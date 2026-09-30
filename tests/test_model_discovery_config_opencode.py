"""Discovery contracts for harness.config and harness.opencode composition."""

import json
from pathlib import Path

import pytest

from agent_core.config import HarnessConfig, current_config, use_config
from agent_core.harness.config import build_opencode_config_dict
from agent_core.harness.cost import PaidAttempts
from agent_core.harness.lifecycle import WorkspaceBootstrapRequest
from agent_core.harness.opencode import create_opencode_harness
from agent_core.harness.process import TurnResult
from agent_core.harness.registry import HarnessSpec
from agent_core.model_selection.cache import CatalogCache
from agent_core.model_selection.configuration import load_selection_config
from agent_core.model_selection.runtime import DiscoveryRuntime, NoAvailableModels
from agent_core.model_selection.sources import parse_benchmarks


VARIANTS = {
    "pool/a": {"careful": {"reasoningEffort": "high"}},
    "pool/b": {"quick": {"reasoningEffort": "low"}},
}


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    def no_live_process(*args, **kwargs):
        pytest.fail("integration tests must use a recording process")

    monkeypatch.setattr("agent_core.harness.process.OpenCodeProcess.run_turn", no_live_process)
    monkeypatch.setenv("POOL_KEY", "host-synthetic-key")
    monkeypatch.delenv("AGENT_MODEL_CODING_INDEX_MIN", raising=False)
    path = tmp_path / "trusted.json"
    path.write_text(json.dumps({
        "cache_root": str(tmp_path / "catalog"),
        "availability_db": str(tmp_path / "health.db"),
        "providers": [{"id": "pool", "base_url": "https://provider.example/v1",
                       "credential_scope_id": "team", "credential_generation": "1",
                       "api_key_env": "POOL_KEY"}],
        "policy": {"application_id": "test", "ranking_strategy": "best-score"},
        "variant_options": {"pool/a": {"reasoningEffort": "high"},
                            "pool/b": {"reasoningEffort": "low"}},
        "bindings": {
            "pool/a": {"benchmark_id": "a", "effort": "high", "variant": "careful",
                       "capability_approved": True},
            "pool/b": {"benchmark_id": "b", "effort": "low", "variant": "quick",
                       "capability_approved": True},
        },
    }))
    config = load_selection_config(path, environ={})
    records = parse_benchmarks({"data": [
        {"id": name, "name": f"{name} ({effort})", "slug": name,
         "evaluations": {"artificial_analysis_coding_index": score}}
        for name, effort, score in [("a", "high", 90), ("b", "low", 80)]
    ]})
    CatalogCache(config).publish(
        inventory={"pool": ["a", "b"]},
        benchmarks={"records": [record.model_dump() for record in records]},
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "prompt.md").write_text("do the work")
    return path, config, repo


def harness(discovery, **options):
    path, _, _ = discovery
    return create_opencode_harness(HarnessSpec(name="opencode", options={
        "model_selection_config": str(path),
        **options,
    }))


def test_discovery_rejects_silently_dropped_fallback_provider(discovery):
    with pytest.raises(ValueError, match="discovery omits configured fallback providers"):
        harness(discovery, opencode_provider_chain="pool:a;openai:gpt-example;deepseek:example")


def test_discovery_allows_same_provider_with_different_admitted_models(discovery):
    agent = harness(discovery, opencode_provider_chain="pool:unapproved")
    assert agent.preferred_model() == "pool/a"


@pytest.mark.parametrize("entrypoint", ["process", "session"])
def test_provider_auth_quarantine_falls_back_across_providers(discovery, monkeypatch, entrypoint):
    path, old_config, repo = discovery
    catalog = CatalogCache(old_config).load()
    data = json.loads(path.read_text())
    data["providers"].append({**data["providers"][0], "id": "backup",
        "base_url": "https://backup.example/v1", "api_key_env": "BACKUP_KEY"})
    data["bindings"]["backup/b"] = data["bindings"].pop("pool/b")
    data["variant_options"]["backup/b"] = data["variant_options"].pop("pool/b")
    path.write_text(json.dumps(data))
    config = load_selection_config(path, environ={})
    CatalogCache(config).publish(inventory={"pool": ["a"], "backup": ["b"]},
                                benchmarks=catalog["benchmarks"])
    monkeypatch.setenv("BACKUP_KEY", "backup-synthetic-key")
    agent = harness(discovery, opencode_provider_chain="pool:a;backup:b")
    process = RecordingProcess(TurnResult(type="error", fallback_eligible=True,
                                         fallback_reason="provider_auth_failed"))
    agent.process = process
    monkeypatch.setattr("agent_core.harness.client.OpenCodeProcess", lambda: process)
    if entrypoint == "process":
        result = agent.run_turn(repo_path=repo, message="work")
    else:
        session = agent.open_session(repo_path=repo)
        result = session.run_turn(repo_path=repo, prompt_file=repo / "prompt.md")
        session.close()
    assert result.type == "completed"
    assert [c["model_id"] for c in process.calls] == ["pool/a", "backup/b"]
    assert process.calls[-1]["config"]["provider"]["backup"]["options"] == {
        "baseURL": "https://backup.example/v1", "apiKey": "{env:BACKUP_KEY}"}
    assert agent.preferred_model(repo) == "backup/b"


@pytest.mark.parametrize("entrypoint", ["process", "session"])
@pytest.mark.parametrize("alternative", [False, True])
def test_default_efficient_ranking_reaches_both_opencode_entrypoints(discovery, monkeypatch, entrypoint, alternative):
    path, _, repo = discovery
    config = json.loads(path.read_text())
    del config["policy"]["ranking_strategy"]
    if alternative:
        config["bindings"]["pool/a"] = [config["bindings"]["pool/a"], {
            "benchmark_id": "a-low", "effort": "low", "variant": "quick", "capability_approved": True}]
        config["variant_options"]["pool/a"] = {
            "careful": {"reasoningEffort": "high"}, "quick": {"reasoningEffort": "low"}}
    path.write_text(json.dumps(config))
    if alternative:
        loaded = load_selection_config(path, environ={})
        catalog = CatalogCache(loaded).load()
        records = catalog["benchmarks"]["records"]
        records.append(dict(records[0], benchmark_id="a-low", effort="low", score=85.0))
        CatalogCache(loaded).publish(inventory=catalog["inventory"], benchmarks={"records": records})
    agent = harness(discovery)
    process = RecordingProcess()
    agent.process = process
    monkeypatch.setattr("agent_core.harness.client.OpenCodeProcess", lambda: process)
    if entrypoint == "process":
        result = agent.run_turn(repo_path=repo, message="work")
    else:
        session = agent.open_session(repo_path=repo)
        result = session.run_turn(repo_path=repo, prompt_file=repo / "prompt.md")
        session.close()
    assert result.type == "completed"
    expected = "pool/a" if alternative else "pool/b"
    assert [call["model_id"] for call in process.calls] == [expected]
    assert process.calls[0]["config"]["model"] == expected
    assert process.calls[0]["variant"] == "quick"


class RecordingProcess:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def run_turn(self, message=None, **kwargs):
        config_path = Path((kwargs.get("env") or {}).get("OPENCODE_CONFIG")
                           or Path(kwargs["repo_path"]) / "opencode.json")
        self.calls.append({**kwargs, "message": message,
                           "config": json.loads(config_path.read_text()),
                           "scoped_model": current_config().opencode_model})
        return self.results.pop(0) if self.results else TurnResult(type="completed", cost_usd=0.25)


def fail():
    return TurnResult(type="error", fallback_eligible=True, fallback_reason="rate_limit", cost_usd=0.1)


def forbid_legacy(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("discovery used legacy health/inventory resolution")

    for module, names in {
        "config": ("available_provider_candidates", "provider_candidates", "is_model_healthy"),
        "opencode": ("available_provider_candidates",),
        "runner": ("available_provider_candidates", "provider_candidates", "is_model_healthy",
                   "is_model_permanently_unhealthy", "last_successful_model"),
    }.items():
        for name in names:
            monkeypatch.setattr(f"agent_core.harness.{module}.{name}", forbidden)


def test_config_builder_bypasses_legacy_and_registers_only_resolved_variants(monkeypatch, tmp_path):
    forbid_legacy(monkeypatch)
    config = HarnessConfig(_env_file=None, opencode_selection_mode="discovery",
                           opencode_model="pool/b", opencode_variant="quick",
                           opencode_provider_chain="pool:pool/a,pool/b",
                           opencode_provider_base_urls="pool.base_url=https://provider.example/v1",
                           opencode_provider_tokens="pool.token=synthetic-key",
                           opencode_discovery_variants=VARIANTS)
    with use_config(config):
        result = build_opencode_config_dict(str(tmp_path))
    assert result["model"] == result["small_model"] == "pool/b"
    assert result["$schema"] == "https://opencode.ai/config.json"
    assert isinstance(result["permission"], dict)
    assert result["provider"]["pool"]["name"] == "pool"
    assert set(result["provider"]) == {"pool"}
    assert set(result["provider"]["pool"]["models"]) == {"a", "b"}
    assert result["provider"]["pool"]["models"]["a"]["variants"] == VARIANTS["pool/a"]


@pytest.mark.parametrize("chain,model,variant", [
    ("", "pool/a", "careful"), ("pool:pool/a", "outside/model", "careful"),
    ("pool:pool/a", "pool/a", "unknown"),
])
def test_discovery_config_fails_closed(chain, model, variant, tmp_path):
    with use_config(HarnessConfig(_env_file=None, opencode_selection_mode="discovery",
                                  opencode_provider_chain=chain, opencode_model=model,
                                  opencode_variant=variant, opencode_discovery_variants=VARIANTS)):
        with pytest.raises(ValueError, match=("discovery variant requires explicit trusted options"
                                            if variant == "unknown" else "discovery model is outside the resolved chain")):
            build_opencode_config_dict(str(tmp_path))


def test_process_fallback_uses_exact_effort_private_config_and_shared_health(discovery, monkeypatch):
    forbid_legacy(monkeypatch)
    _, config, repo = discovery
    original = current_config()
    agent = harness(discovery)
    process = RecordingProcess(fail())
    agent.process = process
    ledger = PaidAttempts()
    result = agent.run_turn(repo_path=repo, message="work", model_id="legacy/unapproved",
                            paid_attempts=ledger)
    assert result.type == "completed"
    assert [call["model_id"] for call in process.calls] == ["pool/a", "pool/b"]
    assert [call["variant"] for call in process.calls] == ["careful", "quick"]
    assert [call["config"]["model"] for call in process.calls] == ["pool/a", "pool/b"]
    assert [call["scoped_model"] for call in process.calls] == ["pool/a", "pool/b"]
    assert len({call["repo_path"] for call in process.calls}) == 2
    assert not (repo / "opencode.json").exists()
    assert all(not Path(call["repo_path"]).exists() for call in process.calls)
    assert ledger.count == 2
    assert ledger.aggregate().provider_cost_usd == pytest.approx(0.35)
    assert current_config() is original
    assert not DiscoveryRuntime(config, environ={"POOL_KEY": "host-synthetic-key"}).is_healthy("pool/a")


def reorder(config):
    cache = CatalogCache(config)
    data = cache.load()
    data["benchmarks"]["records"][1]["score"] = 95
    cache.publish(inventory=data["inventory"], benchmarks=data["benchmarks"])


def test_new_calls_and_resume_resolve_again_without_historic_preference(discovery):
    _, config, repo = discovery
    agent = harness(discovery)
    agent.process = RecordingProcess()
    assert agent.preferred_model(repo) == "pool/a"
    agent.run_turn(repo_path=repo, message="first")
    reorder(config)
    assert agent.preferred_model(repo) == "pool/b"
    agent.run_turn(repo_path=repo, message="continue", model_id="pool/a",
                   session_id="historic-session", bootstrap_message="full context")
    assert [call["model_id"] for call in agent.process.calls] == ["pool/a", "pool/b"]
    assert agent.process.calls[1]["session_id"] is None
    assert agent.process.calls[1]["message"] == "full context"


@pytest.mark.parametrize("entrypoint", ["run_turn", "open_session", "preferred_model"])
def test_unavailable_fails_before_billing_or_client_creation(discovery, entrypoint):
    _, config, repo = discovery
    agent = harness(discovery)
    runtime = DiscoveryRuntime(config, environ={"POOL_KEY": "host-synthetic-key"})
    for model in ("pool/a", "pool/b"):
        runtime.mark_unhealthy(model, reason="rate_limit")
    ledger = PaidAttempts()
    agent.process = RecordingProcess()
    agent.session_client_factory = lambda *_: pytest.fail("opened unavailable provider")
    kwargs = {"repo_path": repo}
    if entrypoint == "run_turn":
        kwargs.update(message="work", paid_attempts=ledger)
    with pytest.raises(NoAvailableModels):
        getattr(agent, entrypoint)(**kwargs)
    assert not agent.process.calls
    assert ledger.count == 0


def test_missing_variant_options_rejected_before_any_submission(discovery):
    path, _, repo = discovery
    data = json.loads(path.read_text())
    data["variant_options"] = {}
    path.write_text(json.dumps(data))
    agent = harness(discovery)
    agent.process = RecordingProcess()
    ledger = PaidAttempts()
    with pytest.raises(ValueError, match="variant"):
        agent.run_turn(repo_path=repo, message="work", paid_attempts=ledger)
    assert ledger.count == 0
    assert not agent.process.calls


def test_factory_uses_captured_host_environment_not_target_dotenv(discovery, monkeypatch):
    _, _, repo = discovery
    (repo / ".env").write_text("POOL_KEY=target-key\nAGENT_MODEL_CODING_INDEX_MIN=99\n")
    monkeypatch.chdir(repo)
    agent = harness(discovery)
    monkeypatch.setenv("POOL_KEY", "later-key")
    monkeypatch.setenv("AGENT_MODEL_CODING_INDEX_MIN", "99")
    agent.process = RecordingProcess()
    agent.run_turn(repo_path=repo, message="work", env={"POOL_KEY": "turn-key"})
    assert agent.process.calls[0]["config"]["provider"]["pool"]["options"]["apiKey"] == "{env:POOL_KEY}"


@pytest.mark.parametrize("override", [None, "", "nan", "101"])
def test_invalid_raw_override_fails_at_factory(discovery, override):
    with pytest.raises(ValueError):
        harness(discovery, model_coding_index_min=override)


def test_explicit_threshold_and_absolute_config_required(discovery):
    agent = harness(discovery, model_coding_index_min="85")
    _, _, repo = discovery
    assert agent.preferred_model(repo) == "pool/a"
    with pytest.raises(ValueError, match="absolute"):
        harness(discovery, model_selection_config="relative.json")


@pytest.mark.parametrize("options", [{"model_coding_index_min": "70"}, {"opencode_selection_mode": "discovery"}])
def test_discovery_factory_requires_config_with_mode_or_threshold(options):
    with pytest.raises(ValueError, match="^discovery requires an absolute model_selection_config path$"):
        create_opencode_harness(HarnessSpec(name="opencode", options=options))


def test_prepare_workspace_uses_current_admitted_model(discovery):
    _, _, repo = discovery
    harness(discovery).prepare_workspace(repo_path=repo)
    generated = json.loads((repo / "opencode.json").read_text())
    assert generated["model"] == "pool/a"
    assert generated["provider"]["pool"]["models"]["a"]["variants"] == VARIANTS["pool/a"]


def test_session_actual_process_uses_bound_effort_and_ignores_caller_model(discovery, monkeypatch):
    from agent_core.harness.client import OpenCodeClient

    forbid_legacy(monkeypatch)
    _, config, repo = discovery
    sent_models = []
    original_send = OpenCodeClient.send_message
    def send(client, *args, **kwargs):
        sent_models.append(kwargs["model_id"])
        return original_send(client, *args, **kwargs)
    monkeypatch.setattr(OpenCodeClient, "send_message", send)
    process = RecordingProcess(fail())
    monkeypatch.setattr("agent_core.harness.client.OpenCodeProcess", lambda: process)
    agent = harness(discovery)
    session = agent.open_session(repo_path=repo, model_id="outside/model", variant="wrong")
    ledger = PaidAttempts()
    result = session.run_turn(repo_path=repo, prompt_file=repo / "prompt.md",
                              model_id="outside/model", paid_attempts=ledger)
    assert result.type == "completed"
    assert [call["model_id"] for call in process.calls] == ["pool/a", "pool/b"]
    assert sent_models == ["pool/a", "pool/b"]
    assert [call["variant"] for call in process.calls] == ["careful", "quick"]
    assert [call["config"]["model"] for call in process.calls] == ["pool/a", "pool/b"]
    assert ledger.count == 2
    assert not DiscoveryRuntime(config, environ={"POOL_KEY": "host-synthetic-key"}).is_healthy("pool/a")
    session.close()


def test_active_session_keeps_selection_but_new_session_refreshes(discovery, monkeypatch):
    _, config, repo = discovery
    process = RecordingProcess()
    monkeypatch.setattr("agent_core.harness.client.OpenCodeProcess", lambda: process)
    agent = harness(discovery)
    first = agent.open_session(repo_path=repo)
    first.run_turn(repo_path=repo, prompt_file=repo / "prompt.md")
    reorder(config)
    first.run_turn(repo_path=repo, prompt_file=repo / "prompt.md")
    second = agent.open_session(repo_path=repo)
    second.run_turn(repo_path=repo, prompt_file=repo / "prompt.md")
    assert [call["model_id"] for call in process.calls] == ["pool/a", "pool/a", "pool/b"]
    first.close()
    second.close()


@pytest.mark.parametrize("entrypoint", ["check_readiness", "bootstrap_workspace"])
def test_lifecycle_paid_calls_cannot_bypass_discovery(discovery, entrypoint):
    _, _, repo = discovery
    agent = harness(discovery)
    agent.process = RecordingProcess(TurnResult(type="completed", result="OK"))
    if entrypoint == "check_readiness":
        assert agent.check_readiness(repo_path=repo, timeout_seconds=10).ready
    else:
        assert agent.bootstrap_workspace(
            repo_path=repo, request=WorkspaceBootstrapRequest(purpose="initialize")
        ).completed
    assert [call["model_id"] for call in agent.process.calls] == ["pool/a"]
    assert agent.process.calls[0]["variant"] == "careful"


def test_empty_variant_options_are_not_a_registration(tmp_path):
    with use_config(HarnessConfig(_env_file=None, opencode_selection_mode="discovery",
                                  opencode_provider_chain="pool:pool/a", opencode_model="pool/a",
                                  opencode_variant="careful",
                                  opencode_discovery_variants={"pool/a": {"careful": {}}})):
        with pytest.raises(ValueError, match="variant"):
            build_opencode_config_dict(str(tmp_path))


def test_single_selected_session_still_reports_shared_failure(discovery, monkeypatch):
    _, config, repo = discovery
    process = RecordingProcess(fail())
    monkeypatch.setattr("agent_core.harness.client.OpenCodeProcess", lambda: process)
    agent = harness(discovery, model_coding_index_min="85")
    session = agent.open_session(repo_path=repo)
    ledger = PaidAttempts()
    result = session.run_turn(repo_path=repo, prompt_file=repo / "prompt.md", paid_attempts=ledger)
    assert result.type == "rate_limited"
    assert not result.fallback_eligible
    assert ledger.count == 1
    assert not DiscoveryRuntime(config, environ={"POOL_KEY": "host-synthetic-key"}).is_healthy("pool/a")
    session.close()


def test_two_application_thresholds_do_not_contaminate_each_other(discovery):
    _, _, repo = discovery
    high = harness(discovery, model_coding_index_min="85")
    low = harness(discovery, model_coding_index_min="70")
    high.process = RecordingProcess()
    low.process = RecordingProcess()
    for agent in (high, low, high):
        agent.run_turn(repo_path=repo, message="work")
    assert set(high.process.calls[0]["config"]["provider"]["pool"]["models"]) == {"a"}
    assert set(low.process.calls[0]["config"]["provider"]["pool"]["models"]) == {"a", "b"}
    assert set(high.process.calls[1]["config"]["provider"]["pool"]["models"]) == {"a"}


PROVIDER_IDS = ("google", "openrouter", "cursor", "ollama", "openai", "deepseek",
                "tencent", "token-pool", "custom")


@pytest.mark.parametrize("provider", PROVIDER_IDS)
def test_discovery_registration_uses_exact_scoped_endpoint_key_for_every_provider(provider, tmp_path):
    config = HarnessConfig(
        _env_file=None, opencode_selection_mode="discovery", opencode_model=f"{provider}/model",
        opencode_provider_chain=f"{provider}:{provider}/model",
        opencode_provider_base_urls=f"{provider}.base_url=https://scoped.example/tenant/v1",
        opencode_provider_tokens=f"{provider}.token=scoped-synthetic-key",
        openai_api_key="wrong-legacy-key", openai_base_url="https://wrong.example/v1",
        opencode_variant="careful",
        opencode_discovery_variants={f"{provider}/model": {"careful": {"reasoningEffort": "high"}}},
    )
    with use_config(config):
        result = build_opencode_config_dict(str(tmp_path))
    entry = result["provider"][provider]
    assert entry["npm"] == "@ai-sdk/openai-compatible"
    assert entry["options"] == {"baseURL": "https://scoped.example/tenant/v1",
                                "apiKey": "scoped-synthetic-key"}
    assert entry["models"]["model"]["variants"] == {"careful": {"reasoningEffort": "high"}}
    assert "plugin" not in result
    assert "wrong-legacy-key" not in json.dumps(result)


@pytest.mark.parametrize("provider", PROVIDER_IDS)
@pytest.mark.parametrize("missing", ["endpoint", "key"])
def test_discovery_registration_never_uses_legacy_endpoint_or_key(provider, missing, tmp_path):
    config = HarnessConfig(
        _env_file=None, opencode_selection_mode="discovery", opencode_model=f"{provider}/model",
        opencode_provider_chain=f"{provider}:{provider}/model",
        opencode_provider_base_urls=(f"{provider}.base_url=https://scoped.example/v1" if missing != "endpoint" else ""),
        opencode_provider_tokens=(f"{provider}.token=scoped-synthetic-key" if missing != "key" else ""),
        openai_api_key="wrong-legacy-key", openai_base_url="https://wrong.example/v1",
    )
    with use_config(config), pytest.raises(ValueError, match="discovery provider") as error:
        build_opencode_config_dict(str(tmp_path))
    assert "scoped-synthetic-key" not in str(error.value)
    assert "wrong-legacy-key" not in str(error.value)


@pytest.mark.parametrize("entrypoint", ["process", "session"])
def test_runtime_google_scope_reaches_actual_submission(discovery, monkeypatch, entrypoint):
    path, old_config, repo = discovery
    cached = CatalogCache(old_config).load()
    data = json.loads(path.read_text())
    data["providers"][0]["id"] = "google"
    for field in ("bindings", "variant_options"):
        data[field] = {key.replace("pool/", "google/", 1): value for key, value in data[field].items()}
    path.write_text(json.dumps(data))
    config = load_selection_config(path, environ={})
    CatalogCache(config).publish(inventory={"google": ["a", "b"]}, benchmarks=cached["benchmarks"])
    process = RecordingProcess()
    agent = harness(discovery)
    agent.process = process
    monkeypatch.setattr("agent_core.harness.client.OpenCodeProcess", lambda: process)
    ledger = PaidAttempts()
    if entrypoint == "process":
        agent.run_turn(repo_path=repo, message="work", paid_attempts=ledger)
    else:
        session = agent.open_session(repo_path=repo)
        session.run_turn(repo_path=repo, prompt_file=repo / "prompt.md", paid_attempts=ledger)
        session.close()
    assert ledger.count == 1
    registration = process.calls[0]["config"]["provider"]["google"]
    assert registration["npm"] == "@ai-sdk/openai-compatible"
    assert registration["options"] == {"baseURL": "https://provider.example/v1", "apiKey": "{env:POOL_KEY}"}


@pytest.mark.parametrize("entrypoint", ["process", "session"])
@pytest.mark.parametrize("missing_field", ["opencode_provider_base_urls", "opencode_provider_tokens"])
def test_missing_scoped_registration_fails_before_billing(discovery, monkeypatch, entrypoint, missing_field):
    _, _, repo = discovery
    agent = harness(discovery)
    original = agent.discovery_runtime.config_updates

    def incomplete_registration(selection, model):
        return {**original(selection, model), missing_field: ""}

    monkeypatch.setattr(agent.discovery_runtime, "config_updates", incomplete_registration)
    process = RecordingProcess()
    agent.process = process
    agent.session_client_factory = lambda *_: pytest.fail("opened invalid provider registration")
    ledger = PaidAttempts()
    with pytest.raises(ValueError, match="discovery provider"):
        if entrypoint == "process":
            agent.run_turn(repo_path=repo, message="work", paid_attempts=ledger)
        else:
            agent.open_session(repo_path=repo)
    assert ledger.count == 0
    assert not process.calls


@pytest.mark.parametrize("entrypoint", ["process", "session"])
@pytest.mark.parametrize("fallback_enabled", [False, True])
@pytest.mark.parametrize("blocked_first", [False, True])
def test_discovery_respects_fallback_setting(discovery, monkeypatch, entrypoint, fallback_enabled, blocked_first):
    _, config, repo = discovery
    if blocked_first:
        DiscoveryRuntime(config, environ={"POOL_KEY": "host-synthetic-key"}).mark_unhealthy(
            "pool/a", reason="rate_limit"
        )
    agent = harness(discovery, opencode_provider_fallback_enabled=fallback_enabled)
    process = RecordingProcess(fail())
    agent.process = process
    monkeypatch.setattr("agent_core.harness.client.OpenCodeProcess", lambda: process)
    ledger = PaidAttempts()
    if entrypoint == "process":
        result = agent.run_turn(repo_path=repo, message="work", paid_attempts=ledger)
    else:
        session = agent.open_session(repo_path=repo)
        result = session.run_turn(repo_path=repo, prompt_file=repo / "prompt.md", paid_attempts=ledger)
        session.close()
    expected = ["pool/b"] if blocked_first else ["pool/a", "pool/b"] if fallback_enabled else ["pool/a"]
    assert [call["model_id"] for call in process.calls] == expected
    assert ledger.count == len(expected)
    assert (result.type == "completed") == (len(expected) == 2)
    registered = process.calls[0]["config"]["provider"]["pool"]["models"]
    assert set(registered) == {model.partition("/")[2] for model in expected}


def test_bootstrap_preserves_token_usage(discovery):
    from agent_core.harness.records import token_usage_from_turn

    _, _, repo = discovery
    agent = harness(discovery)
    turn = TurnResult(type="completed", result="initialized", tokens={"input": 123, "output": 45})
    agent.process = RecordingProcess(turn)
    result = agent.bootstrap_workspace(repo_path=repo, request=WorkspaceBootstrapRequest(purpose="initialize"))
    assert result.usage == token_usage_from_turn(turn)
    assert result.usage["input_tokens"] == 123


@pytest.mark.parametrize("session_id,bootstrap", [("historic", None), (None, "only for resuming")])
def test_fresh_prompt_is_not_replaced_without_complete_resume_context(discovery, session_id, bootstrap):
    _, _, repo = discovery
    agent = harness(discovery)
    agent.process = RecordingProcess()
    agent.run_turn(repo_path=repo, message="original", session_id=session_id, bootstrap_message=bootstrap)
    assert agent.process.calls[0]["message"] == "original"


def test_discovery_wraps_supplied_workspace_even_when_isolation_disabled(discovery):
    from contextlib import contextmanager

    _, _, repo = discovery
    agent = harness(discovery)
    agent.isolate_attempts = False
    outer = repo / "outer"
    outer.mkdir()
    entered = []
    @contextmanager
    def workspace(model):
        entered.append(model)
        yield outer
    agent.workspace_factory = workspace
    agent.process = RecordingProcess()
    agent.run_turn(repo_path=repo, message="work")
    assert entered == ["pool/a"]
    assert Path(agent.process.calls[0]["repo_path"]) != outer
    assert not (outer / "opencode.json").exists()


def test_manual_factory_preserves_absent_discovery_options(tmp_path, monkeypatch):
    from agent_core.harness import opencode
    from contextlib import nullcontext

    calls = []
    def run(process, **kwargs):
        calls.append(kwargs)
        assert kwargs["discovery_policy"] is None
        assert kwargs["candidate_options"] is None
        with kwargs["workspace_factory"]("pool/a") as cwd:
            assert cwd == tmp_path
        return TurnResult(type="completed")
    monkeypatch.setattr(opencode, "run_turn_with_fallback", run)
    agent = create_opencode_harness(HarnessSpec(name="opencode", options={}))
    agent.workspace_factory = lambda _: nullcontext(tmp_path)
    assert agent.run_turn(repo_path=tmp_path, message="manual").type == "completed"
    assert len(calls) == 1


def test_resolution_config_factory_receives_ranked_model_and_repository(discovery, monkeypatch):
    from agent_core.harness import config as config_module

    _, _, repo = discovery
    agent = harness(discovery)
    base = agent._config_for("pool/a", repo)
    configured = []
    def factory(model, path):
        configured.append((model, path))
        return base
    agent.config = factory
    original = config_module.build_opencode_config_dict
    validated = []
    def build(path, **kwargs):
        validated.append(path)
        return original(path, **kwargs)
    monkeypatch.setattr(config_module, "build_opencode_config_dict", build)
    assert agent.preferred_model(repo) == "pool/a"
    assert configured[0] == ("pool/a", repo)
    assert validated == [str(repo), str(repo)]
