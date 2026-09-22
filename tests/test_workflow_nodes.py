"""Tests for the workflow steps every consumer shares."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_core.git import GitWorkspace
from agent_core.prompts import PromptLibrary
from agent_core.workflow import NodeRegistry, WorkflowSpec
from agent_core.workflow.graph import build_graph
from agent_core.workflow.nodes import (
    MissingContextError,
    agent_turn,
    prepare_workspace,
    render_prompt,
)
from agent_core.workflow.registry import shared_registry


class FakeResult:
    def __init__(self, type="completed", result="answer"):
        self.type = type
        self.result = result


class FakeRunner:
    def __init__(self, result=None):
        self.calls = []
        self._result = result or FakeResult()

    def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


@pytest.fixture
def origin(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(seed), "config", k, v], check=True)
    (seed / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "first"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "main"], check=True, capture_output=True)
    return bare


@pytest.fixture
def prompts(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "review.md.j2").write_text("Review {{ commit_id }} in {{ repo_path }}\n", encoding="utf-8")
    return PromptLibrary(d)


# -- prepare_workspace ------------------------------------------------------------


def test_prepare_workspace_clones_and_reports_the_commit(origin, tmp_path):
    ws = GitWorkspace(tmp_path / "cache")
    out = prepare_workspace(
        {"repo_url": str(origin), "branch": "main"}, {}, {"workspace": ws}
    )
    assert (Path(out["repo_path"]) / "app.py").exists()
    assert len(out["commit_id"]) == 40


def test_prepare_workspace_resolves_the_commit_actually_checked_out(origin, tmp_path):
    """A branch trigger records the tip at trigger time.

    Reporting a review against a commit that is not the one reviewed makes the
    report impossible to trust, so the resolved sha is written back.
    """
    ws = GitWorkspace(tmp_path / "cache")
    out = prepare_workspace({"repo_url": str(origin), "branch": "main"}, {}, {"workspace": ws})

    head = subprocess.run(["git", "-C", str(origin), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()
    assert out["commit_id"] == head


def test_prepare_workspace_accepts_a_local_checkout(tmp_path):
    """A path rather than a URL: development and tests, with nothing to clone."""
    repo = tmp_path / "local"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True, capture_output=True)

    ws = GitWorkspace(tmp_path / "cache")
    out = prepare_workspace({"repo_url": str(repo), "branch": "main"}, {}, {"workspace": ws})
    assert Path(out["repo_path"]) == repo


def test_prepare_workspace_scope_isolates_two_tasks_on_one_local_repo(tmp_path):
    repo = tmp_path / "local"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True, capture_output=True)

    ws = GitWorkspace(tmp_path / "cache")
    a = prepare_workspace(
        {"repo_url": str(repo), "branch": "main", "task_ref": "task-a"},
        {},
        {"workspace": ws},
    )
    b = prepare_workspace(
        {"repo_url": str(repo), "branch": "main", "task_ref": "task-b"},
        {},
        {"workspace": ws},
    )
    assert Path(a["repo_path"]) != Path(b["repo_path"])
    assert Path(a["repo_path"]) != repo
    assert (Path(a["repo_path"]) / "a.txt").exists()


def test_a_shared_node_without_its_context_says_what_is_missing():
    with pytest.raises(MissingContextError, match="workspace"):
        prepare_workspace({"repo_url": "x", "branch": "main"}, {}, {})


# -- render_prompt ----------------------------------------------------------------


def test_render_prompt_writes_the_prompt_into_the_turn_directory(prompts, tmp_path):
    out = render_prompt(
        {"turn_dir": str(tmp_path), "commit_id": "abc", "repo_path": "/repo"},
        {"template": "review.md.j2", "into": "reviewer"},
        {"prompts": prompts},
    )
    assert Path(out["prompt_file"]).read_text().startswith("Review abc in /repo")
    assert Path(out["prompt_inputs_file"]).exists()


def test_render_prompt_can_restrict_which_state_keys_reach_the_template(prompts, tmp_path):
    """Otherwise every prompt depends on every earlier step's output."""
    out = render_prompt(
        {"turn_dir": str(tmp_path), "commit_id": "abc", "repo_path": "/repo", "secret": "s"},
        {"template": "review.md.j2", "values": ["commit_id", "repo_path"]},
        {"prompts": prompts},
    )
    assert "secret" not in Path(out["prompt_inputs_file"]).read_text()


# -- agent_turn -------------------------------------------------------------------


def test_agent_turn_passes_the_prompt_as_a_file(tmp_path):
    """A long prompt on a command line hits the argument length limit."""
    runner = FakeRunner()
    agent_turn(
        {"prompt_file": str(tmp_path / "p.md"), "repo_path": str(tmp_path)},
        {},
        {"runner": runner},
    )
    assert runner.calls[0]["prompt_file"] == tmp_path / "p.md"


def test_agent_turn_reports_a_failure_as_state_rather_than_raising(tmp_path):
    """Whether a failed turn ends the run is the workflow's decision, not the node's."""
    runner = FakeRunner(FakeResult(type="stalled", result=""))
    out = agent_turn(
        {"prompt_file": str(tmp_path / "p.md"), "repo_path": str(tmp_path)}, {}, {"runner": runner}
    )
    assert out["turn_status"] == "stalled"


def test_agent_turn_stores_its_result_under_a_configured_key(tmp_path):
    """Two turns in one workflow must not overwrite each other."""
    runner = FakeRunner()
    out = agent_turn(
        {"prompt_file": str(tmp_path / "p.md"), "repo_path": str(tmp_path)},
        {"output_key": "judge_result"},
        {"runner": runner},
    )
    assert "judge_result" in out


def test_agent_turn_does_not_spawn_when_already_cancelled(tmp_path):
    runner = FakeRunner()
    out = agent_turn(
        {"prompt_file": str(tmp_path / "p.md"), "repo_path": str(tmp_path)},
        {},
        {"runner": runner, "is_cancelled": lambda: True},
    )
    assert out["turn_status"] == "cancelled"
    assert runner.calls == []


def test_agent_turn_without_a_prompt_says_which_step_was_skipped(tmp_path):
    with pytest.raises(MissingContextError, match="render_prompt"):
        agent_turn({"repo_path": str(tmp_path)}, {}, {"runner": FakeRunner()})


# -- the whole thing together -----------------------------------------------------


def test_a_product_workflow_is_config_plus_one_product_node(origin, prompts, tmp_path):
    """The point of the exercise: a consumer defines a flow and one own step."""
    product = NodeRegistry()

    @product.node("save_report")
    def save_report(state, context):
        context["saved"].append(state["turn_text"])
        return {"report": "written"}

    spec = WorkflowSpec.from_dict(
        {
            "name": "review",
            "nodes": [
                "prepare_workspace",
                {"name": "prompt", "uses": "render_prompt",
                 "config": {"template": "review.md.j2", "values": ["commit_id", "repo_path"]}},
                {"name": "review", "uses": "agent_turn"},
                "save_report",
            ],
        }
    )

    saved = []
    graph = build_graph(
        spec,
        shared_registry.extend(product),
        context={
            "workspace": GitWorkspace(tmp_path / "cache"),
            "prompts": prompts,
            "runner": FakeRunner(),
            "saved": saved,
        },
    )

    final = graph.invoke({"repo_url": str(origin), "branch": "main", "turn_dir": str(tmp_path)})

    assert final["report"] == "written"
    assert saved == ["answer"]
    assert len(final["commit_id"]) == 40, "state from the first step survives to the last"


def test_importing_the_package_registers_the_shared_nodes():
    """`shared_registry` must never be importable-but-empty.

    Registration is a side effect of importing `workflow.nodes`; a consumer
    that imported only the registry got an empty one, and found out at graph
    build time with an error pointing nowhere near the missing import.
    """
    import importlib
    import sys

    for name in [m for m in sys.modules if m.startswith("agent_core.workflow")]:
        del sys.modules[name]

    registry = importlib.import_module("agent_core.workflow").shared_registry
    assert {"prepare_workspace", "render_prompt", "agent_turn", "human_gate"} <= set(
        registry.nodes
    )
    assert "gate_decision" in registry.selectors


def test_gate_decision_returns_the_decision_or_errors():
    from agent_core.workflow.nodes import gate_decision

    assert gate_decision({"decision": "approve"}) == "approve"
    assert gate_decision({"decision": "reject"}) == "reject"
    with pytest.raises(KeyError, match="no decision"):
        gate_decision({})


def test_yaml_human_gate_suspends_using_the_yaml_node_name(tmp_path, private_root):
    from agent_core.runtime import RuntimeStore
    from agent_core.workflow.checkpoints import WorkflowRunIdentity, open_checkpointer
    from agent_core.workflow.execution import invoke_workflow
    from agent_core.workflow.nodes import human_gate_node

    store = RuntimeStore(tmp_path / "rt.db")
    store.init()
    reg = NodeRegistry()
    reg.add_node("stamp", lambda state: {"intent_md": "# Intent\n", "task_ref": "t1"})
    reg.add_node("human_gate", human_gate_node)
    spec = WorkflowSpec.from_dict(
        {
            "nodes": [
                "stamp",
                {
                    "name": "review_intent",
                    "uses": "human_gate",
                    "config": {"kind": "input", "artifact_key": "intent_md"},
                },
            ],
            "edges": [["stamp", "review_intent"]],
        }
    )
    run = WorkflowRunIdentity(
        product="dev-flow",
        task_id="t1",
        unit_id="main",
        workflow_run_id="r1",
        cycle="intent-spec",
        version="v1",
    )
    with open_checkpointer(private_root / "cp.sqlite", forbidden_roots=()) as saver:
        graph = build_graph(spec, reg, context={"runtime_store": store}, checkpointer=saver)
        result = invoke_workflow(graph, identity=run, initial_state={}, recursion_limit=50)
    assert result.disposition == "suspended"
    gates = store.pending_gates()
    assert len(gates) == 1
    assert gates[0].node == "review_intent"
    assert "# Intent" in (gates[0].prompt or {}).get("artifact_markdown", "")


def test_two_human_gate_nodes_do_not_share_an_id(tmp_path, private_root):
    from agent_core.identity import Principal
    from agent_core.runtime import RuntimeStore
    from agent_core.workflow.checkpoints import WorkflowRunIdentity, open_checkpointer
    from agent_core.workflow.execution import invoke_workflow
    from agent_core.workflow.nodes import gate_decision, human_gate_node

    store = RuntimeStore(tmp_path / "rt.db")
    store.init()
    reg = NodeRegistry()
    reg.add_node("stamp", lambda state: {"intent_md": "a", "spec_md": "b", "task_ref": "t1"})
    reg.add_node("human_gate", human_gate_node)
    reg.add_selector("gate_decision", gate_decision)
    spec = WorkflowSpec.from_dict(
        {
            "entry": "stamp",
            "nodes": [
                "stamp",
                {
                    "name": "review_intent",
                    "uses": "human_gate",
                    "config": {"artifact_key": "intent_md"},
                },
                {
                    "name": "review_spec",
                    "uses": "human_gate",
                    "config": {"artifact_key": "spec_md"},
                },
            ],
            "edges": [["stamp", "review_intent"]],
            "branches": [
                {
                    "from": "review_intent",
                    "selector": "gate_decision",
                    "routes": {"approve": "review_spec", "reject": "stamp"},
                }
            ],
        }
    )
    run = WorkflowRunIdentity(
        product="dev-flow",
        task_id="t1",
        unit_id="main",
        workflow_run_id="r1",
        cycle="intent-spec",
        version="v1",
    )
    path = private_root / "cp.sqlite"
    with open_checkpointer(path, forbidden_roots=()) as saver:
        graph = build_graph(spec, reg, context={"runtime_store": store}, checkpointer=saver)
        first = invoke_workflow(graph, identity=run, initial_state={}, recursion_limit=50)
    assert first.disposition == "suspended"
    intent_gate = store.pending_gates()[0]
    assert intent_gate.node == "review_intent"
    store.answer_gate(
        gate_id=intent_gate.gate_id,
        response={"decision": "approve", "comments": "ok"},
        principal=Principal(subject="u", kind="user"),
    )
    with open_checkpointer(path, forbidden_roots=()) as saver:
        graph = build_graph(spec, reg, context={"runtime_store": store}, checkpointer=saver)
        second = invoke_workflow(
            graph,
            identity=run,
            initial_state={},
            recursion_limit=50,
            resume_value={"decision": "approve", "comments": "ok"},
        )
    assert second.disposition == "suspended"
    pending = store.pending_gates()
    assert len(pending) == 1
    assert pending[0].node == "review_spec"
    assert pending[0].gate_id != intent_gate.gate_id
