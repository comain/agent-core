"""Choosing which outside system a request came from.

Same shape as the harness registry, for the same reason: a caller is
registered under a name and asked for by name, so supporting another one is
registering an adapter rather than editing the service.

Inbound needs one thing the harness registry does not -- given a request,
*which* protocol should handle it. A service usually knows from the route it
arrived on, which is why `create_protocol` exists at all; `protocol_for`
covers the case where several callers share an endpoint and the answer is in
the headers.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from agent_core.integrations.protocol import TriggerProtocol


class UnknownProtocolError(LookupError):
    """A protocol was asked for by a name nobody registered."""


_FACTORIES: Dict[str, Callable[..., TriggerProtocol]] = {}


def register_protocol(
    name: str, factory: Callable[..., TriggerProtocol], *, replace: bool = False
) -> None:
    """Make an adapter available under a name."""
    key = _normalise(name)
    if key in _FACTORIES and not replace:
        raise ValueError(
            f"a protocol is already registered as {name!r}; pass replace=True to override it"
        )
    _FACTORIES[key] = factory


def create_protocol(name: str, *args: Any, **kwargs: Any) -> TriggerProtocol:
    """Build the adapter registered under ``name``."""
    key = _normalise(name)
    try:
        factory = _FACTORIES[key]
    except KeyError:
        raise UnknownProtocolError(
            f"no protocol registered as {name!r}; available: {available_protocols() or ['(none)']}"
        ) from None
    return factory(*args, **kwargs)


def protocol_for(
    headers: Mapping[str, str], candidates: Optional[List[TriggerProtocol]] = None
) -> Optional[TriggerProtocol]:
    """The first candidate that recognises these headers, if any.

    For endpoints several callers share. A protocol opts in by implementing
    `recognises(headers)`; one that does not is never selected this way, which
    keeps a protocol that only ever has its own route from having to care.
    """
    for protocol in candidates or []:
        recognises = getattr(protocol, "recognises", None)
        if recognises is not None and recognises(headers):
            return protocol
    return None


def available_protocols() -> List[str]:
    return sorted(_FACTORIES)


def unregister_protocol(name: str) -> None:
    """Remove a registration. For tests."""
    _FACTORIES.pop(_normalise(name), None)


def _normalise(name: str) -> str:
    return str(name or "").strip().lower()
