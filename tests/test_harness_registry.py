"""Choosing which agent runs a turn, by name."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.harness.node import run_harness_node
from agent_core.harness.registry import (
    Harness,
    HarnessSpec,
    UnknownHarnessError,
    available_harnesses,
    create_configured_harness,
    create_harness,
    register_harness,
    unregister_harness,
)


class PiHarness:
    """A second implementation, standing in for a future agent.

    Deliberately shares nothing with OpenCode: its own result type, its own
    field names internally, its own way of being constructed.
    """

    def __init__(self, settings=None, *, answer='{"findings": []}'):
        self.settings = settings
        self.answer = answer
        self.turns = 0

    def run_turn(self, *, prompt_file, repo_path, **kwargs):
        self.turns += 1

        class PiResult:
            type = "completed"
            result = self.answer
            session_id = "pi-session-1"
            tokens = {"input": 3, "output": 2, "total": 5}
            cost_usd = 0.001
            model_id = "pi/reasoner-1"
            error = None
            raw_log_path = None

        return PiResult()

    def accepts_policy(self, _spec):
        return True


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for name in ("pi", "pi-two", "temp"):
        unregister_harness(name)


def test_an_implementation_is_asked_for_by_name():
    register_harness("pi", PiHarness)
    harness = create_harness("pi", {"some": "settings"})

    assert isinstance(harness, PiHarness)
    assert harness.settings == {"some": "settings"}


def test_a_product_passes_only_a_neutral_configuration():
    received = []

    def create_pi(spec):
        received.append(spec)
        return PiHarness()

    register_harness("pi", create_pi)
    spec = HarnessSpec(
        name="pi",
        options={"model": "reasoner-1"},
        timeout_seconds=17,
        permissions={"edit": "deny"},
        readable_dirs=(Path("/evidence"),),
        cache_dir=".product-cache",
    )

    harness = create_configured_harness(spec)

    assert isinstance(harness, PiHarness)
    assert received == [spec]


def test_restricted_policy_rejects_adapter_without_enforcement_capability():
    class TurnOnlyHarness:
        def run_turn(self, **_kwargs):
            return None

    register_harness("temp", lambda _spec: TurnOnlyHarness())

    with pytest.raises(ValueError, match="cannot enforce"):
        create_configured_harness(
            HarnessSpec(name="temp", permissions={"edit": "deny"})
        )


def test_the_name_is_case_and_space_insensitive():
    """It comes from configuration, where people type things."""
    register_harness("pi", PiHarness)
    assert isinstance(create_harness("  PI  "), PiHarness)


def test_an_unknown_name_says_what_is_available():
    register_harness("pi", PiHarness)
    with pytest.raises(UnknownHarnessError, match="available: "):
        create_harness("nope")


def test_registering_the_same_name_twice_is_refused():
    """Otherwise which implementation runs depends on import order."""
    register_harness("pi", PiHarness)
    with pytest.raises(ValueError, match="already registered"):
        register_harness("pi", PiHarness)


def test_a_registration_can_be_replaced_deliberately():
    register_harness("pi", PiHarness)
    register_harness("pi", lambda *a, **k: "replaced", replace=True)
    assert create_harness("pi") == "replaced"


def test_the_built_in_harness_is_registered_by_importing_the_package():
    """A consumer that names the default must not have to register it."""
    import agent_core.harness  # noqa: F401

    assert "opencode" in available_harnesses()


def test_a_second_implementation_runs_a_node_with_no_product_changes(tmp_path):
    """The point of the exercise.

    The step is handed a harness it knows nothing about, and the outcome is
    the same shape -- so swapping the agent is configuration, not a code
    change in the product.
    """
    register_harness("pi", PiHarness)
    prompt = tmp_path / "p.md"
    prompt.write_text("review this", encoding="utf-8")

    harness = create_harness("pi", answer='{"findings": [{"title": "x"}]}')
    outcome = run_harness_node(
        harness,
        name="correctness",
        repo_path=tmp_path,
        prompt=lambda attempt, feedback: prompt,
        parse=json.loads,
    )

    assert outcome.accepted
    assert outcome.payload == {"findings": [{"title": "x"}]}
    assert outcome.record.session_id == "pi-session-1"
    assert outcome.record.model_id == "pi/reasoner-1"
    # Usage is translated the same way whatever produced it.
    assert outcome.record.usage["total_tokens"] == 5


def test_any_object_with_run_turn_satisfies_the_protocol():
    """The contract is narrow on purpose; nothing has to subclass anything."""
    assert isinstance(PiHarness(), Harness)


def test_the_contract_is_one_method():
    """A harness that does not choose between models should not carry a stub."""
    from agent_core.harness import preferred_model_of

    class Minimal:
        def run_turn(self, *, prompt_file, repo_path, **kw):
            return None

    assert isinstance(Minimal(), Harness)
    assert preferred_model_of(Minimal()) is None


def test_a_harness_that_knows_its_model_is_asked():
    from agent_core.harness import preferred_model_of

    class Choosy:
        def run_turn(self, **kw):
            return None

        def preferred_model(self, repo_path=None):
            return "pi/reasoner-1"

    assert preferred_model_of(Choosy()) == "pi/reasoner-1"
