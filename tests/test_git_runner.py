"""Running git against a checkout the caller already owns.

`GitWorkspace` is right when it owns the clone. Consumers frequently do not
want that -- they have a checkout made by a CI runner or a task workspace --
and reach for a bare `subprocess.run`, losing a timeout, credentials,
cancellation and typed failures every time.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_core.git.runner import GitRunner, runner_for
from agent_core.git.workspace import GitCancelled, GitCommandError, GitCredentials, GitTimeout


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (tmp_path / "a.txt").write_text("one\n")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return tmp_path


# -- reads -----------------------------------------------------------------

def test_it_runs_a_command(repo):
    assert GitRunner().run(repo, "status", "--porcelain").returncode == 0


def test_output_is_stripped(repo):
    head = GitRunner().output(repo, "rev-parse", "HEAD")

    assert len(head) == 40
    assert head == head.strip()


def test_succeeds_answers_the_question_without_raising(repo):
    runner = GitRunner()

    assert runner.succeeds(repo, "rev-parse", "--verify", "HEAD")
    assert not runner.succeeds(repo, "rev-parse", "--verify", "no-such-ref")


# -- writes ----------------------------------------------------------------

def test_it_writes(repo):
    runner = GitRunner()
    (repo / "b.txt").write_text("two\n")

    runner.run(repo, "add", "-A")
    runner.run(repo, "commit", "-q", "-m", "second")

    assert runner.output(repo, "log", "--oneline").count("\n") == 1


# -- failures --------------------------------------------------------------

def test_a_failing_command_raises_a_typed_error(repo):
    with pytest.raises(GitCommandError):
        GitRunner().run(repo, "rev-parse", "--verify", "no-such-ref")


def test_check_false_hands_back_the_result(repo):
    completed = GitRunner().run(repo, "rev-parse", "--verify", "nope", check=False)

    assert completed.returncode != 0


def test_a_typed_error_is_still_a_runtime_error(repo):
    """So existing `except RuntimeError` handlers keep working."""
    with pytest.raises(RuntimeError):
        GitRunner().run(repo, "rev-parse", "--verify", "no-such-ref")


# -- bounds ----------------------------------------------------------------

def test_a_timeout_is_applied(repo, monkeypatch):
    seen = {}
    real = subprocess.run

    def record(cmd, **kwargs):
        seen.update(kwargs)
        return real(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    GitRunner(timeout=42).run(repo, "status")

    assert seen["timeout"] == 42


def test_a_hang_raises_even_when_check_is_false(repo, monkeypatch):
    """A command that never returned has no exit code to inspect, and calling
    that an ordinary failure loses the distinction that decides a retry."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, kw.get("timeout"))),
    )

    with pytest.raises(GitTimeout):
        GitRunner().run(repo, "fetch", "origin", check=False)


def test_none_means_no_limit(repo, monkeypatch):
    seen = {}
    real = subprocess.run
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (seen.update(kw), real(cmd, **kw))[1],
    )

    GitRunner(timeout=None).run(repo, "status")

    assert seen["timeout"] is None


def test_a_per_call_timeout_overrides_the_runners(repo, monkeypatch):
    seen = {}
    real = subprocess.run
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: (seen.update(kw), real(cmd, **kw))[1]
    )

    GitRunner(timeout=600).run(repo, "status", timeout=5)

    assert seen["timeout"] == 5


# -- cancellation ----------------------------------------------------------

def test_a_cancelled_run_does_not_start(repo, monkeypatch):
    started = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: started.append(1))

    with pytest.raises(GitCancelled):
        GitRunner().run(repo, "fetch", "origin", is_cancelled=lambda: True)

    assert started == []


def test_a_predicate_that_says_no_lets_it_run(repo):
    assert GitRunner().run(repo, "status", is_cancelled=lambda: False).returncode == 0


# -- credentials -----------------------------------------------------------

def test_credentials_reach_the_environment(repo, monkeypatch):
    seen = {}
    real = subprocess.run
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: (seen.update(kw), real(cmd, **kw))[1]
    )

    runner_for(access_token="tok", token_host="git.example.com").run(repo, "status")

    env = seen["env"]
    assert env["GIT_CONFIG_KEY_0"] == "http.https://git.example.com/.extraheader"


def test_no_credentials_still_runs(repo):
    """A local repository needs none, and requiring them would be absurd."""
    assert GitRunner(credentials=GitCredentials()).run(repo, "status").returncode == 0


def test_a_caller_may_supply_its_own_environment(repo, monkeypatch):
    seen = {}
    real = subprocess.run
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: (seen.update(kw), real(cmd, **kw))[1]
    )

    GitRunner().run(repo, "status", env={"PATH": "/usr/bin", "HOME": "/tmp"})

    assert seen["env"] == {"PATH": "/usr/bin", "HOME": "/tmp"}


# -- a repository that is not there ---------------------------------------------
#
# Consumers migrating off `subprocess.run(["git", "-C", path, ...])` hit this
# immediately. `git -C` lets git report the problem: exit 128, a message on
# stderr, no exception. Running with `cwd=` instead fails at the OS layer with
# FileNotFoundError before git starts, so a caller that reads `returncode`
# -- which is most of them, and every one of UTA's CI readers -- crashes where
# it used to see a failed command. That difference blocked the migration and
# broke ten CI tests, so the runner absorbs it here rather than in each caller.


def test_a_missing_repository_is_a_failed_command_not_an_exception(tmp_path):
    runner = GitRunner()

    completed = runner.run(tmp_path / "no-such-repo", "status", check=False)

    assert completed.returncode != 0
    assert "no-such-repo" in (completed.stderr or "")


def test_a_missing_repository_still_raises_when_check_is_set(tmp_path):
    """`check=True` means "raise on failure", and this is a failure."""
    runner = GitRunner()

    with pytest.raises(GitCommandError):
        runner.run(tmp_path / "no-such-repo", "status")


def test_a_missing_repository_reports_no_stdout(tmp_path):
    runner = GitRunner()

    completed = runner.run(tmp_path / "gone", "rev-parse", "HEAD", check=False)

    assert (completed.stdout or "") == ""


def test_output_of_a_missing_repository_is_empty(tmp_path):
    """`output()` is used for reads whose callers treat blank as 'unknown'."""
    runner = GitRunner()

    assert runner.output(tmp_path / "gone", "rev-parse", "HEAD", check=False) == ""


def test_a_real_repository_is_unaffected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    runner = GitRunner()

    completed = runner.run(repo, "status", "--porcelain", check=False)

    assert completed.returncode == 0
