# Spec: Spec-Gen (Corbell) Agent-Core Adoption

## Status

- Phase: plan
- Approval: approved by the user on 2026-08-25 (`approe`)
- Work type: non-Jira tooling and architecture iteration
- Issue tracker: not applicable; the user confirmed this iteration is non-Jira
  work on 2026-08-25
- Canonical repository: `agent-core`
- Affected repositories: `agent-core`, `Corbell` (`spec-generator-agent`);
  `unit-test-agent` and `cr_plugin` if the agent-core release is breaking on
  APIs they already call
- Compatibility: matched pin pairs. A breaking core tag is allowed. Mixed
  old-consumer / new-core is then unsupported (same rule as `0.6.11` → `0.7.0`).
  UTA/CR may remain on last `0.7.x` until they opt in, or take a matched
  source migration in this program.
- Design documents: approved by the user on 2026-08-26
- Usage documents: required during design/implementation because pin, daemon
  loop, task cancel, and workflow resume behavior are operator-visible

## Objective

Move spec-generator-agent (Corbell) onto released agent-core the same way UTA
and CR already did: reuse shared mechanics, keep product policy in the product,
and delete the parallel copies.

This iteration has two coupled outcomes:

1. **Agent-core grows the generic capabilities Corbell needs** and UTA/CR do
   not yet expose, instead of leaving a second harness, git lock, or turn
   facade in Corbell.
2. **Corbell consumes those capabilities.** Spec orchestration
   (`context-prepare` and `spec-docs`) becomes `WorkflowSpec` graphs invoked
   through `invoke_workflow`. LLM turns go through `execute_agent_turn`. The
   daemon loop goes through `TaskDaemon`. Independent OpenCode process,
   affinity, shutdown, and git-auth copies are removed after the pin.

Primary users are:

- agent-core maintainers evolving harness, workflow, git, prompt, and runtime
  facilities;
- spec-generator-agent maintainers operating wiki generation, graph build, and
  task daemons;
- UTA and CR maintainers on a matched agent-core pin (last `0.7.x` or this
  iteration's tag, never mixed).

Success means a maintainer can answer these ownership questions without
reading implementation details:

1. Agent-core owns spawn, stream, structured JSON turns, session affinity,
   fallback cooldowns, shutdown, durable graph invoke/resume, task-daemon
   loop, secure private artifacts, git identity, multi-process repo locks,
   and lease-fenced multi-ref git update.
2. Spec-generator-agent owns wiki page contracts, architecture graph, generation
   intents, downstream activation, publication *policy* (which refs, which paths),
   task rows, and operator UI.
3. LangGraph persists graph position. Product files and `spec_tasks` remain
   product truth. Wiki markdown is not graph state.

## Assumptions

1. This is non-Jira tooling. No issue-tracker key or deploy-approval
   artifact is required.
2. Agent-core **may ship a breaking release** when additive knobs would leave
   a long-lived Corbell facade (R1). Current package is `0.7.9`; a break is
   `0.8.0`. Additive `0.7.10+` is still preferred when the old shape can
   express the new behavior without a product wrapper. UTA (`v0.7.9`) and CR
   (`v0.7.6`) either stay on last `0.7.x` or move as a matched pair with the
   new tag — they are not required to take Corbell-only knobs unless this
   release breaks APIs they already call.
3. Canonical docs live in `agent-core/docs/`. Corbell gets a usage/design
   detail during the design phase; it does not become a second source of
   truth for requirements.
4. Prefer **promoting a Corbell mechanic into agent-core** over wrapping it
   forever in a product facade, whenever a second product could use it.
   Product policy still stays in Corbell (ADR-004).
5. `WikiPlanGate` is an automated validator with bounded replan. It is not a
   human gate. `human_gate` is not required this iteration.
6. Shared `prepare_workspace` (clone one branch) is not the wiki checkout.
   Dual-ref source∪docs merge stays a Corbell node that *uses* core git
   identity, workspace prepare/fetch, and flock.
7. `PublicationCoordinator` (clone → one feature branch → MR) is the wrong
   publisher for the in-repo wiki. Core still gains a **generic** lease-fenced
   multi-ref force-with-lease helper; Corbell supplies refs and lease names.
8. Page markdown, context packs, and drafts stay on disk. LangGraph state
   holds paths, hashes, page lists, and statuses only.
9. No production-log or database query was requested. Scope is from live
   call paths and source ownership.
10. Corbell ADR-001 (copy-via-adaptation, no shared package) is obsolete once
    this iteration ships and must be superseded.

## Requirements

### R1. Reuse first; upgrade agent-core when the capability is generic

- Corbell production code must not keep a long-term parallel of a public
  agent-core capability after the matched pin.
- When Corbell has a mechanic that UTA or CR will also need, or that is
  harness/git/runtime rather than wiki/graph policy, agent-core must grow
  that mechanic in this iteration and Corbell must call it.
- A product-only wrapper is allowed only as a **temporary adapter** during
  migration, then deleted.
- New agent-core names describe capability, not `specgen`, `corbell`, or
  wiki page contracts.

### R2. Release contract: additive preferred, breaking allowed

- Prefer additive knobs when Corbell can call the public API without a
  parallel facade.
- A breaking agent-core release **is allowed and expected** when keeping the
  old shape would force a long-lived product wrapper (R1 wins). Typical
  break candidates: `execute_agent_turn` prompt is file-only,
  `agent_turn` cannot parse JSON / take a message, `SessionAffinity` stores
  only `session_id`, `GitWorkspace.repo_lock` is in-process only if flock
  cannot be added compatibly.
- If breaking:
  - Version is `0.8.0` (or the repo's next breaking tag).
  - Same matched-pair rule as `0.6.11` → `0.7.0`: old binaries never import
    the new tag; new consumers never import last `0.7.x`.
  - Design must list every broken public symbol and which of UTA, CR, and
    Corbell move onto it in this program.
  - UTA and CR **must** either remain pinned to last `0.7.x` or land a
    matched source migration before they install the new tag. A break in an
    API they already call cannot ship as "Corbell-only."
- If additive: new knobs are optional; defaults preserve today's UTA/CR
  behavior; mixed last-`0.7.x`-consumer / new-core is then supported.
- Corbell always pins the tag this iteration releases.

### R3. Harness: Corbell must run turns through agent-core

After the pin, spec workflows must not spawn `opencode run` except through
`agent_core.harness`.

Agent-core must support, with tests:

- Prompt delivery `argv`, `file`, and `stdin`, plus optional CLI `--title`.
  Additive default remains argv/`--file` unless design breaks this and
  migrates UTA/CR.
- Per-turn `pure` override so mixed cursor/non-cursor candidate chains do
  not share one global flag.
- `SessionAffinity` binding `(session_id, model)`; on model/provider change
  the next turn is a **fresh** session that receives a caller-supplied
  bootstrap prompt, not `--continue` on the previous model.
- Fallback cooldowns (rate-limit, model-unavailable, timeout) and prefer
  last success, on top of existing model-health marking.
- Structured JSON extraction and bounded parse/repair retries on
  `run_structured_turn` / `execute_agent_turn` (in-memory message or prompt
  file).
- `execute_agent_turn` accepting an in-memory message **or** a prompt-file
  callable. Required `TurnCostPort` remains; Corbell supplies a port that
  wraps page/session/task budget policy.
- Cancellation as `TurnResult(type="cancelled")` / `is_cancelled`. Products
  may map that to an exception at their daemon boundary; agent-core does not
  need `OpenCodeCancelled` as a public type.
- Graceful shutdown already in core (`install_shutdown_handlers`) is what
  Corbell CLI/daemon must call. Corbell copies of process Protocol + fake
  must be replaced by `OpenCodeProcess.run_turn` and `FakeOpenCodeProcess`.
- Named permission **maps** injected at config write. Profile *values*
  (`restricted` vs final-doc page-lint wrapper, isolated HOME/PATH) stay
  Corbell config.

Writer page-contract repairs stay inside a turn (`attempts` /
`recovery_prompt`), not a second orchestration engine.

### R4. Git mechanics, not wiki policy

- Corbell `git_auth` is replaced by `agent_core.git.identity`. Product still
  supplies host and token.
- `GitWorkspace.repo_lock` must serialize **across processes** (flock) as
  well as threads. Scheduler + UI are multi-process.
- Agent-core must provide a lease-fenced **multi-ref** force-with-lease
  update. Caller supplies ref names, lease namespace, and payload. It must
  not silently open a merge request or invent a feature branch.
- Corbell keeps: dual-ref wiki checkout, generated path allowlist,
  `docs/spec` public tree, fencing token meaning, expected docs SHA.
- Corbell must not publish wiki through `PublicationCoordinator`.

### R5. Prompts and private evidence

- Agent-core must offer an exact `{{name}}` placeholder expander that does
  **not** nested-expand values (values may contain `{{`). Jinja
  `PromptBundle` remains unchanged for UTA/CR.
- Corbell templates and prompt resource hashes stay product-owned.
- Private retry evidence (publication bundles, optional prompt archives)
  uses `SecureArtifactStore`.
- Generated wiki pages under `docs/spec` stay public git content via
  Corbell atomic writes. They must not be stored in `SecureArtifactStore`.

### R6. Runtime: adopt the loop, keep the queue

- Corbell pins released agent-core with the `api` extra (and `langgraph`,
  `yaml` as needed).
- `RuntimeStore.init()` may coexist in the same `state.db` (`ac_*` beside
  `spec_*`).
- `TaskScheduler.run_forever` / embedded UI worker become `TaskDaemon` +
  `DaemonPorts`. Claim, execute, and repo+branch lease renew stay product
  ports. Core runner heartbeats are optional additive later; they must not
  replace repo leases that fence publish and downstream activation.
- Unused `spec_task_control` is replaced by core controls that a running
  OpenCode cancel check actually polls. Intent cancel (pause desired-state)
  stays product.
- `spec_tasks`, generation intents, quotas, and delivery outbox stay Corbell
  tables. Core sees `task_ref=str(task_id)` only.
- SSE/`stream_task_events` may be adopted for the task UI; operator snapshot
  `current_stage`/`detail` stays on `spec_tasks`.

### R7. Spec orchestration is LangGraph via agent-core

Corbell must stop owning a custom stage runner for `ContextPrepareWorkflow`
and `SpecDocsWorkflow` control flow.

- Topology is `WorkflowSpec` data (YAML or equivalent), compiled with
  `build_graph`, started/resumed with `invoke_workflow` and
  `open_checkpointer`.
- Checkpointer paths follow existing owner-only, no-repo-overlap rules.
- Thread ids include topology version. Corrupt lineage fails closed. A clean
  rerun mints a new workflow-run id.
- **context-prepare** is a linear graph with optional keyword-plan retry
  branch. Stage hash-skip is a product node no-op against stage evidence, not
  omitted nodes (LangGraph resume is “next node”).
- **spec-docs** is an outer sequential service loop plus an inner per-service
  graph. Nesting uses `invoke_workflow` (shared node optional; a product node
  is acceptable this iteration).
- Inner graph: prepare context → impact → plan turn → **automated plan
  gate branch** (pass / replan / fail) → **batched page fan-out** →
  deterministic review branch (pass / repair-selected-pages / fail) →
  promote.
- Page parallelism uses existing `FanoutSpec` with `max_parallel` = writer
  lane count. The graph must **not** Send the entire write-order at once.
  Batches of size `max_parallel` preserve isolated-failure budget: after too
  many isolatable page failures, remaining pages are not dispatched.
- `write_page` returns status in collected fan-out results; isolatable
  failures do not raise out of the node.
- Shared `agent_turn` (`result_mode: normalized`) is the LLM node for
  **keyword plan and wiki plan** only, once R3 lands.
- Page write/repair is a **product node** `write_page`: `TurnCostPort` +
  `SessionAffinity` + `Harness.run_turn` from `create_configured_harness`
  (not shared `agent_turn`, not a product import of `OpenCodeHarness`).
- `recursion_limit` must be derived from bounded retries × batches, not left
  as an unbounded default.
- Output-dir rollback on hard failure stays **outside** the graph (daemon
  execute wrapper). LangGraph does not transaction the wiki tree.
- Existing stage functions (graph build, impact, plan-gate validate, page
  lint, review, promote) remain callable; only orchestration moves.

### R8. Graph state vs product files

LangGraph state may contain JSON-safe identifiers, paths, hashes, page path
lists, iteration counters, and per-page statuses.

LangGraph state must not contain:

- page markdown bodies;
- full context packs;
- graph sqlite blobs;
- prompt bytes;
- secrets or OpenCode raw streams.

Deleting a checkpoint may repeat paid turns. It must not change already
published wiki correctness. `spec_tasks` remains operator truth.

### R9. Product policy that must not move into agent-core

These stay Corbell, even under a reuse-first policy:

- architecture graph, tree-sitter extractors, external overlay, MCP tools;
- wiki page contract, coverage, mermaid, OKF, recursive wiki, evidence rules;
- generation intents, dirty coalesce, daily quotas, impact outbox;
- `workspace.yaml`, Jira/Linear/Notion export, SSO;
- `ModelBudgetGuard` **policy** (caps and rotation). Admission calls
  `TurnCostPort`; it does not reimplement paid-attempt ordering.

### R10. Consumer cleanup

- After the matched pin, Corbell production imports of
  `spec_generator_agent.core.opencode.process` / fallback / client JSON spawn
  path are gone except a documented adapter deleted in the same iteration.
- Tests retarget `FakeOpenCodeProcess.run_turn` (or a one-line adapter).
- Corbell ADR-001 is superseded. Usage docs describe pin, daemon, cancel, and
  resume.
- UTA and CR are not required to adopt Corbell-only knobs (stdin delivery,
  placeholder expander, multi-ref publish) unless this release breaks APIs
  they already call — in which case they take a matched migration or stay
  on last `0.7.x`.

### R11. Documentation and observability

- Agent-core README documents the new harness/git/prompt knobs and whether
  the tag is additive `0.7.x` or breaking `0.8.0`, including the matched-pin
  rule.
- Resume disposition, cancelled turns, plan-gate fail, isolated page-failure
  budget, and publication lease loss are observable with bounded, sanitized
  events.
- Logs must not dump wiki page bodies, prompt bytes, or provider secrets.

## Tech Stack

- Python 3.11+ (Corbell already `>=3.11`; agent-core `>=3.11`).
- Agent-core after this iteration's tag (`0.7.10+` if additive, `0.8.0` if
  breaking), with extras `langgraph`, `yaml`, `api` as used.
- LangGraph `>=1.0,<2.0` and `langgraph-checkpoint-sqlite>=2.0,<4.0`
  (Corbell today declares `langgraph>=0.2` and does not import it; the pin
  must align with agent-core's 1.x range).
- SQLite: product `state.db` plus native LangGraph saver file. No SQL against
  LangGraph checkpoint tables from Corbell.
- OpenCode CLI remains the harness implementation.

## Scope Discovery

Inspected: `agent-core` public harness/workflow/git/runtime/prompts;
`Corbell/spec_generator_agent/core/{opencode,tasks,spec,git_auth,atomic_io,prompt_resources,workflows}`;
UTA `WorkflowSpec` / `invoke_workflow`; CR `code-review.yaml` fan-out;
ADR-004; Corbell ADR-001; 2026-08-07 harness assessment.

| Repository/module | Existing responsibility | Decision | Reason |
| --- | --- | --- | --- |
| `agent_core.harness.OpenCodeProcess` | argv/`--file` `run_turn` | In scope | Add stdin, title, per-turn `pure`. |
| `agent_core.harness.affinity.SessionAffinity` | session_id only | In scope | Bind model + bootstrap on change. |
| `agent_core.harness.turns` / `structured_output` | file-based structured loop | In scope | Accept message; JSON parse on `agent_turn`. |
| `agent_core.harness.execution.execute_agent_turn` | `prompt: Callable → Path` | In scope | Allow in-memory message; pass parse/session from node config. |
| `agent_core.harness.nodes.agent_turn` | prompt_file only, no parse | In scope | Corbell JSON planner/writer must use the shared node. |
| `agent_core.harness` shutdown / FakeOpenCodeProcess | already present | In scope as consume | Corbell deletes copies. |
| Fallback cooldowns / prefer-last-success | Corbell-only today | In scope | Generic provider mechanic. |
| `agent_core.git.identity` | token/SSH env | In scope as consume | Replace `core/git_auth.py`. |
| `GitWorkspace.repo_lock` | in-process `RLock` | In scope | Multi-process flock. |
| Multi-ref force-with-lease helper | missing | In scope | Generic git mechanic; wiki refs stay product. |
| `PublicationCoordinator` | clone/branch/MR | Out of scope for wiki | Wrong publish model. |
| `agent_core.prompts` Jinja bundles | UTA/CR | In scope additive | Exact `{{name}}` expander beside Jinja. |
| `SecureArtifactStore` | private evidence | In scope as consume | Bundles only, not `docs/spec`. |
| `TaskDaemon` / `RuntimeStore` / controls | CR already uses | In scope as consume | Corbell loop and cancel. |
| `WorkflowSpec` / `FanoutSpec` / `invoke_workflow` | UTA/CR | In scope as consume | Spec orchestration. |
| Shared `invoke_child_workflow` node | UTA has product glue | Optional | Product node acceptable this iteration. |
| `human_gate` | CR/UTA inbox | Out of scope | Plan gate is automated. |
| Shared `prepare_workspace` | single-branch clone | Out of scope as wiki checkout | Dual-ref merge is product. |
| `spec_generator_agent.core.opencode` | independent ~1.5k LOC harness | In scope to delete after pin | Replaced by core. |
| `ContextPrepareWorkflow` / `SpecDocsWorkflow` control flow | custom Python | In scope | Becomes WorkflowSpec. |
| Stage functions (graph, impact, gate, writer, review, promote) | product | Keep | Called from nodes. |
| `spec_tasks` / intents / repo leases / downstream sync | product orchestration | Out of scope for promotion | Unique ledger; core has no task table. |
| Architecture graph / MCP / wiki protocol | product domain | Out of scope | ADR-004. |
| Jira/Linear/Notion / SSO / `workspace.yaml` | product | Out of scope | |
| UTA operation ledger / CR review policy | other products | Out of scope | Must keep working unchanged. |
| LangGraph-ifying leftover `LLMClient` PRD flows | Corbell ADR-001 leftover | Out of scope | Not spec-docs/context-prepare. |
| Direct SQL on LangGraph checkpoint tables | never | Never | |

## Target Project Structure

Exact filenames are a design decision. Ownership must remain:

```text
agent-core/
  src/agent_core/harness/     # turns, affinity, process knobs, agent_turn config
  src/agent_core/workflow/    # unchanged public invoke/spec/fanout; optional child node
  src/agent_core/git/         # identity, flock, multi-ref lease update
  src/agent_core/prompts/     # Jinja bundles + exact placeholder expander
  src/agent_core/runtime/     # TaskDaemon, artifacts, controls (consume)
  tests/                      # new contracts; break list; UTA/CR-shaped regression

Corbell (spec-generator-agent)/
  spec_generator_agent/core/workflows/   # WorkflowSpec YAML + product node registry
  spec_generator_agent/core/spec/        # page/graph/impact/review policy (no orchestrator)
  spec_generator_agent/core/tasks/       # spec_tasks, intents, DaemonPorts
  spec_generator_agent/core/opencode/    # deleted or thin leftover adapter removed same iteration
  tests/                                 # graph topology, resume, fan-out budget, pin
```

## API and Code Style

- Public contracts: frozen dataclasses, enums, or runtime-checkable protocols.
- Keyword-only arguments on new capability methods.
- Capability names, not product names: `delivery="stdin"`, not
  `specgen_page_prompt`.
- Generic APIs accept `Path`, immutable mappings, JSON-safe DTOs. They do
  not accept `TaskStore` or `workspace.yaml`.
- Unsupported optional behavior is explicit.

Representative shape (design may change names):

```python
result = affinity.run(
    process,
    key,
    message=prompt,
    bootstrap_message=bootstrap,
    delivery="stdin",
    title="spec_docs_plan",
    pure=False,
    repo_path=repo,
    is_cancelled=cancel.is_cancelled,
)

execution = execute_agent_turn(
    AgentTurnRequest(
        name="wiki_plan",
        repo_path=repo,
        prompt=lambda attempt, feedback: prompt_path_or_text,
        parse=extract_json_object,
        attempts=3,
        session_scope="none",
    ),
    context,
)
```

## Commands

Run from each repository root.

### agent-core

```bash
.venv/bin/python -m pytest
.venv/bin/python -m pytest tests/test_harness_affinity.py tests/test_harness_shutdown.py tests/test_agent_turn_normalized.py tests/test_workflow_execution.py
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m build
```

### Corbell (`spec-generator-agent`)

```bash
.venv/bin/python -m pytest
.venv/bin/python -m pytest tests/test_opencode_driver.py tests/test_final_doc_workflow.py tests/test_tasks.py tests/test_task_scheduler_orchestration.py
.venv/bin/python -m compileall -q spec_generator_agent tests
```

### UTA / CR (matched pin)

```bash
# If this tag is additive, run against the new tag with no source change:
# unit-test-agent
.venv312/bin/python -m pytest
# cr_plugin
.venv/bin/python -m pytest

# If this tag is breaking, last 0.7.x consumers must not install it.
# Either keep them on last 0.7.x, or run the same suites after a matched
# source migration onto the new tag.
```

### Cross-repository source boundaries

```bash
rg -n "from spec_generator_agent.core.opencode" Corbell/spec_generator_agent
rg -n "OpenCodeCliProcess|install_opencode_shutdown_handlers|git_subprocess_env" Corbell/spec_generator_agent
rg -n "ContextPrepareWorkflow|SpecDocsWorkflow" Corbell/spec_generator_agent
rg -n "SqliteSaver|langgraph.checkpoint" Corbell/spec_generator_agent
```

After cleanup, OpenCode spawn and shutdown must go through `agent_core`.
Orchestration classes above remain only as thin facades or are gone.
Corbell must not import `SqliteSaver`.

## Testing Strategy

### Agent-core contract tests (new behavior)

- Stdin and `--title` work when requested; large stdin prompts do not hit
  argv limits in the test double. If the release is additive, default
  `run_turn` remains argv/`--file`.
- Per-turn `pure=False` omits `--pure`; default still follows settings.
- Affinity continues the same model with `--continue`; a different model
  starts a new session and sends bootstrap, never `--continue` on the old id.
- Cooldown skips an unhealthy candidate for the documented window and prefers
  the last success.
- `execute_agent_turn` / `agent_turn` parse JSON from a message or file and
  retry while parse fails, within `attempts`.
- Flock: two processes cannot hold `repo_lock` for the same repo/scope at
  once; same-process reentry still works.
- Multi-ref force-with-lease: success, lease lost, and partial-ref failure
  do not leave a one-ref-updated remote.
- Placeholder expander: `{{name}}` exact; values containing `{{` do not
  expand; missing names fail closed.
- Existing checkpoint, fan-out `max_parallel`, and UTA/CR-shaped harness
  tests still pass.

### Corbell consumer tests

- Pin test refuses the wrong agent-core version (same idea as CR
  `agent_core_pin`).
- `context-prepare` graph: linear stages, keyword-plan retry, hash-skip does
  not re-run a completed matching stage, resume after crash continues the
  next node.
- `spec-docs-service` graph: plan-gate pass/retry/fail; page batch fan-out
  respects lane `max_parallel`; isolated failures collect; dispatch stops
  after the failure budget; review repair re-queues only failing paths.
- Outer spec-docs: sequential services; nested invoke does not restart a
  completed inner lineage (`reused_completed`).
- Cancel during a turn stops the OpenCode process and records a cancelled
  task, not empty JSON success.
- Wiki publish still uses lease-fenced refs; no MR created.
- Golden-ish page contract tests keep asserting dict JSON from writer/planner
  turns.
- Existing `test_opencode_driver.py` / `test_final_doc_workflow.py` retargeted
  to core fakes still cover parse retries and affinity bootstrap.

### Compatibility

- Additive tag: UTA and CR suites stay green on the new tag with no source
  change.
- Breaking tag: UTA and CR either stay on last `0.7.x` (proven by pin tests
  that refuse the new tag) or land matched migrations and pass on the new
  tag. Mixed pairs are unsupported.

## Boundaries

- **Always:** write tests first for new core knobs; prefer additive but
  break when R1 requires it; if breaking, list broken symbols in design and
  use matched pins (no mixed pairs); pin Corbell only to a released tag;
  keep wiki files out of LangGraph state and out of `SecureArtifactStore`;
  fail closed on corrupt checkpoints; run the listed pytest commands before
  considering a slice done.
- **Ask first:** promoting generation intents or wiki page contracts into
  agent-core; using `human_gate` for plan approval; forcing UTA/CR onto
  Corbell-only knobs that did not break their existing APIs; rewriting
  leftover non-task `LLMClient` flows.
- **Never:** `PublicationCoordinator` as the wiki publisher; `prepare_workspace`
  as the dual-ref wiki checkout; putting page markdown in checkpoints;
  treating corrupt checkpoint as absence; importing `SqliteSaver` from
  Corbell; encoding spec-gen fingerprints (`spec-docs-writer`, `docs/spec`
  layout) into agent-core public types; chmod-repairing unsafe checkpoint
  paths; committing secrets or `.env` tokens.

## Success Criteria

- Agent-core has a released tag containing R3–R5 mechanics (`0.7.10+` or
  `0.8.0`). Design records additive vs breaking and the broken-symbol list.
- UTA and CR are on a legal pin: same new tag after matched migration, or
  last `0.7.x` with a pin test that refuses the new tag if it is breaking.
- Corbell depends on that tag and has no production OpenCode spawn path
  outside `agent_core.harness`.
- `context-prepare` and `spec-docs` task types run through `invoke_workflow`
  + `TaskDaemon`; custom `run()` orchestrators are gone or are one-line
  wrappers.
- Isolated page-failure budget, plan-gate bounded replan, and review repair
  still hold under the graph.
- Repo+branch leases still fence publish; wiki still updates via multi-ref
  force-with-lease without an MR.
- Corbell ADR-001 is superseded; usage documents pin, cancel, and resume.

## Rollout

1. Design decides additive `0.7.10+` vs breaking `0.8.0` from the R1/R2
   break list.
2. Implement and test agent-core. If breaking, freeze last `0.7.x` as the
   remaining old-consumer pin (UTA/CR already on `0.7.6`/`0.7.9`).
3. Tag/release agent-core.
4. Pin Corbell to that tag; migrate runtime loop, then context-prepare
   graph, then spec-docs graphs, then delete harness copies.
5. If the tag broke UTA/CR-called APIs, land those matched migrations (or
   leave those products on last `0.7.x`) before they install it.
6. Prove cancel, resume, and a dry-run/publish path in tests.

Rollback: Corbell reverts the pin and product commits. Breaking core is not
installed by old consumers. Additive leftovers on `0.7.x` may remain.

## Open Questions

Resolved by this spec unless the user overrides at approval:

- Human plan-approval inbox: **not this iteration**.
- LangGraph rewrite of leftover `LLMClient` PRD/review CLI: **not this
  iteration**.
- Promoting intents/graph/wiki schema: **never this iteration**.
- Shared `invoke_child_workflow` node: **optional**; product node is enough.

Still open for design (not spec blockers):

1. Exact public names for prompt delivery, multi-ref publish, and
   placeholder expander.
2. Whether Corbell usage docs live only in Corbell or also as a short
   pointer under `agent-core/docs/usage-*.md`.
3. Precise `recursion_limit` formula (design from max pages / lanes /
   review iterations).
4. Which public APIs actually break vs stay additive (R2). Breaking is
   allowed; the list is a design output, not a spec freeze.

## Changelog

- 2026-08-25 — Spec draft: allow a breaking agent-core release when
  additive APIs would leave a Corbell facade; matched-pin rule as in 0.7.
- 2026-08-25 — R7 refined by design: shared `agent_turn` is planner/keyword
  only; page write/repair is product `write_page` (affinity + harness +
  cost port). Writers on `agent_turn` cannot keep stdin, sandbox, or
  session continue.
- 2026-08-26 — Writer path is `Harness` + `SessionAffinity` via
  `create_configured_harness`. Product code must not import
  `OpenCodeHarness`; OpenCode knobs stay in `HarnessSpec.options`.

## Sibling Documents

| Artifact | Path | Status |
| --- | --- | --- |
| Spec | `docs/spec-spec-gen-agent-core-adoption.md` | approved 2026-08-25 |
| Design overview | `docs/design-spec-gen-agent-core-adoption.md` | approved 2026-08-26 |
| Design agent-core | `docs/design-spec-gen-agent-core-adoption-agent-core.md` | approved 2026-08-26 |
| Design Corbell | `Corbell/docs/design-spec-gen-agent-core-adoption.md` | approved 2026-08-26 |
| Usage | `docs/usage-spec-gen-agent-core-adoption.md` | approved with design 2026-08-26 |
| Plan | `docs/plan-spec-gen-agent-core-adoption.md` | pending review |
| Todo | `docs/todo-spec-gen-agent-core-adoption.md` | pending review |
| Related | `docs/spec-workflow-persistence-and-common-capabilities.md`, ADR-004, Corbell ADR-001 superseded by ADR-015 | |
