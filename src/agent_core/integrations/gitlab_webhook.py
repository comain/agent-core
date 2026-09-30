"""GitLab webhooks: a push or a merge request asking for work.

Inbound only. `gitlab.py` beside this is the outbound API client that creates
merge requests; the two share a vendor and nothing else.

Unlike a CI orchestrator, a webhook fires constantly and most of what it
sends is not work. Deciding what to ignore is most of this adapter, and
getting it wrong is expensive in both directions: ignore too much and reviews
silently never happen, ignore too little and every comment on a merge request
starts one.

**Verification.** GitLab sends a shared secret in a header and does not sign
the body, so there is nothing to recompute -- the check is that the secret
matches, compared in constant time. A webhook endpoint is reachable by
anyone who learns the URL, so an adapter configured without a secret refuses
every request rather than trusting them: an unverified public endpoint that
starts work is worse than one that is switched off.

**Reporting.** The result goes back as a commit status, which is what makes it
show up against the branch and on the merge request. The HTTP call is passed
in, so a product keeps its own retries and credentials.
"""

from __future__ import annotations

import hmac
import json
from typing import Any, Dict, Mapping, Optional

from agent_core.integrations.protocol import (
    Outcome,
    Reply,
    ReportResult,
    Trigger,
    TriggerProtocol,
    VerificationFailed,
)

#: Merge-request actions that mean the code changed. `close`, `merge` and
#: `approved` do not, and reviewing on them repeats work that already ran.
CODE_CHANGING_ACTIONS = frozenset({"open", "reopen", "update"})

#: GitLab's own commit-status values.
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


def _header(headers: Mapping[str, str], name: str) -> str:
    """Headers are case-insensitive; a plain dict is not."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value or ""
    return ""


def _branch_from_ref(ref: str) -> str:
    """`refs/heads/feature/x` -> `feature/x`. Tags are not branches."""
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    return ""


def parse_gitlab_event(payload: Mapping[str, Any]) -> Optional[Trigger]:
    """Read a push or merge-request event, or return None to ignore it.

    Ignoring covers: any other event kind, a tag, a branch deletion, and a
    merge request whose action did not change the code.
    """
    kind = str(payload.get("object_kind") or "")
    project = payload.get("project") or {}
    repo_url = project.get("git_ssh_url") or project.get("git_http_url") or ""
    app_name = str(project.get("path_with_namespace") or project.get("name") or "")

    if kind == "push":
        branch = _branch_from_ref(str(payload.get("ref") or ""))
        commit = str(payload.get("checkout_sha") or "")
        # A deleted branch reports an all-zero sha and has nothing to review.
        if not branch or not commit or set(commit) == {"0"}:
            return None
        return Trigger(
            repo_url=str(repo_url),
            branch=branch,
            app_name=app_name,
            commit_id=commit,
            operator=str(payload.get("user_username") or ""),
            source="gitlab-push",
            metadata={"event": "push"},
            reply_to={
                "project_id": str(project.get("id") or ""),
                "commit_id": commit,
                "ref": branch,
            },
        )

    if kind == "merge_request":
        attributes = payload.get("object_attributes") or {}
        if str(attributes.get("action") or "") not in CODE_CHANGING_ACTIONS:
            return None
        branch = str(attributes.get("source_branch") or "")
        commit = str((attributes.get("last_commit") or {}).get("id") or "")
        if not branch or not commit:
            return None
        user = payload.get("user") or {}
        return Trigger(
            repo_url=str(repo_url),
            branch=branch,
            app_name=app_name,
            commit_id=commit,
            operator=str(user.get("username") or ""),
            source="gitlab-merge-request",
            metadata={
                "event": "merge_request",
                "merge_request_iid": attributes.get("iid"),
                "target_branch": attributes.get("target_branch"),
            },
            reply_to={
                "project_id": str(project.get("id") or ""),
                "commit_id": commit,
                "ref": branch,
                "merge_request_iid": attributes.get("iid"),
            },
        )

    return None


def build_commit_status(outcome: Outcome, *, name: str = "code-review") -> Dict[str, Any]:
    """The commit status body, which is how a result reaches the UI."""
    body: Dict[str, Any] = {
        "state": STATUS_SUCCESS if outcome.passed else STATUS_FAILED,
        "name": name,
        "context": name,
    }
    if outcome.report_url:
        body["target_url"] = outcome.report_url
    if outcome.summary:
        # GitLab truncates this in the UI; sending the whole summary would
        # push the link out of view.
        body["description"] = outcome.summary[:140]
    return body


class GitLabProtocol(TriggerProtocol):
    """Adapts GitLab's webhooks to a product's core."""

    name = "gitlab"

    def __init__(self, *, secret: str = "", status_name: str = "code-review", send=None) -> None:
        self.secret = secret
        self.status_name = status_name
        self.send = send

    # -- inbound ------------------------------------------------------------

    def verify(self, body: bytes, headers: Mapping[str, str]) -> None:
        if not self.secret:
            raise VerificationFailed(
                "this endpoint is reachable by anyone who learns its URL and no webhook "
                "secret is configured; refusing rather than accepting unverified work"
            )
        token = _header(headers, "x-gitlab-token")
        if not token:
            raise VerificationFailed("missing X-Gitlab-Token header")
        if not hmac.compare_digest(self.secret, token):
            raise VerificationFailed("invalid webhook token")

    def parse(self, body: bytes, headers: Mapping[str, str]) -> Optional[Trigger]:
        return parse_gitlab_event(json.loads(body or b"{}"))

    def accepted(self, trigger: Trigger, *, task_id: str, task_url: str, report_url: str) -> Reply:
        return Reply(
            status_code=202,
            body={"status": "accepted", "task_id": task_id,
                  "task_url": task_url, "report_url": report_url},
        )

    def failed(self, exc: Exception) -> Reply:
        """A rejected webhook answers 4xx.

        Unlike an orchestrator that reads the body, GitLab records the status
        code and shows failed deliveries in its UI, which is where someone
        notices a misconfigured secret.
        """
        unverified = isinstance(exc, VerificationFailed)
        return Reply(
            status_code=401 if unverified else 400,
            body={"status": "rejected", "error": str(exc)},
        )

    def recognises(self, headers: Mapping[str, str]) -> bool:
        return bool(_header(headers, "x-gitlab-event"))

    # -- outbound -----------------------------------------------------------

    def can_report(self, reply_to: Mapping[str, Any]) -> bool:
        return bool(reply_to.get("project_id") and reply_to.get("commit_id"))

    def report(self, reply_to: Mapping[str, Any], outcome: Outcome) -> ReportResult:
        if not self.can_report(reply_to):
            return ReportResult(delivered=False, error="no GitLab commit to report against")
        if self.send is None:
            raise RuntimeError("GitLabProtocol needs a send= to post a commit status")
        path = f"/projects/{reply_to['project_id']}/statuses/{reply_to['commit_id']}"
        body = build_commit_status(outcome, name=self.status_name)
        if reply_to.get("ref"):
            body["ref"] = reply_to["ref"]
        return self.send(path, body)
