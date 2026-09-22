"""Server-sent-events delivery for the task event log.

Framework-agnostic on purpose. agent-core is consumed by four products and
should not drag a web framework into all of them, so what lives here is the part
that is actually easy to get wrong -- wire format, resumption, and keepalive --
while each product supplies its own HTTP response object:

    # FastAPI / Starlette
    return StreamingResponse(
        stream_task_events(store, task_ref=ref, after_id=last_event_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

``X-Accel-Buffering: no`` matters: nginx buffers proxied responses by default,
which holds SSE frames until the buffer fills and makes a live progress view
look frozen.

Resumption is by event id. A browser reconnecting sends ``Last-Event-ID``; pass
it as ``after_id`` and the client receives exactly what it missed. This works
because the id is the autoincrement primary key -- monotonic, never reused, and
unaffected by the one-second resolution of the stored timestamps.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence

from agent_core.harness.registry import TurnProgress
from agent_core.runtime.progress import (
    PublicActivity,
    ProgressEnvelope,
    project_turn_kind,
    project_public_tool_activity,
    sanitize_public_progress_detail,
)

#: Sent when nothing has happened for a while. A comment frame is ignored by
#: EventSource but keeps the connection from being reaped by an idle proxy
#: timeout (nginx defaults to 60s).
_KEEPALIVE = ": keepalive\n\n"

#: Event types after which the stream should end. A progress view that never
#: closes leaks a connection per viewer per task.
DEFAULT_TERMINAL_TYPES = frozenset({"task_completed", "task_failed", "task_cancelled"})

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_CONTEXT_VALUE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_PRIVATE_CONTEXT_MARKERS = (
    "command",
    "content",
    "error",
    "message",
    "path",
    "prompt",
    "reason",
    "secret",
    "token",
)
_SAFE_TOOL_STATUSES = frozenset(
    {"started", "pending", "running", "completed", "failed", "error", "finished"}
)
def format_frame(*, event_id: Optional[int], event: Optional[str], data: Any) -> str:
    """Render one SSE frame.

    Multi-line payloads must be split across repeated ``data:`` lines -- a raw
    newline inside a data field silently truncates the frame at the newline.
    """
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event:
        lines.append(f"event: {event}")
    payload = data if isinstance(data, str) else json.dumps(data, default=str)
    for line in payload.split("\n"):
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def row_to_frame(row: Any) -> str:
    """Render a stored event row as an SSE frame."""
    payload: Dict[str, Any] = {
        "id": row["id"],
        "type": row["event_type"],
        "severity": row["severity"],
        "stage": row["stage"],
        "message": row["message"],
        "created_at": row["created_at"],
    }
    try:
        payload["payload"] = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload["payload"] = {}
    return format_frame(event_id=row["id"], event=row["event_type"], data=payload)


def stream_task_events(
    store: Any,
    *,
    task_ref: str,
    after_id: int = 0,
    poll_interval: float = 1.0,
    keepalive_interval: float = 15.0,
    max_duration: Optional[float] = 3600.0,
    terminal_types: Sequence[str] = tuple(DEFAULT_TERMINAL_TYPES),
    batch_size: int = 500,
    _sleep=time.sleep,
    _now=time.monotonic,
) -> Iterator[str]:
    """Yield SSE frames for a task, following the log until it terminates.

    Polling rather than push: SQLite has no usable change notification, and a
    one-second poll against an indexed ``(task_ref, id)`` lookup is cheap next to
    the model calls this is reporting on.

    ``max_duration`` bounds the connection. A viewer who leaves a tab open on a
    task that never reaches a terminal event would otherwise hold a connection
    and a poll loop forever; ``None`` disables the bound for callers that manage
    their own lifecycle.
    """
    terminal = set(terminal_types)
    started = _now()
    last_id = after_id
    last_emit = _now()

    while True:
        rows = store.events_since(task_ref=task_ref, after_id=last_id, limit=batch_size)
        for row in rows:
            yield row_to_frame(row)
            last_id = row["id"]
            last_emit = _now()
            if row["event_type"] in terminal:
                return

        if rows:
            # Drained a full batch: come straight back for the rest rather than
            # sleeping, or a burst is delivered at one batch per second.
            if len(rows) == batch_size:
                continue
        else:
            if _now() - last_emit >= keepalive_interval:
                yield _KEEPALIVE
                last_emit = _now()

        if max_duration is not None and _now() - started >= max_duration:
            yield format_frame(event_id=None, event="stream_timeout",
                               data={"reason": "max_duration reached", "last_id": last_id})
            return

        _sleep(poll_interval)


class HarnessEventBridge:
    """Turns OpenCode stream events into trusted diagnostic task events.

    This is what makes progress *tool-level* rather than stage-level: the
    harness already parses tool calls, reasoning updates, and step boundaries,
    and this forwards them into the event log an SSE client is tailing.

    This preserves model text, tool titles, and raw errors. Do not use it for a
    public stream; use :class:`RuntimeProgressPublisher` with the neutral
    harness ``on_progress`` callback instead.
    """

    #: OpenCode event types mapped to a severity for the log.
    _SEVERITY = {"error": "error"}

    def __init__(self, store: Any, *, task_ref: str, stage: Optional[str] = None, parser: Any = None):
        self.store = store
        self.task_ref = task_ref
        self.stage = stage
        if parser is None:
            from agent_core.harness.stream import OpenCodeStreamParser

            parser = OpenCodeStreamParser()
        self.parser = parser

    def handle_event(self, event: Dict[str, Any]) -> Optional[int]:
        """Record one parsed OpenCode event. Returns the event id, or None if skipped."""
        line = self.parser.progress_line(event)
        if not line:
            return None
        etype = str(event.get("type") or "unknown")
        return self.store.append_event(
            task_ref=self.task_ref,
            event_type=f"agent_{etype}",
            severity=self._SEVERITY.get(etype, "info"),
            stage=self.stage,
            message=line,
            payload={"opencode_type": etype},
        )

    def handle_line(self, raw: str) -> Optional[int]:
        """Record one raw JSONL line from the harness stream."""
        event = self.parser.parse_line(raw)
        return self.handle_event(event) if event else None


class RuntimeProgressPublisher:
    """Persist a public-safe projection of neutral agent progress.

    Raw model text, reasoning, tool titles/commands, and provider errors are
    intentionally never persisted. ``context`` is product-supplied public
    correlation data (for example a reviewer name), so callers must not put
    prompts, credentials, paths, or model output in it.
    """

    def __init__(
        self,
        store: Any,
        *,
        task_ref: str,
        stage: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.store = store
        self.task_ref = task_ref
        self.stage = self._safe_name(stage, "") or None
        self.context = self._safe_context(context or {})

    def __call__(self, progress: TurnProgress) -> Optional[int]:
        return self.publish(progress)

    @staticmethod
    def _safe_name(value: Optional[str], default: str) -> str:
        candidate = str(value or "")
        return candidate if _SAFE_NAME.fullmatch(candidate) else default

    @classmethod
    def _safe_context(cls, context: Mapping[str, Any]) -> Dict[str, Any]:
        safe: Dict[str, Any] = {}
        for raw_key, value in context.items():
            key = str(raw_key)
            lowered = key.lower()
            if not _SAFE_NAME.fullmatch(key) or any(
                marker in lowered for marker in _PRIVATE_CONTEXT_MARKERS
            ):
                continue
            if value is None or isinstance(value, (bool, int, float)):
                safe[key] = value
            elif _SAFE_CONTEXT_VALUE.fullmatch(str(value)):
                safe[key] = str(value)
        return safe

    def publish(self, progress: TurnProgress) -> Optional[int]:
        """Record one allowlisted update, or return ``None`` when it is private.

        The kind dispatch is shared with `project_turn_progress` rather than
        duplicated: a workflow node's progress and a streamed event describe
        the same activity, and two tables that must agree eventually will not.
        """
        projection = project_turn_kind(progress)
        if projection is None:
            return None

        envelope = ProgressEnvelope(
            kind=progress.kind,
            message=projection.message,
            activity=projection.activity,
            status=projection.status,
            detail=projection.detail,
            context=self.context,
            severity=projection.severity,
            projected=projection.projected,
        )
        return self.store.append_event(
            task_ref=self.task_ref,
            event_type="agent_progress",
            severity=envelope.severity,
            stage=self.stage,
            message=envelope.message,
            payload=envelope.payload(),
        )
