# ADR-001: Own Neutral Harness Lifecycle In Agent-Core

## Status

Accepted

## Date

2026-08-20

## Context

Agent-core already abstracts harness selection, turns, sessions, fallback,
progress, cancellation, and cleanup. UTA nevertheless imports
`OpenCodeProcess`, `OpenCodeAuthClient`, and `generate_opencode_config` to
prepare repositories, probe readiness/authentication, and bootstrap project
context. Those calls make a product branch on the selected agent and prevent a
Pi or future harness from working through configuration alone.

The missing capabilities belong to the provider adapter: only the harness
knows whether it needs workspace configuration, how readiness/authentication
is checked, and how a project bootstrap is executed.

## Decision

Agent-core will add optional, implementation-neutral workspace preparation,
readiness, and bootstrap protocols plus public helper functions.

OpenCode will implement those protocols using its existing configuration,
auth/process/session, and cleanup internals. Provider-specific results and
errors will be normalized before returning to a product. Products will decide
when preparation/readiness/bootstrap is needed and will retain ownership of
product prompts, policies, and harvested artifacts.

Preparation defaults to no-op for a harness that needs none. Readiness requires
either an implementation or an explicit declaration that no external
readiness check is required. Bootstrap is optional and reports unsupported
rather than pretending success.

Agent-core may depend on concrete agent/harness bindings such as OpenCode or
Pi. It must not depend on Java/Python product generation or enforcement
bindings.

## Alternatives Considered

### Keep UTA wrappers around concrete OpenCode APIs

Rejected because the wrapper would still encode provider configuration, auth,
error parsing, and bootstrap semantics in the consumer. Adding Pi would require
another UTA branch.

### Make lifecycle methods mandatory on `Harness`

Rejected because existing and third-party harnesses that only run turns would
break. Optional runtime-checkable protocols preserve compatibility while
making unsupported capabilities explicit.

### Probe readiness automatically before every turn

Rejected because it adds latency/provider traffic per turn and creates new
failure points in durable replay. Readiness is an explicit application-startup
operation.

### Put project-summary harvesting in agent-core

Rejected because deciding which summary is authoritative and harvesting UTA
artifacts is product policy. Agent-core owns only neutral execution.

## Consequences

1. UTA can remove concrete OpenCode imports and select agents through config.
2. OpenCode/Pi-specific lifecycle behavior remains in the owning adapter.
3. Third-party harnesses remain source compatible.
4. Consumers receive normalized readiness states rather than raw provider
   payloads.
5. Products must explicitly handle unsupported readiness/bootstrap.
6. Agent-core gains additive tests and a release that must precede the UTA
   consumer update.
