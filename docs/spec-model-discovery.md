# Automatic Model Discovery And Application Policy

Status: approved; initial Coding Index threshold confirmed as 70

Non-Jira tooling work. Owner: agent-core. Consumers: UTA and CR.

## Contents

1. Background and scope
2. Selection requirements
3. Reliability and compatibility
4. Verification and acceptance criteria
5. Delivery and decisions
6. Changelog

## 1. Background And Scope

### 1.1 Current behavior

The harness parses an explicitly ordered provider chain. The provider models
endpoint filters availability; it does not add new models. Health filtering
removes unavailable candidates. UTA snapshots its configured chain at task
creation. This couples model adoption to manual configuration updates.

### 1.2 Intended outcome

Automatically discover provider models and admit coding-capable models using
Artificial Analysis scores and explicit application policy. UTA and CR must
share implementation, while retaining independent policy. No per-task model-list
snapshot is required; resumed tasks resolve the current eligible list.
Java/Python and individual workflow stages must not implement model selection.

### 1.3 Non-goals

No DeepSWE integration, benchmark execution service, billing-based fallback,
new stall detector, or change to provider-error classification. Do not infer
coding capability from model-name size adjectives or creation dates.

## 2. Selection Requirements

### 2.1 Discovery and benchmark source

- Fetch provider models through their authenticated OpenAI-compatible models
  endpoints using existing credential resolution.
- Artificial Analysis is the only external quality source. Use its documented
  models API and coding-index field, not its general intelligence index.
- Preserve stable benchmark model/creator IDs, source, retrieval time, score
  type, model configuration identity, and a snapshot digest. Record methodology
  version when supplied; never invent a version absent from upstream data.
- Match provider-qualified runtime IDs to benchmark identities with explicit,
  auditable aliases where required. Ambiguous aliases remain unscored. Do not
  conflate variants or reasoning efforts or use fuzzy substring matching.
- A missing/null score is unknown, never zero. Malformed, nonfinite, or
  out-of-range scores cannot admit a model.

### 2.2 Shared eligibility and per-application policy

- Shared eligibility excludes known non-coding/non-tool-capable models and
  shared denials. Unknown capability requires validated support or approval.
- Application allowlist is optional; absent means no additional restriction,
  explicitly empty means no models allowed. Entries match provider-qualified
  IDs with documented, case-sensitive glob semantics.
- Application denylist always wins; applications cannot undo shared denials.
- Each application can set its coding-index minimum.
  A supplied production-owned threshold overrides the application's configured
  minimum; otherwise use that application minimum, or 70 when neither is supplied.
  Thresholds must be finite numbers in [0, 100]; zero is valid. Invalid supplied
  values fail configuration validation instead of silently falling back. Only
  operator-owned service configuration can override this policy, never a target
  repository, prompt, or task request.
  The initial default for UTA and CR is Coding Index >= 70 (inclusive, on the
  published 0-100 scale). Compare the unrounded score for the exact evaluated
  model/version and configured reasoning effort; do not take the maximum across
  configurations. This is not Intelligence Index or Coding Agent Index.
  Discovery mode has no preferred-model ordering override.
- Unscored models remain pending by default. Explicit unscored-model approval
  may admit those identities, but cannot override a denylist or a known score
  below threshold. Allowlisting alone is not unscored approval.
- Apply shared eligibility, application allow/deny rules, score admission, and
  existing health checks. De-duplicate by provider-qualified runtime identity.
- `policy.ranking_strategy` selects `price-efficient` (default, the former
  `best-efficient` name still loads) or `best-score`. `price-efficient` ranks by
  discounted price, then effort, then score.
- `providers[].pricing_discount` (default 1, range [0,1]) is the fraction of
  published list price paid through that endpoint; 0 makes a pool's models free
  and therefore ranked by efficiency alone. List prices come from OpenRouter's
  public API on the daily refresh; an unknown price ranks after known prices.
- `price_weights` (default `{"prompt": 10, "completion": 1}`) blends the published
  input and output rates into the one price used for ranking.
  Best-score sorts by descending unrounded score then identity. Best-efficient
  sorts by effort (none, minimal, low, medium, high, xhigh, max/default), then
  descending score and identity. Unknown effort labels sort after known labels.
  Default/empty effort is max for ranking only, never provider configuration.
  Scored empty/default rows, and every xhigh/max binding, are `prohibited_effort`
  and are not selected. Unscored empty-effort identities can still use explicit
  unscored approval.
  Ranking considers all approved exact model/effort bindings for each runtime
  model. Apply eligibility and provider-option validation per variant first,
  rank all eligible variants, then retain the first per runtime model. Never
  deduplicate before ranking or borrow a different effort's score.
  Explicitly approved unscored models follow every scored model, ordered by identity.
  First attempt uses the first available healthy member; fallback follows this order for the
  current invocation without retrying an already attempted model in that invocation.
- No eligible models produces a typed, actionable error listing exclusion
  reasons. Never revert to the unfiltered manual chain in discovery mode.

### 2.3 Task consistency and observability

- Resolve the current catalog and application policy when starting/resuming a
  task or opening a new agent invocation. Keep only an in-memory candidate list
  for that invocation; do not persist task model lists or numeric fallback indices.
- Persist availability/cooldown state by credential scope and provider/model
  identity so refresh, resume, and process restart do not retry known unavailable
  models before recovery. Preserve current failure classification and expiry rules.
- Do not switch a running LLM call merely because a catalog refresh occurred.
- Batch, CI repair, and CR invocation consume the same resolution contract.
- Expose why a discovered model was accepted, denied, unmatched, or unhealthy.
  No credentials or authentication headers in snapshots, logs, or reports.
- Emit Artificial Analysis attribution wherever its scores are displayed.

## 3. Reliability And Compatibility

### 3.1 Refresh behavior

Refresh daily (every 24 hours) outside LLM turns/repair startup. Use bounded HTTP timeouts, response
sizes, validation, concurrency control, and atomic last-known-good persistence.
On timeout/429/5xx/invalid data preserve the valid cache; do not replace it with
an empty catalog. Cache identities must isolate credential/provider scopes.
Authentication failures must be visible and must not expose another scope's data.

Distinguish refresh age from maximum acceptable staleness. Beyond the configured
staleness bound, new discovery-mode tasks fail clearly rather than trusting an
indefinitely stale quality decision. An already running invocation is not interrupted.

### 3.2 Backward compatibility

Discovery is opt-in. Manual mode preserves current selection/fallback behavior.
Discovery-mode resume uses current policy even for an older task; historical
selected-model fields must not override it. No task-schema migration or new task
snapshot fields are required. Persist only shared availability state and caches.

Application policy must not leak through shared mutable settings or caches.
The benchmark API key is server-side only and separate from provider tokens.

## 4. Verification And Acceptance Criteria

1. A new discovered, uniquely matched, qualifying model joins a new task without
   editing the manual provider chain.
2. Denials, explicit-empty allowlist, unknown scores, approved unknown models,
   threshold boundaries, and ambiguous aliases have deterministic tested outcomes.
3. Two application policies applied to the same catalog produce independent
   chains; calls in either order cannot contaminate each other.
4. Upstream order changes cannot reorder equal inputs. Resume after catalog/
   policy refresh uses the newly ranked list while honoring stored health
   state by identity, never by an index into the old list.
5. Fetch failures preserve valid evidence; stale/missing evidence and empty
   eligibility produce clear errors, never an unauthorized fallback model.
6. Legacy manual selection and provider fallback regression tests pass. New
   logic does not classify billed activity or no-output differently.
7. Cross-layer tests verify fresh resolution, resume, harness model registration,
   and first/fallback model invocation, not just the selector in isolation.
8. Package build and relevant full suites pass for agent-core and consumers.
9. Authenticated live fetch verifies actual score availability/schema and alias
   coverage for token-pool. Report unmatched models without inventing scores.
10. Deployment verifies provider/model identity and policy evidence on real new
    tasks; active tasks are not interrupted to activate this feature.

## 5. Delivery And Decisions

Release shared agent-core first; consumers must pin that released version before
rollout. Update configuration/usage docs, including key setup, score attribution,
policy examples, diagnostics, and rollback to manual mode for new tasks.
Deploy only after checking both enforcement jobs and repair work are idle.

Decisions needed before activation (not hardcoded guesses):
- Initial allowed/denied/unscored-approved models for each application.
- Runtime reasoning-effort mappings for initial admitted models.

Authenticated local Artificial Analysis fetching has been verified using the
existing .env credential, without exposing its value. Production credential
availability and consumer configuration still require rollout verification.

The design must specify refresh/staleness defaults, concrete cache storage,
alias format, configuration ownership, and how discovered models are registered
with the harness. A successful models endpoint alone is not proof that a model
can perform tool calls or that its coding quality meets the policy.

Source: https://artificialanalysis.ai/api-reference (verified 2026-09-07).
The documented API has stable IDs and requires an API key and attribution.

## 6. Changelog

- 2026-09-07: Daily refresh approved; explicit production-owned threshold takes
  precedence over application minimum and default 70. Repository/task overrides
  are not permitted.

- 2026-09-07: User removed per-task model snapshots. Resume uses current catalog
  and policy; only availability/cooldown state persists by scoped model identity.

- 2026-09-07: User requires strict descending score ordering, replacing preferred
  model precedence; deterministic identity ties and approved unscored models last.

- 2026-09-07: User approved the spec and set Coding Index >= 70 for the initial
  policy, superseding the discussed 47 Intelligence Index and 51 Coding Index
  thresholds. Confirmed authenticated API access and configuration-specific scores.

- 2026-09-16: User asked for a provider-level pricing discount (token pool 0,
  OpenAI 1) and a price-first ranker so pool models stay efficiency-ranked while
  full-price providers lead with their cheapest eligible model.
- 2026-09-07: Initial spec from approved non-Jira workflow and agreed automatic
  discovery/application filtering contract. Artificial Analysis only; no DeepSWE.
- 2026-09-15: User reclassified unsuffixed/default effort as max for ranking
  only, so provider-default Flash records are not preferred over measured
  low/medium/high variants. Bound effort and provider options are unchanged.
- 2026-09-07: Added user-approved best-efficient default and configurable best-score
  alternative. Default effort originally ranked as medium; superseded 2026-09-15.
- 2026-09-07: Corrected fixed-effort selection gap: admit and rank all registered
  variants before choosing one per model, preserving exact scores and capability checks.
