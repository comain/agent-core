"""Approval inbox rendering.

Framework-agnostic, like the SSE layer: these are pure functions from data to
strings, and each product supplies its own routing and response objects. Four
consumers should not inherit a web framework because one of them wants an inbox.

Everything rendered here is untrusted. Gate prompts carry model-generated text
and reviewer comments, so every interpolation goes through :func:`html.escape`.
A design proposal containing ``<script>`` is not hypothetical -- models emit
code, and code is what this system reviews.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 1.5rem; font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
h1 { font-size: 1.1rem; margin: 0 0 1rem; }
.empty { opacity: .6; padding: 2rem 0; }
.gate { border: 1px solid rgba(128,128,128,.35); border-radius: 8px; padding: .85rem 1rem; margin-bottom: .75rem; }
.gate header { display: flex; flex-wrap: wrap; gap: .5rem; align-items: baseline; }
.node { font-weight: 700; }
.ref { opacity: .7; font-family: ui-monospace, monospace; font-size: .85em; }
.kind { font-size: .72rem; text-transform: uppercase; letter-spacing: .04em;
        border: 1px solid currentColor; border-radius: 999px; padding: .05rem .5rem; }
.kind-input { color: #7a5; } .kind-approve { color: #58a; }
.waited { margin-left: auto; font-variant-numeric: tabular-nums; opacity: .75; }
.waited.long { color: #c60; font-weight: 700; }
.prompt { margin: .6rem 0 0; padding: .6rem .7rem; border-radius: 6px;
          background: rgba(128,128,128,.09); white-space: pre-wrap;
          overflow-x: auto; font-family: ui-monospace, monospace; font-size: .86em; }
form { margin-top: .7rem; display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }
textarea { flex: 1 1 22rem; min-height: 3.2rem; padding: .4rem; font: inherit; }
button { padding: .35rem .9rem; font: inherit; cursor: pointer; }
table { border-collapse: collapse; width: 100%; }
"""


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def humanize_wait(since: Optional[str], *, now: Optional[datetime] = None) -> str:
    """How long a gate has been waiting.

    The primary signal in an inbox where gates never expire: the queue itself is
    the only thing that surfaces a review nobody has picked up.
    """
    started = _parse_iso(since)
    if started is None:
        return "unknown"
    delta = (now or datetime.now(timezone.utc)) - started
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def is_stale(since: Optional[str], *, threshold_seconds: int = 86400, now: Optional[datetime] = None) -> bool:
    started = _parse_iso(since)
    if started is None:
        return False
    return ((now or datetime.now(timezone.utc)) - started).total_seconds() >= threshold_seconds


def gate_to_dict(gate: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """JSON-serialisable view of a gate, for an API or a client-side inbox."""
    return {
        "gate_id": gate.gate_id,
        "task_ref": gate.task_ref,
        "thread_id": gate.thread_id,
        "node": gate.node,
        "kind": gate.kind,
        "state": gate.state,
        "prompt": gate.prompt,
        "response_schema": gate.response_schema,
        "requested_at": gate.requested_at,
        "waited": humanize_wait(gate.requested_at, now=now),
        "stale": is_stale(gate.requested_at, now=now),
        "answered_by": gate.answered_by,
        "answered_at": gate.answered_at,
    }


def render_prompt(prompt: Dict[str, Any]) -> str:
    """Render a gate prompt readably, without trusting any of it."""
    if not prompt:
        return "<em>no details supplied</em>"
    parts: List[str] = []
    artifact = prompt.get("artifact_markdown")
    if artifact:
        parts.append(f'<pre class="artifact">{_escape(artifact)}</pre>')
    for key, value in prompt.items():
        if key in ("gate_id", "kind", "node", "artifact_markdown"):
            continue  # header metadata, or already rendered as the artifact
        text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
        parts.append(f"<strong>{_escape(key)}</strong>\n{_escape(text)}")
    return "\n\n".join(parts) if parts else "<em>no details supplied</em>"


def render_gate(gate: Any, *, action_url: str = "", now: Optional[datetime] = None) -> str:
    """One inbox entry, with the form appropriate to its kind."""
    waited = humanize_wait(gate.requested_at, now=now)
    stale_class = " long" if is_stale(gate.requested_at, now=now) else ""
    kind_class = "kind-input" if gate.kind == "input" else "kind-approve"

    if gate.kind == "input":
        # An input gate wants a structured answer -- design review needs comments,
        # not a boolean. Conflating the two is what the ACP split avoids.
        controls = (
            '<textarea name="comments" placeholder="Comments (sent to the workflow)"></textarea>'
            '<button name="decision" value="approve" type="submit">Approve</button>'
            '<button name="decision" value="reject" type="submit">Reject</button>'
        )
    else:
        controls = (
            '<button name="decision" value="approve" type="submit">Approve</button>'
            '<button name="decision" value="reject" type="submit">Reject</button>'
        )

    action = _escape(action_url or f"/gates/{gate.gate_id}/answer")
    return f"""
    <article class="gate">
      <header>
        <span class="node">{_escape(gate.node)}</span>
        <span class="kind {kind_class}">{_escape(gate.kind)}</span>
        <span class="ref">{_escape(gate.task_ref)}</span>
        <span class="waited{stale_class}">waiting {_escape(waited)}</span>
      </header>
      <div class="prompt">{render_prompt(gate.prompt)}</div>
      <form method="post" action="{action}">
        <input type="hidden" name="gate_id" value="{_escape(gate.gate_id)}">
        {controls}
      </form>
    </article>
    """


def render_inbox(
    gates: Sequence[Any],
    *,
    title: str = "Approval inbox",
    action_url_for: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> str:
    """The full inbox page: every gate awaiting a human, across all tasks."""
    if not gates:
        body = '<p class="empty">Nothing waiting. All workflows are running or complete.</p>'
    else:
        body = "\n".join(
            render_gate(
                g,
                action_url=action_url_for(g) if action_url_for else "",
                now=now,
            )
            for g in gates
        )
    count = len(gates)
    heading = f"{_escape(title)} <span class=\"ref\">({count})</span>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(title)}</title><style>{_STYLE}</style></head>
<body><h1>{heading}</h1>{body}</body></html>"""
