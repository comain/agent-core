"""RDC: a pipeline trigger that expects an ack.

Two products spoke this format independently, with two implementations of the
same envelope. Identical wire formats maintained twice is a slow leak: one
side gains a field, or fixes a fallback, and the other does not, and nothing
fails until a pipeline sends the shape only one of them handles.

The format:

**Inbound** — the request is nested under ``attribute``, with free-form extras
under ``data``, and several fields have more than one accepted spelling. Every
field is also accepted at the top level, because some callers send it flat.

**Outbound** — an ack of ``{state, attribute, data}``, where ``state`` is 0 for
pass and -1024 for fail. RDC reads the outcome from the body, so the ack is
sent to a configured URL rather than one the trigger carried.

What is *not* here is what a product puts in the ack's ``data``: a review
sends a score and a summary, a test generator sends coverage. That is passed
in as the outcome's details.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

from agent_core.integrations.protocol import (
    Outcome,
    Reply,
    ReportResult,
    Trigger,
    TriggerProtocol,
)

#: RDC's own success and failure codes for the ack.
STATE_PASSED = 0
STATE_FAILED = -1024


def _pick(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _as_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def parse_rdc_payload(payload: Mapping[str, Any]) -> Trigger:
    """Read RDC's trigger envelope.

    Every field is looked for under ``attribute`` first and then at the top
    level: callers send both shapes, and one of them arriving unhandled means
    a review that silently never starts.
    """
    attribute = payload.get("attribute") or {}
    if not isinstance(attribute, Mapping):
        raise ValueError("attribute must be an object")
    metadata = payload.get("data") or {}

    def field(*names: str) -> Any:
        for name in names:
            found = _pick(attribute.get(name), payload.get(name))
            if found is not None:
                return found
        return None

    repo_url = field("gitUrl", "gitRepositoryPath")
    branch = field("branch", "gitBranchName")
    app_name = field("appName")
    if not repo_url or not branch:
        raise ValueError("an RDC trigger needs a repository and a branch")

    return Trigger(
        repo_url=str(repo_url),
        branch=str(branch),
        app_name=str(app_name or ""),
        commit_id=_as_str(field("commitId", "commit_id")),
        operator=_as_str(field("operator")),
        source="rdc",
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        # Everything the ack will need. Persisted with the task, because the
        # ack is sent long after the request object is gone.
        reply_to={
            "task_id": _as_str(field("taskId")) or "",
            "record_id": _as_str(field("recordId")) or "",
            "task_template_id": _as_str(field("taskTemplateId")) or "",
            "parent_id": _as_str(field("parentId")) or "",
            "operator": _as_str(field("operator")) or "",
        },
    )


def build_rdc_ack(reply_to: Mapping[str, Any], outcome: Outcome) -> Dict[str, Any]:
    """The ack body. Kept exactly as both products already send it."""
    return {
        "state": STATE_PASSED if outcome.passed else STATE_FAILED,
        "attribute": {
            "taskId": str(reply_to.get("task_id") or ""),
            "recordId": str(reply_to.get("record_id") or ""),
            "taskTemplateId": str(reply_to.get("task_template_id") or ""),
            "parentId": str(reply_to.get("parent_id") or ""),
            "url": outcome.report_url,
            "reportUrl": outcome.report_url,
            "operator": str(reply_to.get("operator") or ""),
        },
        "data": {str(k): v for k, v in (outcome.details or {}).items()},
    }


class RdcProtocol(TriggerProtocol):
    """Adapts RDC's wire format to a product's core.

    ``send`` performs the HTTP POST and returns a `ReportResult`; it is passed
    in rather than implemented here so a product keeps its own retry policy,
    timeouts and logging.
    """

    name = "rdc"

    def __init__(self, *, ack_url: str = "", send=None) -> None:
        self.ack_url = ack_url
        self.send = send

    # -- inbound ------------------------------------------------------------

    def parse(self, body: bytes, headers: Mapping[str, str]) -> Optional[Trigger]:
        return parse_rdc_payload(json.loads(body or b"{}"))

    def accepted(self, trigger: Trigger, *, task_id: str, task_url: str, report_url: str) -> Reply:
        return Reply(status_code=200, body=self.result_body(0, "处理中", {
            "taskId": task_id,
            "status": "queued",
            "url": report_url or task_url,
            "reportUrl": report_url,
            "taskUrl": task_url,
        }))

    def failed(self, exc: Exception) -> Reply:
        return Reply(status_code=200, body=self.result_body(-1, str(exc), {}))

    @staticmethod
    def result_body(status: int, message: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """RDC's envelope for a synchronous reply.

        Always HTTP 200: the caller reads the outcome from the body. The
        duplicate `errcode` and `message` keys are deliberate -- different
        callers read different ones, and dropping either is invisible until a
        pipeline reports success for a failed request.
        """
        return {
            "status": status,
            "errcode": status,
            "msg": message,
            "message": message,
            "data": data,
        }

    # -- outbound -----------------------------------------------------------

    def can_report(self, reply_to: Mapping[str, Any]) -> bool:
        """RDC needs both identifiers to match the ack to its pipeline step."""
        return bool(self.ack_url and reply_to.get("task_id") and reply_to.get("record_id"))

    def report(self, reply_to: Mapping[str, Any], outcome: Outcome) -> ReportResult:
        if not self.can_report(reply_to):
            return ReportResult(delivered=False, error="no RDC ack target configured")
        if self.send is None:
            raise RuntimeError("RdcProtocol needs a send= to deliver an ack")
        return self.send(self.ack_url, build_rdc_ack(reply_to, outcome))
