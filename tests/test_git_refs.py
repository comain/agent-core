"""Atomic multi-ref lease updates — wiki docs + lease token as one push."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_core.git import GitCommandError, GitWorkspace, update_refs_with_lease


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _sha(path: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _seed(tmp_path: Path) -> tuple[Path, Path, str]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        _git(clone, "config", key, value)
    (clone / "page.md").write_text("v1\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "first")
    first = _sha(clone, "HEAD")
    _git(clone, "update-ref", "refs/heads/docs", first)
    _git(clone, "update-ref", "refs/heads/lease", first)
    _git(clone, "push", "origin", "HEAD:main", "docs:docs", "lease:lease")
    return origin, clone, first


def _commit(clone: Path, text: str) -> str:
    (clone / "page.md").write_text(text)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "next")
    return _sha(clone, "HEAD")


def test_update_refs_with_lease_pushes_both_refs(tmp_path):
    origin, clone, first = _seed(tmp_path)
    nxt = _commit(clone, "v2\n")
    ws = GitWorkspace(tmp_path / "cache")

    update_refs_with_lease(
        ws,
        clone,
        ref_updates={"refs/heads/docs": nxt, "refs/heads/lease": nxt},
        lease={"refs/heads/docs": first, "refs/heads/lease": first},
    )

    assert _sha(origin, "docs") == nxt
    assert _sha(origin, "lease") == nxt


def test_lease_lost_updates_neither_ref(tmp_path):
    origin, clone, first = _seed(tmp_path)
    nxt = _commit(clone, "v2\n")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        _git(other, "config", key, value)
    _git(other, "checkout", "-B", "docs", "origin/docs")
    moved = _commit(other, "stolen\n")
    _git(other, "push", "origin", "HEAD:refs/heads/docs")

    ws = GitWorkspace(tmp_path / "cache")
    with pytest.raises(GitCommandError):
        update_refs_with_lease(
            ws,
            clone,
            ref_updates={"refs/heads/docs": nxt, "refs/heads/lease": nxt},
            lease={"refs/heads/docs": first, "refs/heads/lease": first},
        )

    assert _sha(origin, "docs") == moved
    assert _sha(origin, "lease") == first


def test_empty_expected_sha_is_a_first_claim(tmp_path):
    origin, clone, first = _seed(tmp_path)
    ws = GitWorkspace(tmp_path / "cache")
    update_refs_with_lease(
        ws,
        clone,
        ref_updates={"refs/heads/fresh": first},
        lease={"refs/heads/fresh": ""},
    )
    assert _sha(origin, "fresh") == first


def test_push_is_one_atomic_execute(tmp_path):
    recorded: list[tuple] = []

    class Recorder:
        def execute(self, path, *args, is_cancelled=None, check=False):
            recorded.append(args)
            raise GitCommandError(["git", "push"], 1, "atomic rejected")

    with pytest.raises(GitCommandError, match="atomic rejected"):
        update_refs_with_lease(
            Recorder(),
            tmp_path,
            ref_updates={"refs/heads/docs": "a" * 40, "refs/heads/lease": "b" * 40},
            lease={"refs/heads/docs": "c" * 40, "refs/heads/lease": "d" * 40},
        )

    assert len(recorded) == 1
    args = recorded[0]
    assert args[0] == "push"
    assert "--atomic" in args
    assert f"--force-with-lease=refs/heads/docs:{'c' * 40}" in args
    assert f"--force-with-lease=refs/heads/lease:{'d' * 40}" in args
    assert "origin" in args
    assert args[args.index("origin") + 1] == "--"
    assert f"{'a' * 40}:refs/heads/docs" in args
    assert f"{'b' * 40}:refs/heads/lease" in args


@pytest.mark.parametrize(
    "ref_updates,lease",
    [
        ({"--upload-pack=evil": "a" * 40}, {"--upload-pack=evil": "b" * 40}),
        ({"refs/heads/docs": "not-a-sha"}, {"refs/heads/docs": "c" * 40}),
        ({"refs/heads/../foo": "a" * 40}, {"refs/heads/../foo": "b" * 40}),
        ({"refs/heads/docs": "a" * 40 + "\norigin"}, {"refs/heads/docs": "b" * 40}),
    ],
)
def test_update_refs_rejects_unsafe_ref_or_sha(tmp_path, ref_updates, lease):
    class Boom:
        def execute(self, *a, **k):
            raise AssertionError("must not spawn git")

    with pytest.raises(ValueError):
        update_refs_with_lease(Boom(), tmp_path, ref_updates=ref_updates, lease=lease)
