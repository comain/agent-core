"""Resolving a :class:`Principal` from request headers.

Two mechanisms, deliberately kept separate:

* a **trusted proxy header** for humans, set by the SSO proxy
* a **service token** for machine callers such as CI

A request presenting both is rejected rather than silently preferred one way or
the other: it means either a misconfigured caller or someone probing which
mechanism wins, and neither should be resolved by a default.
"""

from __future__ import annotations

import hmac
from typing import Callable, Dict, Mapping, Optional

from agent_core.identity.principal import AuthenticationError, Principal


class ProxyHeaderResolver:
    """Reads the identity a fronting proxy asserts.

    Header names are configurable because SSO proxies disagree about them
    (``X-Forwarded-User``, ``X-Auth-Request-User``, ``Remote-User``, ...).
    """

    def __init__(
        self,
        *,
        user_header: str = "X-Forwarded-User",
        email_header: str = "X-Forwarded-Email",
        groups_header: str = "X-Forwarded-Groups",
        display_name_header: str = "X-Forwarded-Preferred-Username",
        groups_separator: str = ",",
    ):
        self.user_header = user_header.lower()
        self.email_header = email_header.lower()
        self.groups_header = groups_header.lower()
        self.display_name_header = display_name_header.lower()
        self.groups_separator = groups_separator

    def resolve(self, headers: Mapping[str, str]) -> Optional[Principal]:
        lowered = {k.lower(): v for k, v in headers.items()}
        subject = (lowered.get(self.user_header) or "").strip()
        if not subject:
            return None
        raw_groups = (lowered.get(self.groups_header) or "").strip()
        groups = frozenset(
            g.strip() for g in raw_groups.split(self.groups_separator) if g.strip()
        )
        return Principal(
            subject=subject,
            kind="user",
            display_name=(lowered.get(self.display_name_header) or "").strip() or None,
            email=(lowered.get(self.email_header) or "").strip() or None,
            groups=groups,
        )


class ServiceTokenResolver:
    """Authenticates machine callers against configured shared tokens.

    Comparison uses :func:`hmac.compare_digest` so a wrong token cannot be
    narrowed down by timing.
    """

    def __init__(self, tokens: Optional[Mapping[str, str]] = None, *, header: str = "Authorization"):
        # {service_name: token}
        self._tokens: Dict[str, str] = dict(tokens or {})
        self.header = header.lower()

    def resolve(self, headers: Mapping[str, str]) -> Optional[Principal]:
        lowered = {k.lower(): v for k, v in headers.items()}
        raw = (lowered.get(self.header) or "").strip()
        if not raw:
            return None
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
        if not token:
            return None
        for name, expected in self._tokens.items():
            if expected and hmac.compare_digest(token, expected):
                return Principal(subject=name, kind="service")
        raise AuthenticationError("service token not recognised")


class IdentityResolver:
    """Combines the mechanisms and enforces that exactly one is used."""

    def __init__(
        self,
        *,
        proxy: Optional[ProxyHeaderResolver] = None,
        service: Optional[ServiceTokenResolver] = None,
        trust_proxy_headers: bool = True,
    ):
        self.proxy = proxy or ProxyHeaderResolver()
        self.service = service or ServiceTokenResolver()
        self.trust_proxy_headers = trust_proxy_headers

    def resolve(self, headers: Mapping[str, str]) -> Optional[Principal]:
        """Return the authenticated principal, or ``None`` if anonymous.

        Raises :class:`AuthenticationError` for a *bad* credential. Absent and
        invalid are different: absent may be acceptable on an open route, while
        invalid always indicates a caller doing something wrong.
        """
        from_proxy = self.proxy.resolve(headers) if self.trust_proxy_headers else None
        from_service = self.service.resolve(headers)

        if from_proxy and from_service:
            raise AuthenticationError(
                "request presents both a proxy identity and a service token; "
                "refusing to guess which one applies"
            )
        return from_proxy or from_service
