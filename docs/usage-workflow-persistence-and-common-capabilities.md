# Usage: Workflow Persistence and Common Capabilities

## Status

Approved operator/developer usage contract for the design in
`docs/design-workflow-persistence-and-common-capabilities.md`. Commands and API
names become active only after implementation and release.

## Durable Workflow Usage

Products open a native LangGraph saver through agent-core and pass it directly to
graph compilation:

```python
from agent_core.workflow import (
    WorkflowRunIdentity,
    delete_checkpoint_lineage,
    invoke_workflow,
    open_checkpointer,
)

with open_checkpointer(checkpoint_path, forbidden_roots=[target_repo]) as saver:
    graph = build_graph(spec, registry=registry, context=context, checkpointer=saver)
    result = invoke_workflow(
        graph,
        identity=WorkflowRunIdentity(
            product="uta",
            task_id="42",
            unit_id="unit-1",
            workflow_run_id="run-1",
            cycle="generation-cycle",
            version="v2",
        ),
        initial_state=state,
        recursion_limit=256,
    )
```

Do not import `SqliteSaver`, write LangGraph table SQL, pass fresh state to resume
a pending lineage, or use checkpoint contents as product task truth.

Disposition meanings:

| Disposition | Meaning | Operator action |
| --- | --- | --- |
| `started` | No lineage existed; initial state ran. | None. |
| `resumed` | Pending lineage continued from stored next node. | Confirm product reconciliation did not repeat paid work. |
| `reused_completed` | Stored terminal result was returned without node execution. | Confirm terminal product evidence remains valid. |
| error/corrupt | Lineage exists but cannot be trusted. | Quarantine; use an explicit clean rerun with a new workflow-run identity. |

## Product Evidence

LangGraph checkpoints do not include repository edits, provider charges, product
database transitions, or private result artifacts atomically. A product with
side-effecting nodes must keep its own operation evidence and reconcile it before
re-entering expensive work.

UTA operators retain the current clean-rerun and retained-evidence procedures.
Never delete an operation artifact to make a checkpoint appear resumable.

### Retention is a separate operation

Only a product retention job may call `delete_checkpoint_lineage`. UTA preserves
this order: checkpoint lineage, operation artifacts, operation rows, then prompt
artifacts. If any step fails, later evidence remains. The helper maps an absent
or inherited-not-implemented saver deletion capability to
`CheckpointCapabilityError`; it never edits LangGraph tables.

## Secure Artifacts And Prompt Bundles

Private roots must:

- live outside every target source repository;
- be owned by the service user;
- use `0700` directories and `0600` files;
- contain no symlink in their managed path chain.

The secure store refuses unsafe existing paths rather than broadening or silently
repairing permissions. Repair only the exact configured application-state root:

```bash
chmod 700 /path/to/application-state/workflow-state
chmod 600 /path/to/application-state/workflow-state/checkpoints.sqlite
```

Do not recursively chmod or delete a home directory, repository, or workspace
root.

Prompt bundles contain `prompt.md`, `inputs.json`, `manifest.json`, and optional
references. The manifest is the integrity index; it contains hashes and sizes,
not prompt contents. It is written last under a namespace lock. Missing manifest
means incomplete, never trusted. A conflict under the same immutable operation identity
requires investigation or a deliberate new run identity, not overwrite.

## Typed Agent Turns

New product code constructs `HarnessBinding`, `AgentTurnRequest`, and
`AgentTurnContext`, then calls `execute_agent_turn`. LangGraph `agent_turn` is a
projection adapter over this same executor; direct CR nodes use it without losing
their parse/accept logic. There is no callback-map compatibility API in 0.7.

Required ports are validated before paid work. A missing cost, result, session,
or guard capability is a configuration error; do not replace it with a no-op to
make startup pass.

## Session Diagnostics

Sessions are identified by `AgentSessionRef(harness, locator, scope)`. Persist the
entire ordered `session_refs` tuple because fallback may use several sessions.
`scope=process` cannot support restart diagnostics; OpenCode's native DB locator
uses `scope=durable`. References validate syntax at load time and harness support
only when diagnostics is requested. Locators are opaque and never filenames.

UTA assessment retains repeated session IDs and adds harness selection:

```bash
uta assess \
  --harness opencode \
  --session-id ses_one \
  --session-id ses_two \
  --json
```

Diagnostics items are a closed `available`/`unsupported`/`unavailable` union.
Core input-limit excess raises `DiagnosticsLimitExceeded`; UTA projects it as CLI
`limit_exceeded`. None of these conditions means zero usage, and report totals are absent unless every
requested item is available. Narrow an oversized request rather than treating
partial totals as authoritative.

The compatibility `--db-path` option is accepted for one release for OpenCode
only. Configure the OpenCode diagnostics database through harness options after
that window.

## CR Configuration Compatibility

Canonical CR configuration uses:

```text
CR_AGENT_HARNESS=<registered harness>
CR_AGENT_HARNESS_OPTIONS=<opaque adapter options>
CR_AGENT_REVIEW_V2_GLOBAL_AGENT_CONCURRENCY=<positive integer>
```

Legacy `CR_AGENT_REVIEW_V2_*OPENCODE*` settings remain a product-owned one-release
reader when the selected harness is OpenCode. Neutral values win when both are
set. Warnings never include tokens or option values.

CR responses expose authoritative plural `agent_session_refs`. Existing singular
`session_id`, `source_session_id`, `parent_session_id`, `llm_session_id`, and
OpenCode aliases remain endpoint-specific compatibility projections. An optional
`agent_session` is explicitly the latest-ref convenience only; consumers that
need complete fallback history use `agent_session_refs`.

## Publication Outcomes

Shared publication distinguishes Git push from MR creation:

| Status | Meaning | Action |
| --- | --- | --- |
| `created` | Branch pushed, remote SHA verified, MR created. | None. |
| `existing` | Stable branch already has an open MR. | Reuse the returned URL. |
| `manual` | Branch pushed and verified; credentials unavailable. | Open the returned manual URL. |
| `failed` | Branch pushed and verified; MR API failed. | Use manual URL or repair MR only; do not repush automatically. |
| `not_requested` | Branch pushed and verified; no MR requested. | Product-specific follow-up. |

A remote-SHA mismatch or unexpected dirty path is a publication failure. Product
retries reuse a stable publication identity/branch; the coordinator checks for an
open MR before creation and after an ambiguous timeout.

## Rollout And Rollback

1. Pin and deploy existing consumers with agent-core 0.6.11.
2. Release the breaking agent-core 0.7.0 contract while old consumers remain
   pinned to 0.6.11.
3. Migrate and pin UTA to released 0.7.x, then canary it.
4. Migrate CR schema/source, pin 0.7.x, then canary direct turn and publication
   flows.
5. Remove persisted CR legacy-column writes only in a later approved cleanup.

Rollback always restores a mutually compatible agent-core/consumer pair. Do not
roll back agent-core alone beneath a consumer that imports the new contracts.

## Verification Commands

### agent-core

```bash
.venv/bin/python -m pytest
.venv/bin/python -m build
```

### UTA

```bash
.venv312/bin/python -m pytest
.venv312/bin/ruff check uta tests tools/python-enforcement
.venv312/bin/python scripts/check_package_dependencies.py
```

### CR

```bash
.venv/bin/python -m pytest
```

## Support Checklist

When a durable workflow behaves unexpectedly, collect only metadata:

- agent-core version;
- workflow topology version and thread ID;
- disposition (`started`, `resumed`, `reused_completed`);
- product operation ID/status and artifact digest;
- checkpoint/artifact path permissions;
- sanitized error kind;
- publication remote SHA/MR status when applicable.

Do not attach raw checkpoint values, prompts, reasoning, commands, credentials,
provider DB rows, or target source code to a public incident.

## Changelog

- 2026-08-20 — Initial usage design drafted with the cross-repo design.
- 2026-08-20 — Revision 2 separates retention usage and documents the 0.7
  coordinated cutover, canonical executor, durable session refs, manifest-last
  bundles, and idempotent publication.
