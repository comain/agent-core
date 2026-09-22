"""One typed shape for everything on the event stream.

The log is the contract between a run and anything watching it, and it was
being read by picking fields out of an untyped payload at each call site. Every
new kind of event meant another reader that knew, informally, which keys its
own producer happened to write -- and a viewer served by an older release had
no way to tell "an event I do not understand" from "an event that is wrong".

Two rules make that safe:

**A reader ignores what it does not know.** An unknown kind parses into the same
envelope with ``known=False`` rather than raising, so a page rendered by
yesterday's deploy keeps working against today's server, and a product may add
kinds without waiting for its viewers.

**Attribution and correlation are part of the envelope, not the payload.** Who
caused an event (``source``) and which piece of work it belongs to (``call_id``)
are questions every consumer asks, so they are asked once here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

#: Where an event came from. The distinction a reader needs is not which
#: component emitted it but who is answerable for it: the agent's own work, a
#: person's decision, or the machinery around both.
SOURCES = frozenset({"agent", "user", "environment"})

#: The kinds agent-core itself emits. A product adds its own through
#: ``register_kinds``; nothing here refuses a kind it has not been told about.
CORE_KINDS = frozenset(
    {
        "agent_progress",
        "gate_opened",
        "gate_resumed",
        "gate_resume_failed",
        "task_completed",
        "task_failed",
        "task_cancelled",
        "stream_timeout",
    }
)

_registered: set = set(CORE_KINDS)


def register_kinds(kinds: Iterable[str]) -> None:
    """Declare the kinds a product emits, so its own events read as known."""
    _registered.update(str(kind) for kind in kinds)


def known_kinds() -> frozenset:
    return frozenset(_registered)


@dataclass(frozen=True)
class Event:
    """One event, as a consumer sees it."""

    id: int
    kind: str
    message: str
    created_at: str
    severity: str = "info"
    source: str = "agent"
    stage: Optional[str] = None
    #: Groups the events of one piece of work -- a call and its result, or the
    #: branches of one fanned-out step.
    call_id: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)
    #: False for a kind this build has never heard of. Such an event is still
    #: delivered: a consumer that cannot render it should pass over it, not
    #: treat it as corrupt.
    known: bool = True

    @property
    def is_failure(self) -> bool:
        return self.severity == "error"


def _mapping(row: Any) -> Mapping[str, Any]:
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {key: row[key] for key in row.keys()}  # pragma: no cover - odd row types


def parse_event(row: Any) -> Event:
    """Read one stored row into an envelope, refusing nothing.

    A row whose payload will not parse yields an empty ``data`` rather than an
    exception: the event still happened, and losing the whole stream because
    one payload is malformed is the worse failure.
    """
    values = _mapping(row)
    raw = values.get("payload_json")
    if isinstance(raw, (dict, list)):
        data = raw if isinstance(raw, dict) else {}
    else:
        try:
            parsed = json.loads(raw or "{}")
            data = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            data = {}

    kind = str(values.get("event_type") or values.get("kind") or "")
    source = str(values.get("source") or "agent")
    return Event(
        id=int(values.get("id") or 0),
        kind=kind,
        message=str(values.get("message") or ""),
        created_at=str(values.get("created_at") or ""),
        severity=str(values.get("severity") or "info"),
        source=source if source in SOURCES else "agent",
        stage=values.get("stage") or None,
        call_id=values.get("call_id") or None,
        data=data,
        known=kind in _registered,
    )


def parse_events(rows: Iterable[Any]) -> Tuple[Event, ...]:
    return tuple(parse_event(row) for row in rows)


def group_by_call(events: Sequence[Event]) -> Dict[str, Tuple[Event, ...]]:
    """The events of each piece of work, in order, keyed by ``call_id``.

    What turns a flat log into an account of what happened: the call and its
    result together, and one agent's line of work separable from the five
    running beside it. Events with no call id are not grouped -- they belong to
    the run rather than to any one piece of it.
    """
    grouped: Dict[str, list] = {}
    for event in events:
        if not event.call_id:
            continue
        grouped.setdefault(event.call_id, []).append(event)
    return {call: tuple(items) for call, items in grouped.items()}
