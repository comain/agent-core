# agent-core

Shared execution and service capabilities for AI development products.

[Usage](docs/usage.md) · [Changelog](CHANGELOG.md) · [Decisions](docs/decisions/) · [Model selection operations](docs/usage-model-discovery.md)

agent-core is the library behind [`cragent`](https://github.com/comain/code-review-agent), [`unit-test-agent`](https://github.com/comain/unit-test-agent), [`spec_generator_agent`](https://github.com/comain/spec_generator_agent), and `dev-flow-agent`. A product selects an agent by configuration and composes the packages below. Product code keeps domain policy and does not import OpenCode, Pi, or any future agent implementation directly.

## Features

- **Harness** — configured agent turns, reusable sessions, fallback, and shutdown, behind one contract.
- **Model selection** — admit models from a cached catalog and an application policy, with provider order as fallback priority.
- **Git** — clone, inspect, commit, and push with credentials that never touch global git config.
- **Prompts and profiles** — strict templates, reproducible artifacts, and YAML/JSON agent profiles.
- **Workflow** — declarative graphs, shared nodes, and checkpointed invocation.
- **Runtime** — task store, bounded progress, artifacts, attachments, and server-sent events.
- **Identity and gates** — proxy-asserted principals, human approval, and an inbox.
- **Delivery** — trigger protocols and GitLab, mounted separately from the task API.

## Requirements

Python >= 3.11. That floor is the oldest interpreter a consumer runs in production: [unit-test-agent](https://github.com/comain/unit-test-agent) production is 3.11.15, its beta and [spec_generator_agent](https://github.com/comain/spec_generator_agent) production are 3.12, and [cragent](https://github.com/comain/code-review-agent) runs 3.13. Verified on 3.11, 3.12, and 3.13.

## Getting started

```bash
pip install -e /path/to/agent-core
```

Optional extras: `pip install "agent-core[langgraph]"` for human gates, `pip install "agent-core[api]"` for the task router, and `pip install "agent-core[yaml]"` for YAML workflows.

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

Sessions, prompts, configuration, and the other packages are in [Usage](docs/usage.md).

## Packages

Import from the package that owns the capability. Symbols not listed in [Usage](docs/usage.md) are private.

| Package | Responsibility | Start here |
|---|---|---|
| `agent_core.harness` | Turns, sessions, fallback, records | `HarnessSpec`, `create_configured_harness`, `open_harness_session`, `run_harness_node` |
| `agent_core.model_selection` | Catalog admission and ranking | `ModelPolicy`, `resolve_selection` |
| `agent_core.git` | Clone, query, commit, push | `GitWorkspace`, `GitCredentials`, `BotIdentity` |
| `agent_core.prompts` | Templates and prompt artifacts | `PromptLibrary`, `materialize_prompt`, `opaque_artifact_id` |
| `agent_core.profiles` | YAML/JSON agent profiles | `AgentProfile`, `ProfileRegistry` |
| `agent_core.workflow` | Specs, nodes, checkpoints | `WorkflowSpec`, `invoke_workflow`, `open_checkpointer` |
| `agent_core.runtime` | Store, progress, daemon, artifacts | `RuntimeStore`, `TaskDaemon`, `stream_task_events` |
| `agent_core.identity` | Principals and authorization | `IdentityResolver`, `Policy` |
| `agent_core.gates` | LangGraph human gates (optional) | `human_gate`, `resume_answered_gates` |
| `agent_core.ui` | Approval inbox rendering | `render_inbox`, `gate_to_dict` |
| `agent_core.integrations` | Triggers and delivery | `TriggerProtocol`, `create_trigger_router`, `GitLabClient` |
| `agent_core.api` | Task and gate HTTP surface (optional) | `create_task_router` |
| `agent_core.db` | Ordered SQLite migrations | `Migration`, `apply_migrations` |

The harness contract is agent-neutral. OpenCode types are exported for products that still call that adapter directly; new workflow code should not branch on them.

## Layout

```
src/agent_core/
  harness/            turns, sessions, fallback, OpenCode adapter
  model_selection/    catalog admission, ranking, refresh CLI
  git/                credentials, workspace, publish
  workflow/           specs, nodes, checkpoints
  runtime/            store, progress, daemon, artifacts
  identity/           principals and policy
  gates/              LangGraph suspend/resume
  integrations/       triggers, GitLab, delivery
  api/                shared task router
```

## Documentation

| Area | Guide |
|---|---|
| Using the packages | [Usage](docs/usage.md) |
| Model catalog refresh | [Operations](docs/usage-model-discovery.md) |
| Why the contracts look like this | [Decisions](docs/decisions/) |
| Release history | [Changelog](CHANGELOG.md) |
| Harness parity with the source | [Parity](docs/parity.md) |

## Testing

```bash
.venv/bin/python -m pytest
```

Behavioural parity against the source harness is `tools/parity_check.py`. Evidence is in [docs/parity.md](docs/parity.md).

## Project lineage

The OpenCode harness was extracted from [`unit-test-agent`](https://github.com/comain/unit-test-agent) at `0d70115783fc4cd075a26e04909585ba2f34b288`. This public repository has its own history. The port snapshot and the intentional layout differences are recorded in [docs/port-baseline.md](docs/port-baseline.md). `PROJECT_ROOT` is not part of this package: the source derived a repository root from package depth, and that expression does not point at a repository root here.
