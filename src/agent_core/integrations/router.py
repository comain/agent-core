"""Endpoints for accepting triggers, so enabling a protocol is one line.

Having the adapters shared still left every product writing the same route
for each of them: read the body, verify it, parse it, decide whether it was
work at all, submit it, and answer in that caller's format. Six steps, the
same every time, and each one has a way of going quietly wrong -- a product
that forgets `verify` has an open endpoint, one that treats an ignorable
event as an error fills a pipeline's UI with red.

    router.include_router(
        create_trigger_router(
            [TriggerMount(rdc, "/api/v1/rdc/trigger")],
            submit=submit,
        )
    )

The product supplies one function: given a `Trigger`, start the work and say
where to watch it. Everything else is the protocol's.

Paths are explicit rather than derived from the protocol name, because a
product adopting this already has endpoints its callers post to, and a URL
that changes is a caller that stops working.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

try:
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - exercised by the import test
    raise ImportError(
        'agent_core.integrations.router requires FastAPI. '
        'Install it with: pip install "agent-core[api]"'
    ) from exc

from agent_core.integrations.protocol import Reply, Trigger, TriggerProtocol, VerificationFailed

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Accepted:
    """Where to watch the work a trigger started."""

    task_id: str
    task_url: str = ""
    report_url: str = ""


@dataclass(frozen=True)
class TriggerMount:
    """One protocol, and the path its caller posts to."""

    protocol: TriggerProtocol
    path: str
    #: Overrides the protocol's own name in logs. Two mounts of one protocol
    #: on different paths are otherwise indistinguishable.
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or getattr(self.protocol, "name", "") or "trigger"


#: `submit(trigger, request)` -> Accepted. The second argument is the raw
#: request, for a product that builds absolute URLs from the host it was
#: reached on.
SubmitFn = Callable[..., Accepted]


def create_trigger_router(
    mounts: Sequence[TriggerMount],
    *,
    submit: SubmitFn,
    prefix: str = "",
) -> APIRouter:
    """Mount an endpoint per protocol.

    Every route runs the same sequence, and the differences between callers
    live entirely in the protocol:

    - a request that fails verification is answered by the protocol and never
      reaches `submit`
    - an event the protocol does not consider work is answered as ignored,
      not as an error
    - anything raised while starting the work is answered in the caller's own
      format, because a caller that cannot read the error cannot act on it
    """
    router = APIRouter(prefix=prefix)

    for mount in mounts:
        _add_route(router, mount, submit)
    return router


def _add_route(router: APIRouter, mount: TriggerMount, submit: SubmitFn) -> None:
    protocol = mount.protocol
    label = mount.label
    wants_request = _wants_request(submit)

    @router.post(mount.path, name=f"trigger:{label}")
    async def handle(request: Request) -> JSONResponse:
        body = await request.body()
        headers = dict(request.headers)

        try:
            protocol.verify(body, headers)
        except VerificationFailed as exc:
            logger.warning("%s rejected an unverified request: %s", label, exc)
            return _respond(protocol.failed(exc))
        except Exception as exc:  # noqa: BLE001 - a protocol may raise its own
            logger.warning("%s could not verify a request: %s", label, exc)
            return _respond(protocol.failed(exc))

        try:
            trigger = protocol.parse(body, headers)
        except Exception as exc:  # noqa: BLE001 - malformed input is the caller's
            logger.warning("%s could not read a request: %s", label, exc)
            return _respond(protocol.failed(exc))

        if trigger is None:
            logger.info("%s ignored an event that is not work", label)
            return _respond(protocol.ignored())

        try:
            accepted = submit(trigger, request) if wants_request else submit(trigger)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, and logged
            logger.exception("%s could not start work", label)
            return _respond(protocol.failed(exc))

        return _respond(
            protocol.accepted(
                trigger,
                task_id=accepted.task_id,
                task_url=accepted.task_url,
                report_url=accepted.report_url,
            )
        )

    return None


def _wants_request(submit: SubmitFn) -> bool:
    """Whether the product's submit takes the raw request as well.

    Decided once from the signature rather than by calling and catching
    TypeError: that would also swallow a TypeError raised inside the
    product's own code and silently call it a second time.
    """
    try:
        parameters = inspect.signature(submit).parameters
    except (TypeError, ValueError):  # builtins and C callables have no signature
        return False
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters.values()):
        return True
    positional = [
        p
        for p in parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return len(positional) >= 2


def _respond(reply: Reply) -> JSONResponse:
    return JSONResponse(status_code=reply.status_code, content=reply.body)
