"""Identity and authorization.

Humans are identified by a header an SSO proxy injects; machines by a shared
service token. agent-core stores no credentials and issues none.
"""

from agent_core.identity.guard import (
    InsecureDeploymentError,
    assert_trusted_deployment,
)
from agent_core.identity.policy import (
    ANSWER_GATE,
    CANCEL_GATE,
    MUTATE_TASK,
    PROTECTED_ACTIONS,
    READ_REPORT,
    Policy,
)
from agent_core.identity.principal import (
    AuthenticationError,
    AuthorizationError,
    Principal,
)
from agent_core.identity.resolver import (
    IdentityResolver,
    ProxyHeaderResolver,
    ServiceTokenResolver,
)

__all__ = [
    "Principal",
    "AuthenticationError",
    "AuthorizationError",
    "IdentityResolver",
    "ProxyHeaderResolver",
    "ServiceTokenResolver",
    "Policy",
    "ANSWER_GATE",
    "CANCEL_GATE",
    "MUTATE_TASK",
    "READ_REPORT",
    "PROTECTED_ACTIONS",
    "assert_trusted_deployment",
    "InsecureDeploymentError",
]
