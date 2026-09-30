"""Force-with-lease updates of several refs in one atomic push."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Mapping, Optional

from agent_core.git.workspace import GitWorkspace

_SAFE_REF = re.compile(r"^refs/[A-Za-z0-9._/-]+$")
_SAFE_SHA = re.compile(r"^[0-9a-f]{40,64}$")


def _validate_ref(ref: str) -> str:
    if not ref or ".." in ref or "//" in ref or not _SAFE_REF.fullmatch(ref):
        raise ValueError(f"git ref is invalid: {ref[:80]!r}")
    return ref


def _validate_object_id(value: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return value
    if not value or not _SAFE_SHA.fullmatch(value):
        raise ValueError(f"git object id is invalid: {value[:80]!r}")
    return value


def update_refs_with_lease(
    workspace: GitWorkspace,
    path: Path,
    *,
    ref_updates: Mapping[str, str],
    lease: Mapping[str, str],
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    """Push ``ref_updates`` only if each ref still matches ``lease``.

    One ``git push --atomic --force-with-lease``. The remote must accept
    ``--atomic``; a lease mismatch or a missing atomic capability raises and
    leaves **no** subset of refs updated. Empty expected SHA is a first claim
    (the ref must not exist). Does not resolve ``path_for`` — ``path`` is the
    checkout to push from.
    """
    if set(ref_updates) != set(lease):
        raise ValueError(
            f"ref_updates keys {sorted(ref_updates)} must match lease keys {sorted(lease)}"
        )
    if not ref_updates:
        raise ValueError("ref_updates must not be empty")
    for ref, sha in ref_updates.items():
        _validate_ref(ref)
        _validate_object_id(sha)
    for ref, expected in lease.items():
        _validate_ref(ref)
        _validate_object_id(expected, allow_empty=True)
    lease_args = [f"--force-with-lease={ref}:{expected}" for ref, expected in lease.items()]
    refspecs = [f"{sha}:{ref}" for ref, sha in ref_updates.items()]
    workspace.execute(
        path,
        "push",
        "--atomic",
        *lease_args,
        "origin",
        "--",
        *refspecs,
        check=True,
        is_cancelled=is_cancelled,
    )
