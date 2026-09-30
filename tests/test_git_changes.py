from pathlib import Path
import subprocess

import pytest

from agent_core.git import ChangeCollector, GitCancelled, GitTimeout, GitWorkspace


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-b", "master", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "feature")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('feature')\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "feature")
    return repo


def test_change_collector_resolves_base_and_collects_typed_context(tmp_path):
    repo = _repo(tmp_path)
    collector = ChangeCollector(GitWorkspace(tmp_path / "cache"))

    changes = collector.collect(repo, base_refs=["master"], refresh=False)

    assert changes.diff_range.endswith("..HEAD")
    assert changes.changed_files == ("src/app.py",)
    assert "app.py" in changes.diff_stat
    assert "feature" in changes.commit_log
    assert changes.to_dict()["changed_files"] == ["src/app.py"]


def test_change_collector_uses_recent_commit_fallback_and_bounds_log(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "other.py").write_text("print('other')\n", encoding="utf-8")
    _git(repo, "add", "src/other.py")
    _git(repo, "commit", "-m", "other")
    collector = ChangeCollector(GitWorkspace(tmp_path / "cache"))

    changes = collector.collect(
        repo,
        base_refs=["origin/does-not-exist"],
        fallback_refs=["HEAD~1"],
        max_commits=1,
    )

    assert changes.diff_range == "HEAD~1..HEAD"
    assert changes.changed_files == ("src/other.py",)
    assert changes.commit_log.splitlines() == [_git(repo, "log", "-1", "--oneline")]


@pytest.mark.parametrize(
    "options, message",
    [
        ({"diff_filter": ""}, "diff filter"),
        ({"diff_filter": "Z"}, "diff filter"),
        ({"stat_width": 0}, "stat_width"),
        ({"max_commits": 0}, "max_commits"),
        ({"base_refs": ["--upload-pack"]}, "invalid Git ref"),
    ],
)
def test_change_collector_rejects_unsafe_or_unbounded_options(tmp_path, options, message):
    repo = _repo(tmp_path)
    collector = ChangeCollector(GitWorkspace(tmp_path / "cache"))
    options = dict(options)
    base_refs = options.pop("base_refs", [])

    with pytest.raises(ValueError, match=message):
        collector.collect(repo, base_refs=base_refs, refresh=False, **options)


def test_change_collector_returns_head_when_no_range_exists(tmp_path):
    repo = _repo(tmp_path)
    collector = ChangeCollector(GitWorkspace(tmp_path / "cache"))

    assert collector.resolve_range(repo, base_refs=[], fallback_refs=[]) == "HEAD"


def test_change_collector_skips_unavailable_refs_but_not_cancellation():
    class Workspace:
        @staticmethod
        def ref_exists(path, ref, **kwargs):
            if ref == "cancelled":
                raise GitCancelled("stop")
            raise GitTimeout("unavailable")

    collector = ChangeCollector(Workspace())

    assert collector.resolve_range(
        Path("/repo"),
        base_refs=["origin/main"],
        fallback_refs=["HEAD~1"],
    ) == "HEAD"
    with pytest.raises(GitCancelled, match="stop"):
        collector.resolve_range(
            Path("/repo"),
            base_refs=["cancelled"],
            fallback_refs=[],
        )


def test_change_collector_skips_base_when_merge_base_is_unavailable():
    class Workspace:
        @staticmethod
        def ref_exists(path, ref, **kwargs):
            return True

        @staticmethod
        def output(path, *args, **kwargs):
            raise GitTimeout("unavailable")

    assert ChangeCollector(Workspace()).resolve_range(
        Path("/repo"),
        base_refs=["origin/main"],
        fallback_refs=[],
    ) == "HEAD"


def test_change_collector_refreshes_only_origin_refs():
    class Workspace:
        refreshed = []

        @staticmethod
        def output(path, *args, **kwargs):
            return "git@example/repo.git"

        @classmethod
        def refresh_ref(cls, path, branch, **kwargs):
            cls.refreshed.append(branch)
            return True

    collector = ChangeCollector(Workspace())

    collector.refresh_base_refs(Path("/repo"), ["master", "origin/main"])

    assert Workspace.refreshed == ["main"]


def test_change_collector_skips_refresh_without_origin():
    class Workspace:
        @staticmethod
        def output(path, *args, **kwargs):
            return ""

        @staticmethod
        def refresh_ref(path, branch, **kwargs):
            raise AssertionError("must not refresh without origin")

    ChangeCollector(Workspace()).refresh_base_refs(Path("/repo"), ["origin/main"])


@pytest.mark.parametrize("failure", [None, GitTimeout, GitCancelled, "transient"])
def test_strict_collection_recovers_shallow_history_before_test_only_tail(tmp_path, monkeypatch, failure):
    source = _repo(tmp_path)
    for i in range(6):
        (source / "test_app.py").write_text(f"assert {i} >= 0\n")
        _git(source, "add", ".")
        _git(source, "commit", "-m", "repair tests")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "--depth", "6", "--branch", "feature", source.as_uri(), str(shallow))
    collector = ChangeCollector(GitWorkspace(tmp_path / "cache", retry_times=2))
    head = _git(shallow, "rev-parse", "HEAD")
    attempts = []
    if failure == "transient":
        polling = collector.workspace._run_polling
        def transient_failure(cmd, **kwargs):
            if "--unshallow" in cmd:
                attempts.append(cmd)
                if len(attempts) == 1:
                    return subprocess.CompletedProcess(cmd, 128, "", "fatal: Connection reset by peer")
            return polling(cmd, **kwargs)
        monkeypatch.setattr(collector.workspace, "_run_polling", transient_failure)
    elif failure:
        execute = collector.workspace.execute
        def fail_recovery(path, *args, **kwargs):
            if "--unshallow" in args:
                raise failure("recovery interrupted")
            return execute(path, *args, **kwargs)
        monkeypatch.setattr(collector.workspace, "execute", fail_recovery)
        with pytest.raises(failure):
            collector.collect(shallow, base_refs=["origin/master"], strict_base=True)
        return
    changes = collector.collect(shallow, base_refs=["origin/master"], strict_base=True)
    assert changes.diff_range == f"{_git(source, 'rev-parse', 'master')}..HEAD"
    assert "src/app.py" in changes.changed_files
    assert _git(shallow, "rev-parse", "HEAD") == head
    if failure == "transient":
        assert len(attempts) == 2


def test_strict_collection_rejects_missing_base(tmp_path):
    repo = _repo(tmp_path)
    collector = ChangeCollector(GitWorkspace(tmp_path / "cache"))
    with pytest.raises(RuntimeError):
        collector.collect(repo, base_refs=["origin/missing"], strict_base=True)


def test_strict_collection_does_not_hide_diff_failure(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    collector = ChangeCollector(GitWorkspace(tmp_path / "cache"))
    query = collector.workspace.query
    def fail_diff(path, *args, **kwargs):
        if args[0] == "diff":
            raise GitTimeout("diff failed")
        return query(path, *args, **kwargs)
    monkeypatch.setattr(collector.workspace, "query", fail_diff)
    with pytest.raises(GitTimeout):
        collector.collect(repo, base_refs=["master"], refresh=False, strict_base=True)
