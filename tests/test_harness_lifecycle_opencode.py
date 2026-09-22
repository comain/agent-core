"""OpenCode's own preparation and readiness, behind the neutral contract.

The probe is exercised through a fake `opencode` process rather than through
patched internals: what matters is which turn is asked for, how the reply is
classified, and what a product is allowed to see afterwards.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent_core.config import HarnessConfig
from agent_core.harness import (
    HarnessSpec,
    ReadinessCheckingHarness,
    ReadinessRetryPolicy,
    ReadinessStatus,
    WorkspacePreparingHarness,
    check_harness_readiness,
    create_configured_harness,
    prepare_harness_workspace,
)
from agent_core.harness import lifecycle
from agent_core.harness.opencode import OpenCodeHarness
from agent_core.harness.process import TurnResult


class FakeProcess:
    """Stands in for the `opencode` binary; records what it was asked to run."""

    def __init__(self, *results: TurnResult) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    def run_turn(self, message=None, *, repo_path, model_id=None, timeout=3600, **kwargs):
        self.calls.append(
            {"message": message, "repo_path": repo_path, "model_id": model_id, "timeout": timeout}
        )
        if not self._results:
            raise AssertionError("the harness probed more times than the test allowed")
        result = self._results[0]
        if len(self._results) > 1:
            self._results.pop(0)
        return result


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    return workspace


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(lifecycle, "_sleep", recorded.append)
    return recorded


def harness_with(*results: TurnResult, model: str = "openai/gpt-5") -> OpenCodeHarness:
    return OpenCodeHarness(HarnessConfig(opencode_model=model), process=FakeProcess(*results))


# --- preparation -----------------------------------------------------------


def test_preparation_writes_the_configuration_with_the_existing_builder(repo):
    harness = harness_with()

    assert prepare_harness_workspace(harness, repo_path=repo) is None

    config = json.loads((repo / "opencode.json").read_text(encoding="utf-8"))
    assert config["model"]
    assert "provider" in config


def test_opencode_answers_the_preparation_and_readiness_protocols():
    harness = harness_with()
    assert isinstance(harness, WorkspacePreparingHarness)
    assert isinstance(harness, ReadinessCheckingHarness)


def test_a_spec_may_declare_the_probe_requirement():
    harness = create_configured_harness(HarnessSpec(name="opencode", readiness="probe"))
    assert isinstance(harness, ReadinessCheckingHarness)


def test_registered_harness_resolves_the_bundled_executable_before_isolation(
    tmp_path, monkeypatch
):
    bundled = tmp_path / ".opencode" / "bin" / "opencode"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("binary", encoding="utf-8")
    bundled.chmod(0o700)
    monkeypatch.setattr(shutil, "which", lambda _command: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    harness = create_configured_harness(HarnessSpec(name="opencode"))

    assert harness._config_for(None, tmp_path).opencode_bin == str(bundled)


# --- readiness classification ---------------------------------------------


def test_a_confirming_reply_is_ready(repo, sleeps):
    harness = harness_with(TurnResult(type="completed", result="OK"))

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert outcome.ready is True
    assert outcome.status is ReadinessStatus.READY
    assert len(harness.process.calls) == 1
    assert sleeps == []


def test_the_probe_uses_the_configured_model_and_the_callers_timeout(repo):
    harness = harness_with(TurnResult(type="completed", result="OK"), model="openai/gpt-5-mini")

    check_harness_readiness(harness, repo_path=repo, timeout_seconds=45)

    call = harness.process.calls[0]
    assert call["model_id"] == "openai/gpt-5-mini"
    assert call["timeout"] == 45
    assert Path(call["repo_path"]) == repo.resolve()


def test_a_provider_auth_error_is_authentication_required_and_is_not_retried(repo, sleeps):
    harness = harness_with(
        TurnResult(type="error", error={"name": "ProviderAuthError", "data": {"message": "no auth"}})
    )

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert outcome.status is ReadinessStatus.AUTHENTICATION_REQUIRED
    assert outcome.ready is False
    assert len(harness.process.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "message",
    ["Incorrect API key provided", "invalid_api_key", "The API key is missing"],
)
def test_an_invalid_key_api_error_is_authentication_required(repo, sleeps, message):
    harness = harness_with(
        TurnResult(type="error", error={"name": "APIError", "data": {"message": message}})
    )

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert outcome.status is ReadinessStatus.AUTHENTICATION_REQUIRED
    assert len(harness.process.calls) == 1
    assert sleeps == []


def test_a_rate_limited_probe_is_unavailable_and_is_retried(repo, sleeps):
    harness = harness_with(TurnResult(type="rate_limited", error={"retry_after_seconds": 30}))

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert outcome.status is ReadinessStatus.UNAVAILABLE
    assert len(harness.process.calls) == 3
    assert sleeps == [3.0, 6.0]


def test_a_timed_out_probe_is_unavailable_and_is_retried(repo, sleeps):
    harness = harness_with(TurnResult(type="timeout"))

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert outcome.status is ReadinessStatus.UNAVAILABLE
    assert "timed out" in outcome.detail
    assert len(harness.process.calls) == 3
    assert sleeps == [3.0, 6.0]


def test_a_provider_failure_is_unavailable_rather_than_authenticated(repo, sleeps):
    harness = harness_with(
        TurnResult(type="error", error={"name": "APIError", "data": {"message": "500 upstream"}})
    )

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert outcome.status is ReadinessStatus.UNAVAILABLE
    assert len(harness.process.calls) == 3


def test_a_reply_without_the_confirmation_is_unavailable(repo, sleeps):
    harness = harness_with(TurnResult(type="completed", result="I would rather not"))

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert outcome.status is ReadinessStatus.UNAVAILABLE
    assert len(harness.process.calls) == 3


def test_a_recovered_provider_stops_the_retries(repo, sleeps):
    harness = harness_with(
        TurnResult(type="rate_limited"),
        TurnResult(type="completed", result="OK"),
    )

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert outcome.status is ReadinessStatus.READY
    assert len(harness.process.calls) == 2
    assert sleeps == [3.0]


def test_the_worst_case_probe_budget_is_the_369_seconds_it_was(repo, sleeps):
    """3 x 120s probes plus 3s and 6s of backoff, exactly as before."""
    harness = harness_with(TurnResult(type="timeout"))

    check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert sum(call["timeout"] for call in harness.process.calls) + sum(sleeps) == 369


def test_one_attempt_is_the_callers_to_ask_for(repo, sleeps):
    harness = harness_with(TurnResult(type="timeout"))

    check_harness_readiness(
        harness,
        repo_path=repo,
        timeout_seconds=120,
        retry=ReadinessRetryPolicy(attempts=1),
    )

    assert len(harness.process.calls) == 1
    assert sleeps == []


# --- nothing raw escapes ---------------------------------------------------


def test_detail_carries_no_credentials_config_or_raw_payload(repo, sleeps):
    harness = harness_with(
        TurnResult(
            type="error",
            error={
                "name": "APIError",
                "data": {
                    "message": 'upstream said {"api_key": "sk-live-9876543210", "model": "x"}',
                    "stack": "at Object.<anonymous> (/opt/opencode/index.js:1:1)",
                },
            },
        )
    )

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert "sk-live-9876543210" not in outcome.detail
    assert "index.js" not in outcome.detail
    assert "opencode.json" not in outcome.detail
    assert len(outcome.detail) <= lifecycle.DETAIL_MAX_LENGTH


def test_a_rate_limit_detail_mentions_the_wait_without_the_payload(repo, sleeps):
    harness = harness_with(
        TurnResult(type="rate_limited", error={"retry_after_seconds": 42, "token": "sk-secret"})
    )

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert "42" in outcome.detail
    assert "sk-secret" not in outcome.detail


def test_readiness_is_not_run_implicitly_by_a_turn(repo, tmp_path):
    """Products choose when to probe; a turn must not add a provider call."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do a thing", encoding="utf-8")
    harness = harness_with(TurnResult(type="completed", result="done"))

    harness.run_turn(prompt_file=prompt, repo_path=repo)

    assert all(call["message"] != "Reply with only: OK" for call in harness.process.calls)
