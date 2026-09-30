"""Shared classification of transient Git failures."""

import pytest

from agent_core.git import (
    GitCancelled,
    GitCommandError,
    GitTimeout,
    is_retryable_git_failure,
    retry_git_operation,
)


@pytest.mark.parametrize(
    "error",
    [
        GitTimeout("fetch timed out"),
        GitCommandError(["git", "fetch"], 128, "connection reset"),
        GitCommandError(["git", "fetch", "origin"], 128, "connection reset"),
        GitCommandError(["git", "clone", "repo"], 128, "early EOF"),
    ],
)
def test_transient_git_failures_are_retryable(error):
    assert is_retryable_git_failure(error)


@pytest.mark.parametrize(
    "error",
    [
        GitCancelled("operator cancelled"),
        GitCommandError(["git"], 128, "connection reset"),
        GitCommandError(["git", "status"], 128, "bad revision"),
        GitCommandError(["git", "fetch"], 128, "bad revision"),
        RuntimeError("invalid review output"),
    ],
)
def test_permanent_or_non_git_failures_are_not_retryable(error):
    assert not is_retryable_git_failure(error)


def test_retry_git_operation_retries_transient_failures_until_success():
    calls = []

    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise GitTimeout("fetch timed out")
        return "ok"

    assert retry_git_operation(operation, attempts=3) == "ok"
    assert len(calls) == 3


def test_retry_git_operation_does_not_retry_permanent_failures():
    calls = []

    def operation():
        calls.append(1)
        raise RuntimeError("remote has no master branch")

    with pytest.raises(RuntimeError, match="no master branch"):
        retry_git_operation(operation, attempts=3)
    assert len(calls) == 1
