"""Accepting work from an outside system, and reporting back to it.

Every product in this family is triggered by something it does not control --
a CI pipeline, a code-hosting webhook, an internal orchestrator -- and owes
that system an answer when the work finishes. Each wrote the same four steps
against whichever caller it had first:

    verify it -> parse it -> answer now -> answer again when finished

Written as branches, that shape does not grow. A second caller becomes an
`elif` in the parser and another in the reporter, and a third means editing
both again; the shared pieces stay shared by luck rather than by structure,
and one product fixes a field the other does not.

Written as a protocol, a caller is an adapter: something to register, not
something to edit the service for.

What is deliberately *not* here is what any product's work means. `Trigger`
carries what all of them need to start -- which repository, which branch,
which commit -- plus two open bags: `metadata` for whatever the caller sent,
and `reply_to` for whatever the protocol will need to find its way home.
`Outcome` carries the answer in the same spirit: passed or not, a sentence, a
link, and a `details` bag for the numbers a particular product reports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


class VerificationFailed(Exception):
    """The request did not come from who it claims to come from.

    Distinct from a parse failure: a body that cannot be read is a bug
    somewhere, while a body that fails verification may be an attack, and the
    two should not be logged, alerted on, or answered the same way.
    """


@dataclass(frozen=True)
class Trigger:
    """A request to do some work, in terms every product shares."""

    repo_url: str
    branch: str
    app_name: str = ""
    commit_id: Optional[str] = None
    operator: Optional[str] = None
    source: str = "manual"
    #: Whatever the caller sent that this package has no opinion about.
    metadata: Dict[str, Any] = field(default_factory=dict)
    #: Whatever the protocol will need to report back later -- a callback URL,
    #: a set of pipeline identifiers, a check run id. Persisted by the product
    #: with the task and handed back to `report`, so reporting does not depend
    #: on the original request still being in memory.
    reply_to: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    """What to tell the caller once the work is finished."""

    passed: bool
    summary: str = ""
    report_url: str = ""
    #: Numbers a particular product reports -- a score, a count of findings,
    #: a coverage delta. Protocols pass through what they recognise.
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Reply:
    """The synchronous HTTP answer to an inbound trigger.

    Status and body only: a protocol should be testable without a web
    framework, and the route layer turns this into a response.
    """

    status_code: int
    body: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReportResult:
    """What happened when the outcome was sent back."""

    delivered: bool
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


class TriggerProtocol(ABC):
    """One outside system's wire format, adapted to a product's core.

    A product holds these by name and never branches on which one it has.
    """

    #: How this protocol is registered and configured.
    name: str = ""

    # -- inbound ------------------------------------------------------------

    def verify(self, body: bytes, headers: Mapping[str, str]) -> None:
        """Reject a request that did not come from this system.

        Raises `VerificationFailed`. The default trusts the caller, which is
        honest for an internal orchestrator on a private network and wrong for
        anything reachable from outside -- a signed webhook overrides it.
        """
        return None

    @abstractmethod
    def parse(self, body: bytes, headers: Mapping[str, str]) -> Optional[Trigger]:
        """Read a request, or return None for an event to ignore.

        Ignoring is not failing: a webhook fires for many events and most of
        them are not work.
        """

    @abstractmethod
    def accepted(self, trigger: Trigger, *, task_id: str, task_url: str, report_url: str) -> Reply:
        """Answer a request that was accepted, in this caller's format."""

    def ignored(self) -> Reply:
        """Answer an event that was understood and deliberately not acted on."""
        return Reply(status_code=200, body={"status": "ignored"})

    @abstractmethod
    def failed(self, exc: Exception) -> Reply:
        """Answer a request that could not be verified or read."""

    # -- outbound -----------------------------------------------------------

    def can_report(self, reply_to: Mapping[str, Any]) -> bool:
        """Whether this task carries enough to report back to."""
        return bool(reply_to)

    @abstractmethod
    def report(self, reply_to: Mapping[str, Any], outcome: Outcome) -> ReportResult:
        """Send the outcome home."""
