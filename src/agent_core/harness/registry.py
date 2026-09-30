"""Choosing which agent runs a turn.

The shared step already takes any object with `run_turn` — that seam has
existed since it was extracted. What did not exist was a way to *name* one,
so every consumer imported a concrete class and a second implementation
meant editing product code.

A harness is registered under a name and asked for through one neutral
specification, so which agent a deployment uses becomes configuration:

    harness = create_configured_harness(HarnessSpec(name="pi", options={...}))

Adding one is registering it. Nothing in a product changes, because nothing
in a product names it.

`OpenCodeHarness` is not registered here: this module deliberately imports no
implementation, so a consumer using a different agent does not pay for
OpenCode's dependencies. `agent_core.harness` registers the built-in one on
import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)


@runtime_checkable
class Harness(Protocol):
    """Whatever runs one turn and reports what came back.

    The contract is deliberately narrow. Everything the shared step needs is
    here, and everything else -- how a process is spawned, how a stream is
    parsed, how a provider chain is walked -- is the implementation's own
    business.

    ``run_turn`` returns an object carrying, at minimum: ``type`` (``
    "completed"`` when the agent finished normally), ``result`` (the text it
    produced), ``session_id``, ``tokens``, ``cost_usd``, ``model_id`` and
    optionally ``error`` and ``raw_log_path``. `agent_core.harness.TurnResult`
    is that shape; an implementation may return its own as long as it answers
    to the same attributes.

    Implementations also accept an optional ``on_progress`` callback and emit
    :class:`TurnProgress`. Provider-native callbacks remain adapter internals;
    products never need to know which agent implementation was selected.
    """

    def run_turn(
        self,
        *,
        prompt_file: Path,
        repo_path: Path,
        **kwargs: Any,
    ) -> Any:
        """Run one turn against a prepared workspace."""


@runtime_checkable
class PolicyEnforcingHarness(Protocol):
    """Optional adapter capability for enforcing portable execution policy."""

    def accepts_policy(self, spec: "HarnessSpec") -> bool:
        """Return true only when the adapter enforces the supplied policy."""


@dataclass(frozen=True)
class TurnProgress:
    """Implementation-neutral progress emitted while an agent turn runs.

    ``message`` is intended for trusted logs and adapters. Products exposing
    progress publicly must project it through a sanitizer such as
    :class:`agent_core.runtime.RuntimeProgressPublisher`; model text, reasoning,
    commands, and provider errors may contain repository data or credentials.
    """

    kind: str
    message: str
    tool: Optional[str] = None
    status: Optional[str] = None
    detail: Optional[str] = None


class UnknownHarnessError(LookupError):
    """A harness was asked for by a name nobody registered."""


@dataclass(frozen=True)
class HarnessSpec:
    """Implementation-neutral configuration supplied by a product.

    ``options`` is deliberately opaque to the product-facing registry. The
    selected implementation owns those keys, while timeout, permissions,
    readable directories, and cache placement remain portable policy.

    ``readiness`` is how a deployment says whether this harness has to prove it
    can reach a provider before work starts. ``None`` -- what every existing
    construction produces -- declares nothing, and the readiness helper then
    insists on the capability rather than assuming success. ``"not_required"``
    is an explicit statement that a local or offline harness needs no probe.
    ``"probe"`` states the requirement even when the harness object is built
    elsewhere, and is refused here if the built harness cannot answer, so the
    mismatch is a construction error rather than a startup surprise.
    """

    name: str
    options: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 3600
    permissions: Mapping[str, Any] = field(default_factory=dict)
    readable_dirs: Tuple[Path, ...] = ()
    cache_dir: str = ".agent_cache"
    readiness: Optional[Literal["probe", "not_required"]] = None

    def __post_init__(self) -> None:
        if self.readiness is not None and self.readiness not in ("probe", "not_required"):
            raise ValueError(
                f"readiness must be 'probe', 'not_required', or None; got {self.readiness!r}"
            )


_FACTORIES: Dict[str, Callable[..., Harness]] = {}


def register_harness(name: str, factory: Callable[..., Harness], *, replace: bool = False) -> None:
    """Make an implementation available under a name.

    Registering the same name twice is refused unless ``replace`` is passed:
    silently winning would make which implementation runs depend on import
    order, which is not a thing anyone should have to debug.
    """
    key = _normalise(name)
    if key in _FACTORIES and not replace:
        raise ValueError(
            f"a harness is already registered as {name!r}; pass replace=True to override it"
        )
    _FACTORIES[key] = factory


def create_harness(name: str, *args: Any, **kwargs: Any) -> Harness:
    """Build the harness registered under ``name``.

    Positional and keyword arguments are handed to the factory untouched. This
    is the low-level extension API; products normally use
    :func:`create_configured_harness`.
    """
    key = _normalise(name)
    try:
        factory = _FACTORIES[key]
    except KeyError:
        raise UnknownHarnessError(
            f"no harness registered as {name!r}; available: {available_harnesses() or ['(none)']}"
        ) from None
    return factory(*args, **kwargs)


def create_configured_harness(spec: HarnessSpec) -> Harness:
    """Build the selected implementation from one neutral specification.

    The spec's readiness declaration travels with the harness it created, so a
    product asking `check_harness_readiness` later gets the answer its own
    configuration gave rather than a guess. Imported here rather than at module
    scope so this module keeps importing nothing but the standard library.
    """
    from agent_core.harness.lifecycle import ReadinessCheckingHarness, declare_readiness

    harness = create_harness(spec.name, spec)
    restricted = bool(spec.permissions or spec.readable_dirs)
    if restricted and (
        not isinstance(harness, PolicyEnforcingHarness)
        or not harness.accepts_policy(spec)
    ):
        raise ValueError(
            f"harness {spec.name!r} cannot enforce the requested execution policy"
        )
    if spec.readiness == "probe" and not isinstance(harness, ReadinessCheckingHarness):
        raise ValueError(
            f"harness {spec.name!r} was declared readiness='probe' but cannot check "
            "readiness; declare 'not_required' or implement check_readiness"
        )
    declare_readiness(harness, spec.readiness)
    return harness


def available_harnesses() -> List[str]:
    """Registered names, for an error message or a `--help` listing."""
    return sorted(_FACTORIES)


def unregister_harness(name: str) -> None:
    """Remove a registration. For tests; a running service has no use for it."""
    _FACTORIES.pop(_normalise(name), None)


def preferred_model_of(harness: Any, repo_path: Optional[Path] = None) -> Optional[str]:
    """Which model this harness would use for a turn started now, if it knows.

    Callers need it to attribute the cost of a turn the provider did not
    attribute. It is deliberately *not* part of the `Harness` contract: an
    implementation that does not choose between models has nothing to say, and
    requiring it would make every such implementation carry a stub.

    Returns None when the harness does not answer, which callers should treat
    as "price it against whatever the turn reports, or not at all".
    """
    answer = getattr(harness, "preferred_model", None)
    if answer is None:
        return None
    return answer(repo_path) if repo_path is not None else answer()


def _normalise(name: str) -> str:
    return str(name or "").strip().lower()
