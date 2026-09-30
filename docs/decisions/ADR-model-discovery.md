# Shared Model Admission And Fresh Resolution

Status: proposed, pending design approval.

## Context

Manual provider chains cannot discover new coding-capable models. A shared
agent runtime must not impose one application's policy on another. Resumed tasks
should benefit from current models without forgetting known availability failures.

## Decision

Use an agent-core pure policy resolver over cached provider and Artificial
Analysis data. Default coding threshold is 70. Resolve score-descending model/
effort bindings each invocation/resume, retaining only an in-memory list during
execution. Persist availability by scoped model identity, not task-list index.
Consumers supply policies and
storage; harness adapters own provider execution details. Keep manual mode for
compatibility and rollback. No DeepSWE fallback.

## Consequences

No added network latency during task invocation. Unknown identities/capabilities
need explicit approval; automatic discovery is not automatic trust. A refresh
job and bounded stale-cache policy are operational requirements. No task snapshot
migration is required. A running LLM call is not switched on catalog refresh.

## Alternatives

Rejected live queries per invocation, maximum score across configurations,
per-application selector implementations, and opaque model-name fuzzy matching.
Per-task frozen catalogs were initially proposed but removed by user request
on 2026-09-07 to simplify resume and automatically adopt fresh lists.
