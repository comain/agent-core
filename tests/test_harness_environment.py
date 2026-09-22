"""Benchmark credentials must not cross the agent subprocess boundary."""

import json
import os

import pytest

from agent_core.config import settings
from agent_core.harness.process import OpenCodeProcess
from agent_core.harness.server import OpenCodeServer


AA_KEYS = ("ARTIFICIAL_ANALYSIS_API_KEY", "ARTIFICAL_ANALYSIS_KEY")


class LaunchRecorded(Exception):
    """Stop at Popen without starting an agent or making provider requests."""


@pytest.fixture
def launch(monkeypatch):
    environment = {"PATH": "/usr/bin:/bin"}
    # Mutmut's trampoline requires this key, including an empty baseline value.
    if "MUTANT_UNDER_TEST" in os.environ:
        environment["MUTANT_UNDER_TEST"] = os.environ["MUTANT_UNDER_TEST"]
    monkeypatch.setattr(os, "environ", environment)
    recorded = {}

    def record(cmd, **kwargs):
        recorded.update(cmd=cmd, env=kwargs["env"])
        raise LaunchRecorded

    monkeypatch.setattr("subprocess.Popen", record)
    return recorded


@pytest.mark.parametrize("mutation_control", [None, "", "stats", "synthetic_mutant"])
def test_launch_fixture_preserves_mutation_control_not_credentials(
    monkeypatch, request, mutation_control
):
    if mutation_control is None:
        monkeypatch.delenv("MUTANT_UNDER_TEST", raising=False)
    else:
        monkeypatch.setenv("MUTANT_UNDER_TEST", mutation_control)
    for key in (*AA_KEYS, "UNRELATED_API_KEY"):
        monkeypatch.setenv(key, "synthetic-parent-secret")

    request.getfixturevalue("launch")

    expected = {"PATH": "/usr/bin:/bin"}
    if mutation_control is not None:
        expected["MUTANT_UNDER_TEST"] = mutation_control
    assert dict(os.environ) == expected


@pytest.mark.parametrize("source", ["parent", "caller", "both"])
@pytest.mark.parametrize("delivery", ["argv", "file", "stdin"])
def test_process_strips_aa_keys_after_caller_merge(
    monkeypatch, tmp_path, launch, caplog, source, delivery
):
    for key in AA_KEYS:
        monkeypatch.delenv(key, raising=False)
        if source in {"parent", "both"}:
            monkeypatch.setenv(key, "synthetic-parent-aa-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-provider-secret")
    monkeypatch.setattr(settings, "opencode_provider_tokens", "openrouter.token=synthetic-config-token")
    overrides = {"OPENCODE_CONFIG": str(tmp_path / "opencode.json")}
    if source in {"caller", "both"}:
        overrides.update({key: "synthetic-caller-aa-secret" for key in AA_KEYS})
    overrides["OPENROUTER_API_KEY"] = "synthetic-caller-provider-token"
    original = overrides.copy()
    parent = os.environ.copy()

    with pytest.raises(LaunchRecorded):
        OpenCodeProcess().run_turn(
            "hello", repo_path=str(tmp_path), model_id="openrouter/test-model",
            delivery=delivery, env=overrides,
        )

    assert not set(AA_KEYS).intersection(launch["env"])
    assert launch["env"]["ANTHROPIC_API_KEY"] == "synthetic-provider-secret"
    assert launch["env"]["OPENROUTER_API_KEY"] == overrides["OPENROUTER_API_KEY"]
    assert launch["env"]["OPENCODE_CONFIG"] == overrides["OPENCODE_CONFIG"]
    assert overrides == original
    assert dict(os.environ) == parent
    serialized = json.dumps(launch) + caplog.text
    assert "synthetic-parent-aa-secret" not in serialized
    assert "synthetic-caller-aa-secret" not in serialized


@pytest.mark.parametrize("keys", [AA_KEYS[:1], AA_KEYS[1:], AA_KEYS])
@pytest.mark.parametrize("print_logs", [False, True])
def test_server_strips_aa_keys_after_provider_merge(
    monkeypatch, tmp_path, launch, caplog, keys, print_logs
):
    for key in AA_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in keys:
        monkeypatch.setenv(key, "synthetic-server-aa-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-provider-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-parent-provider-token")
    monkeypatch.setattr(settings, "openrouter_api_key", "synthetic-config-provider-token")
    monkeypatch.setattr(settings, "opencode_server_print_logs", print_logs)
    monkeypatch.setattr(settings, "opencode_server_log_to_file", False)
    monkeypatch.setattr(OpenCodeServer, "_terminate_stale_listeners", lambda self: None)
    parent = os.environ.copy()

    with pytest.raises(LaunchRecorded):
        OpenCodeServer(str(tmp_path)).start()

    assert not set(AA_KEYS).intersection(launch["env"])
    assert launch["env"]["ANTHROPIC_API_KEY"] == "synthetic-provider-secret"
    assert launch["env"]["OPENROUTER_API_KEY"] == "synthetic-config-provider-token"
    assert dict(os.environ) == parent
    assert "synthetic-server-aa-secret" not in json.dumps(launch) + caplog.text


@pytest.mark.parametrize("keys", [(), AA_KEYS[:1], AA_KEYS[1:], AA_KEYS])
def test_sanitize_agent_env_copies_mapping_and_preserves_other_credentials(keys):
    from types import MappingProxyType

    from agent_core.harness.environment import sanitize_agent_env

    providers = {
        key: "synthetic-provider-token"
        for key in (
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
            "GOOGLE_GENERATIVE_AI_API_KEY", "OPENROUTER_API_KEY",
            "DEEPSEEK_API_KEY", "TENCENT_API_KEY", "TOKEN_POOL_API_KEY",
        )
    }
    original = {"PATH": "/usr/bin", **providers, **dict.fromkeys(keys, "synthetic-aa-token")}
    sanitized = sanitize_agent_env(MappingProxyType(original))

    assert sanitized == {"PATH": "/usr/bin", **providers}
    assert sanitized is not original
    assert all(original[key] == "synthetic-aa-token" for key in keys)
    assert sanitize_agent_env(sanitized) == sanitized
