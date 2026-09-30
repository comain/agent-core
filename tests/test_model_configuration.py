"""Target-specific tests for model_selection.configuration."""

import json

import pytest

from agent_core.model_selection.configuration import load_selection_config


def write_config(tmp_path, **updates):
    data = {"schema_version": 1, "cache_root": str(tmp_path / "cache"),
            "availability_db": str(tmp_path / "availability.db"),
            "providers": [{"id": "pool", "base_url": "https://provider.example/v1",
                           "credential_scope_id": "team", "credential_generation": "1",
                           "api_key_env": "POOL_KEY"}],
            "policy": {"application_id": "uta", "minimum_coding_score": 65}}
    data.update(updates)
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(data))
    return path


def test_production_threshold_wins_without_mutating_source(tmp_path):
    path = write_config(tmp_path)
    a = load_selection_config(path, environ={"AGENT_MODEL_CODING_INDEX_MIN": "72"})
    b = load_selection_config(path, environ={})
    assert a.policy.minimum_coding_score == 72
    assert a.threshold_source == "production"
    assert b.policy.minimum_coding_score == 65
    assert b.threshold_source == "application"


@pytest.mark.parametrize("value", ["", " ", "nan", "inf", "-1", "101", "true"])
def test_invalid_override_is_not_ignored(tmp_path, value):
    with pytest.raises(ValueError):
        load_selection_config(write_config(tmp_path), environ={"AGENT_MODEL_CODING_INDEX_MIN": value})


def test_zero_and_default(tmp_path):
    path = write_config(tmp_path, policy={"application_id": "cr"})
    assert load_selection_config(path, environ={}).policy.minimum_coding_score == 70
    assert load_selection_config(path, environ={}).threshold_source == "default"
    assert load_selection_config(path, environ={"AGENT_MODEL_CODING_INDEX_MIN": "0"}).policy.minimum_coding_score == 0


def test_literal_secrets_and_relative_paths_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_selection_config(write_config(tmp_path, api_key="secret"), environ={})
    with pytest.raises(ValueError):
        load_selection_config(write_config(tmp_path, cache_root="./cache"), environ={})


def test_provider_scope_and_url_validation(tmp_path):
    path = write_config(tmp_path)
    config = load_selection_config(path, environ={})
    assert config.providers[0].base_url == "https://provider.example/v1"
    data = json.loads(path.read_text())
    data["providers"][0]["base_url"] = "https://secret:password@provider.example/v1"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_selection_config(path, environ={})


def test_internal_http_provider_endpoint_is_preserved(tmp_path):
    path = write_config(tmp_path)
    data = json.loads(path.read_text())
    data["providers"][0]["base_url"] = "http://token-pool.internal/v1"
    path.write_text(json.dumps(data))
    assert load_selection_config(path, environ={}).providers[0].base_url == "http://token-pool.internal/v1"


@pytest.mark.parametrize("options", [{"apiKey": "secret"}, {"headers": {"Authorization": "secret"}},
                                    {"temperature": 0.2}, {"reasoningEffort": "invented"}])
def test_variant_options_are_credential_free_exact_effort(tmp_path, options):
    with pytest.raises(ValueError):
        load_selection_config(write_config(tmp_path, variant_options={"pool/a": options}), environ={})


@pytest.mark.parametrize("updates", [{"schema_version": 2}, {"providers": []}])
def test_unsupported_schema_and_empty_inventory_rejected(tmp_path, updates):
    with pytest.raises(ValueError, match="^invalid model selection configuration$"):
        load_selection_config(write_config(tmp_path, **updates), environ={})


def test_duplicate_provider_ids_rejected(tmp_path):
    path = write_config(tmp_path)
    data = json.loads(path.read_text())
    data["providers"] *= 2
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="^invalid model selection configuration$"):
        load_selection_config(path, environ={})


@pytest.mark.parametrize("data,message", [([], "model selection config must be an object"),
                                         ({"policy": []}, "invalid application policy")])
def test_invalid_document_shape(tmp_path, data, message):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=f"^{message}$"):
        load_selection_config(path, environ={})


def test_config_path_and_size_limits(tmp_path):
    with pytest.raises(ValueError, match="^model selection config path must be absolute$"):
        load_selection_config("relative.json", environ={})
    path = write_config(tmp_path)
    body = path.read_text()
    path.write_text(body + " " * (1024 * 1024 - len(body)))
    assert load_selection_config(path, environ={}).schema_version == 1
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="^model selection config exceeds 1 MiB$"):
        load_selection_config(path, environ={})


def test_exact_variant_options_round_trip(tmp_path):
    variants = {"pool/a": {"reasoningEffort": "xhigh"}}
    config = load_selection_config(write_config(tmp_path, variant_options=variants), environ={})
    assert config.variant_options == variants


@pytest.mark.parametrize("strategy", ["price-efficient", "best-score"])
def test_ranking_strategy_round_trip(tmp_path, strategy):
    path = write_config(tmp_path, policy={"application_id": "uta", "ranking_strategy": strategy})
    assert load_selection_config(path, environ={}).policy.ranking_strategy == strategy


def test_legacy_efficiency_strategy_loads_as_price_efficient(tmp_path):
    path = write_config(tmp_path, policy={"application_id": "uta", "ranking_strategy": "best-efficient"})
    assert load_selection_config(path, environ={}).policy.ranking_strategy == "price-efficient"


def test_bad_ranking_strategy_fails_config_load(tmp_path):
    path = write_config(tmp_path, policy={"application_id": "uta", "ranking_strategy": "typo"})
    with pytest.raises(ValueError, match="invalid model selection configuration"):
        load_selection_config(path, environ={})
