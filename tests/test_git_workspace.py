"""Tests for cloned workspaces, against real local git repositories.

Real repos rather than mocks: the behaviour worth protecting is what git
actually does with a stale remote-tracking ref or a dirty checkout, and a mock
would only assert the commands we already believe we send.
"""

from __future__ import annotations

import os
import select
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_core.git import (
    BotIdentity,
    GitCancelled,
    GitCommandError,
    GitCredentials,
    GitTimeout,
    GitWorkspace,
)

BOT = BotIdentity(name="agent-bot", email="agent@local")


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


class Origin:
    """A bare remote plus the scratch clone used to seed and advance it."""

    def __init__(self, bare: Path, seed: Path):
        self.bare = bare
        self.seed = seed

    def __str__(self) -> str:
        return str(self.bare)

    def __fspath__(self) -> str:
        return str(self.bare)


def _advance(bare, content, message):
    """Add a commit to the bare remote via its seed clone."""
    seed = bare.seed
    (seed / "README.md").write_text(content)
    _git(seed, "commit", "-qam", message)
    _git(seed, "push", "-q", "origin", "main")


def _head(bare):
    return subprocess.run(["git", "-C", str(bare), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare remote, seeded through a scratch clone.

    Bare because that is what a service pushes to; a non-bare remote refuses
    updates to its checked-out branch and would test the wrong thing.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("v1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "first")
    _git(seed, "push", "-q", "-u", "origin", "main")
    return Origin(bare, seed)


@pytest.fixture
def ws(tmp_path):
    return GitWorkspace(tmp_path / "cache")


# -- preparing -------------------------------------------------------------------


def test_clone_then_checkout(origin, ws):
    path = ws.prepare(str(origin), branch="main")
    assert (path / "README.md").read_text() == "v1\n"
    assert path == ws.path_for(str(origin))


def test_second_prepare_refreshes_rather_than_recloning(origin, ws):
    ws.prepare(str(origin), branch="main")
    _advance(origin, "v2\n", "second")

    path = ws.prepare(str(origin), branch="main")
    assert (path / "README.md").read_text() == "v2\n", "must see the new commit"


def test_fetch_updates_the_remote_tracking_ref(origin, ws):
    """`git fetch origin <branch>` updates FETCH_HEAD only.

    If origin/<branch> is left stale, checking it out silently returns the
    previous commit -- a task reviewing code that is one push out of date.
    """
    ws.prepare(str(origin), branch="main")
    _advance(origin, "v2\n", "second")
    expected = _head(origin)

    path = ws.prepare(str(origin), branch="main")
    assert ws.current_commit(path) == expected


def test_checkout_of_a_specific_commit(origin, ws):
    first = _head(origin)
    _advance(origin, "v2\n", "second")

    path = ws.prepare(str(origin), branch="main", commit=first)
    assert (path / "README.md").read_text() == "v1\n"


def test_leftover_edits_are_cleaned_before_reuse(origin, ws):
    """An agent must start from a clean tree, not another task's leftovers."""
    path = ws.prepare(str(origin), branch="main")
    (path / "scratch.txt").write_text("junk")
    (path / "README.md").write_text("locally mangled")

    path = ws.prepare(str(origin), branch="main")
    assert not (path / "scratch.txt").exists()
    assert (path / "README.md").read_text() == "v1\n"


def test_concurrent_prepares_of_one_repo_are_serialised(origin, ws):
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: ws.prepare(str(origin), branch="main"), range(4)))
    assert len(set(paths)) == 1
    assert (paths[0] / "README.md").read_text() == "v1\n"


def test_failure_reports_the_subcommand_not_the_url(origin, ws):
    """The URL can carry a token, so it must not appear in an exception."""
    from agent_core.git import GitCommandError

    with pytest.raises(GitCommandError) as exc:
        ws.prepare(str(origin), branch="no-such-branch")
    assert "clone" in str(exc.value)


# -- cancellation ----------------------------------------------------------------


def test_cancellation_before_a_command(origin, ws):
    with pytest.raises(GitCancelled):
        ws.prepare(str(origin), branch="main", is_cancelled=lambda: True)


def test_cancellation_observed_after_a_long_command(origin, ws):
    """A fetch may outlive the decision to stop, so it is checked afterwards too."""
    calls = []

    def cancelled():
        calls.append(1)
        return len(calls) > 1

    with pytest.raises(GitCancelled):
        ws.prepare(str(origin), branch="main", is_cancelled=cancelled)


# -- publishing ------------------------------------------------------------------


def test_commit_and_push_reaches_the_origin(origin, ws):
    path = ws.prepare(str(origin), branch="main")
    (path / "generated.py").write_text("print('hi')\n")

    assert ws.commit_all_and_push(path, branch="main", message="add generated", identity=BOT)

    log = subprocess.run(["git", "-C", str(origin), "log", "--oneline", "-1", "main"],
                         capture_output=True, text=True).stdout
    assert "add generated" in log


def test_push_with_nothing_to_commit_returns_false(origin, ws):
    """An agent that changed nothing is a normal outcome, not a failure."""
    path = ws.prepare(str(origin), branch="main")
    assert ws.commit_all_and_push(path, branch="main", message="noop", identity=BOT) is None


def test_commit_uses_the_supplied_identity(origin, ws):
    path = ws.prepare(str(origin), branch="main")
    (path / "x.txt").write_text("x")
    ws.commit_all_and_push(path, branch="main", message="m",
                           identity=BotIdentity(name="dev-flow", email="df@local"))

    author = subprocess.run(["git", "-C", str(path), "log", "-1", "--format=%an <%ae>"],
                            capture_output=True, text=True).stdout.strip()
    assert author == "dev-flow <df@local>"


def test_has_changes(origin, ws):
    path = ws.prepare(str(origin), branch="main")
    assert ws.has_changes(path) is False
    (path / "new.txt").write_text("x")
    assert ws.has_changes(path) is True


def test_output_runs_a_read_command_inside_the_workspace(origin, ws):
    path = ws.prepare(str(origin), branch="main")

    assert ws.output(path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_query_raises_when_a_read_command_fails(origin, ws):
    path = ws.prepare(str(origin), branch="main")

    with pytest.raises(GitCommandError):
        ws.query(path, "rev-parse", "refs/heads/does-not-exist")


def test_execute_returns_command_result_for_product_specific_policy(origin, ws):
    path = ws.prepare(str(origin), branch="main")

    completed = ws.execute(path, "rev-parse", "--verify", "refs/heads/does-not-exist")

    assert completed.returncode != 0
    assert isinstance(completed.stderr, str)


def test_execute_can_raise_for_required_command(origin, ws):
    path = ws.prepare(str(origin), branch="main")

    with pytest.raises(GitCommandError):
        ws.execute(path, "rev-parse", "refs/heads/does-not-exist", check=True)


def test_prepare_can_clone_full_history(origin, tmp_path):
    _advance(origin, "v2\n", "second")
    _advance(origin, "v3\n", "third")
    ws = GitWorkspace(tmp_path / "cache", clone_depth=None)

    path = ws.prepare(str(origin), branch="main")

    assert ws.query(path, "rev-list", "--count", "HEAD") == "3"


# -- credentials -----------------------------------------------------------------


def test_clone_url_is_rewritten_only_when_a_token_is_used(tmp_path):
    plain = GitWorkspace(tmp_path / "a", GitCredentials(ssh_key_path="/k"))
    assert plain._clone_url("git@h:g/r.git") == "git@h:g/r.git"

    tokened = GitWorkspace(tmp_path / "b",
                           GitCredentials(access_token="t", token_host="h"))
    assert tokened._clone_url("git@h:g/r.git") == "https://h/g/r.git"


def test_repos_are_isolated_by_name(tmp_path, origin):
    ws = GitWorkspace(tmp_path / "cache")
    assert ws.path_for("git@h:group/alpha.git").name == "alpha"
    assert ws.path_for("git@h:other/beta.git").name == "beta"


def test_push_works_from_the_detached_head_prepare_leaves(origin, ws):
    """prepare() checks out origin/<branch>, detaching HEAD.

    Committing there and pushing <branch> pushes the stale local branch and
    reports success -- the generated work is committed and never published.
    """
    path = ws.prepare(str(origin), branch="main")
    head_state = subprocess.run(["git", "-C", str(path), "symbolic-ref", "-q", "HEAD"],
                                capture_output=True, text=True)
    assert head_state.returncode != 0, "precondition: prepare leaves HEAD detached"

    (path / "generated.py").write_text("x\n")
    assert ws.commit_all_and_push(path, branch="main", message="from detached", identity=BOT)

    log = subprocess.run(["git", "-C", str(origin), "log", "--oneline", "-1", "main"],
                         capture_output=True, text=True).stdout
    assert "from detached" in log, "the commit must actually reach the remote"


# -- publish safety --------------------------------------------------------------
#
# Modelled on the live push implementation in another consumer, which is
# markedly more careful than the deprecated one: it refuses disallowed paths,
# rebases onto the current tip, and verifies the remote actually moved.


def test_push_returns_the_commit_sha(origin, ws):
    path = ws.prepare(str(origin), branch="main")
    (path / "a.txt").write_text("x")
    sha = ws.commit_all_and_push(path, branch="main", message="m", identity=BOT)
    assert sha and len(sha) == 40


def test_disallowed_paths_abort_the_push(origin, ws):
    """An agent asked to write tests must not be able to publish production edits.

    Finding that out at review time is too late.
    """
    from agent_core.git import PushPolicyError

    path = ws.prepare(str(origin), branch="main")
    (path / "src_prod.py").write_text("danger")
    with pytest.raises(PushPolicyError, match="src_prod.py"):
        ws.commit_all_and_push(path, branch="main", message="m", identity=BOT,
                               allow_path=lambda p: p.startswith("tests/"))


def test_allowed_paths_pass_the_guard(origin, ws):
    path = ws.prepare(str(origin), branch="main")
    (path / "tests").mkdir()
    (path / "tests" / "test_x.py").write_text("def test_x(): pass\n")
    assert ws.commit_all_and_push(path, branch="main", message="m", identity=BOT,
                                  allow_path=lambda p: p.startswith("tests/"))


def test_push_rebases_onto_a_moved_branch(origin, ws):
    """The remote can move between checkout and push."""
    path = ws.prepare(str(origin), branch="main")
    _advance(origin, "moved\n", "remote moved on")

    (path / "mine.txt").write_text("mine\n")
    assert ws.commit_all_and_push(path, branch="main", message="mine", identity=BOT)

    log = subprocess.run(["git", "-C", str(origin), "log", "--oneline", "-3", "main"],
                         capture_output=True, text=True).stdout
    assert "mine" in log and "remote moved on" in log, "both commits must survive"


def test_conflicting_rebase_aborts_and_raises(origin, ws):
    """A half-rebased tree must not be left for the next task."""
    from agent_core.git import PushConflictError

    path = ws.prepare(str(origin), branch="main")
    _advance(origin, "theirs\n", "theirs")
    (path / "README.md").write_text("mine\n")  # same file, diverged

    with pytest.raises(PushConflictError, match="rebase"):
        ws.commit_all_and_push(path, branch="main", message="mine", identity=BOT)

    in_progress = Path(path / ".git" / "rebase-merge").exists() or \
        Path(path / ".git" / "rebase-apply").exists()
    assert not in_progress, "the rebase must be aborted, not left half-applied"


# -- read path: interruptible git ------------------------------------------------


def test_cancelling_interrupts_a_running_git_command(origin, tmp_path):
    """Cancel must stop a command that is *already running*, not wait it out.

    A consumer checking `is_cancelled` only before and after each command leaves
    an operator unable to stop a ten-minute clone -- the reason a fork of this
    code polls and kills the process group instead.
    """
    slow_bin = tmp_path / "bin"
    slow_bin.mkdir()
    fake = slow_bin / "git"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)

    ws = GitWorkspace(tmp_path / "cache", git_bin=str(fake), poll_interval=0.05)

    started = time.monotonic()
    with pytest.raises(GitCancelled):
        ws.prepare(str(origin), branch="main", is_cancelled=lambda: time.monotonic() - started > 0.3)
    assert time.monotonic() - started < 10, "cancel must not wait for the command to finish"


def test_a_timed_out_git_command_leaves_no_child_behind(tmp_path, origin):
    """The whole process group is killed, not just the direct child.

    git delegates to ssh; killing only git orphans the ssh it spawned.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "child-alive"
    fake = bin_dir / "git"
    # A stand-in for `git` that spawns a child of its own, as git does with ssh.
    fake.write_text(
        "#!/bin/sh\n"
        f"( sleep 30; echo alive > {marker} ) &\n"
        "sleep 30\n"
    )
    fake.chmod(0o755)

    ws = GitWorkspace(tmp_path / "cache", git_bin=str(fake), timeout=1, poll_interval=0.05)
    with pytest.raises(GitTimeout):
        ws.prepare(str(origin), branch="main")

    time.sleep(0.4)
    assert not marker.exists(), "the grandchild survived the kill"


# -- read path: refusing unsafe input --------------------------------------------


@pytest.mark.parametrize(
    "branch",
    [
        "--upload-pack=touch /tmp/pwned",  # an option, not a branch
        "../../etc/passwd",
        "main;rm -rf /",
        "a" * 256,
        "",
        "refs/heads/x@{1}",
        "feature/..//x",
    ],
)
def test_an_unsafe_branch_is_refused(ws, origin, branch):
    """Refused before reaching git, so a name that is really a flag cannot run."""
    with pytest.raises(ValueError):
        ws.prepare(str(origin), branch=branch)


def test_a_repo_url_outside_the_allowed_hosts_is_refused(tmp_path, origin):
    ws = GitWorkspace(tmp_path / "cache", allowed_hosts=["git.example.com"])
    with pytest.raises(ValueError, match="host"):
        ws.prepare("git@evil.example:x/y.git", branch="main")


def test_allowed_hosts_unset_permits_any_host(ws, origin):
    """Not every deployment has a host allowlist; absence must not mean deny."""
    assert ws.prepare(str(origin), branch="main").exists()


def test_a_repo_name_cannot_escape_the_cache_directory(tmp_path):
    ws = GitWorkspace(tmp_path / "cache")

    # Taking the last path segment already neutralises the obvious traversal...
    assert ws.path_for("git@h:x/../../../../etc/passwd").parent == ws.cache_dir
    # ...but a url *ending* in a traversal segment makes the name itself "..",
    # which would place the checkout above the cache directory.
    for escaping in ("git@h:x/..", "git@h:x/.", "ssh://h/-flag"):
        with pytest.raises(ValueError):
            ws.path_for(escaping)


# -- read path: inspecting refs ---------------------------------------------------


def test_ref_exists_distinguishes_present_from_absent(origin, ws):
    path = ws.prepare(str(origin), branch="main")
    assert ws.ref_exists(path, "origin/main")
    assert not ws.ref_exists(path, "origin/nope")


def test_remote_branch_head_reports_the_tip_without_checking_it_out(origin, ws):
    path = ws.prepare(str(origin), branch="main")
    before = ws.current_commit(path)
    _advance(origin, "moved\n", "moved")

    head = ws.remote_branch_head(path, branch="main")

    assert head == _head(origin)
    assert ws.current_commit(path) == before, "inspecting must not move the checkout"


def test_refresh_ref_updates_the_remote_tracking_ref(origin, ws):
    path = ws.prepare(str(origin), branch="main")
    _advance(origin, "moved\n", "moved")

    ws.refresh_ref(path, "main")

    assert ws.rev_parse(path, "origin/main") == _head(origin)


def test_refreshing_a_ref_the_remote_does_not_have_is_not_an_error(origin, ws):
    """A base branch that does not exist upstream is a normal case.

    A consumer refreshing a list of candidate base refs must not fail the task
    because one of them was never pushed.
    """
    path = ws.prepare(str(origin), branch="main")
    assert ws.refresh_ref(path, "no-such-branch") is False
    assert ws.refresh_ref(path, "main") is True


def test_a_failing_refresh_still_raises(origin, ws, monkeypatch):
    """Only a missing ref is tolerated -- a broken remote must still surface."""
    path = ws.prepare(str(origin), branch="main")
    _git(path, "remote", "set-url", "origin", str(tmp_path_unreachable()))
    with pytest.raises(GitCommandError):
        ws.refresh_ref(path, "main")


def tmp_path_unreachable() -> Path:
    return Path("/nonexistent-remote-path.git")


def test_a_command_with_output_larger_than_a_pipe_buffer_completes(tmp_path, origin):
    """Staying cancellable must not mean deadlocking on a chatty command.

    A loop that only polled the exit status would hang here: the child blocks
    writing once the pipe buffer fills, and never exits.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text("#!/bin/sh\nawk 'BEGIN{for(i=0;i<200000;i++) print \"noisy output line\"}'\n")
    fake.chmod(0o755)

    ws = GitWorkspace(tmp_path / "cache", git_bin=str(fake), timeout=20, poll_interval=0.05)
    out = ws._output(["git", "whatever"])

    assert out.count("noisy output line") == 200000


# -- per-task isolation ----------------------------------------------------------
#
# The shared cache is right for a service that keeps re-running against one
# repository: cloning once and refreshing is much cheaper. It is wrong for CI,
# where two tasks on the same repository must not see each other's tree --
# one task's half-applied patch or leftover build output becomes another's
# baseline, and the failure surfaces as an inexplicable diff in an unrelated
# task. `scope` gives that finer granularity here, so a consumer does not have
# to keep its own workspace manager to get it.


def test_two_scopes_of_one_repository_are_separate_checkouts(origin, ws):
    """The isolation guarantee: same repo, different tasks, different trees."""
    first = ws.prepare(str(origin), branch="main", scope="task-1")
    second = ws.prepare(str(origin), branch="main", scope="task-2")

    assert first != second
    assert first.exists() and second.exists()
    assert (first / "README.md").read_text() == (second / "README.md").read_text()


def test_work_in_one_scope_is_invisible_to_another(origin, ws):
    first = ws.prepare(str(origin), branch="main", scope="task-1")
    (first / "scratch.txt").write_text("half-finished\n")

    second = ws.prepare(str(origin), branch="main", scope="task-2")

    assert not (second / "scratch.txt").exists()


def test_a_scope_is_reusable_within_itself(origin, ws):
    """Re-preparing the same scope refreshes it rather than making a new one,
    so a retry of the same task does not re-clone."""
    first = ws.prepare(str(origin), branch="main", scope="task-1")
    again = ws.prepare(str(origin), branch="main", scope="task-1")

    assert first == again


def test_scopes_are_locked_independently(origin, ws):
    """Two tasks on one repository must not serialise behind each other."""
    with ws.repo_lock(str(origin), scope="task-1"):
        # Acquiring a different scope's lock here must not deadlock.
        with ws.repo_lock(str(origin), scope="task-2"):
            pass


def test_repo_lock_reenters_in_the_same_process(origin, ws):
    """prepare already holds the lock; nested callers must not self-deadlock."""
    with ws.repo_lock(str(origin)):
        with ws.repo_lock(str(origin)):
            pass


HOLD_REPO_LOCK = """
import sys
from agent_core.git import GitWorkspace

ws = GitWorkspace(sys.argv[1])
scope = sys.argv[3] or None
with ws.repo_lock(sys.argv[2], scope=scope):
    print("held", flush=True)
    sys.stdin.read()
"""


def test_repo_lock_blocks_another_process_until_released(origin, tmp_path):
    """fcntl.flock, not just an in-process RLock: daemon and UI are two PIDs."""
    cache = str(tmp_path / "cache")
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_REPO_LOCK, cache, str(origin), ""],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"

    waiter = subprocess.Popen(
        [sys.executable, "-c", HOLD_REPO_LOCK, cache, str(origin), ""],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.4)
    assert waiter.poll() is None
    ready, _, _ = select.select([waiter.stdout], [], [], 0)
    assert ready == [], "the second process acquired the lock while the first still holds it"

    holder.stdin.close()
    holder.wait(timeout=5)
    assert waiter.stdout.readline().strip() == "held"
    waiter.stdin.close()
    waiter.wait(timeout=5)


def test_repo_lock_file_is_owner_only(origin, ws):
    with ws.repo_lock(str(origin)):
        locks = list((ws.cache_dir / ".locks").iterdir())
    assert locks
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in locks)


def test_repo_lock_closes_the_fd_if_flock_is_interrupted(origin, ws, monkeypatch):
    """A leaked LOCK_EX fd would make the next acquire wait on ourselves."""
    import agent_core.git.workspace as workspace

    def boom(handle, exclusive):
        raise KeyboardInterrupt

    monkeypatch.setattr(workspace, "_flock", boom)
    with pytest.raises(KeyboardInterrupt):
        with ws.repo_lock(str(origin)):
            pass
    monkeypatch.undo()
    with ws.repo_lock(str(origin)):
        pass


def test_repo_lock_refuses_a_symlinked_locks_directory(origin, tmp_path):
    from agent_core.paths import UnsafePathError

    cache = tmp_path / "cache"
    cache.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (cache / ".locks").symlink_to(tmp_path / "elsewhere")
    ws = GitWorkspace(cache)
    with pytest.raises(UnsafePathError, match="symlink"):
        with ws.repo_lock(str(origin)):
            pass


def test_repo_lock_refuses_windows(monkeypatch):
    """Do not patch os.name — pathlib reads it. Patch the platform guard."""
    import agent_core.git.workspace as workspace

    monkeypatch.setattr(workspace, "_is_windows", lambda: True)
    with pytest.raises(RuntimeError, match="POSIX"):
        workspace._require_posix_flock()


def test_an_unscoped_prepare_is_unchanged(origin, ws):
    """The shared-cache default must keep working exactly as before."""
    path = ws.prepare(str(origin), branch="main")

    assert path == ws.path_for(str(origin))
    assert path.parent == ws.cache_dir


@pytest.mark.parametrize("hostile", ["..", "../escape", "a/b", "a\\b", "-rf", ""])
def test_a_scope_cannot_escape_the_cache_directory(ws, hostile):
    """A scope is often a task id from a request body. It becomes a directory
    name, so it must not be able to point outside the cache."""
    with pytest.raises(ValueError):
        ws.path_for("https://example.invalid/org/repo.git", scope=hostile)


# -- capabilities a CI consumer needs from prepare --------------------------------
#
# Three properties unit-test-agent's own workspace manager had and this class
# did not. They are here rather than in the consumer because they are what any
# product that *pushes from the checkout* or *retries a flaky clone* needs.


def test_prepare_can_leave_a_local_branch_rather_than_a_detached_head(origin, ws):
    """`git push origin <branch>` needs a local branch of that name.

    `commit_all_and_push` copes with a detached HEAD by pushing an explicit
    refspec, but a consumer running its own push -- unit-test-agent's delivery
    does, so it can rebase and retry on rejection -- gets `src refspec <branch>
    does not match any` and strands work that was already committed.
    """
    path = ws.prepare(str(origin), branch="main", local_branch=True)

    head = subprocess.run(["git", "-C", str(path), "symbolic-ref", "--short", "-q", "HEAD"],
                          capture_output=True, text=True)
    assert head.stdout.strip() == "main"

    (path / "generated.py").write_text("x\n")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "from a local branch")
    _git(path, "push", "-q", "-u", "origin", "main")

    log = subprocess.run(["git", "-C", str(origin), "log", "--oneline", "-1", "main"],
                         capture_output=True, text=True).stdout
    assert "from a local branch" in log


def test_the_local_branch_is_reset_to_the_remote_tip_on_reuse(origin, ws):
    """A reused scope must not keep the previous run's local commits.

    Without `-B` the checkout would keep whatever the last task committed, and
    the next task would generate against, and push, that stale history.
    """
    path = ws.prepare(str(origin), branch="main", scope="task-1", local_branch=True)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "leftover.py").write_text("x\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "not pushed")
    _advance(origin, "v2\n", "remote moved on")

    again = ws.prepare(str(origin), branch="main", scope="task-1", local_branch=True)

    assert again == path
    assert ws.rev_parse(path, "HEAD") == _head(origin)
    assert not (path / "leftover.py").exists()


def test_a_fresh_prepare_discards_files_git_would_keep(origin, ws):
    """`clean -fd` leaves ignored files: build output, caches, virtualenvs.

    Reuse is normally what a scope wants -- it is why the clone is cached at
    all -- but a CI task that compiles must not inherit the last task's
    `target/` classes, so it can ask for the tree to be rebuilt instead.
    """
    path = ws.prepare(str(origin), branch="main", scope="task-1")
    (path / ".gitignore").write_text("target/\n")
    (path / "target").mkdir()
    (path / "target" / "Stale.class").write_text("stale\n")

    reused = ws.prepare(str(origin), branch="main", scope="task-1")
    assert (reused / "target" / "Stale.class").exists(), "precondition: reuse keeps ignored files"

    rebuilt = ws.prepare(str(origin), branch="main", scope="task-1", fresh=True)

    assert rebuilt == path
    assert not (rebuilt / "target").exists()
    assert (rebuilt / "README.md").exists(), "the checkout is rebuilt, not merely emptied"


def _flaky_git(bin_dir: Path, counter: Path, *, failures: int, stderr: str, code: int = 128) -> Path:
    """A `git` whose *clone* fails a given number of times, then really clones.

    Scoped to one subcommand so the counter is the number of clone attempts.
    Counting every invocation would fold in the checkout and clean that follow
    a successful clone, and the assertion would stop meaning anything.
    """
    fake = bin_dir / "git"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" != "clone" ]; then exec /usr/bin/git "$@"; fi\n'
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        f"echo $((n + 1)) > {counter}\n"
        f"if [ \"$n\" -lt {failures} ]; then\n"
        f"  echo '{stderr}' >&2\n"
        f"  exit {code}\n"
        "fi\n"
        'exec /usr/bin/git "$@"\n'
    )
    fake.chmod(0o755)
    return fake


def test_a_transient_clone_failure_is_retried(origin, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "n"
    fake = _flaky_git(bin_dir, counter, failures=2, stderr="fatal: Connection reset by peer")

    ws = GitWorkspace(tmp_path / "cache", git_bin=str(fake), poll_interval=0.05,
                      retry_times=2, retry_delay_seconds=0.0)

    path = ws.prepare(str(origin), branch="main")

    assert (path / "README.md").exists()
    assert counter.read_text().strip() == "3", "it must have taken all three clone attempts"


def test_a_failure_that_is_not_transient_is_not_retried(origin, tmp_path):
    """Retrying a repository that does not exist just makes the error slower
    to arrive, and makes it look like a network problem."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "n"
    fake = _flaky_git(bin_dir, counter, failures=99, stderr="fatal: repository not found")

    ws = GitWorkspace(tmp_path / "cache", git_bin=str(fake), poll_interval=0.05,
                      retry_times=2, retry_delay_seconds=0.0)

    with pytest.raises(GitCommandError):
        ws.prepare(str(origin), branch="main")

    assert counter.read_text().strip() == "1", "a missing repository must fail on the first attempt"


def test_retry_is_off_unless_a_consumer_asks_for_it(origin, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "n"
    fake = _flaky_git(bin_dir, counter, failures=99, stderr="fatal: Connection reset by peer")

    ws = GitWorkspace(tmp_path / "cache", git_bin=str(fake), poll_interval=0.05)

    with pytest.raises(GitCommandError):
        ws.prepare(str(origin), branch="main")

    assert counter.read_text().strip() == "1"


# -- the remote's own default branch ----------------------------------------------


def test_the_default_branch_is_read_from_the_remote(origin, ws):
    """A consumer that clones without naming a branch has to get it somewhere.

    Guessing "main" or "master" is wrong for exactly the repositories where it
    matters, and cloning first to look is a wasted clone when the answer is a
    single `ls-remote`.
    """
    assert ws.default_branch(str(origin)) == "main"


def test_prepare_without_a_branch_uses_the_remote_default(origin, ws):
    path = ws.prepare(str(origin))

    assert (path / "README.md").exists()
    assert ws.rev_parse(path, "HEAD") == _head(origin)


def test_prepare_without_a_branch_can_still_leave_a_local_branch(origin, ws):
    path = ws.prepare(str(origin), local_branch=True)

    head = subprocess.run(["git", "-C", str(path), "symbolic-ref", "--short", "-q", "HEAD"],
                          capture_output=True, text=True)
    assert head.stdout.strip() == "main"


def test_the_default_branch_of_a_repository_that_is_not_there_is_an_error(tmp_path, ws):
    with pytest.raises(GitCommandError):
        ws.default_branch(str(tmp_path / "nope.git"))


def test_a_disallowed_host_is_refused_before_the_remote_is_asked(tmp_path):
    ws = GitWorkspace(tmp_path / "cache", allowed_hosts=["git.example.com"])

    with pytest.raises(ValueError, match="not allowed"):
        ws.default_branch("git@evil.example.com:g/r.git")


def test_an_empty_branch_is_refused_rather_than_read_as_the_default(origin, ws):
    """Absent and empty are different: absent is "whatever the remote says",
    empty is a caller that thought it had a branch and did not."""
    with pytest.raises(ValueError):
        ws.prepare(str(origin), branch="")


def test_a_missing_branch_can_be_started_from_the_default(origin, ws):
    """A dev workflow owns branch creation. A run told to work on a branch
    that does not exist yet should start it, not stop with
    `Remote branch ... not found`."""
    path = ws.prepare(
        str(origin), branch="ticket-100-bugfix", create_missing=True, local_branch=True
    )

    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert head == "ticket-100-bugfix"
    # It starts where trunk is.
    here = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert here == _head(origin)

    # Without the flag a branch that is not there is still an error: for a
    # reviewer it means the request was wrong, not that one should be invented.
    with pytest.raises(GitCommandError):
        ws.prepare(str(origin), branch="never-existed", scope="other")


def test_a_push_to_a_branch_this_run_created_is_verified_against_the_remote(origin, ws):
    """A single-branch clone fetches `+refs/heads/master:...origin/master` and
    nothing else, so a branch the run created has no `origin/<branch>` however
    well the push went. `git rev-parse` then prints the argument back and fails
    on stderr -- which read as a remote commit that did not match, and failed
    an approved run whose work was safely pushed."""
    path = ws.prepare(
        str(origin), branch="ticket-100-bugfix", create_missing=True, local_branch=True
    )
    (path / "note.md").write_text("triage\n")

    sha = ws.commit_all_and_push(
        path,
        branch="ticket-100-bugfix",
        message="docs: triage",
        identity=BotIdentity(name="t", email="t@t"),
    )
    assert sha

    # The remote really has it -- which is what the verification is about.
    listed = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "ticket-100-bugfix"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert listed == sha


def test_a_stale_tracking_ref_does_not_fail_a_push_that_landed(origin, ws):
    """The nastier half of the same bug. A single-branch clone never updates
    `origin/<branch>` on push either, so on the second run the ref holds a real
    commit from the first -- valid-looking, and wrong. Checking against it
    failed an approved run whose work was on the remote."""
    path = ws.prepare(
        str(origin), branch="ticket-100-bugfix", create_missing=True, local_branch=True
    )
    (path / "one.md").write_text("first\n")
    first = ws.commit_all_and_push(
        path, branch="ticket-100-bugfix", message="first", identity=BOT
    )

    # Narrow the refspec to the clone's own branch, which is what a
    # single-branch clone has, and pin the tracking ref where such a clone
    # leaves it: at the first push, never updated again.
    subprocess.run(
        ["git", "-C", str(path), "config", "--replace-all", "remote.origin.fetch",
         "+refs/heads/master:refs/remotes/origin/master"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "update-ref", "refs/remotes/origin/ticket-100-bugfix", first],
        check=True, capture_output=True,
    )

    (path / "two.md").write_text("second\n")
    second = ws.commit_all_and_push(
        path, branch="ticket-100-bugfix", message="second", identity=BOT
    )
    assert second != first

    on_remote = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "ticket-100-bugfix"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert on_remote == second
