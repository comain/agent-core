# Model Discovery Implementation Plan

Status: approved; implementation in progress. Non-Jira work.
Inputs: [spec](spec-model-discovery.md), [design](design-model-discovery.md),
[review](review-model-discovery.md), [workflow](workflow-model-discovery.md).

## 1. Delivery Contract

Artificial Analysis Coding Index default 70; operator override then application
setting; descending unrounded score; daily refresh; application allow/deny;
current list on resume; only operational availability persists. No DeepSWE,
task list snapshots, preferred-model promotion, or new provider-failure rules.

Every task uses RED -> GREEN -> relevant regression -> build -> scoped commit.
Keep evidence of the failing test before the fix and passing command afterward.
Use repo-local virtual environments. No credential values in fixtures/evidence.
Implementation is present for T1-T10; the acceptance checkboxes remain open where
the broader enforcement, live mapping, or release evidence is still outstanding.
Preserve unrelated UTA uv.lock. Do not absorb another task's edits in commits.

## 2. Task Tree

### 2.1 Shared Admission

#### T1. Pure selection contract
Dependencies: none. Scope: medium, agent-core contracts/resolver plus tests.
- [ ] Model identities, policy, binding, catalog and ephemeral result are validated.
- [ ] Default 70/inclusive boundary, null/empty allowlist, deny precedence,
      explicit unscored approvals and strict descending score/identity ties pass.
- [ ] Shuffled input and repeated interleaved application policies are deterministic.
Verification: add/run `tests/test_model_policy.py`; test 69.999/70/70.001,
NaN/Infinity/null, duplicates, cross-application isolation. Package build.

#### T2. Benchmark/provider ingestion and binding
Dependencies: T1. Scope: medium, shared source adapters/binding tests.
- [ ] Authenticated bounded requests normalize inventory and documented AA Coding
      Index, preserving stable IDs and attribution/provenance without secrets.
- [ ] Unique tested name/version/effort matching admits new recognized models;
      ambiguous effort/model, unknown capability or unsupported variant is pending.
- [ ] No maximum-score substitution, fuzzy match, alternate metric, or redirects.
Verification: `tests/test_model_catalog_sources.py`, realistic sanitized AA
records, 200/401/403/429/500, malformed/oversized payloads and effort variants.

#### T3. Shared cache and refresh command
Dependencies: T2. Scope: medium, trusted config/cache/CLI plus tests.
- [ ] One shared JSON configuration drives worker and refresh provider scope/root;
      finite [0,100] production threshold overrides application then default.
- [ ] Atomic last-valid cache survives concurrent/failed refresh; age bounds and
      auth failures fail closed; API timeout/response budgets enforced.
- [ ] `refresh --config` and `explain --config` emit bounded diagnostics and useful
      exit codes, never keys; no CWD-based repository environment loading.
Verification: `tests/test_model_catalog_refresh.py`, temporary cache plus HTTP
fixture, missing/null/blank/zero threshold, auth invalidation, interrupted writes.

Checkpoint A: T1-T3 tests and agent-core suite/build pass; review catalog identity
and quality mapping before integrating the paid execution path.

### 2.2 Availability And Execution

#### T4. One scoped availability store
Dependencies: T1. Scope: medium, store and tracker adapters plus tests.
- [ ] Persist transient failures/auth quarantine by endpoint, provider, credential
      scope/generation and model where applicable; no policy/index persistence.
- [ ] Successful refresh clears only matching scope auth quarantine; success
      cannot erase a newer failure; expired cooldown and rotation recover safely.
- [ ] Manual tracker semantics remain unchanged; discovery uses one injected store.
Verification: `tests/test_model_availability.py`, SQLite restart/concurrency,
scope isolation, credential generation, expiry and conditional-update tests.

#### T5. Process invocation honors discovery policy
Dependencies: T3,T4. Scope: medium, harness runner/composition plus tests.
- [ ] Explicit discovery mode consumes current resolved list, disables preferred/
      last-success and all-unhealthy bypass; zero eligible means zero billing.
- [ ] Check availability and persist failure/success through injected store.
- [ ] Resume ignores historic model/index; cost, cancellation, callbacks and
      existing failure eligibility/classification remain unchanged.
Verification: extend `test_harness_runner.py` and `test_harness_opencode.py`;
record actual submitted models and paid-attempt counts; manual regression suite.

#### T6. Reusable sessions and effort/config parity
Dependencies: T5. Scope: medium, sessions/OpenCode config plus tests.
- [ ] Reusable sessions use same scoped health reader/writer and order as process
      turns; all unavailable does not create a provider session.
- [ ] Every fallback config and request carry its exact matched effort/variant;
      unsupported effort fails before paid submission, without false rate limit.
- [ ] No manual-chain fallback in config builder; an active call/conversation is
      not switched on refresh, but a new invocation resolves again.
Verification: extend `test_harness_sessions.py`, `test_opencode_config.py` and
cross-entrypoint recording-transport tests; full lifecycle/cost regressions.

#### T7. Isolate benchmark credentials from agent launches
Dependencies: T3. Scope: medium, shared environment sanitization and launch tests.
- [ ] Strip canonical/legacy AA keys after all environment merges in both process
      and server launch paths, even caller-supplied environment overrides.
- [ ] Provider credentials continue to work; no AA secret enters config/log/report.
- [ ] Refresh-only secret-file separation is documented and launcher-testable;
      do not claim environment removal isolates worker-readable files.
Verification: synthetic sentinel tests in process/server launches and serialization;
no live secret printing or agent prompting to read credential files.

Checkpoint B: T4-T7 and full agent-core suite/build/enforcement pass; no policy
bypass via either entrypoint. Independent simplify/review before release work.

### 2.3 Consumer Adoption And Operations

#### T8. UTA execution integration
Dependencies: T5,T6,T7. Scope: medium, settings/harness/CLI bridge and tests.
- [ ] Batch and CI-repair use shared trusted config at execution/resume, not task
      model snapshots. Enforcement-only jobs remain independent of AA/catalog.
- [ ] Operator threshold override validated; target repo/task cannot override
      policy or select an excluded/preferred model. No task-schema migration.
- [ ] Resume after refresh uses new score order and retained identity health.
Verification: consumer harness/config bridge tests plus batch/repair regression,
Java/Python progress, budget, cost and callback tests. Test local agent-core build;
do not commit an unreleased dependency pin for deployment.

#### T9. CR execution integration
Dependencies: T5,T6,T7. Scope: medium, settings/harness wiring plus tests.
- [ ] Scan/fix worker entrypoints use shared resolution; no new request_json
      snapshot fields or legacy OpenCode-specific configuration proliferation.
- [ ] Resume uses current policy/catalog; unrelated feedback/learning/report and
      callback paths retain behavior.
- [ ] CR and UTA can select different lists/minimums from one catalog safely.
Verification: CR harness/config scan/fix/resume tests; full CR suite/build against
local shared package. Capture actual first/fallback model and effort.

#### T10. Daily launcher and operator guidance
Dependencies: T3,T7,T8,T9. Scope: medium shared launcher/template/usage docs.
- [ ] Daily 03:15 host-time cron launcher uses absolute config/interpreter,
      lock/no overlap, isolated refresh credentials, bounded failure status.
- [ ] Test launcher and worker read identical cache/provider scopes across restart
      and rotation; deployment instructions include ownership and timezone.
- [ ] Usage/README document default70/override, AA attribution, approved mappings,
      exclusion reasons, stale/auth recovery, manual refresh and rollback.
Verification: actual launcher against fixture endpoint; inspect parsed cron entry,
permissions, nonzero failure exit/status and preserved cache. No prod cron yet.

Checkpoint C: Both consumers and shared package pass full suites/builds and local
enforcement; catalog refresh-to-worker integration and secret isolation proven.

### 2.4 Release And Production Proof

#### T11. Live mapping and code-quality gate
Dependencies: T8,T9,T10. Scope: verification/evidence, no unrelated code.
- [ ] Fetch live AA/token-pool inventories and report matches, effort bindings,
      unsupported capabilities and score exclusions. Use already configured key
      without displaying it. Obtain explicit approval for unresolved bindings.
- [ ] Run tool-capable OpenCode smoke for admitted rollout bindings plus meaningful
      UTA/CR workflow checks; /models listing alone is not capability proof.
- [ ] Simplify and five-axis code review; fix findings with regression tests;
      every spec acceptance criterion has recorded evidence.
Verification: retain sanitized commands/results, reviewed diff and clean scoped
commits. Block release if mapping or invocation parity remains unverified.

#### T12. Release shared package and pin consumers
Dependencies: T11. Scope: package release metadata and consumer pins.
- [ ] Inspect latest release/version procedure; publish a new agent-core release,
      verify remote ref and installed package before updating consumer pins.
- [ ] UTA/CR use the released version, not local editable or snapshot dependencies.
- [ ] Repeat consumer regression/build/enforcement at exact released dependency.
Verification: git remote refs, package version, package install and tests; preserve
unrelated work. Record versions/commits, not guessed future release numbers.

#### T13. Idle-only deploy and real verification
Dependencies: T12. Scope: approved operational rollout and evidence.
- [ ] Re-read current remote deployment instructions; verify repair AND enforcement
      idle, install refresh job/secrets safely and populate cache before enabling.
- [ ] Deploy via git pull; verify production effective threshold/config source,
      catalog freshness, credential permissions and runtime model/effort.
- [ ] Verify real UTA repair and CR scan; restart/resume when safe proves persisted
      cooldown and fresh list. Rollback new invocations to manual on failure;
      never interrupt active calls to deploy or fabricate passed evidence.
Verification: sanitized report/task URLs, active model and effort, progress/cost,
refresh status, safe restart checks. Stop and report external blockers honestly.

## 3. Requirement Coverage

| Source | Requirement | Tasks |
| --- | --- | --- |
| Spec 4.1 | New qualifying model admitted without chain edits | T1,T2,T5,T11 |
| Spec 4.2 | Strict filters, threshold, unknown/ambiguous bindings | T1,T2,T3 |
| Spec 4.3 | Independent application policies | T1,T8,T9 |
| Spec 4.4 | Determinism, fresh resume, identity health | T4,T5,T6,T8,T9 |
| Spec 4.5 | Last-valid cache, expiry, fail closed | T3,T4,T5,T10 |
| Spec 4.6 | Manual fallback/billing regressions | T5,T6,T8,T9 |
| Spec 4.7 | Invocation/effort integration | T5,T6,T8,T9,T11 |
| Spec 4.8 | Full tests, builds and package consumers | T8,T9,T11,T12 |
| Spec 4.9 | Live schema/catalog verification | T11 |
| Spec 4.10 | Real production behavior, no disruption | T13 |
| Design 3.2 | Production threshold and effort matching | T2,T3,T6,T11 |
| Design 3.3/review 1 | No legacy ordering/cooldown bypass | T5,T6 |
| Design 3.4/review 2 | One health authority/auth recovery | T4,T5,T6 |
| Design 4.2/review 3 | Refresh-only credentials, env isolation | T7,T10,T13 |
| Design 3.5/review 4 | Shared refresh inputs/daily execution | T3,T10,T13 |
| Design 2.2 | No task-schema/list persistence changes | T8,T9 |
| Design 4 | Bounds, secure cache, diagnostics | T2,T3,T4,T7,T10 |
| Design 5.2 | Shared-first release, rollback, attribution | T10,T11,T12,T13 |

## 4. Risks And Stop Conditions

- Unknown benchmark/effort or tool capability: report pending bindings, never
  invent scores or enable unscored models without explicit approval.
- Services busy: defer operational restart, continue non-disruptive preparation.
- AA/source schema changed: preserve last-good evidence, fix parser tests first.
- Existing worktree edits: do not delete/stash or include them in these commits.
- Failed gates: investigate; do not lower gates to declare delivery successful.
- No target task/report supplied for final verification: choose existing safe
  test fixtures for local work; agree production test identity before triggering.

## 5. Changelog

- 2026-09-07: Initial dependency-ordered plan following approved spec/design review
  fixes. All implementation tasks pending; no deployment claimed.
- 2026-09-07: Implemented local shared selection/cache/health, both OpenCode
  invocation paths, consumer wiring, and refresh launcher. Initial full shared
  suite: 1,928 passed. Review hardening and internal HTTP compatibility added;
  final shared suite: 1,984 passed (two dependency deprecation warnings), wheel
  build passed, new selection package Ruff passed. No release or deployment performed.

## 6. Verification Ledger

- T1-T7: RED/GREEN tests cover policy, bounded source parsing, cache, availability,
  exact variant registration, process/session parity, and child secret removal.
- T8: UTA discovery-consumer tests: 13 passed against local agent-core source.
- T9: CR discovery-config tests: 35 passed against local agent-core source;
  earlier full CR regression: 412 passed before the last integration hardening.
- T10: 55 launcher/related tests passed, including real CLI fixture invocation;
  no cron job installed.
- T11 source check: production token-pool GET returned HTTP 200 and 42 models;
  AA parsed all 643 records (255 scored). User confirmed the existing internal
  HTTP endpoint is intentional; it was preserved exactly. See
  [verification ledger](verification-model-discovery.md) for failing enforcement
  evidence and remaining live tool/effort checks.
- Release gates still open: policy enforcement, final independent review, live
  mapping/inference proof, released package pins, and idle-only production rollout.
- T11 focused enforcement checkpoint: source adapter now passes 97.7273%
  coverage and 114/119 mutation; configuration passes 100% coverage and 13/13
  mutation. These target passes do not complete the whole-branch enforcement gate.
- T11 CLI checkpoint: automatic strict discovery now passes 96.875% coverage
  and 21/21 mutation; subprocess integration checks remain in the regression
  suite. Remaining target gates and live verification still block release.
