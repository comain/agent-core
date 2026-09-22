# Usage

How a product calls agent-core. Package boundaries and the short examples are in the [README](../README.md). Release notes are in [CHANGELOG.md](../CHANGELOG.md).

## Contents

- [Sessions and workflows](#sessions-and-workflows)
- [Prompts](#prompts)
- [Configuration](#configuration)
- [Harness interface](#harness-interface)
- [Runtime](#runtime)
- [Identity](#identity)
- [Human gates](#human-gates)
- [Approval inbox](#approval-inbox)
- [Task router](#task-router)
- [Attachments](#attachments)
- [Session affinity](#session-affinity)
- [Workflow tests](#workflow-tests)
- [Shutdown](#shutdown)
- [Model selection](#model-selection)
- [Pricing](#pricing)
- [Git](#git)

## Sessions and workflows

For several turns in one agent conversation, open one neutral session and use it for every shared node:

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

`HarnessSession` is itself a `Harness`. Products do not create provider sessions, send native messages, poll native events, or branch on OpenCode or Pi. `snapshot()` exposes aggregate usage, retrospective diagnostics, and patch count without leaking the native client.

When the harness has several provider candidates, `open_harness_session` returns one neutral fallback session. It isolates and closes each failed candidate conversation, keeps the successful conversation for later turns, and reports the sum of provider-reported costs. An exhausted chain is a final turn failure. It cannot ask the product to requeue.

For a declarative workflow, use the shared `agent_turn` node with `result_mode: normalized`. That opt-in delegates to `run_harness_node`, opens a phase-scoped session when requested, runs product guards, flushes best-effort progress, snapshots usage before close, and returns a JSON-safe `AgentTurnResult`. An optional `on_result` port persists the accepted result before the graph checkpoints the node. Workflows that omit `result_mode` keep their previous behavior. Neutral `cost_gate` and `on_cost` ports run before the provider call and after normalization. `AgentTurnResult.provider_cost_usd` is nullable: a missing provider cost stays unknown and is never replaced with a price-table estimate.

Durable graph callers pair `open_checkpointer(path, forbidden_roots=[...])` with `invoke_workflow(...)`. The helper yields LangGraph's own saver for `compile(checkpointer=...)`. `forbidden_roots` is required so that putting workflow state inside the repository being edited is an explicit choice. Checkpoint directories are created `0700` and the database `0600`. An existing shared or symlinked path is refused. The helper distinguishes a new lineage, a pending lineage resumed with no new input, and a completed lineage returned without re-executing nodes. Corrupt state raises. Checkpointing covers graph state, not product side effects, so the consumer still owns its operation ledger and result artifacts.

## Prompts

`PromptLibrary` renders templates with strict undefined-variable checking. `render(...)` and `render_to_file(...)` keep their existing behavior. Callers that need a stable provider-cache prefix can opt into `render_sections(...)`. The first boundary marker partitions the raw template, then each section is rendered on its own and returned as `RenderedPrompt(stable, volatile)`. The method does not trim or normalize whitespace. Select `keep_trailing_newline=False` only when migrating a renderer that dropped each section's final newline.

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

`materialize_prompt(...)` keeps its permissive defaults. Durable or security-sensitive callers can opt into deterministic JSON-only metadata, a UTF-8 byte limit, and owner-only files:

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

Strict mode rejects unsafe filenames, symlink destinations, non-JSON metadata, non-finite numbers, and oversized metadata before publishing either file. It writes recoverable temporary files and atomically replaces an incomplete prompt/metadata pair. Callers that omit those options keep the legacy serialization, filenames, and filesystem modes.

agent-core does not choose the artifact root or the retention rule. The product places security-sensitive pairs in owner-only application state outside the target Git repository, keeps stable workflow identity in metadata, and deletes them with the product lineage. A standalone command should own a run-scoped directory and remove it when the command closes.

Harness and provider IDs are opaque transport values, not filesystem names. Map one before placing it in a path:

```python
from agent_core.prompts import opaque_artifact_id

session_artifact_id = opaque_artifact_id(session.session_id, namespace="session")
```

The returned lowercase SHA-256 component is stable without exposing the raw provider ID.

Direct use of the OpenCode adapter looks like this. New workflow code should prefer the neutral harness above.

```python
from agent_core.config import HarnessConfig, use_config
from agent_core.harness import effective_model, generate_opencode_config

config = HarnessConfig(
    opencode_provider_chain="pool:pool/example-model",
    opencode_model="pool/example-model",
    opencode_bin="/usr/local/bin/opencode",
)

with use_config(config):
    generate_opencode_config("/path/to/repo")
    model = effective_model("generation")
```

## Configuration

`HarnessConfig` is a flat `pydantic-settings` model. `current_config()` reads a `ContextVar`, so different tasks or threads can run under different configuration in the same process.

| Function | Purpose |
|---|---|
| `HarnessConfig(...)` | Build configuration explicitly |
| `use_config(cfg)` | Scope configuration for a block |
| `current_config()` | Read the active configuration |
| `settings` | Proxy view; `settings.X` always reads the active config |
| `propagate(fn)` | Carry configuration across a thread hop |

Defaults bind to the `AGENT_` prefix (`AGENT_OPENCODE_MODEL`, and so on). Choose another prefix by subclassing:

```python
from pydantic_settings import SettingsConfigDict
from agent_core.config import HarnessConfig

class AppHarnessConfig(HarnessConfig):
    model_config = SettingsConfigDict(env_prefix="APP_", extra="ignore")
```

A thread started by `ThreadPoolExecutor` begins with an empty context. A callable submitted from inside `use_config(...)` silently falls back to the module default unless it is wrapped:

```python
with use_config(cfg):
    executor.submit(propagate(run_reviewer), task)
```

`tests/test_config_scoping.py` pins both the silent fallback and reuse of one wrapped callable across a pool.

`generate_opencode_config()` writes `opencode.json` into the repository root, which every turn in that repo then shares. Concurrent turns that want different models overwrite each other. Give each turn a private directory:

```python
from agent_core.harness import per_turn_workspace

with per_turn_workspace(repo, label="security", model_id="pool/example-model") as cwd:
    process.run_turn(..., repo_path=str(cwd))
```

The private directory's contents are symlinks back to the real checkout. The turn sees the whole repo, only the config is private, and the directory is removed when the block exits.

## Harness interface

Import from `agent_core.harness`. Anything not listed there is private and may change.

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

`PROJECT_ROOT` is intentionally absent. The source harness derived a repository root from package depth, and that expression does not point at a repository root in this layout. Pass paths in explicitly.

## Runtime

Canonical tables for events, controls, worker liveness, and human gates are namespaced `ac_*` so they can share a database with a product's existing tables. Work is referenced by an opaque `task_ref`. Consumers use different key shapes, so no single foreign key can point at all of them.

Adopt per capability. A product can take human gates and event streaming while keeping its own task and control tables.

```python
from agent_core.runtime import RuntimeStore

store = RuntimeStore("runtime.db"); store.init()
gate = store.open_gate(task_ref="task-1", node="design_review", kind="input",
                       thread_id="wf-1", prompt={"question": "Approve?"})
store.pending_gates()
store.events_since(task_ref="task-1", after_id=0)
```

Workflow state is not stored here. The workflow engine owns that.

### Live progress

agent-core yields server-sent events. The product supplies the HTTP response, so a web framework is not required to use the runtime.

```python
from agent_core.runtime import (
    RuntimeProgressPublisher,
    group_progress_events,
    public_progress_events,
    stream_task_events,
)

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

groups = group_progress_events(public_progress_events(store, "task-1"))
harness.run_turn(prompt_file=prompt, repo_path=repo, on_progress=publish)

return StreamingResponse(
    stream_task_events(store, task_ref="task-1", after_id=last_event_id),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
)
```

The publisher projects neutral progress into public-safe events. Tool actions may carry a bounded, sanitized repository-relative target. Agent text may carry an adapter-supplied synopsis. Raw commands, absolute paths, structured model output, secrets, and provider errors are not persisted.

- `X-Accel-Buffering: no` stops nginx from buffering proxied frames.
- Each frame carries `id:`. A reconnecting client sends `Last-Event-ID`, and `after_id` delivers what was missed. The id is the autoincrement key, not the one-second timestamp.
- Neutral tools map to stable activities such as `code_review`, `repository_checks`, and `reference_research`. Repository targets become relative paths. Secrets, absolute paths, URLs, commands, code fences, and structured model output are removed. Text details come from the harness adapter, not from the trusted log message.
- `group_progress_events` partitions events by `session_ref` and folds repetitive adjacent activity. Products supply only safe correlation values such as `reviewer:security` or `feedback:<id>`. `HarnessEventBridge` is for trusted diagnostic logs only.

## Identity

Humans are identified by a header an SSO proxy injects. Machines use a shared service token. agent-core stores no credentials and issues none.

```python
from agent_core.identity import IdentityResolver, Policy, ANSWER_GATE, assert_trusted_deployment

assert_trusted_deployment(bind_host="127.0.0.1", trust_proxy_headers=True, proxy_is_fronting=False)

principal = IdentityResolver().resolve(request.headers)   # None when anonymous
Policy().authorize(principal, ANSWER_GATE)
```

Header trust is safe only when the service cannot be reached except through the proxy. `assert_trusted_deployment` refuses to start on a non-loopback address unless an operator declares that a proxy is the sole ingress. Otherwise anyone who can reach the port can act as any user, and the logs do not show it. Declaring the proxy is a separate flag from enabling header trust, so local auth is not also a claim about production network topology.

`gate:answer`, `gate:cancel`, and `task:mutate` require an authenticated principal. `report:read` stays open until existing callers are updated, so enforcement can turn on without breaking anonymous reads. Service principals cannot answer human gates unless explicitly configured.

## Human gates

`agent_core.gates.langgraph` bridges a workflow engine's suspend/resume to the gate store. It is an optional extra, so a product that does not run graphs does not install a workflow engine to use the harness:

```bash
pip install "agent-core[langgraph]"
```

```python
from agent_core.gates.langgraph import human_gate, resume_answered_gates

def design_review(state):
    answer = human_gate(
        store, task_ref=state["task_ref"], node="design_review", kind="input",
        prompt={"design": state["design"]},
        response_schema={"decision": "approve|reject", "comments": "string"},
        config={"configurable": {"thread_id": state["task_ref"]}},
    )
    return {"decision": answer["decision"]}

store.answer_gate(gate_id=gate.gate_id, response={"decision": "approve"}, principal=principal)
resume_answered_gates(graph, store)
```

LangGraph re-runs the gated node from the top when a run resumes, and `interrupt()` then returns the answer instead of raising. Code before the gate runs again. Gate ids are derived from `(thread_id, node, attempt)`, and `open_gate` returns the existing record for an id it already holds. Pass a distinct `attempt` when a graph legitimately revisits the same gate. Keep other side effects out of the prefix of a gated node.

`expires_in_seconds` defaults to `None`. An unanswered review blocks its workflow rather than being auto-approved or auto-rejected. That is safe because the run is suspended and holds no worker, and because `pending_gates()` lists every waiting gate.

## Approval inbox

`agent_core.ui` is pure functions from data to strings. The product supplies routing and the HTTP response.

```python
from agent_core.ui import render_inbox, gate_to_dict

html = render_inbox(store.pending_gates(), action_url_for=lambda g: f"/gates/{g.gate_id}/answer")
payload = [gate_to_dict(g) for g in store.pending_gates()]
```

Everything rendered is escaped. Gate prompts carry model-generated text, and markup in a prompt is expected input. Because gates do not expire, each entry shows how long it has waited, and anything over 24 hours is flagged.

## Task router

The shared HTTP surface is an optional extra:

```bash
pip install "agent-core[api]"
```

```python
from agent_core.api.router import create_task_router, TaskServicePorts

app.include_router(create_task_router(
    store,
    ports=TaskServicePorts(get_task=..., list_tasks=..., health=...),
    prefix="/app",
))
```

| Route | Purpose |
|---|---|
| `GET /healthz`, `/health`, `/healthcheck.html` | liveness |
| `GET /api/v1/tasks`, `/api/v1/tasks/{id}` | task read |
| `POST /api/v1/tasks/{id}/{stop,cancel,requeue}` | control mutations (auth required) |
| `GET /api/v1/tasks/{id}/events` | SSE progress, resumes on `Last-Event-ID` |
| `GET /gates`, `/gates/data` | approval inbox, HTML and JSON |
| `POST /gates/{id}/answer`, `/gates/{id}/cancel` | answer or cancel a gate (auth required) |
| `GET /api/v1/admin/running` | runners and staleness |

`prefix` mounts the router under a product's existing base path. Status pages and report documents stay with the product. Trigger endpoints such as `/api/v1/rdc/trigger` are mounted with `create_trigger_router` from `agent_core.integrations`, not with this router. A route whose port is not supplied returns 501, so "not implemented here" stays distinct from "wrong URL".

## Attachments

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

Uploaded filenames are hostile: they are the only caller-controlled part of a filesystem write. Only the basename survives, it is reduced to a conservative character set, and the stored name is prefixed with a content hash. `task_ref` gets the same treatment. A caller cannot choose where a file lands or overwrite another task's attachment.

## Session affinity

`run_turn` accepts a `session_id`, and nothing else chooses one, so every turn starts fresh unless the product keeps the id.

```python
from agent_core.harness import SessionAffinity

affinity = SessionAffinity(max_turns=10)
affinity.run(harness, "review-42", message="propose a design", repo_path=repo)
affinity.run(harness, "review-42", message="address these comments", repo_path=repo)
```

The second turn continues the first session. `max_turns` rotates the session before re-sent history costs more than it saves. Two workers sharing a key will not each start a session.

## Workflow tests

`FakeOpenCodeProcess` matches the `OpenCodeProcess.run_turn` signature, returns scripted results, and records what it was asked.

```python
from agent_core.harness import FakeOpenCodeProcess

process = FakeOpenCodeProcess(["first answer", "second answer"])
workflow.run(process)
assert process.call_count == 2
assert "design" in process.prompts[0]
```

Script entries may be strings, prepared `TurnResult`s, or exceptions. The last entry repeats, so a test that only cares about the first turn does not have to enumerate the rest.

## Shutdown

Each turn is spawned into its own process group, so it outlives the service unless something reaps it. A deploy that sends SIGTERM and exits otherwise leaves in-flight runs holding a provider connection.

```python
from agent_core.harness import install_shutdown_handlers

install_shutdown_handlers(graceful=True)
```

`graceful=True` makes the first signal reap children and return, so a `TaskDaemon` stops claiming new work and finishes what it holds. A second signal exits immediately.

## Model selection

Admission reads a cached catalog of benchmark scores, prices, and provider inventory, then applies the application's policy. A model that fails the policy is not called. When nothing eligible remains, discovery raises `NoAvailableModels` instead of falling through to a manual or default model.

`ModelPolicy`, `Candidate`, `ResolvedSelection`, and `resolve_selection` are the package exports. `DiscoveryRuntime` lives in `agent_core.model_selection.runtime`, and `load_selection_config` lives in `agent_core.model_selection.configuration`.

`ModelPolicy` carries the coding-score floor (default 70), `price-efficient` or `best-score` ranking, an allowlist, a denylist, and explicit approvals for models that have no score. A null allowlist leaves inventory unrestricted. An empty allowlist denies every model. A shared denylist wins over an application allowlist. Scored rows with empty, `max`, or `xhigh` effort are not admitted.

Provider order in the operator config is fallback priority. Ranking happens inside one provider: discounted price, then effort, then coding score. A cheaper or higher-scoring model on a later provider does not jump ahead of an eligible model on an earlier one. An unavailable or credential-exhausted provider falls through to the next.

The config file is an absolute path outside the repository being edited (`AGENT_MODEL_SELECTION_CONFIG` or `--config`). It names credential environment variables and never contains key material. `refresh` publishes the catalog. Workers only read it.

```python
from agent_core.model_selection import Candidate, ModelPolicy, resolve_selection

policy = ModelPolicy(application_id="review", minimum_coding_score=70)
selection = resolve_selection(
    [Candidate(identity="pool/example-model", score=80, benchmark_id="bench-1",
               effort="medium", price=1.2, capability_approved=True)],
    policy,
)
assert selection.model_ids == ("pool/example-model",)
```

```bash
python -m agent_core.model_selection explain --config /etc/agent-model-selection/catalog.json
python -m agent_core.model_selection refresh --config /etc/agent-model-selection/catalog.json
```

`explain` prints the eligible identities and the reason each other candidate was rejected. Secret separation and the refresh launcher are in [usage-model-discovery.md](usage-model-discovery.md).

## Pricing

Providers do not reliably report cost. Some return no usage at all, so a derived figure is often the only cost number that exists. Rates are USD per million tokens.

```python
from agent_core.pricing import cost_for_turn, is_priced

cost = cost_for_turn(result)          # provider figure if reported, else derived
if not is_priced(result.model_id):
    ...                               # newly adopted model, priced at defaults
```

Longest match wins, so a general `gpt-5.4` rule does not shadow `gpt-5.4-mini` based on declaration order. Unknown models are visible: `estimate_cost` still falls back, because a missing rate must not break reporting, but `is_priced()` reports the gap and the fallback is logged once per model. Pass `table=` to override rates per consumer.

## Git

A consumer clones a project into a workspace, lets an agent work in it, and pushes the result, authenticating with an SSH key or an access token.

```python
from agent_core.git import BotIdentity, GitCredentials, GitWorkspace

ws = GitWorkspace(
    "/var/cache/repos",
    GitCredentials(ssh_key_path="/opt/keys/id_ed25519"),
)
repo = ws.prepare("git@git.example.com:group/app.git", branch="main")
head = ws.query(repo, "rev-parse", "HEAD")
ws.commit_all_and_push(
    repo, branch="feature/x", message="generated tests",
    identity=BotIdentity("agent-bot", "bot@local"),
)
```

Credentials travel in the subprocess environment. Nothing is written to `~/.ssh/config` or global git config, so concurrent tasks using different identities cannot interfere. A per-repository lock serialises access to each cache directory.

- `prepare()` leaves HEAD detached at `origin/<branch>`. `commit_all_and_push` moves HEAD onto the branch first. Without that, the commit is unreachable and `push` reports "Everything up-to-date".
- Fetch writes the remote-tracking ref explicitly. Plain `git fetch origin <branch>` updates only `FETCH_HEAD`, so a later checkout of `origin/<branch>` returns the previous commit.
- `token_host` has no default. An auth header sent to the wrong host is a credential leak.

`output()` is best-effort for optional probes. `query()` raises `GitCommandError` and should be used when missing evidence must fail the task. Set `clone_depth=None` for workflows that must retain full history. Normal task workspaces stay shallow.
