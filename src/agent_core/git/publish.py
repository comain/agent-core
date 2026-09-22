"""Publish an explicit, policy-approved subset of a Git workspace.

Products decide which generated artifacts may be published. This module owns
the mechanical safety properties that must be identical everywhere: stage only
those paths, reject residue the product did not explicitly clean up, rebase on
the current remote branch, push the current commit, and verify the remote SHA.

## Why "nothing to publish" is two different answers

A run that produces no diff can mean two things, and a caller has to tell them
apart. Either the product genuinely had nothing to say — and there is nothing
to open a merge request about — or this exact publication already happened and
the branch is sitting on the remote, in which case the work is done and what
remains is to make sure someone is looking at it.

Returning `None` for both, as this did, makes a retry after a failed merge
request indistinguishable from a no-op: the branch is pushed, the MR was never
created, and the retry reports "nothing changed" and stops. So the outcome is a
closed set of three — `PUSHED`, `REUSED_PUBLISHED`, `NO_CHANGES` — and the
difference between the last two is decided by evidence on the remote, not by
guessing.

## The publication marker

The evidence is a trailer on the commit: `Agent-Publication-Id: <id>`. A stable
publication identity produces a stable branch name *and* a marker to check when
that branch already exists, so a second attempt can tell "my earlier push" from
"someone else's branch that happens to have the same name". Comparing content
instead would be wrong in both directions — identical content from a different
publication is not this one, and this publication rebased onto a moved base has
a different tree.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence, Union

from agent_core.git.runner import GitRunner
from agent_core.git.workspace import BotIdentity, PushConflictError, PushPolicyError, validate_branch


#: The trailer that identifies a commit as one publication's own.
PUBLICATION_TRAILER = "Agent-Publication-Id"


class GitPublicationStatus(Enum):
    PUSHED = "pushed"
    #: This publication is already on the remote, verified by its marker.
    REUSED_PUBLISHED = "reused_published"
    #: The product had nothing to say, and nothing was published before.
    NO_CHANGES = "no_changes"


@dataclass(frozen=True)
class GitPublishRequest:
    """A product-approved change set to commit and publish.

    ``paths`` and ``cleanup_paths`` are repository-relative. Any dirty path
    outside both sets is refused, so a caller cannot accidentally publish or
    silently discard another task's work.

    ``base_branch`` is where a publish branch starts when it does not exist
    yet; an existing one is updated in place. They are separate fields because
    conflating them is how a first publication rebases onto a branch that is
    not there and a second one silently starts over from the base, discarding
    the first.
    """

    branch: str
    paths: Sequence[str]
    message: str
    identity: BotIdentity
    cleanup_paths: Sequence[str] = ()
    allow_path: Optional[Callable[[str], bool]] = None
    base_branch: Optional[str] = None
    #: Stable across retries of the same publication. Without it a retry cannot
    #: tell its own earlier push from an unrelated branch of the same name.
    publication_id: str = ""


@dataclass(frozen=True)
class PushedPublication:
    publish_branch: str
    commit_sha: str
    remote_sha: str
    changed_paths: tuple
    committed_at: str
    publication_id: str = ""
    status: GitPublicationStatus = GitPublicationStatus.PUSHED


@dataclass(frozen=True)
class ReusedPublication:
    """The branch is already there and carries this publication's marker.

    Nothing is pushed again — the point is that the caller may go on to make
    sure a merge request exists, which is the step that failed last time.
    """

    publish_branch: str
    remote_sha: str
    publication_id: str
    status: GitPublicationStatus = GitPublicationStatus.REUSED_PUBLISHED


@dataclass(frozen=True)
class NoPublicationChanges:
    publication_id: str = ""
    status: GitPublicationStatus = GitPublicationStatus.NO_CHANGES


class GitScopedPublisher:
    """Commit and push a scoped subset of an existing checkout."""

    def __init__(self, repo_path: str | Path, *, runner: Optional[GitRunner] = None) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        self.runner = runner or GitRunner()

    def publish(self, request: GitPublishRequest) -> "GitPublicationOutcome":
        """Commit and push the approved paths, or explain why nothing moved."""
        branch = validate_branch(request.branch)
        requested = _paths(request.paths)
        cleanup = _paths(request.cleanup_paths)
        if set(requested) & set(cleanup):
            raise ValueError("publish and cleanup paths must not overlap")
        if request.allow_path is not None:
            refused = [path for path in requested if not request.allow_path(path)]
            if refused:
                raise PushPolicyError(f"refusing to publish disallowed paths: {', '.join(refused)}")

        changed = self.changed_paths()
        unexpected = sorted(
            path
            for path in changed
            if path not in requested and not _is_within_any(path, cleanup)
        )
        if unexpected:
            raise PushPolicyError(
                "refusing to publish unrequested workspace changes: " + ", ".join(unexpected)
            )
        if cleanup:
            self.discard(cleanup)

        selected = tuple(path for path in requested if path in set(self.changed_paths()))
        if not selected:
            # Nothing to say now -- but possibly because it was already said.
            published = self._published_remote_sha(branch, request.publication_id)
            if published is not None:
                return ReusedPublication(
                    publish_branch=branch,
                    remote_sha=published,
                    publication_id=request.publication_id,
                )
            return NoPublicationChanges(publication_id=request.publication_id)

        self._run("config", "user.name", request.identity.name)
        self._run("config", "user.email", request.identity.email)
        self._run("add", "--", *selected)
        self._run("commit", "-m", _message_with_marker(request))

        fetched = self._fetch(branch)
        base = request.base_branch and validate_branch(request.base_branch)
        if fetched:
            target = f"origin/{branch}"
        elif base and self._fetch(base):
            # First publication onto a branch that does not exist yet: start
            # from the base the product named, not from whatever this checkout
            # happened to be sitting on.
            target = f"origin/{base}"
        else:
            target = None

        if target is not None:
            rebased = self._run("rebase", target, check=False)
            if rebased.returncode != 0:
                self._run("rebase", "--abort", check=False)
                raise PushConflictError(
                    f"rebase onto {target} failed: {_combined_output(rebased)}"
                )

        commit_sha = self._output("rev-parse", "HEAD")
        pushed = self._run("push", "-u", "origin", f"HEAD:refs/heads/{branch}", check=False)
        if pushed.returncode != 0:
            raise PushConflictError(f"push failed: {_combined_output(pushed)}")
        remote_ref = self._remote_sha(branch)
        if remote_ref != commit_sha:
            raise PushConflictError(f"remote ref mismatch after push: local={commit_sha} remote={remote_ref}")
        return PushedPublication(
            publish_branch=branch,
            commit_sha=commit_sha,
            remote_sha=remote_ref,
            changed_paths=selected,
            committed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            publication_id=request.publication_id,
        )

    def _fetch(self, branch: str) -> bool:
        """Fetch one branch. False means the remote does not have it.

        Only an explicit missing-ref response means "the branch is not there
        yet". Authentication, transport, and repository failures must stop
        before a push rather than being misclassified as first publication.
        """
        fetched = self._run(
            "fetch",
            "--force",
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            check=False,
        )
        if fetched.returncode == 0:
            return True
        detail = _combined_output(fetched)
        lowered = detail.lower()
        if "couldn't find remote ref" in lowered or "remote ref does not exist" in lowered:
            return False
        raise PushConflictError(f"fetch before push failed: {detail}")

    def _remote_sha(self, branch: str) -> str:
        line = self._output("ls-remote", "origin", f"refs/heads/{branch}")
        return line.split(maxsplit=1)[0] if line else ""

    def _published_remote_sha(self, branch: str, publication_id: str) -> Optional[str]:
        """The remote tip of ``branch`` if it is this publication's own.

        Checked by marker rather than by content: identical content from a
        different publication is not this one, and this publication rebased
        onto a moved base has a different tree. Without a publication id there
        is nothing to verify, so nothing is claimed.
        """
        if not publication_id:
            return None
        remote_sha = self._remote_sha(branch)
        if not remote_sha:
            return None
        if not self._fetch(branch):
            return None
        message = self.runner.output(
            self.repo_path, "log", "-1", "--format=%B", remote_sha
        )
        marker = f"{PUBLICATION_TRAILER}: {publication_id}"
        return remote_sha if marker in message else None

    def changed_paths(self) -> tuple[str, ...]:
        """Return dirty paths, including the destination side of renames."""
        entries = [entry for entry in self._output("status", "--porcelain=1", "-z", "--untracked-files=all").split("\0") if entry]
        paths: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            status, raw_path = entry[:2], entry[2:].lstrip(" ")
            if "R" in status or "C" in status:
                if index < len(entries):
                    raw_path = entries[index]
                    index += 1
            paths.append(_path(raw_path))
        return tuple(sorted(dict.fromkeys(paths)))

    def discard(self, paths: Sequence[str]) -> None:
        """Discard only caller-approved residue from this workspace."""
        for path in paths:
            self._run("checkout", "--", path, check=False)
            self._run("clean", "-fd", "--", path, check=False)

    def _run(self, *args: str, check: bool = True):
        return self.runner.run(self.repo_path, *args, check=check)

    def _output(self, *args: str) -> str:
        return self.runner.output(self.repo_path, *args)


#: The closed set of things "publish" can end in.
GitPublicationOutcome = Union[PushedPublication, ReusedPublication, NoPublicationChanges]


def _message_with_marker(request: GitPublishRequest) -> str:
    """Append the publication trailer, unless the caller already wrote one."""
    message = request.message
    if not request.publication_id:
        return message
    marker = f"{PUBLICATION_TRAILER}: {request.publication_id}"
    if marker in message:
        return message
    separator = "\n" if message.endswith("\n") else "\n\n"
    return f"{message}{separator}{marker}\n"


def _paths(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_path(path) for path in paths))


def _path(value: str) -> str:
    raw = str(value).replace("\\", "/")
    if raw in {"", ".", "./"}:
        raise ValueError(f"path is not a safe repository-relative path: {value!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"path is not a safe repository-relative path: {value!r}")
    normalized = path.as_posix()
    if normalized.startswith("-"):
        raise ValueError(f"path is not a safe repository-relative path: {value!r}")
    return normalized


def _combined_output(completed) -> str:
    return ((completed.stderr or completed.stdout or "").strip())[:400]


def _is_within_any(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


# ---------------------------------------------------------------------------
# Coordinating a publication with a forge
# ---------------------------------------------------------------------------
#
# Pushing a branch and opening a merge request are two writes to two systems
# with no transaction between them. Everything below is about the gap.
#
# The dangerous state is a pushed branch and a merge request that was never
# created: the work is on the remote and nobody knows. A retry has to be able
# to finish the job, which needs three things — a stable branch name so the
# retry looks in the right place, the publication marker so it can tell its own
# branch from someone else's, and a lookup before every create so it never
# opens a second merge request for a branch that already has one.
#
# The other dangerous state is an *ambiguous* create: the POST timed out, and
# the merge request may or may not exist. Retrying blindly duplicates it, and
# giving up strands the branch. So a create that fails ambiguously is followed
# by one more lookup, and only if that finds nothing is the outcome reported as
# failed — with a link a human can use, because the branch is still up there.
#
# A pushed branch is never rolled back. Deleting a remote branch to tidy up
# after a forge error is how a coordinator destroys work that succeeded.

class MergeRequestStatus(Enum):
    CREATED = "created"
    #: One was already open for this branch. Not an error: it is the retry
    #: path working.
    EXISTING = "existing"
    #: No credentials, or a failure that leaves a human to finish it.
    MANUAL = "manual"
    FAILED = "failed"
    NOT_REQUESTED = "not_requested"


@dataclass(frozen=True)
class MergeRequestSpec:
    """What to open, in terms no forge is implied by."""

    project: str
    source_branch: str
    target_branch: str
    title: str


@dataclass(frozen=True)
class PublicationRequest:
    repository_url: str
    base_branch: str
    publish_branch: str
    publication_id: str
    paths: Sequence[str]
    commit_message: str
    identity: BotIdentity
    cleanup_paths: Sequence[str] = ()
    allow_path: Optional[Callable[[str], bool]] = None
    merge_request: Optional[MergeRequestSpec] = None


@dataclass(frozen=True)
class MergeRequestOutcome:
    """What ensuring a merge request produced.

    Carries the original exception rather than only a formatted sentence: a
    product that reports its own message needs the cause, and re-parsing one
    out of prose is how a message change becomes someone else's bug.
    """

    status: MergeRequestStatus
    url: Optional[str] = None
    manual_url: Optional[str] = None
    detail: str = ""
    error: Optional[BaseException] = None


@dataclass(frozen=True)
class PublicationResult:
    git: "GitPublicationOutcome"
    merge_request_status: MergeRequestStatus = MergeRequestStatus.NOT_REQUESTED
    merge_request_url: Optional[str] = None
    #: Where a human can finish what the API could not.
    manual_merge_request_url: Optional[str] = None
    detail: str = ""


def ensure_merge_request(publisher, spec: MergeRequestSpec) -> MergeRequestOutcome:
    """Make sure exactly one merge request exists for ``spec``.

    Query, create once, and — only if the create failed ambiguously — query
    again. The second lookup is the whole point: a timed-out POST may well have
    succeeded, and the difference between "create it" and "check whether it is
    already there" is the difference between one merge request and two.
    """
    existing = _find(publisher, spec)
    if existing:
        return MergeRequestOutcome(
            status=MergeRequestStatus.EXISTING,
            url=existing,
            detail="a merge request was already open",
        )

    try:
        url = publisher.create_merge_request(spec)
    except Exception as exc:  # noqa: BLE001 - the forge's failure, not ours
        settled = _find(publisher, spec)
        if settled:
            # The create did land; only the answer was lost.
            return MergeRequestOutcome(
                status=MergeRequestStatus.CREATED,
                url=settled,
                detail="created despite an ambiguous reply",
            )
        return MergeRequestOutcome(
            status=MergeRequestStatus.FAILED,
            manual_url=_manual_url(publisher, spec),
            detail=f"the forge rejected the merge request: {exc}"[:400],
            error=exc,
        )

    if url:
        return MergeRequestOutcome(status=MergeRequestStatus.CREATED, url=url)
    # No credentials configured: the branch is up and a human can open it.
    return MergeRequestOutcome(
        status=MergeRequestStatus.MANUAL,
        manual_url=_manual_url(publisher, spec),
        detail="no forge credentials; open the merge request by hand",
    )


def _find(publisher, spec: MergeRequestSpec) -> Optional[str]:
    finder = getattr(publisher, "find_merge_request", None)
    if not callable(finder):
        return None
    try:
        return finder(spec)
    except Exception:  # noqa: BLE001 - a failed lookup is not a failed publication
        return None


def _manual_url(publisher, spec: MergeRequestSpec) -> Optional[str]:
    builder = getattr(publisher, "manual_url", None)
    if not callable(builder):
        return None
    try:
        return builder(spec)
    except Exception:  # noqa: BLE001
        return None


class PublicationCoordinator:
    """Clone, mutate once, publish, and make sure a merge request exists."""

    def __init__(
        self,
        workspace,
        *,
        merge_requests=None,
        runner: Optional[GitRunner] = None,
    ) -> None:
        self.workspace = workspace
        self.merge_requests = merge_requests
        self.runner = runner or GitRunner()

    def publish(self, request: PublicationRequest, *, mutate) -> PublicationResult:
        """Run ``mutate(checkout_path)`` once and publish what it approved."""
        publish_branch = validate_branch(request.publish_branch)
        base_branch = validate_branch(request.base_branch)

        path = self.workspace.prepare(
            request.repository_url,
            branch=base_branch,
            local_branch=True,
            scope=f"publication-{request.publication_id}" if request.publication_id else None,
            fresh=True,
        )
        self._checkout_publish_branch(path, publish_branch)

        # Exactly once. A mutation run twice against one checkout has produced
        # doubled files and duplicated appends in every product that tried it.
        mutate(path)

        outcome = GitScopedPublisher(path, runner=self.runner).publish(
            GitPublishRequest(
                branch=publish_branch,
                base_branch=base_branch,
                publication_id=request.publication_id,
                paths=request.paths,
                cleanup_paths=request.cleanup_paths,
                message=request.commit_message,
                identity=request.identity,
                allow_path=request.allow_path,
            )
        )

        if outcome.status is GitPublicationStatus.NO_CHANGES:
            # Nothing was published and nothing was published before. There is
            # nothing for a reviewer to look at.
            return PublicationResult(git=outcome, detail="no approved changes")
        if request.merge_request is None or self.merge_requests is None:
            return PublicationResult(git=outcome)

        merge_request = ensure_merge_request(
            self.merge_requests, request.merge_request
        )
        return PublicationResult(
            git=outcome,
            merge_request_status=merge_request.status,
            merge_request_url=merge_request.url,
            manual_merge_request_url=merge_request.manual_url,
            detail=merge_request.detail,
        )

    def _checkout_publish_branch(self, path: Path, branch: str) -> None:
        """Continue an existing publish branch, or start one from the base."""
        fetched = self.runner.run(
            path,
            "fetch",
            "--force",
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            check=False,
        )
        if fetched.returncode == 0:
            self.runner.run(path, "checkout", "-B", branch, f"origin/{branch}")
        else:
            self.runner.run(path, "checkout", "-B", branch)
