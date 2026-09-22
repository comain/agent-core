"""One answer to "is this path safe to hold private data?".

Checkpoint databases and artifact files carry the same kind of content — graph
state, prompts, model output — and had drifted into two different, weaker
answers to the same question: the checkpointer checked a symlink and a mode
bit, the artifact store checked ``".." in parts``. Both are load-bearing, so
they share one implementation here.

## What "safe" means, and where the boundary is

A **managed root** is a directory this process owns: the directory holding a
checkpoint database, or an artifact store's root. Everything at or below a
managed root must be owner-only and free of symlinks, and this module refuses
rather than repairs — a directory that is already group-readable is a
misconfiguration to report, not to silently `chmod` away, because the window in
which it was readable has already happened.

Ancestors *above* a managed root are deliberately not held to that standard.
They cannot be: `/tmp` is world-writable, and on macOS both `/tmp` and `/var`
are symlinks, so a rule that walked to the filesystem root and rejected shared
or symlinked ancestors would reject every well-formed path on the platform we
develop on. Ancestors are instead resolved through `realpath` for the
containment checks, which is what actually catches "this root reaches into the
target repository by way of a symlink".

## Why forbidden roots exist

A store or checkpoint under the repository the agent is editing is not a
storage bug, it is a correctness bug: the agent commits its own scratch state,
or a workspace reset deletes the evidence of what it did. Callers pass the
repository as a forbidden root and overlap is rejected in **both** directions —
a root inside the repo and a root that *contains* the repo are equally wrong.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePath
from typing import Iterable, Sequence, Type, Union

__all__ = [
    "UnsafePathError",
    "PRIVATE_DIR_MODE",
    "PRIVATE_FILE_MODE",
    "assert_no_forbidden_overlap",
    "assert_within",
    "assert_owner_only",
    "assert_private_file",
    "ensure_private_directory",
    "relative_parts",
]

#: Owner-only, because everything this module guards is task data.
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

#: Anything outside owner permissions. One name, so the error text is uniform.
_SHARED_BITS = 0o077


class UnsafePathError(RuntimeError):
    """A path cannot be trusted to hold private data.

    Subclassed by the callers that have their own public error type
    (`UnsafeCheckpointPathError`, `ArtifactSecurityError`) so a product can
    catch either the specific or the general case.
    """


def _fail(error: Type[Exception], message: str) -> None:
    raise error(message)


def relative_parts(
    name: Union[str, PurePath],
    *,
    error: Type[Exception] = UnsafePathError,
) -> tuple:
    """Split a caller-supplied artifact name into validated path components.

    Conservative on purpose: everything not obviously a plain relative path is
    refused, because the caller can always spell the name differently and a
    permissive reading here is a traversal.

    Backslash is rejected outright rather than treated as a literal character.
    A name that means one thing on POSIX and another on Windows is a name no
    product should be storing artifacts under, and rejecting it costs nothing.

    A leading ``-`` is rejected because these paths reach shell-adjacent tools
    (``git``, ``opencode``) where a filename that parses as an option is how a
    file argument silently becomes a flag.
    """
    if isinstance(name, PurePath):
        text = str(name)
    elif isinstance(name, str):
        text = name
    else:
        _fail(error, f"artifact name must be a string, not {type(name).__name__}")
        raise AssertionError  # pragma: no cover - _fail always raises

    if not text:
        _fail(error, "artifact name is empty")
    if "\x00" in text:
        _fail(error, "artifact name contains a NUL byte")
    if "\\" in text:
        _fail(error, f"artifact name contains a backslash: {text!r}")
    if os.path.isabs(text) or text.startswith("/"):
        _fail(error, f"artifact name must be relative: {text!r}")

    parts = tuple(text.split("/"))
    for part in parts:
        if part == "":
            _fail(error, f"artifact name has an empty path component: {text!r}")
        if part in {".", ".."}:
            _fail(error, f"artifact name has a traversal component: {text!r}")
        if part.startswith("-"):
            _fail(error, f"artifact name component looks like an option: {text!r}")
    return parts


def assert_owner_only(
    path: Path,
    *,
    error: Type[Exception] = UnsafePathError,
) -> None:
    """Refuse a path readable by more than its owner, rather than repairing it.

    Uses `lstat`, so a symlink's own mode is what is judged; callers check for
    symlinks separately and this must not be the thing that follows one.
    """
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    if mode & _SHARED_BITS:
        _fail(
            error,
            f"{path} is accessible beyond its owner ({oct(mode)}); "
            f"repair it with: chmod {oct(PRIVATE_DIR_MODE)[2:]} {path}",
        )


def _resolved(path: Path) -> Path:
    """Absolute, with every existing symlink component resolved.

    `realpath` rather than `Path.resolve(strict=True)` because the path being
    checked routinely does not exist yet — that is the normal case for a first
    write.
    """
    return Path(os.path.realpath(os.path.abspath(os.fspath(path))))


def _contains(ancestor: Path, descendant: Path) -> bool:
    return ancestor == descendant or ancestor in descendant.parents


def assert_no_forbidden_overlap(
    path: Path,
    forbidden_roots: Iterable[Union[str, Path]],
    *,
    error: Type[Exception] = UnsafePathError,
) -> None:
    """Refuse a path that overlaps a caller-named root, in either direction."""
    target = _resolved(path)
    for raw in forbidden_roots:
        forbidden = _resolved(Path(raw).expanduser())
        if _contains(forbidden, target):
            _fail(error, f"{path} is inside a forbidden root: {forbidden}")
        if _contains(target, forbidden):
            _fail(error, f"{path} contains a forbidden root: {forbidden}")


def assert_within(
    root: Path,
    path: Path,
    *,
    error: Type[Exception] = UnsafePathError,
) -> None:
    """Refuse a path that resolves outside ``root``.

    The component-by-component checks are what actually keep a name inside the
    store; this is the cheap independent restatement of the invariant they are
    supposed to guarantee, so a future bug in one of them surfaces as a refusal
    rather than as a write outside the root.
    """
    if not _contains(_resolved(root), _resolved(path)):
        _fail(error, f"{path} resolves outside {root}")


def assert_private_file(
    path: Path,
    *,
    file_mode: int = PRIVATE_FILE_MODE,
    error: Type[Exception] = UnsafePathError,
) -> None:
    """Refuse an existing destination that is not a private regular file."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        _fail(error, f"{path} is a symlink")
    if not stat.S_ISREG(info.st_mode):
        _fail(error, f"{path} is not a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if mode & _SHARED_BITS:
        _fail(
            error,
            f"{path} is accessible beyond its owner ({oct(mode)}); "
            f"repair it with: chmod {oct(file_mode)[2:]} {path}",
        )


def ensure_private_directory(
    path: Path,
    *,
    mode: int = PRIVATE_DIR_MODE,
    forbidden_roots: Sequence[Union[str, Path]] = (),
    error: Type[Exception] = UnsafePathError,
) -> Path:
    """Return ``path`` as an owner-only directory, creating it if it is absent.

    Missing ancestors are created too, each with ``mode`` applied explicitly:
    `mkdir`'s mode argument is masked by the process umask, so a private
    directory that relies on it is private only on machines with a strict
    umask. An existing directory is checked, never chmodded — see the module
    docstring.
    """
    assert_no_forbidden_overlap(path, forbidden_roots, error=error)

    # `lexists`, not `exists`: a dangling symlink is *there* — `exists()` says
    # no, `mkdir` then fails with FileExistsError, and the real problem (a
    # symlinked root) never gets named.
    missing = []
    cursor = path
    while not os.path.lexists(cursor) and cursor.parent != cursor:
        missing.append(cursor)
        cursor = cursor.parent

    for candidate in reversed(missing):
        os.mkdir(candidate)
        os.chmod(candidate, mode)

    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        _fail(error, f"{path} is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        _fail(error, f"{path} is not a directory")
    assert_owner_only(path, error=error)
    return path
