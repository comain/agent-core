"""Introducing an agent to a repository once, without naming the agent.

The OpenCode side is driven through an injected client so the session dance is
real code running against a stub server, and the second harness is a whole
implementation rather than a patch -- it is the only honest way to find out
whether the product-facing API still needs OpenCode.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from agent_core.config import HarnessConfig
from agent_core.harness import (
    BootstrapResult,
    BootstrapUnsupportedError,
    HarnessReadiness,
    HarnessSpec,
    ReadinessStatus,
    WorkspaceBootstrapRequest,
    bootstrap_harness_workspace,
    check_harness_readiness,
    create_configured_harness,
    prepare_harness_workspace,
    register_harness,
    unregister_harness,
)
from agent_core.harness.opencode import OpenCodeHarness
from agent_core.harness.process import TurnResult


class StubClient:
    """A stand-in OpenCode server that records the whole session lifecycle."""

    def __init__(self, *, text="AGENTS.md written", completion_type="completed", fail=None):
        self.text = text
        self.completion_type = completion_type
        self.fail = fail
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.messages: list[str] = []
        self.inits: list[dict] = []

    def create_session(self, model_id=None, permission=None, variant=None, **kwargs):
        session_id = f"session-{len(self.created) + 1}"
        self.created.append(session_id)
        return session_id

    def delete_session(self, session_id):
        self.deleted.append(session_id)

    def send_message(self, session_id, prompt, model_id=None, variant=None, **kwargs):
        self.messages.append(prompt)
        if self.fail:
            raise self.fail

    def send_message_and_get_user_info(self, session_id, prompt, model_id=None, **kwargs):
        self.messages.append(prompt)
        if self.fail:
            raise self.fail
        return {"id": "message-1", "model": {"providerID": "openai", "modelID": "gpt-5"}}

    def init_session(self, session_id, message_id, provider_id, model_id):
        self.inits.append(
            {
                "session_id": session_id,
                "message_id": message_id,
                "provider_id": provider_id,
                "model_id": model_id,
            }
        )

    def poll_completion(self, session_id, timeout=600, on_update=None, **kwargs):
        return {"type": self.completion_type, "result": self.text}

    def latest_turn_result(self, session_id):
        return TurnResult(
            type=self.completion_type,
            result=self.text,
            session_id=session_id,
            model_id="openai/gpt-5",
            tokens={"input": 10, "output": 4, "total": 14},
            cost_usd=0.002,
        )

    def analyze_session_tokens(self, session_id):
        return {}

    def analyze_session_retrospect(self, session_id):
        return {}

    def get_session_patch_count(self, session_id):
        return 0


def opencode_with(client: StubClient) -> OpenCodeHarness:
    return OpenCodeHarness(
        HarnessConfig(opencode_model="openai/gpt-5"),
        models=("openai/gpt-5",),
        session_client_factory=lambda repo, model_id=None: client,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    return workspace


# --- OpenCode bootstrap ----------------------------------------------------


def test_bootstrap_without_a_prompt_uses_the_harness_own_initialisation(repo):
    client = StubClient()
    harness = opencode_with(client)

    result = bootstrap_harness_workspace(
        harness,
        repo_path=repo,
        request=WorkspaceBootstrapRequest(purpose="project bootstrap", timeout_seconds=60),
    )

    assert isinstance(result, BootstrapResult)
    assert result.completed is True
    assert result.session_id == "session-1"
    assert result.output_text == "AGENTS.md written"
    assert result.duration_seconds >= 0
    assert result.usage["total_tokens"] == 14
    assert client.inits and client.inits[0]["session_id"] == "session-1"


def test_bootstrap_with_a_prompt_runs_that_prompt_instead(repo, tmp_path):
    prompt = tmp_path / "bootstrap.md"
    prompt.write_text("Summarise this repository.", encoding="utf-8")
    client = StubClient()
    harness = opencode_with(client)

    result = bootstrap_harness_workspace(
        harness,
        repo_path=repo,
        request=WorkspaceBootstrapRequest(purpose="summary", prompt_file=prompt),
    )

    assert result.completed is True
    assert client.messages == ["Summarise this repository."]
    assert client.inits == []


def test_a_bootstrap_that_produced_no_text_is_still_a_typed_result(repo):
    client = StubClient(text="")
    harness = opencode_with(client)

    result = bootstrap_harness_workspace(
        harness,
        repo_path=repo,
        request=WorkspaceBootstrapRequest(purpose="project bootstrap"),
    )

    assert result.output_text == ""
    assert result.completed is True


def test_an_unfinished_bootstrap_reports_incomplete_rather_than_raising(repo):
    client = StubClient(completion_type="error", text="")
    harness = opencode_with(client)

    result = bootstrap_harness_workspace(
        harness,
        repo_path=repo,
        request=WorkspaceBootstrapRequest(purpose="project bootstrap"),
    )

    assert result.completed is False
    assert client.deleted == ["session-1"]


def test_the_temporary_session_is_closed_exactly_once(repo):
    client = StubClient()
    harness = opencode_with(client)

    bootstrap_harness_workspace(
        harness,
        repo_path=repo,
        request=WorkspaceBootstrapRequest(purpose="project bootstrap"),
    )

    assert client.deleted == ["session-1"]


def test_a_failing_bootstrap_still_closes_its_session_and_keeps_its_own_error(repo):
    client = StubClient(fail=RuntimeError("provider exploded"))
    harness = opencode_with(client)

    with pytest.raises(RuntimeError, match="provider exploded"):
        bootstrap_harness_workspace(
            harness,
            repo_path=repo,
            request=WorkspaceBootstrapRequest(purpose="project bootstrap"),
        )

    assert client.deleted == ["session-1"]


def test_a_cleanup_failure_does_not_replace_the_primary_error(repo):
    class RefusesToClose(StubClient):
        def delete_session(self, session_id):
            super().delete_session(session_id)
            raise RuntimeError("cleanup also failed")

    client = RefusesToClose(fail=RuntimeError("provider exploded"))
    harness = opencode_with(client)

    with pytest.raises(RuntimeError, match="provider exploded"):
        bootstrap_harness_workspace(
            harness,
            repo_path=repo,
            request=WorkspaceBootstrapRequest(purpose="project bootstrap"),
        )

    assert client.deleted == ["session-1"]


def test_a_cleanup_failure_does_not_lose_a_successful_bootstrap(repo):
    class RefusesToClose(StubClient):
        def delete_session(self, session_id):
            super().delete_session(session_id)
            raise RuntimeError("cleanup also failed")

    client = RefusesToClose()
    harness = opencode_with(client)

    result = bootstrap_harness_workspace(
        harness,
        repo_path=repo,
        request=WorkspaceBootstrapRequest(purpose="project bootstrap"),
    )

    assert result.completed is True


def test_bootstrap_does_not_reuse_a_products_working_session(repo):
    """Each bootstrap gets its own session, so it cannot pollute a real one."""
    client = StubClient()
    harness = opencode_with(client)
    request = WorkspaceBootstrapRequest(purpose="project bootstrap")

    first = bootstrap_harness_workspace(harness, repo_path=repo, request=request)
    second = bootstrap_harness_workspace(harness, repo_path=repo, request=request)

    assert [first.session_id, second.session_id] == ["session-1", "session-2"]
    assert client.deleted == ["session-1", "session-2"]


# --- a second harness, with no OpenCode anywhere near it -------------------


class PiHarness:
    """A whole second implementation: its own everything, sharing no code."""

    name = "pi"

    def __init__(self, spec=None):
        self.spec = spec
        self.prepared: list[Path] = []
        self.probes = 0
        self.bootstraps: list[str] = []

    def run_turn(self, *, prompt_file, repo_path, **kwargs):
        class PiResult:
            type = "completed"
            result = "pi ran a turn"
            session_id = "pi-1"
            tokens = {"input": 1, "output": 1, "total": 2}
            cost_usd = 0.0
            model_id = "pi/reasoner-1"
            error = None
            raw_log_path = None

        return PiResult()

    def prepare_workspace(self, *, repo_path):
        self.prepared.append(Path(repo_path))
        (Path(repo_path) / ".pi").mkdir(exist_ok=True)

    def check_readiness(self, *, repo_path, timeout_seconds):
        self.probes += 1
        return HarnessReadiness(ready=True, status=ReadinessStatus.READY, detail="pi is local")

    def bootstrap_workspace(self, *, repo_path, request):
        self.bootstraps.append(request.purpose)
        return BootstrapResult(
            completed=True,
            session_id="pi-bootstrap-1",
            output_text="pi looked around",
            duration_seconds=0.1,
        )


@pytest.fixture
def pi_registered():
    register_harness("pi", PiHarness, replace=True)
    yield
    unregister_harness("pi")


def test_a_second_harness_serves_the_whole_product_api_without_opencode(repo, pi_registered):
    """The proof the abstraction is real: same calls, no OpenCode in sight."""
    harness = create_configured_harness(HarnessSpec(name="pi", readiness="probe"))

    prepare_harness_workspace(harness, repo_path=repo)
    readiness = check_harness_readiness(harness, repo_path=repo, timeout_seconds=120)
    bootstrap = bootstrap_harness_workspace(
        harness,
        repo_path=repo,
        request=WorkspaceBootstrapRequest(purpose="project bootstrap"),
    )

    assert (repo / ".pi").is_dir()
    assert readiness.status is ReadinessStatus.READY
    assert harness.probes == 1
    assert bootstrap.output_text == "pi looked around"
    assert harness.bootstraps == ["project bootstrap"]


def test_the_neutral_lifecycle_path_touches_no_opencode_object(tmp_path):
    """Asserted in a subprocess: the whole product API runs on a fake harness.

    The package still registers OpenCode on import -- that is what makes
    `name="opencode"` work -- so the claim being checked is the useful one:
    nothing on the neutral path is an OpenCode object, and no OpenCode code
    runs while a second implementation is driven through it.
    """
    probe = f"""
from pathlib import Path
from agent_core.harness import lifecycle
from agent_core.harness.lifecycle import (
    BootstrapResult,
    HarnessReadiness,
    ReadinessStatus,
    WorkspaceBootstrapRequest,
    bootstrap_harness_workspace,
    check_harness_readiness,
    prepare_harness_workspace,
)


class Pi:
    def run_turn(self, **kwargs):
        raise AssertionError

    def prepare_workspace(self, *, repo_path):
        pass

    def check_readiness(self, *, repo_path, timeout_seconds):
        return HarnessReadiness(ready=True, status=ReadinessStatus.READY)

    def bootstrap_workspace(self, *, repo_path, request):
        return BootstrapResult(True, "pi-1", "done", 0.0)


repo = Path({str(tmp_path)!r})
harness = Pi()
prepare_harness_workspace(harness, repo_path=repo)
check_harness_readiness(harness, repo_path=repo, timeout_seconds=5)
bootstrap_harness_workspace(
    harness, repo_path=repo, request=WorkspaceBootstrapRequest(purpose="bootstrap")
)
borrowed = [
    name
    for name, value in vars(lifecycle).items()
    if "opencode" in str(getattr(value, "__module__", "")).lower()
]
print(borrowed)
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == "[]", completed.stdout


def test_a_turn_only_harness_reports_bootstrap_unsupported(repo):
    class TurnOnly:
        def run_turn(self, *, prompt_file, repo_path, **kwargs):
            raise AssertionError

    with pytest.raises(BootstrapUnsupportedError):
        bootstrap_harness_workspace(
            TurnOnly(),
            repo_path=repo,
            request=WorkspaceBootstrapRequest(purpose="project bootstrap"),
        )
