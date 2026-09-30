"""Starting or resuming a durable workflow, without ever repeating the expensive part.

The spike invalidated the assumption that supplying the same `thread_id` is
enough. On LangGraph 1.x, invoking a *pending* lineage with a fresh mapping
starts again at the entry node, and invoking a *completed* one with a mapping
re-enters it. Only a pending invocation with `None` resumes.

So a product must not call a durable graph directly, and this owns the four
cases. Each test below counts the expensive entry node and proves it runs
exactly once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typing_extensions import TypedDict

from agent_core.workflow.checkpoints import WorkflowRunIdentity, open_checkpointer
from agent_core.workflow.execution import (
    WorkflowCheckpointError,
    invoke_workflow,
)

CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures/contracts/agent_core_v0_6_11.json").read_text(
        encoding="utf-8"
    )
)


class S(TypedDict, total=False):
    trail: list
    boom: bool


def identity(**over):
    base = dict(product="uta", task_id="t1", unit_id="u1",
                workflow_run_id="r1", cycle="cycle", version="v1")
    base.update(over)
    return WorkflowRunIdentity(**base)


@pytest.fixture
def graph_factory(private_root):
    """A two-node graph whose entry node is the expensive one."""
    from langgraph.graph import END, StateGraph

    counts = {"expensive": 0, "cheap": 0}

    def expensive(state):
        counts["expensive"] += 1
        return {"trail": state.get("trail", []) + ["expensive"]}

    def cheap(state):
        counts["cheap"] += 1
        if state.get("boom"):
            raise RuntimeError("worker died")
        return {"trail": state["trail"] + ["cheap"]}

    def build(saver):
        g = StateGraph(S)
        g.add_node("expensive", expensive)
        g.add_node("cheap", cheap)
        g.set_entry_point("expensive")
        g.add_edge("expensive", "cheap")
        g.add_edge("cheap", END)
        return g.compile(checkpointer=saver)

    return build, counts, private_root / "cp.sqlite"


# -- the four cases --------------------------------------------------------

def test_an_absent_lineage_starts(graph_factory):
    build, counts, path = graph_factory
    with open_checkpointer(path, forbidden_roots=()) as saver:
        result = invoke_workflow(build(saver), identity=identity(),
                                 initial_state={"trail": []}, recursion_limit=50)
    assert result.disposition == "started"
    assert result.state["trail"] == ["expensive", "cheap"]
    assert counts["expensive"] == 1


def test_a_pending_lineage_resumes_without_repeating(graph_factory):
    """The property the whole design exists for."""
    build, counts, path = graph_factory
    run = identity()

    with open_checkpointer(path, forbidden_roots=()) as saver:
        with pytest.raises(RuntimeError):
            invoke_workflow(build(saver), identity=run,
                            initial_state={"trail": [], "boom": True}, recursion_limit=50)
    assert counts["expensive"] == 1

    with open_checkpointer(path, forbidden_roots=()) as saver:          # a new process
        graph = build(saver)
        graph.update_state(run.invoke_config(recursion_limit=50), {"boom": False})
        result = invoke_workflow(graph, identity=run,
                                 initial_state={"trail": []}, recursion_limit=50)

    assert result.disposition == "resumed"
    assert counts["expensive"] == 1, "the expensive node ran twice"
    assert result.state["trail"] == ["expensive", "cheap"]


def test_a_completed_lineage_is_reused_not_re_entered(graph_factory):
    """Invoking a finished graph with a mapping re-enters it; that would repeat
    everything and overwrite a result the product may already have committed."""
    build, counts, path = graph_factory
    run = identity()

    with open_checkpointer(path, forbidden_roots=()) as saver:
        invoke_workflow(build(saver), identity=run,
                        initial_state={"trail": []}, recursion_limit=50)
    assert counts["expensive"] == 1

    with open_checkpointer(path, forbidden_roots=()) as saver:
        result = invoke_workflow(build(saver), identity=run,
                                 initial_state={"trail": []}, recursion_limit=50)

    assert result.disposition == "reused_completed"
    assert counts["expensive"] == 1, "a completed lineage was re-entered"
    assert result.state["trail"] == ["expensive", "cheap"]


def test_corruption_is_never_treated_as_absence(graph_factory):
    """Restarting on a corrupt checkpoint silently repeats paid-for work and
    can overwrite a committed result. It must fail loudly instead."""
    build, counts, path = graph_factory

    class Corrupt:
        def get_state(self, config):
            raise ValueError("could not deserialize checkpoint")

    with pytest.raises(WorkflowCheckpointError) as caught:
        invoke_workflow(Corrupt(), identity=identity(),
                        initial_state={"trail": []}, recursion_limit=50)
    assert identity().thread_id in str(caught.value)
    assert counts["expensive"] == 0


def test_checkpoint_dispositions_match_the_0_6_11_contract_fixture():
    class Snapshot:
        def __init__(self, *, values, next_nodes):
            self.values = values
            self.next = next_nodes

    class RecordingGraph:
        def __init__(self, snapshot=None, *, corrupt=False):
            self.snapshot = snapshot
            self.corrupt = corrupt
            self.invocations = []

        def get_state(self, config):
            if self.corrupt:
                raise ValueError("private corrupt payload")
            return self.snapshot

        def invoke(self, state, config):
            self.invocations.append(state)
            marker = "started" if state is not None else "resumed"
            return {"marker": marker}

    observed = {}
    cases = {
        "absent": RecordingGraph(Snapshot(values={}, next_nodes=())),
        "pending": RecordingGraph(
            Snapshot(values={"marker": "checkpointed"}, next_nodes=("next",))
        ),
        "completed": RecordingGraph(
            Snapshot(values={"marker": "completed"}, next_nodes=())
        ),
    }
    for name, graph in cases.items():
        result = invoke_workflow(
            graph,
            identity=identity(),
            initial_state={"marker": "initial"},
            recursion_limit=50,
        )
        argument = (
            "not_called"
            if not graph.invocations
            else (None if graph.invocations[0] is None else "initial")
        )
        observed[name] = {
            "disposition": result.disposition,
            "invoke_argument": argument,
            "state": dict(result.state),
        }

    corrupt = RecordingGraph(corrupt=True)
    with pytest.raises(WorkflowCheckpointError):
        invoke_workflow(
            corrupt,
            identity=identity(),
            initial_state={"marker": "initial"},
            recursion_limit=50,
        )
    observed["corrupt"] = {
        "error": "WorkflowCheckpointError",
        "invoke_argument": "not_called" if not corrupt.invocations else "called",
    }

    assert observed == CONTRACT["checkpoint_states"]


def test_a_clean_rerun_uses_a_new_identity(graph_factory):
    """A rerun is a new lineage, minted explicitly -- never corruption
    reinterpreted as a fresh start."""
    build, counts, path = graph_factory

    with open_checkpointer(path, forbidden_roots=()) as saver:
        invoke_workflow(build(saver), identity=identity(workflow_run_id="r1"),
                        initial_state={"trail": []}, recursion_limit=50)
    with open_checkpointer(path, forbidden_roots=()) as saver:
        again = invoke_workflow(build(saver), identity=identity(workflow_run_id="r2"),
                                initial_state={"trail": []}, recursion_limit=50)

    assert again.disposition == "started"
    assert counts["expensive"] == 2, "a new run should do the work again"


def test_the_recursion_limit_reaches_the_graph(graph_factory):
    """It is invoke-time config; a limit that never arrives is not a limit."""
    build, counts, path = graph_factory
    seen = {}

    class Recording:
        def get_state(self, config):
            seen["config"] = config
            class Snap:
                next = ()
                values = {}
                created_at = None
            return Snap()
        def invoke(self, state, config):
            seen["invoke"] = config
            return {"trail": []}

    invoke_workflow(Recording(), identity=identity(),
                    initial_state={"trail": []}, recursion_limit=77)
    assert seen["config"]["recursion_limit"] == 77


# -- human-gate suspension -------------------------------------------------


def _gated_graph(saver):
    from langgraph.graph import END, StateGraph
    from langgraph.types import interrupt
    from typing_extensions import TypedDict

    class G(TypedDict, total=False):
        doc: str
        decision: str
        writes: int

    def write(state):
        return {"doc": "intent", "writes": int(state.get("writes") or 0) + 1}

    def review(state):
        answer = interrupt({"gate": "review"})
        return {"decision": answer["decision"]}

    g = StateGraph(G)
    g.add_node("write", write)
    g.add_node("review", review)
    g.set_entry_point("write")
    g.add_edge("write", "review")
    g.add_edge("review", END)
    return g.compile(checkpointer=saver)


def test_a_gated_lineage_starts_as_suspended(private_root):
    run = identity()
    with open_checkpointer(private_root / "gate.sqlite", forbidden_roots=()) as saver:
        result = invoke_workflow(
            _gated_graph(saver),
            identity=run,
            initial_state={},
            recursion_limit=50,
        )
    assert result.disposition == "suspended"
    assert result.state.get("doc") == "intent"


def test_a_suspended_lineage_is_not_invoked_again_without_resume_value(private_root):
    run = identity()
    path = private_root / "gate.sqlite"
    with open_checkpointer(path, forbidden_roots=()) as saver:
        invoke_workflow(_gated_graph(saver), identity=run, initial_state={}, recursion_limit=50)
    with open_checkpointer(path, forbidden_roots=()) as saver:
        result = invoke_workflow(
            _gated_graph(saver),
            identity=run,
            initial_state={},
            recursion_limit=50,
        )
    assert result.disposition == "suspended"
    assert result.state.get("writes") == 1


def test_resume_value_continues_past_the_gate(private_root):
    run = identity()
    path = private_root / "gate.sqlite"
    with open_checkpointer(path, forbidden_roots=()) as saver:
        first = invoke_workflow(
            _gated_graph(saver), identity=run, initial_state={}, recursion_limit=50
        )
    assert first.disposition == "suspended"
    with open_checkpointer(path, forbidden_roots=()) as saver:
        result = invoke_workflow(
            _gated_graph(saver),
            identity=run,
            initial_state={},
            recursion_limit=50,
            resume_value={"decision": "approve"},
        )
    assert result.disposition == "resumed"
    assert result.state["decision"] == "approve"
