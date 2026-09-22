"""Integration coverage for provider fallback, using a fake `opencode` binary.

`test_harness_runner.py` covers the fallback *rules* against a stubbed process.
These drive the real thing: a real subprocess, a real generated `opencode.json`,
and a capture file recording which models were actually attempted.

Ported from the consumer whose fork implemented fallback, because that is where
the behaviour was specified. The fake reads its model from `opencode.json`,
which is what proves each attempt gets its own workspace -- without that, a
fallback silently re-runs the model that just failed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent_core.config import HarnessConfig, use_config
from agent_core.harness import (
    OpenCodeProcess,
    per_turn_workspace,
    reset_model_health,
    run_turn_with_fallback,
)


@pytest.fixture(autouse=True)
def clean_health():
    reset_model_health()
    yield
    reset_model_health()


def _fake(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fallback_fake(tmp_path: Path, capture: Path, *, fail_when: str) -> Path:
    """A fake opencode that records its model and fails for some of them.

    ``fail_when`` is a Python expression over ``model``.
    """
    return _fake(tmp_path, "fake-opencode", f"""#!/usr/bin/env python3
import json, pathlib, sys, time
capture = pathlib.Path({str(capture)!r})
model = json.loads(pathlib.Path("opencode.json").read_text())["model"]
with capture.open("a") as fh:
    fh.write(model + "\\n")
if {fail_when}:
    print('ERROR service=llm error={{"error":{{"name":"AI_APICallError","cause":{{"code":"ConnectionRefused"}}}}}}',
          file=sys.stderr, flush=True)
    time.sleep(20)
else:
    print('{{"type":"step_start","sessionID":"ses_ok","part":{{"type":"step-start"}}}}')
    print('{{"type":"text","sessionID":"ses_ok","part":{{"type":"text","text":"done"}}}}')
    print('{{"type":"step_finish","sessionID":"ses_ok","cost":0.01,"part":{{"reason":"stop","tokens":{{"total":2}}}}}}')
""")


def _config(fake: Path, chain: str) -> HarnessConfig:
    return HarnessConfig(
        opencode_bin=str(fake),
        opencode_provider_chain=chain,
        opencode_provider_fallback_enabled=True,
        opencode_turn_log_enabled=False,
        index_source_dirs="",
        opencode_external_dirs="",
        agent_cache_dir=".agent_cache",
    )


def _run(repo: Path, prompt: Path, models, timeout=30):
    return run_turn_with_fallback(
        OpenCodeProcess(),
        repo_path=str(repo),
        prompt_file=prompt,
        models=models,
        timeout=timeout,
        workspace_factory=lambda model: per_turn_workspace(repo, label="test", model_id=model),
    )


@pytest.fixture
def repo_and_prompt(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review this", encoding="utf-8")
    return repo, prompt


# -- the behaviour ---------------------------------------------------------------


def test_each_attempt_runs_against_its_own_model(tmp_path, repo_and_prompt):
    """The load-bearing property.

    The fake reads its model from opencode.json. If every attempt shared one
    workspace, the capture would repeat the first model and a "fallback" would
    re-run exactly what just failed.
    """
    repo, prompt = repo_and_prompt
    capture = tmp_path / "models.txt"
    fake = _fallback_fake(tmp_path, capture, fail_when="model.startswith('token-pool/')")

    with use_config(_config(fake, "token-pool:a,b;openai:c")):
        started = time.monotonic()
        result = _run(repo, prompt, ["token-pool/a", "openai/c"])

    assert time.monotonic() - started < 15
    assert capture.read_text().splitlines() == ["token-pool/a", "openai/c"]
    assert result.type == "completed"
    assert result.model_id == "openai/c"
    assert result.session_id == "ses_ok"


def test_unreachable_provider_skips_its_remaining_models(tmp_path, repo_and_prompt):
    """A refused connection condemns the provider, not just the model."""
    repo, prompt = repo_and_prompt
    capture = tmp_path / "models.txt"
    fake = _fallback_fake(tmp_path, capture, fail_when="model.startswith('token-pool/')")

    with use_config(_config(fake, "token-pool:a,b;openai:c")):
        result = _run(repo, prompt, ["token-pool/a", "token-pool/b", "openai/c"])

    attempted = capture.read_text().splitlines()
    assert attempted == ["token-pool/a", "openai/c"], "token-pool/b should never be spawned"
    assert result.type == "completed"


def test_first_success_does_not_touch_the_rest_of_the_chain(tmp_path, repo_and_prompt):
    repo, prompt = repo_and_prompt
    capture = tmp_path / "models.txt"
    fake = _fallback_fake(tmp_path, capture, fail_when="False")

    with use_config(_config(fake, "token-pool:a;openai:c")):
        result = _run(repo, prompt, ["token-pool/a", "openai/c"])

    assert capture.read_text().splitlines() == ["token-pool/a"]
    assert result.type == "completed"


def test_exhausting_the_chain_reports_the_last_failure(tmp_path, repo_and_prompt):
    repo, prompt = repo_and_prompt
    capture = tmp_path / "models.txt"
    fake = _fallback_fake(tmp_path, capture, fail_when="True")

    with use_config(_config(fake, "token-pool:a;openai:c")):
        result = _run(repo, prompt, ["token-pool/a", "openai/c"], timeout=8)

    assert result.type != "completed"
    assert capture.read_text().splitlines() == ["token-pool/a", "openai/c"]


def test_cost_and_tokens_are_captured_from_a_real_turn(tmp_path, repo_and_prompt):
    """D8b: declaring the fields is not enough, the success path must fill them."""
    repo, prompt = repo_and_prompt
    capture = tmp_path / "models.txt"
    fake = _fallback_fake(tmp_path, capture, fail_when="False")

    with use_config(_config(fake, "token-pool:a")):
        result = _run(repo, prompt, ["token-pool/a"])

    assert result.tokens.get("total") == 2
    assert result.cost_usd == 0.01
    assert result.model_id == "token-pool/a"


def test_workspace_is_removed_after_each_attempt(tmp_path, repo_and_prompt):
    repo, prompt = repo_and_prompt
    capture = tmp_path / "models.txt"
    fake = _fallback_fake(tmp_path, capture, fail_when="model.startswith('token-pool/')")

    with use_config(_config(fake, "token-pool:a;openai:c")):
        _run(repo, prompt, ["token-pool/a", "openai/c"])

    workspaces = repo / ".agent_cache" / "opencode" / "workspaces"
    assert not workspaces.exists() or list(workspaces.iterdir()) == []
