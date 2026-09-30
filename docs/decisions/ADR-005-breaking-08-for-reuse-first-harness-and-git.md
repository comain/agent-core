# ADR-005: Breaking 0.8.0 For Reuse-First Harness And Git Behavior

## Status

Accepted

## Date

2026-08-25

## Context

Spec-generator-agent (Corbell) still ships an independent OpenCode harness and
in-process git locks. The adoption spec requires Corbell to call public
agent-core APIs (R1) and allows a breaking release when additive knobs would
leave a long-lived product facade (R2).

These behaviors cannot be optional defaults without changing what existing
callers observe:

1. `OpenCodeHarness.run_turn` swallows `session_id`; `SessionAffinity` calls
   `process.run_turn` and never walks fallback. Corbell needs continue on the
   bound model and bootstrap on failover.
2. `GitWorkspace.repo_lock` is in-process only; Corbell's scheduler and UI
   are multi-process.
3. `run_turn_with_fallback` does not skip unhealthy models when `models=` is
   passed (and `OpenCodeHarness` always passes it). Timed windows already live
   on `ModelHealthTracker` (120s / 15m / 10m) and must become 60s / 15m / 5m
   timeout plus skip-on-`models=` and prefer-last-success, process-lifetime.

UTA and CR are on `0.7.6`/`0.7.9`. Mixed old-consumer / new-core was already
unsupported for the `0.6.11` → `0.7.0` break.

## Decision

Ship **agent-core 0.8.0** with these behavior changes:

- `OpenCodeHarness.run_turn` accepts `message` xor `prompt_file` and forwards
  `session_id` / `delivery` / `title` / `pure` / `env` / `project_dir` /
  `bootstrap_message`. First candidate may continue; later candidates are a
  **fresh** session. Later candidates use `message=bootstrap_message` **iff
  it is set**; otherwise they keep the original `message`/`prompt_file` and
  still drop `session_id`. Writers always pass bootstrap. Planner/keyword
  turns do not. Tests: two-candidate with bootstrap (writer) and without
  (planner file/message).
  `SessionAffinity.run` calls the **harness**. Affinity is **not** inside
  `execute_agent_turn`. `run_turn_with_fallback` gains `session_id` (first
  only) and `bootstrap_message`.
- `GitWorkspace.repo_lock` uses POSIX `fcntl.flock` plus `RLock`. Windows is
  unsupported for this lock.
- `ModelHealthTracker` + `run_turn_with_fallback`: skip unhealthy even when
  `models=` is set; rate_limit 60s; timeout 5m; prefer last success; if all
  cooling, try anyway. No second cooldown table on the function.

`run_harness_node` maps a `str` prompt to `message=` (additive for Path
callers; 0.8 because harness session forwarding is breaking).

Stdin, `--title`, per-turn `pure`, placeholder expander, and
`update_refs_with_lease` are additive on the same tag.

Corbell pins `0.8.x`. UTA and CR remain on last `0.7.x`. Mixed pairs are
unsupported. The overview break table lists who moves.

## Alternatives Considered

### Additive 0.7.10 only, wrap the three behaviors in Corbell

- Advantage: UTA/CR can install the tag without reading release notes.
- Rejected: that is the facade R1 forbids, and the 2026-08-07 assessment's
  "leave Corbell alone" outcome.

### Break execute_agent_turn into a new type and delete Path prompts

- Advantage: one prompt shape.
- Rejected: UTA/CR file prompts work; widening the callable to `Path | str`
  is enough.

### Force UTA/CR onto 0.8 in this program

- Advantage: one pin everywhere.
- Rejected: spec does not require it; Corbell is the consumer in scope.

## Consequences

- README must list the three breaks.
- Core tests that assumed swallowed `session_id`, unlocked multi-process
  clones, or immediate retry of an unhealthy model while `models=` is set
  must change. Tracker-across-two-turns is required.
- A 0.7.x consumer that accidentally installs 0.8 may wait on flock or skip
  a cooling model; pin tests are the guard.
