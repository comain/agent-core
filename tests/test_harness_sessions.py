"""Reusable harness sessions stay agent-neutral at the workflow boundary."""

from pathlib import Path

import pytest

from agent_core.harness import (
    AgentSessionRef,
    HarnessSession,
    OpenCodeHarness,
    SessionLocatorScope,
    SessionSnapshot,
    SessionUnsupportedError,
    TurnProgress,
    open_harness_session,
    run_harness_node,
)
from agent_core.harness.process import TurnResult
from agent_core.config import HarnessConfig


class FakeSessionClient:
    def __init__(self) -> None:
        self.created = []
        self.sent = []
        self.deleted = []
        self.turn = 0
        self.poll_kwargs = []

    def create_session(self, **kwargs):
        self.created.append(kwargs)
        return "session-1"

    def send_message(self, session_id, content, **kwargs):
        self.sent.append((session_id, content, kwargs))

    def poll_completion(self, session_id, timeout, on_update, **kwargs):
        self.poll_kwargs.append(kwargs)
        self.turn += 1
        if on_update is not None:
            on_update("reasoning: inspecting the target")
        return {"type": "completed", "result": f"answer-{self.turn}"}

    def latest_turn_result(self, session_id):
        return TurnResult(
            type="completed",
            result=f"answer-{self.turn}",
            session_id="provider-session",
            model_id="provider/model",
            tokens={"input": self.turn, "output": 2, "total": self.turn + 2},
            cost_usd=0.25,
            patch_count=1,
        )

    def analyze_session_tokens(self, session_id):
        return {"total_tokens": {"total": 7}}

    def analyze_session_retrospect(self, session_id):
        return {"hints": ["keep the useful fact"]}

    def get_session_patch_count(self, session_id):
        return 2

    def delete_session(self, session_id):
        self.deleted.append(session_id)


def test_open_session_rejects_a_one_turn_only_harness(tmp_path):
    class OneTurnHarness:
        def run_turn(self, **kwargs):
            raise AssertionError("not called")

    with pytest.raises(SessionUnsupportedError, match="reusable sessions"):
        open_harness_session(OneTurnHarness(), repo_path=tmp_path)


def test_an_opencode_session_is_a_reusable_harness_node_runner(tmp_path):
    client = FakeSessionClient()
    harness = OpenCodeHarness(session_client_factory=lambda repo: client)
    session = open_harness_session(
        harness,
        repo_path=tmp_path,
        model_id="provider/model",
        permissions={"edit": "allow"},
        variant="careful",
    )
    progress = []

    first = run_harness_node(
        session,
        name="generate",
        repo_path=tmp_path,
        prompt=lambda attempt, feedback: _prompt(tmp_path, "first"),
        on_progress=progress.append,
    )
    second = run_harness_node(
        session,
        name="repair",
        repo_path=tmp_path,
        prompt=lambda attempt, feedback: _prompt(tmp_path, "second"),
    )

    assert isinstance(session, HarnessSession)
    assert first.accepted and first.result.result == "answer-1"
    assert second.accepted and second.result.result == "answer-2"
    assert first.record.session_id == second.record.session_id == "session-1"
    assert first.record.usage["input_tokens"] == 1
    assert client.created == [
        {
            "model_id": "provider/model",
            "permission": {"edit": "allow"},
            "variant": "careful",
        }
    ]
    assert [content for _, content, _ in client.sent] == ["first", "second"]
    assert progress == [
        TurnProgress(
            kind="reasoning",
            message="reasoning: inspecting the target",
            detail="inspecting the target",
        )
    ]

    assert session.snapshot() == SessionSnapshot(
        session_id="session-1",
        usage={"total_tokens": {"total": 7}},
        retrospect={"hints": ["keep the useful fact"]},
        patch_count=2,
        provider_cost_usd=0.5,
        session_refs=(
            AgentSessionRef("opencode", "session-1", SessionLocatorScope.PROCESS),
        ),
    )
    session.close()
    session.close()
    assert client.deleted == ["session-1"]


def test_a_session_turn_carries_neutral_stall_and_cancellation_policy_to_the_adapter(tmp_path):
    client = FakeSessionClient()
    session = open_harness_session(
        OpenCodeHarness(session_client_factory=lambda repo: client),
        repo_path=tmp_path,
    )
    cancelled = lambda: False

    session.run_turn(
        prompt_file=_prompt(tmp_path, "generate"),
        repo_path=tmp_path,
        stalled_no_progress_seconds=75,
        is_cancelled=cancelled,
    )

    assert client.poll_kwargs == [
        {"stalled_no_progress_seconds": 75, "is_cancelled": cancelled}
    ]


def test_a_configured_session_passes_its_task_configuration_to_the_client(
    tmp_path, monkeypatch
):
    configured = HarnessConfig(opencode_model="configured/model")
    seen = {}
    client = FakeSessionClient()

    def client_factory(*, repo_path, config):
        seen.update(repo_path=repo_path, config=config)
        return client

    monkeypatch.setattr("agent_core.harness.client.OpenCodeClient", client_factory)

    session = open_harness_session(OpenCodeHarness(configured), repo_path=tmp_path)

    assert seen == {"repo_path": str(tmp_path.resolve()), "config": configured}
    session.close()


def test_opencode_phase_session_fallback_isolates_candidates_and_sums_cost(tmp_path):
    clients = []

    class CandidateClient(FakeSessionClient):
        def __init__(self, model_id, *, fallback, cost):
            super().__init__()
            self.model_id = model_id
            self.fallback = fallback
            self.cost = cost

        def create_session(self, **kwargs):
            self.created.append(kwargs)
            return f"session-{self.model_id}"

        def poll_completion(self, session_id, timeout, on_update, **kwargs):
            return {
                "type": "error" if self.fallback else "completed",
                "result": "" if self.fallback else "answer",
                "fallback_eligible": self.fallback,
                "fallback_reason": "rate_limit" if self.fallback else None,
            }

        def latest_turn_result(self, session_id):
            return TurnResult(
                type="error" if self.fallback else "completed",
                result="" if self.fallback else "answer",
                session_id=session_id,
                model_id=self.model_id,
                tokens={"input": 1, "output": 1, "total": 2},
                cost_usd=self.cost,
                fallback_eligible=self.fallback,
                fallback_reason="rate_limit" if self.fallback else None,
            )

    def factory(repo, model_id):
        client = CandidateClient(
            model_id,
            fallback=model_id == "p/first",
            cost=0.10 if model_id == "p/first" else 0.25,
        )
        clients.append(client)
        return client

    session = open_harness_session(
        OpenCodeHarness(models=("p/first", "q/second"), session_client_factory=factory),
        repo_path=tmp_path,
    )
    result = session.run_turn(prompt_file=_prompt(tmp_path, "generate"), repo_path=tmp_path)

    assert result.type == "completed"
    assert result.model_id == "q/second"
    assert [client.model_id for client in clients] == ["p/first", "q/second"]
    assert clients[0].deleted == ["session-p/first"]
    assert clients[1].deleted == []
    assert session.snapshot().provider_cost_usd == pytest.approx(0.35)

    session.close()
    assert clients[1].deleted == ["session-q/second"]


def test_opencode_phase_session_exhaustion_returns_the_last_failure_as_final(tmp_path):
    clients = []

    class FailingClient(FakeSessionClient):
        def __init__(self, model_id, cost):
            super().__init__()
            self.model_id = model_id
            self.cost = cost

        def create_session(self, **kwargs):
            return f"session-{self.model_id}"

        def poll_completion(self, session_id, timeout, on_update, **kwargs):
            return {
                "type": "error",
                "fallback_eligible": True,
                "fallback_reason": "rate_limit",
            }

        def latest_turn_result(self, session_id):
            return TurnResult(
                type="error",
                session_id=session_id,
                model_id=self.model_id,
                cost_usd=self.cost,
                fallback_eligible=True,
                fallback_reason="rate_limit",
            )

    def factory(repo, model_id):
        client = FailingClient(model_id, 0.10 if model_id == "p/first" else 0.25)
        clients.append(client)
        return client

    session = open_harness_session(
        OpenCodeHarness(models=("p/first", "q/second"), session_client_factory=factory),
        repo_path=tmp_path,
    )
    result = session.run_turn(prompt_file=_prompt(tmp_path, "generate"), repo_path=tmp_path)

    assert result.type == "error"
    assert result.model_id == "q/second"
    assert result.fallback_eligible is False
    assert session.snapshot().provider_cost_usd == pytest.approx(0.35)
    assert [client.deleted for client in clients] == [
        ["session-p/first"],
        ["session-q/second"],
    ]


def _prompt(directory: Path, text: str) -> Path:
    path = directory / f"{text}.md"
    path.write_text(text, encoding="utf-8")
    return path
