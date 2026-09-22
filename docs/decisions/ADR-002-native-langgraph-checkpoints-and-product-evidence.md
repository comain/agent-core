# ADR-002: Use Native LangGraph Checkpointers and Separate Product Evidence

## Status

Accepted

## Date

2026-08-20

## Context

Agent-core currently wraps LangGraph's SQLite saver in a subclass that manually
forwards the saver interface so products can also call a custom lineage-deletion
method. The wrapper is passed to `graph.compile`, even though the wrapped native
saver already satisfies LangGraph's required base class.

UTA also persists workflow-operation rows and immutable result artifacts. Those
are sometimes mistaken for a second checkpoint system. They exist because a
LangGraph checkpoint cannot atomically contain repository edits, provider
charges, filesystem artifacts, or UTA product-database transitions.

The durable contract must prevent repeated paid work without copying a
third-party saver API or pretending graph state is product truth.

## Decision

Agent-core will yield the native LangGraph saver from its secure resource-owning
`open_checkpointer` context manager and pass that object directly to graph
compilation.

Agent-core will retain:

- `WorkflowRunIdentity` and its stable versioned thread ID;
- secure path creation and permission verification;
- deterministic saver setup and close behavior;
- `invoke_workflow` and its absent/pending/completed/corrupt classification;
- a separate `delete_checkpoint_lineage(saver, identity)` helper using the
  saver's public deletion capability.

Agent-core will remove the forwarding saver subclass and private-table SQL
fallback. Checkpoint roots use the same forbidden-root/ancestor/symlink checks as
private artifacts, and existing broad parents are not chmodded. A saver without
public lineage deletion, including an inherited method raising
`NotImplementedError`, fails explicitly.

UTA will retain its operation ledger and product evidence. Generic artifact
bytes may use agent-core storage, but operation state, fingerprints,
reconciliation, accounting policy, terminal validation, and retention eligibility
remain UTA-owned.

## Alternatives Considered

### Keep the forwarding subclass

- Advantage: current `checkpointer.delete(identity)` call remains unchanged.
- Rejected: it mirrors an evolving third-party interface, already requires many
  forwarding methods, and makes agent-core appear to implement checkpointing.

### Generate forwarding methods automatically

- Advantage: less handwritten boilerplate.
- Rejected: still creates a parallel interface and hides compatibility failures
  until runtime.

### Store product effects entirely in graph state

- Advantage: one apparent persistence system.
- Rejected: Git/files/provider charges/product transactions cannot be atomically
  committed with the checkpoint, and large/private artifacts should not be graph
  state.

### Remove UTA operation evidence

- Advantage: smaller UTA codebase.
- Rejected: a crash after a paid repository-changing turn but before node return
  would permit blind replay and duplicate or overwrite work.

## Consequences

- LangGraph remains the sole graph-checkpoint engine and schema owner.
- Agent-core maintains only the behavior products need around it.
- UTA retention changes from `checkpointer.delete(identity)` to the shared helper.
- Unsupported deletion is visible rather than relying on private table names.
- Product operation evidence remains a complementary consistency boundary.
- A coordinated breaking 0.7 rollout is required: consumers first pin 0.6.11,
  then migrate as a source+released-core pair.

## Links

- Spec: `docs/spec-workflow-persistence-and-common-capabilities.md`
- Overview: `docs/design-workflow-persistence-and-common-capabilities.md`
