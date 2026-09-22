# ADR-003: Use a Typed Agent-Turn Execution Context

## Status

Accepted

## Date

2026-08-20

## Context

The shared `agent_turn` node currently discovers its runner, cancellation,
session factory, workspace guards, progress sink, cost gate/sink, result sink,
and attempt recorder through string keys in a generic mapping. Misspellings and
missing required capabilities fail only when a node reaches the corresponding
branch. CR's reviewer, judge, feedback, and retrospective paths call
`run_harness_node` directly rather than using that LangGraph node. UTA and CR
therefore need one lifecycle that supports both graph projection and direct
parse/accept callers.

The products must continue to own task-state interpretation and persistence, but
the shared node needs an agent-agnostic, validated contract.

## Decision

Agent-core will define `execute_agent_turn(request, context)` as the canonical
lifecycle. It delegates attempt, parse, acceptance, retry, and recovery to the
existing `run_harness_node`; it does not create a second loop. LangGraph
`agent_turn` becomes a state/config projection adapter, while CR direct nodes call
the same executor with their existing callbacks.

The executor returns `AgentTurnExecutionResult`, pairing the exact `NodeOutcome`
needed by direct parse/accept callers with the normalized `AgentTurnResult` needed
for durable graph projection. All normal terminal outcomes are normalized;
product-result durability occurs after guard acceptance and session cleanup,
while a guard rejection remains non-durable.

Agent-core will define frozen `HarnessBinding`, `AgentTurnRequest`, and
`AgentTurnContext` values composed from small runtime-checkable protocols. The
binding carries the configured neutral harness name explicitly. Required
capabilities are validated before the first paid call.

The context will carry the selected neutral harness binding plus optional cancellation,
session, guard, progress, cost, result, and attempt-recording ports. Product
composition code will adapt product task state and persistence to those ports.
The shared context will contain no product settings, task manager, or database
type.

Cost callbacks surround every real provider submission with a monotonic ordinal
across retry, fallback, and recovery. Recorder start remains before submission;
each terminal `TurnRecord` carries the ordered session refs for that attempt, so
products preserve crash-visible STARTED rows and atomic terminal enrichment.

An optional execution-observer port can check product deadlines/leases before
each paid attempt. Product daemon heartbeats remain independent background work;
they are not inferred from provider progress. Updated UTA and CR code
construct the typed contract directly as part of the coordinated 0.7 migration.
There is no legacy mapping adapter.

## Alternatives Considered

### Keep the mapping and add key validation

- Advantage: smallest immediate diff.
- Rejected: key names remain an implicit API, type checkers cannot validate port
  signatures, and every new capability expands the hidden contract.

### Use one large service class

- Advantage: one nominal interface.
- Rejected: optional capabilities become stubs, implementations inherit methods
  they do not own, and products tend to pass their task manager as the service.

### Product-specific context subclasses

- Advantage: products can expose all their helpers directly.
- Rejected: shared nodes would need product knowledge or `Any`, defeating the
  neutral boundary.

## Consequences

- Required capability errors move to graph/application construction.
- Port ordering and failure semantics become contract-testable.
- Products keep control of persistence and status policy.
- Direct and LangGraph consumers share the same ordered lifecycle.
- This is a breaking 0.7 API; old consumers remain pinned to 0.6.11.
- Adding a future harness does not change UTA or CR wiring beyond configuration.

## Links

- Spec: `docs/spec-workflow-persistence-and-common-capabilities.md`
- Overview: `docs/design-workflow-persistence-and-common-capabilities.md`
