"""Private files that two products can both trust.

Every consumer of this package persists evidence of what a model did —
prompts, diffs, judge input, retrospective material — and each had grown its
own version of the same mechanics. The copy in this package was the weakest of
them: it checked `".." in parts`, wrote with a plain `write_bytes`, and
inherited whatever mode the umask happened to give.

What "trust" has to mean for that content:

**Confined.** A name is a relative path under one caller-owned root, and every
component is validated without following a symlink. Traversal, absolute paths,
NUL, backslash ambiguity and option-like names are refused rather than
normalized — see `agent_core.paths`.

**Private.** Directories `0700`, files `0600`, and an existing entry with
broader permissions is an error naming the path, not something to chmod away.

**Atomic.** A reader either sees the previous bytes or the complete new ones.
Writes go to `<destination>.<uuid>.partial`, are flushed and fsynced, then
`os.replace`d — so a crash mid-write leaves recognizable residue beside the
destination and never a half-written artifact under the real name.

**Immutable where it is evidence.** `immutable=True` says this identity has one
correct content: identical bytes are a no-op, different bytes are
`ArtifactConflictError`. That is what makes a retry safe and a silent
overwrite of evidence impossible. Immutable writes reject content over a byte
limit rather than truncating it, because truncated evidence that reports
success is worse than no evidence.

**Bounded on read.** Every read and every immutable comparison has an explicit
maximum. A verified read of an artifact whose size grew is a refusal decided
from `stat`, before any of it is loaded.

**Serialized per namespace.** Two workers writing the same directory — two
attempts of one operation, a retry racing its predecessor — are ordered by an
advisory lock, and a reader verifying bytes cannot land between a write's
`replace` and the digest it is checking.

The lock set is a fixed 256 files under a reserved `.locks` directory rather
than one lock file per namespace. A per-namespace file grows the inode count
with the workload and leaves a lock file behind for every namespace ever used,
including the ones just deleted — and deleting the lock file is itself the
race. A fixed set is bounded, needs no cleanup, and costs only that two
unrelated namespaces whose digests collide serialize against each other, which
is a performance footnote and not a correctness one.

**Deleted exactly, or not at all.** `delete_namespace` takes a typed
`NamespaceLayout` describing what may be there, and refuses the whole deletion
if it finds anything else. Retention that recursively deletes whatever it finds
is one path bug away from deleting a repository.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List, Sequence, Tuple, Union
from uuid import uuid4

from agent_core.paths import (
    PRIVATE_DIR_MODE,
    PRIVATE_FILE_MODE,
    UnsafePathError,
    assert_owner_only,
    assert_private_file,
    assert_within,
    ensure_private_directory,
    relative_parts,
)

__all__ = [
    "ArtifactConflictError",
    "ArtifactError",
    "ArtifactLayoutError",
    "ArtifactLockedError",
    "ArtifactSecurityError",
    "ArtifactTooLarge",
    "IndexedFileRule",
    "NamespaceLayout",
    "SecureArtifactStore",
    "StoredArtifact",
]

#: Reserved top-level name: the striped lock set lives here, so a caller may
#: not write into it.
LOCKS_DIRNAME = ".locks"

#: One slot per value of the digest's first byte. Fixed, so the inode cost of
#: locking does not grow with the number of namespaces a product ever used.
LOCK_SLOTS = 256

_PARTIAL_SUFFIX = ".partial"

#: `O_NOFOLLOW` is POSIX; naming it once keeps every `os.open` here uniform.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: `<destination>.<uuid4 hex>.partial` -- the only residue a crash can leave.
_RESIDUE = re.compile(r"^(?P<destination>.+)\.[0-9a-f]{32}\.partial$")


class ArtifactError(RuntimeError):
    """Base for every refusal from the artifact store."""


class ArtifactSecurityError(ArtifactError, UnsafePathError):
    """A path, permission or file type the store will not treat as an artifact.

    Deliberately both an `ArtifactError` and an `UnsafePathError`: a product
    catching either "something was wrong with this artifact" or "a path was
    unsafe" should see it, and which of the two a caller cares about depends on
    whether it is reporting to an operator or to a workflow.
    """


class ArtifactConflictError(ArtifactError):
    """An immutable identity was reused for different bytes, or a read's digest
    did not match what the caller expected."""


class ArtifactTooLarge(ArtifactError):
    """Content exceeded an explicit byte limit and truncation was not allowed."""


class ArtifactLockedError(ArtifactError):
    """Another process holds the namespace, and this operation will not wait.

    Only deletion refuses rather than waits. A writer blocking behind another
    writer is ordinary contention; a deletion blocking behind a writer is a
    retention job holding a lock while someone is actively producing the very
    artifacts it came to remove, and the right answer there is to come back
    later.
    """


class ArtifactLayoutError(ArtifactError):
    """The namespace does not contain exactly what the caller said it would.

    Deletion refuses the whole namespace rather than removing the parts it
    recognized: a layout that no longer matches means the caller's model of
    what lives there is wrong, and a partial delete under a wrong model is how
    retention removes something irreplaceable.
    """


@dataclass(frozen=True)
class IndexedFileRule:
    """A family of files numbered by the product: ``chunk-0.json`` and friends.

    Bounds are part of the rule because they are what makes an indexed family
    checkable at all. Without them "anything matching the prefix" is a wildcard,
    and a wildcard in a deletion layout is the recursive delete this refuses.
    """

    prefix: str
    suffix: str
    minimum: int
    maximum: int

    def matches(self, name: str) -> bool:
        if not (name.startswith(self.prefix) and name.endswith(self.suffix)):
            return False
        index = name[len(self.prefix) : len(name) - len(self.suffix) or None]
        return index.isdigit()


@dataclass(frozen=True)
class NamespaceLayout:
    """Exactly what a namespace may contain, for deletion to check against.

    ``allowed_directories`` names subdirectories that belong to the namespace
    wholesale — naming one is the caller's statement that everything inside it
    is theirs, at any depth. What is refused inside one is a symlink or
    anything that is not a regular file, which is what actually makes a
    recursive delete dangerous; depth does not. An earlier revision capped it
    at one level and that was over-constrained: a prompt bundle's references
    are grouped by kind (``references/personas/backend.md``), and forcing that
    flat would have changed paths already quoted in stored prompts.
    """

    required_files: frozenset = frozenset()
    optional_files: frozenset = frozenset()
    allowed_directories: frozenset = frozenset()
    indexed_files: tuple = ()

    def permits_file(self, name: str) -> bool:
        return (
            name in self.required_files
            or name in self.optional_files
            or any(rule.matches(name) for rule in self.indexed_files)
        )


@dataclass(frozen=True)
class StoredArtifact:
    """What was persisted, as the writer's receipt.

    ``relative_path`` rather than an absolute one: it is the half of the
    identity a product should be storing in its own database, and an absolute
    path recorded there breaks the first time the store root moves.
    """

    relative_path: str
    sha256: str
    bytes: int
    truncated: bool = False


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _truncate_utf8(raw: bytes, max_bytes: int) -> bytes:
    """Cut to at most ``max_bytes`` without splitting a code point.

    Backing off one byte at a time terminates in at most three steps: UTF-8
    sequences are four bytes at most.
    """
    cut = raw[:max_bytes]
    while cut:
        try:
            cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
            continue
        break
    return cut


class SecureArtifactStore:
    """Write and read private artifacts beneath one caller-owned root."""

    def __init__(
        self,
        root: Union[str, Path],
        *,
        directory_mode: int = PRIVATE_DIR_MODE,
        file_mode: int = PRIVATE_FILE_MODE,
        forbidden_roots: Sequence[Union[str, Path]] = (),
    ):
        self.root = Path(root).expanduser()
        self._directory_mode = directory_mode
        self._file_mode = file_mode
        self._forbidden_roots = tuple(forbidden_roots)
        ensure_private_directory(
            self.root,
            mode=directory_mode,
            forbidden_roots=self._forbidden_roots,
            error=ArtifactSecurityError,
        )

        # In-process exclusion, one per slot. `flock` alone is not enough: two
        # threads of one process would each be granted the same lock (the
        # kernel arbitrates between open file descriptions, and each thread
        # opens its own), and re-entering the same slot on a second descriptor
        # would deadlock against the first. The RLock gives both — mutual
        # exclusion between threads and re-entrancy within one.
        self._guards = [threading.RLock() for _ in range(LOCK_SLOTS)]
        self._depth = [0] * LOCK_SLOTS
        self._held = {}
        self._locks_dir = self.root / LOCKS_DIRNAME
        self._prepare_lock_set()

    # -- the striped lock set ---------------------------------------------

    def _prepare_lock_set(self) -> None:
        """Create the 256 lock files once, and validate them every open.

        Validated rather than assumed: these files are the only thing standing
        between two workers and a torn artifact, and a symlinked or shared lock
        file is a lock that does not lock.
        """
        ensure_private_directory(
            self._locks_dir, mode=self._directory_mode, error=ArtifactSecurityError
        )
        for slot in range(LOCK_SLOTS):
            path = self._lock_path(slot)
            if not os.path.lexists(path):
                try:
                    os.close(
                        os.open(
                            path,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                            self._file_mode,
                        )
                    )
                except FileExistsError:  # another worker got there first
                    pass
                else:
                    os.chmod(path, self._file_mode)
            assert_private_file(
                path, file_mode=self._file_mode, error=ArtifactSecurityError
            )

    def _lock_path(self, slot: int) -> Path:
        return self._locks_dir / f"{slot:02x}.lock"

    @staticmethod
    def _slot_for(namespace: str) -> int:
        """The digest's first byte. Collisions serialize unrelated namespaces,
        which costs throughput and never correctness."""
        return hashlib.sha256(namespace.encode("utf-8")).digest()[0]

    @contextmanager
    def _namespace_lock(self, namespace: str, *, blocking: bool = True) -> Iterator[None]:
        slot = self._slot_for(namespace)
        guard = self._guards[slot]
        if not guard.acquire(blocking=blocking):
            raise ArtifactLockedError(f"{namespace or self.root} is in use")
        try:
            outermost = self._depth[slot] == 0
            if outermost:
                self._held[slot] = self._acquire_flock(slot, namespace, blocking)
            self._depth[slot] += 1
            try:
                yield
            finally:
                self._depth[slot] -= 1
                if self._depth[slot] == 0:
                    handle = self._held.pop(slot)
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    os.close(handle)
        finally:
            guard.release()

    def _acquire_flock(self, slot: int, namespace: str, blocking: bool) -> int:
        handle = os.open(self._lock_path(slot), os.O_RDWR | _NOFOLLOW)
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            os.close(handle)
            raise ArtifactSecurityError(f"{self._lock_path(slot)} is not a private file")
        try:
            fcntl.flock(
                handle, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except OSError as exc:
            os.close(handle)
            raise ArtifactLockedError(
                f"another process holds {namespace or self.root}"
            ) from exc
        return handle

    # -- paths -------------------------------------------------------------

    def _destination(self, name: str, *, create_parents: bool) -> Tuple[Path, str]:
        """Return the validated destination and the namespace that guards it.

        The namespace is the artifact's directory. Files written side by side
        are therefore serialized together, which is what a multi-file commit
        needs — the manifest and the files it describes are one unit, and a
        lock per file would let a reader see a manifest whose files are still
        being written.
        """
        parts = relative_parts(name, error=ArtifactSecurityError)
        if parts[0] == LOCKS_DIRNAME:
            raise ArtifactSecurityError(
                f"{LOCKS_DIRNAME!r} is reserved by the store: {name!r}"
            )
        if parts[-1].endswith(_PARTIAL_SUFFIX):
            # Otherwise a caller could write a name indistinguishable from
            # crash residue, and recovery would delete a real artifact.
            raise ArtifactSecurityError(
                f"artifact name may not end in {_PARTIAL_SUFFIX!r}: {name!r}"
            )

        directory = self.root
        for part in parts[:-1]:
            directory = directory / part
            if create_parents:
                ensure_private_directory(
                    directory,
                    mode=self._directory_mode,
                    error=ArtifactSecurityError,
                )
            elif directory.is_symlink():
                raise ArtifactSecurityError(f"{directory} is a symlink")
            elif not directory.exists():
                raise FileNotFoundError(f"no artifact at {name}")
            elif not directory.is_dir():
                raise ArtifactSecurityError(f"{directory} is not a directory")

        destination = directory / parts[-1]
        assert_within(self.root, destination, error=ArtifactSecurityError)
        return destination, "/".join(parts[:-1])

    # -- writing -----------------------------------------------------------

    def write_bytes(
        self,
        name: str,
        data: bytes,
        *,
        immutable: bool = False,
        max_bytes: int = 0,
    ) -> StoredArtifact:
        """Persist exactly ``data``, atomically and privately.

        ``max_bytes`` rejects rather than truncates: bytes have no safe cut
        point, so a caller that wants a bounded prefix has to take it itself.
        """
        if max_bytes > 0 and len(data) > max_bytes:
            raise ArtifactTooLarge(
                f"{name}: {len(data)} bytes exceeds the {max_bytes}-byte limit"
            )
        destination, namespace = self._destination(name, create_parents=True)
        with self._namespace_lock(namespace):
            assert_private_file(
                destination, file_mode=self._file_mode, error=ArtifactSecurityError
            )
            if immutable and destination.exists():
                self._assert_same_bytes(name, destination, data)
            else:
                self._atomic_write(destination, data)

        return StoredArtifact(
            relative_path=str(destination.relative_to(self.root)),
            sha256=_digest(data),
            bytes=len(data),
        )

    def write_text(
        self,
        name: str,
        content: str,
        *,
        max_bytes: int = 0,
        immutable: bool = False,
    ) -> StoredArtifact:
        """Persist ``content`` as UTF-8.

        A mutable write over ``max_bytes`` is truncated on a code-point
        boundary and says so in the result; an immutable one is refused,
        because evidence that silently lost its tail is evidence no one can
        rely on.
        """
        raw = content.encode("utf-8")
        truncated = False
        if max_bytes > 0 and len(raw) > max_bytes:
            if immutable:
                raise ArtifactTooLarge(
                    f"{name}: {len(raw)} bytes exceeds the {max_bytes}-byte "
                    "limit and immutable artifacts are never truncated"
                )
            raw = _truncate_utf8(raw, max_bytes)
            truncated = True

        stored = self.write_bytes(name, raw, immutable=immutable)
        if not truncated:
            return stored
        return StoredArtifact(
            relative_path=stored.relative_path,
            sha256=stored.sha256,
            bytes=stored.bytes,
            truncated=True,
        )

    def write_json(
        self,
        name: str,
        value: Any,
        *,
        immutable: bool = False,
        max_bytes: int = 0,
    ) -> StoredArtifact:
        """Persist ``value`` as deterministic JSON.

        Sorted keys and `ensure_ascii=False` make the bytes — and therefore the
        digest — a function of the value alone, which is what lets an immutable
        rewrite of the same data be recognized as a no-op. `allow_nan=False`
        because `NaN` is not JSON and a reader in another language will not
        accept what Python writes for it.
        """
        text = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        return self.write_bytes(
            name, text.encode("utf-8"), immutable=immutable, max_bytes=max_bytes
        )

    def _assert_same_bytes(self, name: str, destination: Path, data: bytes) -> None:
        """Bounded comparison for an immutable rewrite.

        The bound is the payload itself: a destination of a different size
        cannot be the same content, and that is decided from `stat` without
        reading anything.
        """
        size = destination.stat().st_size
        if size != len(data) or self._read_exact(destination, len(data)) != data:
            raise ArtifactConflictError(
                f"{name} already exists with different content "
                f"({size} bytes stored, {len(data)} offered)"
            )

    def _atomic_write(self, destination: Path, data: bytes) -> None:
        partial = destination.parent / f"{destination.name}.{uuid4().hex}{_PARTIAL_SUFFIX}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
        try:
            with os.fdopen(os.open(partial, flags, self._file_mode), "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(partial, self._file_mode)
            os.replace(partial, destination)
        except BaseException:
            # Any failure leaves no trusted artifact and no residue we could
            # later mistake for a recoverable partial write.
            partial.unlink(missing_ok=True)
            raise
        self._fsync_directory(destination.parent)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Make the rename itself durable, not just the bytes it points at."""
        handle = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)

    # -- reading -----------------------------------------------------------

    def read_bytes(self, name: str, *, max_bytes: int) -> bytes:
        """Read an artifact, refusing anything larger than ``max_bytes``."""
        destination, namespace = self._destination(name, create_parents=False)
        # Under the lock: otherwise a read can land between a concurrent
        # write's `replace` and the digest a verified read is checking, and
        # report tampering that never happened.
        with self._namespace_lock(namespace):
            assert_private_file(
                destination, file_mode=self._file_mode, error=ArtifactSecurityError
            )
            if not destination.exists():
                raise FileNotFoundError(f"no artifact at {name}")
            size = destination.stat().st_size
            if size > max_bytes:
                raise ArtifactTooLarge(
                    f"{name}: {size} bytes exceeds the {max_bytes}-byte read limit"
                )
            return self._read_exact(destination, max_bytes)

    def read_verified(self, name: str, *, sha256: str, max_bytes: int) -> bytes:
        """Read an artifact and refuse it unless it still hashes to ``sha256``.

        The digest a product recorded when it wrote the artifact is what makes
        this a verification rather than a hope: a file edited in place, or
        restored from the wrong backup, fails here instead of being fed back
        into a workflow as trusted input.
        """
        data = self.read_bytes(name, max_bytes=max_bytes)
        actual = _digest(data)
        if actual != sha256:
            raise ArtifactConflictError(
                f"{name} does not match the expected digest "
                f"(expected {sha256}, found {actual})"
            )
        return data

    def read_verified_text(self, name: str, *, sha256: str, max_bytes: int) -> str:
        return self.read_verified(name, sha256=sha256, max_bytes=max_bytes).decode(
            "utf-8"
        )

    def read_verified_json(self, name: str, *, sha256: str, max_bytes: int) -> Any:
        return json.loads(
            self.read_verified_text(name, sha256=sha256, max_bytes=max_bytes)
        )

    def locked(self, namespace: str):
        """Hold one namespace across several operations.

        A multi-file commit — a prompt bundle whose manifest must land last —
        is one unit, and a reader must not see the manifest before the files it
        describes. Re-entrant, so the writes inside take the lock they already
        hold rather than deadlocking against themselves.
        """
        parts = relative_parts(namespace, error=ArtifactSecurityError)
        if parts[0] == LOCKS_DIRNAME:
            raise ArtifactSecurityError(f"{LOCKS_DIRNAME!r} is reserved by the store")
        return self._namespace_lock("/".join(parts))

    def delete_file(self, name: str) -> bool:
        """Remove one validated artifact. False if it was not there.

        Deliberately narrow: it takes a single validated name rather than a
        pattern or a directory, so the only thing it can remove is a file the
        caller could have written. Namespace deletion goes through
        `delete_namespace` and its typed layout.
        """
        with self.locked(self._namespace_of(name)):
            try:
                destination, _ = self._destination(name, create_parents=False)
            except FileNotFoundError:
                return False
            assert_private_file(
                destination, file_mode=self._file_mode, error=ArtifactSecurityError
            )
            if not destination.is_file():
                return False
            os.unlink(destination)
            self._fsync_directory(destination.parent)
            return True

    def _namespace_of(self, name: str) -> str:
        parts = relative_parts(name, error=ArtifactSecurityError)
        return "/".join(parts[:-1])

    def exists(self, name: str) -> bool:
        """Whether an artifact is there, without following a symlink to say so."""
        try:
            destination, _ = self._destination(name, create_parents=False)
        except (ArtifactSecurityError, FileNotFoundError):
            return False
        return destination.is_file() and not destination.is_symlink()

    # -- deletion ----------------------------------------------------------

    def delete_namespace(self, namespace: str, layout: NamespaceLayout) -> int:
        """Delete one namespace, but only if it holds exactly what ``layout``
        says it may. Returns the number of files removed.

        Nothing is removed until everything has been checked, so a namespace
        that has drifted from its layout comes back unchanged and reportable
        rather than half-deleted. A missing namespace is 0, not an error:
        retention runs more than once and the second run is not a failure.

        The lock is taken without waiting. A namespace someone is actively
        writing is not one to delete out from under them.
        """
        parts = relative_parts(namespace, error=ArtifactSecurityError)
        if parts[0] == LOCKS_DIRNAME:
            raise ArtifactSecurityError(f"{LOCKS_DIRNAME!r} is reserved by the store")
        directory = self.root.joinpath(*parts)
        assert_within(self.root, directory, error=ArtifactSecurityError)

        with self._namespace_lock("/".join(parts), blocking=False):
            if not os.path.lexists(directory):
                return 0
            if directory.is_symlink():
                raise ArtifactSecurityError(f"{directory} is a symlink")
            if not directory.is_dir():
                raise ArtifactSecurityError(f"{directory} is not a directory")
            assert_owner_only(directory, error=ArtifactSecurityError)

            files, directories = self._plan_deletion(directory, layout)
            for path in files:
                os.unlink(path)
            for path in directories:
                os.rmdir(path)
            os.rmdir(directory)
            self._fsync_directory(directory.parent)
            return len(files)

    def _plan_deletion(
        self, directory: Path, layout: NamespaceLayout
    ) -> Tuple[List[Path], List[Path]]:
        """Decide what would be deleted, refusing anything unaccounted for."""
        files: List[Path] = []
        directories: List[Path] = []
        found = set()
        counts = [0] * len(layout.indexed_files)

        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            path = Path(entry.path)
            if entry.is_symlink():
                raise ArtifactSecurityError(f"{path} is a symlink")
            if entry.is_dir(follow_symlinks=False):
                if entry.name not in layout.allowed_directories:
                    raise ArtifactLayoutError(f"{path} is not in the layout")
                nested_files, nested_dirs = self._plan_allowed_directory(path)
                files.extend(nested_files)
                # Deepest first, so every rmdir sees an empty directory.
                directories.extend(nested_dirs)
                directories.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ArtifactSecurityError(f"{path} is not a regular file")

            if layout.permits_file(entry.name):
                found.add(entry.name)
                for index, rule in enumerate(layout.indexed_files):
                    if rule.matches(entry.name):
                        counts[index] += 1
                files.append(path)
                continue

            residue = _RESIDUE.match(entry.name)
            if residue and layout.permits_file(residue.group("destination")):
                # A crash between this store's write and its rename. The name
                # is one only this store produces, for a destination the layout
                # allows, so it is recognizable residue rather than content.
                files.append(path)
                continue
            raise ArtifactLayoutError(f"{path} is not in the layout")

        missing = layout.required_files - found
        if missing:
            raise ArtifactLayoutError(
                f"{directory} is missing required files: {sorted(missing)}"
            )
        for index, rule in enumerate(layout.indexed_files):
            if not rule.minimum <= counts[index] <= rule.maximum:
                raise ArtifactLayoutError(
                    f"{directory} has {counts[index]} {rule.prefix}*{rule.suffix} "
                    f"files, outside {rule.minimum}..{rule.maximum}"
                )
        return files, directories

    @classmethod
    def _plan_allowed_directory(cls, directory: Path) -> Tuple[List[Path], List[Path]]:
        """Everything under a directory the layout named, at any depth.

        The caller declared this subtree theirs, so depth is not the risk. A
        symlink is: following one would delete whatever it points at, outside
        the namespace entirely. Anything that is neither a regular file nor a
        directory is refused too — a socket or a device node under an artifact
        root is not something to remove on a retention job's judgement.
        """
        files: List[Path] = []
        directories: List[Path] = []
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            path = Path(entry.path)
            if entry.is_symlink():
                raise ArtifactSecurityError(f"{path} is a symlink")
            if entry.is_dir(follow_symlinks=False):
                nested_files, nested_dirs = cls._plan_allowed_directory(path)
                files.extend(nested_files)
                directories.extend(nested_dirs)
                directories.append(path)
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ArtifactLayoutError(f"{path} is not a regular file")
            files.append(path)
        return files, directories

    @staticmethod
    def _read_exact(destination: Path, limit: int) -> bytes:
        """Open without following a symlink, and never read past ``limit``."""
        flags = os.O_RDONLY | _NOFOLLOW
        with os.fdopen(os.open(destination, flags), "rb") as stream:
            return stream.read(limit)
