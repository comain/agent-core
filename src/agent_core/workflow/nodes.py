"""Workflow steps every consumer shares.

Three things are the same in all four products: get the repository onto disk,
render a prompt from a template, and run an agent turn against it. They are
registered here so a product's workflow can name them and implement only what
is genuinely its own.

Each is an ordinary function taking ``(state, config, context)`` — no graph is
needed to call one, which is how they are tested.

**Context keys** are the run-wide objects a node cannot get from serialisable
state:

``workspace``   an :class:`agent_core.git.GitWorkspace`
``runner``      something with ``run_turn(prompt_file=…, repo_path=…, …)``
``prompts``     an :class:`agent_core.prompts.PromptLibrary`
``runtime_store``  a :class:`agent_core.runtime.RuntimeStore` (human_gate)
``is_cancelled``  optional ``() -> bool``, checked before long steps
``turn_dir``    optional directory; when set, a prompt *file* must sit under it
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Mapping

from agent_core.harness.execution import AgentTurnRequest, execute_agent_turn
from agent_core.harness.structured_output import extract_json_object
from agent_core.harness.turn_result import AgentTurnResult
from agent_core.workflow.registry import shared_registry

logger = logging.getLogger(__name__)


class MissingContextError(RuntimeError):
    """A shared node was given no way to do its work."""


def _require(context: Mapping[str, Any], key: str, node: str):
    try:
        return context[key]
    except KeyError:
        raise MissingContextError(
            f"node {node!r} needs context[{key!r}]; got: {sorted(context) or '(nothing)'}"
        ) from None


def _cancelled(context: Mapping[str, Any]) -> bool:
    check = context.get("is_cancelled")
    return bool(check and check())


@shared_registry.node("prepare_workspace")
def prepare_workspace(state, config, context) -> Dict[str, Any]:
    """Clone or refresh the repository and check out what is under review.

    Reads ``repo_url``, ``branch`` and optional ``commit_id`` from the state;
    writes back ``repo_path`` and the ``commit_id`` actually checked out.

    Resolving the commit matters even when the caller supplied one: a task
    triggered on a branch records the tip *at trigger time*, and reporting a
    review against a commit that is not the one reviewed makes the report
    impossible to trust.
    """
    workspace = _require(context, "workspace", "prepare_workspace")

    repo_url = state.get("repo_url") or config.get("repo_url")
    branch = state.get("branch") or config.get("branch")
    if not repo_url or not branch:
        raise MissingContextError("prepare_workspace needs repo_url and branch in the state")

    scope = state.get("task_ref") or config.get("scope")
    local = Path(str(repo_url))
    if (
        not scope
        and local.exists()
        and (local / ".git").exists()
    ):
        # A path rather than a URL: development and tests, with nothing to
        # clone. Skipped when ``scope`` is set so two tasks do not share a
        # checkout.
        repo_path = local
    else:
        repo_path = workspace.prepare(
            str(repo_url),
            branch=str(branch),
            commit=state.get("commit_id") or None,
            scope=str(scope) if scope else None,
            # A workflow that writes code owns its branch: told to work on one
            # the remote does not have, it starts it from trunk rather than
            # stopping. A reviewing workflow leaves this off and treats a
            # missing branch as a mistake in the request.
            create_missing=bool(context.get("create_branch")),
            local_branch=bool(context.get("create_branch")),
            is_cancelled=context.get("is_cancelled"),
        )

    resolved = workspace.current_commit(repo_path)
    logger.info("prepared %s at %s", Path(repo_path).name, resolved[:12])
    return {"repo_path": str(repo_path), "commit_id": resolved}


@shared_registry.node("render_prompt")
def render_prompt(state, config, context) -> Dict[str, Any]:
    """Render a template into the turn directory.

    ``config`` names the ``template``, the ``into`` subdirectory and which
    state keys to expose as ``values``; passing the whole state to a template
    would make every prompt depend on every earlier step.
    """
    prompts = _require(context, "prompts", "render_prompt")
    template = config.get("template")
    if not template:
        raise MissingContextError("render_prompt needs config['template']")

    keys = config.get("values")
    values: Dict[str, Any] = (
        {k: state.get(k) for k in keys} if keys else dict(state)
    )
    values.update(config.get("extra") or {})

    directory = Path(state.get("turn_dir") or state.get("repo_path") or ".") / str(
        config.get("into") or "prompt"
    )
    artifact = prompts.render_to_file(template, directory=directory, values=values)
    return {"prompt_file": str(artifact.path), "prompt_inputs_file": str(artifact.inputs_path)}


def _is_legacy(config: Mapping[str, Any]) -> bool:
    """`result_mode` is the single activation switch.

    Anything else -- a `session_scope` set on an old workflow, an `attempts`
    someone added hopefully -- leaves the node on exactly today's path. A
    partial opt-in, where sessions and guards engage on a workflow never
    designed for them, is the failure this boundary exists to prevent.
    """
    return str(config.get("result_mode") or "legacy") != "normalized"


def _resolve(config: Mapping[str, Any], state: Mapping[str, Any], key: str) -> Any:
    """A literal, or a value pulled from state.

    ``{"from_state": "key"}`` is resolved here rather than by each workflow, and
    a missing key yields ``None`` -- never the mapping itself, which would be
    sent to a provider as a model id.
    """
    value = config.get(key)
    if isinstance(value, Mapping) and "from_state" in value:
        return state.get(str(value["from_state"]))
    return value


def _project(execution, config: Mapping[str, Any]) -> Dict[str, Any]:
    """The only thing that crosses back into graph state: JSON, no objects."""
    result: AgentTurnResult = execution.result
    output_key = str(config.get("output_key") or "turn_result")
    projected = {
        output_key: result.as_dict(),
        "turn_status": result.status,
        "turn_text": result.text,
    }
    if config.get("parse") == "json":
        projected["payload"] = execution.outcome.payload
    return projected


def _is_under(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _confine_prompt_file(prompt_file, repo_path, context) -> Path:
    """Planner files live in turn_dir, never in the wiki checkout."""
    path = Path(prompt_file).expanduser().resolve()
    wiki = (Path(repo_path) / "docs" / "spec").resolve()
    if _is_under(path, wiki):
        raise ValueError(
            f"prompt_file {path} sits under the wiki tree {wiki}; "
            "agent_turn refuses files under repo_path/docs/spec"
        )
    turn_dir = context.get("turn_dir")
    if turn_dir:
        root = Path(str(turn_dir)).expanduser().resolve()
        if not _is_under(path, root):
            raise ValueError(f"prompt_file {path} is not under turn_dir {root}")
    return path


def _parse(config: Mapping[str, Any]):
    value = config.get("parse")
    if value in (None, "", "none"):
        return None
    if value == "json":
        return extract_json_object
    raise ValueError(f"unknown parse {value!r}; expected 'json'")


def _message(state, config) -> str | None:
    value = _resolve(config, state, "message")
    if value is None:
        value = state.get("message")
    text = str(value) if value is not None else ""
    return text or None


def _turn_request(state, config, prompt: Path | str, repo_path) -> AgentTurnRequest:
    """Everything the executor needs, read off state and config.

    Nothing live crosses this boundary in either direction: the request carries
    values and one prompt callable, and the answer comes back as a mapping. A
    client, a sink or a settings object reaching graph state is how a
    checkpoint stops being serializable -- or worse, starts holding a prompt.
    """

    def render(attempt, feedback):
        return prompt

    return AgentTurnRequest(
        name=str(config.get("label") or "agent_turn"),
        repo_path=Path(repo_path),
        prompt=render,
        operation_id=str(state.get("operation_id") or ""),
        model_id=_resolve(config, state, "model_id"),
        effort_strategy=str(state.get("effort_strategy") or "default"),
        attempts=int(config.get("attempts", 1)),
        timeout_seconds=_resolve(config, state, "timeout_seconds"),
        on_failure=str(config.get("on_failure") or "fail"),
        session_scope=str(config.get("session_scope") or "none"),
        progress_detail=str(config.get("progress_detail") or "summary"),
        parse=_parse(config),
        delivery=_resolve(config, state, "delivery"),
        title=_resolve(config, state, "title"),
        pure=_resolve(config, state, "pure"),
    )


@shared_registry.node("agent_turn")
def agent_turn(state, config, context) -> Dict[str, Any]:
    """Run one agent turn against the prepared workspace.

    Legacy mode still takes a prompt *file*: a long prompt on a command line
    hits the argument length limit, and every consumer had independently
    arrived at writing it to disk first.

    Normalized mode takes an in-memory ``message`` or a file. A file must not
    sit under ``repo_path/docs/spec`` (the wiki tree) and, when
    ``context["turn_dir"]`` is set, must sit under that directory. ``parse:
    json`` retries via ``attempts`` using ``extract_json_object``.

    A failed turn is returned as state rather than raised. Whether a failure
    ends the run is the workflow's decision -- expressed as a branch -- not
    this node's.

    Two modes. Without ``result_mode: normalized`` this is exactly the direct
    single-turn path it has always been, returning the harness's own result
    object; that is what cr_plugin runs in production today. With it, the node
    is a projection onto `execute_agent_turn`: it needs
    ``context["turn_context"]`` -- one typed `AgentTurnContext` holding the
    harness binding and whichever ports the product implements -- and returns
    a JSON-safe `AgentTurnResult` mapping.
    """
    repo_path = state.get("repo_path")
    if not repo_path:
        raise MissingContextError("agent_turn needs repo_path in the state; run prepare_workspace first")

    prompt_file = state.get("prompt_file")
    message = _message(state, config)

    if _is_legacy(config):
        if not prompt_file:
            raise MissingContextError(
                "agent_turn needs prompt_file in the state; run render_prompt first"
            )
        return _legacy_turn(
            _require(context, "runner", "agent_turn"),
            state,
            config,
            context,
            prompt_file,
            repo_path,
        )

    if prompt_file and message:
        raise ValueError("agent_turn takes message or prompt_file, not both")
    if message:
        prompt: Path | str = message
    elif prompt_file:
        prompt = _confine_prompt_file(prompt_file, repo_path, context)
    else:
        raise MissingContextError(
            "agent_turn needs prompt_file or message in the state; run render_prompt first"
        )

    # Normalized mode is a projection and nothing else: state and config in, a
    # JSON mapping out. The lifecycle -- cancellation, session, guard, cost,
    # progress, durability -- belongs to `execute_agent_turn`, which a
    # product's direct call path uses too. Two copies of that ordering, drifting
    # apart, is what this deletes.
    execution = execute_agent_turn(
        _turn_request(state, config, prompt, repo_path),
        _require(context, "turn_context", "agent_turn"),
    )
    return _project(execution, config)


def _legacy_turn(runner, state, config, context, prompt_file, repo_path):
    """Today's path, byte for byte. New config keys are not read here."""
    if _cancelled(context):
        return {"turn_status": "cancelled", "turn_result": None}

    result = runner.run_turn(
        prompt_file=Path(prompt_file),
        repo_path=Path(repo_path),
        model_id=config.get("model_id"),
        timeout_seconds=config.get("timeout_seconds"),
        is_cancelled=context.get("is_cancelled"),
    )

    output_key = str(config.get("output_key") or "turn_result")
    status = getattr(result, "type", "unknown")
    logger.info("turn %s finished status=%s", config.get("label") or "", status)
    return {
        output_key: result,
        "turn_status": status,
        "turn_text": getattr(result, "result", "") or "",
    }


@shared_registry.selector("turn_succeeded")
def turn_succeeded(state) -> str:
    """Branch key for the commonest decision: did the turn produce an answer?"""
    return "ok" if state.get("turn_status") == "completed" else "failed"


@shared_registry.node("human_gate")
def human_gate_node(state, config, context) -> Dict[str, Any]:
    """Suspend until a human answers. LangGraph is imported only when this runs."""
    from agent_core.gates.langgraph import human_gate as open_human_gate

    store = _require(context, "runtime_store", "human_gate")
    task_ref = state.get("task_ref")
    if not task_ref:
        raise MissingContextError("human_gate needs task_ref in the state")

    prompt: Dict[str, Any] = {}
    for key in config.get("prompt_keys") or ():
        prompt[str(key)] = state.get(key)
    artifact_key = config.get("artifact_key")
    if artifact_key:
        prompt["artifact_markdown"] = state.get(str(artifact_key)) or ""

    attempt_key = config.get("attempt_key")
    attempt = str(state.get(attempt_key) or "") if attempt_key else ""
    graph_node = str(config.get("graph_node") or "human_gate")
    answer = open_human_gate(
        store,
        task_ref=str(task_ref),
        node=graph_node,
        kind=str(config.get("kind") or "input"),
        prompt=prompt,
        config=config,
        response_schema=config.get("response_schema")
        or {"decision": "approve|reject", "comments": "string"},
        attempt=attempt,
    )
    if not isinstance(answer, Mapping):
        answer = {"decision": answer}
    decision_key = str(config.get("decision_key") or "decision")
    comments_key = str(config.get("comments_key") or "comments")
    return {
        decision_key: answer.get("decision"),
        comments_key: answer.get("comments") or "",
    }


@shared_registry.selector("gate_decision")
def gate_decision(state) -> str:
    """Branch on a human gate's ``decision``. Empty is an error, not a default."""
    key = str((state or {}).get("decision") or "")
    if not key:
        raise KeyError("gate_decision: state has no decision")
    return key
