"""Pushing a branch and opening a merge request, with no transaction between.

Everything here is about the gap between those two writes. The dangerous state
is a pushed branch whose merge request was never created: the work is on the
remote and nobody knows. A retry has to be able to finish the job without
pushing twice or opening a second merge request — which is what the stable
branch, the publication marker, and the lookup-before-create are for.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.git import (
    BotIdentity,
    GitPublicationStatus,
    GitWorkspace,
    MergeRequestSpec,
    MergeRequestStatus,
    PublicationCoordinator,
    PublicationRequest,
    PushPolicyError,
    PushConflictError,
)
from agent_core.git.publish import GitScopedPublisher

BOT = BotIdentity(name="agent-bot", email="agent@local")
SPEC = MergeRequestSpec(
    project="group/project",
    source_branch="agent/op-1",
    target_branch="main",
    title="Generated tests",
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "seed@local")
    _git(seed, "config", "user.name", "seed")
    (seed / "README.md").write_text("initial\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "push", "-q", "origin", "main")
    return remote


@pytest.fixture
def coordinator(tmp_path, forge):
    workspace = GitWorkspace(tmp_path / "work")
    return PublicationCoordinator(workspace, merge_requests=forge)


class Forge:
    """A merge-request publisher that records exactly what it was asked."""

    def __init__(self, *, existing=None, create=lambda spec: "https://forge/mr/1"):
        self.existing = existing
        self._create = create
        self.calls: list = []

    def find_merge_request(self, spec):
        self.calls.append("find")
        return self.existing

    def create_merge_request(self, spec):
        self.calls.append("create")
        url = self._create(spec)
        self.existing = url
        return url

    def manual_url(self, spec):
        return f"https://forge/{spec.project}/new?source={spec.source_branch}"


@pytest.fixture
def forge():
    return Forge()


def request_for(origin, **over):
    fields = dict(
        repository_url=str(origin),
        base_branch="main",
        publish_branch="agent/op-1",
        publication_id="op-1",
        paths=("tests/test_generated.py",),
        commit_message="Publish generated tests",
        identity=BOT,
        merge_request=SPEC,
    )
    fields.update(over)
    return PublicationRequest(**fields)


def write_tests(path, content="def test_generated(): pass\n"):
    (path / "tests").mkdir(exist_ok=True)
    (path / "tests" / "test_generated.py").write_text(content)


def test_fetch_failure_is_not_mistaken_for_a_missing_publish_branch(tmp_path, monkeypatch):
    publisher = GitScopedPublisher(tmp_path)
    monkeypatch.setattr(
        publisher,
        "_run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=128, stdout="", stderr="authentication failed"
        ),
    )

    with pytest.raises(PushConflictError, match="fetch before push failed"):
        publisher._fetch("agent/op-1")


def test_explicit_missing_ref_allows_first_publication(tmp_path, monkeypatch):
    publisher = GitScopedPublisher(tmp_path)
    monkeypatch.setattr(
        publisher,
        "_run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=128,
            stdout="",
            stderr="fatal: couldn't find remote ref agent/op-1",
        ),
    )

    assert publisher._fetch("agent/op-1") is False


# -- the happy path --------------------------------------------------------

def test_a_publication_pushes_once_and_opens_one_merge_request(origin, coordinator, forge):
    result = coordinator.publish(request_for(origin), mutate=write_tests)

    assert result.git.status is GitPublicationStatus.PUSHED
    assert result.merge_request_status is MergeRequestStatus.CREATED
    assert result.merge_request_url == "https://forge/mr/1"
    assert forge.calls == ["find", "create"], "it created without looking first"
    assert _git(origin, "rev-parse", "agent/op-1") == result.git.commit_sha


def test_the_mutation_runs_exactly_once(origin, coordinator):
    """A mutation run twice against one checkout has produced doubled files and
    duplicated appends in every product that tried it."""
    runs: list = []

    def mutate(path):
        runs.append(path)
        write_tests(path)

    coordinator.publish(request_for(origin), mutate=mutate)

    assert len(runs) == 1


def test_the_publish_branch_is_not_the_base_branch(origin, coordinator):
    coordinator.publish(request_for(origin), mutate=write_tests)

    assert _git(origin, "rev-parse", "main") != _git(origin, "rev-parse", "agent/op-1")


# -- the gap ---------------------------------------------------------------

def test_a_retry_after_the_merge_request_failed_finishes_the_job(origin, tmp_path):
    """The dangerous state: branch pushed, nobody told. The retry must neither
    push again nor stop at "nothing changed"."""
    workspace = GitWorkspace(tmp_path / "work")
    broken = Forge(create=_raise)
    first = PublicationCoordinator(workspace, merge_requests=broken).publish(
        request_for(origin), mutate=write_tests
    )
    assert first.git.status is GitPublicationStatus.PUSHED
    assert first.merge_request_status is MergeRequestStatus.FAILED
    assert first.manual_merge_request_url

    working = Forge()
    again = PublicationCoordinator(
        GitWorkspace(tmp_path / "work2"), merge_requests=working
    ).publish(request_for(origin), mutate=write_tests)

    assert again.git.status is GitPublicationStatus.REUSED_PUBLISHED
    assert again.merge_request_status is MergeRequestStatus.CREATED
    assert _git(origin, "rev-list", "--count", "agent/op-1") == "2", "it pushed again"


def test_an_existing_merge_request_is_not_duplicated(origin, tmp_path):
    workspace = GitWorkspace(tmp_path / "work")
    forge = Forge(existing="https://forge/mr/7")

    result = PublicationCoordinator(workspace, merge_requests=forge).publish(
        request_for(origin), mutate=write_tests
    )

    assert result.merge_request_status is MergeRequestStatus.EXISTING
    assert result.merge_request_url == "https://forge/mr/7"
    assert "create" not in forge.calls


def test_an_ambiguous_create_is_resolved_by_looking_again(origin, tmp_path):
    """A timed-out POST may well have succeeded. Retrying blindly duplicates
    the merge request; giving up strands the branch."""
    class Ambiguous(Forge):
        def create_merge_request(self, spec):
            self.calls.append("create")
            self.existing = "https://forge/mr/9"   # it landed
            raise RuntimeError("read timeout")     # the answer did not

    forge = Ambiguous()
    result = PublicationCoordinator(
        GitWorkspace(tmp_path / "work"), merge_requests=forge
    ).publish(request_for(origin), mutate=write_tests)

    assert result.merge_request_status is MergeRequestStatus.CREATED
    assert result.merge_request_url == "https://forge/mr/9"
    assert forge.calls == ["find", "create", "find"]


def test_a_definitive_failure_keeps_the_branch_and_offers_a_manual_link(
    origin, tmp_path
):
    """A pushed branch is never rolled back: deleting a remote branch to tidy
    up after a forge error destroys work that succeeded."""
    forge = Forge(create=_raise)

    result = PublicationCoordinator(
        GitWorkspace(tmp_path / "work"), merge_requests=forge
    ).publish(request_for(origin), mutate=write_tests)

    assert result.merge_request_status is MergeRequestStatus.FAILED
    assert result.manual_merge_request_url.startswith("https://forge/group/project")
    assert _git(origin, "rev-parse", "agent/op-1")


def _raise(spec):
    raise RuntimeError("the forge is down")


def test_missing_credentials_are_reported_as_manual_not_failed(origin, tmp_path):
    """Nothing is broken; a human just has to click the link."""
    forge = Forge(create=lambda spec: None)

    result = PublicationCoordinator(
        GitWorkspace(tmp_path / "work"), merge_requests=forge
    ).publish(request_for(origin), mutate=write_tests)

    assert result.merge_request_status is MergeRequestStatus.MANUAL
    assert result.manual_merge_request_url


# -- nothing to publish ----------------------------------------------------

def test_a_product_no_op_asks_for_no_merge_request(origin, coordinator, forge):
    result = coordinator.publish(request_for(origin), mutate=lambda path: None)

    assert result.git.status is GitPublicationStatus.NO_CHANGES
    assert result.merge_request_status is MergeRequestStatus.NOT_REQUESTED
    assert forge.calls == []


def test_a_publication_without_a_merge_request_spec_publishes_and_stops(
    origin, coordinator, forge
):
    result = coordinator.publish(
        request_for(origin, merge_request=None), mutate=write_tests
    )

    assert result.git.status is GitPublicationStatus.PUSHED
    assert result.merge_request_status is MergeRequestStatus.NOT_REQUESTED
    assert forge.calls == []


# -- refusals --------------------------------------------------------------

def test_an_unapproved_path_stops_before_any_forge_work(origin, coordinator, forge):
    def mutate(path):
        write_tests(path)
        (path / "unrelated.txt").write_text("someone else's work\n")

    with pytest.raises(PushPolicyError):
        coordinator.publish(request_for(origin), mutate=mutate)

    assert forge.calls == []
    assert "agent/op-1" not in _git(origin, "branch", "--list")


def test_a_second_approved_change_lands_on_top_of_the_first(origin, tmp_path, forge):
    """Not on a branch that silently restarted from the base and discarded it."""
    workspace = GitWorkspace(tmp_path / "work")
    coordinator = PublicationCoordinator(workspace, merge_requests=forge)
    coordinator.publish(request_for(origin), mutate=write_tests)

    def add_more(path):
        (path / "tests" / "test_more.py").write_text("def test_more(): pass\n")

    result = coordinator.publish(
        request_for(origin, paths=("tests/test_more.py",)), mutate=add_more
    )

    assert result.git.status is GitPublicationStatus.PUSHED
    assert _git(origin, "ls-tree", "-r", "--name-only", "agent/op-1").split() == [
        "README.md",
        "tests/test_generated.py",
        "tests/test_more.py",
    ]


def test_the_outcome_carries_the_cause_not_only_a_sentence(origin, tmp_path):
    """A product that reports its own message needs the exception. Re-parsing
    one out of prose is how a message change becomes someone else's bug."""
    from agent_core.git import ensure_merge_request

    failure = RuntimeError("timeout")

    def boom(spec):
        raise failure

    outcome = ensure_merge_request(Forge(create=boom), SPEC)

    assert outcome.status is MergeRequestStatus.FAILED
    assert outcome.error is failure
    assert outcome.manual_url
