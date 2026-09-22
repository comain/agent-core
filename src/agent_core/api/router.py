"""Shared task-service router.

The surface every consumer needs and none of them should write again: health,
task status, control mutations, live progress, and the approval inbox.

Requires the optional dependency::

    pip install "agent-core[api]"

FastAPI is not a hard dependency. One consumer serves its UI from stdlib
``http.server``, and it should not have to install a framework it never imports
in order to use the harness or the runtime.

## What is here and what is not

Two consumers independently converged on the same URL shapes -- ``/healthcheck.html``,
``/task-status/{task_id}``, ``/task-status/{task_id}/data``,
``/reports/{task_id}/index.html``, ``/api/v1/rdc/trigger`` are character-identical
between them -- so those paths are preserved rather than reinvented. A migrating
product keeps its existing URLs.

**Domain rendering stays with the product.** Findings tables, coverage reports,
and spec documents are what each product exists to produce; only the task-service
scaffolding around them is shared. Products mount this router and add their own.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

try:
    from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover - exercised by the import test
    raise ImportError(
        'agent_core.api.router requires FastAPI. Install it with: pip install "agent-core[api]"'
    ) from exc

from agent_core.identity import (
    ANSWER_GATE,
    CANCEL_GATE,
    MUTATE_TASK,
    AuthenticationError,
    AuthorizationError,
    IdentityResolver,
    Policy,
    Principal,
)
from agent_core.runtime import GateAlreadyAnswered, RuntimeStore, stream_task_events
from agent_core.ui import gate_to_dict, render_inbox


class TaskServicePorts:
    """Product-specific behaviour behind the shared routes.

    Everything is optional: a product mounts the router and supplies only what it
    has. A route whose port is missing returns 501 rather than 404, so the
    difference between "not implemented here" and "wrong URL" stays visible.
    """

    def __init__(
        self,
        *,
        get_task: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        list_tasks: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        request_control: Optional[Callable[..., Any]] = None,
        health: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self.get_task = get_task
        self.list_tasks = list_tasks
        self.request_control = request_control
        self.health = health


def _principal_dependency(resolver: IdentityResolver) -> Callable[[Request], Optional[Principal]]:
    def dependency(request: Request) -> Optional[Principal]:
        try:
            return resolver.resolve(dict(request.headers))
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    return dependency


def _authorize(policy: Policy, principal: Optional[Principal], action: str) -> Principal:
    try:
        return policy.authorize(principal, action)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def create_task_router(
    store: RuntimeStore,
    *,
    ports: Optional[TaskServicePorts] = None,
    resolver: Optional[IdentityResolver] = None,
    policy: Optional[Policy] = None,
    prefix: str = "",
    inbox_path: str = "/gates",
) -> APIRouter:
    """Build the shared router.

    ``prefix`` lets a product mount this under its existing base path without
    changing the URLs its users already have.
    """
    ports = ports or TaskServicePorts()
    resolver = resolver or IdentityResolver()
    policy = policy or Policy()
    router = APIRouter(prefix=prefix)
    principal_dep = _principal_dependency(resolver)

    def _require_port(port: Any, name: str) -> Any:
        if port is None:
            raise HTTPException(status_code=501, detail=f"{name} is not implemented by this service")
        return port

    # -- health ------------------------------------------------------------

    @router.get("/healthz")
    @router.get("/health")
    def health() -> Dict[str, Any]:
        payload = {"status": "ok"}
        if ports.health:
            try:
                payload.update(ports.health() or {})
            except Exception as exc:  # noqa: BLE001
                # A health endpoint that 500s tells a load balancer nothing
                # useful; report degradation instead.
                return {"status": "degraded", "error": str(exc)}
        return payload

    @router.get("/healthcheck.html", response_class=HTMLResponse)
    def healthcheck_html() -> str:
        return "<html><body>ok</body></html>"

    # -- tasks -------------------------------------------------------------

    @router.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str) -> Dict[str, Any]:
        record = _require_port(ports.get_task, "get_task")(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown task: {task_id}")
        return record

    @router.get("/api/v1/tasks")
    def list_tasks(
        limit: int = Query(50, ge=1, le=500),
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return _require_port(ports.list_tasks, "list_tasks")(limit=limit, status=status)

    # -- control mutations -------------------------------------------------

    @router.post("/api/v1/tasks/{task_id}/{action}")
    def control(
        task_id: str,
        action: str,
        reason: Optional[str] = None,
        principal: Optional[Principal] = Depends(principal_dep),
    ) -> Dict[str, Any]:
        if action not in ("stop", "cancel", "requeue"):
            raise HTTPException(status_code=404, detail=f"unknown action: {action}")
        actor = _authorize(policy, principal, MUTATE_TASK)

        # Check the task exists before recording anything. Writing the control
        # first turned a typo in a task id into a 500 and left an audit record
        # for a task that never existed.
        if ports.get_task is not None and ports.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown task: {task_id}")

        control_id = store.request_control(
            task_ref=task_id, action=action, reason=reason, requested_by=actor.subject
        )
        store.append_event(
            task_ref=task_id,
            event_type=f"task_{action}_requested",
            message=f"{action} requested by {actor.subject}",
            payload={"reason": reason},
        )
        if ports.request_control:
            try:
                ports.request_control(task_ref=task_id, action=action, reason=reason, principal=actor)
            except KeyError as exc:
                # A product that has no get_task port, or that races a delete,
                # signals an unknown task this way rather than by 500ing.
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"task_id": task_id, "action": action, "control_id": control_id, "requested_by": actor.subject}

    # -- live progress -----------------------------------------------------

    @router.get("/api/v1/tasks/{task_id}/events")
    def events(
        task_id: str,
        request: Request,
        after_id: int = Query(0, ge=0),
    ) -> StreamingResponse:
        if ports.get_task is not None and ports.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown task: {task_id}")
        # A reconnecting EventSource sends Last-Event-ID; honouring it here means
        # a client resumes exactly where it left off without extra bookkeeping.
        header = request.headers.get("last-event-id")
        if header and after_id == 0:
            try:
                after_id = int(header)
            except ValueError:
                pass
        return StreamingResponse(
            stream_task_events(store, task_ref=task_id, after_id=after_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # nginx buffers proxied responses by default, which holds frames
                # back and makes a live view look frozen.
                "X-Accel-Buffering": "no",
            },
        )

    # -- approval inbox ----------------------------------------------------

    @router.get(inbox_path, response_class=HTMLResponse)
    def inbox_page() -> str:
        gates = store.pending_gates()
        return render_inbox(gates, action_url_for=lambda g: f"{prefix}{inbox_path}/{g.gate_id}/answer")

    @router.get(f"{inbox_path}/data")
    def inbox_data() -> List[Dict[str, Any]]:
        return [gate_to_dict(g) for g in store.pending_gates()]

    @router.post(f"{inbox_path}/{{gate_id}}/answer")
    async def answer_gate(
        gate_id: str,
        request: Request,
        principal: Optional[Principal] = Depends(principal_dep),
    ) -> Response:
        actor = _authorize(policy, principal, ANSWER_GATE)

        # Accept both a form post from the shipped inbox page and a JSON body
        # from a scripted client.
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            payload: Any = await request.json()
        else:
            form = await request.form()
            payload = {k: v for k, v in form.items() if k != "gate_id"}

        try:
            gate = store.answer_gate(gate_id=gate_id, response=payload, principal=actor, policy=policy)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GateAlreadyAnswered as exc:
            # 409, not 400: the request was well formed and simply lost a race
            # with another reviewer.
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if "application/json" in content_type:
            return JSONResponse(gate_to_dict(gate))
        return Response(status_code=303, headers={"Location": f"{prefix}{inbox_path}"})

    @router.post(f"{inbox_path}/{{gate_id}}/cancel")
    def cancel_gate(
        gate_id: str, principal: Optional[Principal] = Depends(principal_dep)
    ) -> Dict[str, Any]:
        _authorize(policy, principal, CANCEL_GATE)
        return {"gate_id": gate_id, "cancelled": store.cancel_gate(gate_id)}

    # -- operations --------------------------------------------------------

    @router.get("/api/v1/admin/running")
    def running(
        stale_after_seconds: int = Query(120, ge=1, le=86_400),
    ) -> Dict[str, Any]:
        stale = {r["runner_id"] for r in store.stale_runners(older_than_seconds=stale_after_seconds)}
        conn = store.connect()
        try:
            rows = list(conn.execute("SELECT * FROM ac_runner_heartbeats ORDER BY heartbeat_at DESC"))
        finally:
            conn.close()
        return {
            "runners": [
                {
                    "runner_id": r["runner_id"],
                    "task_ref": r["task_ref"],
                    "status": r["status"],
                    "message": r["message"],
                    "heartbeat_at": r["heartbeat_at"],
                    "stale": r["runner_id"] in stale,
                }
                for r in rows
            ],
            "stale_count": len(stale),
        }

    return router
