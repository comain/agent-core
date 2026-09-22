# Behavioural Parity Evidence

Task 1 acceptance criterion: prove the ported harness behaves like the source
harness, beyond "the ported tests pass".

**Status: both phases pass.** Deterministic parity and live turn parity were run
on 2026-08-06.

## Why the test suite alone is not enough

The 8 ported test files stub the model. They prove each module behaves as its
own tests expect; they cannot prove the *assembled* harness produces the same
observable behaviour as the original. A port defect that changed how the
subprocess is configured would pass every one of them.

## Phase 1 — Deterministic parity: PASSED

`tools/parity_check.py` drives both harnesses with identical configuration and
compares the three things the harness actually produces before any model is
contacted:

| Compared | Result |
|---|---|
| Generated `opencode.json` | identical |
| Spawn command (`_build_cmd`) | identical |
| Subprocess environment (`_build_env`) | identical, apart from one deliberate addition |

The single expected difference is `AGENT_SERVICE_PYTHON_BIN`, added by
deviation D5 alongside the legacy name, and the check asserts it is present
rather than merely tolerating it.

### What this specifically proves about D2 and D3

The first run of this check **failed**, and the failure was informative. The
source harness allowed a set of external directories the ported harness did
not:

```
~/group-a/**            (from the consumer's opencode_external_dirs.json)
~/group-b/**             (same)
~/{group-a,group-b,group-c}/api/**   (from the index_source_dirs default)
```

Those are exactly the values D3 removed as deployment-specific defaults and D2
stopped locating by package depth. Supplying both as explicit *inputs* to both
harnesses makes the outputs byte-identical.

That is the evidence that matters: **D2 and D3 changed defaults and path
resolution, not the logic that consumes them.** Had the port broken external
directory handling, this comparison would still fail with equivalent inputs.

Reproduce with:

```
.venv/bin/python tools/parity_check.py
```

## Phase 2 — Live turn parity: PASSED

One real OpenCode turn driven through each harness with identical configuration,
comparing the structural result.

**Re-run 2026-08-07, after deviations D7–D15 landed** — cancellation,
attachments, prompt-file input, permissions, chain-order selection, process
tracking. The ported harness still produces structurally identical turns to the
source, which is the claim the whole extraction rests on.

```
provider  token-pool  ->  http://token-pool.example/v1
model     gpt-5.5
prompt    "Reply with exactly the word: parity. No explanation."
```

The pinned model changed from `claude-haiku-4-5-20251001` to `gpt-5.5`. The
first re-run needed three attempts: the source stalled on attempt 1, the
**ported** side stalled on attempt 2, and both completed on attempt 3. The
intermittency hit each side once, which is what a symmetric provider-side fault
looks like — and it is the known `claude-haiku` behaviour recorded below. A
check that pins a flaky model becomes a check nobody reads, so it now uses a
model verified reliable through opencode. With that change the run passes on the
first attempt.

| Field | Source harness | Ported harness |
|---|---|---|
| terminal state | `completed` | `completed` |
| text produced | yes (193 chars) | yes (193 chars) |
| session id present | yes | yes |
| error | none | none |
| `fallback_eligible` | `False` | `False` |
| `fallback_reason` | `None` | `None` |
| `patch_count` | 0 | 0 |
| token fields | `[]` | `[]` |

No structural differences. The model's prose is deliberately **not** compared —
it is not deterministic — so text is compared by presence and length only.

Reproduce with:

```
.venv/bin/python tools/parity_check.py --live
```

The provider key is read from `.env.local` (gitignored, never committed).

### Earlier blocker, now cleared

This check was initially reported as NOT RUN: the provider chain then pointed at
an unreachable private address that refused connections from the
development environment. Switching to the token-pool endpoint above resolved it.
The failure mode is worth remembering — the harness was fine; the environment
was not.

### What this run does and does not prove

**Proves:** the assembled ported harness spawns a real OpenCode process, streams
a real model response, parses it, and reports the same terminal state and result
shape as the source harness.

**Does not prove:**

- **Token accounting.** Both harnesses returned an empty token field for this
  provider, so the run is parity-preserving but does not exercise usage
  extraction. Identical on both sides, so it is not a port defect — but it is
  untested against this provider.
- **Rate-limit detection** against a real 429.
- **Provider fallback** against a real provider error.
- **Patch application**, since the prompt deliberately makes no edits.

Those three paths are covered by the ported tests against synthetic fixtures.
The residual risk is that a real provider emits a shape those fixtures do not
capture — a risk the port does not change, because the fixtures are the ones the
source relies on too.

## Known limitation of the live check (found 2026-08-06)

The live phase compares **two independent model calls**. When the provider is
unhealthy one side can stall while the other completes, and the check then
reports a "structural difference" that says nothing about the port. This was
observed directly: a run reported `source=stalled, ported=completed` purely
because the provider answered one call and not the other.

The check now retries up to three times and reports **INCONCLUSIVE** (exit 2)
when either side fails to complete, instead of a false difference. Only a run
in which both sides completed is evidence about the port.

## Provider stability note (corrected 2026-08-06)

An earlier version of this section claimed the opencode → provider path was
broadly unstable. **That was mostly wrong, and the cause was our own probe.**

`UnknownError: Unexpected server error` traced to
`ProviderModelNotFoundError`: an ad-hoc probe invoked the `opencode` binary
directly without the environment `harness.process._build_env` constructs, which
is what injects provider tokens. Without it opencode loads the provider but
cannot resolve the model. Running the same prompt through the harness-built
environment succeeded immediately.

**Lesson for future probes: drive opencode through `_build_env`, never a bare
`subprocess.run`.** A probe that skips it fails in a way that looks like a
provider outage.

What *is* real: the live parity check has observed genuine one-sided stalls
(`source=stalled, ported=completed`) using the harness's own code path, and
`claude-haiku-4-5-20251001` stalls through opencode while answering normally
over plain HTTP. So a single live failure is still not evidence of a defect —
but the path is far more reliable than the earlier note implied.
