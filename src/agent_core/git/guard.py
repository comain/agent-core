"""Checking what an agent actually changed in a checkout.

The harness can already stop an agent writing at all: set
``opencode_permissions = {"edit": "deny"}`` and the tools that modify files are
refused. That is the right control when the agent is only meant to read, and
it is the only control there was.

It is all-or-nothing, though, and plenty of work sits in between. A run that is
*supposed* to write tests should not also be rewriting the source it is testing
to make them pass; one that may write a report should not be editing the
checkout around it. "Deny" cannot say that, because the distinction is not
which tool ran, it is which paths came out different.

So this is the other half: snapshot the working tree, run the turn, and compare.
Nothing here decides what is allowed -- a consumer passes that in, because which
paths are legitimate is a fact about that product's layout and nothing this
package can know. What it owns is reading the tree correctly and reporting the
difference.

## Why a digest and not just the path list

Asking git twice and diffing the path sets misses a file the agent rewrote that
was *already* dirty before the turn: it appears in both listings and looks
untouched. Hashing the contents catches that, which matters because a file
already modified is exactly the one a consumer is least likely to be watching.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Union

logger = logging.getLogger(__name__)

#: Recorded instead of a digest when a path cannot be hashed. Distinct values
#: rather than one, because "it is a directory" and "it vanished" mean
#: different things when a consumer reads a violation report.
MISSING = "<missing>"
DIRECTORY = "<dir>"
UNREADABLE = "<unreadable>"


#: How long a status read may take before it is abandoned.
#:
#: `git status` is normally instant, but not always: a very large checkout, a
#: cold filesystem cache, or an index lock held by another process can make it
#: block. Without a bound, a guard meant to protect a turn becomes the thing
#: that hangs it -- and it runs twice per turn, before and after.
DEFAULT_TIMEOUT_SECONDS = 30


def _git(
    repo_path: Union[str, Path],
    *args: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Optional[subprocess.CompletedProcess]:
    """Run a read-only git command, or return None if it could not be run.

    Returns rather than raises for the same reason the callers do: a guard that
    cannot read the tree leaves its caller where it was, and must not convert
    an unreadable checkout into a failed turn.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git %s timed out after %ss in %s", args[0], timeout, repo_path)
        return None
    except OSError as exc:
        # No git on PATH, or the directory is gone.
        logger.warning("could not run git %s in %s: %s", args[0], repo_path, exc)
        return None


def changed_paths(
    repo_path: Union[str, Path], *, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> set:
    """Every path git reports as modified, added, renamed or untracked.

    Uses ``--porcelain=1 -z`` rather than the human format: a path containing a
    space, a quote or a newline is otherwise quoted and escaped, and a consumer
    comparing the result against its own path strings will not match it. ``-z``
    is the only output git guarantees is unambiguous.

    A rename is emitted as ``R  <new>\0<old>\0``: the new name is in the entry
    itself and the old name is the field that follows. The old one is consumed
    and discarded -- it no longer exists, so a consumer asking whether it was
    allowed to change would be asking about a file that is not there, while the
    path that *does* now exist went unexamined. Getting this backwards lets a
    rename into a forbidden directory pass a guard unnoticed, which is how it
    was found.

    Returns an empty set when git fails. A guard that cannot read the tree must
    not answer "nothing changed" *and* must not crash the run; the caller sees
    an empty result and its own before/after comparison finds no evidence of a
    violation, which is the same position it was in before this existed.
    """
    result = _git(
        repo_path, "status", "--porcelain=1", "-z", "--untracked-files=all", timeout=timeout
    )
    paths = set()
    if result is None or result.returncode != 0:
        return paths

    entries = [entry for entry in result.stdout.split("\0") if entry]
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 3:
            continue
        status_code = entry[:2]
        path = entry[2:].lstrip(" ")
        if status_code.startswith("R") or status_code.endswith("R"):
            # Consume the old name without using it; `path` is already the new one.
            index += 1
        if path:
            paths.add(path)
    return paths


def snapshot(repo_path: Union[str, Path]) -> Dict[str, str]:
    """Path -> content digest for everything currently dirty.

    Only dirty paths: hashing a whole repository before every turn would cost
    more than the turn on any real checkout.
    """
    root = Path(repo_path)
    taken: Dict[str, str] = {}
    for relative in changed_paths(root):
        path = root / relative
        if not path.exists():
            taken[relative] = MISSING
        elif path.is_dir():
            taken[relative] = DIRECTORY
        else:
            try:
                taken[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                taken[relative] = UNREADABLE
    return taken


def changed_since(repo_path: Union[str, Path], before: Dict[str, str]) -> List[str]:
    """Paths that differ from ``before``, including ones already dirty then.

    Sorted, so a violation message is stable and a test can assert on it.
    """
    after = snapshot(repo_path)
    touched = {path for path, digest in after.items() if before.get(path) != digest}
    touched |= {path for path in before if path not in after}
    return sorted(touched)


@dataclass(frozen=True)
class WorkspaceViolation:
    """Paths an agent changed that it was not permitted to."""

    paths: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.paths)

    def describe(self, limit: int = 10) -> str:
        shown = ", ".join(self.paths[:limit])
        if len(self.paths) > limit:
            shown += f", and {len(self.paths) - limit} more"
        return f"agent changed {len(self.paths)} path(s) it may not: {shown}"


def verify(
    repo_path: Union[str, Path],
    before: Dict[str, str],
    *,
    allowed: Callable[[str], bool],
    ignore: Optional[Iterable[str]] = None,
) -> WorkspaceViolation:
    """Compare the tree against ``before`` and report disallowed changes.

    ``allowed(path)`` is the consumer's policy: which paths this turn was
    entitled to touch. ``ignore`` skips paths outright, for the caches and
    scratch files a harness writes on its own account -- those are the tool's
    doing, not the agent's, and a consumer should not have to encode them.

    Reports rather than raises. What a violation *means* differs -- one product
    fails the task, another reverts the file and carries on -- and a guard that
    decides that for them is a guard they have to work around.
    """
    skip = tuple(ignore or ())
    offending = [
        path
        for path in changed_since(repo_path, before)
        if not path.startswith(skip) and not allowed(path)
    ]
    return WorkspaceViolation(paths=offending)
