"""Real-repository tests for publishing a scoped set of workspace changes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_core.git import (
    BotIdentity,
    GitPublicationStatus,
    GitPublishRequest,
    GitScopedPublisher,
    PushPolicyError,
)


BOT = BotIdentity(name="agent-bot", email="agent@local")


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "seed@local")
    _git(repo, "config", "user.name", "seed")
    (repo / "README.md").write_text("initial\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "push", "-q", "-u", "origin", "main")
    return origin, repo


def _request(*paths: str, cleanup_paths: tuple[str, ...] = ()) -> GitPublishRequest:
    return GitPublishRequest(
        branch="main",
        paths=paths,
        cleanup_paths=cleanup_paths,
        message="Publish generated tests\n\nGenerated-by: test",
        identity=BOT,
    )


def test_publishes_only_requested_paths(workspace: tuple[Path, Path]) -> None:
    origin, repo = workspace
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("def test_generated(): pass\n")

    result = GitScopedPublisher(repo).publish(_request("tests/test_generated.py"))

    assert result.status is GitPublicationStatus.PUSHED
    assert result.changed_paths == ("tests/test_generated.py",)
    assert result.remote_sha == result.commit_sha
    assert _git(origin, "show", "--name-only", "--format=", "main") == "tests/test_generated.py"


def test_publishes_a_modified_tracked_path(workspace: tuple[Path, Path]) -> None:
    origin, repo = workspace
    (repo / "README.md").write_text("updated\n")

    result = GitScopedPublisher(repo).publish(_request("README.md"))

    assert result is not None
    assert result.changed_paths == ("README.md",)
    assert _git(origin, "show", "--name-only", "--format=", "main") == "README.md"


def test_refuses_unrequested_workspace_changes(workspace: tuple[Path, Path]) -> None:
    _, repo = workspace
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("pass\n")
    (repo / "production.py").write_text("do not publish\n")

    with pytest.raises(PushPolicyError, match="production.py"):
        GitScopedPublisher(repo).publish(_request("tests/test_generated.py"))


def test_discards_only_explicit_cleanup_paths(workspace: tuple[Path, Path]) -> None:
    _, repo = workspace
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("pass\n")
    (repo / ".uta_cache").mkdir()
    (repo / ".uta_cache" / "scratch.txt").write_text("runtime\n")

    result = GitScopedPublisher(repo).publish(
        _request("tests/test_generated.py", cleanup_paths=(".uta_cache/scratch.txt",))
    )

    assert result is not None
    assert not (repo / ".uta_cache" / "scratch.txt").exists()


def test_cleanup_root_discards_its_dirty_descendants(workspace: tuple[Path, Path]) -> None:
    _, repo = workspace
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("pass\n")
    (repo / ".uta_cache" / "nested").mkdir(parents=True)
    (repo / ".uta_cache" / "nested" / "scratch.txt").write_text("runtime\n")

    result = GitScopedPublisher(repo).publish(
        _request("tests/test_generated.py", cleanup_paths=(".uta_cache",))
    )

    assert result is not None
    assert not (repo / ".uta_cache").exists()


def test_an_unchanged_workspace_with_no_prior_publication_is_no_changes(
    workspace: tuple[Path, Path],
) -> None:
    """A true product no-op. Distinct from "already published", which a caller
    must be able to act on differently."""
    _, repo = workspace

    result = GitScopedPublisher(repo).publish(_request("tests/test_generated.py"))

    assert result.status is GitPublicationStatus.NO_CHANGES


def test_rebases_onto_the_current_remote_tip(workspace: tuple[Path, Path], tmp_path: Path) -> None:
    origin, repo = workspace
    remote_writer = tmp_path / "remote-writer"
    subprocess.run(["git", "clone", "-q", str(origin), str(remote_writer)], check=True, capture_output=True)
    _git(remote_writer, "config", "user.email", "remote@local")
    _git(remote_writer, "config", "user.name", "remote")
    (remote_writer / "README.md").write_text("remote change\n")
    _git(remote_writer, "commit", "-am", "remote update")
    _git(remote_writer, "push", "-q", "origin", "main")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("pass\n")

    result = GitScopedPublisher(repo).publish(_request("tests/test_generated.py"))

    assert result is not None
    log = _git(origin, "log", "--format=%s", "main")
    assert "remote update" in log
    assert "Publish generated tests" in log


@pytest.mark.parametrize("path", ["", ".", "/etc/passwd", "../outside", "tests/../outside", "-option"])
def test_rejects_unsafe_publish_paths(workspace: tuple[Path, Path], path: str) -> None:
    _, repo = workspace

    with pytest.raises(ValueError):
        GitScopedPublisher(repo).publish(_request(path))


# -- the publication algebra -------------------------------------------------

def _publication(*paths: str, publication_id: str = "op-1", base_branch: str = "main",
                 branch: str = "agent/op-1") -> GitPublishRequest:
    return GitPublishRequest(
        branch=branch,
        base_branch=base_branch,
        publication_id=publication_id,
        paths=paths,
        message="Publish generated tests",
        identity=BOT,
    )


def test_a_first_publication_creates_the_branch_from_the_base(
    workspace: tuple[Path, Path],
) -> None:
    """The branch does not exist yet, so there is nothing to rebase onto but
    the base the product named."""
    origin, repo = workspace
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("def test_generated(): pass\n")

    result = GitScopedPublisher(repo).publish(_publication("tests/test_generated.py"))

    assert result.status is GitPublicationStatus.PUSHED
    assert result.publish_branch == "agent/op-1"
    assert _git(origin, "rev-parse", "agent/op-1") == result.commit_sha
    assert _git(origin, "rev-parse", "main") != result.commit_sha, "the base moved"


def test_the_commit_carries_the_publication_marker(workspace: tuple[Path, Path]) -> None:
    origin, repo = workspace
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("x\n")

    GitScopedPublisher(repo).publish(_publication("tests/test_generated.py"))

    assert "Agent-Publication-Id: op-1" in _git(
        origin, "log", "-1", "--format=%B", "agent/op-1"
    )


def test_a_retry_after_a_successful_push_reuses_it_without_pushing_again(
    workspace: tuple[Path, Path],
) -> None:
    """The case `None` could not express: the branch is up, the merge request
    was never created, and the retry has to know to go and create it."""
    origin, repo = workspace
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("x\n")
    first = GitScopedPublisher(repo).publish(_publication("tests/test_generated.py"))

    again = GitScopedPublisher(repo).publish(_publication("tests/test_generated.py"))

    assert again.status is GitPublicationStatus.REUSED_PUBLISHED
    assert again.remote_sha == first.commit_sha
    assert again.publication_id == "op-1"
    assert _git(origin, "rev-list", "--count", "agent/op-1") == "2", "it pushed again"


def test_a_branch_of_the_same_name_from_another_publication_is_not_reused(
    workspace: tuple[Path, Path],
) -> None:
    """Same name is not the same publication. Reuse is decided by the marker,
    because identical content from a different run is still not this run."""
    origin, repo = workspace
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("x\n")
    GitScopedPublisher(repo).publish(_publication("tests/test_generated.py"))

    result = GitScopedPublisher(repo).publish(
        _publication("tests/test_generated.py", publication_id="op-2")
    )

    assert result.status is GitPublicationStatus.NO_CHANGES


def test_a_publication_without_an_id_never_claims_a_reuse(
    workspace: tuple[Path, Path],
) -> None:
    """Nothing to verify means nothing is claimed."""
    _, repo = workspace

    result = GitScopedPublisher(repo).publish(
        _publication("tests/test_generated.py", publication_id="")
    )

    assert result.status is GitPublicationStatus.NO_CHANGES


def test_an_existing_publish_branch_is_updated_not_restarted(
    workspace: tuple[Path, Path],
) -> None:
    """A second approved change belongs on top of the first, not on a branch
    that silently discarded it."""
    origin, repo = workspace
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("first\n")
    GitScopedPublisher(repo).publish(_publication("tests/test_generated.py"))

    (repo / "tests" / "test_more.py").write_text("second\n")
    result = GitScopedPublisher(repo).publish(_publication("tests/test_more.py"))

    assert result.status is GitPublicationStatus.PUSHED
    assert _git(origin, "ls-tree", "-r", "--name-only", "agent/op-1").split() == [
        "README.md",
        "tests/test_generated.py",
        "tests/test_more.py",
    ]


def test_an_unexpected_dirty_path_stops_the_publication_before_the_commit(
    workspace: tuple[Path, Path],
) -> None:
    origin, repo = workspace
    (repo / "tests").mkdir()
    (repo / "tests" / "test_generated.py").write_text("x\n")
    (repo / "unrelated.txt").write_text("someone else's work\n")

    with pytest.raises(PushPolicyError):
        GitScopedPublisher(repo).publish(_publication("tests/test_generated.py"))

    assert _git(repo, "log", "-1", "--format=%s") == "initial", "it committed anyway"
    assert "agent/op-1" not in _git(origin, "branch", "--list")
