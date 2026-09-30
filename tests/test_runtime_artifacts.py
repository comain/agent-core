"""Private files that two products can both trust.

The old store checked `".." in parts` and called `write_bytes`. These tests
describe what replaced it: confinement that survives a symlink, writes that a
crash cannot leave half-applied, evidence that cannot be silently overwritten,
and reads that are bounded and verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading

import pytest

from agent_core.runtime import (
    ArtifactConflictError,
    ArtifactLayoutError,
    ArtifactLockedError,
    ArtifactSecurityError,
    ArtifactTooLarge,
    IndexedFileRule,
    NamespaceLayout,
    SecureArtifactStore,
    StoredArtifact,
)


@pytest.fixture
def store(private_root):
    return SecureArtifactStore(private_root / "artifacts")


def contents(directory):
    """Names in a store directory, minus the store's own lock set."""
    return sorted(p.name for p in directory.iterdir() if p.name != ".locks")


# -- what a caller gets back -----------------------------------------------

def test_a_write_reports_the_bytes_that_were_persisted(store):
    stored = store.write_text("review/diff.patch", "abcdef")

    assert stored == StoredArtifact(
        relative_path="review/diff.patch",
        sha256=hashlib.sha256(b"abcdef").hexdigest(),
        bytes=6,
        truncated=False,
    )
    assert (store.root / "review/diff.patch").read_bytes() == b"abcdef"


def test_the_receipt_is_relative_so_a_product_can_store_it(store):
    """An absolute path in a product's database breaks the first time the
    store root moves; the relative half is the durable identity."""
    assert store.write_text("a/b/c.txt", "x").relative_path == "a/b/c.txt"


def test_json_is_deterministic_utf8(store):
    stored = store.write_json("payload.json", {"z": "中文", "a": 1})

    raw = (store.root / "payload.json").read_bytes()
    assert json.loads(raw) == {"a": 1, "z": "中文"}
    assert raw.index(b'"a"') < raw.index(b'"z"')
    assert "中文" in raw.decode("utf-8"), "ensure_ascii would have escaped it"
    assert stored.sha256 == hashlib.sha256(raw).hexdigest()


def test_json_refuses_nan(store):
    """`NaN` is not JSON. Writing it produces bytes no other language's parser
    will accept, and the failure surfaces wherever the artifact is read."""
    with pytest.raises(ValueError):
        store.write_json("bad.json", {"value": float("nan")})


# -- bounds ----------------------------------------------------------------

def test_text_truncates_on_a_code_point_boundary_and_says_so(store):
    """A cut in the middle of a multi-byte character makes the whole artifact
    undecodable, which is a worse outcome than a shorter one."""
    stored = store.write_text("clip.txt", "a中文", max_bytes=3)

    assert (store.root / "clip.txt").read_bytes().decode("utf-8") == "a"
    assert stored.truncated is True
    assert stored.bytes == 1


def test_immutable_text_is_rejected_rather_than_truncated(store):
    """Evidence that silently lost its tail is evidence no one can rely on."""
    with pytest.raises(ArtifactTooLarge):
        store.write_text("evidence.txt", "a" * 100, max_bytes=10, immutable=True)
    assert not (store.root / "evidence.txt").exists()


def test_bytes_are_rejected_rather_than_cut(store):
    """Bytes have no safe cut point, so the caller has to take the prefix."""
    with pytest.raises(ArtifactTooLarge):
        store.write_bytes("blob.bin", b"0123456789", max_bytes=4)


def test_a_read_needs_an_explicit_limit_and_enforces_it(store):
    store.write_text("big.txt", "0123456789")

    with pytest.raises(ArtifactTooLarge):
        store.read_bytes("big.txt", max_bytes=4)
    with pytest.raises(TypeError):
        store.read_bytes("big.txt")


# -- immutability ----------------------------------------------------------

def test_an_immutable_rewrite_of_identical_bytes_is_a_no_op(store):
    first = store.write_bytes("run/evidence.bin", b"xyz", immutable=True)
    before = os.stat(store.root / "run/evidence.bin").st_ino

    assert store.write_bytes("run/evidence.bin", b"xyz", immutable=True) == first
    assert os.stat(store.root / "run/evidence.bin").st_ino == before, "rewritten"


def test_reusing_an_immutable_identity_for_other_bytes_is_a_conflict(store):
    store.write_bytes("run/evidence.bin", b"xyz", immutable=True)

    with pytest.raises(ArtifactConflictError):
        store.write_bytes("run/evidence.bin", b"other", immutable=True)
    assert (store.root / "run/evidence.bin").read_bytes() == b"xyz"


def test_a_mutable_write_replaces(store):
    store.write_text("notes.txt", "first")
    store.write_text("notes.txt", "second")

    assert (store.root / "notes.txt").read_text() == "second"


# -- verified reads --------------------------------------------------------

def test_a_verified_read_returns_the_bytes_that_were_written(store):
    stored = store.write_json("in.json", {"a": [1, 2]})

    assert store.read_verified_json(
        "in.json", sha256=stored.sha256, max_bytes=1024
    ) == {"a": [1, 2]}


def test_a_verified_read_refuses_content_that_changed_underneath(store):
    """An artifact edited in place, or restored from the wrong backup, fails
    here instead of being fed back into a workflow as trusted input."""
    stored = store.write_text("evidence.txt", "original")
    (store.root / "evidence.txt").write_text("tampered")

    with pytest.raises(ArtifactConflictError):
        store.read_verified("evidence.txt", sha256=stored.sha256, max_bytes=1024)


def test_a_verified_read_of_a_missing_artifact_says_so(store):
    with pytest.raises(FileNotFoundError):
        store.read_verified("gone.txt", sha256="0" * 64, max_bytes=16)

    with pytest.raises(FileNotFoundError):
        store.read_bytes("no/such/dir.txt", max_bytes=16)


def test_a_read_refuses_an_artifact_someone_widened(store):
    stored = store.write_text("evidence.txt", "original")
    os.chmod(store.root / "evidence.txt", 0o644)

    with pytest.raises(ArtifactSecurityError):
        store.read_verified("evidence.txt", sha256=stored.sha256, max_bytes=1024)


# -- confinement -----------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    [
        "../outside.txt",          # traversal
        "a/../../outside.txt",     # traversal after a valid component
        "/etc/passwd",             # absolute
        "",                        # empty
        "a//b.txt",                # empty component
        "./b.txt",                 # current-directory component
        "a\\b.txt",                # means two different things on two platforms
        "-rf",                     # parses as an option to a shell-adjacent tool
        "a/-rf",
        "with\x00nul.txt",
        ".locks/steal.lock",       # reserved for the store's own lock set
        "out.txt.partial",         # indistinguishable from crash residue
    ],
)
def test_names_that_are_not_plainly_inside_the_store_are_refused(store, name):
    with pytest.raises(ArtifactSecurityError):
        store.write_text(name, "unsafe")


def test_a_symlinked_destination_is_not_followed(store, tmp_path):
    """Otherwise the artifact lands wherever the link points, with whatever
    permissions that file already had."""
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    (store.root / "link.txt").symlink_to(outside)

    with pytest.raises(ArtifactSecurityError, match="symlink"):
        store.write_text("link.txt", "hijacked")
    assert outside.read_text() == "original"


def test_a_symlinked_parent_is_not_followed(store, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (store.root / "sub").symlink_to(elsewhere)

    with pytest.raises(ArtifactSecurityError, match="symlink"):
        store.write_text("sub/note.txt", "hijacked")
    assert list(elsewhere.iterdir()) == []


def test_a_store_inside_the_repository_being_edited_is_refused(private_root):
    """A workspace reset deletes the evidence, or the agent commits it."""
    repo = private_root / "repo"
    repo.mkdir(mode=0o700)

    with pytest.raises(ArtifactSecurityError, match="forbidden root"):
        SecureArtifactStore(repo / ".artifacts", forbidden_roots=[repo])


def test_a_store_containing_the_repository_is_refused(private_root):
    repo = private_root / "repo"
    repo.mkdir(mode=0o700)

    with pytest.raises(ArtifactSecurityError, match="forbidden root"):
        SecureArtifactStore(private_root, forbidden_roots=[repo])


# -- privacy ---------------------------------------------------------------

def test_everything_it_creates_is_owner_only(store):
    store.write_text("deep/nested/note.txt", "private")

    assert stat.S_IMODE(os.stat(store.root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.root / "deep").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.root / "deep/nested").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.root / "deep/nested/note.txt").st_mode) == 0o600


def test_a_shared_root_is_refused_not_repaired(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)

    with pytest.raises(ArtifactSecurityError) as caught:
        SecureArtifactStore(shared)

    assert str(shared) in str(caught.value)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755, "it was repaired anyway"


def test_writing_over_a_widened_artifact_is_refused(store):
    store.write_text("note.txt", "first")
    os.chmod(store.root / "note.txt", 0o666)

    with pytest.raises(ArtifactSecurityError):
        store.write_text("note.txt", "second")


# -- durability ------------------------------------------------------------

def test_the_bytes_are_fsynced_before_the_rename(store, monkeypatch):
    """Ordering is the whole guarantee: a rename made durable before its
    contents leaves a valid name pointing at nothing after a power loss."""
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: order.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(
        os, "replace", lambda a, b: order.append("replace") or real_replace(a, b)
    )

    store.write_text("durable.txt", "x")

    assert order[0] == "fsync"
    assert order.count("replace") == 1
    assert order[-1] == "fsync", "the directory entry was not made durable"


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_a_failed_write_leaves_no_artifact_and_no_residue(store, monkeypatch, failing):
    """A partial write must not be readable under the real name, and must not
    leave debris that a later reader could mistake for content."""
    def boom(*_args, **_kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(os, failing, boom)

    with pytest.raises(OSError):
        store.write_text("victim.txt", "half")

    assert not (store.root / "victim.txt").exists()
    assert contents(store.root) == []


def test_an_interrupted_write_never_exposes_partial_bytes(store, monkeypatch):
    """The previous content stays readable until the replace lands."""
    store.write_text("note.txt", "committed")

    real_replace = os.replace

    def fail_before_replace(*_args, **_kwargs):
        assert (store.root / "note.txt").read_text() == "committed"
        raise OSError("crash")

    monkeypatch.setattr(os, "replace", fail_before_replace)
    with pytest.raises(OSError):
        store.write_text("note.txt", "replacement")
    monkeypatch.setattr(os, "replace", real_replace)

    assert (store.root / "note.txt").read_text() == "committed"
    assert contents(store.root) == ["note.txt"]


# -- the namespace lock ----------------------------------------------------

BUNDLE = NamespaceLayout(
    required_files=frozenset({"prompt.md"}),
    optional_files=frozenset({"inputs.json"}),
    allowed_directories=frozenset({"references"}),
    indexed_files=(IndexedFileRule("chunk-", ".json", 1, 3),),
)

HOLD_THE_LOCK = """
import sys
from agent_core.runtime import SecureArtifactStore

store = SecureArtifactStore(sys.argv[1])
# The lock itself is what this child exists to hold, so it reaches for it
# directly rather than racing a real write and hoping the timing lands.
with store._namespace_lock(sys.argv[2]):
    (store.root / sys.argv[2] / sys.argv[3]).write_bytes(b"")
    print("held", flush=True)
    sys.stdin.read()
"""


def _holder(root, namespace, residue):
    """A separate process holding ``namespace``, with residue already written."""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLD_THE_LOCK, str(root), namespace, residue],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "held", "the holder never started"
    return proc


def test_arbitrarily_many_namespaces_leave_exactly_256_lock_files(store):
    """The reason the lock set is striped rather than per-namespace: a lock
    file per namespace grows without bound, and deleting one is itself a race."""
    for index in range(300):
        store.write_text(f"run/{index}/note.txt", "x")

    assert len(list((store.root / ".locks").iterdir())) == 256


def test_the_lock_set_is_owner_only(store):
    for path in (store.root / ".locks").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_lock_file_that_is_not_a_private_file_is_refused(private_root):
    """A symlinked lock file is a lock that does not lock, and every guarantee
    above it is then decoration."""
    root = private_root / "artifacts"
    SecureArtifactStore(root)
    slot = root / ".locks" / "00.lock"
    slot.unlink()
    slot.symlink_to(private_root / "elsewhere")

    with pytest.raises(ArtifactSecurityError):
        SecureArtifactStore(root)


def test_the_lock_is_reentrant_within_one_thread(store):
    """A multi-file commit takes the namespace once and writes several files
    under it. Without re-entrancy that is a self-deadlock, not an error."""
    with store._namespace_lock("run/1"):
        store.write_text("run/1/a.txt", "a")
        store.write_text("run/1/b.txt", "b")

    assert (store.root / "run/1/b.txt").read_text() == "b"


def test_another_thread_is_excluded(store):
    """`flock` alone would grant both threads the lock: the kernel arbitrates
    between open descriptions, and each thread opens its own."""
    refused = []
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with store._namespace_lock("run/1"):
            holding.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert holding.wait(5)
        with pytest.raises(ArtifactLockedError):
            with store._namespace_lock("run/1", blocking=False):
                refused.append("entered")
    finally:
        release.set()
        worker.join(5)

    assert refused == []


def test_deletion_refuses_a_namespace_another_process_is_writing(store):
    """Retention that waits behind an active writer holds a lock while someone
    produces the very artifacts it came to remove."""
    store.write_text("run/1/prompt.md", "p")
    store.write_text("run/1/chunk-0.json", "{}")
    holder = _holder(store.root, "run/1", "ignored.tmp")
    try:
        with pytest.raises(ArtifactLockedError):
            store.delete_namespace("run/1", BUNDLE)
    finally:
        holder.kill()
        holder.wait(5)

    assert (store.root / "run/1/prompt.md").exists()


def test_a_killed_writer_releases_the_lock_and_leaves_recoverable_residue(store):
    """Process death is the case advisory locks handle for free — and the
    partial file it leaves is the crash residue deletion knows how to clear."""
    store.write_text("run/1/prompt.md", "p")
    store.write_text("run/1/chunk-0.json", "{}")
    residue = "prompt.md." + "0" * 32 + ".partial"
    holder = _holder(store.root, "run/1", residue)
    holder.kill()
    holder.wait(5)

    assert (store.root / "run/1" / residue).exists(), "no residue to recover"
    assert store.delete_namespace("run/1", BUNDLE) == 3
    assert not (store.root / "run/1").exists()


# -- exact deletion --------------------------------------------------------

def _bundle(store, namespace="run/1"):
    store.write_text(f"{namespace}/prompt.md", "p")
    store.write_json(f"{namespace}/inputs.json", {"a": 1})
    store.write_text(f"{namespace}/chunk-0.json", "{}")
    store.write_text(f"{namespace}/references/ref.txt", "r")


def test_a_namespace_matching_its_layout_is_deleted_exactly(store):
    _bundle(store)
    _bundle(store, "run/2")

    assert store.delete_namespace("run/1", BUNDLE) == 4
    assert not (store.root / "run/1").exists()
    assert (store.root / "run/2/prompt.md").exists(), "a sibling was deleted"


def test_deleting_a_namespace_that_is_already_gone_is_not_an_error(store):
    """Retention runs more than once; the second run is not a failure."""
    assert store.delete_namespace("run/never", BUNDLE) == 0


@pytest.mark.parametrize(
    "residue, expected",
    [
        ("prompt.md." + "0" * 32 + ".partial", None),   # this store's own
        ("prompt.md.partial", ArtifactLayoutError),     # no uuid segment
        ("prompt.md.xyz.partial", ArtifactLayoutError), # not a uuid
        ("gone.md." + "0" * 32 + ".partial", ArtifactLayoutError),  # unknown dest
    ],
)
def test_only_residue_this_store_could_have_written_is_recoverable(
    store, residue, expected
):
    """Anything else is a file someone put there, and deleting it would make
    this retention job the thing that lost it."""
    _bundle(store)
    (store.root / "run/1" / residue).write_bytes(b"")

    if expected is None:
        assert store.delete_namespace("run/1", BUNDLE) == 5
    else:
        with pytest.raises(expected):
            store.delete_namespace("run/1", BUNDLE)
        assert (store.root / "run/1/prompt.md").exists(), "a partial delete happened"


def test_an_unknown_file_stops_the_whole_deletion(store):
    _bundle(store)
    (store.root / "run/1/unexpected.txt").write_text("someone else's")

    with pytest.raises(ArtifactLayoutError, match="not in the layout"):
        store.delete_namespace("run/1", BUNDLE)
    assert (store.root / "run/1/prompt.md").exists()


def test_a_missing_required_file_stops_the_deletion(store):
    """The layout no longer describes what is there, so the caller's model of
    the namespace is wrong -- and a delete under a wrong model is the danger."""
    _bundle(store)
    (store.root / "run/1/prompt.md").unlink()

    with pytest.raises(ArtifactLayoutError, match="missing required"):
        store.delete_namespace("run/1", BUNDLE)
    assert (store.root / "run/1/inputs.json").exists()


@pytest.mark.parametrize("chunks", [0, 4])
def test_an_indexed_family_outside_its_bounds_stops_the_deletion(store, chunks):
    store.write_text("run/1/prompt.md", "p")
    for index in range(chunks):
        store.write_text(f"run/1/chunk-{index}.json", "{}")

    with pytest.raises(ArtifactLayoutError, match="outside"):
        store.delete_namespace("run/1", BUNDLE)


def test_an_unlisted_directory_stops_the_deletion(store):
    _bundle(store)
    (store.root / "run/1/extra").mkdir(mode=0o700)

    with pytest.raises(ArtifactLayoutError, match="not in the layout"):
        store.delete_namespace("run/1", BUNDLE)


def test_an_allowed_directory_may_nest(store):
    """The caller declared this subtree theirs, so depth is not the risk — and
    references are grouped by kind, so forcing them flat would change paths
    already quoted in stored prompts."""
    _bundle(store)
    (store.root / "run/1/references/personas").mkdir(mode=0o700)
    (store.root / "run/1/references/personas/backend.md").write_text("persona")

    assert store.delete_namespace("run/1", BUNDLE) == 5
    assert not (store.root / "run/1").exists()


def test_a_symlink_nested_under_an_allowed_directory_stops_the_deletion(
    store, tmp_path
):
    """Depth is not the risk; following a link out of the namespace is."""
    outside = tmp_path / "precious.txt"
    outside.write_text("keep me")
    _bundle(store)
    (store.root / "run/1/references/personas").mkdir(mode=0o700)
    (store.root / "run/1/references/personas/link.md").symlink_to(outside)

    with pytest.raises(ArtifactSecurityError, match="symlink"):
        store.delete_namespace("run/1", BUNDLE)
    assert outside.read_text() == "keep me"


def test_a_symlink_anywhere_in_the_namespace_stops_the_deletion(store, tmp_path):
    """Following it would delete whatever it points at."""
    outside = tmp_path / "precious.txt"
    outside.write_text("keep me")
    _bundle(store)
    (store.root / "run/1/link.txt").symlink_to(outside)

    with pytest.raises(ArtifactSecurityError, match="symlink"):
        store.delete_namespace("run/1", BUNDLE)
    assert outside.read_text() == "keep me"


@pytest.mark.parametrize("namespace", ["../outside", ".locks", "/etc", "a\\b"])
def test_deletion_validates_the_namespace_like_any_other_path(store, namespace):
    with pytest.raises(ArtifactSecurityError):
        store.delete_namespace(namespace, BUNDLE)
