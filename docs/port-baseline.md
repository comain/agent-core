# Port Baseline

Recorded at the start of the harness extraction (Task 1 of the dev-flow-agent
roadmap). Required by the design so drift against UTA is measurable at Task 9
migration time, and so acceptance criterion AC5 ("sibling repos unchanged") is
verifiable — both sibling repos already carried unrelated local edits before
this work began, so "clean" is defined against this snapshot, not against zero.

## Source of the port

| Field | Value |
|---|---|
| Repo | `unit-test-agent` (sibling checkout) |
| Branch | `main` |
| Commit | `0d70115783fc4cd075a26e04909585ba2f34b288` |
| Ported path | `uta/opencode/` (9 modules, 3,373 LOC) |
| Ported tests | 11 files, 3,370 LOC |

## Pre-existing dirty state — unit-test-agent

```
 D remote-deployment-target.md
 M todo.md
?? doc/
?? remote-deployment.md
```

## Pre-existing dirty state — cr_plugin

At commit `934d5ece50433a1805d9389dff9f18b8e13782e1`:

```
 M src/cr_agent/review_v2/storage.py
 M tests/test_review_v2_storage.py
?? remote-deopy.md
?? remote-deployment.md
```

## AC5 verification rule

`git status --porcelain` for each sibling repo must match the corresponding
block above **exactly** at the end of the port. Any additional entry means this
task modified a sibling repo, which the spec forbids.

## Toolchain

| Field | Value |
|---|---|
| Python | 3.9.6 (matches UTA's venv, so the port runs against the same interpreter the oracle was written for) |
| pip / setuptools | upgraded to 26.0.1 / 82.0.1 in agent-core's venv — the venv-seeded 21.2.4 / 58.0.4 predates PEP 621 metadata support and installed the package as `UNKNOWN` |

## Python floor raised to >= 3.11 (2026-08-06)

The original `>=3.9` was inherited from the source repo's declared
`requires-python`. Checking what the consumers actually run showed that floor
matched nothing deployed:

| Consumer | declares | dev venv | **production** |
|---|---|---|---|
| unit-test-agent | `>=3.9` | 3.9.6 | **3.11.15** (beta 3.12.9) |
| cr_plugin | `>=3.9` | 3.13.3 | — |
| Corbell | `>=3.11` | 3.11.12 | **3.12** |
| dev-flow-agent | — | 3.13.3 | — |

Two of the three declared floors are stale metadata. The only surviving 3.9
references are UTA's local dev venv and one instruction in cr_plugin's
`operation.md` (`python3.9 -m venv .venv`).

The floor is now **3.11**, matching the lowest interpreter any consumer runs in
production. Verified green on 3.11.12, 3.12.10, and 3.13.3.

The cost of the old floor was not hypothetical: it ruled out LangGraph 1.x
(which requires >= 3.10) and would have forced the human-gate workflow bridge
out of agent-core into a single consumer, even though gates are cross-cutting.
