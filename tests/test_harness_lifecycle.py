"""Neutral harness lifecycle: preparation, readiness, and bootstrap contracts.

These tests drive the contract from the product side. The doubles are whole
harnesses implementing the published protocols -- not patches into agent-core
internals -- because the thing being proved is that a product can work through
the contract alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.harness import (
    BootstrapResult,
    BootstrapUnsupportedError,
    HarnessReadiness,
    HarnessSpec,
    ReadinessCheckingHarness,
    ReadinessRetryPolicy,
    ReadinessStatus,
    ReadinessUnsupportedError,
    WorkspaceBootstrapRequest,
    WorkspaceBootstrappingHarness,
    WorkspacePreparingHarness,
    bootstrap_harness_workspace,
    check_harness_readiness,
    create_configured_harness,
    prepare_harness_workspace,
    readiness_declaration_of,
    register_harness,
    unregister_harness,
)
from agent_core.harness import lifecycle


class TurnOnlyHarness:
    """The narrowest thing that is still a harness: it runs a turn."""

    def run_turn(self, *, prompt_file, repo_path, **kwargs):  # pragma: no cover - unused
        raise AssertionError("lifecycle helpers must not run turns")


class LifecycleHarness(TurnOnlyHarness):
    """A second implementation with every optional capability."""

    def __init__(self, outcomes=None, *, bootstrap_text="ready to go"):
        self.prepared: list[Path] = []
        self.probes: list[tuple[Path, int]] = []
        self.bootstraps: list[WorkspaceBootstrapRequest] = []
        self._outcomes = list(outcomes or [])
        self._bootstrap_text = bootstrap_text

    def prepare_workspace(self, *, repo_path: Path) -> None:
        self.prepared.append(Path(repo_path))

    def check_readiness(self, *, repo_path: Path, timeout_seconds: int) -> HarnessReadiness:
        self.probes.append((Path(repo_path), timeout_seconds))
        if self._outcomes:
            return self._outcomes.pop(0)
        return HarnessReadiness(ready=True, status=ReadinessStatus.READY)

    def bootstrap_workspace(self, *, repo_path: Path, request) -> BootstrapResult:
        self.bootstraps.append(request)
        return BootstrapResult(
            completed=True,
            session_id="fake-session",
            output_text=self._bootstrap_text,
            duration_seconds=0.5,
        )


def unavailable(detail: str = "provider unavailable") -> HarnessReadiness:
    return HarnessReadiness(ready=False, status=ReadinessStatus.UNAVAILABLE, detail=detail)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    return workspace


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """A fake clock: the helper must never really wait during a test."""
    recorded: list[float] = []
    monkeypatch.setattr(lifecycle, "_sleep", recorded.append)
    return recorded


# --- preparation -----------------------------------------------------------


def test_preparation_is_a_no_op_for_a_harness_that_does_not_implement_it(repo):
    prepare_harness_workspace(TurnOnlyHarness(), repo_path=repo)


def test_preparation_delegates_once_to_a_capable_harness(repo):
    harness = LifecycleHarness()
    prepare_harness_workspace(harness, repo_path=repo)
    assert harness.prepared == [repo.resolve()]


def test_preparation_rejects_a_path_that_is_not_a_directory(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(ValueError):
        prepare_harness_workspace(LifecycleHarness(), repo_path=missing)


# --- readiness metadata ----------------------------------------------------


def test_absent_readiness_metadata_never_means_authenticated(repo):
    with pytest.raises(ReadinessUnsupportedError):
        check_harness_readiness(TurnOnlyHarness(), repo_path=repo, timeout_seconds=120)


def test_declared_not_required_returns_ready_without_probing(repo):
    register_harness("fake-offline", lambda spec: LifecycleHarness(), replace=True)
    try:
        harness = create_configured_harness(
            HarnessSpec(name="fake-offline", readiness="not_required")
        )
        outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)
    finally:
        unregister_harness("fake-offline")
    assert outcome.ready is True
    assert outcome.status is ReadinessStatus.READY
    assert harness.probes == []


def test_the_declaration_is_retained_by_the_created_harness(repo):
    register_harness("fake-offline", lambda spec: LifecycleHarness(), replace=True)
    try:
        harness = create_configured_harness(
            HarnessSpec(name="fake-offline", readiness="not_required")
        )
        plain = create_configured_harness(HarnessSpec(name="fake-offline"))
    finally:
        unregister_harness("fake-offline")
    assert readiness_declaration_of(harness) == "not_required"
    assert readiness_declaration_of(plain) is None


def test_declaring_probe_without_the_protocol_fails_at_construction():
    register_harness("fake-turn-only", lambda spec: TurnOnlyHarness(), replace=True)
    try:
        with pytest.raises(ValueError):
            create_configured_harness(HarnessSpec(name="fake-turn-only", readiness="probe"))
    finally:
        unregister_harness("fake-turn-only")


def test_an_unknown_readiness_declaration_is_refused_by_the_spec():
    with pytest.raises(ValueError):
        HarnessSpec(name="opencode", readiness="maybe")


def test_declared_probe_uses_the_protocol(repo):
    register_harness("fake-probe", lambda spec: LifecycleHarness(), replace=True)
    try:
        harness = create_configured_harness(HarnessSpec(name="fake-probe", readiness="probe"))
        outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=30)
    finally:
        unregister_harness("fake-probe")
    assert outcome.status is ReadinessStatus.READY
    assert harness.probes == [(repo.resolve(), 30)]


def test_old_harness_specs_are_constructed_unchanged():
    spec = HarnessSpec(name="opencode", options={"opencode_model": "openai/gpt-5"})
    assert spec.readiness is None
    assert spec.timeout_seconds == 3600


# --- retry policy ----------------------------------------------------------


def test_readiness_retries_unavailable_three_times_with_the_uta_backoff(repo, sleeps):
    """Parity with `uta/app/cli.py::_probe_openai_auth_ready_with_retry`.

    Three attempts, `time.sleep(3 * attempt)` between them. The attempt count
    and the sleep sequence are the budget the product already pays for; this
    migration is not allowed to change either.
    """
    harness = LifecycleHarness([unavailable("first"), unavailable("second"), unavailable("third")])

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert len(harness.probes) == 3
    assert sleeps == [3.0, 6.0]
    assert outcome.status is ReadinessStatus.UNAVAILABLE
    assert outcome.detail == "third"


def test_the_worst_case_readiness_budget_is_369_seconds(repo, sleeps):
    harness = LifecycleHarness([unavailable(), unavailable(), unavailable()])

    check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    probe_budget = sum(timeout for _, timeout in harness.probes)
    assert probe_budget + sum(sleeps) == 369


def test_authentication_required_is_returned_immediately(repo, sleeps):
    denied = HarnessReadiness(
        ready=False,
        status=ReadinessStatus.AUTHENTICATION_REQUIRED,
        detail="provider authentication is required",
    )
    harness = LifecycleHarness([denied, unavailable()])

    outcome = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)

    assert outcome.status is ReadinessStatus.AUTHENTICATION_REQUIRED
    assert len(harness.probes) == 1
    assert sleeps == []


def test_ready_is_returned_without_a_second_probe(repo, sleeps):
    harness = LifecycleHarness()
    assert check_harness_readiness(harness, repo_path=repo, timeout_seconds=120).ready is True
    assert len(harness.probes) == 1
    assert sleeps == []


def test_a_caller_can_ask_for_one_attempt(repo, sleeps):
    harness = LifecycleHarness([unavailable(), unavailable(), unavailable()])

    check_harness_readiness(
        harness,
        repo_path=repo,
        timeout_seconds=120,
        retry=ReadinessRetryPolicy(attempts=1),
    )

    assert len(harness.probes) == 1
    assert sleeps == []


def test_a_non_positive_timeout_is_rejected_before_any_probe(repo, sleeps):
    harness = LifecycleHarness()
    with pytest.raises(ValueError):
        check_harness_readiness(harness, repo_path=repo, timeout_seconds=0)
    assert harness.probes == []


def test_a_policy_with_no_attempts_is_rejected(repo):
    with pytest.raises(ValueError):
        ReadinessRetryPolicy(attempts=0)


def test_a_harness_returning_something_else_is_a_contract_error(repo):
    class Rogue(TurnOnlyHarness):
        def check_readiness(self, *, repo_path, timeout_seconds):
            return True

    with pytest.raises(TypeError):
        check_harness_readiness(Rogue(), repo_path=repo, timeout_seconds=120)


# --- sanitized, bounded detail ---------------------------------------------


def test_detail_is_bounded():
    readiness = HarnessReadiness(ready=False, status=ReadinessStatus.UNAVAILABLE, detail="x" * 5000)
    assert len(readiness.detail) <= lifecycle.DETAIL_MAX_LENGTH


def test_detail_redacts_credentials_and_raw_payloads():
    readiness = HarnessReadiness(
        ready=False,
        status=ReadinessStatus.AUTHENTICATION_REQUIRED,
        detail='auth failed api_key=sk-live-abcdef123456 payload {"token": "hunter2"}\nline two',
    )
    assert "sk-live-abcdef123456" not in readiness.detail
    assert "hunter2" not in readiness.detail
    assert "\n" not in readiness.detail


# --- bootstrap -------------------------------------------------------------


def test_bootstrap_returns_the_typed_result(repo):
    harness = LifecycleHarness()
    request = WorkspaceBootstrapRequest(purpose="project bootstrap", timeout_seconds=60)

    result = bootstrap_harness_workspace(harness, repo_path=repo, request=request)

    assert isinstance(result, BootstrapResult)
    assert result.completed is True
    assert result.output_text == "ready to go"
    assert harness.bootstraps == [request]


def test_bootstrap_is_unsupported_rather_than_pretending_success(repo):
    request = WorkspaceBootstrapRequest(purpose="project bootstrap")
    with pytest.raises(BootstrapUnsupportedError):
        bootstrap_harness_workspace(TurnOnlyHarness(), repo_path=repo, request=request)


def test_a_bootstrap_request_validates_its_own_fields(tmp_path):
    with pytest.raises(ValueError):
        WorkspaceBootstrapRequest(purpose="")
    with pytest.raises(ValueError):
        WorkspaceBootstrapRequest(purpose="x", timeout_seconds=0)
    with pytest.raises(ValueError):
        WorkspaceBootstrapRequest(purpose="x", prompt_file=tmp_path / "missing.md")


def test_bootstrap_defaults_carry_no_prompt():
    request = WorkspaceBootstrapRequest(purpose="project bootstrap")
    assert request.prompt_file is None
    assert request.timeout_seconds == 120


# --- protocol detection ----------------------------------------------------


def test_the_protocols_are_runtime_checkable():
    harness = LifecycleHarness()
    assert isinstance(harness, WorkspacePreparingHarness)
    assert isinstance(harness, ReadinessCheckingHarness)
    assert isinstance(harness, WorkspaceBootstrappingHarness)
    plain = TurnOnlyHarness()
    assert not isinstance(plain, WorkspacePreparingHarness)
    assert not isinstance(plain, ReadinessCheckingHarness)
    assert not isinstance(plain, WorkspaceBootstrappingHarness)


def test_the_neutral_module_imports_no_concrete_harness():
    source = Path(lifecycle.__file__).read_text(encoding="utf-8")
    for forbidden in ("opencode", "OpenCode", "uta", "java", "python_enforce"):
        assert forbidden not in source, f"lifecycle.py must stay neutral, found {forbidden!r}"
