# Diagnosing a failed turn

When a turn fails, the database row a product writes usually says something
like `stalled` with zero tokens and no session id. That is the *symptom*. This
is where the cause is.

## The raw turn log

Every turn writes a JSONL log of the agent subprocess, whether it succeeds or
fails:

```
<repo_path>/.agent_cache/opencode_turns/<UTC timestamp>_<model>.jsonl
```

The directory comes from `opencode_turn_log_dir` (default
`.agent_cache/opencode_turns`), resolved relative to the repository unless it
is absolute, and can be turned off with `opencode_turn_log_enabled=false`.
A product overriding `agent_cache_dir` puts it under that instead — cr_plugin
writes to `<repo>/.cr_agent/opencode_turns/`.

One file per *model attempt*, not per turn: a turn that falls back through the
provider chain leaves one file per model, timestamped, which is how you see
the order things were tried in.

Three kinds of record:

| `kind` | what it holds |
|---|---|
| `turn_start` | model, timeout, pid |
| `stream_line` | one line of the subprocess's stdout or stderr, verbatim |
| `turn_finish` | `result_type`, session id, tokens, cost, `fallback_eligible`, `fallback_reason` |

The `stream_line` records are the point. They are the agent's own stderr,
which is otherwise discarded.

## Reading a stall

`result_type: "stalled"` with `fallback_reason: "no_output"` means the process
exited without emitting a single event. That is *not* the same as the model
declining to answer — the model was very likely never reached.

Look at the last few `stream_line` records with `stream: "stderr"` before
`turn_finish`. A real example:

```json
{"kind": "stream_line", "stream": "stderr",
 "raw": "Error: File not found: runtime/audit/<task>/reviewers/correctness/prompt.md"}
{"kind": "turn_finish", "result_type": "stalled",
 "fallback_eligible": true, "fallback_reason": "no_output"}
```

The prompt path was relative, and the agent does not run in the caller's
working directory — it runs in a per-turn workspace. So the file resolved to
nothing and the process exited immediately.

## Why one broken turn looks like eight broken models

That failure is indistinguishable, from the outside, from a model producing
nothing — so it is eligible for fallback, and the harness walks the whole
provider chain. Two seconds per model, every model failing the same way.

Three things follow, and each one misleads:

- **The recorded model is the last one tried**, not the one that mattered. A
  product row saying `model=deepseek/...` may mean "the chain ended there",
  not "deepseek was chosen".
- **Every model is marked unhealthy** in the in-process health tracker, so
  subsequent turns fail in seconds without reaching any provider. Fast repeat
  failures after one slow failure are the signature of this, not of a fast
  network error.
- **The tracker is per process and survives the task.** A long-lived daemon
  stays poisoned until restart or the cooldown expires; a fresh process — a
  shell, a test — starts clean. So "it works when I run it by hand" is
  expected here and proves nothing on its own. `reset_model_health()` clears
  it in tests.

## Reproducing by hand, without fooling yourself

Running the same turn in a shell is the obvious next step and the easiest way
to test something other than what failed. Two mistakes are easy to make:

- **Typing an absolute path** where the service passes a relative one. This is
  precisely how the bug above hid: every manual attempt worked.
- **Starting a fresh process**, which resets the health tracker and hides
  whatever poisoned it.

Pass the same arguments the caller passes, including `model_id` and
`is_cancelled`, and read them out of the failing run rather than retyping
them.

## When the log is missing

If there is no file, either `opencode_turn_log_enabled` is false, or the
process never started — check that the binary in `opencode_bin` exists and is
executable. `turn_start` is written before the subprocess is launched, so its
absence points at configuration, not at the agent.
