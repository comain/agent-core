"""Typed change-range and diff-summary collection over a Git workspace."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any, Callable, Optional, Sequence

from agent_core.git.workspace import GitCommandError, GitTimeout, GitWorkspace


_SAFE_REF = re.compile(r"[A-Za-z0-9._/~^{}-]+")
_DIFF_FILTER_CODES = frozenset("ACDMRTUXB")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChangeSet:
    """The bounded Git context most agent workflows need before a turn."""

    diff_range: str
    changed_files: tuple[str, ...]
    diff_stat: str
    commit_log: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "diff_range": self.diff_range,
            "changed_files": list(self.changed_files),
            "diff_stat": self.diff_stat,
            "commit_log": self.commit_log,
        }


class ChangeCollector:
    """Resolve what changed without owning product review policy."""

    def __init__(self, workspace: GitWorkspace) -> None:
        self.workspace = workspace

    def collect(
        self,
        path: Path,
        *,
        base_refs: Sequence[str],
        fallback_refs: Sequence[str] = ("HEAD~5", "HEAD~3", "HEAD~1"),
        refresh: bool = True,
        strict_base: bool = False,
        diff_filter: str = "ACMR",
        stat_width: int = 160,
        max_commits: int = 100,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> ChangeSet:
        if not diff_filter or not set(diff_filter) <= _DIFF_FILTER_CODES:
            raise ValueError(f"invalid diff filter: {diff_filter!r}")
        if stat_width < 1:
            raise ValueError("stat_width must be positive")
        if max_commits < 1:
            raise ValueError("max_commits must be positive")
        base_refs = tuple(_validate_ref(ref) for ref in base_refs)
        fallback_refs = tuple(_validate_ref(ref) for ref in fallback_refs)
        if strict_base:
            diff_range = self.resolve_strict_range(
                path, base_refs=base_refs, refresh=refresh, is_cancelled=is_cancelled,
            )
        else:
            if refresh:
                self.refresh_base_refs(path, base_refs, is_cancelled=is_cancelled)
            diff_range = self.resolve_range(
                path,
                base_refs=base_refs,
                fallback_refs=fallback_refs,
                is_cancelled=is_cancelled,
            )
        query = self.workspace.query if strict_base else self.workspace.output
        output = lambda *args: query(  # noqa: E731
            path,
            *args,
            is_cancelled=is_cancelled,
        )
        changed_files = tuple(
            item.strip()
            for item in output(
                "diff",
                "--name-only",
                f"--diff-filter={diff_filter}",
                diff_range,
            ).splitlines()
            if item.strip()
        )
        return ChangeSet(
            diff_range=diff_range,
            changed_files=changed_files,
            diff_stat=output("diff", f"--stat={stat_width}", diff_range),
            commit_log=output(
                "log",
                "--oneline",
                "--no-merges",
                f"--max-count={max_commits}",
                diff_range,
            ),
        )

    def resolve_strict_range(
        self,
        path: Path,
        *,
        base_refs: Sequence[str],
        refresh: bool = True,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> str:
        """Require a real merge-base; recover shallow history without moving HEAD.

        Missing candidate refs may be skipped, but fetch errors, cancellation,
        and unrelated histories cannot become a recent-commit review. Recovery
        uses the workspace's bounded command timeout and transient retry policy.
        """
        refs = tuple(_validate_ref(ref) for ref in base_refs)
        query = lambda *args: self.workspace.query(path, *args, is_cancelled=is_cancelled)
        head = query("rev-parse", "HEAD")
        for ref in refs:
            if refresh and ref.startswith("origin/"):
                if not self.workspace.refresh_ref(path, ref.removeprefix("origin/"), is_cancelled=is_cancelled):
                    continue
            base_head = query("rev-parse", "--verify", f"{ref}^{{commit}}")
            result = self.workspace.execute(path, "merge-base", head, base_head, is_cancelled=is_cancelled)
            if result.returncode == 1 and query("rev-parse", "--is-shallow-repository") == "true":
                logger.info("recovering shallow history for comparison base=%s", ref)
                # Fetch advertised refs: older Git servers reject historical SHA
                # requests. Keep comparing the pinned commits even if refs advance.
                # All heads cover detached HEAD in a single-branch clone.
                self.workspace.execute(path, "fetch", "--no-tags", "--unshallow",
                                       "origin", "+refs/heads/*:refs/remotes/origin/*",
                                       check=True, is_cancelled=is_cancelled)
            elif result.returncode == 0 and result.stdout.strip():
                return f"{result.stdout.strip()}..HEAD"
            base = query("merge-base", head, base_head)
            if base:
                return f"{base}..HEAD"
        raise RuntimeError(f"Cannot resolve required comparison base: {', '.join(refs)}")

    def refresh_base_refs(
        self,
        path: Path,
        base_refs: Sequence[str],
        *,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        if not self.workspace.output(
            path,
            "remote",
            "get-url",
            "origin",
            is_cancelled=is_cancelled,
        ):
            return
        for ref in base_refs:
            ref = _validate_ref(ref)
            if not ref.startswith("origin/"):
                continue
            self.workspace.refresh_ref(
                path,
                ref.removeprefix("origin/"),
                is_cancelled=is_cancelled,
            )

    def resolve_range(
        self,
        path: Path,
        *,
        base_refs: Sequence[str],
        fallback_refs: Sequence[str] = ("HEAD~5", "HEAD~3", "HEAD~1"),
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> str:
        for ref in base_refs:
            ref = _validate_ref(ref)
            try:
                exists = self.workspace.ref_exists(path, ref, is_cancelled=is_cancelled)
            except (GitCommandError, GitTimeout) as exc:
                logger.warning("cannot inspect base ref %s: %s", ref, exc)
                continue
            if not exists:
                continue
            try:
                merge_base = self.workspace.output(
                    path,
                    "merge-base",
                    "HEAD",
                    ref,
                    is_cancelled=is_cancelled,
                )
            except (GitCommandError, GitTimeout) as exc:
                logger.warning("cannot resolve merge base for %s: %s", ref, exc)
                continue
            if merge_base:
                return f"{merge_base}..HEAD"
        for ref in fallback_refs:
            ref = _validate_ref(ref)
            try:
                if self.workspace.ref_exists(path, ref, is_cancelled=is_cancelled):
                    return f"{ref}..HEAD"
            except (GitCommandError, GitTimeout) as exc:
                logger.warning("cannot inspect fallback ref %s: %s", ref, exc)
        return "HEAD"


def _validate_ref(ref: str) -> str:
    ref = str(ref).strip()
    if not ref or ref.startswith("-") or not _SAFE_REF.fullmatch(ref):
        raise ValueError(f"invalid Git ref: {ref[:80]!r}")
    return ref
