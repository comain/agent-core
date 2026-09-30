# Model Discovery Development Workflow

Status: approved

Non-Jira tooling work, approved by the user on 2026-09-07. Canonical documents
live in agent-core; UTA and CR are consumers. Approval covers this workflow,
not a claim that its later verification or release gates have passed.

1. [x] Inspect the current provider chain and agree on shared/application scope.
2. [x] Spec: write requirements and obtain approval; Coding Index >= 70 confirmed.
3. [x] Design: document shared contracts, consumer integration, and rollout.
4. [x] Design review: resolve executable-policy and compatibility gaps.
5. [x] Plan: trace every acceptance criterion to an implementation/test task.
6. [ ] Build: failing tests first, implementation, regression tests, package build.
7. [ ] Verify live Artificial Analysis and token-pool integration without exposing keys.
8. [ ] Simplify and conduct five-axis code review; resolve findings.
9. [ ] Release agent-core, then update and verify consumer dependency pins.
10. [ ] Deploy only when affected services are idle; verify real tasks and rollback.

Authenticated Artificial Analysis fetching verified locally. Initial threshold
is Coding Index >= 70. Remaining release decisions include initial application
model policies, reasoning-effort mappings, and production credential verification.
Preserve unrelated UTA uv.lock and runtime artifacts.

Four Important findings and follow-up permissions correction approved and
addressed. User approved plan-model-discovery.md; implementation is in progress.
