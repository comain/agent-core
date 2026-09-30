"""What an agent is doing, in a form that is safe to show a person.

Two halves of one contract live here on purpose.

The **projection** turns a provider's raw tool traffic into a stable,
provider-neutral activity, and refuses or redacts anything that looks private.
It grew inside `runtime/sse.py`, but it is not a transport concern: workflow
nodes report phase progress with no event stream involved, and a second
implementation of a *redaction* boundary is the worst kind to have two of --
the copies drift, and the drift is invisible until something private is
already on a screen. `sse.py` now imports it from here.

The **reader** groups stored events back into per-session timelines. Keeping
it in the same module as the writer's envelope is what makes a renamed field
fail a test instead of silently emptying a UI.

Nothing here is authoritative. Progress is for a human watching; a product's
own database remains the record of what actually happened.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


_TOOL_ACTIVITIES = {
    "code_review": frozenset({"read", "grep", "glob", "list", "ls", "lsp"}),
    "repository_checks": frozenset({"bash", "shell", "exec", "terminal"}),
    "reference_research": frozenset({"webfetch", "websearch", "web_search", "fetch"}),
    "workspace_update": frozenset({"apply_patch", "patch", "edit", "write"}),
    "delegated_analysis": frozenset({"task", "delegate", "subagent"}),
    "planning": frozenset({"todo", "plan"}),
}

_ACTIVITY_MESSAGES = {
    "code_review": "Reviewing code and context",
    "repository_checks": "Running repository checks",
    "reference_research": "Consulting references",
    "workspace_update": "Updating the workspace",
    "delegated_analysis": "Running delegated analysis",
    "planning": "Organizing the analysis",
    "agent_work": "Working through the review",
}

_PUBLIC_STATUSES = {
    "started": "running",
    "pending": "running",
    "running": "running",
    "completed": "completed",
    "finished": "completed",
    "failed": "issue",
    "error": "issue",
}

_TOOL_MESSAGES = {
    "read": "Read project context",
    "grep": "Searched the repository",
    "glob": "Located relevant files",
    "list": "Listed project context",
    "ls": "Listed project context",
    "lsp": "Inspected code structure",
    "bash": "Ran repository checks",
    "shell": "Ran repository checks",
    "exec": "Ran repository checks",
    "terminal": "Ran repository checks",
    "webfetch": "Consulted a reference",
    "websearch": "Researched a reference",
    "web_search": "Researched a reference",
    "fetch": "Consulted a reference",
    "apply_patch": "Updated workspace files",
    "patch": "Updated workspace files",
    "edit": "Updated workspace files",
    "write": "Updated workspace files",
    "task": "Ran delegated analysis",
    "delegate": "Ran delegated analysis",
    "subagent": "Ran delegated analysis",
    "todo": "Updated the analysis plan",
    "plan": "Updated the analysis plan",
}
_REPO_PATH = re.compile(
    r"(?:^|/)((?:src|tests?|docs?|lib|packages|config|scripts)/"
    r"[A-Za-z0-9_.@/+:-]+)"
)
_ABSOLUTE_PATH = re.compile(r"(?<![\w.])/(?:[A-Za-z0-9_.@+-]+/)+[A-Za-z0-9_.@+-]+")
_TOOL_ABSOLUTE_PATH = re.compile(r"/(?:[A-Za-z0-9_.@+-]+/)+[A-Za-z0-9_.@+-]+")
_URL = re.compile(r"\b(?:https?|ssh|git)://\S+|\bgit@[^\s:]+:[^\s]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|authorization)"
    r"\s*([:=])\s*[^\s,;]+"
)
_LONG_OPAQUE_VALUE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]+|[A-Fa-f0-9]{32,})\b")
_COMMAND_LIKE = re.compile(
    r"(?i)^(?:cat|git|rg|grep|bash|sh|python\d*|pytest|npm|yarn|pnpm|curl|"
    r"sed|awk|find|ls|cd|make|mvn|gradle)\s"
)
_SAFE_MODEL_TRANSITION = re.compile(
    r"^[A-Za-z0-9_.+:/-]{1,160} -> [A-Za-z0-9_.+:/-]{1,160}$"
)


@dataclass(frozen=True)
class PublicActivity:
    """A provider-neutral activity safe and useful enough for a person."""

    code: str
    message: str
    status: str
    detail: Optional[str] = None


def sanitize_public_progress_detail(value: Optional[str]) -> Optional[str]:
    """Return one bounded synopsis with obvious private material removed.

    This is intentionally a synopsis boundary, not a general-purpose secret
    scanner. Structured output, code fences, and command-like lines are
    refused rather than partially exposed.
    """
    text = " ".join(str(value or "").split()).strip()
    if not text or text.startswith(("{", "[", "```", "$ ")):
        return None
    if _COMMAND_LIKE.match(text):
        return None
    text = _URL.sub("[link]", text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    text = _LONG_OPAQUE_VALUE.sub("[redacted]", text)
    text = _ABSOLUTE_PATH.sub("[path]", text)
    text = text[:180].rstrip()
    return text or None


def _public_tool_detail(tool: str, value: Optional[str]) -> Optional[str]:
    raw = " ".join(str(value or "").split()).strip()
    if not raw:
        return None
    if path := _REPO_PATH.search(raw):
        return path.group(1).rstrip(".,:;)")[:180]
    if tool in {"bash", "shell", "exec", "terminal"}:
        lowered = raw.lower()
        if any(token in lowered for token in ("pytest", " test", "tests", "jest")):
            return "Test suite"
        if lowered.startswith("git "):
            return "Git inspection"
        if lowered.startswith(("rg ", "grep ")):
            return "Repository search"
        return None
    if path := _TOOL_ABSOLUTE_PATH.search(raw):
        return path.group(0).rsplit("/", 1)[-1].rstrip(".,:;)")[:180]
    safe = sanitize_public_progress_detail(raw)
    if safe and "/" in safe and " " not in safe:
        return safe.rsplit("/", 1)[-1]
    return safe


def project_public_tool_activity(
    tool: Optional[str],
    status: Optional[str],
    detail: Optional[str] = None,
) -> PublicActivity:
    """Turn a provider tool update into a stable human-facing activity."""
    safe_tool = str(tool or "tool")
    activity = next(
        (
            name
            for name, tools in _TOOL_ACTIVITIES.items()
            if safe_tool.lower() in tools
        ),
        "agent_work",
    )
    safe_status = str(status or "updated")
    return PublicActivity(
        code=activity,
        message=_TOOL_MESSAGES.get(safe_tool.lower(), _ACTIVITY_MESSAGES[activity]),
        status=_PUBLIC_STATUSES.get(safe_status, "updated"),
        detail=_public_tool_detail(safe_tool.lower(), detail),
    )


#: A name safe to store as an identifier -- no whitespace, no punctuation that
#: could confuse a consumer, bounded length. Shared with `sse.py`, which
#: applies it to stage and context keys.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

#: Tool statuses a provider is allowed to assert. Anything else becomes
#: "updated" rather than reaching a person as a raw provider string.
_SAFE_TOOL_STATUSES = frozenset(
    {"started", "pending", "running", "completed", "failed", "error", "finished"}
)

#: The vetted summary for each turn kind. A message is *chosen* from this
#: table, never copied from the provider: `TurnProgress.message` is documented
#: as trusted-log material and may hold model text, commands or credentials.
_KIND_PROJECTIONS = {
    "error": ("Agent needs attention", "agent_error", "issue", "error"),
    "connection": ("Connecting to the agent runtime", "connection", "running", "info"),
    "waiting": ("Waiting for an agent operation", "waiting", "waiting", "info"),
    "rate_limit": ("Waiting for model capacity", "capacity_wait", "waiting", "warning"),
    "reasoning": ("Analyzing the change", "analysis", "running", "info"),
    "text": ("Agent update", "agent_update", "updated", "info"),
}


def safe_name(value: Optional[str], default: str) -> str:
    """A provider-supplied identifier, or a default when it is not one."""
    candidate = str(value or "")
    return candidate if _SAFE_NAME.fullmatch(candidate) else default


@dataclass(frozen=True)
class KindProjection:
    """The vetted public form of one turn update."""

    message: str
    activity: str
    status: str
    severity: str
    detail: Optional[str]
    #: True when `detail` came through the stricter tool projection.
    projected: bool


def project_turn_kind(progress: Any) -> Optional[KindProjection]:
    """Project a `TurnProgress` by kind, or None when it should not be shown.

    Two kinds are dropped, both deliberately:

    * ``step`` -- provider protocol boundaries are transport noise. A product
      already knows its session started; repeating that between every tool
      call buries the work a person is trying to follow.
    * anything unrecognized -- fail closed. A kind nobody has mapped has no
      vetted summary, and the only other thing to show would be the raw
      provider message, which is exactly what must not be shown.
    """
    kind = getattr(progress, "kind", "")
    if kind == "model_selected":
        model = str(getattr(progress, "detail", "") or "")
        effort = str(getattr(progress, "status", "") or "")
        message = "Model selection updated"
        if (re.fullmatch(r"[a-zA-Z0-9_.+-]{1,128}/[a-zA-Z0-9_.+-]{1,128}", model)
                and effort in {"default", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}):
            message = f"Selected model {model} (effort: {effort})"
        return KindProjection(message=message, activity="model_selected", status="running",
                              severity="info", detail=None, projected=True)
    if kind == "tool":
        tool = safe_name(getattr(progress, "tool", None), "tool")
        status = safe_name(getattr(progress, "status", None), "updated")
        if status not in _SAFE_TOOL_STATUSES:
            status = "updated"
        activity = project_public_tool_activity(tool, status, getattr(progress, "detail", None))
        return KindProjection(
            message=activity.message,
            activity=activity.code,
            status=activity.status,
            severity="info",
            detail=activity.detail,
            projected=True,
        )

    if kind == "model_fallback":
        transition = str(getattr(progress, "detail", "") or "")
        reason = safe_name(getattr(progress, "status", None), "provider_error")
        message = "Switching to fallback model"
        if _SAFE_MODEL_TRANSITION.fullmatch(transition):
            message = f"Model fallback: {transition} ({reason})"
        return KindProjection(
            message=message,
            activity="model_fallback",
            status="running",
            severity="warning",
            detail=transition or None,
            projected=False,
        )

    mapped = _KIND_PROJECTIONS.get(kind)
    if mapped is None:
        return None
    message, activity_code, status, severity = mapped
    return KindProjection(
        message=message,
        activity=activity_code,
        status=status,
        severity=severity,
        detail=getattr(progress, "detail", None),
        projected=False,
    )


@dataclass(frozen=True)
class AgentProgressEvent:
    """One progress update, safe to store and safe to show.

    The counterpart of `TurnProgress`, which is trusted-log material. Crossing
    from one to the other is `project_turn_progress` and nothing else.
    """

    sequence: int
    session_id: Optional[str]
    phase: str
    kind: str
    summary: str
    detail: Optional[str] = None
    tool: Optional[str] = None
    status: Optional[str] = None


def project_turn_progress(
    progress: Any,
    *,
    phase: str,
    session_id: Optional[str] = None,
    sequence: int = 0,
    detail_policy: str = "summary",
) -> Optional[AgentProgressEvent]:
    """Turn a harness update into a publishable event, or None to drop it.

    ``detail_policy`` is ``"summary"`` by default because that policy has no
    path at all from provider text to the emitted event -- there is nothing to
    redact because nothing is carried. ``"public_detail"`` adds a bounded,
    redacted synopsis, and is for a product that has already decided its
    progress view is report-visible.

    Any other value is treated as ``"summary"``. A policy is configuration, and
    a typo in a workflow file must fail closed.
    """
    projection = project_turn_kind(progress)
    if projection is None:
        return None

    detail = None
    if detail_policy == "public_detail":
        detail = (
            projection.detail
            if projection.projected
            else sanitize_public_progress_detail(projection.detail)
        )

    return AgentProgressEvent(
        sequence=int(sequence),
        session_id=session_id,
        phase=str(phase),
        kind=str(getattr(progress, "kind", "")),
        summary=projection.message,
        detail=detail,
        tool=safe_name(getattr(progress, "tool", None), "") or None,
        status=projection.status,
    )

#: A kind that is worth more than an informational line. `error` is what a
#: product filters on to page someone; `rate_limit` is expected but slow, so
#: it warns rather than pages.
_KIND_SEVERITY = {"error": "error", "rate_limit": "warning"}


@dataclass(frozen=True)
class ProgressEnvelope:
    """One progress report, ready to store, with its detail already bounded.

    The shape is the payload `sse.py` has always written -- `kind`, `activity`,
    `status`, an optional `detail`, and whatever context the publisher carries.
    Naming it lets a workflow node emit phase progress without importing an
    event-stream transport, and lets the reader below be tested against real
    writer output rather than a fixture someone wrote from memory.

    Frozen because it is a record of a moment. A mutated envelope would report
    something that never happened.
    """

    kind: str
    message: str
    activity: str = "agent_work"
    status: str = "updated"
    detail: Optional[str] = None
    context: Mapping[str, Any] = field(default_factory=dict)
    severity: str = ""
    #: True when `detail` already came through `project_public_tool_activity`.
    #: Re-running a projected value through the generic synopsis rule can only
    #: lose information -- it was already judged safe by a stricter rule.
    projected: bool = False

    def __post_init__(self) -> None:
        if not self.severity:
            object.__setattr__(self, "severity", _KIND_SEVERITY.get(self.kind, "info"))

    @classmethod
    def from_tool(
        cls,
        tool: Optional[str],
        status: Optional[str],
        detail: Optional[str] = None,
        **context: Any,
    ) -> "ProgressEnvelope":
        """Build an envelope from a provider tool update."""
        projection = project_public_tool_activity(tool, status, detail)
        return cls(
            kind="tool",
            message=projection.message,
            activity=projection.code,
            status=projection.status,
            detail=projection.detail,
            context=context,
            projected=True,
        )

    def payload(self) -> Dict[str, Any]:
        """The JSON-safe dict to store.

        Context is stamped first and the projection last, deliberately: context
        is caller-supplied, and a caller that could set `activity` or `status`
        could put an unprojected provider string in front of a person.
        """
        payload = {key: _json_safe(value) for key, value in dict(self.context).items()}
        payload["kind"] = self.kind
        payload["activity"] = self.activity
        payload["status"] = self.status
        detail = self.detail if self.projected else sanitize_public_progress_detail(self.detail)
        if detail:
            # Omitted rather than null when there is nothing to say: the reader
            # does `payload.get("detail") or ""`, and an absent key is what
            # every stored event already looks like.
            payload["detail"] = detail
        return payload


def _json_safe(value: Any) -> Any:
    """Coerce a context value into something `json.dumps` accepts.

    A payload is serialized at write time, deep inside the publisher. Letting
    an unserializable value through means the failure surfaces there, far from
    whoever put it in the context -- and loses the progress report entirely.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


_LEGACY_TOOL_EVENT = re.compile(
    r"^tool\[([^\]]+)]\s+([^\s]+)(?:\s+-\s+(.*))?$"
)


def public_progress_events(
    store: Any,
    task_ref: str,
    *,
    limit: int = 1000,
) -> list[Dict[str, Any]]:
    """Read normalized, public-safe progress from a runtime store.

    The legacy normalization lets consumers improve their UI without losing
    reports written by an older publisher. Provider step boundaries are
    omitted because they describe the stream protocol rather than useful work.
    """
    try:
        rows = store.events_since(task_ref=task_ref, limit=limit)
    except sqlite3.OperationalError:
        return []

    events = []
    for row in rows:
        if row["event_type"] != "agent_progress":
            continue
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        message = str(row["message"] or "")
        if message.startswith("step "):
            continue
        if not payload.get("activity") and (match := _LEGACY_TOOL_EVENT.match(message)):
            projection = project_public_tool_activity(
                match.group(1), match.group(2), match.group(3)
            )
            payload = {
                **payload,
                "activity": projection.code,
                "status": projection.status,
            }
            if projection.detail:
                payload["detail"] = projection.detail
            message = projection.message

        events.append(
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "severity": row["severity"],
                "stage": row["stage"],
                "message": message,
                "payload": payload,
                "created_at": row["created_at"],
            }
        )
    return events


def group_progress_events(events: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Partition parallel sessions and collapse repetitive adjacent activity."""
    grouped: dict[str, Dict[str, Any]] = {}
    for event in events:
        payload = event.get("payload") or {}
        session_ref = _session_ref(event, payload)
        tab = grouped.setdefault(
            session_ref,
            {
                "key": session_ref,
                "label": _session_label(session_ref, payload),
                "kind": str(payload.get("session_kind") or "agent"),
                "events": [],
                "event_count": 0,
                "issue_count": 0,
                "latest_message": "",
                "latest_at": None,
            },
        )
        status = str(payload.get("status") or "updated")
        activity = str(payload.get("activity") or "agent_work")
        detail = str(payload.get("detail") or "")
        condensed = tab["events"]
        signature = (activity, status, event["message"], detail)
        if condensed and (
            condensed[-1]["activity"],
            condensed[-1]["status"],
            condensed[-1]["message"],
            condensed[-1]["detail"],
        ) == signature:
            condensed[-1]["count"] += 1
            condensed[-1]["latest_at"] = event.get("created_at")
        else:
            condensed.append(
                {
                    "activity": activity,
                    "status": status,
                    "message": event["message"],
                    "detail": detail,
                    "severity": event.get("severity") or "info",
                    "count": 1,
                    "started_at": event.get("created_at"),
                    "latest_at": event.get("created_at"),
                }
            )
        tab["event_count"] += 1
        tab["issue_count"] += int(status == "issue")
        tab["latest_message"] = event["message"]
        tab["latest_at"] = event.get("created_at")
    return list(grouped.values())


def _session_ref(event: Dict[str, Any], payload: Dict[str, Any]) -> str:
    if payload.get("session_ref"):
        return str(payload["session_ref"])
    if payload.get("feedback_session_id"):
        return f"feedback:{payload['feedback_session_id']}"
    if payload.get("reviewer"):
        return f"reviewer:{payload['reviewer']}"
    return str(event.get("stage") or "agent")


def _session_label(session_ref: str, payload: Dict[str, Any]) -> str:
    if payload.get("session_label"):
        return str(payload["session_label"])
    value = session_ref.split(":", 1)[-1]
    return value.replace("_", " ").strip().title() or "Agent"
