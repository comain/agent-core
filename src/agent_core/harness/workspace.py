"""Per-turn workspace isolation.

`generate_opencode_config` writes `opencode.json` into the repository root. That
is fine for one turn at a time and wrong the moment turns run concurrently: two
turns wanting different models overwrite each other's config, and whichever
wrote last decides what both of them run.

This is not hypothetical -- a consumer fans reviewers out across a thread pool,
which is exactly that race, and its fork solved it by giving each turn a private
project directory whose contents are symlinks back to the real repository. The
turn sees the whole repo; only the config is private.

    with per_turn_workspace(repo_path, label="security", model_id="p/m") as cwd:
        process.run_turn(..., repo_path=str(cwd))

The directory is removed when the block exits.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from agent_core.config import current_config
from agent_core.harness.config import build_opencode_config_dict


def _safe_label(label: Optional[str]) -> str:
    return "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in (label or "turn")
    )[:48]


def _absolute_gitdir(repo_path: Path, git_file: Path) -> str:
    """Rewrite a `.git` *file* to point at an absolute gitdir.

    Worktrees and submodules use a `.git` file containing `gitdir: <path>`, and
    that path is usually relative to the repository. Symlinking the file into a
    directory at a different depth silently breaks it, so the path is made
    absolute instead.
    """
    text = git_file.read_text(encoding="utf-8")
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return text
    gitdir = Path(text[len(prefix):].strip())
    if not gitdir.is_absolute():
        gitdir = (repo_path / gitdir).resolve()
    return f"gitdir: {gitdir}\n"


def link_repo_contents(repo_path: Path, target_dir: Path) -> None:
    """Symlink every repo entry into ``target_dir``.

    Skips the config the turn is about to write, and the cache directory, which
    is where these workspaces live -- linking it would nest a workspace inside
    itself.
    """
    cache_dir = current_config().agent_cache_dir
    skip = {"opencode.json", cache_dir, cache_dir.lstrip(".")}
    for child in repo_path.iterdir():
        if child.name in skip:
            continue
        target = target_dir / child.name
        if child.name == ".git" and child.is_file():
            target.write_text(_absolute_gitdir(repo_path, child), encoding="utf-8")
            continue
        os.symlink(child, target, target_is_directory=child.is_dir())


@contextmanager
def per_turn_workspace(
    repo_path: Path,
    *,
    label: Optional[str] = None,
    model_id: Optional[str] = None,
) -> Iterator[Path]:
    """A private project directory for one turn, removed on exit.

    Use the yielded path as the turn's working directory. Concurrent turns get
    independent configs while sharing one checkout.
    """
    repo_path = Path(repo_path).resolve()
    workspace = (
        repo_path
        / current_config().agent_cache_dir
        / "opencode"
        / "workspaces"
        / f"{int(time.time() * 1000)}-{_safe_label(label)}-{uuid.uuid4().hex[:8]}"
    )
    # exist_ok=False on purpose: a collision means the uniqueness assumption
    # broke, and sharing a workspace is the bug this exists to prevent.
    workspace.mkdir(parents=True, exist_ok=False)
    try:
        link_repo_contents(repo_path, workspace)
        (workspace / "opencode.json").write_text(
            json.dumps(
                build_opencode_config_dict(str(repo_path), model_id=model_id),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        yield workspace
    finally:
        # ignore_errors: cleanup must not mask the turn's own failure.
        shutil.rmtree(workspace, ignore_errors=True)
