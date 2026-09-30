"""Who is acting.

Identity is resolved from a trusted reverse proxy that has already performed
SSO and injects the authenticated user as a header. agent-core never
handles a password, and never mints a credential.

The whole scheme rests on one assumption: **the only route to this service is
through that proxy.** If the app is reachable directly, anyone can send the
header and impersonate anyone. That assumption is not left to documentation --
:func:`agent_core.identity.guard.assert_trusted_deployment` refuses to start a
service that is bound to a non-loopback interface without an explicit
acknowledgement that a proxy fronts it.

Machine callers (CI) authenticate with a shared service token instead, and
are represented as a :class:`Principal` of kind ``service`` so an audit record
can distinguish "a person approved this" from "a pipeline did".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class Principal:
    """An authenticated actor.

    ``subject`` is the stable identifier used in audit records. It is whatever
    the proxy asserts (typically a directory username) and is never derived from
    user-supplied body content.
    """

    subject: str
    kind: str  # "user" | "service"
    display_name: Optional[str] = None
    email: Optional[str] = None
    groups: FrozenSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.subject or not self.subject.strip():
            raise ValueError("principal subject must be non-empty")
        if self.kind not in ("user", "service"):
            raise ValueError(f"principal kind must be 'user' or 'service', got {self.kind!r}")

    @property
    def is_human(self) -> bool:
        return self.kind == "user"

    def has_group(self, group: str) -> bool:
        return group in self.groups

    def __repr__(self) -> str:  # keep audit logs readable, never dump groups wholesale
        return f"Principal({self.kind}:{self.subject})"


class AuthenticationError(RuntimeError):
    """No usable identity could be established. Maps to HTTP 401."""


class AuthorizationError(RuntimeError):
    """Identity established, but not permitted to perform this action. HTTP 403."""
