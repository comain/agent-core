# Spec: Workflow Persistence Simplification and Common Capability Extraction

## Status

- Phase: planning
- Approval: approved by the user on 2026-08-20
- Work type: non-Jira tooling and architecture iteration
- Issue tracker: not applicable; the user previously confirmed this migration is
  non-Jira work
- Canonical repository: `agent-core`
- Affected repositories: `agent-core`, `unit-test-agent`, `cr_plugin`
- Design documents: approved by the user for planning on 2026-08-21
- Usage documents: required during the design/implementation iteration because
  public configuration, diagnostics, and operational recovery behavior are affected

## Objective

Simplify durable workflow persistence so that LangGraph remains visibly and
unambiguously responsible for graph checkpoints, while agent-core supplies only
the security, identity, lifecycle, and invocation policy that products need.
Keep product-side reconciliation for effects LangGraph cannot checkpoint, such
as repository edits, provider charges, filesystem artifacts, and product database
writes.

In the same iteration, move mechanics that UTA and CR currently duplicate into
agent-core. The target is a smaller, agent-agnostic product boundary without
turning UTA generation policy, CR review policy, enforcement logic, or product
persistence into agent-core APIs.

Primary users are:

- agent-core maintainers evolving reusable workflow, harness, Git, prompt, and
  runtime facilities;
- UTA maintainers operating crash-resumable, repository-changing generation
  workflows;
- CR maintainers operating review, feedback, and retrospective workflows;
- third-party agent-core consumers that need a stable checkpoint and execution
  contract without importing LangGraph implementation details throughout their
  product.

Success means that a maintainer can answer these ownership questions without
reading implementation details:

1. LangGraph persists graph position and serializable state.
2. Agent-core safely opens, identifies, invokes, resumes, reuses, and deletes a
   LangGraph lineage.
3. Products persist and reconcile their own externally visible side effects.
4. Generic artifact, execution-context, session, prompt-bundle, and publication
   mechanics are implemented once in agent-core.

## Assumptions

1. LangGraph 1.x and its SQLite checkpoint package remain the initial backend.
2. Existing checkpoint identities and stored LangGraph data remain readable;
   this iteration does not require checkpoint migration or task replay.
3. UTA's operation ledger remains necessary because repository and product
   side effects cannot be committed atomically with a LangGraph checkpoint.
4. UTA and CR remain authoritative for task state, retry budgets, business
   status, retention eligibility, templates, and publication policy.
5. This is a coordinated breaking release. UTA and CR first pin agent-core
   `0.6.11`, agent-core then releases `0.7.0`, and each consumer moves to the
   released `0.7.x` contract as one tested pair. There is no old-consumer/new-core
   compatibility window.
6. No production-log or database query was requested for this architecture-only
   scope pass. Prioritization is based on live production call paths and source
   ownership, not traffic counts.

## Requirements

### R1. LangGraph is the sole graph-checkpoint implementation

- Agent-core must use a supported LangGraph `BaseCheckpointSaver` implementation
  directly when compiling a graph.
- Agent-core must not reproduce or manually forward the complete LangGraph saver
  interface.
- Agent-core must not own a parallel checkpoint schema, serialization format, or
  checkpoint migration system.
- Products must not import `SqliteSaver`, construct LangGraph invocation config,
  or issue SQL against LangGraph checkpoint tables.
- The public agent-core boundary must continue to provide:
  - stable workflow-run identity;
  - topology-version isolation;
  - owner-only checkpoint paths;
  - explicit resource lifetime and close behavior;
  - lineage deletion through supported saver capabilities;
  - start, pending-resume, completed-reuse, and corrupt-lineage classification.
- A missing saver deletion capability must fail explicitly. It must not be
  emulated by assuming private LangGraph table names.
- Checkpoint roots use the same ancestor/symlink/forbidden-root validation as
  private artifacts. Existing broad or shared parents are rejected rather than
  chmodded, including either-direction overlap with a target repository.
- The supported dependency range is `langgraph>=1.0,<2.0` and
  `langgraph-checkpoint-sqlite>=2.0,<4.0`.

### R2. Resume behavior remains safe and observable

- A workflow with no checkpoint starts with initial state.
- A pending workflow resumes from stored state without re-supplying fresh input.
- A completed workflow returns its stored result without invoking any node.
- A corrupt or unreadable lineage fails closed and never becomes an implicit
  clean rerun.
- A clean rerun uses a new workflow-run identity.
- Products can observe whether execution started, resumed, or reused a completed
  result.
- Existing UTA and CR checkpoint thread identities must remain stable unless the
  corresponding workflow topology version intentionally changes.

### R3. Graph checkpoints and product operation evidence remain separate

- Documentation and APIs must not imply that a LangGraph checkpoint makes Git,
  filesystem, provider, or product-database effects atomic.
- UTA retains product ownership of:
  - operation claim and completion rows;
  - repository and output fingerprints;
  - allowed-edit decisions;
  - crash-reconciliation outcome selection;
  - provider-cost accounting policy;
  - terminal product-evidence validation;
  - retention eligibility and deletion ordering.
- UTA's policy is fail-closed on an unknown charge before the next fallback or
  recovery submission whenever a currency cap applies. Without a currency cap,
  its bounded chain may continue but aggregate cost remains unavailable.
- Agent-core may own generic operation identity, immutable envelope, and artifact
  storage primitives only when those primitives contain no UTA phase, language,
  task-status, database-schema, workspace-policy, or retry-policy semantics.
- The full UTA `WorkflowOperationLedger` is explicitly out of scope for promotion
  until a second product adopts equivalent side-effect reconciliation.
- Fault tests must cover a crash before work, after an external effect but before
  node return, after product evidence but before checkpoint, and after checkpoint.

### R4. Agent-core provides secure artifact storage

Agent-core must provide one reusable artifact facility for private workflow and
prompt evidence with these observable properties:

- paths are confined beneath an explicit caller-owned root;
- absolute paths, traversal, symlink roots, symlink parents, and symlink
  destinations are rejected;
- private directories are owner-only and private files are owner-only;
- writes use a temporary file, flush data, atomically replace the destination,
  and preserve deterministic UTF-8/JSON bytes;
- immutable writes are idempotent when bytes match and fail when the same
  identity is reused for different bytes;
- stored results include relative path, byte length, and SHA-256 digest;
- verified reads reject missing, changed, incorrectly permissioned, or escaped
  artifacts;
- deletion is restricted to an exact validated namespace and refuses unexpected
  entries instead of recursively deleting an ambiguous tree;
- every read and immutable comparison requires an explicit maximum byte count;
- text, JSON, and byte writes share the same confinement and durability rules;
- a per-namespace lock serializes writers, verification, and deletion;
- deletion accepts a typed exact namespace layout, not an arbitrary predicate,
  and refuses active locks, partial files, or unexpected entries;
- callers receive an unambiguous truncation or rejection result.

UTA may build operation evidence on this facility. CR may build private review
context and retrospective evidence on it. Product code still chooses artifact
identity, content, disclosure, retention eligibility, and deletion order.

### R5. Agent-core provides one typed turn executor

- Agent-core must expose `execute_agent_turn(request, context)` as the canonical
  lifecycle for both LangGraph nodes and direct callers. The LangGraph
  `agent_turn` node is a projection adapter over it; CR reviewer, judge, feedback,
  and retrospective nodes call it directly while retaining parse/accept callbacks.
- The executor reuses `run_harness_node` as its attempt/parse/accept engine; no
  second attempt loop is permitted.
- It returns both the exact `NodeOutcome` for direct callers and a normalized
  `AgentTurnResult` for durable projection. The result port receives that combined
  DTO after snapshot/cleanup; on guard rejection its separate reject callback may
  persist audit/session refs but cannot mark a reusable result.
- Cost admission/recording surrounds every actual provider submission with one
  monotonic paid-attempt ordinal across retry, fallback, and recovery. Attempt N
  is recorded before attempt N+1 is admitted; exceptions report explicit unknown
  cost rather than bypassing the port.
- The context carries `HarnessBinding(name, harness)`; names are never inferred
  from Python classes.
- The context must compose narrow capabilities for:
  - cancellation;
  - progress publication and final flush;
  - workspace validation before and after a turn;
  - mandatory provider-cost admission/recording mechanics (a no-cap product uses
    no-op admission but still records reported/unknown cost);
  - normalized result durability;
  - session opening;
  - an optional execution observer whose pre-attempt check can reject expired work
    before a paid call.
- Capability absence and required-capability failure must be explicit and typed.
- Product adapters translate product task state into these neutral ports once at
  the application boundary. Agent-core must not import product task managers or
  product database types.
- Product daemon heartbeats remain independent background liveness mechanisms;
  they are not driven by model progress. Silent provider stalls remain bounded by
  harness timeout/cancellation.
- UTA and CR migrate directly in the coordinated `0.7.0` cutover. Agent-core does
  not retain a callback-mapping compatibility adapter.

### R6. Session identity and diagnostics are provider-neutral

- Agent-core must define a neutral session reference with harness name, opaque
  locator, and `durable`/`process` locator scope. Deserialization validates syntax
  only; registry support is resolved only when diagnostics run.
- A turn result carries an ordered, de-duplicated tuple of every session reference
  used by fallback candidates. A latest-session convenience projection is not the
  durable source of truth.
- OpenCode exposes its native database session locator after a provider response;
  its wrapper UUID is process-scoped only. Restart diagnostics use the durable
  native locator.
- Agent-core must define an optional diagnostics capability and a bounded,
  JSON-safe result covering exact overall usage, neutral usage-by-model, timing,
  tool counts, patch counts, and typed diagnostic signals. Signals use fixed
  categories/codes, counts, optional allowlisted tool names, and bounded private
  sanitized summaries; raw reasoning, commands, tool input, model output, and
  provider rows are forbidden.
- The OpenCode adapter may implement provider-specific data collection,
  including access to its private local database. That implementation must live
  beside the OpenCode adapter rather than inside UTA or CR.
- A harness without diagnostics support returns an explicit unsupported result;
  products must not assume missing diagnostics means zero usage or success.
- Diagnostics use a closed available/unsupported/unavailable item union. Input
  count or byte-limit excess raises `DiagnosticsLimitExceeded` before partial
  totals; raw rows are bounded before JSON parsing (1 MiB each, 64 MiB total).
- Public progress/report projections must not expose raw reasoning, commands,
  secrets, full model output, or provider database records.
- UTA's assessment CLI becomes a thin consumer of the diagnostics capability.
  Its user-visible comparison and table rendering remain UTA-owned.
- CR and UTA production code must stop introducing new
  `opencode_session_id`/`opencode_session_ids` fields. Existing persisted columns
  and response aliases may be read during a documented compatibility window,
  but internal DTOs and new writes use neutral session terminology.

### R7. Prompt artifacts form a secure reproducible bundle

- Agent-core prompt support must be able to materialize one bundle containing:
  - the exact rendered prompt;
  - the strict JSON input projection;
  - zero or more named reference files;
  - a deterministic manifest containing relative paths, byte lengths, and
    digests.
- Reference names and paths are treated as untrusted path components and use the
  secure artifact rules from R4.
- Completion uses a manifest-last protocol under a namespace lock: files are
  atomically written at final paths and `manifest.json` is atomically written
  last. A bundle is complete only when that manifest verifies every listed file.
  This preserves CR's existing mixed reviewer directory and absolute path bytes.
- Products own templates, variable selection, stable/volatile partitioning,
  phase identity, prompt root selection, and retention eligibility.
- Existing prompt bytes must remain frozen by golden tests. Moving storage must
  not normalize whitespace or silently stringify unsupported metadata.

### R8. Git publication is one shared orchestration

- Agent-core must provide a provider-neutral publication request/result that can
  compose an isolated workspace, a product-approved path set, commit and push,
  remote-SHA verification, optional merge-request creation, and a manual fallback
  URL/result when API creation is unavailable.
- Products own repository/project identity, source and target branch policy,
  allowed paths, generated content, commit message, merge-request title, and
  credentials.
- The shared capability must never stage an unapproved path, discard an
  unapproved path, or report success before verifying the remote ref.
- The Git contract distinguishes `base_branch` and `publish_branch`, including
  first publication when the publish branch does not exist and later updates.
- A stable product publication identity selects the source branch. MR creation is
  an idempotent ensure operation that checks for an open MR before creation and
  after an ambiguous API failure.
- If a retry finds the stable publish branch already at the verified content for
  the same publication identity, Git returns `REUSED_PUBLISHED` and orchestration
  continues to MR ensure. It must not raise no-changes or repush.
- CR feedback-pattern publication must use the shared orchestration rather than
  manually combining workspace, GitLab, URL, and cleanup mechanics.
- UTA delivery may continue to use the scoped-publish subset when no merge
  request is needed.

### R9. Consumer cleanup and compatibility

- CR's neutral harness setting remains product-owned, but legacy
  `review_v2_opencode_*` environment compatibility must not become an agent-core
  concern. Adapter-specific option validation belongs to the selected agent-core
  harness implementation.
- CR must migrate provider-named concurrency and session fields to neutral names,
  preserving documented read aliases only for the compatibility window.
- UTA session usage and retrospective code must consume normalized session or
  turn diagnostics rather than `client: Any` provider methods.
- UTA's operation ledger must consume the shared secure artifact primitive
  without losing existing artifact identity, hashes, permissions, crash outcomes,
  or retention behavior.
- Both consumers pin agent-core `0.6.11` before core `0.7.0` lands, then update to
  released `0.7.x` with their source migrations. Old binaries never import the
  breaking core release.
- CR adds plural neutral session-reference storage. Generic legacy session
  columns receive the latest locator for every harness during rollback
  compatibility; provider-named columns are dual-written only for that provider.
- CR's mandatory no-cap cost port stores nullable provider cost plus explicit
  `recorded`/`unavailable` provenance; historic numeric rows are
  `legacy_unverified` and unknown is never converted to authoritative zero.

### R10. Documentation and observability

- Agent-core README and API documentation clearly distinguish graph checkpoint,
  operation evidence, and product truth.
- UTA and CR architecture documents show the typed execution context, neutral
  session references, artifact boundary, and publication boundary.
- Resume disposition, artifact verification failures, unsupported diagnostics,
  and publication outcomes are observable using bounded, sanitized records.
- No logs or public events expose checkpoint contents, prompt contents, raw
  reasoning, provider credentials, or private artifact payloads.

## Tech Stack

- Python 3.11+ with frozen dataclasses and runtime-checkable protocols.
- LangGraph `>=1.0,<2.0` and `langgraph-checkpoint-sqlite>=2.0,<4.0` as optional
  workflow dependencies.
- SQLite through the native LangGraph saver only; products retain their own task
  schemas and transactions.
- Git subprocess mechanics through `agent_core.git`; forge APIs remain adapters.

## Scope Discovery

The following table records the inspected source candidates and the scope
decision. The scan covered active production modules, public exports, tests,
configuration fields, and documentation in all three repositories.

| Repository/module | Existing responsibility or duplication | Decision | Reason |
| --- | --- | --- | --- |
| `agent_core.workflow.checkpoints` | Opens LangGraph SQLite saver but wraps and manually forwards its saver API | In scope | Simplify to direct saver use while retaining security and lifecycle policy. |
| `agent_core.workflow.execution` | Correctly classifies absent, pending, completed, and corrupt lineages | In scope, preserve | Shared resume semantics prevent duplicate paid work. |
| `agent_core.workflow.graph` | Compiles declarative graphs with a supplied checkpointer | In scope | Must receive the native supported saver boundary. |
| `agent_core.runtime.artifacts` | Basic bounded writes and hashes, without atomic/private/verified lifecycle | In scope | Both UTA and CR need stronger identical mechanics. |
| `agent_core.prompts` | Strict rendering and secure prompt/input pair | In scope | Extend to reference bundles without moving product templates. |
| `agent_core.workflow.nodes` | Reads string-keyed execution callbacks | In scope | Both products construct the same neutral execution capabilities. |
| `agent_core.harness` OpenCode adapter | Owns provider sessions, usage, progress, and lifecycle | In scope | Correct home for optional OpenCode diagnostics. |
| `agent_core.git.publish` and `agent_core.integrations.gitlab` | Scoped push and MR HTTP client are separate | In scope | CR duplicates their generic orchestration. |
| `uta.testgen.graph.application` | Binds checkpoint, ledger, session, progress, and result ports | In scope as consumer | Migrates to native saver handle and typed execution context. |
| `uta.testgen.operations` artifact mechanics | Atomic owner-only operation artifact files | In scope for generic storage extraction | Mechanics are reusable; UTA identities and reconciliation remain product-owned. |
| `uta.testgen.operations.WorkflowOperationLedger` | Workspace-aware seven-outcome crash reconciliation | Out of scope for promotion | Only UTA has this product requirement today. |
| `uta.testgen.prompts.artifacts` | UTA prompt identity, roots, leases, metadata, retention | Partly in scope | Generic file mechanics move; identity and lifecycle policy stay in UTA. |
| `uta.testgen.session_analysis` and `session_usage` | Provider-shaped calls through `client: Any` | In scope | Replace with neutral session diagnostics. |
| `uta.app.opencode_assessment` / assessment command | Reads OpenCode private schema and renders analysis | Partly in scope | Collection moves to adapter; UTA presentation remains. |
| `uta.shared.delivery` | Uses core scoped Git publication with UTA path policy | In scope as compatibility consumer | Validates the shared publish subset remains sufficient. |
| `cr_agent.review.pipeline.context` and retrospective repository | Use the weak core ArtifactStore | In scope | Migrate to the secure store while preserving private-audit policy. |
| `cr_agent.review.pipeline.prompts` | Uses PromptLibrary but directly writes reference trees | In scope | Consume secure prompt bundles. |
| `cr_agent.review.workflow.control` | Adapts CR task rows to cancellation callbacks | In scope as adapter | Product interpretation stays; output becomes a typed neutral port. |
| `cr_agent.review.feedback.patterns.publication` | Manually composes isolated clone, push, GitLab MR, fallback URL, cleanup | In scope | Generic publication orchestration is duplicated. |
| CR configuration and feedback-session schema | Contains provider-named settings and session fields | In scope for neutral migration | Concrete provider names leak beyond adapter/config compatibility. |
| UTA/CR product task databases | Authoritative task state and transactions | Out of scope | Product schemas and atomic business transitions do not belong in agent-core. |
| UTA enforcement contract and Java/Python enforcement bindings | Independently distributed enforcement domain | Out of scope | Must remain usable without agent-core. |
| UTA Java/Python test-generation phases | Language and product behavior | Out of scope | Not a common agent capability. |
| CR reviewer planning, risk, judging, feedback, retrospective policy | Code-review product behavior | Out of scope | Not a common agent capability. |
| Agent-core pricing and product currency-cap policy | Model pricing utility versus task budget semantics | Out of scope | Product caps and task transitions remain product-owned. |
| LangGraph checkpoint table layout | Third-party implementation detail | Never in scope | No direct SQL or duplicated schema. |

## Target Project Structure

Exact filenames are a design decision, but the ownership structure must remain:

```text
agent-core/
  src/agent_core/workflow/     # LangGraph identity/lifecycle/invocation and graph projection
  src/agent_core/runtime/      # secure generic artifact storage
  src/agent_core/harness/      # canonical turn execution, neutral sessions/results, diagnostics
  src/agent_core/prompts.py    # rendering and reproducible prompt bundles
  src/agent_core/git/          # scoped Git publication
  src/agent_core/integrations/ # optional forge/MR integrations
  tests/                       # public contracts, security, faults, compatibility

unit-test-agent/
  uta/testgen/                 # product workflow, operation reconciliation, prompt identity
  uta/app/                     # composition, assessment presentation, retention coordination
  uta/tasks/                   # product persistence only
  uta/enforcement/             # enforcement contracts and proxies, independent of agent-core internals
  tools/python-enforcement/    # independently distributed Python enforcement implementation

cr_plugin/
  src/cr_agent/review/         # review policy and product persistence
  src/cr_agent/app|api/        # composition and presentation
  tests/                       # consumer compatibility and product behavior
```

## API and Code Style

- Public contracts use frozen dataclasses, enums, or runtime-checkable protocols.
- Public names describe capability, not provider or product:
  `session_ref`, not `opencode_session_id`; `TurnExecutionContext`, not a task
  manager bridge.
- Generic APIs accept `Path`, immutable sequences/mappings, and JSON-safe result
  DTOs. They do not accept product settings or database handles.
- Capability methods use keyword-only arguments.
- Unsupported optional capabilities are explicit; absence is never converted to
  success, zero cost, or an empty diagnostic report.
- Exceptions exposed across the public boundary contain bounded, sanitized
  explanations rather than provider payloads or artifact contents.

Representative style:

```python
@runtime_checkable
class CancellationSource(Protocol):
    def is_cancelled(self) -> bool: ...


@dataclass(frozen=True)
class TurnExecutionContext:
    cancellation: CancellationSource | None = None
    progress: ProgressSink | None = None
    cost: TurnCostPort | None = None
    result: TurnResultPort | None = None
```

This example defines shape and naming only. The design phase decides the final
protocol decomposition and transition API.

## Commands

Commands are run from each repository root using its configured environment.

### agent-core

```bash
.venv/bin/python -m pytest
.venv/bin/python -m pytest tests/test_checkpoints.py tests/test_workflow_execution.py tests/test_agent_turn_normalized.py tests/test_runtime_artifacts.py tests/test_prompts.py
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m build
```

### unit-test-agent

```bash
.venv312/bin/python -m pytest
.venv312/bin/python -m pytest tests/test_generation_workflow_application.py tests/test_generation_operation_artifacts.py tests/test_prompt_artifacts.py tests/test_prompt_render.py tests/test_workflow_retention.py tests/test_standalone_generation_execution.py
.venv312/bin/ruff check uta tests tools/python-enforcement
.venv312/bin/python scripts/check_package_dependencies.py
```

### cr_plugin

```bash
.venv/bin/python -m pytest
.venv/bin/python -m pytest tests/review/workflow tests/review/pipeline/test_context.py tests/review/pipeline/test_prompts.py tests/review/feedback
.venv/bin/python -m compileall -q src tests
```

### Cross-repository source boundaries

```bash
rg -n "SqliteSaver|langgraph\.checkpoint" unit-test-agent/uta cr_plugin/src/cr_agent
rg -n "opencode_session_id|opencode_session_ids|client: Any" unit-test-agent/uta cr_plugin/src/cr_agent
rg -n "WorkflowCheckpointer\(|def (get_tuple|put_writes|aget_tuple|aput_writes)" agent-core/src/agent_core/workflow
```

Allowed hits are restricted to explicit compatibility migrations, tests, and
the agent-core LangGraph/OpenCode adapter implementations documented by design.

## Testing Strategy

### Contract tests in agent-core

- Compile a real LangGraph graph with the native saver supplied by the managed
  checkpoint lifecycle.
- Start, crash/pause, resume with `None`, reuse completed state, reject corrupt
  state, delete one lineage, and preserve another lineage.
- Verify owner-only permissions, symlink rejection, close behavior, and no direct
  checkpoint-table SQL.
- Inject a fake saver backend to prove consumers do not depend on SQLite types.
- Verify typed turn-context capability validation, call ordering, cancellation,
  progress flush, observer admission, cost/result ports, and identical direct vs
  LangGraph executor traces.
- Verify two paid retries use distinct monotonic ordinals, the first recorded cost
  can block the second admission, and provider exceptions record unknown cost.
- Fault-inject every secure artifact write stage and verify no partial trusted
  artifact is returned.
- Verify prompt bundle bytes, manifest determinism, reference path confinement,
  and metadata rejection.
- Verify optional diagnostics supported/unsupported behavior, aggregation,
  sanitization, raw-row/total input byte limits, and bounded output.
- Verify publication path refusal, cleanup refusal, rebase conflict, push failure,
  remote-SHA mismatch, MR success, missing credentials, API failure, and manual
  fallback result.

### UTA consumer and fault tests

- Preserve the existing seven operation-reconciliation outcomes and exact
  operation identities.
- Prove an external repository edit followed by a pre-checkpoint crash is adopted,
  verified, retried, or blocked according to existing policy without repeating
  paid work blindly.
- Prove checkpoint-first retention safety in the unchanged order: checkpoint,
  operation artifacts, operation rows, prompt artifacts.
- Prove Java and Python generation routes consume the typed execution context and
  neutral session diagnostics.
- Preserve prompt golden bytes and standalone cleanup/recovery behavior.
- Run package-dependency enforcement proving the independently distributed
  enforcement packages do not import agent-core or UTA.

### CR consumer tests

- Preserve reviewer, judge, feedback, and retrospective behavior while consuming
  the typed execution context.
- Verify private review artifacts and prompt references use the secure store and
  never escape or enter the reviewed repository.
- Verify feedback-pattern publication uses one shared publication result for push,
  MR, and manual fallback outcomes.
- Verify plural neutral session references and the exact table/API dual-write
  matrix, including old-binary rollback visibility.
- Inventory every private direct write and prove only named report/publication
  outputs bypass the secure store.

### Compatibility and integration

- Test old consumers on pinned agent-core 0.6.11 and updated UTA/CR on released
  0.7.x; mixed pairs fail dependency/import validation before workers start.
- Freeze public DTO JSON and existing checkpoint thread IDs.
- Existing persisted product rows and artifact files remain readable throughout
  the compatibility window.
- No real provider, paid model, Git push, or merge-request creation is required by
  the default test suite; scripted/fake adapters cover those contracts.

## Capacity, Reliability, and Security Requirements

- Removing the saver-forwarding wrapper must not add checkpoint writes, rows, or
  payload copies.
- Secure artifact storage must stream or reject bounded content rather than retain
  an additional unbounded full copy solely for hashing.
- Diagnostics have explicit maximum record counts and byte limits determined in
  design; truncation is visible.
- Publication uses one Git publication sequence plus bounded MR lookup, optional
  create, and ambiguity lookup.
- SQLite connections and file descriptors close deterministically.
- A failed progress or diagnostic projection cannot abort or alter a completed
  model result unless the corresponding capability is explicitly authoritative.
- Artifact, checkpoint, and diagnostic paths reject containment overlap with a
  target repository where deletion could remove product source.
- Existing secret-redaction and public-progress restrictions remain mandatory.

## Rollout and Compatibility Requirements

1. Pin and deploy both current consumers on agent-core 0.6.11.
2. Complete and release the breaking agent-core 0.7.0 contract while old
   consumers remain pinned.
3. Migrate UTA to released 0.7.x; CR must stop tracking `main` before its matching
   source migration.
4. Migrate UTA checkpoint lifecycle and generic artifacts while preserving its
   operation ledger and run beta scripted Java/Python recovery canaries.
5. Migrate CR artifacts, execution context, neutral session fields, prompt bundles,
   and publication; run review/feedback/retrospective canaries.
6. Observe at least one normal, one resumed, and one completed-reuse workflow in
   each product where the product supports that disposition.
7. Remove persisted product read/write aliases only after repository-wide scans
   and compatibility checks; agent-core has no mapping adapter in 0.7.

Rollback must restore a mutually compatible released agent-core/consumer set.
Rolling back only agent-core underneath a consumer requiring its new typed APIs is
not supported.

## Boundaries

### Always do

- Use LangGraph's supported saver APIs and real integration tests.
- Preserve checkpoint identities and product operation identities.
- Keep graph state JSON-safe and free of live clients, connections, callbacks,
  credentials, or task managers.
- Validate paths before filesystem mutation.
- Release and verify agent-core before updating consumers.
- Keep source-boundary tests for product, enforcement, and provider dependencies.
- Update README, architecture, usage, and ADR documentation with implementation.

### Ask first

- Change an existing checkpoint thread identity or topology version.
- Add or migrate a product database column beyond the neutral session-name
  compatibility work described here.
- Add a new runtime dependency or change supported LangGraph versions.
- Change UTA reconciliation outcomes, paid-work replay policy, or retention order.
- Remove a public response alias or persisted compatibility reader before the
  documented window ends.
- Expand diagnostics to expose raw model text, reasoning, commands, or provider
  records.

### Never do

- Implement a second checkpoint engine or copy LangGraph's checkpoint schema.
- Treat checkpoint corruption as absence.
- Claim a graph checkpoint atomically commits repository or product side effects.
- Move UTA/CR task persistence, enforcement, language logic, or business policy
  into agent-core.
- Make independently distributed enforcement packages depend on agent-core.
- Store secrets, raw reasoning, or unbounded provider payloads in checkpoints,
  public events, manifests, or diagnostics.
- Delete broad or unresolved filesystem paths during retention or rollback.
- Preserve duplicate product bridges after all consumers use the core contract.

## Success Criteria

1. Agent-core compiles workflows using a native supported LangGraph saver and no
   custom class manually forwards saver protocol methods.
2. Start, pending resume, completed reuse, corruption refusal, topology isolation,
   and lineage deletion pass against a real SQLite saver.
3. No UTA or CR production module imports `SqliteSaver`, issues checkpoint-table
   SQL, or manually constructs LangGraph thread configuration.
4. UTA's existing operation reconciliation and terminal evidence validation remain
   behaviorally unchanged and pass crash-window tests.
5. One secure agent-core artifact implementation replaces generic private-file
   mechanics in UTA and CR while preserving existing readable artifacts.
6. UTA's LangGraph node and CR's direct nodes share `execute_agent_turn`; new
   production code does not depend on callback-key dictionaries or duplicate the
   attempt/parse/accept loop.
7. Internal session DTOs are provider-neutral and retain every fallback candidate;
   OpenCode restart diagnostics use its native durable locator beside the adapter,
   and unsupported harness diagnostics fail explicitly.
8. CR prompt references and UTA prompt inputs are emitted as deterministic secure
   bundles with frozen prompt bytes.
9. CR feedback-pattern publication uses the shared publication orchestration and
   first/update/race/timeout retries create neither duplicate branches nor MRs;
   UTA scoped delivery continues to pass path-safety and remote-verification tests.
10. Product persistence, enforcement, language bindings, workflow policy, and
    retention eligibility remain outside agent-core and pass dependency gates.
11. Full default suites, focused fault suites, package builds, compile checks, and
    dependency scans pass in all three repositories.
12. Agent-core is tagged and both consumer repositories pin the verified release.
13. Documentation clearly explains why LangGraph checkpoints and product operation
    evidence are complementary rather than duplicate persistence systems.

## Approved Decisions

1. Provider-named persisted fields have a product-owned rollback window with the
   exact CR table matrix in design; the Python API itself cuts over in agent-core
   0.7 without aliases.
2. Neutral diagnostics DTO/protocol live in agent-core, OpenCode DB access lives
   beside its adapter, and UTA owns assessment presentation/model buckets.
3. Secure artifact mechanics move; UTA's ledger and reconciliation remain in UTA
   until a second product demonstrates the same policy.
4. Unsupported saver deletion is explicit, including inherited
   `NotImplementedError`; private checkpoint SQL is forbidden.
5. Consumers first pin 0.6.11, then migrate to released 0.7.x as matched pairs.

## Changelog

- 2026-08-20 — Iteration 1 specification drafted from the workflow persistence
  review and the UTA/CR/agent-core common-capability scan.
- 2026-08-20 — Revision 2 integrates all design-review dispositions approved by
  the user and closes the former open questions.
