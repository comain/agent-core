"""What a workflow *is*, as data.

A product's workflow is a list of steps, the order they run in, and where it
branches. That is configuration, not code — the code is the individual steps.
Keeping the shape in data is what lets a new consumer describe its pipeline
without writing an orchestrator, and lets an existing one reorder or disable a
step without a release.

The spec is validated when it is built, not when the graph runs. A workflow
that names a node nobody registered, or an edge into a node that does not
exist, is a configuration error worth catching at startup rather than an
AttributeError twenty minutes into a task.
"""

from __future__ import annotations

import json
from pathlib import Path

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

#: Reserved target meaning "the workflow is finished".
END = "__end__"


class WorkflowSpecError(ValueError):
    """The workflow description is not usable."""


def _load_yaml(text: str, path: "Path") -> Any:
    """Parse YAML, saying what to install if it is missing.

    Imported here rather than at module scope so a consumer writing its flows
    in JSON, or in Python, never needs the dependency at all.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise WorkflowSpecError(
            f"reading {str(path)!r} needs PyYAML: pip install 'agent-core[yaml]'"
        ) from exc
    return yaml.safe_load(text)


@dataclass(frozen=True)
class NodeSpec:
    """One step.

    ``uses`` names a registered implementation; ``name`` is this workflow's
    label for it. They differ whenever a workflow runs the same implementation
    twice with different configuration -- two review passes, say -- which is
    exactly why the indirection exists.
    """

    name: str
    uses: str
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BranchSpec:
    """A conditional edge.

    ``selector`` is a registered function returning a key; ``routes`` maps that
    key to the next node. ``default`` covers keys the map does not name, and
    without one an unmapped key is an error rather than a silent stop.
    """

    source: str
    selector: str
    routes: Dict[str, str]
    default: Optional[str] = None


@dataclass(frozen=True)
class FanoutSpec:
    """Run one node once per item, then join.

    How many reviewers, test targets or files a task needs is decided while it
    runs, so the work cannot be a fixed set of nodes in the graph. The count
    comes from ``over`` -- a state key holding a list -- and each item gets its
    own invocation of ``node``.

    ``collect`` names the state key the per-item results accumulate into. It
    needs its own reducer: parallel branches all writing one key would
    otherwise overwrite each other, and the run would end with one result
    instead of all of them.
    """

    source: str
    over: str
    node: str
    join: str
    item_key: str = "item"
    collect: str = "fanout_results"
    #: Most branches to run at once. LangGraph starts every Send immediately,
    #: and when a branch spawns an agent process that means one process per
    #: item -- fine for three reviewers, not for thirty test targets. None
    #: leaves it unbounded.
    max_parallel: Optional[int] = None


@dataclass(frozen=True)
class WorkflowSpec:
    name: str
    entry: str
    nodes: List[NodeSpec]
    edges: List[tuple] = field(default_factory=list)
    branches: List[BranchSpec] = field(default_factory=list)
    fanouts: List[FanoutSpec] = field(default_factory=list)

    @property
    def accumulating_keys(self) -> frozenset:
        """State keys that must append rather than overwrite."""
        return frozenset(f.collect for f in self.fanouts)

    @property
    def node_names(self) -> List[str]:
        return [n.name for n in self.nodes]

    def node(self, name: str) -> NodeSpec:
        for candidate in self.nodes:
            if candidate.name == name:
                return candidate
        raise WorkflowSpecError(f"no such node: {name}")

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "WorkflowSpec":
        """Load a workflow from YAML or JSON.

        A flow is data -- which steps, in what order, branching on what -- so
        it can live in a file a reader can open without following imports, and
        a deployment can change without a code change.

        YAML is the reason this is not JSON-only: the decisions in a flow need
        explaining ("this reviewer runs before that gate, because..."), and
        JSON has nowhere to put a comment.

        Errors name the file. A workflow that fails to load names a path, not
        a dictionary that could have come from anywhere.
        """
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowSpecError(f"cannot read workflow {str(path)!r}: {exc}") from exc

        suffix = path.suffix.lower()
        try:
            if suffix in (".yaml", ".yml"):
                data = _load_yaml(text, path)
            elif suffix == ".json":
                data = json.loads(text)
            else:
                raise WorkflowSpecError(
                    f"unsupported workflow format {suffix!r} for {str(path)!r}; use .yaml, .yml or .json"
                )
        except json.JSONDecodeError as exc:
            raise WorkflowSpecError(f"invalid JSON in workflow {str(path)!r}: {exc}") from exc

        if not isinstance(data, Mapping):
            raise WorkflowSpecError(
                f"workflow {str(path)!r} must be a mapping at the top level, got {type(data).__name__}"
            )
        try:
            return cls.from_dict(data)
        except WorkflowSpecError as exc:
            raise WorkflowSpecError(f"{str(path)}: {exc}") from exc

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowSpec":
        if not isinstance(data, Mapping):
            raise WorkflowSpecError("workflow must be a mapping")
        name = str(data.get("name") or "workflow")

        raw_nodes = data.get("nodes")
        if not raw_nodes:
            raise WorkflowSpecError("workflow has no nodes")
        nodes: List[NodeSpec] = []
        for item in raw_nodes:
            if isinstance(item, str):
                # Shorthand: a step whose label is its implementation.
                nodes.append(NodeSpec(name=item, uses=item))
                continue
            if "name" not in item and "uses" not in item:
                raise WorkflowSpecError(f"node needs a name or uses: {item!r}")
            node_name = str(item.get("name") or item["uses"])
            nodes.append(
                NodeSpec(
                    name=node_name,
                    uses=str(item.get("uses") or node_name),
                    config=dict(item.get("config") or {}),
                )
            )

        seen = set()
        for node in nodes:
            if node.name in seen:
                raise WorkflowSpecError(f"duplicate node name: {node.name}")
            seen.add(node.name)

        entry = str(data.get("entry") or nodes[0].name)
        if entry not in seen:
            raise WorkflowSpecError(f"entry names an unknown node: {entry}")

        edges = [tuple(e) if not isinstance(e, Mapping) else (e["from"], e["to"])
                 for e in (data.get("edges") or [])]

        branches = [
            BranchSpec(
                source=str(b["from"]),
                selector=str(b["selector"]),
                routes={str(k): str(v) for k, v in (b.get("routes") or {}).items()},
                default=str(b["default"]) if b.get("default") else None,
            )
            for b in (data.get("branches") or [])
        ]

        fanouts = [
            FanoutSpec(
                source=str(f["from"]),
                over=str(f["over"]),
                node=str(f["node"]),
                join=str(f["join"]),
                item_key=str(f.get("item_key") or "item"),
                collect=str(f.get("collect") or "fanout_results"),
                max_parallel=(
                    int(f["max_parallel"])
                    if f.get("max_parallel") is not None
                    else None
                ),
            )
            for f in (data.get("fanout") or data.get("fanouts") or [])
        ]

        spec = cls(
            name=name, entry=entry, nodes=nodes, edges=edges,
            branches=branches, fanouts=fanouts,
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        known = set(self.node_names) | {END}
        for source, target in self.edges:
            if source not in known:
                raise WorkflowSpecError(f"edge starts at an unknown node: {source}")
            if target not in known:
                raise WorkflowSpecError(f"edge ends at an unknown node: {target}")
        for branch in self.branches:
            if branch.source not in known:
                raise WorkflowSpecError(f"branch starts at an unknown node: {branch.source}")
            for key, target in branch.routes.items():
                if target not in known:
                    raise WorkflowSpecError(
                        f"branch {branch.source}[{key}] targets an unknown node: {target}"
                    )
            if branch.default is not None and branch.default not in known:
                raise WorkflowSpecError(
                    f"branch {branch.source} default targets an unknown node: {branch.default}"
                )
        self._validate_fanouts(known)

    def _validate_fanouts(self, known) -> None:
        for fan in self.fanouts:
            for label, value in (("source", fan.source), ("node", fan.node), ("join", fan.join)):
                if value not in known:
                    raise WorkflowSpecError(
                        f"fanout {label} names an unknown node: {value}"
                    )
            if fan.node == fan.join:
                raise WorkflowSpecError(
                    f"fanout node and join are the same node: {fan.node}"
                )
            if fan.max_parallel is not None and fan.max_parallel <= 0:
                raise WorkflowSpecError("fanout max_parallel must be positive")

    def linear(self) -> "WorkflowSpec":
        """Fill in the edges of a straight-line workflow.

        The common case by far: steps run in the order listed and the last one
        ends the run. Writing those edges out adds nothing.
        """
        if self.edges or self.branches or self.fanouts:
            return self
        names = self.node_names
        edges = [(a, b) for a, b in zip(names, names[1:])] + [(names[-1], END)]
        return WorkflowSpec(
            name=self.name, entry=self.entry, nodes=self.nodes, edges=edges,
            branches=[], fanouts=[],
        )
