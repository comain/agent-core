"""Where node implementations are looked up by name.

The spec refers to steps by string. Something has to turn those strings into
callables, and a registry keeps that mapping explicit: a product registers the
steps it implements, agent-core registers the ones every product shares, and a
name that resolves to nothing is reported when the workflow is built.

A node is a plain callable taking ``(state, config)`` and returning the keys it
wants merged into the state. It is deliberately *not* a class hierarchy —
subclassing an abstract Node adds ceremony without adding a capability, and a
function is easier to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

#: A node receives the run state and its own configuration, and returns the
#: state keys it wants updated. Returning nothing is allowed and means "no
#: change" -- useful for a step that only has an effect elsewhere.
NodeFn = Callable[..., Optional[Mapping[str, Any]]]

#: A selector receives the state and returns the branch key to follow.
SelectorFn = Callable[..., str]


class UnknownNodeError(KeyError):
    """The workflow names a node or selector nobody registered."""

    def __init__(self, kind: str, name: str, known):
        self.kind = kind
        self.name = name
        super().__init__(
            f"no {kind} registered as {name!r}. Registered: {', '.join(sorted(known)) or '(none)'}"
        )


@dataclass
class NodeRegistry:
    """Named node and selector implementations."""

    nodes: Dict[str, NodeFn] = None  # type: ignore[assignment]
    selectors: Dict[str, SelectorFn] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.nodes = dict(self.nodes or {})
        self.selectors = dict(self.selectors or {})

    # -- registration ------------------------------------------------------

    def node(self, name: str) -> Callable[[NodeFn], NodeFn]:
        """Decorator registering a node implementation."""

        def register(fn: NodeFn) -> NodeFn:
            self.add_node(name, fn)
            return fn

        return register

    def selector(self, name: str) -> Callable[[SelectorFn], SelectorFn]:
        def register(fn: SelectorFn) -> SelectorFn:
            self.add_selector(name, fn)
            return fn

        return register

    def add_node(self, name: str, fn: NodeFn) -> None:
        if name in self.nodes and self.nodes[name] is not fn:
            raise ValueError(f"node {name!r} is already registered")
        self.nodes[name] = fn

    def add_selector(self, name: str, fn: SelectorFn) -> None:
        if name in self.selectors and self.selectors[name] is not fn:
            raise ValueError(f"selector {name!r} is already registered")
        self.selectors[name] = fn

    # -- lookup ------------------------------------------------------------

    def get_node(self, name: str) -> NodeFn:
        try:
            return self.nodes[name]
        except KeyError:
            raise UnknownNodeError("node", name, self.nodes) from None

    def get_selector(self, name: str) -> SelectorFn:
        try:
            return self.selectors[name]
        except KeyError:
            raise UnknownNodeError("selector", name, self.selectors) from None

    def extend(self, other: "NodeRegistry") -> "NodeRegistry":
        """A registry holding both, with ``other`` winning on a clash.

        This is how a product overrides a shared node -- keeping the workflow
        config unchanged while swapping the implementation underneath.
        """
        merged = NodeRegistry(dict(self.nodes), dict(self.selectors))
        merged.nodes.update(other.nodes)
        merged.selectors.update(other.selectors)
        return merged


#: Nodes agent-core provides to every consumer. Products should build their own
#: registry and `extend` this one rather than registering into it, so that one
#: product's node names cannot collide with another's.
shared_registry = NodeRegistry()
