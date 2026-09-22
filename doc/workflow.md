# Dev Workflow: OpenCode 2.x harness support

Jira: none
Iteration: 1
Status: approved
Approved-by: user 2026-09-21

## Steps

- [x] incremental-implementation — dual-stack OpenCode 1/2 runtime, drop Cursor plugin
- [x] test-driven-development — prove CLI flags, HTTP `/api`, native config, no plugin bootstrap
- [ ] git-workflow-and-versioning — commit the feature branch
- [ ] shipping-and-launch — verify cr_plugin beta against OpenCode 2.x

# Dev Workflow: Bounded Dynamic Fan-out

Iteration: 1
Status: approved
Approved-by: user 2026-08-10

## Steps

- [x] incremental-implementation — finish bounded fan-out and migrate cr_plugin review_and_judge
- [x] test-driven-development — verify concurrency, join, failure, cancellation, and regressions
- [x] code-review-and-quality — review both repositories
- [x] code-simplification — remove unnecessary complexity without changing behavior
- [x] git-workflow-and-versioning — commit and push agent-core, then cr_plugin
- [x] shipping-and-launch — verify remote refs and report final test evidence

# Dev Workflow: Expose Configured Harness API

Iteration: 2
Status: approved
Approved-by: user 2026-08-12

## Steps

- [x] api-and-interface-design — define an implementation-neutral harness specification
- [x] test-driven-development — prove configured OpenCode and future agents share one API
- [x] incremental-implementation — move built-in adapter construction behind agent-core
- [x] code-review-and-quality — review compatibility, configuration ownership, and the full diff
- [x] git-workflow-and-versioning — commit and push agent-core before its consumer

# Dev Workflow: Share Prompt Construction

Iteration: 3
Status: approved
Approved-by: user 2026-08-12

## Steps

- [x] api-and-interface-design — preserve the prompt artifact contract with an additive API
- [x] test-driven-development — prove deterministic prompt inputs and strict template validation
- [x] incremental-implementation — expose the minimal reusable prompt-writing option
- [x] code-review-and-quality — review compatibility, reproducibility, and API scope
- [x] git-workflow-and-versioning — leave reviewed changes ready for a user-directed commit

# Dev Workflow: Stream Safe Agent Progress

Iteration: 4
Status: approved
Approved-by: user 2026-08-12

## Steps

- [x] api-and-interface-design — define one neutral progress callback for every harness
- [x] security-and-hardening — expose only allowlisted progress on the public stream
- [x] test-driven-development — prove sanitization, persistence, resumption, and UI wiring
- [x] incremental-implementation — connect runtime events to CR model turns and pages
- [x] observability-and-instrumentation — preserve task and stage correlation without sensitive payloads
- [x] code-review-and-quality — review concurrency, lifecycle, security, and compatibility
- [x] git-workflow-and-versioning — leave reviewed changes ready for a user-directed commit
