"""Who may do what.

Deliberately small. The staged enforcement decision is that gate answers and
task mutations require an authenticated principal from day one, while read-only
report pages stay open until every existing caller has been updated. That
staging is expressed here rather than scattered across route handlers, so
tightening it later is one change in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional

from agent_core.identity.principal import (
    AuthenticationError,
    AuthorizationError,
    Principal,
)

# Actions the core knows about. Products may define more.
ANSWER_GATE = "gate:answer"
CANCEL_GATE = "gate:cancel"
MUTATE_TASK = "task:mutate"     # stop / cancel / requeue
READ_REPORT = "report:read"

#: Actions that require an authenticated principal in the current stage.
#: READ_REPORT is intentionally absent -- see the module docstring.
PROTECTED_ACTIONS = frozenset({ANSWER_GATE, CANCEL_GATE, MUTATE_TASK})


@dataclass(frozen=True)
class Policy:
    """Authorization rules.

    ``required_groups`` is empty by default, meaning *any* authenticated
    principal may act. That is a real decision, not an oversight: the SSO
    population is already the set of people who should be able to review, and
    inventing a group scheme before anyone has asked for one would be guessing.
    Populate it when a real restriction exists.
    """

    required_groups: FrozenSet[str] = field(default_factory=frozenset)
    allow_service_gate_answers: bool = False

    #: Accept an unauthenticated caller and attribute the action to
    #: ``anonymous_subject``. This exists for one situation: adopting the
    #: shared router in a service whose controls are open today. Turning
    #: enforcement on in the same change would 401 every existing caller, so
    #: a product can mount the router first and tighten afterwards, in one
    #: place, once its callers send credentials.
    #:
    #: It is off by default, and a service that sets it is unauthenticated by
    #: choice -- the audit trail will say `anonymous`, which is the honest
    #: record of who acted.
    allow_anonymous: bool = False
    anonymous_subject: str = "anonymous"

    #: Demo flag: unauthenticated callers may answer gates as a *human*
    #: principal. Distinct from ``allow_anonymous`` (service principal, cannot
    #: answer gates) and ``allow_service_gate_answers`` (lets tokens approve).
    allow_anonymous_gates: bool = False

    def authorize(self, principal: Optional[Principal], action: str) -> Principal:
        """Return the principal if permitted; raise otherwise."""
        if action not in PROTECTED_ACTIONS:
            # Open action. Still return the principal when present so the caller
            # can attribute it.
            return principal  # type: ignore[return-value]

        if principal is None:
            if action in (ANSWER_GATE, CANCEL_GATE) and self.allow_anonymous_gates:
                principal = Principal(subject=self.anonymous_subject, kind="user")
            elif self.allow_anonymous:
                # Attributed, not authenticated. Deliberately `service` rather
                # than `user`, so opening task control to unauthenticated
                # callers does not also let them answer a human gate.
                principal = Principal(subject=self.anonymous_subject, kind="service")
            else:
                raise AuthenticationError(f"{action} requires an authenticated principal")

        if action in (ANSWER_GATE, CANCEL_GATE) and not principal.is_human:
            if not self.allow_service_gate_answers:
                # A pipeline approving its own gate defeats the purpose of the
                # gate. Allowed only when explicitly configured.
                raise AuthorizationError(
                    f"service principal {principal.subject!r} may not answer human gates"
                )

        if self.required_groups and not (self.required_groups & principal.groups):
            raise AuthorizationError(
                f"{principal.subject!r} lacks any of the required groups: "
                f"{sorted(self.required_groups)}"
            )
        return principal
