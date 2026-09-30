"""Deployment guard for trusted-proxy authentication.

Trusting an identity header is safe exactly when the service cannot be reached
except through the proxy that sets it. That is a deployment property, not a code
property, so it cannot be verified from inside the process -- but the *dangerous
configurations* can be detected and refused.

The failure this prevents is quiet and total: a service bound to 0.0.0.0 with
header trust enabled lets anyone on the network approve any gate as anyone, and
nothing in the logs looks wrong.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class InsecureDeploymentError(RuntimeError):
    """The service is configured in a way that makes header trust forgeable."""


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def assert_trusted_deployment(
    *,
    bind_host: str,
    trust_proxy_headers: bool,
    proxy_is_fronting: bool,
    allow_insecure: bool = False,
) -> None:
    """Refuse to start in a configuration where the identity header is forgeable.

    ``proxy_is_fronting`` is an explicit operator assertion that a proxy is the
    only ingress. It is deliberately a separate flag from ``trust_proxy_headers``
    so that turning on header trust is not silently also a claim about network
    topology -- someone enabling auth locally should not accidentally assert
    production ingress.

    ``allow_insecure`` exists for tests and single-user local runs. It logs at
    ERROR because a bypass that is easy to enable is one that ends up in
    production.
    """
    if not trust_proxy_headers:
        return

    if _is_loopback(bind_host):
        # Only reachable from the host itself; a proxy on the same box is the
        # normal deployment and nothing external can forge the header.
        return

    if proxy_is_fronting:
        logger.info(
            "trusting identity headers on %s; operator asserts a proxy is the only ingress",
            bind_host,
        )
        return

    if allow_insecure:
        logger.error(
            "INSECURE: trusting identity headers while bound to %s with no proxy assertion. "
            "Anyone able to reach this port can act as any user. Never use in production.",
            bind_host,
        )
        return

    raise InsecureDeploymentError(
        f"identity headers are trusted but the service is bound to {bind_host!r}, which is not "
        "loopback, and no proxy has been declared as the sole ingress. Anyone who can reach this "
        "port could impersonate any user. Bind to loopback behind a proxy, declare the proxy "
        "explicitly, or disable header trust."
    )
