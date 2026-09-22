# Usage: Spec-Gen Agent-Core Adoption

Work type: non-Jira. Operators of spec-generator-agent and maintainers of
agent-core / UTA / CR.

## Setup

Corbell pins agent-core **v0.8.3** with extras
`api,langgraph,yaml`. Last UTA/CR pins stay on **0.7.x** until those
products opt in. Do not mix a 0.7 consumer with 0.8 core.

## Commands

```bash
# agent-core
.venv/bin/python -m pytest
.venv/bin/pip wheel . --no-deps -w /tmp/agent-core-wheel

# Corbell
.venv/bin/python -m pytest
spec-generator-agent tasks daemon
```

Task stop: HTTP `POST /api/v1/spec-tasks/{id}/stop` or
`spec-generator-agent tasks stop ID`. Intent cancel still pauses *desired*
work, not a running agent child.

## Behavior Changes For Operators

- Daemon restart resumes the LangGraph lineage (`started` / `resumed` /
  `reused_completed`). A corrupt checkpoint fails; mint a new task rather
  than deleting the saver file to "fix" it.
- Page writers still run concurrently up to the configured lane count.
  After four isolated page failures, remaining pages are not started and
  **the task fails**. Review does not run on a partial writer set.
- Wiki publish is `--atomic --force-with-lease` on the docs + lease refs.
  No merge request. Lease loss updates neither ref.
- Product tests use `agent_core.harness.FakeOpenCodeProcess`. SpecGenAgent no
  longer owns a process, fallback, stream parser, or shutdown implementation.
- Wrong agent-core version: process refuses to start (pin test).

## Rollout

1. Release agent-core 0.8.x; the adoption pin is `v0.8.3`.
2. Deploy SpecGenAgent with the matching pin.
3. Run the canaries below before production rollout.

## Operator Canaries

Run from the SpecGenAgent checkout. These are local and do not publish a
remote documentation branch.

```bash
# Stop/cancel reaches the shared control plane and the task becomes stopped.
.venv/bin/pytest -q tests/test_tasks.py -k 'core_control or stop_control'

# Graceful daemon shutdown and durable graph continuation remain observable.
.venv/bin/pytest -q \
  tests/test_task_scheduler_orchestration.py \
  tests/test_context_prepare.py \
  -k 'shutdown or resumed or reused_completed'

# Dry-run both docs graph levels with fakes; no model or Git remote is used.
.venv/bin/pytest -q \
  tests/test_spec_docs_service_workflow.py \
  tests/test_spec_docs_outer_workflow.py
```

For the rollout canary, queue one real `context-prepare` and one real
`spec-docs` task, stop the first through the API, send `SIGTERM` to the daemon
during the second, restart it, and require a `resumed` or `reused_completed`
disposition before allowing publish.

## Rollback

Revert SpecGenAgent to the pre-adoption dependency and workflow commits as one
release. Do not downgrade only agent-core under the new workflow code. Leave
UTA/CR on 0.7.x until they migrate.

## Support

Logs must not contain wiki page bodies or prompt bytes. Useful signals:
resume disposition, cancelled turn, plan-gate fail, page-failure budget,
lease-lost on publish.
