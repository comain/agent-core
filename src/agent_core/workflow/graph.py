"""Building a runnable graph from a spec and a registry.

Requires the optional dependency::

    pip install "agent-core[langgraph]"

The state is a dict, but *not* a bare ``dict`` schema. LangGraph treats that as
a single value and **replaces** the whole state with whatever a node returns, so
one step returning one key silently discards everything earlier steps produced.
Measured, not assumed: a graph seeded with ``{"keep": "me"}`` whose node returns
``{"x": 1}`` ends with ``{"x": 1}``. The schema below merges instead, which is
what a workflow state has to do when its keys come from configuration and no
TypedDict can name them in advance.

Node functions are adapted rather than passed through. LangGraph calls a node
with ``(state, config)``; a product node also needs its YAML configuration and
must not import LangGraph. The wrapper merges the two: YAML keys stay, the
runtime ``configurable`` (thread id) is copied in, and ``graph_node`` is the
YAML *name* so two steps that ``uses`` the same implementation do not share an
id stem. A node that ignores config still takes only ``state``.
"""

from __future__ import annotations

import inspect
import logging
import threading
from typing import Annotated, Any, Callable, Dict, Mapping, Optional

try:
    from langgraph.graph import END as LG_END
    from langgraph.graph import StateGraph
    from langgraph.types import Send
except ImportError as exc:  # pragma: no cover - exercised by the import test
    raise ImportError(
        "agent_core.workflow.graph requires LangGraph. "
        'Install it with: pip install "agent-core[langgraph]"'
    ) from exc

from agent_core.workflow.registry import NodeRegistry
from agent_core.workflow.spec import END, BranchSpec, FanoutSpec, WorkflowSpec

logger = logging.getLogger(__name__)


def _merge_state(previous: Optional[Mapping[str, Any]], update: Optional[Mapping[str, Any]]):
    """Later keys win; everything else survives."""
    return {**(previous or {}), **(update or {})}


def _state_reducer(accumulate: frozenset):
    """Merge state, but *append* under the keys a fan-out collects into.

    Parallel branches are reduced one update at a time, so a fanned-out node
    writing its result under a shared key would overwrite the previous branch's
    -- the run would finish with one reviewer's findings instead of every
    reviewer's. Only the collect keys behave this way; everything else keeps
    last-write-wins, because a step correcting an earlier value is the normal
    case and appending there would be surprising.
    """
    if not accumulate:
        return _merge_state

    def merge(previous, update):
        previous = previous or {}
        update = update or {}
        merged = dict(previous)
        for key, value in update.items():
            if key in accumulate:
                existing = list(previous.get(key) or [])
                merged[key] = existing + (list(value) if isinstance(value, list) else [value])
            else:
                merged[key] = value
        return merged

    return merge


#: The default state: a dict whose updates merge rather than replace.
WorkflowState = Annotated[Dict[str, Any], _merge_state]


def _bind(
    fn: Callable,
    config: Mapping[str, Any],
    *,
    context: Optional[Mapping[str, Any]] = None,
    graph_node: str = "",
):
    """Adapt a node to LangGraph's ``(state, config)`` call.

    A node may take just the state, or the state plus any of ``config`` and
    ``context``. Accepting all three shapes keeps trivial nodes trivial -- a
    step that ignores configuration should not have to declare it.
    """
    params = set(inspect.signature(fn).parameters)
    yaml_config = dict(config)
    bound_context = dict(context or {})

    def call(state, config=None):
        extra: Dict[str, Any] = {}
        if "config" in params:
            merged = dict(yaml_config)
            merged["graph_node"] = graph_node
            if isinstance(config, Mapping):
                configurable = config.get("configurable")
                if configurable is not None:
                    merged["configurable"] = dict(configurable)
            extra["config"] = merged
        if "context" in params:
            extra["context"] = bound_context
        return fn(state, **extra)

    call.__name__ = getattr(fn, "__name__", "node")
    return call


def _limited(fn: Callable, semaphore: "threading.Semaphore"):
    """Hold a slot for the duration of one fanned invocation."""

    def call(state, config=None):
        with semaphore:
            return fn(state, config)

    call.__name__ = getattr(fn, "__name__", "node")
    return call


def _branch_router(branch: BranchSpec, selector: Callable):
    routes = dict(branch.routes)
    default = branch.default

    def route(state):
        key = str(selector(state))
        target = routes.get(key, default)
        if target is None:
            raise KeyError(
                f"branch at {branch.source!r} produced {key!r}, "
                f"which is not one of {sorted(routes)} and there is no default"
            )
        logger.debug("branch %s -> %s (key=%s)", branch.source, target, key)
        return LG_END if target == END else target

    return route


def _fanout_router(fan: FanoutSpec):
    """Turn the item list into one invocation of the node per item.

    Each invocation gets the whole state plus its item, so a fanned node can
    still read what earlier steps produced -- the repo path, the commit -- and
    does not need everything threaded through the item.
    """

    def route(state):
        items = state.get(fan.over) or []
        if not items:
            # Nothing to fan out over. Go straight to the join, which still has
            # to run: deciding what an empty result means is its business.
            return [fan.join]
        return [Send(fan.node, {**state, fan.item_key: item}) for item in items]

    return route


def build_graph(
    spec: WorkflowSpec,
    registry: NodeRegistry,
    *,
    context: Optional[Mapping[str, Any]] = None,
    checkpointer: Any = None,
    state_schema: Any = None,
):
    """Compile ``spec`` into a runnable LangGraph.

    ``context`` is what the whole run shares and no step should have to receive
    through the state -- a database handle, the settings, a cancellation check.
    It is bound at build time, so it never has to be serialisable, which state
    passing through a checkpointer does.

    Every name is resolved here rather than at run time: a workflow naming a
    node nobody implemented is a configuration error, and finding it when the
    graph is built is the difference between a failed startup and a task that
    dies halfway through.
    """
    spec = spec.linear()
    if state_schema is None:
        state_schema = Annotated[Dict[str, Any], _state_reducer(spec.accumulating_keys)]
    graph = StateGraph(state_schema)

    # A fanned node may need a ceiling: every Send starts at once, so an
    # unbounded fan-out over a node that spawns an agent process spawns one per
    # item. The semaphore is created here, per graph, which is per task.
    limits = {
        f.node: threading.Semaphore(f.max_parallel)
        for f in spec.fanouts
        if f.max_parallel
    }

    for node in spec.nodes:
        bound = _bind(
            registry.get_node(node.uses),
            node.config,
            context=context,
            graph_node=node.name,
        )
        graph.add_node(node.name, _limited(bound, limits[node.name]) if node.name in limits else bound)

    graph.set_entry_point(spec.entry)

    branch_sources = {b.source for b in spec.branches} | {f.source for f in spec.fanouts}
    for source, target in spec.edges:
        if source in branch_sources:
            # A node cannot have both an unconditional edge and a branch: the
            # unconditional one would always win and the branch never run.
            raise ValueError(
                f"node {source!r} has both an edge and a branch; remove one"
            )
        graph.add_edge(source, LG_END if target == END else target)

    for branch in spec.branches:
        router = _branch_router(branch, registry.get_selector(branch.selector))
        targets = set(branch.routes.values()) | ({branch.default} if branch.default else set())
        graph.add_conditional_edges(
            branch.source,
            router,
            {t: (LG_END if t == END else t) for t in targets},
        )

    for fan in spec.fanouts:
        graph.add_conditional_edges(fan.source, _fanout_router(fan), [fan.node, fan.join])
        graph.add_edge(fan.node, fan.join)

    return graph.compile(checkpointer=checkpointer)
