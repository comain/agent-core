# Todo: Spec-Gen Agent-Core Adoption

Plan: `docs/plan-spec-gen-agent-core-adoption.md`
Status: complete 2026-08-26; ready for ship review

## Phase A — agent-core harness

- [x] T1 Process knobs (delivery, title, pure, env, project_dir)
- [x] T2 Fallback: session_id first, bootstrap iff set
- [x] T3 ModelHealthTracker windows + skip on models=
- [x] T4 opencode adapter forwards knobs; products use HarnessSpec
- [x] T5 SessionAffinity model-bound, wraps harness
- [x] T6 execute_agent_turn / run_harness_node str prompts
- [x] T7 agent_turn parse json + confined prompt files

## Checkpoint A

- [x] T1–T7 green; human review

## Phase B — git, placeholders, tag

- [x] T8 POSIX flock on repo_lock
- [x] T9 update_refs_with_lease --atomic
- [x] T10 render_placeholders
- [x] T11 Version 0.8.0 + README break list

## Checkpoint B

- [x] Full agent-core pytest; tag v0.8.0; UTA/CR stay on 0.7.x

## Phase C — Corbell pin and daemon

- [x] T12 Pin Corbell to v0.8.0 + pin test
- [x] T13 TaskDaemon + DaemonPorts
- [x] T14 git_auth → identity
- [x] T15 Core controls for stop/cancel

## Checkpoint C

- [x] Pin + scheduler + cancel green

## Phase D — graphs and cleanup

- [x] T16 context-prepare WorkflowSpec
- [x] T17 spec-docs-service writer wave + LWW page_status
- [x] T18 write_page + lane sandbox via HarnessSpec.options
- [x] T19 Outer spec-docs + atomic publish
- [x] T20 Delete OpenCode copies
- [x] T21 Usage/README canaries

## Checkpoint D

- [x] Corbell pytest + source-boundary rgs; ready to ship
