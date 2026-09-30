"""Git access: credentials, cloned workspaces, and publishing results.

The identity half was byte-identical across two products and partially forked
into a third; the workspace half is the clone/work/push cycle every consumer
repeats.
"""

from agent_core.git.identity import (
    append_git_config,
    env_with_identity,
    env_with_ssh_key,
    has_access_token,
    repo_name_from_url,
    ssh_command_for_key,
    url_for_access_token,
)
from agent_core.git.changes import ChangeCollector, ChangeSet
from agent_core.git.publish import (
    MergeRequestOutcome,
    MergeRequestSpec,
    MergeRequestStatus,
    PublicationCoordinator,
    PublicationRequest,
    PublicationResult,
    ensure_merge_request,
    GitPublicationOutcome,
    GitPublicationStatus,
    GitPublishRequest,
    GitScopedPublisher,
    NoPublicationChanges,
    PushedPublication,
    ReusedPublication,
)
from agent_core.git.refs import update_refs_with_lease
from agent_core.git.retry import is_retryable_git_failure, retry_git_operation
from agent_core.git.workspace import (
    BotIdentity,
    GitCancelled,
    GitCommandError,
    GitCredentials,
    GitTimeout,
    GitWorkspace,
    PushConflictError,
    PushPolicyError,
    git_url_host,
    validate_branch,
)

__all__ = [
    "ChangeCollector",
    "ChangeSet",
    "GitPublishRequest",
    "MergeRequestOutcome",
    "MergeRequestSpec",
    "MergeRequestStatus",
    "PublicationCoordinator",
    "PublicationRequest",
    "PublicationResult",
    "ensure_merge_request",
    "GitPublicationOutcome",
    "GitPublicationStatus",
    "PushedPublication",
    "ReusedPublication",
    "NoPublicationChanges",
    "GitScopedPublisher",
    "ssh_command_for_key",
    "env_with_ssh_key",
    "env_with_identity",
    "url_for_access_token",
    "has_access_token",
    "repo_name_from_url",
    "append_git_config",
    "GitWorkspace",
    "GitCredentials",
    "BotIdentity",
    "GitCommandError",
    "GitCancelled",
    "GitTimeout",
    "validate_branch",
    "git_url_host",
    "PushPolicyError",
    "PushConflictError",
    "is_retryable_git_failure",
    "retry_git_operation",
    "update_refs_with_lease",
]

from agent_core.git import guard  # noqa: F401 - workspace change verification
from agent_core.git.guard import (  # noqa: F401
    WorkspaceViolation,
    changed_paths as workspace_changed_paths,
    snapshot as workspace_snapshot,
    verify as verify_workspace,
)

from agent_core.git.runner import GitRunner, runner_for  # noqa: F401
