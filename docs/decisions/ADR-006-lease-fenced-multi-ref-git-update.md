# ADR-006: Lease-Fenced Multi-Ref Git Update

## Status

Accepted

## Date

2026-08-25

## Context

ADR-004 put scoped Git publication (clone, mutate, push a feature branch,
ensure MR) in agent-core. Corbell publishes an in-repo wiki with an atomic
two-ref `--force-with-lease` (`docs` plus a lease marker ref) and no merge
request.

Using `PublicationCoordinator` would drop fencing and turn the wiki into a
code-review artifact.

## Decision

Add `update_refs_with_lease(workspace, path, *, ref_updates, lease, …)` under
`agent_core.git`. `path` is the checkout to push **from** (Corbell:
`PreparedProject.repo_dir`). It does not take `repo_url` or call `path_for`.

It runs **one**
`workspace.execute(path, "push", "--atomic", *--force-with-lease=…, "origin", *sha:ref, check=True, is_cancelled=is_cancelled)`
(cancel, timeout, process-group kill). Empty expected SHA means first claim
and must be explicit.

If the remote rejects `--atomic` or any lease mismatches, the helper raises
and **must not** leave a subset of refs updated. Tests use a temp repo with
two refs.

It does **not** clone, invent a feature branch, or open an MR. Raw `env`
subprocess is rejected because it bypasses workspace cancel/timeout.

Corbell keeps dual-ref checkout, generated path allowlist, fencing-token
meaning, and `docs/spec` layout.

## Alternatives Considered

### Teach PublicationCoordinator a "no-MR, multi-ref" mode

- Advantage: one publisher type.
- Rejected: the coordinator's lifecycle is isolated clone + one publish
  branch. Parameterizing that into a long-lived cache would encode two
  products poorly.

### Leave multi-ref push in Corbell forever

- Advantage: no new core API.
- Rejected: the mechanic is generic (atomic leased ref update). Spec R1
  says promote it.

## Consequences

- ADR-004 still owns MR-oriented publication. This is a sibling git
  primitive, not a replacement.
- Tests use a temporary git repo with two refs, never a live GitLab project.
