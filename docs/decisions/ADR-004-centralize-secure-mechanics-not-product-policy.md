# ADR-004: Centralize Secure Mechanics, Not Product Policy

## Status

Accepted

## Date

2026-08-20

## Context

UTA and CR both materialize private artifacts and prompts, track agent sessions,
and publish scoped Git changes. Agent-core already contains partial implementations
for artifacts, prompt rendering, scoped Git publication, and GitLab access, but
products still duplicate atomic file safety, reference manifests, provider-named
session handling, isolated workspace lifecycle, and publication result mapping.

Moving all product logic into agent-core would create an equally harmful boundary:
UTA and CR have different task schemas, policies, evidence, retry behavior,
retention eligibility, and domain outputs.

## Decision

Agent-core will own reusable mechanics with neutral contracts:

- secure atomic artifact storage with bounded reads, exact namespace layouts, and
  a fixed striped namespace-lock set;
- reproducible manifest-last prompt bundles that also work in mixed directories;
- ordered neutral session references for all fallback candidates and optional
  harness diagnostics with typed sanitized signals;
- isolated scoped Git publication with distinct base/publish branches and
  idempotent forge/MR ensure behavior, including a verified
  `REUSED_PUBLISHED` retry state after push-before-MR failure.

Products will own:

- artifact and prompt content, identity, disclosure, and retention eligibility;
- templates and input projections;
- task status, persistence, accounting, and reconciliation;
- publication path policy, branches, content, messages, project, and credentials;
- diagnostic presentation and product decisions based on diagnostics.

The full UTA operation ledger will not move until another product demonstrates
the same reconciliation requirement.

## Alternatives Considered

### Leave every product wrapper in place

- Advantage: no coordinated release.
- Rejected: security and failure semantics continue to drift, and future products
  must choose which copy to imitate.

### Move UTA's full workflow persistence into agent-core

- Advantage: fewer UTA files.
- Rejected: it would encode UTA fingerprints, phase attempts, task rows, and
  retention policy in a supposedly product-neutral library.

### Create a new fourth shared repository

- Advantage: could isolate filesystem/Git utilities from agent execution.
- Rejected: these mechanics already extend agent-core public capabilities and do
  not justify another release/dependency boundary in this iteration.

## Consequences

- Agent-core gains stronger reusable primitives and a larger public contract.
- UTA and CR wrappers become thin policy adapters rather than alternate mechanics.
- Independently distributed enforcement remains free of agent-core.
- Coordinated agent-core 0.7 release and matched consumer tests are required; old
  consumers remain pinned to 0.6.11.
- Future promotion of operation reconciliation requires evidence from a second
  consumer and a new ADR.

## Links

- Spec: `docs/spec-workflow-persistence-and-common-capabilities.md`
- Overview: `docs/design-workflow-persistence-and-common-capabilities.md`
