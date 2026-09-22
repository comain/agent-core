# Design Detail (agent-core): Spec-Gen Agent-Core Adoption

## Changes In This Repo

Version **0.8.0**. Modules:

| Module | Change |
| --- | --- |
| `harness/process.py` | `run_turn` kwargs `delivery`, `title`, `pure`, `env`, `project_dir` (`--dir`). Stdin when `delivery="stdin"`. Default argv/`--file` at 60k. |
| `harness/opencode.py` | **Adapter only.** `run_turn` accepts `message` xor `prompt_file`; forwards `session_id`, `delivery`, `title`, `pure`, `env`, `project_dir`, `bootstrap_message`. Interprets `HarnessSpec.options`. First candidate continues; later candidates get bootstrap iff set. Products do not import this class. |
| `harness/affinity.py` | Store `(session_id, model_id)`. `run(harness, key, …)` calls **`Harness.run_turn`**, not `process.run_turn` and not `OpenCodeHarness` by type. Model match → continue; mismatch → `bootstrap_message`, no continue. |
| `harness/tiered_router.py` | `ModelHealthTracker`: rate_limit 60s, timeout 5m, keep 15m unavailable / 10m no_output. Record last-success model. |
| `harness/runner.py` | `session_id` on first candidate only; `bootstrap_message` as `message` on later. Skip unhealthy even when `models=` is set. If all cooling, try anyway. Prefer last-success. Record last-success on `type=="completed"`. |
| `harness/node.py` | `run_harness_node`: prompt callable `str` → `message=`; `Path` → `prompt_file=`. |
| `harness/execution.py` | `AgentTurnRequest.prompt` → `Path \| str`. Optional delivery/title/pure. Does **not** claim affinity. |
| `harness/turns.py` | `run_structured_turn` accepts `message=` xor `prompt_file=`. |
| `workflow/nodes.py` | `agent_turn`: `parse: json`, delivery/title/pure. Prompt file **must** be under `context["turn_dir"]`; fail closed if under `repo_path`/`docs/spec`. No shared `render_prompt` for spec-gen. |
| `git/workspace.py` | POSIX `fcntl.flock` + `RLock`. Production POSIX-only; Windows unsupported (no fake lock). |
| `git/publish.py` | `update_refs_with_lease` via workspace execute. |
| `prompts.py` | `render_placeholders`. Jinja `PromptBundle` untouched. |
| tests | Two-candidate failover uses bootstrap not continuation; tracker across two turns; `--atomic` neither-ref; flock two-process; `agent_turn` rejects wiki-tree prompt files. |

No change to `PublicationCoordinator`, `WorkflowSpec` YAML schema, `TaskDaemon` loop shape, or `SecureArtifactStore`.

## Key Data Structures And Abstractions

```python
# OpenCodeProcess.run_turn — additive kwargs, 0.8 defaults as today except
# when callers pass delivery/title/pure.
delivery: Literal["argv", "file", "stdin"] = "argv"  # "file" if over threshold

# SessionAffinity internal
_sessions: dict[str, tuple[str, str]]  # key -> (session_id, model_id)

# AgentTurnRequest.prompt
Callable[[int, Optional[str]], Path | str]

# update_refs_with_lease — GitWorkspace.execute, not a raw env subprocess
def update_refs_with_lease(
    workspace: GitWorkspace,
    path: Path,                      # checkout to push FROM (Corbell: repo_dir)
    *,
    ref_updates: Mapping[str, str],  # refname -> commit sha
    lease: Mapping[str, str],        # refname -> expected sha; "" = first claim
    is_cancelled: Callable[[], bool] | None = None,
) -> None: ...
```

The helper runs one
`workspace.execute(path, "push", "--atomic", *[f"--force-with-lease={ref}:{expected}" …], "origin", *[f"{sha}:{ref}" …], check=True, is_cancelled=is_cancelled)`.
It does **not** resolve `path_for(repo_url)`. If the remote rejects `--atomic`,
it raises and **must not** leave a subset of refs updated. Tests use a temp
repo with two refs. No MR, no feature branch, no isolated clone.

`render_placeholders(template: str, values: Mapping[str, str]) -> str`
replaces `{{name}}` for keys in `values` only. Unknown `{{...}}` raises.
Values are inserted literally (no second pass). Unused-key checks stay in
the product wrapper.

## Data Dependency Flow

N/A as a product pipeline. This repo supplies libraries. Callers pass repo paths, messages, and lease maps; this repo does not read `spec_tasks` or wiki trees.

## Key Process Flow (intra-repo)

```text
# Planner / keyword (shared agent_turn → execute_agent_turn)
prompt callable returns Path in turn_dir or str
  → run_harness_node message= or prompt_file=
  → context.binding.harness.run_turn  # Harness protocol
  → parse json

# Writer (product node — not agent_turn)
harness = create_configured_harness(HarnessSpec(name="opencode", options=...))
TurnCostPort / ModelBudgetGuard preflight
  → SessionAffinity.run(harness, page_key,
        message=continuation, bootstrap_message=full)
  → harness.run_turn:                 # still Harness, not OpenCodeHarness
       first candidate: session_id + continuation
       later candidates: no session, message=bootstrap iff set
  → record cost; record (session_id, model) on affinity
  → MODEL_BUDGET is non-isolatable
```

`repo_lock`: POSIX flock file under `cache_dir/.locks` keyed by repo+scope,
then `RLock`.

## Key Control Flow

- Affinity: explicit `session_id` in kwargs still wins. Else claim stored id
  only when stored model equals this turn's `model_id`.
- Harness + session: continue on the bound model first with the **current**
  message. Fallback candidates never get `--continue`. They receive
  `bootstrap_message` **when set**; otherwise the original `message` /
  `prompt_file`. Writers always pass bootstrap.
- Cancel: `is_cancelled` → `TurnResult(type="cancelled")`. No public
  `OpenCodeCancelled`.
- Health: `mark_model_unhealthy` on the process-lifetime `_tracker`. Skip
  those models even when `OpenCodeHarness` passes `models=`. If every
  candidate is cooling, attempt them anyway. Last success becomes preferred.
- Git lease: `--atomic` all-or-nothing; raise on mismatch or missing atomic.

## API And Schema Changes (this repo)

No SQLite schema in agent-core beyond existing `ac_*` (Corbell may `RuntimeStore.init()`; no new tables required here).

Public export additions: `render_placeholders`, `update_refs_with_lease`, `SessionAffinity.model_id`.

Breaking: see overview ADR-005 list. README release notes for 0.8.0.

## Key Design Tradeoffs (repo-local)

- **Stdin vs making `--file` the only large-prompt path.** We **will** add stdin because Corbell page prompts already use it and argv/`--file` can change model behavior. Default stays argv/`--file` so UTA/CR on 0.8 (if they bump) keep current delivery unless they opt in.
- **Cooldown on `ModelHealthTracker`, not a function-local map.** The walker
  is per-call; Corbell's windows must survive to the next page. Also
  considered: a new helper object; rejected as a second clock.
- **Flock file location.** Under `GitWorkspace.cache_dir / ".locks"`, `0600`.
  POSIX only. Also considered: locking the checkout directory; rejected
  because it collides with git operations.
- **Affinity is not inside `execute_agent_turn`.** Also considered: stuffing
  `SessionAffinity.claim` into the executor; rejected — CR/UTA would inherit
  writer policy they do not have.
- **Products never import `OpenCodeHarness`.** Also considered: Corbell
  constructing `OpenCodeHarness(...)` for lane scratch; rejected — pass
  factory and knobs through `HarnessSpec.options` so the registry stays the
  only construction path.

## Capacity, Reliability, And Security

- One flock per repo+scope; expected holders: 1 daemon worker + occasional UI. Wait is git-duration (seconds to minutes), not a hot RPC.
- Stdin prompts: no extra disk; `--file` still used when caller passes `Path`.
- `update_refs_with_lease`: one git push. Caller bounds how many refs (Corbell: docs + lease ref).
- Placeholders: O(template size); templates are small.

## Failure-Mode Handling

| Mode | Detection | Recovery |
| --- | --- | --- |
| Stdin child ignores stdin | turn timeout / empty stream | existing timeout; tests with fake process |
| Flock deadlock | process holds lock and dies | OS releases flock on PID death |
| Partial multi-ref push | `--atomic` rejected or lease mismatch | raise; neither ref updated; temp-repo test |
| Affinity bootstrap omitted on model change | new session with current message only | Corbell always passes bootstrap for writers |
| Cooling model retried next page | tracker still unhealthy | skip until window; two-turn test |

## Repo-Local Risks And Verification

- Harness: two-candidate test — bound model fails, second gets
  `bootstrap_message`, not continuation; no `--continue` on failover.
- Affinity wraps harness; model-mismatch **and** mid-chain failover use
  bootstrap.
- Tracker skip on `models=` **across two** `run_turn_with_fallback` calls.
- Two-process POSIX flock (subprocess).
- `update_refs_with_lease` `--atomic` two-ref; lease-lost updates neither.
- `render_placeholders` nested `{{` in values.
- `run_harness_node` / `execute_agent_turn` with `prompt=lambda *_: "…"` .
- Existing harness tests updated for tracker windows and flock.
