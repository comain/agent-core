# Agent-Core Model Discovery Detail

Status: approved direction with review fixes incorporated. Contracts: [overview](design-model-discovery.md).

## 1. Changes And Flow

Add `model_selection/` containing contracts, pure resolver, HTTP source adapters,
file cache, and refresh/explain CLI. Reuse httpx, validation conventions, and
existing provider credential parsing; no new HTTP framework or global policy.
AA uses HTTPS only; providers accept trusted configured HTTP or HTTPS endpoints.
Preserve the approved internal HTTP endpoint without transport rewriting.
Adapters produce catalog -> resolver produces ephemeral selection -> harness consumes it.

Extend harness composition with a selection resolver and per-member execution
settings. No application/language names in shared code.
`harness/opencode.py` must honor current selection in `open_session` and
process `run_turn` paths. `harness/config.py` registers selected models/variants.
`tiered_router.py` remains the legacy manual resolver and manual-mode health source;
do not append auto-discovered models to a mutable global provider chain.
Discovery injects the single scoped AvailabilityStore described in overview 3.4
into readers/writers, including `harness/runner.py`, `harness/sessions.py`, and
config building. Explicit discovery mode disables last-success/preferred ordering
and all-unhealthy retries. Process/server launchers remove AA keys after all
environment merges. Shared config parsing and refresh launcher follow overview 3.5.

## 2. APIs And Persistence

Public operations: `resolve_selection(catalog, bindings, shared_policy, app_policy)`
returns ephemeral selection and diagnostics; cache loading and refresh are separate IO.
Contracts are defined once in the overview. No consumer task schema changes.

Resolve credentials at runtime and map effort through adapter-owned variants.
Current typed `variant_options` entries are keyed by runtime identity and require
only `reasoningEffort`: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`.
Reject arbitrary SDK fields. Explicit custom-provider registration must match the
binding's effort; other adapters/effort mechanisms remain pending until exact
mappings are implemented and tested.
Persist only operational availability/cooldown state in the SQLite cache described
in the overview, using identity rather than fallback indices. An in-memory
attempted-model set bounds each invocation. Reject preferred-model bypasses.

## 3. Risks And Verification

Use bounded refresh and failure rules from overview section 4. Integration tests
cover both harness entrypoints and config isolation. Preserve global legacy
settings behavior only for manual mode; new policy resolution is instance-scoped.
Test cross-application concurrency, cache atomicity, legacy fallback and package
installation. No per-provider code generation from external metadata.

## 4. Changelog

- 2026-09-07: Initial shared implementation detail; not implemented yet.
- 2026-09-07: Replaced task snapshots with fresh resolution and persistent availability.
