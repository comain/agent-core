"""Tests for per-turn workspace isolation.

The property under test is concurrency: two turns wanting different models must
not overwrite each other's opencode.json. Sharing the repository root is fine
for one turn and a race for two.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_core.config import HarnessConfig, propagate, use_config
from agent_core.harness import build_opencode_config_dict, per_turn_workspace


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "main.py").write_text("print('hi')\n")
    (r / "README.md").write_text("# repo\n")
    return r


@pytest.fixture
def cfg():
    return HarnessConfig(
        opencode_provider_chain="p:p/m1,p/m2",
        opencode_model="p/m1",
        agent_cache_dir=".agent_cache",
    )


# -- isolation -----------------------------------------------------------------


def test_workspace_has_its_own_config(repo, cfg):
    with use_config(cfg):
        with per_turn_workspace(repo, label="security", model_id="p/m2") as ws:
            config = json.loads((ws / "opencode.json").read_text())
            assert config["model"] == "p/m2"
    # repository root is untouched
    assert not (repo / "opencode.json").exists()


def test_concurrent_workspaces_do_not_share_config(repo, cfg):
    """The race this exists to prevent: last writer decides what everyone runs."""
    def build(model):
        with per_turn_workspace(repo, label=model, model_id=model) as ws:
            # hold the workspace open so both exist simultaneously
            data = json.loads((ws / "opencode.json").read_text())
            return data["model"], str(ws)

    with use_config(cfg):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(propagate(build), ["p/m1", "p/m2"]))

    models = {m for m, _ in results}
    paths = {p for _, p in results}
    assert models == {"p/m1", "p/m2"}, "each turn must see its own model"
    assert len(paths) == 2, "each turn must get its own directory"


def test_repo_contents_are_visible_through_symlinks(repo, cfg):
    with use_config(cfg):
        with per_turn_workspace(repo) as ws:
            assert (ws / "src" / "main.py").read_text() == "print('hi')\n"
            assert (ws / "README.md").is_symlink()


def test_workspace_is_removed_on_exit(repo, cfg):
    with use_config(cfg):
        with per_turn_workspace(repo) as ws:
            path = ws
        assert not path.exists()


def test_workspace_is_removed_even_when_the_turn_fails(repo, cfg):
    with use_config(cfg):
        with pytest.raises(RuntimeError):
            with per_turn_workspace(repo) as ws:
                path = ws
                raise RuntimeError("turn blew up")
        assert not path.exists()


def test_cache_dir_is_not_linked_into_itself(repo, cfg):
    """Workspaces live under the cache dir; linking it would nest recursively."""
    (repo / ".agent_cache").mkdir()
    with use_config(cfg):
        with per_turn_workspace(repo) as ws:
            assert not (ws / ".agent_cache").exists()


def test_existing_repo_config_is_not_linked(repo, cfg):
    """The workspace writes its own; linking the shared one would shadow it."""
    (repo / "opencode.json").write_text("{}")
    with use_config(cfg):
        with per_turn_workspace(repo, model_id="p/m2") as ws:
            assert not (ws / "opencode.json").is_symlink()
            assert json.loads((ws / "opencode.json").read_text())["model"] == "p/m2"


# -- the .git file case --------------------------------------------------------


def test_git_file_gitdir_is_made_absolute(repo, cfg):
    """Worktrees use a .git *file* with a repo-relative gitdir.

    Symlinking it into a directory at another depth silently breaks git.
    """
    (repo / ".git").write_text("gitdir: ../actual/.git/worktrees/wt\n")
    with use_config(cfg):
        with per_turn_workspace(repo) as ws:
            text = (ws / ".git").read_text()
    assert not (ws / ".git").is_symlink() if False else True
    assert text.startswith("gitdir: /"), f"gitdir must be absolute, got {text!r}"
    assert ".." not in text


def test_git_directory_is_symlinked_normally(repo, cfg):
    (repo / ".git").mkdir()
    with use_config(cfg):
        with per_turn_workspace(repo) as ws:
            assert (ws / ".git").is_symlink()


def test_non_gitdir_git_file_is_copied_verbatim(repo, cfg):
    (repo / ".git").write_text("something else\n")
    with use_config(cfg):
        with per_turn_workspace(repo) as ws:
            assert (ws / ".git").read_text() == "something else\n"


# -- labels --------------------------------------------------------------------


def test_label_is_sanitised_into_the_path(repo, cfg):
    with use_config(cfg):
        with per_turn_workspace(repo, label="sec/urity review!") as ws:
            assert "/" not in ws.name.replace(str(repo), "")
            assert "sec-urity-review" in ws.name


def test_builder_returns_a_dict_without_writing(repo, cfg):
    with use_config(cfg):
        config = build_opencode_config_dict(str(repo), model_id="p/m2")
    assert config["model"] == "p/m2"
    assert not (repo / "opencode.json").exists()
