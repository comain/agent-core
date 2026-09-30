"""Checking what an agent actually changed in a checkout.

The harness could already stop an agent writing at all -- `opencode_permissions
= {"edit": "deny"}` -- which is right when it should only read and is the only
control there was. It cannot express "may write tests, may not rewrite the
source they test", because that distinction is not about which tool ran, it is
about which paths came out different.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_core.git.guard import (
    DIRECTORY,
    MISSING,
    changed_paths,
    changed_since,
    snapshot,
    verify,
)


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("original\n")
    (tmp_path / "tests" / "test_app.py").write_text("original\n")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return tmp_path


def allow_tests_only(path: str) -> bool:
    return path.startswith("tests/")


# -- reading the tree ------------------------------------------------------

def test_a_clean_tree_reports_nothing(repo):
    assert changed_paths(repo) == set()


def test_it_sees_a_modification(repo):
    (repo / "src" / "app.py").write_text("changed\n")
    assert changed_paths(repo) == {"src/app.py"}


def test_it_sees_an_untracked_file(repo):
    (repo / "tests" / "test_new.py").write_text("new\n")
    assert "tests/test_new.py" in changed_paths(repo)


def test_it_sees_a_deletion(repo):
    (repo / "src" / "app.py").unlink()
    assert "src/app.py" in changed_paths(repo)


def test_a_path_with_a_space_is_not_quoted(repo):
    """The reason for `-z`: the human format quotes and escapes these."""
    (repo / "tests" / "test with space.py").write_text("x\n")
    assert "tests/test with space.py" in changed_paths(repo)


def test_a_path_with_a_quote_is_not_escaped(repo):
    (repo / "tests" / 'weird"name.py').write_text("x\n")
    assert 'tests/weird"name.py' in changed_paths(repo)


def test_a_rename_reports_the_new_name(repo):
    """The old path is gone; asking whether it was allowed to change is moot."""
    subprocess.run(["git", "mv", "src/app.py", "src/renamed.py"], cwd=repo, check=True, capture_output=True)
    paths = changed_paths(repo)
    assert "src/renamed.py" in paths


def test_a_directory_that_is_not_a_repository_reports_nothing(tmp_path):
    """A guard that cannot read the tree must neither lie nor crash the run."""
    assert changed_paths(tmp_path) == set()


# -- snapshots -------------------------------------------------------------

def test_a_snapshot_digests_dirty_files(repo):
    (repo / "src" / "app.py").write_text("changed\n")
    taken = snapshot(repo)
    assert taken["src/app.py"] not in (MISSING, DIRECTORY)
    assert len(taken["src/app.py"]) == 64


def test_a_deleted_path_is_recorded_as_missing(repo):
    (repo / "src" / "app.py").unlink()
    assert snapshot(repo)["src/app.py"] == MISSING


def test_nothing_dirty_means_an_empty_snapshot(repo):
    assert snapshot(repo) == {}


# -- the comparison --------------------------------------------------------

def test_no_change_between_snapshots(repo):
    before = snapshot(repo)
    assert changed_since(repo, before) == []


def test_a_new_change_is_detected(repo):
    before = snapshot(repo)
    (repo / "src" / "app.py").write_text("changed\n")
    assert changed_since(repo, before) == ["src/app.py"]


def test_a_file_already_dirty_and_then_rewritten_is_detected(repo):
    """The case a path-set diff misses: it is in both listings either way."""
    (repo / "src" / "app.py").write_text("dirty before\n")
    before = snapshot(repo)

    (repo / "src" / "app.py").write_text("rewritten by the agent\n")

    assert changed_since(repo, before) == ["src/app.py"]


def test_a_file_reverted_to_clean_is_detected(repo):
    (repo / "src" / "app.py").write_text("dirty before\n")
    before = snapshot(repo)
    (repo / "src" / "app.py").write_text("original\n")

    assert changed_since(repo, before) == ["src/app.py"]


# -- the policy ------------------------------------------------------------

def test_a_permitted_change_is_not_a_violation(repo):
    before = snapshot(repo)
    (repo / "tests" / "test_app.py").write_text("new test\n")

    assert not verify(repo, before, allowed=allow_tests_only)


def test_a_forbidden_change_is_reported(repo):
    """Writing tests is fine; rewriting the source they test is the thing to catch."""
    before = snapshot(repo)
    (repo / "src" / "app.py").write_text("made the test pass\n")

    violation = verify(repo, before, allowed=allow_tests_only)

    assert violation
    assert violation.paths == ["src/app.py"]
    assert "src/app.py" in violation.describe()


def test_ignored_prefixes_are_skipped(repo):
    """Harness caches are the tool's doing, not the agent's."""
    (repo / ".agent_cache").mkdir()
    before = snapshot(repo)
    (repo / ".agent_cache" / "turn.jsonl").write_text("{}\n")

    assert not verify(repo, before, allowed=allow_tests_only, ignore=[".agent_cache/"])


def test_violations_are_sorted_so_a_message_is_stable(repo):
    before = snapshot(repo)
    (repo / "src" / "b.py").write_text("x\n")
    (repo / "src" / "a.py").write_text("x\n")

    assert verify(repo, before, allowed=allow_tests_only).paths == ["src/a.py", "src/b.py"]


def test_it_reports_rather_than_raises(repo):
    """One product fails the task, another reverts the file and carries on."""
    before = snapshot(repo)
    (repo / "src" / "app.py").write_text("x\n")

    violation = verify(repo, before, allowed=allow_tests_only)

    assert isinstance(violation.paths, list)


def test_describe_truncates_a_long_list(repo):
    before = snapshot(repo)
    for i in range(15):
        (repo / "src" / f"f{i:02d}.py").write_text("x\n")

    described = verify(repo, before, allowed=allow_tests_only).describe(limit=3)

    assert "and 12 more" in described


def test_a_rename_out_of_an_allowed_directory_is_caught(repo):
    """The bug this found, stated as the behaviour that matters.

    git emits `R  <new>\0<old>\0`. Reading the old name instead of the new one
    means an agent can move a file from somewhere it may write into somewhere
    it may not, and the guard examines the path that no longer exists while the
    one that now does goes unchecked.
    """
    before = snapshot(repo)
    subprocess.run(
        ["git", "mv", "tests/test_app.py", "src/smuggled.py"],
        cwd=repo, check=True, capture_output=True,
    )

    violation = verify(repo, before, allowed=allow_tests_only)

    assert violation, "a rename into src/ escaped the guard"
    assert "src/smuggled.py" in violation.paths


# -- bounded, because it runs twice per turn --------------------------------

def test_a_hanging_status_is_abandoned_rather_than_blocking(monkeypatch, repo):
    """`git status` is normally instant -- but not on a very large checkout, a
    cold cache, or with an index lock held elsewhere. A guard meant to protect
    a turn must not become the thing that hangs it."""
    import subprocess as sp

    def hang(*args, **kwargs):
        raise sp.TimeoutExpired(cmd=args[0] if args else "git", timeout=kwargs.get("timeout"))

    monkeypatch.setattr(sp, "run", hang)

    assert changed_paths(repo) == set()


def test_a_missing_git_binary_does_not_crash_the_run(monkeypatch, repo):
    import subprocess as sp

    monkeypatch.setattr(sp, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))

    assert changed_paths(repo) == set()


def test_the_timeout_is_actually_passed_to_the_subprocess(monkeypatch, repo):
    """A default that never reaches `subprocess.run` is not a timeout."""
    import subprocess as sp

    seen = {}
    real = sp.run

    def record(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(sp, "run", record)
    changed_paths(repo, timeout=7)

    assert seen.get("timeout") == 7


def test_a_snapshot_of_an_unreadable_tree_is_empty_not_wrong(monkeypatch, repo):
    """Empty means "found no evidence", which is where the caller already was."""
    import subprocess as sp

    monkeypatch.setattr(sp, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))

    assert snapshot(repo) == {}


def test_a_unicode_path_survives_the_z_parsing(repo):
    """Runtime artifacts carry non-ASCII names in real repositories."""
    target = repo / "mutants" / "chat_robot" / "test" / "agent"
    target.mkdir(parents=True)
    (target / "提示词.txt").write_text("x\n", encoding="utf-8")

    assert "mutants/chat_robot/test/agent/提示词.txt" in changed_paths(repo)


def test_unicode_and_a_rename_together(repo):
    """Both parsing hazards in one listing, which is how they occur."""
    (repo / "tests" / "配置.py").write_text("x\n", encoding="utf-8")
    subprocess.run(
        ["git", "mv", "tests/test_app.py", "tests/renamed.py"],
        cwd=repo, check=True, capture_output=True,
    )

    paths = changed_paths(repo)

    assert "tests/配置.py" in paths
    assert "tests/renamed.py" in paths
