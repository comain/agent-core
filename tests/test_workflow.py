"""Tests for config-driven workflows."""

from __future__ import annotations

import threading

import pytest

from agent_core.workflow import (
    END,
    NodeRegistry,
    UnknownNodeError,
    WorkflowSpec,
    WorkflowSpecError,
)
from agent_core.workflow.graph import build_graph


@pytest.fixture
def registry():
    reg = NodeRegistry()

    @reg.node("append")
    def append(state, config):
        return {"trace": [*state.get("trace", []), config.get("label", "?")]}

    @reg.node("noop")
    def noop(state):
        return None

    @reg.selector("by_flag")
    def by_flag(state):
        return state.get("flag", "other")

    return reg


# -- the spec --------------------------------------------------------------------


def test_a_list_of_names_is_a_linear_workflow():
    spec = WorkflowSpec.from_dict({"name": "w", "nodes": ["a", "b", "c"]}).linear()
    assert spec.entry == "a"
    assert spec.edges == [("a", "b"), ("b", "c"), ("c", END)]


def test_a_node_may_be_used_twice_under_different_names():
    """Two passes of one implementation is the reason name and uses differ."""
    spec = WorkflowSpec.from_dict(
        {
            "nodes": [
                {"name": "first", "uses": "review", "config": {"depth": 1}},
                {"name": "second", "uses": "review", "config": {"depth": 2}},
            ]
        }
    )
    assert [n.uses for n in spec.nodes] == ["review", "review"]
    assert spec.node("second").config == {"depth": 2}


def test_a_duplicate_node_name_is_refused():
    with pytest.raises(WorkflowSpecError, match="duplicate"):
        WorkflowSpec.from_dict({"nodes": [{"name": "a", "uses": "x"}, {"name": "a", "uses": "y"}]})


def test_an_edge_into_nowhere_is_refused():
    with pytest.raises(WorkflowSpecError, match="unknown node"):
        WorkflowSpec.from_dict({"nodes": ["a"], "edges": [["a", "ghost"]]})


def test_a_branch_to_an_unknown_node_is_refused():
    with pytest.raises(WorkflowSpecError, match="unknown node"):
        WorkflowSpec.from_dict(
            {
                "nodes": ["a", "b"],
                "branches": [{"from": "a", "selector": "s", "routes": {"x": "ghost"}}],
            }
        )


def test_an_unknown_entry_is_refused():
    with pytest.raises(WorkflowSpecError, match="entry"):
        WorkflowSpec.from_dict({"nodes": ["a"], "entry": "b"})


def test_a_workflow_with_no_nodes_is_refused():
    with pytest.raises(WorkflowSpecError, match="no nodes"):
        WorkflowSpec.from_dict({"nodes": []})


# -- the registry ----------------------------------------------------------------


def test_an_unregistered_node_names_what_is_registered(registry):
    spec = WorkflowSpec.from_dict({"nodes": ["missing"]})
    with pytest.raises(UnknownNodeError) as exc:
        build_graph(spec, registry)
    assert "append" in str(exc.value), "the error should say what *is* available"


def test_registering_the_same_name_twice_is_refused(registry):
    with pytest.raises(ValueError, match="already registered"):
        registry.add_node("append", lambda state: None)


def test_extend_lets_a_product_override_a_shared_node(registry):
    """Swapping an implementation must not require changing the workflow."""
    product = NodeRegistry()
    product.add_node("append", lambda state: {"trace": ["overridden"]})

    merged = registry.extend(product)
    spec = WorkflowSpec.from_dict({"nodes": ["append"]})
    assert build_graph(spec, merged).invoke({})["trace"] == ["overridden"]
    # The original is untouched -- extend returns a new registry.
    assert build_graph(spec, registry).invoke({})["trace"] == ["?"]


# -- running ---------------------------------------------------------------------


def test_a_linear_workflow_runs_its_nodes_in_order(registry):
    spec = WorkflowSpec.from_dict(
        {
            "nodes": [
                {"name": "one", "uses": "append", "config": {"label": "1"}},
                {"name": "two", "uses": "append", "config": {"label": "2"}},
            ]
        }
    )
    assert build_graph(spec, registry).invoke({})["trace"] == ["1", "2"]


def test_a_node_that_ignores_config_needs_no_config_parameter(registry):
    spec = WorkflowSpec.from_dict({"nodes": ["noop"]})
    assert build_graph(spec, registry).invoke({"trace": ["kept"]})["trace"] == ["kept"]


def test_context_is_bound_at_build_time_not_passed_through_state():
    """A db handle or settings object must not have to survive a checkpointer."""
    reg = NodeRegistry()

    @reg.node("use_ctx")
    def use_ctx(state, context):
        return {"seen": context["db"].name}

    class Db:
        name = "reviews"

    spec = WorkflowSpec.from_dict({"nodes": ["use_ctx"]})
    graph = build_graph(spec, reg, context={"db": Db()})
    assert graph.invoke({})["seen"] == "reviews"


def test_a_node_sees_langgraph_thread_id_and_yaml_keys():
    """YAML config is merged with the runtime config, not replaced by it."""
    reg = NodeRegistry()

    @reg.node("capture")
    def capture(state, config):
        return {
            "thread_id": (config.get("configurable") or {}).get("thread_id"),
            "depth": config.get("depth"),
            "graph_node": config.get("graph_node"),
        }

    spec = WorkflowSpec.from_dict(
        {"nodes": [{"name": "review_intent", "uses": "capture", "config": {"depth": 1}}]}
    )
    graph = build_graph(spec, reg)
    result = graph.invoke({}, config={"configurable": {"thread_id": "flow-1"}})
    assert result["thread_id"] == "flow-1"
    assert result["depth"] == 1
    assert result["graph_node"] == "review_intent"


def test_graph_node_is_the_yaml_name_not_uses():
    """Two nodes that uses the same implementation must not share a gate id stem."""
    reg = NodeRegistry()
    seen = []

    @reg.node("shared")
    def shared(state, config):
        seen.append(config.get("graph_node"))
        return {}

    spec = WorkflowSpec.from_dict(
        {
            "nodes": [
                {"name": "review_intent", "uses": "shared"},
                {"name": "review_spec", "uses": "shared"},
            ],
            "edges": [["review_intent", "review_spec"]],
        }
    )
    build_graph(spec, reg).invoke({})
    assert seen == ["review_intent", "review_spec"]


def test_a_branch_routes_on_the_selector_key(registry):
    spec = WorkflowSpec.from_dict(
        {
            "nodes": [
                {"name": "start", "uses": "append", "config": {"label": "s"}},
                {"name": "left", "uses": "append", "config": {"label": "L"}},
                {"name": "right", "uses": "append", "config": {"label": "R"}},
            ],
            "entry": "start",
            "edges": [["left", END], ["right", END]],
            "branches": [
                {
                    "from": "start",
                    "selector": "by_flag",
                    "routes": {"go_left": "left", "go_right": "right"},
                }
            ],
        }
    )
    graph = build_graph(spec, registry)
    assert graph.invoke({"flag": "go_left"})["trace"] == ["s", "L"]
    assert graph.invoke({"flag": "go_right"})["trace"] == ["s", "R"]


def test_a_branch_can_end_the_run(registry):
    """The skip path: a workflow that decides there is nothing to do."""
    spec = WorkflowSpec.from_dict(
        {
            "nodes": [
                {"name": "start", "uses": "append", "config": {"label": "s"}},
                {"name": "work", "uses": "append", "config": {"label": "W"}},
            ],
            "edges": [["work", END]],
            "branches": [
                {
                    "from": "start",
                    "selector": "by_flag",
                    "routes": {"skip": END},
                    "default": "work",
                }
            ],
        }
    )
    graph = build_graph(spec, registry)
    assert graph.invoke({"flag": "skip"})["trace"] == ["s"]
    assert graph.invoke({"flag": "anything"})["trace"] == ["s", "W"]


def test_an_unmapped_branch_key_without_a_default_is_an_error(registry):
    spec = WorkflowSpec.from_dict(
        {
            "nodes": [
                {"name": "start", "uses": "append", "config": {"label": "s"}},
                {"name": "work", "uses": "append", "config": {"label": "W"}},
            ],
            "edges": [["work", END]],
            "branches": [
                {"from": "start", "selector": "by_flag", "routes": {"known": "work"}}
            ],
        }
    )
    with pytest.raises(KeyError, match="not one of"):
        build_graph(spec, registry).invoke({"flag": "surprise"})


def test_a_node_cannot_have_both_an_edge_and_a_branch(registry):
    """The edge would always win, so the branch would silently never run."""
    spec = WorkflowSpec.from_dict(
        {
            "nodes": [
                {"name": "start", "uses": "append", "config": {"label": "s"}},
                {"name": "work", "uses": "append", "config": {"label": "W"}},
            ],
            "edges": [["start", "work"], ["work", END]],
            "branches": [
                {"from": "start", "selector": "by_flag", "routes": {"a": "work"}}
            ],
        }
    )
    with pytest.raises(ValueError, match="both an edge and a branch"):
        build_graph(spec, registry)


# -- dynamic fan-out --------------------------------------------------------------


@pytest.fixture
def fanout_registry():
    reg = NodeRegistry()

    @reg.node("plan")
    def plan(state):
        return {"workers": state.get("workers", [])}

    @reg.node("work")
    def work(state, config):
        item = state["worker"]
        return {"results": [{"who": item, "depth": config.get("depth", 0)}]}

    @reg.node("join")
    def join(state):
        return {"summary": sorted(r["who"] for r in state.get("results", []))}

    return reg


def _fanout_spec():
    return WorkflowSpec.from_dict(
        {
            "nodes": ["plan", "work", "join"],
            "entry": "plan",
            "edges": [["join", END]],
            "fanout": [
                {
                    "from": "plan",
                    "over": "workers",
                    "node": "work",
                    "join": "join",
                    "item_key": "worker",
                    "collect": "results",
                }
            ],
        }
    )


def test_fanout_runs_the_node_once_per_item(fanout_registry):
    """How many reviewers a task needs is decided while it runs."""
    graph = build_graph(_fanout_spec(), fanout_registry)
    final = graph.invoke({"workers": ["a", "b", "c"]})
    assert final["summary"] == ["a", "b", "c"]


def test_every_branch_result_survives_the_join(fanout_registry):
    """The reason collect keys need their own reducer.

    Parallel branches are reduced one update at a time, so a shared key with
    ordinary last-write-wins would end the run with one result instead of all.
    """
    graph = build_graph(_fanout_spec(), fanout_registry)
    final = graph.invoke({"workers": [f"w{i}" for i in range(12)]})
    assert len(final["results"]) == 12


def test_a_fanned_node_still_sees_earlier_state(fanout_registry):
    """A reviewer needs the repo path, not only its own item."""
    reg = NodeRegistry()

    @reg.node("plan")
    def plan(state):
        return {}

    @reg.node("work")
    def work(state):
        return {"results": [f"{state['worker']}@{state['repo_path']}"]}

    @reg.node("join")
    def join(state):
        return {"done": sorted(state["results"])}

    graph = build_graph(_fanout_spec(), reg)
    final = graph.invoke({"workers": ["a", "b"], "repo_path": "/repo"})
    assert final["done"] == ["a@/repo", "b@/repo"]


def test_an_empty_item_list_still_reaches_the_join(fanout_registry):
    """What an empty result means is the join's decision, not the router's."""
    graph = build_graph(_fanout_spec(), fanout_registry)
    final = graph.invoke({"workers": []})
    assert final["summary"] == []


def test_non_collected_keys_keep_last_write_wins(fanout_registry):
    """Appending everywhere would be surprising for an ordinary correction."""
    reg = NodeRegistry()

    @reg.node("plan")
    def plan(state):
        return {"note": "first"}

    @reg.node("work")
    def work(state):
        return {"results": [state["worker"]]}

    @reg.node("join")
    def join(state):
        return {"note": "last"}

    final = build_graph(_fanout_spec(), reg).invoke({"workers": ["a"]})
    assert final["note"] == "last"


def test_fanout_config_reaches_the_per_item_node():
    reg = NodeRegistry()

    @reg.node("plan")
    def plan(state):
        return {}

    @reg.node("work")
    def work(state, config):
        return {"results": [config["depth"]]}

    @reg.node("join")
    def join(state):
        return {}

    spec = WorkflowSpec.from_dict(
        {
            "nodes": [
                "plan",
                {"name": "work", "uses": "work", "config": {"depth": 7}},
                "join",
            ],
            "entry": "plan",
            "edges": [["join", END]],
            "fanout": [
                {"from": "plan", "over": "workers", "node": "work", "join": "join",
                 "item_key": "worker", "collect": "results"}
            ],
        }
    )
    assert build_graph(spec, reg).invoke({"workers": ["a", "b"]})["results"] == [7, 7]


def test_fanout_respects_max_parallel():
    reg = NodeRegistry()
    lock = threading.Lock()
    paired = threading.Event()
    active = 0
    peak = 0

    @reg.node("plan")
    def plan(state):
        return {}

    @reg.node("work")
    def work(state):
        # Each branch holds its slot until a second one joins it, so the peak
        # is two by construction rather than by timing: a sleep long enough to
        # overlap on an idle machine is not long enough on a loaded one, and
        # this test would then pass while measuring nothing.
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 2:
                paired.set()
        paired.wait(timeout=5)
        with lock:
            active -= 1
        return {"results": [state["worker"]]}

    @reg.node("join")
    def join(state):
        return {}

    spec = WorkflowSpec.from_dict(
        {
            "nodes": ["plan", "work", "join"],
            "entry": "plan",
            "edges": [["join", END]],
            "fanout": [
                {
                    "from": "plan",
                    "over": "workers",
                    "node": "work",
                    "join": "join",
                    "item_key": "worker",
                    "collect": "results",
                    "max_parallel": 2,
                }
            ],
        }
    )

    final = build_graph(spec, reg).invoke({"workers": list("abcde")})

    assert sorted(final["results"]) == list("abcde")
    assert peak == 2


def test_fanout_rejects_non_positive_max_parallel():
    with pytest.raises(WorkflowSpecError, match="max_parallel must be positive"):
        WorkflowSpec.from_dict(
            {
                "nodes": ["plan", "work", "join"],
                "fanout": [
                    {
                        "from": "plan",
                        "over": "workers",
                        "node": "work",
                        "join": "join",
                        "max_parallel": 0,
                    }
                ],
            }
        )


def test_a_fanout_to_an_unknown_node_is_refused():
    with pytest.raises(WorkflowSpecError, match="unknown node"):
        WorkflowSpec.from_dict(
            {
                "nodes": ["plan", "join"],
                "fanout": [{"from": "plan", "over": "x", "node": "ghost", "join": "join"}],
            }
        )


def test_a_fanout_whose_node_is_its_own_join_is_refused():
    with pytest.raises(WorkflowSpecError, match="same node"):
        WorkflowSpec.from_dict(
            {
                "nodes": ["plan", "work"],
                "fanout": [{"from": "plan", "over": "x", "node": "work", "join": "work"}],
            }
        )
