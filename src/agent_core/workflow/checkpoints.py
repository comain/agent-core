"""Resuming a workflow after the process that ran it is gone.

A long workflow that dies halfway — a redeployed worker, an operator's
restart, a crash — should not repeat the expensive part. LangGraph can persist
graph position; what belongs here is the boundary around it.

Three things are deliberate.

**The saver is LangGraph's own object.** A product passes a path and receives
the concrete saver, which it hands straight to `compile(checkpointer=...)`.
This module owns the path, the permissions and the connection's lifetime, and
nothing else. An earlier revision wrapped the saver in a `WorkflowCheckpointer`
subclass so the backend could be swapped without touching a consumer; that
bought a hypothetical and cost a real thing — every method of an evolving
third-party protocol had to be forwarded by hand, and a method missed there
fails deep inside a graph run rather than at the boundary. Backend choice is
not exposed in this iteration.

**Checkpoint state is not product truth.** The product database stays
authoritative for task status, attempts, artifacts, results and everything a
user sees. This owns only serializable graph state, the next node to run, and
interrupts. Deleting the checkpoint file costs repeated work; it never costs
correctness.

**The file is owner-only, and a broad path is refused rather than repaired.**
Graph state is task data. The directory is created `0700` and the database
`0600`; an existing symlink, a group-readable parent, or a path overlapping a
repository the agent edits is an error naming the exact path, not something
this quietly fixes. See `agent_core.paths` for where that boundary sits.

## Why the version is in the thread id

`checkpoint_ns` looks like the natural home for a topology version. It is not:
LangGraph resolves it as a **subgraph path**, so
``checkpoint_ns="generation-cycle:v1"`` makes ``get_state`` raise
``ValueError: Subgraph generation-cycle not found``. The version therefore
lives in the thread id, where it does what was wanted — an incompatible
topology gets a distinct lineage instead of resuming into nodes that moved.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Sequence, Union

from agent_core.paths import (
    PRIVATE_DIR_MODE,
    PRIVATE_FILE_MODE,
    UnsafePathError,
    assert_no_forbidden_overlap,
    assert_private_file,
    ensure_private_directory,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only, never at runtime
    from langgraph.checkpoint.base import BaseCheckpointSaver

try:  # pragma: no cover - depends on the install
    from langgraph.checkpoint.sqlite import SqliteSaver as _saver_factory
except ImportError:  # pragma: no cover
    _saver_factory = None


class UnsafeCheckpointPathError(UnsafePathError):
    """The checkpoint path is a symlink, shared, or overlaps a forbidden root."""


class CheckpointCapabilityError(RuntimeError):
    """The saver cannot do what was asked, and nothing was done.

    Raised instead of reaching around the saver. The alternative — deleting
    rows from `checkpoints` and `writes` through the connection — is what the
    previous revision did, and it is a schema we do not own: LangGraph is free
    to rename those tables in a minor release, at which point retention starts
    silently deleting nothing while reporting success.
    """


@dataclass(frozen=True)
class WorkflowRunIdentity:
    """Everything needed to resume one workflow run, and nothing else.

    Frozen because it is a key: a mutated identity resumes a different lineage
    than the caller believes, and nothing would report that.

    ``unit_id`` is whatever the product iterates over — a batch of classes, a
    single target, a file. One lineage per unit is what makes per-unit resume
    meaningful; one per task would restart every unit together.
    """

    product: str
    task_id: str
    unit_id: str
    workflow_run_id: str
    cycle: str
    version: str

    @property
    def thread_id(self) -> str:
        return (
            f"{self.product}:{self.cycle}:{self.version}"
            f":{self.task_id}:{self.unit_id}:{self.workflow_run_id}"
        )

    def invoke_config(self, *, recursion_limit: int) -> Mapping[str, Any]:
        """The config LangGraph wants, so no product builds it by hand.

        Products should not have to know that the thread id goes under
        ``configurable`` while the recursion limit does not — two call sites
        spelling that differently is how a resume quietly starts a new lineage,
        or a cyclic graph silently keeps LangGraph's default limit.
        """
        return {
            "configurable": {"thread_id": self.thread_id},
            "recursion_limit": int(recursion_limit),
        }


@contextmanager
def open_checkpointer(
    path: Union[str, Path],
    *,
    forbidden_roots: Sequence[Union[str, Path]],
) -> Iterator["BaseCheckpointSaver"]:
    """Yield LangGraph's saver for ``path``, closing it on exit.

    A context manager rather than a factory because the saver holds a SQLite
    connection. A daemon that forgets to close one leaks a file handle per run
    and fails much later, somewhere unrelated.

    ``forbidden_roots`` has no default. The overwhelmingly common mistake is
    putting workflow state inside the repository the agent is editing, where a
    workspace reset deletes it or the agent commits it; a keyword the caller
    must answer makes that a decision instead of an oversight. Passing ``()``
    is a legitimate answer for a store with nothing to overlap.
    """
    if _saver_factory is None:
        raise ImportError(
            "workflow checkpointing needs the langgraph extra: "
            "pip install 'agent-core[langgraph]'"
        )

    target = Path(path).expanduser()
    # Validate before creating anything: a path this refuses should leave no
    # directory behind for the next run to inherit as "already there".
    assert_no_forbidden_overlap(
        target, forbidden_roots, error=UnsafeCheckpointPathError
    )
    assert_private_file(
        target, file_mode=PRIVATE_FILE_MODE, error=UnsafeCheckpointPathError
    )
    ensure_private_directory(
        target.parent,
        mode=PRIVATE_DIR_MODE,
        forbidden_roots=forbidden_roots,
        error=UnsafeCheckpointPathError,
    )

    with _saver_factory.from_conn_string(str(target)) as saver:
        saver.setup()
        if target.exists():
            # SQLite creates the database with the umask's mode. Narrow it
            # immediately, then re-check: this is the one path the application
            # provably owns, so tightening it is repair of our own artifact
            # rather than silently fixing someone else's directory.
            os.chmod(target, PRIVATE_FILE_MODE)
            assert_private_file(
                target, file_mode=PRIVATE_FILE_MODE, error=UnsafeCheckpointPathError
            )
        logger.debug("checkpointer open at %s", target)
        yield saver


def delete_checkpoint_lineage(
    saver: "BaseCheckpointSaver",
    identity: WorkflowRunIdentity,
) -> None:
    """Drop one lineage, so retention does not mean writing LangGraph's SQL.

    Deletes only ``identity``'s thread. Other lineages in the same database —
    other units of the same task, other tasks entirely — are untouched, which
    is what makes per-unit retention possible at all.

    A saver that cannot do this raises `CheckpointCapabilityError` and leaves
    everything intact. Note that *having* the method proves nothing:
    `BaseCheckpointSaver.delete_thread` exists and raises `NotImplementedError`,
    so a saver that never overrode it presents a perfectly callable attribute.
    The capability is therefore decided by calling it, not by `hasattr`.
    """
    deleter = getattr(saver, "delete_thread", None)
    if not callable(deleter):
        raise CheckpointCapabilityError(
            f"{type(saver).__name__} cannot delete a lineage; "
            f"{identity.thread_id} is unchanged"
        )
    try:
        deleter(identity.thread_id)
    except NotImplementedError as exc:
        raise CheckpointCapabilityError(
            f"{type(saver).__name__} inherits an unimplemented delete_thread; "
            f"{identity.thread_id} is unchanged"
        ) from exc
