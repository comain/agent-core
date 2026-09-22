# agent-core

The agent-agnostic execution and service capability layer for `dev-flow-agent`,
[`cragent`](https://github.com/comain/code-review-agent),
[`unit-test-agent`](https://github.com/comain/unit-test-agent), and
[`spec_generator_agent`](https://github.com/comain/spec_generator_agent).

Products select an agent by configuration and compose shared Git, prompt,
profile, workflow, runtime, identity, delivery, and API capabilities. Product
code keeps domain policy and does not import OpenCode, Pi, or any future agent
implementation directly. The original OpenCode harness was extracted from
[`unit-test-agent`](https://github.com/comain/unit-test-agent).

Release history is in [CHANGELOG.md](CHANGELOG.md).

## Install

```bash
pip install -e /path/to/agent-core
```

Requires Python >= 3.11.

The floor matches the **lowest interpreter any consumer actually runs in
production** ([unit-test-agent](https://github.com/comain/unit-test-agent)
production is 3.11.15; its beta and
[spec_generator_agent](https://github.com/comain/spec_generator_agent)
production are 3.12; [cragent](https://github.com/comain/code-review-agent)
runs 3.13). An earlier `>=3.9` floor was inherited from stale
`requires-python` metadata in two `pyproject.toml` files and matched nothing
deployed. Verified green on 3.11, 3.12, and 3.13.

## Quick start

Products select an agent through configuration and consume only the neutral
harness contract:

```python
from agent_core.harness import HarnessSpec, create_configured_harness

spec = HarnessSpec(
    name=settings.harness,
    options=settings.harness_options,
    timeout_seconds=settings.task_timeout_seconds,
)
harness = create_configured_harness(spec)
harness.run_turn(prompt_file=prompt, repo_path=repo)
```

The registered implementation owns interpretation of `options`; product
workflow code never imports or branches on an agent class.

For a workflow that needs several turns in the same agent conversation, open
one neutral session and use it as the runner for every shared node:

```python
from agent_core.harness import open_harness_session, run_harness_node

session = open_harness_session(harness, repo_path=repo)
try:
    generated = run_harness_node(
        session,
        name="generate",
        repo_path=repo,
        prompt=lambda attempt, feedback: generation_prompt,
    )
    repaired = run_harness_node(
        session,
        name="repair",
        repo_path=repo,
        prompt=lambda attempt, feedback: repair_prompt,
    )
finally:
    diagnostics = session.snapshot()
    session.close()
```

`HarnessSession` is itself a `Harness`: products do not create provider
sessions, send native messages, poll native events, or branch on OpenCode/Pi.
The selected adapter owns that lifecycle. `snapshot()` exposes aggregate
usage, retrospective diagnostics, and patch count without leaking the native
client.

When a configured harness has multiple provider candidates, `open_session`
returns one neutral fallback session. It isolates and closes each failed
candidate conversation, retains the successful conversation for later phase
turns, and reports the sum of provider-reported costs. An exhausted chain is a
final turn failure and cannot request product-level requeue.

For a declarative workflow, use the shared `agent_turn` node with
`result_mode: normalized`. That single opt-in delegates to `run_harness_node`,
opens a phase-scoped neutral session when requested, runs product guards,
flushes best-effort progress, snapshots usage/retrospective/patch count before
close, and returns a JSON-safe `AgentTurnResult`. An optional product
`on_result` port persists the accepted result before the graph checkpoints the
node; legacy workflows that omit `result_mode` keep their prior behavior.
Products may also provide neutral `cost_gate` and `on_cost` ports. The gate
runs before a provider call; the cost sink runs after normalization and before
workspace-result acceptance. `AgentTurnResult.provider_cost_usd` is nullable:
missing provider cost remains unknown at this boundary and is never replaced
with a price-table estimate.

Durable graph callers should pair `open_checkpointer(path, forbidden_roots=[...])`
with `invoke_workflow(...)`. It yields LangGraph's own saver, to be passed
straight to `compile(checkpointer=...)`; `forbidden_roots` is required so that
putting workflow state inside the repository being edited is a decision rather
than an oversight. Checkpoint directories are created `0700` and the database
`0600`; an existing shared or symlinked path is refused, never chmodded. The invocation helper distinguishes a new lineage, a
pending lineage (resumed with no new input), and a completed lineage (returned
without re-executing nodes); corrupt state raises instead of silently
restarting. Checkpointing covers graph state, not product side effects, so the
consumer still owns its operation ledger and result artifacts.

## Prompt construction and artifacts

`PromptLibrary` renders templates with strict undefined-variable checking. Its
existing `render(...)` and `render_to_file(...)` behavior is unchanged in
0.6.1. Consumers that need a stable provider-cache prefix can opt into
`render_sections(...)`: the first boundary marker partitions the raw template,
then each section is rendered independently and returned as
`RenderedPrompt(stable, volatile)`. The method does not trim or normalize
whitespace. Select `keep_trailing_newline=False` only when migrating a legacy
renderer that dropped each section's final newline.

Source-text caching is also opt-in:

```python
from agent_core.prompts import PromptLibrary

prompts = PromptLibrary(template_dir, cache_source_text=True)
rendered = prompts.render_sections(
    "repair.md.j2",
    boundary="{# CACHE_BOUNDARY #}",
    keep_trailing_newline=False,
    task=task,
)
full_prompt = rendered.stable + rendered.volatile
```

`materialize_prompt(...)` retains its permissive legacy defaults. Durable or
security-sensitive callers can opt into deterministic JSON-only metadata, a
UTF-8 byte limit, and owner-only prompt/metadata files:

```python
from agent_core.prompts import materialize_prompt

artifact = materialize_prompt(
    full_prompt,
    directory=directory,
    metadata={"task_id": task_id, "phase": phase},
    strict_metadata=True,
    metadata_max_bytes=4096,
    file_mode=0o600,
)
```

Secure mode rejects unsafe filenames, symlink destinations, non-JSON metadata,
non-finite numbers, and oversized metadata before publishing either file. It
writes recoverable temporary files and atomically replaces an incomplete
prompt/metadata pair. Callers that omit the new options keep the pre-0.6
serialization, filenames, and filesystem modes.

agent-core deliberately does not choose the artifact root or retention rule.
The consuming product must place security-sensitive pairs in owner-only
application state outside the target Git repository, keep stable workflow
identity in metadata, and delete them with the product lineage. Standalone
commands should own a run-scoped directory and remove it when the command
closes; crash-orphan pruning remains product policy.

Harness and provider IDs are opaque transport values, not filesystem names.
Use the shared deterministic mapping before placing one in an artifact path or
safe identity sidecar:

```python
from agent_core.prompts import opaque_artifact_id

session_artifact_id = opaque_artifact_id(session.session_id, namespace="session")
```

The returned lowercase SHA-256 component is stable without exposing the raw
provider ID or constraining future harness identifier formats.

For direct use of OpenCode-specific primitives:

```python
from agent_core.config import HarnessConfig, use_config
from agent_core.harness import effective_model, generate_opencode_config

config = HarnessConfig(
    opencode_provider_chain="token-pool:token-pool/gpt-5.5",
    opencode_model="token-pool/gpt-5.5",
    opencode_bin="/usr/local/bin/opencode",
)

with use_config(config):
    generate_opencode_config("/path/to/repo")
    model = effective_model("generation")
```

## Configuration

`HarnessConfig` is a flat `pydantic-settings` model. It is read through
`current_config()`, which resolves a `ContextVar`, so different tasks or threads
can run under different configuration in the same process.

| Function | Purpose |
|---|---|
| `HarnessConfig(...)` | Build configuration explicitly |
| `use_config(cfg)` | Scope configuration for a block |
| `current_config()` | Read the active configuration |
| `settings` | Proxy view; `settings.X` always reads the active config |
| `propagate(fn)` | **Required** to carry configuration across a thread hop |

### Environment variables

Defaults bind to the `AGENT_` prefix (`AGENT_OPENCODE_MODEL`, etc.). Choose your
own by subclassing:

```python
from pydantic_settings import SettingsConfigDict
from agent_core.config import HarnessConfig

class CrHarnessConfig(HarnessConfig):
    model_config = SettingsConfigDict(env_prefix="CR_", extra="ignore")
```

### ⚠️ Thread pools need `propagate()`

A thread started by `ThreadPoolExecutor` begins with an **empty context**. A
callable submitted from inside `use_config(...)` will therefore *silently* fall
back to the module default — reading the wrong provider, model, or credentials
with no error raised.

```python
# WRONG - the worker silently uses default configuration
with use_config(cfg):
    executor.submit(run_reviewer, task)

# RIGHT
with use_config(cfg):
    executor.submit(propagate(run_reviewer), task)
```

Both halves of this behaviour are pinned by tests in `tests/test_config_scoping.py`,
including reuse of one wrapped callable across a whole pool.

### ⚠️ Concurrent turns need `per_turn_workspace()`

`generate_opencode_config()` writes `opencode.json` into the repository root,
which every turn in that repo then shares. Two concurrent turns wanting
different models overwrite each other and the last writer decides what both run.

```python
from agent_core.harness import per_turn_workspace

with per_turn_workspace(repo, label="security", model_id="p/m2") as cwd:
    process.run_turn(..., repo_path=str(cwd))
```

Each turn gets a private directory whose contents are symlinks back to the real
checkout — the turn sees the whole repo, only the config is private — removed
when the block exits.

## Capability map

Import from the package that owns the capability; consumer code should not
reach into implementation modules.

| Package | Shared responsibility | Main public API |
|---|---|---|
| `agent_core.harness` | Configured agent selection, reusable sessions, turns, fallback, records | `Harness`, `HarnessSession`, `HarnessSpec`, `TurnProgress`, `create_configured_harness`, `open_harness_session`, `run_harness_node`, `TurnResult`, `TurnRecord`, `AgentTurnResult`, `AgentSessionRef`, `PaidAttempts`, `execute_agent_turn`, `diagnose_sessions` |
| `agent_core.git` | Credentials, bounded/cancellable Git execution, change discovery, publishing | `GitWorkspace`, `GitCredentials`, `ChangeCollector`, `ChangeSet`, `BotIdentity`, Git errors, `is_retryable_git_failure`, `retry_git_operation` |
| `agent_core.prompts` | Strict template rendering, stable/volatile sections, and reproducible prompt artifacts | `PromptLibrary`, `RenderedPrompt`, `PromptArtifact`, `materialize_prompt` |
| `agent_core.profiles` | YAML/JSON-defined agent profiles | `AgentProfile`, `ProfileRegistry`, `ProfileError` |
| `agent_core.workflow` | Declarative specs, shared nodes, turn projection, safe checkpoint invocation | `WorkflowSpec`, `NodeRegistry`, `shared_registry`, `AgentTurnResult` (re-exported), `open_checkpointer`, `delete_checkpoint_lineage`, `invoke_workflow`, `agent_core.workflow.graph.build_graph` |
| `agent_core.runtime` | Runtime store, bounded progress, daemon, artifacts, attachments, SSE | `RuntimeStore`, `RuntimeProgressPublisher`, `ProgressBatcher`, `ProgressBudget`, `TaskDaemon`, `DaemonConfig`, `SecureArtifactStore`, `StoredArtifact`, `NamespaceLayout`, `AttachmentStore`, `stream_task_events` |
| `agent_core.identity` | Principal resolution and authorization policy | `IdentityResolver`, `Principal`, `Policy`, deployment trust guard |
| `agent_core.integrations` | Trigger protocols, delivery, RDC/GitLab adapters | `TriggerProtocol`, `TriggerMount`, `create_trigger_router`, `deliver_json`, `GitLabClient` |
| `agent_core.api` | Shared task/control/gate HTTP surface | `TaskServicePorts`, `create_task_router` |
| `agent_core.db` | Ordered, idempotent SQLite migrations | `Migration`, `apply_migrations` |

The public interfaces are implementation-neutral except for symbols explicitly
documented as OpenCode primitives below.

## Harness public interface

Import from `agent_core.harness`. Anything not listed is private and may change.

| Module | Symbols |
|---|---|
| registry | `Harness`, `HarnessSpec`, `create_configured_harness`, `create_harness`, `register_harness`, `available_harnesses`, `UnknownHarnessError` |
| sessions | `HarnessSession`, `ResumableHarness`, `SessionSnapshot`, `SessionUnsupportedError`, `open_harness_session` |
| client | `OpenCodeClient`, `OpenCodeAuthClient` |
| process | `OpenCodeProcess`, `TurnResult`, `classify_provider_model_error` |
| server | `OpenCodeServer` |
| stream | `OpenCodeStreamParser` |
| config | `generate_opencode_config`, `CURSOR_PLUGIN_NAME`, `EXTERNAL_DIRS_CONFIG`, `GLOBAL_OPENCODE_CONFIG`, `GLOBAL_OPENCODE_PLUGIN_ROOT`, `OPENCODE_PLUGIN_CACHE_ROOT` |
| tiered_router | `ProviderCandidate`, `ModelHealthTracker`, `effective_model`, `cheap_model_for_phase`, `provider_candidates`, `available_provider_candidates`, `opencode_model_id`, `provider_local_model_id`, `provider_token_statuses`, `mark_model_unhealthy`, `model_health_for_candidates`, `reset_model_availability_cache`, `parse_provider_chain`, `parse_provider_tokens`, `parse_provider_base_urls`, `parse_model_list_response` |
| fallback | `ProviderRateLimitError`, `raise_for_provider_fallback_event`, `poll_completion_with_task_guard` |
| runner | `run_turn_with_fallback`, `should_skip_provider` |
| rate_limit | `opencode_log_dir`, `debug_log_dir`, `recent_log_files`, `parse_rate_limit_payload`, `detect_rate_limit_in_logs` |
| net | `format_host_for_url`, `build_base_url` |

`PROJECT_ROOT` is intentionally absent — see deviation D2.

## Runtime (`agent_core.runtime`)

Canonical tables for events, controls, worker liveness, and human gates,
namespaced `ac_*` so they can share a database with a product's existing tables.
Work is referenced by an opaque `task_ref` — the four consumers use TEXT,
INTEGER, and parent/child key shapes, so no single foreign key can point at all
of them.

Adopt **per capability**: a product can take human gates and event streaming
while keeping its own task and control tables.

```python
from agent_core.runtime import RuntimeStore

store = RuntimeStore("runtime.db"); store.init()
gate = store.open_gate(task_ref="task-1", node="design_review", kind="input",
                       thread_id="wf-1", prompt={"question": "Approve?"})
store.pending_gates()          # the approval inbox, across all tasks
store.events_since(task_ref="task-1", after_id=0)   # SSE cursor
```

Workflow state is **not** stored here — the workflow engine owns that.

### Live progress (SSE)

Framework-agnostic: agent-core yields SSE frames, your product supplies the HTTP
response, so a web framework is not forced on all four consumers.

```python
from agent_core.runtime import (
    RuntimeProgressPublisher,
    group_progress_events,
    public_progress_events,
    stream_task_events,
)

# The harness contract is agent-agnostic. The publisher projects its neutral
# progress into public-safe events. Tool actions may carry a bounded, sanitized
# repository-relative target; agent text/reasoning may carry an adapter-supplied
# synopsis. Raw commands, absolute paths, structured model output, secrets, and
# provider errors are not persisted.
publish = RuntimeProgressPublisher(
    store,
    task_ref="task-1",
    stage="review",
    context={
        "reviewer": "security",
        "session_ref": "reviewer:security",
        "session_kind": "reviewer",
    },
)

# A UI can recover history, normalize events written by older publishers, drop
# provider step chatter, and partition parallel sessions without understanding
# OpenCode (or whichever harness produced the neutral progress).
groups = group_progress_events(public_progress_events(store, "task-1"))
harness.run_turn(prompt_file=prompt, repo_path=repo, on_progress=publish)

# FastAPI / Starlette
return StreamingResponse(
    stream_task_events(store, task_ref="task-1", after_id=last_event_id),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
)
```

Two things that are easy to get wrong and are handled here:

- **`X-Accel-Buffering: no`** — nginx buffers proxied responses by default, which
  holds frames back and makes a live view look frozen.
- **Resumption** — each frame carries `id:`, so a reconnecting browser sends
  `Last-Event-ID` and you pass it as `after_id` to deliver exactly what was
  missed. This works because the id is the autoincrement key, unaffected by the
  one-second resolution of stored timestamps.
- **Human-safe projection** — `RuntimeProgressPublisher` maps neutral tools to
  stable activities such as `code_review`, `repository_checks`, and
  `reference_research`, while preserving a bounded `detail` synopsis when it
  is safe. Repository targets are reduced to relative paths; obvious secrets,
  absolute paths, URLs, commands, code fences, and structured model output are
  removed. Text/reasoning details must be supplied explicitly by the harness
  adapter and never come from the trusted log message. Provider step boundaries
  remain transport chatter and raw errors remain private.
- **Parallel-session partitioning** — `public_progress_events` normalizes both
  current and legacy stored events; `group_progress_events` partitions them by
  `session_ref` and folds repetitive adjacent activity. Products supply only
  safe correlation values such as `reviewer:security` or `feedback:<id>`.
  `HarnessEventBridge` remains for trusted diagnostic logs only.

## Identity (`agent_core.identity`)

Humans are identified by a header an SSO proxy injects; machines by a shared
service token. agent-core stores no credentials and issues none.

```python
from agent_core.identity import IdentityResolver, Policy, ANSWER_GATE, assert_trusted_deployment

# Refuses to start where the identity header would be forgeable.
assert_trusted_deployment(bind_host="127.0.0.1", trust_proxy_headers=True, proxy_is_fronting=False)

principal = IdentityResolver().resolve(request.headers)   # None when anonymous
Policy().authorize(principal, ANSWER_GATE)                # raises 401/403 equivalents
```

### ⚠️ The trust assumption

Header trust is safe **only if the service cannot be reached except through the
proxy**. `assert_trusted_deployment` refuses to start when bound to a
non-loopback address unless an operator explicitly declares that a proxy is the
sole ingress — because the failure mode otherwise is silent: anyone able to
reach the port can act as any user, and nothing looks wrong in the logs.

Declaring the proxy is a **separate flag** from enabling header trust, so
turning on auth locally is not accidentally also a claim about production
network topology.

### Staged enforcement

`gate:answer`, `gate:cancel`, and `task:mutate` require an authenticated
principal today. `report:read` stays open until existing callers are updated —
staged deliberately so nothing currently working breaks while anonymous
approvals stop immediately. Service principals cannot answer human gates unless
explicitly configured: a pipeline approving its own gate defeats the gate.

## Human gates (`agent_core.gates.langgraph`)

Bridges a workflow engine's suspend/resume to the gate store an approval inbox
reads. Optional dependency — three of the four consumers do not run graphs, and
should not have to install a workflow engine to use the harness:

```bash
pip install "agent-core[langgraph]"
```

```python
from agent_core.gates.langgraph import human_gate, resume_answered_gates

def design_review(state):                      # inside a graph node
    answer = human_gate(
        store, task_ref=state["task_ref"], node="design_review", kind="input",
        prompt={"design": state["design"]},
        response_schema={"decision": "approve|reject", "comments": "string"},
        config={"configurable": {"thread_id": state["task_ref"]}},
    )
    return {"decision": answer["decision"]}

# elsewhere: a human answers from the inbox, a daemon tick resumes the run
store.answer_gate(gate_id=gate.gate_id, response={"decision": "approve"}, principal=p)
resume_answered_gates(graph, store)
```

### ⚠️ Gated nodes re-execute on resume

LangGraph re-runs the gated node **from the top** when a run resumes;
`interrupt()` then returns the answer instead of raising. Measured: the code
before the gate ran twice, the code after it once.

So gate ids are derived deterministically from `(thread_id, node, attempt)` and
`open_gate` returns the existing record for an id it already holds. Without
that, the inbox accumulates a duplicate of every gate ever answered. Pass a
distinct `attempt` when a graph legitimately revisits the same gate — a revision
cycle asking for review a second time is a separate inbox entry.

Any other side effect placed before the gate in the same node will also run
twice. Keep gates at the start of their node.

### Gates wait indefinitely

`expires_in_seconds` defaults to `None`. An unanswered design review blocks its
workflow rather than being auto-approved or auto-rejected on a timer. That is
safe only because the run is suspended holding no worker, and because
`pending_gates()` makes every waiting gate visible.

## Approval inbox (`agent_core.ui`)

Pure functions from data to strings — products supply routing and responses, so
no web framework is imposed on four consumers.

```python
from agent_core.ui import render_inbox, gate_to_dict

html = render_inbox(store.pending_gates(), action_url_for=lambda g: f"/gates/{g.gate_id}/answer")
payload = [gate_to_dict(g) for g in store.pending_gates()]   # or JSON for a SPA
```

Everything rendered is escaped. Gate prompts carry model-generated text and
reviewer comments, and this system reviews *code* — markup in a prompt is
expected input, not an edge case.

Because gates never expire, the queue is the only thing that surfaces a review
nobody picked up, so each entry shows how long it has waited and anything over
24h is visually flagged.

## Task-service router (`agent_core.api`)

The HTTP surface every consumer needs and none should write again. Optional
extra, so a consumer serving its UI from stdlib `http.server` is unaffected:

```bash
pip install "agent-core[api]"
```

```python
from agent_core.api.router import create_task_router, TaskServicePorts

app.include_router(create_task_router(
    store,
    ports=TaskServicePorts(get_task=..., list_tasks=..., health=...),
    prefix="/cr-agent",          # keep your existing base path
))
```

| Route | Purpose |
|---|---|
| `GET /healthz`, `/health`, `/healthcheck.html` | liveness |
| `GET /api/v1/tasks`, `/api/v1/tasks/{id}` | task read |
| `POST /api/v1/tasks/{id}/{stop,cancel,requeue}` | control mutations (**auth required**) |
| `GET /api/v1/tasks/{id}/events` | SSE progress, resumes on `Last-Event-ID` |
| `GET /gates`, `/gates/data` | approval inbox, HTML and JSON |
| `POST /gates/{id}/answer`, `/gates/{id}/cancel` | answer a gate (**auth required**) |
| `GET /api/v1/admin/running` | runners and staleness |

Two consumers independently converged on the same URL shapes —
`/healthcheck.html`, `/task-status/{id}`, `/reports/{id}/index.html`,
`/api/v1/rdc/trigger` are character-identical between them — so those paths are
preserved rather than reinvented, and `prefix` lets a product mount the router
without changing URLs its users already have.

**Domain rendering stays with the product.** Findings tables, coverage reports
and spec documents are what each product exists to produce; only the scaffolding
around them is shared. A route whose port is not supplied returns **501**, not
404, so "not implemented here" stays distinguishable from "wrong URL".

## Attachments and image input (`agent_core.runtime.AttachmentStore`)

Manual task creation accepts images — a design sketch, a screenshot of a
failure, a photo of a whiteboard.

```python
from agent_core.runtime import AttachmentStore

store = AttachmentStore("/var/lib/agent/attachments")
store.save(task_ref="task-1", filename=upload.filename, data=await upload.read())

OpenCodeProcess().run_turn(
    "What is wrong in this screenshot?",
    repo_path=repo,
    attachments=store.paths("task-1", images_only=True),
)
```

**Validated end to end**: a 64×64 blue PNG attached this way was correctly
described as "Blue" by `gpt-5.5` through the harness, and the provider also
accepts `image_url` data URIs directly over HTTP.

Uploaded filenames are treated as hostile — they are the only caller-controlled
part of a filesystem write. Only the basename survives, it is reduced to a
conservative character set, and the stored name is prefixed with a content hash,
so a caller can neither choose where a file lands nor overwrite another task's
attachment. `task_ref` gets the same treatment.

## Session affinity

`run_turn` takes a `session_id` but nothing decides what it should be, so by
default every turn starts fresh and the model re-reads context it produced
moments earlier.

```python
from agent_core.harness import SessionAffinity

affinity = SessionAffinity(max_turns=10)
affinity.run(harness, "review-42", message="propose a design", repo_path=repo)
# ... a human answers a gate ...
affinity.run(harness, "review-42", message="address these comments", repo_path=repo)
```

The second turn continues the first's session, so the model sees its own
proposal. `max_turns` rotates the session before re-sent history costs more than
it saves. Thread-safe: two workers sharing a key will not each start a session.

## Testing your workflows (`FakeOpenCodeProcess`)

A drop-in for `OpenCodeProcess` — same `run_turn` signature, scripted results,
and a record of what it was asked. Without it, every consumer stubs
`subprocess.Popen`, which couples its tests to harness internals and gets
re-implemented per product.

```python
from agent_core.harness import FakeOpenCodeProcess

process = FakeOpenCodeProcess(["first answer", "second answer"])
workflow.run(process)
assert process.call_count == 2
assert "design" in process.prompts[0]
```

Script entries may be strings, prepared `TurnResult`s, or exceptions to raise.
The last entry repeats, so a test that only cares about the first turn need not
enumerate the rest.

## Graceful shutdown

Each turn is spawned into its own process group, so it **outlives the service**
unless something reaps it: a deploy sends SIGTERM, the service exits, and every
in-flight OpenCode run keeps going — holding a provider connection and spending
on a task nobody is waiting for.

```python
from agent_core.harness import install_shutdown_handlers

install_shutdown_handlers(graceful=True)   # once, at service start
```

`graceful=True` makes the *first* signal reap children and return, so a
`TaskDaemon` stops claiming new work and finishes what it holds. A second signal
exits immediately — what an operator pressing Ctrl-C twice means.

## Pricing (`agent_core.pricing`)

Providers do not reliably report cost — the token-pool endpoint these products
use returns no usage at all — so a derived figure is often the only cost number
that exists. Rates are USD per million tokens.

```python
from agent_core.pricing import cost_for_turn, is_priced

cost = cost_for_turn(result)          # provider figure if reported, else derived
if not is_priced(result.model_id):
    ...                               # newly adopted model, priced at defaults
```

Two hazards in the implementation this replaces are fixed:

- **Longest match wins.** Previously an ordered if/elif meant a general
  `gpt-5.4` rule had to be written *after* `gpt-5.4-mini`, or it shadowed it.
  Appending a new rule in the obvious place silently repriced a model. Ordering
  now carries no meaning.
- **Unknown models are visible.** They previously fell through to a silent
  default, so a newly adopted model was billed as something else with no signal.
  `estimate_cost` still falls back — a missing rate must not break reporting —
  but `is_priced()` reports it and the fallback is logged once per model.

Pass `table=` to override rates per consumer.

## Git access (`agent_core.git`)

Every consumer clones a GitLab project into a workspace, lets an agent work in
it, and pushes the result — authenticating with an SSH key or an access token.
The credential half was **byte-identical across two products** (90 lines,
differing only by a trailing blank line) and partially forked into a third.

```python
from agent_core.git import BotIdentity, GitCredentials, GitWorkspace

ws = GitWorkspace(
    "/var/cache/repos",
    GitCredentials(ssh_key_path="/opt/keys/id_ed25519"),
)
repo = ws.prepare("git@git.example.com:group/app.git", branch="main")
# Required evidence uses strict reads; failed Git commands raise.
head = ws.query(repo, "rev-parse", "HEAD")
# ... agent works in `repo` ...
ws.commit_all_and_push(repo, branch="feature/x", message="generated tests",
                       identity=BotIdentity("dev-flow-bot", "bot@local"))
```

Credentials travel in the subprocess environment — nothing is written to
`~/.ssh/config` or global git config — so concurrent tasks using different
identities cannot interfere. A per-repository lock serialises access to each
cache directory, since several tasks routinely target the same repo.

Three failure modes worth knowing, each covered by a test against a real repo:

- **`prepare()` leaves HEAD detached** (it checks out `origin/<branch>`).
  `commit_all_and_push` therefore moves HEAD onto the branch first — without
  that, the commit is unreachable, `push` reports *"Everything up-to-date"*, and
  the generated work is silently never published.
- **Fetch writes the remote-tracking ref explicitly.** Plain
  `git fetch origin <branch>` updates only `FETCH_HEAD`, so a later checkout of
  `origin/<branch>` quietly returns the previous commit.
- **`token_host` has no default.** The original hard-coded one deployment's
  hostname; an auth header sent to the wrong host is a credential leak.

`output()` is intentionally best-effort for optional probes; `query()` raises
`GitCommandError` and should be used when missing evidence must fail the task.
Set `clone_depth=None` for workflows such as retrospective analysis that must
retain full history; normal task workspaces remain shallow by default.

## Testing

```bash
.venv/bin/python -m pytest
.venv/bin/python tools/parity_check.py   # behavioural parity vs the source harness
```

The suite is the source repo's own harness tests, ported with changes limited to
import paths and configuration construction. They are the correctness oracle: a
ported test that only passes after a behaviour change indicates a defect in the
port, not a test to update.

See [`docs/parity.md`](docs/parity.md) for behavioural parity evidence — both
the deterministic and live-turn phases pass.

## Provenance

Ported from [`unit-test-agent`](https://github.com/comain/unit-test-agent) at commit `0d70115783fc4cd075a26e04909585ba2f34b288`.
See [`docs/port-baseline.md`](docs/port-baseline.md).
