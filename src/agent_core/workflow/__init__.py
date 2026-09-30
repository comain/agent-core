"""Configurable workflows: a product describes its pipeline, agent-core runs it.

The aim is that a new consumer defines *what* its workflow is in configuration
and implements only the steps that are genuinely its own. Preparing a git
workspace, rendering a prompt and running an agent turn are the same everywhere
and ship here.

`graph` requires the optional langgraph extra and is not imported eagerly, so
importing this package stays cheap for consumers that only want the registry.
"""

from agent_core.workflow.registry import (
    NodeRegistry,
    UnknownNodeError,
    shared_registry,
)
# Importing this module is what registers the shared nodes into
# `shared_registry`. Without it, `shared_registry` imports fine and is empty --
# and the failure surfaces as "no node registered as 'prepare_workspace'" at
# graph build time, far from the missing import.
from agent_core.workflow import nodes as _nodes  # noqa: F401
# Checkpointing needs the langgraph extra, so it is imported lazily by
# `open_checkpointer` rather than here -- importing this package must stay
# cheap for a consumer that only wants the registry.
# Owned by the harness: a product running turns directly needs the neutral
# result too, and should not have to import the workflow package for it.
from agent_core.harness.turn_result import (
    AgentTurnResult,
    normalize_turn_outcome,
)
from agent_core.workflow.spec import (
    END,
    BranchSpec,
    FanoutSpec,
    NodeSpec,
    WorkflowSpec,
    WorkflowSpecError,
)

__all__ = [
    "AgentTurnResult",
    "normalize_turn_outcome",
    "ResultCommitError",
    "invoke_workflow",
    "WorkflowInvocationResult",
    "WorkflowCheckpointError",
    "is_interrupted",
    "WorkflowRunIdentity",
    "UnsafeCheckpointPathError",
    "CheckpointCapabilityError",
    "delete_checkpoint_lineage",
    "NodeRegistry",
    "UnknownNodeError",
    "shared_registry",
    "WorkflowSpec",
    "WorkflowSpecError",
    "NodeSpec",
    "BranchSpec",
    "FanoutSpec",
    "END",
]


def __getattr__(name):
    """Expose the checkpoint API without importing langgraph at package import."""
    if name in {
        "WorkflowRunIdentity",
        "open_checkpointer",
        "delete_checkpoint_lineage",
        "UnsafeCheckpointPathError",
        "CheckpointCapabilityError",
    }:
        from agent_core.workflow import checkpoints

        return getattr(checkpoints, name)
    if name == "ResultCommitError":
        # 0.7: the durability error belongs to the executor that raises it.
        # `ResultPersistenceError` is gone with the callback-map lifecycle.
        from agent_core.harness.execution import ResultCommitError

        return ResultCommitError
    if name in {
        "invoke_workflow",
        "WorkflowInvocationResult",
        "WorkflowCheckpointError",
        "is_interrupted",
    }:
        from agent_core.workflow import execution

        return getattr(execution, name)
    raise AttributeError(name)
