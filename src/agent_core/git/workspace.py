"""Clone a repository into a local workspace, and push what the agent produced.

Both directions, because every consumer needs both. The **read** path — fetch a
repo at a branch or commit and build a local workspace from it — is what all
four do before an agent runs; the **write** path is how the two that generate
code publish the result. A reviewer uses only the first half, and that is fine.

Concurrency is the part that is easy to get wrong. Several tasks routinely
target the same repository, and a shared clone being fetched by one task while
another checks out a commit produces failures that look like flaky git. A
per-repository lock serialises access to each cache directory.

Two properties are load-bearing on the read path and were each present in only
one consumer before this was shared:

* **Cancellation interrupts a *running* command.** Checking a flag between
  commands leaves an operator unable to stop a ten-minute clone, so commands are
  run under a poll loop and killed by process *group* — git delegates to ssh,
  and killing only git orphans the ssh it spawned.
* **Untrusted input is refused before it reaches git.** Branch names and clone
  URLs arrive in request bodies; a "branch" of `--upload-pack=…` is an option,
  not a branch, and would execute a command.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Union
from urllib.parse import urlparse

from agent_core.git.identity import (
    env_with_identity,
    has_access_token,
    repo_name_from_url,
    ssh_command_for_key,
    url_for_access_token,
)
from agent_core.paths import ensure_private_directory

logger = logging.getLogger(__name__)


def _is_windows() -> bool:
    return os.name == "nt"


def _require_posix_flock() -> None:
    if _is_windows():
        raise RuntimeError(
            "GitWorkspace.repo_lock requires POSIX flock; Windows is unsupported"
        )


def _flock(handle: int, exclusive: bool) -> None:
    import fcntl

    fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_UN)


#: What a commit looks like, so `git rev-parse` echoing an unresolvable
#: argument back at us is not mistaken for one.
_COMMIT = re.compile(r"[0-9a-f]{7,40}")

class GitCommandError(RuntimeError):
    """A git subprocess failed."""

    def __init__(self, cmd: List[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        # The URL may carry a token; report the subcommand, not the full line.
        super().__init__(f"git {cmd[1] if len(cmd) > 1 else '?'} failed ({returncode}): {stderr[:400]}")


class GitCancelled(RuntimeError):
    """The owning task was cancelled while a git command was running."""


class GitTimeout(RuntimeError):
    """A git command outlived its timeout and was killed."""


class PushPolicyError(RuntimeError):
    """The change violates what this product is allowed to publish."""


class PushConflictError(RuntimeError):
    """The remote moved, or the push did not land as expected."""


@dataclass
class GitCredentials:
    """How to authenticate. A token wins over a key when both are set."""

    ssh_key_path: str = ""
    access_token: str = ""
    token_host: str = ""

    @property
    def uses_token(self) -> bool:
        return has_access_token(self.access_token)


#: What a branch name may contain. Deliberately narrow: the value is passed to
#: git as an argument, and a name beginning with `-` is read as an *option*, so
#: `--upload-pack=...` in a request body would otherwise run a command.
_SAFE_BRANCH = re.compile(r"[A-Za-z0-9._/-]+")


def validate_branch(branch: str) -> str:
    """Return ``branch`` if it is safe to pass to git, else raise.

    The rules mirror git's own refname restrictions, plus a leading-dash ban
    that git itself does not impose.
    """
    if not branch or len(branch) > 255:
        raise ValueError("branch is invalid: empty or too long")
    unsafe = (
        branch.startswith(("-", "/", ".")),
        branch.endswith(("/", ".", ".lock")),
        ".." in branch,
        "//" in branch,
        "@{" in branch,
        "\\" in branch,
        not _SAFE_BRANCH.fullmatch(branch),
    )
    if any(unsafe):
        raise ValueError(f"branch is invalid: {branch[:80]!r}")
    return branch


def git_url_host(git_url: str) -> str:
    """Host of a clone URL, or "" if it is not a form we accept."""
    if "://" in git_url:
        parsed = urlparse(git_url)
        if parsed.scheme not in {"ssh", "https"}:
            return ""
        return parsed.hostname or ""
    match = re.match(r"^[^@\s]+@([^:\s]+):\S+$", git_url)
    return match.group(1) if match else ""


@dataclass
class BotIdentity:
    """Who commits appear to be from. Product-specific, so not defaulted here."""

    name: str
    email: str


def _safe_path_component(value: str, *, kind: str) -> str:
    """A single directory name, or a refusal.

    Both the repository name and the scope are derived from values that may
    have arrived in a request body, and both become directories under the
    cache. Neither may traverse out of it or be read as an option by a command
    that later receives the path.
    """
    candidate = str(value or "")
    if (
        not candidate
        or candidate in {".", ".."}
        or "/" in candidate
        or "\\" in candidate
        or candidate.startswith("-")
    ):
        raise ValueError(f"unusable {kind}: {candidate[:80]!r}")
    return candidate


class GitWorkspace:
    """Manages cloned repositories under a cache directory."""

    def __init__(
        self,
        cache_dir: Union[str, Path],
        credentials: Optional[GitCredentials] = None,
        *,
        clone_depth: Optional[int] = 1,
        timeout: int = 600,
        allowed_hosts: Optional[Sequence[str]] = None,
        git_bin: str = "git",
        poll_interval: float = 0.2,
        retry_times: int = 0,
        retry_delay_seconds: float = 0.0,
    ):
        self.cache_dir = Path(cache_dir)
        self.credentials = credentials or GitCredentials()
        self.clone_depth = clone_depth
        self.timeout = timeout
        # None means "no allowlist". An empty list would then read as "deny
        # everything", which is not what a deployment that simply has no
        # allowlist configured means.
        self.allowed_hosts = list(allowed_hosts) if allowed_hosts is not None else None
        self.git_bin = git_bin
        self.poll_interval = poll_interval
        # Off by default: retrying is only ever right for a consumer that
        # knows its remote is flaky, and a silent extra attempt would surprise
        # one that does not.
        self.retry_times = max(0, retry_times)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._locks: Dict[str, threading.RLock] = {}
        self._lock_depth: Dict[str, int] = {}
        self._flock_fds: Dict[str, int] = {}
        self._locks_guard = threading.Lock()
        self._env = env_with_identity(
            ssh_key_path=self.credentials.ssh_key_path,
            access_token=self.credentials.access_token,
            token_host=self.credentials.token_host,
        )

    # -- locking -----------------------------------------------------------

    def _lock_key(self, repo_url: str, scope: Optional[str]) -> str:
        name = repo_name_from_url(repo_url)
        if scope is not None:
            name = f"{scope}/{name}"
        return name

    def _lock_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / ".locks" / f"{digest}.lock"

    def _acquire_flock(self, key: str) -> int:
        import stat

        ensure_private_directory(self.cache_dir / ".locks")
        path = self._lock_path(key)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(path, flags, 0o600)
        try:
            os.fchmod(handle, 0o600)
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise PermissionError(f"{path} is not a private regular file")
            _flock(handle, exclusive=True)
        except BaseException:
            os.close(handle)
            raise
        return handle

    @contextmanager
    def repo_lock(self, repo_url: str, *, scope: Optional[str] = None) -> Iterator[None]:
        """Serialise access to one checkout across threads and processes.

        Keyed by scope as well as repository: two scopes are separate trees, so
        making them queue behind each other would serialise unrelated tasks for
        no safety benefit. POSIX ``fcntl.flock`` plus an in-process ``RLock``:
        flock does not exclude threads that each open their own descriptor.
        Windows is unsupported (no fake lock).
        """
        _require_posix_flock()
        key = self._lock_key(repo_url, scope)
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.RLock())
            self._lock_depth.setdefault(key, 0)
        lock.acquire()
        try:
            if self._lock_depth[key] == 0:
                self._flock_fds[key] = self._acquire_flock(key)
            self._lock_depth[key] += 1
            try:
                yield
            finally:
                self._lock_depth[key] -= 1
                if self._lock_depth[key] == 0:
                    handle = self._flock_fds.pop(key)
                    try:
                        _flock(handle, exclusive=False)
                    finally:
                        os.close(handle)
        finally:
            lock.release()

    def path_for(self, repo_url: str, *, scope: Optional[str] = None) -> Path:
        """Where ``repo_url`` is checked out, optionally isolated by ``scope``.

        Without a scope the checkout is shared: one clone per repository,
        refreshed on reuse. That is right for a service running repeatedly
        against the same repositories, and much cheaper than re-cloning.

        With a scope each caller gets its own tree under
        ``cache_dir/<scope>/<repo>``. CI needs this: two tasks on one
        repository must not share a working tree, or one task's half-applied
        patch or leftover build output silently becomes another's baseline,
        and the damage surfaces as an unexplained diff somewhere unrelated.
        """
        name = _safe_path_component(repo_name_from_url(repo_url), kind="repository name from url")
        if scope is None:
            return self.cache_dir / name
        return self.cache_dir / _safe_path_component(scope, kind="workspace scope") / name

    def _check_url(self, repo_url: str) -> str:
        if self.allowed_hosts is None:
            return repo_url
        host = git_url_host(repo_url)
        if host not in self.allowed_hosts:
            raise ValueError(f"git url host is not allowed: {host or 'unknown'}")
        return repo_url

    # -- running git -------------------------------------------------------

    def _run(
        self,
        cmd: List[str],
        *,
        is_cancelled: Optional[Callable[[], bool]] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run one git command, retrying only what is worth retrying.

        Which failures deserve another attempt is
        :func:`agent_core.git.is_retryable_git_failure`\'s decision, so a
        transport error on a clone or fetch is retried and a bad ref or a
        missing repository fails immediately -- retrying those only makes the
        error slower to arrive, and the delay makes it look like a network
        problem.

        A failure is classified even when ``check`` is False, so a caller that
        inspects the result itself (``refresh_ref`` reads stderr to tell a
        missing branch from an unreachable remote) still gets the benefit.
        """
        # Imported here, not at module scope: the classifier needs this
        # module's exception types, so the dependency only points one way.
        from agent_core.git.retry import is_retryable_git_failure

        attempts = self.retry_times + 1
        for attempt in range(1, attempts + 1):
            if is_cancelled is not None and is_cancelled():
                raise GitCancelled("cancelled before running git")
            try:
                completed = self._run_polling(cmd, is_cancelled=is_cancelled)
            except GitTimeout as exc:
                failure: BaseException = exc
                completed = None
            else:
                if completed.returncode == 0:
                    return completed
                failure = GitCommandError(cmd, completed.returncode, completed.stderr or "")

            last = attempt >= attempts
            if not last and is_retryable_git_failure(failure):
                logger.warning(
                    "git %s failed transiently (attempt %s/%s), retrying: %s",
                    cmd[1] if len(cmd) > 1 else "?", attempt, attempts, failure,
                )
                if self.retry_delay_seconds:
                    time.sleep(self.retry_delay_seconds)
                continue

            if completed is None:
                raise failure
            if check:
                raise failure
            return completed
        raise AssertionError("git retry loop exhausted without returning or raising")

    def _run_polling(
        self,
        cmd: List[str],
        *,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> subprocess.CompletedProcess:
        """Run git, staying responsive to cancellation while it runs.

        `subprocess.run(timeout=...)` blocks, so cancellation could only be
        noticed *between* commands -- an operator pressing stop during a
        ten-minute clone would wait for the clone. This polls instead.

        The child gets its own session so the whole group can be signalled: git
        delegates to ssh, and killing only git orphans the ssh it spawned.
        """
        argv = [self.git_bin if cmd[0] == "git" else cmd[0], *cmd[1:]]
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env,
            start_new_session=True,
        )
        started = time.monotonic()
        subcommand = cmd[1] if len(cmd) > 1 else "?"
        while True:
            try:
                # Returns as soon as the process exits, so a fast command pays
                # nothing for being cancellable. Crucially it also *drains* the
                # pipes: a loop that only polled would deadlock as soon as git
                # wrote more than a pipe buffer, which clone and fetch do.
                stdout, stderr = process.communicate(timeout=self.poll_interval)
                return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                pass
            if is_cancelled is not None and is_cancelled():
                self._terminate_group(process)
                raise GitCancelled(f"cancelled while running git {subcommand}")
            if self.timeout and time.monotonic() - started >= self.timeout:
                self._terminate_group(process)
                raise GitTimeout(f"git {subcommand} timed out after {self.timeout}s")

    @staticmethod
    def _terminate_group(process: subprocess.Popen, *, grace_seconds: float = 2.0) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(process.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                # Already gone, or we could not form a group -- fall back to the
                # direct child so we never leave it running.
                process.kill() if sig == signal.SIGKILL else process.terminate()
            try:
                process.communicate(timeout=grace_seconds)
                return
            except subprocess.TimeoutExpired:
                continue

    def _output(self, cmd: List[str], **kw) -> str:
        return (self._run(cmd, check=False, **kw).stdout or "").strip()

    def _clone_url(self, repo_url: str) -> str:
        return url_for_access_token(repo_url) if self.credentials.uses_token else repo_url

    # -- preparing a checkout ---------------------------------------------

    def default_branch(
        self,
        repo_url: str,
        *,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> str:
        """The branch the remote's HEAD points at.

        A consumer that clones without naming a branch has to get it from
        somewhere. Guessing `main` or `master` is wrong for exactly the
        repositories where it matters, and cloning first to look is a wasted
        clone when the answer is one `ls-remote`.
        """
        self._check_url(repo_url)
        cmd = ["git", "ls-remote", "--symref", self._clone_url(repo_url), "HEAD"]
        completed = self._run(cmd, is_cancelled=is_cancelled)
        for line in (completed.stdout or "").splitlines():
            # `ref: refs/heads/main\tHEAD`
            if line.startswith("ref:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                    return validate_branch(parts[1][len("refs/heads/"):])
        raise GitCommandError(cmd, 1, f"remote has no default branch: {repo_name_from_url(repo_url)}")

    def has_branch(
        self,
        repo_url: str,
        branch: str,
        *,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Whether the remote has ``branch``, asked before cloning.

        One `ls-remote` costs a round trip; discovering it from a failed clone
        costs the clone and a stack trace.
        """
        self._check_url(repo_url)
        validate_branch(branch)
        completed = self._run(
            ["git", "ls-remote", "--heads", self._clone_url(repo_url), f"refs/heads/{branch}"],
            is_cancelled=is_cancelled,
        )
        return bool((completed.stdout or "").strip())

    def prepare(
        self,
        repo_url: str,
        *,
        branch: Optional[str] = None,
        commit: Optional[str] = None,
        scope: Optional[str] = None,
        local_branch: bool = False,
        fresh: bool = False,
        create_missing: bool = False,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> Path:
        """Clone or refresh ``repo_url`` and check out ``commit`` or the branch tip.

        Returns the checkout path. Safe to call concurrently for the same repo.
        Without ``branch`` the remote's default branch is used.

        ``scope`` isolates the checkout -- see :meth:`path_for`. Two scopes of
        one repository are separate trees that never see each other's work,
        which is what CI needs; reusing a scope refreshes it, so a retry of the
        same task does not pay for another clone.

        ``local_branch`` checks out a local branch of that name instead of
        detaching at ``origin/<branch>``, which a consumer needs when it runs
        its own ``git push <branch>`` from the checkout.

        ``create_missing`` starts the branch from the remote's default branch
        when the remote does not have it. A workflow that owns branch creation
        would otherwise have to clone the default branch itself and then
        re-enter.
        Off by default: for a reviewer, a branch that is not there is a
        mistake in the request, not an invitation to invent one.

        ``fresh`` rebuilds the tree from scratch rather than refreshing it.
        Reuse is the default because it is why the clone is cached at all, but
        ``clean -fd`` keeps ignored files, so a task that compiles would
        otherwise inherit the last one\'s build output.
        """
        self._check_url(repo_url)
        # Only an *absent* branch means the remote's own default. An empty one
        # is a caller that thought it had a branch and did not, and is still
        # refused -- these arrive in request bodies.
        if branch is None:
            branch = self.default_branch(repo_url, is_cancelled=is_cancelled)
        else:
            validate_branch(branch)
        path = self.path_for(repo_url, scope=scope)
        with self.repo_lock(repo_url, scope=scope):
            if fresh and path.exists():
                logger.info("discarding cached %s for a fresh checkout", path.name)
                shutil.rmtree(path)
            started_here = False
            if not path.exists():
                logger.info("cloning %s at %s", repo_name_from_url(repo_url), branch)
                source = branch
                if create_missing and not self.has_branch(
                    repo_url, branch, is_cancelled=is_cancelled
                ):
                    source = self.default_branch(repo_url, is_cancelled=is_cancelled)
                    started_here = True
                    logger.info("branch %s is new; starting it from %s", branch, source)
                clone_args = ["git", "clone", "--no-tags", "--branch", source]
                if self.clone_depth is not None:
                    clone_args += ["--depth", str(self.clone_depth)]
                clone_args += [self._clone_url(repo_url), str(path)]
                self._run(
                    clone_args,
                    is_cancelled=is_cancelled,
                )
            else:
                self._sync_remote_url(path, repo_url, is_cancelled=is_cancelled)
                if not self.refresh_ref(path, branch, is_cancelled=is_cancelled):
                    if not create_missing:
                        raise GitCommandError(
                            ["git", "fetch", branch], 1,
                            f"remote has no branch {branch}",
                        )
                    source = self.default_branch(repo_url, is_cancelled=is_cancelled)
                    if not self.refresh_ref(path, source, is_cancelled=is_cancelled):
                        raise GitCommandError(
                            ["git", "fetch", source], 1,
                            f"remote has no branch {source}",
                        )
                    started_here = True

            if started_here:
                # A branch that does not exist yet has no `origin/<branch>` to
                # detach at: start it where trunk is and leave it local until
                # the run has something to push.
                # A single-branch clone fetches only its own branch; widen the
                # refspec so this one has a remote-tracking ref once pushed.
                self._run(
                    ["git", "-C", str(path), "remote", "set-branches", "--add", "origin", branch],
                    is_cancelled=is_cancelled,
                    check=False,
                )
                self._run(
                    ["git", "-C", str(path), "checkout", "--force", "-B", branch,
                     f"origin/{source}"],
                    is_cancelled=is_cancelled,
                )
                self._run(["git", "-C", str(path), "clean", "-fd"], is_cancelled=is_cancelled)
                logger.info("prepared %s on new branch %s", repo_name_from_url(repo_url), branch)
                return path

            self._pin_ssh_command(path, is_cancelled=is_cancelled)

            ref = commit or f"origin/{branch}"
            if local_branch and not commit:
                # `-B` resets an existing local branch to the remote tip, so a
                # reused scope does not keep the previous run\'s commits.
                checkout = ["checkout", "--force", "-B", branch, ref]
            else:
                checkout = ["checkout", "--force", ref]
            self._run(["git", "-C", str(path), *checkout], is_cancelled=is_cancelled)
            # A previous task may have left edits; the agent must start clean.
            self._run(["git", "-C", str(path), "clean", "-fd"], is_cancelled=is_cancelled)
            logger.info("prepared %s at %s", repo_name_from_url(repo_url), ref)
        return path

    def _sync_remote_url(self, path: Path, repo_url: str, *, is_cancelled=None) -> None:
        """Keep origin pointing at the URL our current credentials can use.

        A cache directory can outlive a credential change — cloned over SSH,
        now running with a token — and the stale remote would fail to fetch.
        """
        target = self._clone_url(repo_url)
        current = self._output(["git", "-C", str(path), "remote", "get-url", "origin"],
                               is_cancelled=is_cancelled)
        if current != target:
            self._run(["git", "-C", str(path), "remote", "set-url", "origin", target],
                      is_cancelled=is_cancelled)

    def _pin_ssh_command(self, path: Path, *, is_cancelled=None) -> None:
        """Record the key in the repo's own config.

        The environment covers commands this class runs; this covers git run
        against the checkout by anything else — a build, a tool, an agent.
        """
        command = ssh_command_for_key(self.credentials.ssh_key_path)
        if not command or self.credentials.uses_token:
            return
        self._run(["git", "-C", str(path), "config", "core.sshCommand", command],
                  is_cancelled=is_cancelled)

    # -- publishing --------------------------------------------------------

    def current_commit(self, path: Path) -> str:
        return self._output(["git", "-C", str(path), "rev-parse", "HEAD"])

    def execute(
        self,
        path: Path,
        *args: str,
        is_cancelled: Optional[Callable[[], bool]] = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run a Git command in an existing checkout.

        This is the agent-neutral escape hatch for product workflows that need
        a command not covered by the higher-level workspace operations.  It
        deliberately retains this class's credential, timeout, cancellation,
        and process-group handling instead of making consumers shell out.

        Output is always captured as text.  A non-zero exit is returned by
        default so callers can apply domain-specific retry or reporting policy;
        pass ``check=True`` when failure is exceptional.
        """
        return self._run(
            ["git", "-C", str(path), *args],
            is_cancelled=is_cancelled,
            check=check,
        )

    def output(
        self,
        path: Path,
        *args: str,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> str:
        """Return stdout from a read-only Git command in this workspace.

        Products choose the Git query while the workspace retains credential,
        timeout, and cancellation handling. A non-zero command returns any
        available stdout, matching the best-effort read semantics used by the
        existing query helpers.
        """
        return self._output(
            ["git", "-C", str(path), *args],
            is_cancelled=is_cancelled,
        )

    def query(
        self,
        path: Path,
        *args: str,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> str:
        """Return stdout from a read-only Git command, raising on failure.

        Use this for required repository evidence where an empty string and a
        failed command mean different things. ``output`` remains the
        best-effort variant for probes such as optional refs and config.
        """
        completed = self._run(
            ["git", "-C", str(path), *args],
            is_cancelled=is_cancelled,
        )
        return (completed.stdout or "").strip()

    def rev_parse(self, path: Path, ref: str, **kw) -> str:
        """Commit a ref resolves to, or "" if it does not resolve."""
        return self._output(["git", "-C", str(path), "rev-parse", "--verify", f"{ref}^{{commit}}"], **kw)

    def ref_exists(self, path: Path, ref: str, **kw) -> bool:
        return bool(self.rev_parse(path, ref, **kw))

    def remote_branch_head(self, path: Path, *, branch: str, **kw) -> str:
        """Tip of a remote branch, without disturbing the checkout.

        Used to decide whether a cached clone is stale before doing anything
        with it, so it must not itself move HEAD.
        """
        validate_branch(branch)
        self.refresh_ref(path, branch, **kw)
        return self.rev_parse(path, f"origin/{branch}", **kw)

    def refresh_ref(
        self,
        path: Path,
        branch: str,
        *,
        depth: Optional[int] = -1,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Update ``origin/<branch>`` from the remote.

        Returns False when the remote has no such branch, which is a normal
        outcome: a consumer refreshing a list of *candidate* base refs must not
        fail the task because one of them was never pushed. Any other failure
        still raises -- an unreachable remote is not a missing branch.
        """
        validate_branch(branch)
        # Fetch into the remote-tracking ref explicitly. Bare `git fetch origin
        # <branch>` updates FETCH_HEAD only, leaving origin/<branch> stale, so a
        # later checkout of origin/<branch> silently gets the previous commit.
        cmd = ["git", "-C", str(path), "fetch", "--force", "--no-tags", "--prune"]
        effective = self.clone_depth if depth == -1 else depth
        if effective is not None:
            cmd += ["--depth", str(effective)]
        cmd += ["origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]

        completed = self._run(cmd, is_cancelled=is_cancelled, check=False)
        if completed.returncode == 0:
            return True
        stderr = completed.stderr or ""
        if "couldn't find remote ref" in stderr or "not found in upstream" in stderr:
            logger.info("no remote branch %s in %s", branch, path.name)
            return False
        raise GitCommandError(cmd, completed.returncode, stderr)

    def has_changes(self, path: Path) -> bool:
        return bool(self._output(["git", "-C", str(path), "status", "--porcelain"]))

    def changed_paths(self, path: Path) -> List[str]:
        """Paths modified in the working tree, relative to the repo root."""
        out = self._output(["git", "-C", str(path), "status", "--porcelain"])
        return [line[3:].strip() for line in out.splitlines() if line.strip()]

    def commit_all_and_push(
        self,
        path: Path,
        *,
        branch: str,
        message: str,
        identity: BotIdentity,
        allow_path: Optional[Callable[[str], bool]] = None,
        rebase_before_push: bool = True,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> Optional[str]:
        """Commit everything and push. Returns the commit SHA, or None if nothing changed.

        The no-changes case is a normal outcome — an agent that found nothing to
        change is not a failure — so it returns rather than raising.

        ``allow_path`` is a policy guard: a path it rejects aborts the push
        rather than being committed. An agent asked to write tests that has
        edited production code should not be able to publish that, and finding
        out at review time is too late.

        ``rebase_before_push`` replays onto the current remote tip, because a
        branch that moved since the checkout would otherwise fail the push or,
        worse, need a force.
        """
        run = lambda cmd: self._run(cmd, is_cancelled=is_cancelled)  # noqa: E731

        # prepare() checks out origin/<branch>, which leaves HEAD detached. A
        # commit there is unreachable from any local branch, so `push origin
        # <branch>` pushes the *stale* local branch and reports
        # "Everything up-to-date" -- the work is committed and silently never
        # published, while this method returns True. Put HEAD on the branch
        # first.
        run(["git", "-C", str(path), "checkout", "-B", branch])
        run(["git", "-C", str(path), "config", "user.name", identity.name])
        run(["git", "-C", str(path), "config", "user.email", identity.email])
        self._sync_push_url(path)

        changed = self.changed_paths(path)
        if not changed:
            logger.info("nothing to commit in %s", path.name)
            return None

        if allow_path is not None:
            refused = [p for p in changed if not allow_path(p)]
            if refused:
                raise PushPolicyError(
                    f"refusing to publish disallowed paths: {', '.join(sorted(refused)[:20])}"
                )

        run(["git", "-C", str(path), "add", "-A"])
        run(["git", "-C", str(path), "commit", "-m", message])

        if rebase_before_push:
            fetched = self._run(["git", "-C", str(path), "fetch", "--prune", "origin", branch],
                                is_cancelled=is_cancelled, check=False)
            if fetched.returncode == 0:
                rebased = self._run(["git", "-C", str(path), "rebase", f"origin/{branch}"],
                                    is_cancelled=is_cancelled, check=False)
                if rebased.returncode != 0:
                    # Leave no half-rebased tree behind for the next task.
                    self._run(["git", "-C", str(path), "rebase", "--abort"],
                              is_cancelled=is_cancelled, check=False)
                    raise PushConflictError(
                        f"rebase onto origin/{branch} failed: {(rebased.stderr or '')[:400]}"
                    )

        local = self.current_commit(path)
        run(["git", "-C", str(path), "push", "-u", "origin", branch])

        # Verify rather than trust. A push can report success without moving the
        # remote -- committing on a detached HEAD produces exactly that, and the
        # work is then silently unpublished.
        remote = self._remote_head(path, branch, is_cancelled=is_cancelled)
        if remote and remote != local:
            raise PushConflictError(
                f"push did not land: local={local[:12]} remote={remote[:12]}"
            )
        return local

    def _remote_head(
        self, path: Path, branch: str, *, is_cancelled: Optional[Callable[[], bool]] = None
    ) -> str:
        """Where the remote's branch is, as a commit or "" if it cannot be read.

        Asked of the remote, not of the remote-tracking ref, because that ref
        lies in both directions on a single-branch clone. Its refspec is
        `+refs/heads/master:refs/remotes/origin/master` and nothing else, so
        for any other branch git neither fetches it nor updates it on push:

        - absent, and `git rev-parse` prints the name back and fails on stderr,
          which reads as a remote commit that does not match;
        - or present and stale, a real commit from an earlier run, which reads
          as a remote that moved.

        Both failed an approved run whose work was safely on the remote. One
        `ls-remote` is a round trip on a path that has just done a network
        push, and it is the thing the check is actually about.
        """
        listed = self._run(
            ["git", "-C", str(path), "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
            is_cancelled=is_cancelled,
            check=False,
        )
        if listed.returncode == 0:
            for line in (listed.stdout or "").splitlines():
                sha = line.split("\t")[0].strip()
                if _COMMIT.fullmatch(sha):
                    return sha
            # The remote answered and has no such branch: nothing to compare.
            return ""
        # The remote could not be reached. The tracking ref is all there is,
        # and only if it is a commit rather than an echoed argument.
        tracked = self._output(["git", "-C", str(path), "rev-parse", f"origin/{branch}"])
        return tracked if _COMMIT.fullmatch(tracked or "") else ""

    def _sync_push_url(self, path: Path) -> None:
        if not self.credentials.uses_token:
            return
        current = self._output(["git", "-C", str(path), "remote", "get-url", "origin"])
        rewritten = url_for_access_token(current) if current else ""
        if rewritten and rewritten != current:
            self._run(["git", "-C", str(path), "remote", "set-url", "origin", rewritten])
