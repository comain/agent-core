# Checklist: Workflow Persistence and Common Capabilities

Plan: `docs/plan-workflow-persistence-and-common-capabilities.md`

## Phase 0: Baselines

- [x] T1 Pin and verify the agent-core 0.6.11 consumer pair.
- [x] T2 Freeze agent-core checkpoint and turn contracts.
- [x] T3 Freeze UTA operation, prompt, identity, and retention contracts.
- [x] T4 Freeze CR persistence, prompt, turn, and publication compatibility.

### Checkpoint A

- [x] Clean old-consumer/0.6.11 installs and suites pass.
- [x] Frozen IDs, bytes, DTO JSON, and ordered traces are reviewed.

## Phase 1: Native Workflow And Secure Runtime

- [x] T5 Yield the native LangGraph saver.
- [x] T6 Preserve invocation dispositions and public lineage deletion.
- [x] T7 Implement confined atomic artifact writes and verified reads.
- [x] T8 Add bounded namespace locking and exact deletion.

### Checkpoint B

- [x] Real saver start/resume/reuse/corrupt/delete tests pass.
- [x] No forwarding saver or checkpoint-table SQL remains.
- [x] Artifact security/fault/process-lock tests pass.

## Phase 2: Turn Execution And Diagnostics

- [x] T9 Define neutral sessions and move normalized turn DTOs.
- [x] T10 Surround every paid submission with monotonic cost accounting.
- [x] T11 Add the typed canonical turn executor.
- [x] T12 Make LangGraph `agent_turn` a projection adapter.
- [x] T13 Define bounded diagnostics contracts.
- [x] T14 Implement durable OpenCode session diagnostics.

### Checkpoint C

- [x] Direct and LangGraph executor traces match.
- [x] All adapters report cost/session refs for every paid candidate.
- [x] Diagnostics limits and sanitization gates pass.

## Phase 3: Prompt, Publication, And Core Release

- [x] T15 Materialize manifest-last prompt bundles.
- [x] T16 Close the Git publication outcome algebra.
- [x] T17 Add idempotent MR publication coordination.
- [x] T18 Release the breaking agent-core 0.7 contract.

### Checkpoint D

- [x] Human approves the 0.7 breaking API/removal diff.
- [x] Full core suites, build and clean-wheel imports pass; tag and remote ref are published.
- [x] Old consumers remain pinned to 0.6.11 until their migration begins.

## Phase 4: UTA Migration

- [x] T19 Pin UTA to 0.7 and use the native saver.
- [x] T20 Migrate checkpoint-first retention.
- [x] T21 Compose UTA typed turn contexts.
- [x] T22 Preserve result durability and cap-aware cost policy.
- [x] T23 Back operation artifacts with the secure store.
- [x] T24 Migrate UTA prompts to secure bundles.
- [x] T25 Persist and project neutral UTA session refs.
- [x] T26 Make `uta assess` a diagnostics consumer.
- [x] T27 Prove UTA recovery parity and update operations docs.

### Checkpoint E

- [x] UTA full/fault/E2E/package-dependency gates pass on released 0.7.x.
- [x] Java/Python normal, resumed, and completed-reuse canaries do not repeat turns.
- [ ] Matched-pair rollback to UTA plus core 0.6.11 is rehearsed.

Evidence (2026-08-24): UTA pinned agent-core 0.7.4; 2,021 tests passed
with 16 environment-gated skips, the 162-test recovery/language/standalone
matrix passed, Ruff and the package-dependency checker were clean with zero
cycles and zero concrete harness names. The remaining checkbox is an operator
rollback rehearsal, not an implementation task.

## Phase 5: CR Migration

- [x] T28 Pin CR to 0.7 and isolate legacy harness configuration.
- [x] T29 Add neutral session and cost schema.
- [x] T30 Implement neutral read/write compatibility.
- [x] T31 Build the CR turn-context factory and recorder bridge.
- [x] T32 Migrate reviewer and judge turns.
- [x] T33 Migrate feedback and retrospective turns.
- [x] T34 Migrate reviewer and judge private artifacts.
- [x] T35 Migrate retrospective private artifacts.
- [x] T36 Migrate CR prompts to manifest-last bundles.
- [x] T37 Migrate feedback-pattern publication.
- [x] T38 Project neutral sessions through reports and APIs.
- [x] T39 Prove CR parity, rollback, and update docs.

### Checkpoint F

- [x] CR schema/rollback matrix and all turn-family tests pass.
- [x] Artifact/prompt/publication security and idempotency gates pass.
- [ ] CR canaries and matched-pair rollback evidence are retained.

Evidence (2026-08-24): CR pinned agent-core 0.7.6; 373 tests passed and
compileall was clean. All four turn families, both artifact families, prompt
bundles, and publication run through the shared layer, and the frozen
compatibility contract still matches byte for byte. The remaining checkbox is
operator work — running `docs/canary-0.7.md` against real traffic and
rehearsing the matched-pair rollback — not an implementation task.

## Phase 6: Promotion And Compatibility Window

- [~] T40 Run the matched-pair cross-repository release gate (mechanical half
      done; disposition metrics need production traffic).
- [ ] T41 Complete production canary and soak evidence.
- [ ] T42 Gate the one-release compatibility cleanup.

Evidence (2026-08-24): all three repositories point at released agent-core
0.7.6. agent-core 1,551 passed and compileall clean; UTA 2,021 passed with 16
environment-gated skips against the released wheel; CR 373 passed against the
released wheel rather than an editable checkout. The mismatched pair (UTA 0.7.4
pin, 0.7.6 installed) refused to start before the pin moved, which is the
matched-pair rule demonstrating itself. What T40 still needs is the disposition
and spend metrics, which only production traffic produces — that is T41.

### Checkpoint G

- [ ] Every plan coverage row has passing evidence.
- [ ] Full suites, builds, dependency scans, docs, tags, and pins pass in all repos.
- [ ] No Critical/Important review finding remains.
- [ ] Legacy cleanup remains gated until its separate approval/window completes.

## Non-Jira Release Note

- Jira issue: not applicable by the approved spec.
- Release-approval evidence: not applicable.
- Human approval is still required at the agent-core 0.7 release checkpoint and
  before any production promotion or later compatibility-column cleanup.
