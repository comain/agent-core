"""Classify transient Git failures that are worth retrying."""

from __future__ import annotations

from typing import Callable, TypeVar

from agent_core.git.workspace import GitCancelled, GitCommandError, GitTimeout


T = TypeVar("T")


_TRANSIENT_MARKERS = (
    "connection refused",
    "connection reset",
    "timed out",
    "timeout",
    "failed to connect",
    "could not resolve host",
    "tls",
    "ssl",
    "early eof",
    "remote end hung up",
    "the remote end hung up",
)


def is_retryable_git_failure(error: BaseException) -> bool:
    """Return whether a Git operation failed for a transient reason.

    Cancellation is an operator decision and is never retried. Timeouts are
    transient. Other Git failures are retryable only for network-sensitive
    clone and fetch operations with a recognized transport failure.
    """
    if isinstance(error, GitCancelled):
        return False
    if isinstance(error, GitTimeout):
        return True
    if not isinstance(error, GitCommandError):
        return False
    if len(error.cmd) < 2:
        return False
    # Workspace commands use `git -C <checkout> fetch`, not `git fetch`.
    index = 1
    while index < len(error.cmd) and error.cmd[index] in {"-C", "-c"}:
        index += 2
    if index >= len(error.cmd):
        return False
    subcommand = error.cmd[index].lower()
    if subcommand not in {"clone", "fetch"}:
        return False
    detail = error.stderr.lower()
    return any(marker in detail for marker in _TRANSIENT_MARKERS)


def retry_git_operation(operation: Callable[[], T], *, attempts: int = 3) -> T:
    """Run a Git operation again only for classified transient failures."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if attempt + 1 >= attempts or not is_retryable_git_failure(exc):
                raise
    raise AssertionError("retry loop exhausted without returning or raising")
