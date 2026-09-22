# agent-core 0.7.0 — breaking release notes

Status: **approved for release on 2026-08-24.** The user approved the API diff
and removal list and authorized the coordinated UTA migration.

0.7.0 is a coordinated breaking release. No old-consumer/new-core combination
is supported: UTA and cr_plugin stay pinned to `0.6.11` until each begins its
own migration.

## Why it breaks

Every removal below is a case where the old shape could not express something a
product needed to know, and the resulting ambiguity was being resolved by
guessing:

| Old shape | What it could not say |
| --- | --- |
| `WorkflowCheckpointer` | — (it worked; it cost hand-forwarding every method of an evolving third-party protocol, where a missed method fails deep inside a graph run) |
| `AgentTurnResult.session_id` | Which conversations a fallback chain used. Only the last one could be named, so the candidates that were opened, paid for and abandoned were impossible to attribute. |
| One `cost_gate` per operation | That a chain of three submissions cost three times. Admission ran once, before the guard, and the total arrived after everything was paid for. |
| Callback-map turn context | Which lifecycle a product had actually opted into. A missing key was silently "no guard, no accounting, no durability". |
| `GitPublishResult` / `None` | The difference between "the product had nothing to say" and "this was already published and the merge request is missing". |
| `ArtifactStore` | Nothing — it simply did not do what it claimed: `".." in parts`, non-atomic writes, umask permissions. |

## Removals

- `agent_core.workflow.checkpoints.WorkflowCheckpointer` — `open_checkpointer`
  yields LangGraph's own saver. Lineage deletion moves to
  `delete_checkpoint_lineage(saver, identity)`, which uses the saver's public
  capability and never writes LangGraph's tables.
- `agent_core.workflow.nodes.ResultPersistenceError` — replaced by
  `agent_core.harness.execution.ResultCommitError`, re-exported from
  `agent_core.workflow`.
- The `agent_turn` callback-map lifecycle: `before_turn`, `after_turn`,
  `cost_gate`, `on_cost`, `on_result`, `open_session`, `progress_sink`,
  `recorder`. Normalized mode now requires `context["turn_context"]`, one typed
  `AgentTurnContext`. **Legacy mode is unchanged** and still needs only
  `context["runner"]`.
- `AgentTurnResult.session_id` — replaced by the ordered
  `session_refs: tuple[AgentSessionRef, ...]`.
- `agent_core.runtime.ArtifactStore` — replaced by `SecureArtifactStore`.
- `agent_core.git.GitPublishResult` — `publish` returns the closed
  `GitPublicationOutcome` union.

## Signature changes

- `open_checkpointer(path, *, forbidden_roots)` — the keyword is required.
  Putting workflow state inside the repository being edited should be a
  decision, not an oversight.
- `AgentTurnResult.as_dict()` is now required for serialization;
  `dataclasses.asdict()` leaves `SessionLocatorScope` members in place and fails
  at the checkpoint that tries to store them.
- `GitPublishRequest` gains `base_branch` and `publication_id`.

## Additions

`delete_checkpoint_lineage`, `CheckpointCapabilityError`, `SecureArtifactStore`,
`StoredArtifact`, `NamespaceLayout`, `IndexedFileRule` and the artifact errors;
`AgentSessionRef`, `SessionLocatorScope`, `merge_session_refs`; `PaidAttempts`,
`TurnCostPort`, `AttemptNotAdmitted`, `aggregate_turn_costs`;
`execute_agent_turn` with `HarnessBinding`, `AgentTurnRequest`,
`AgentTurnContext`, `AgentTurnExecutionResult` and its eight ports; the
diagnostics contracts with `diagnose_sessions` and
`OpenCodeSessionDiagnostics`; `PromptReference`, `PromptBundle`,
`render_bundle`, `verify_bundle`; `PublicationCoordinator`,
`MergeRequestSpec`, `MergeRequestStatus`, `ensure_merge_request`.

## Operational consequences

- **Stronger path rules can reject a root that used to work.** Checkpoint and
  artifact roots must be owner-only and free of symlinks, and a shared or
  symlinked one is now refused rather than chmodded — the window in which it
  was readable has already happened. The error names the exact path and the
  `chmod` that repairs it.
- **Cost ports are called far more often**: once per provider submission rather
  than once per operation. A product whose `on_cost` wrote a row per operation
  now writes one per attempt.
- **`REUSED_PUBLISHED` is a new outcome** a caller must handle. Treating it as a
  failure would strand a pushed branch; treating it as `NO_CHANGES` reintroduces
  the bug it was added to fix.

## Dependency bounds

`langgraph>=1.0,<2.0`, `langgraph-checkpoint-sqlite>=2.0,<4.0`. Package import
without the `langgraph` extra remains supported.

## Release checklist

- [x] Full suite green.
- [x] Source-boundary scans: no removed symbol, no LangGraph table SQL, no
      callback-map key.
- [x] `compileall`, wheel and sdist build, clean-environment install and import
      with and without extras.
- [x] **Human approves this diff and removal list.**
- [x] Tag `0.7.0` and push.
- [x] Consumers remain pinned to `0.6.11` until their migration begins.
