"""The built-in harness, assembled here rather than by each consumer."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.config import HarnessConfig
from agent_core.harness import HarnessSpec, TurnProgress, create_configured_harness, create_harness
from agent_core.harness.opencode import OpenCodeHarness


class Recorder:
    """Stands in for the provider chain walker."""

    def __init__(self):
        self.kwargs = None

    def __call__(self, process, **kwargs):
        self.kwargs = kwargs

        class Result:
            type = "completed"
            result = "{}"
            session_id = "s1"
            tokens = {}
            cost_usd = 0.0
            model_id = kwargs.get("preferred_model")
            error = None
            raw_log_path = None

        return Result()


@pytest.fixture
def walker(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr("agent_core.harness.opencode.run_turn_with_fallback", recorder)
    return recorder


def test_attempts_are_isolated_by_default(tmp_path, walker):
    """The generated config names the model, so attempts cannot share a directory.

    A fallback that reused the previous attempt's workspace would re-run the
    model that just failed. That is a property of this implementation, not a
    preference a caller should have to remember.
    """
    prompt = tmp_path / "correctness" / "prompt.md"
    prompt.parent.mkdir()
    prompt.write_text("x", encoding="utf-8")

    OpenCodeHarness().run_turn(prompt_file=prompt, repo_path=tmp_path)

    assert walker.kwargs["workspace_factory"] is not None


def test_isolation_can_be_turned_off(tmp_path, walker):
    OpenCodeHarness(isolate_attempts=False).run_turn(
        prompt_file=tmp_path / "p.md", repo_path=tmp_path
    )
    assert walker.kwargs["workspace_factory"] is None


def test_a_caller_supplied_workspace_factory_wins(tmp_path, walker):
    mine = lambda model: None
    OpenCodeHarness(workspace_factory=mine).run_turn(
        prompt_file=tmp_path / "p.md", repo_path=tmp_path
    )
    assert walker.kwargs["workspace_factory"] is mine


def test_the_chain_is_left_to_be_resolved_at_turn_time(tmp_path, walker):
    """Health changes between turns; a chain fixed at construction goes stale."""
    OpenCodeHarness().run_turn(prompt_file=tmp_path / "p.md", repo_path=tmp_path)
    assert walker.kwargs["models"] is None


def test_message_xor_prompt_file_and_session_are_forwarded(tmp_path, walker):
    OpenCodeHarness(isolate_attempts=False).run_turn(
        message="hello",
        repo_path=tmp_path,
        session_id="ses_1",
        delivery="stdin",
        title="spec_docs_plan",
        pure=False,
        env={"OPENCODE_CONFIG": "/tmp/oc.json"},
        project_dir="/tmp/scratch",
        bootstrap_message="full",
    )
    assert walker.kwargs["message"] == "hello"
    assert walker.kwargs.get("prompt_file") is None
    assert walker.kwargs["session_id"] == "ses_1"
    assert walker.kwargs["delivery"] == "stdin"
    assert walker.kwargs["title"] == "spec_docs_plan"
    assert walker.kwargs["pure"] is False
    assert walker.kwargs["env"]["OPENCODE_CONFIG"] == "/tmp/oc.json"
    assert walker.kwargs["project_dir"] == "/tmp/scratch"
    assert walker.kwargs["bootstrap_message"] == "full"


def test_run_turn_rejects_message_and_prompt_file_together(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one"):
        OpenCodeHarness(isolate_attempts=False).run_turn(
            message="hello", prompt_file=prompt, repo_path=tmp_path
        )


def test_a_preferred_model_is_passed_through(tmp_path, walker):
    OpenCodeHarness().run_turn(
        prompt_file=tmp_path / "p.md", repo_path=tmp_path, model_id="p/m"
    )
    assert walker.kwargs["preferred_model"] == "p/m"


def test_configuration_may_be_built_per_turn(tmp_path, walker):
    """A product whose config names the model or the directory needs that."""
    seen = []

    def config_for(model_id, repo_path):
        seen.append((model_id, repo_path))
        return HarnessConfig()

    OpenCodeHarness(config_for).run_turn(
        prompt_file=tmp_path / "p.md", repo_path=tmp_path, model_id="p/m"
    )

    assert seen and seen[0][0] == "p/m"
    assert seen[0][1] == tmp_path.resolve()


def test_a_ready_configuration_is_accepted_too(tmp_path, walker):
    OpenCodeHarness(HarnessConfig()).run_turn(prompt_file=tmp_path / "p.md", repo_path=tmp_path)
    assert walker.kwargs["repo_path"] == str(tmp_path.resolve())


def test_registered_factory_keeps_the_low_level_config_api():
    config = HarnessConfig(opencode_model="p/m")

    harness = create_harness("opencode", config)

    assert isinstance(harness, OpenCodeHarness)
    assert harness.config is config


def test_the_timeout_of_one_turn_overrides_the_default(tmp_path, walker):
    OpenCodeHarness(timeout=100).run_turn(
        prompt_file=tmp_path / "p.md", repo_path=tmp_path, timeout_seconds=7
    )
    assert walker.kwargs["timeout"] == 7


def test_neutral_progress_is_translated_at_the_opencode_boundary(tmp_path, walker):
    updates = []
    OpenCodeHarness().run_turn(
        prompt_file=tmp_path / "p.md",
        repo_path=tmp_path,
        on_progress=updates.append,
    )

    walker.kwargs["on_update"]("tool[bash] completed - run tests")

    assert updates == [
        TurnProgress(
            kind="tool",
            message="tool[bash] completed - run tests",
            tool="bash",
            status="completed",
            detail="run tests",
        )
    ]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "reasoning: Checking whether retry handling preserves the callback lifecycle",
            TurnProgress(
                kind="reasoning",
                message="reasoning: Checking whether retry handling preserves the callback lifecycle",
                detail="Checking whether retry handling preserves the callback lifecycle",
            ),
        ),
        (
            "text: Review scope loaded; inspecting the changed API boundary",
            TurnProgress(
                kind="text",
                message="text: Review scope loaded; inspecting the changed API boundary",
                detail="Review scope loaded; inspecting the changed API boundary",
            ),
        ),
    ],
)
def test_agent_narrative_is_available_to_the_public_projector(
    tmp_path, walker, line, expected
):
    updates = []
    OpenCodeHarness().run_turn(
        prompt_file=tmp_path / "p.md",
        repo_path=tmp_path,
        on_progress=updates.append,
    )

    walker.kwargs["on_update"](line)

    assert updates == [expected]


def test_a_turn_rejects_two_progress_callbacks(tmp_path, walker):
    with pytest.raises(ValueError, match="progress callback"):
        OpenCodeHarness().run_turn(
            prompt_file=tmp_path / "p.md",
            repo_path=tmp_path,
            on_progress=lambda update: None,
            on_update=lambda line: None,
        )


def test_configured_harness_translates_neutral_policy(tmp_path):
    evidence = tmp_path / "evidence"
    spec = HarnessSpec(
        name="opencode",
        options={
            "opencode_bin": "/x/opencode",
            "opencode_provider_chain": "p:p/m",
            "opencode_turn_log_dir": "turns",
            "opencode_default_external_dirs": False,
        },
        timeout_seconds=123,
        permissions={"*": "allow", "edit": "deny"},
        readable_dirs=(evidence,),
        cache_dir=".product-cache",
    )

    harness = create_configured_harness(spec)
    config = harness.config("p/m", tmp_path)

    assert isinstance(harness, OpenCodeHarness)
    assert harness.timeout == 123
    assert config.opencode_bin == "/x/opencode"
    assert config.opencode_model == "p/m"
    assert config.opencode_permissions == {"*": "allow", "edit": "deny"}
    assert config.opencode_permission_dirs.split(",") == [
        str(evidence.resolve()),
        str(tmp_path.resolve()),
    ]
    assert config.agent_cache_dir == ".product-cache"
    assert config.opencode_turn_log_dir == str(tmp_path / "turns")
    assert config.opencode_pass_model_flag is False
    assert config.opencode_provider_fallback_enabled is True


def test_configured_harness_expands_named_edit_artifacts_for_isolated_workspace(tmp_path):
    spec = HarnessSpec(
        name="opencode",
        permissions={
            "edit": {
                "*": "deny",
                "page.md": "allow",
                "source-map.json": "allow",
            }
        },
    )

    config = create_configured_harness(spec).config("p/m", tmp_path)

    assert config.opencode_permissions["edit"] == {
        "*": "deny",
        "page.md": "allow",
        "**/page.md": "allow",
        "source-map.json": "allow",
        "**/source-map.json": "allow",
    }


def test_configured_harness_places_default_turn_logs_in_product_cache(tmp_path):
    cache_dir = tmp_path / "runtime-cache"
    harness = create_configured_harness(
        HarnessSpec(name="opencode", cache_dir=str(cache_dir))
    )

    config = harness.config("p/m", tmp_path / "repo")

    assert config.opencode_turn_log_dir == str(cache_dir / "opencode_turns")


def test_configured_harness_resolves_relative_product_cache_from_repo(tmp_path):
    harness = create_configured_harness(
        HarnessSpec(name="opencode", cache_dir=".product-cache")
    )

    config = harness.config("p/m", tmp_path)

    assert config.opencode_turn_log_dir == str(
        tmp_path / ".product-cache" / "opencode_turns"
    )


def test_configured_harness_maps_attempt_isolation_option():
    harness = create_configured_harness(
        HarnessSpec(name="opencode", options={"isolate_attempts": False})
    )

    assert harness.isolate_attempts is False
