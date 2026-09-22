"""Starting or resuming a durable workflow, exactly once.

Supplying the same ``thread_id`` is **not** enough to resume. On LangGraph
1.x, invoking a pending lineage with a fresh state mapping starts again at the
entry node, and invoking a completed one with a mapping re-enters it. Only a
pending invocation with ``None`` continues from the stored next node. That was
measured, not assumed, and it is why a product must never call a durable graph
directly.

Five cases:

1. **absent** — no checkpoint tuple and no stored values: invoke with the
   initial state; if the snapshot is then interrupted, report ``suspended``,
   otherwise ``started``;
2. **pending, not interrupted** — invoke with ``None``, report ``resumed``
   (crash recovery);
3. **pending, interrupted, no resume_value** — return ``suspended`` without
   invoking;
4. **pending, interrupted, resume_value set** — invoke
   ``Command(resume=resume_value)``; interrupted again is ``suspended``,
   otherwise ``resumed``;
5. **completed** — return stored values without invoking, ``reused_completed``;
6. **corrupt** — raise.

## Why corruption is never absence

The tempting fifth case is "if the checkpoint looks broken, start fresh". It
is wrong in the expensive direction. A corrupt checkpoint for a run that
already finished would repeat every model turn it paid for, and — worse —
could overwrite a result the product has already committed and reported. A
clean rerun is a deliberate act: mint a new ``workflow_run_id``. Silence here
would turn a storage fault into duplicated spend and a changed answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from agent_core.workflow.checkpoints import WorkflowRunIdentity

logger = logging.getLogger(__name__)


class WorkflowCheckpointError(RuntimeError):
    """A lineage exists but could not be trusted.

    Carries the thread id because that is what an operator needs to find or
    drop it, and never the underlying error's payload -- a deserialization
    failure can quote checkpoint contents, which is task data.
    """

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        super().__init__(f"workflow checkpoint is unusable for {thread_id}")


class DurableWorkflow(Protocol):
    """The slice of a compiled LangGraph graph this needs.

    A structural type rather than `CompiledStateGraph` itself, for two reasons:
    it keeps this module free of a hard langgraph import, and it lets the four
    dispositions be tested against snapshots a real graph will not produce on
    demand — a corrupt one, most of all. Nothing else is implied: the object a
    product passes is the compiled graph, built with the saver
    `open_checkpointer` yielded, and no wrapper type appears in any signature
    here or in `checkpoints`.
    """

    def get_state(self, config: Mapping[str, Any]) -> Any: ...
    def invoke(self, state: Any, config: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class WorkflowInvocationResult:
    """What happened, and the state that resulted.

    ``disposition`` is reported rather than logged because a product needs it:
    a resumed-vs-started counter is the only way to tell a working checkpointer
    from one that silently never resumes.
    """

    disposition: str  # "started" | "resumed" | "reused_completed" | "suspended"
    state: Mapping[str, Any]


def is_interrupted(snapshot: Any) -> bool:
    """True when the snapshot is sitting on a LangGraph interrupt (a human gate)."""
    if snapshot is None:
        return False
    for task in getattr(snapshot, "tasks", ()) or ():
        if getattr(task, "interrupts", None):
            return True
    values = getattr(snapshot, "values", None) or {}
    return isinstance(values, Mapping) and bool(values.get("__interrupt__"))


def _is_absent(snapshot: Any) -> bool:
    """No lineage at all -- narrowly.

    Both conditions, deliberately: a snapshot with values but no next nodes is
    a *completed* run, and treating it as absent would re-run it.
    """
    if snapshot is None:
        return True
    values = getattr(snapshot, "values", None)
    next_nodes = getattr(snapshot, "next", None)
    return not values and not next_nodes


def _validate(snapshot: Any, identity: WorkflowRunIdentity) -> None:
    """Reject a snapshot that exists but cannot be acted on."""
    if not hasattr(snapshot, "values"):
        raise WorkflowCheckpointError(identity.thread_id)
    if not hasattr(snapshot, "next"):
        # A pending snapshot with no next-node sequence cannot be resumed and
        # is not complete either. There is no safe interpretation.
        raise WorkflowCheckpointError(identity.thread_id)


def _classify_after_invoke(
    graph: DurableWorkflow,
    config: Mapping[str, Any],
    invoked: Any,
    kind: str,
) -> WorkflowInvocationResult:
    """Interrupted snapshots are `suspended`; otherwise keep `kind` and invoke output.

    Classification uses ``get_state``, not the invoke return dict. The invoke
    output is still the result ``state`` when not interrupted, so contract
    fixtures that do not update ``get_state`` after ``invoke`` keep working.
    """
    try:
        snapshot = graph.get_state(config)
    except Exception:  # noqa: BLE001
        snapshot = None
    if is_interrupted(snapshot):
        values = getattr(snapshot, "values", None) or {}
        return WorkflowInvocationResult("suspended", values)
    return WorkflowInvocationResult(kind, invoked if invoked is not None else {})


def invoke_workflow(
    graph: DurableWorkflow,
    *,
    identity: WorkflowRunIdentity,
    initial_state: Mapping[str, Any],
    recursion_limit: int,
    resume_value: Any = None,
) -> WorkflowInvocationResult:
    """Start or resume ``graph`` under ``identity``, doing the work once."""
    config = identity.invoke_config(recursion_limit=recursion_limit)

    try:
        snapshot = graph.get_state(config)
    except Exception as exc:  # noqa: BLE001 - never interpreted as absence
        logger.warning("checkpoint unreadable for %s: %s", identity.thread_id, exc)
        raise WorkflowCheckpointError(identity.thread_id) from exc

    if _is_absent(snapshot):
        logger.info("starting %s", identity.thread_id)
        invoked = graph.invoke(initial_state, config)
        return _classify_after_invoke(graph, config, invoked, "started")

    try:
        _validate(snapshot, identity)
    except WorkflowCheckpointError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise WorkflowCheckpointError(identity.thread_id) from exc

    if snapshot.next:
        if is_interrupted(snapshot) and resume_value is None:
            logger.info("suspended %s at %s", identity.thread_id, snapshot.next)
            return WorkflowInvocationResult("suspended", snapshot.values or {})
        if is_interrupted(snapshot):
            logger.info("resuming gate %s at %s", identity.thread_id, snapshot.next)
            from langgraph.types import Command

            invoked = graph.invoke(Command(resume=resume_value), config)
            return _classify_after_invoke(graph, config, invoked, "resumed")
        logger.info("resuming %s at %s", identity.thread_id, snapshot.next)
        # `None`, not the initial state: a mapping restarts at the entry node.
        invoked = graph.invoke(None, config)
        return _classify_after_invoke(graph, config, invoked, "resumed")

    logger.info("reusing completed %s", identity.thread_id)
    return WorkflowInvocationResult("reused_completed", snapshot.values)
