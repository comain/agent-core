"""The edge: accepting work from outside systems and reporting back to them.

`agent_core.api` serves a product's own operators -- health, task control, a
progress stream. This is the other direction: the CI pipelines, webhooks and
orchestrators that ask for work and expect an answer.

No adapter is registered here. Which outside systems a product speaks to is
the product's business, and a consumer that talks to one should not import
the dependencies of the others.
"""

from agent_core.integrations.delivery import deliver_json
from agent_core.integrations.gitlab_webhook import GitLabProtocol
from agent_core.integrations.protocol import (
    Outcome,
    ReportResult,
    Reply,
    Trigger,
    TriggerProtocol,
    VerificationFailed,
)
from agent_core.integrations.gitlab import GitLabClient, GitLabError
from agent_core.integrations.registry import (
    UnknownProtocolError,
    available_protocols,
    create_protocol,
    protocol_for,
    register_protocol,
    unregister_protocol,
)

__all__ = [
    "Outcome",
    "Reply",
    "ReportResult",
    "Trigger",
    "TriggerProtocol",
    "UnknownProtocolError",
    "VerificationFailed",
    "GitLabClient",
    "Accepted",
    "GitLabProtocol",
    "TriggerMount",
    "create_trigger_router",
    "deliver_json",
    "GitLabError",
    "available_protocols",
    "create_protocol",
    "protocol_for",
    "register_protocol",
    "unregister_protocol",
]


def __getattr__(name):
    """Expose the trigger router lazily.

    It needs FastAPI, which is an optional extra: a consumer that speaks a
    protocol without serving HTTP -- a daemon polling a queue -- should not
    have to install a web framework to import this package.
    """
    if name in ("Accepted", "TriggerMount", "create_trigger_router"):
        from agent_core.integrations import router as _router

        return getattr(_router, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
