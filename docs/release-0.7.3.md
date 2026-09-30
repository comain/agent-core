# agent-core 0.7.3 — safe first-publication fetch handling

`GitScopedPublisher` now continues with first-branch publication only when Git
explicitly reports that the requested remote ref is absent. Authentication,
transport, and repository failures remain `PushConflictError` failures before
push instead of being mistaken for a new branch.

No public DTO or persisted format changes from 0.7.2.
