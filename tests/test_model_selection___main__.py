"""Tests for model_selection.__main__, named for strict enforcement discovery."""

import json

import pytest

from test_model_configuration import write_config

from agent_core.model_selection import __main__ as cli


def test_cli_missing_cache_is_actionable_and_secret_free(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "secret-sentinel")
    assert cli.main(["explain", "--config", str(path)]) == 1
    captured = capsys.readouterr()
    assert "catalog missing" in captured.err
    assert "secret-sentinel" not in captured.out + captured.err


def test_cli_requires_explicit_absolute_config(capsys):
    assert cli.main(["explain", "--config", "relative.json"]) == 1
    assert "absolute" in capsys.readouterr().err


def test_explain_uses_cached_catalog_in_score_order(tmp_path, monkeypatch, capsys):
    from test_model_discovery_runtime import runtime

    runtime(tmp_path)
    monkeypatch.setenv("POOL_KEY", "secret-sentinel")
    monkeypatch.delenv("AGENT_MODEL_CODING_INDEX_MIN", raising=False)
    def unexpected_refresh(*args, **kwargs):
        pytest.fail("explain must not refresh the catalog")
    monkeypatch.setattr(cli, "refresh_catalog", unexpected_refresh)
    assert cli.main(["explain", "--config", str(tmp_path / "selection.json")]) == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["application"] == "uta"
    assert output["minimum_coding_score"] == 70
    assert output["threshold_source"] == "default"
    assert output["ranking_strategy"] == "price-efficient"
    assert output["attribution"] == "Artificial Analysis https://artificialanalysis.ai/"
    assert [model["identity"] for model in output["models"]] == ["pool/a", "pool/b"]
    assert [model["score"] for model in output["models"]] == [80, 75]
    assert [decision["identity"] for decision in output["decisions"]] == ["pool/a", "pool/b"]
    assert all(decision["eligible"] for decision in output["decisions"])
    assert captured.err == ""
    assert "secret-sentinel" not in captured.out


def test_explain_empty_available_set_still_returns_diagnostics(tmp_path, monkeypatch, capsys):
    from test_model_discovery_runtime import runtime

    runtime(tmp_path)
    monkeypatch.delenv("POOL_KEY", raising=False)
    monkeypatch.delenv("AGENT_MODEL_CODING_INDEX_MIN", raising=False)
    assert cli.main(["explain", "--config", str(tmp_path / "selection.json")]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["models"] == []
    assert {d["reason"] for d in output["decisions"]} == {"provider_credential_missing"}


def test_refresh_emits_result_and_passes_operator_environment(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    monkeypatch.setenv("AGENT_MODEL_CODING_INDEX_MIN", "73")
    monkeypatch.setenv("ARTIFICIAL_ANALYSIS_API_KEY", "secret-sentinel")
    calls = []
    async def refresh(config, *, environ):
        calls.append(config)
        assert environ["ARTIFICIAL_ANALYSIS_API_KEY"] == "secret-sentinel"
        assert config.policy.minimum_coding_score == 73
        return {"status": "refreshed", "models": 2}
    monkeypatch.setattr(cli, "refresh_catalog", refresh)
    assert cli.main(["refresh", "--config", str(path)]) == 0
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "refreshed", "models": 2}
    assert captured.err == ""


@pytest.mark.parametrize("error,expected", [
    (ValueError("invalid policy"), "invalid policy"),
    (OSError("secret-sentinel"), "OSError"),
    (TimeoutError("secret-sentinel"), "TimeoutError"),
])
def test_refresh_failure_is_sanitized(tmp_path, monkeypatch, capsys, error, expected):
    async def refresh(*args, **kwargs):
        raise error
    monkeypatch.setattr(cli, "refresh_catalog", refresh)
    assert cli.main(["refresh", "--config", str(write_config(tmp_path))]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"model selection failed: {expected}\n"
