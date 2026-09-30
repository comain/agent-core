"""OpenCode as a harness, behind the neutral interface.

The pieces -- spawning the process, parsing the stream, walking the provider
chain, building a per-turn workspace -- already lived here. What did not was
one object assembling them, so the only consumer assembled them itself and
the assembly became product code: forty lines that every future consumer
would have had to write again, and get right again.

Three of those lines are not policy at all:

**A workspace per attempt.** The generated `opencode.json` names the model,
so attempts sharing a directory means a fallback re-runs the model that just
failed. That is a property of this implementation, not a preference, so it is
the default here rather than something a caller remembers to pass.

**The configuration is entered per attempt.** The config names the model the
workspace is generated for, so the attempt's model has to be the one in scope
when it is built -- not the one the turn started with.

**Candidates are resolved at turn time.** Health changes between turns; a
chain fixed at construction would keep offering a model that has since
started failing.

Products use the neutral `HarnessSpec` API. Translation from its portable
policy and opaque options into `HarnessConfig` stays here with the
implementation that understands those options.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, Union

from agent_core.config import HarnessConfig, current_config, use_config
from agent_core.harness.lifecycle import (
    BootstrapResult,
    HarnessReadiness,
    ReadinessStatus,
    WorkspaceBootstrapRequest,
)
from agent_core.harness.records import token_usage_from_turn
from agent_core.harness.process import OpenCodeProcess
from agent_core.harness.registry import HarnessSpec, TurnProgress
from agent_core.harness.runner import TurnResult, run_turn_with_fallback
from agent_core.harness.sessions import (
    AgentSessionRef,
    DiscoveryExecutionPolicy,
    FallbackHarnessSession,
    HarnessSession,
    SessionLocatorScope,
    SessionSnapshot,
)
from agent_core.harness.tiered_router import available_provider_candidates, opencode_model_id
from agent_core.harness.workspace import per_turn_workspace
from agent_core.model_selection.configuration import load_selection_config
from agent_core.model_selection.runtime import DiscoveryRuntime

#: Either a ready configuration, or how to build one for a given model and
#: repository -- products whose configuration names the model or the directory
#: need the second.
ConfigSource = Union[HarnessConfig, Callable[[Optional[str], Path], HarnessConfig], None]
SessionClientFactory = Callable[[Path], Any]

logger = logging.getLogger(__name__)

#: What this implementation says to itself to introduce a repository. A product
#: supplying its own prompt gets that instead; this is the fallback for a
#: bootstrap request that names only a purpose.
INIT_INSTRUCTION = (
    "Initialize this repository and create AGENTS.md with project-specific guidance."
)

#: The probe turn: the cheapest thing that still proves the provider answered
#: with this model's credentials. Kept identical to the confirmation turn
#: products already run at startup.
READINESS_PROMPT = "Reply with only: OK"
_READINESS_TOKEN = "OK"

#: Provider messages that mean the key itself is wrong rather than the request.
_INVALID_KEY_MARKERS = (
    "invalid_api_key",
    "incorrect api key",
    "api key is missing",
)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_]")


def _readiness_from_turn(result: Any) -> HarnessReadiness:
    """Turn one probe turn into a neutral answer, keeping the payload private.

    The order of these checks is the order the product-side probe used, which
    matters: a rate-limited turn carries a payload in the same field an auth
    failure does, and classifying it second is how it stays retryable.
    """
    kind = str(getattr(result, "type", "") or "")
    text = str(getattr(result, "result", "") or "")
    error = getattr(result, "error", None)
    error = error if isinstance(error, Mapping) else {}
    name = str(error.get("name") or "")
    data = error.get("data")
    message = str((data or {}).get("message") or "").lower() if isinstance(data, Mapping) else ""

    if kind == "completed" and _READINESS_TOKEN in text:
        return HarnessReadiness(ready=True, status=ReadinessStatus.READY)

    if kind == "rate_limited":
        retry_after = error.get("retry_after_seconds")
        wait = f"; retry after {int(retry_after)}s" if isinstance(retry_after, (int, float)) else ""
        return _unavailable(f"the provider rate limited the readiness probe{wait}")

    if name == "ProviderAuthError" or (
        name == "APIError" and any(marker in message for marker in _INVALID_KEY_MARKERS)
    ):
        return HarnessReadiness(
            ready=False,
            status=ReadinessStatus.AUTHENTICATION_REQUIRED,
            detail="the provider rejected the configured credentials; authenticate the agent",
        )

    if kind == "timeout":
        return _unavailable("the readiness probe timed out before the model replied")
    if kind == "cancelled":
        return _unavailable("the readiness probe was cancelled")
    if kind == "completed":
        return _unavailable("the model replied without the expected confirmation")

    qualifier = f" ({_SAFE_NAME.sub('', name)})" if name else ""
    return _unavailable(f"the readiness probe did not complete{qualifier}")


def _unavailable(detail: str) -> HarnessReadiness:
    return HarnessReadiness(ready=False, status=ReadinessStatus.UNAVAILABLE, detail=detail)


_TOOL_PROGRESS = re.compile(r"^tool\[([^\]]+)]\s+([^\s]+)(?:\s+-\s+(.*))?$")
_WAITING_PROGRESS = re.compile(r"^waiting:\s+tool\[([^\]]+)]")
_MODEL_FALLBACK_PROGRESS = re.compile(
    r"^model-fallback:\s+(\S+)\s+->\s+(\S+)\s+reason=([a-z0-9_]+)$"
)


def _translate_progress(message: str) -> TurnProgress:
    """Translate OpenCode's log-oriented update into the neutral contract."""
    if match := re.fullmatch(r"model-selected: (\S+) effort=(\S+)", message):
        return TurnProgress(kind="model_selected", message=message,
                            detail=match.group(1), status=match.group(2))
    if match := _TOOL_PROGRESS.match(message):
        return TurnProgress(
            kind="tool",
            message=message,
            tool=match.group(1),
            status=match.group(2),
            detail=match.group(3) or None,
        )
    if message.startswith("step:"):
        detail = message.partition(":")[2].strip()
        return TurnProgress(kind="step", message=message, status=detail.split(" ", 1)[0])
    if message.startswith("text:"):
        return TurnProgress(
            kind="text",
            message=message,
            detail=message.partition(":")[2].strip() or None,
        )
    if message.startswith("reasoning:"):
        detail = message.partition(":")[2].strip()
        return TurnProgress(
            kind="reasoning",
            message=message,
            detail=detail if detail and detail != "updated" else None,
        )
    if message.startswith("error:"):
        return TurnProgress(kind="error", message=message, status="error")
    if message.startswith("connection:"):
        return TurnProgress(kind="connection", message=message)
    if match := _WAITING_PROGRESS.match(message):
        return TurnProgress(kind="waiting", message=message, tool=match.group(1), status="waiting")
    if message.startswith("rate-limit:"):
        return TurnProgress(kind="rate_limit", message=message, status="limited")
    if match := _MODEL_FALLBACK_PROGRESS.match(message):
        return TurnProgress(
            kind="model_fallback",
            message=message,
            status=match.group(3),
            detail=f"{match.group(1)} -> {match.group(2)}",
        )
    return TurnProgress(kind="native", message=message)


class OpenCodeHarness:
    """Runs a turn through OpenCode, walking the provider chain on failure.

    Walking the chain means one `run_turn` can submit to a provider several
    times, so this counts its own paid attempts rather than being counted once
    by its caller.
    """

    #: Read by `run_harness_node`: hand the ledger over, do not wrap.
    accepts_paid_attempts = True

    def accepts_policy(self, _spec: Any) -> bool:
        """OpenCode projects portable policy into its native sandbox config."""

        return True

    def __init__(
        self,
        config: ConfigSource = None,
        *,
        process: Optional[OpenCodeProcess] = None,
        models: Optional[Sequence[str]] = None,
        timeout: int = 3600,
        workspace_factory: Optional[Callable[[str], Any]] = None,
        isolate_attempts: bool = True,
        session_client_factory: Optional[SessionClientFactory] = None,
        discovery_runtime: Optional[DiscoveryRuntime] = None,
    ) -> None:
        self.config = config
        self.process = process or OpenCodeProcess()
        self.models = models
        self.timeout = timeout
        self.workspace_factory = workspace_factory
        self.isolate_attempts = isolate_attempts
        self.session_client_factory = session_client_factory
        self.discovery_runtime = discovery_runtime
        self._discovery_policy: Optional[DiscoveryExecutionPolicy] = None
        self._candidate_options: Optional[Callable[[str], Mapping[str, Any]]] = None

    def _resolved_harness(self, repo: Path, *, effort_strategy: str = "default") -> "OpenCodeHarness":
        """Bind one invocation, leaving this shared harness and active sessions unchanged."""
        if self.discovery_runtime is None:
            return self
        runtime = self.discovery_runtime
        selection = runtime.resolve(**({"effort_strategy": effort_strategy} if effort_strategy != "default" else {}))
        with self._configured(selection.model_ids[0], repo):
            if not current_config().opencode_provider_fallback_enabled:
                selection = replace(selection, candidates=selection.candidates[:1])
        configs = {}
        for model in selection.model_ids:
            base = self._config_for(model, repo) or current_config()
            config = base.model_copy(update=runtime.config_updates(selection, model), deep=True)
            # Reject unsupported effort before a ledger admits any candidate.
            with use_config(config):
                from agent_core.harness.config import build_opencode_config_dict

                build_opencode_config_dict(str(repo), model_id=model)
            configs[model] = config

        def config_for(model: Optional[str], _repo: Path) -> HarnessConfig:
            return configs[model or selection.model_ids[0]]

        child = OpenCodeHarness(
            config_for, process=self.process, models=selection.model_ids,
            timeout=self.timeout, workspace_factory=self.workspace_factory,
            isolate_attempts=self.isolate_attempts,
            session_client_factory=self.session_client_factory,
        )
        child._discovery_policy = runtime
        child._candidate_options = lambda model: {"variant": configs[model].opencode_variant}
        return child

    def _config_for(self, model_id: Optional[str], repo_path: Path) -> Optional[HarnessConfig]:
        if self.config is None or isinstance(self.config, HarnessConfig):
            return self.config
        return self.config(model_id, repo_path)

    def _configured(self, model_id: Optional[str], repo_path: Path):
        config = self._config_for(model_id, repo_path)
        return use_config(config) if config is not None else nullcontext()

    def preferred_model(self, repo_path: Optional[Path] = None) -> Optional[str]:
        """The model this harness would use for a turn started now.

        Callers need it for cost attribution: a turn the provider did not
        attribute has to be priced against something, and the honest answer is
        whichever model would have run. Asking the harness keeps that out of
        product code, which should not know that models come from a provider
        chain at all.

        Resolved on each call rather than cached, for the same reason the
        chain is: an answer from ten minutes ago may name a model that has
        since been marked unhealthy.
        """
        self = self._resolved_harness(Path(repo_path).resolve() if repo_path else Path.cwd())
        if self.models:
            return self.models[0]
        with self._configured(None, Path(repo_path) if repo_path else Path.cwd()):
            candidates = available_provider_candidates()
            return opencode_model_id(candidates[0]) if candidates else None

    def prepare_workspace(self, *, repo_path: Path) -> None:
        """Write the project configuration this implementation runs against.

        The neutral capability from `agent_core.harness.lifecycle`. It returns
        nothing on purpose: the config path, the provider, and the model are
        this implementation's business, and handing them back is how a product
        starts branching on which agent it selected.
        """
        repo = Path(repo_path).resolve()
        self = self._resolved_harness(repo)
        with self._configured(None, repo):
            from agent_core.harness.config import generate_opencode_config

            generate_opencode_config(str(repo))

    def check_readiness(self, *, repo_path: Path, timeout_seconds: int) -> HarnessReadiness:
        """Run one confirmation turn and say what it means, not what it said.

        Retrying is the caller's policy (`check_harness_readiness` owns it), so
        this is exactly one probe. Everything the provider returned stays here:
        a product gets one of three states and a sanitized sentence.
        """
        repo = Path(repo_path).resolve()
        if self.discovery_runtime is not None:
            invocation = self._resolved_harness(repo)
            # Readiness remains a single-candidate probe, not a fallback chain.
            invocation.models = invocation.models[:1]
            return _readiness_from_turn(invocation.run_turn(
                repo_path=repo, message=READINESS_PROMPT, timeout_seconds=timeout_seconds,
            ))
        with self._configured(None, repo):
            model = self.models[0] if self.models else (current_config().opencode_model or None)
        with self._configured(model, repo):
            result = self.process.run_turn(
                READINESS_PROMPT,
                repo_path=str(repo),
                model_id=model,
                timeout=int(timeout_seconds),
            )
        return _readiness_from_turn(result)

    def bootstrap_workspace(
        self, *, repo_path: Path, request: WorkspaceBootstrapRequest
    ) -> BootstrapResult:
        """Let this agent look around the repository once, in its own session.

        A session of its own rather than the product's: a bootstrap turn edits
        files and fills a context window, and doing that inside a working
        conversation changes what every later turn sees. It is opened here,
        closed in `finally`, and closed once.

        Without a prompt this uses OpenCode's own initialisation; with one it
        is an ordinary turn. Either way the product gets the neutral result and
        decides for itself whether the text or the files are worth keeping.
        """
        repo = Path(repo_path).resolve()
        if self.discovery_runtime is not None:
            started = time.monotonic()
            result = self.run_turn(
                repo_path=repo, prompt_file=request.prompt_file,
                message=INIT_INSTRUCTION if request.prompt_file is None else None,
                timeout_seconds=request.timeout_seconds,
            )
            return BootstrapResult(
                completed=result.type == "completed", session_id=result.session_id,
                output_text=result.result, duration_seconds=round(time.monotonic() - started, 3),
                usage=token_usage_from_turn(result),
            )
        with self._configured(None, repo):
            model = self.models[0] if self.models else (current_config().opencode_model or None)

        started = time.monotonic()
        session = self._open_single_session(
            repo=repo, model_id=model, permissions=None, variant=None
        )
        try:
            if request.prompt_file is not None:
                result = session.run_turn(
                    prompt_file=request.prompt_file,
                    repo_path=repo,
                    timeout_seconds=request.timeout_seconds,
                )
            else:
                result = session.initialize(request.timeout_seconds)
        finally:
            # Never allowed to replace what actually happened: a failed cleanup
            # is a warning, and a succeeded bootstrap is still a success.
            try:
                session.close()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("failed to close the bootstrap session: %s", exc)

        return BootstrapResult(
            completed=str(getattr(result, "type", "")) == "completed",
            session_id=getattr(result, "session_id", None) or None,
            output_text=str(getattr(result, "result", "") or ""),
            duration_seconds=round(time.monotonic() - started, 3),
            usage=token_usage_from_turn(result),
        )

    def run_turn(
        self,
        *,
        prompt_file: Optional[Path] = None,
        message: Optional[str] = None,
        repo_path: Path,
        model_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        on_progress: Optional[Callable[[TurnProgress], None]] = None,
        on_update: Optional[Callable[[str], None]] = None,
        paid_attempts: Any = None,
        session_id: Optional[str] = None,
        delivery: str = "argv",
        title: Optional[str] = None,
        pure: Optional[bool] = None,
        env: Optional[Mapping[str, str]] = None,
        project_dir: Optional[str] = None,
        bootstrap_message: Optional[str] = None,
        **_ignored: Any,
    ) -> TurnResult:
        if on_progress is not None and on_update is not None:
            raise ValueError("pass only one progress callback: on_progress or on_update")
        if (message is None) == (prompt_file is None):
            raise ValueError("pass exactly one of message= or prompt_file=")
        native_update = on_update
        if on_progress is not None:
            native_update = lambda message: on_progress(_translate_progress(message))

        repo = Path(repo_path).resolve()
        prompt = Path(prompt_file) if prompt_file is not None else None
        label = (prompt.parent.name if prompt is not None else None) or title or "turn"
        self = self._resolved_harness(repo)
        if self._discovery_policy is not None:
            model_id = None
            if session_id and bootstrap_message is not None:
                message, prompt = bootstrap_message, None
            session_id = None

        workspace_factory = self.workspace_factory
        if self._discovery_policy is not None:
            supplied_workspace = workspace_factory

            @contextmanager
            def workspace_factory(model: str) -> Iterator[Path]:
                with self._configured(model, repo):
                    outer = supplied_workspace(model) if supplied_workspace else nullcontext(repo)
                    with outer as cwd:
                        with per_turn_workspace(Path(cwd), label=label, model_id=model) as workspace:
                            yield workspace

        elif workspace_factory is None and self.isolate_attempts:

            @contextmanager
            def workspace_factory(model: str) -> Iterator[Path]:  # type: ignore[misc]
                # Entered with the *attempt's* model in scope: the generated
                # config names it, and a fallback attempt must not inherit the
                # previous model's directory.
                with self._configured(model, repo):
                    with per_turn_workspace(repo, label=label, model_id=model) as workspace:
                        yield workspace

        with self._configured(model_id, repo):
            return run_turn_with_fallback(
                self.process,
                repo_path=str(repo),
                message=message,
                prompt_file=prompt,
                models=self.models,
                preferred_model=model_id,
                timeout=int(timeout_seconds or self.timeout),
                is_cancelled=is_cancelled,
                on_update=native_update,
                workspace_factory=workspace_factory,
                paid_attempts=paid_attempts,
                session_id=session_id,
                bootstrap_message=bootstrap_message,
                discovery_policy=self._discovery_policy,
                candidate_options=self._candidate_options,
                delivery=delivery,
                title=title,
                pure=pure,
                env=env,
                project_dir=project_dir,
            )

    def open_session(
        self,
        *,
        repo_path: Path,
        model_id: Optional[str] = None,
        permissions: Any = None,
        variant: Optional[str] = None,
        effort_strategy: str = "default",
        **_ignored: Any,
    ) -> HarnessSession:
        """Open a reusable OpenCode conversation behind the neutral contract."""
        repo = Path(repo_path).resolve()
        self = self._resolved_harness(repo, effort_strategy=effort_strategy)
        if self._discovery_policy is not None and (
            effort_strategy != "higher" or model_id not in self.models
        ):
            model_id = None
        candidates = tuple(self.models or ())
        if not candidates:
            with self._configured(model_id, repo):
                candidates = tuple(
                    opencode_model_id(candidate)
                    for candidate in available_provider_candidates()
                )
        if model_id:
            candidates = (model_id,) + tuple(
                candidate for candidate in candidates if candidate != model_id
            )
        if not candidates:
            candidates = (model_id or "",)

        def open_candidate(candidate: str) -> OpenCodeHarnessSession:
            with self._configured(candidate or None, repo):
                selected_variant = (
                    current_config().opencode_variant
                    if self._discovery_policy is not None else variant
                )
                return self._open_single_session(
                    repo=repo,
                    model_id=candidate or None,
                    permissions=permissions,
                    variant=selected_variant,
                )

        if len(candidates) > 1 or self._discovery_policy is not None:
            return FallbackHarnessSession(
                open_candidate, models=candidates, discovery_policy=self._discovery_policy
            )
        return open_candidate(candidates[0])

    def _open_single_session(
        self,
        *,
        repo: Path,
        model_id: Optional[str],
        permissions: Any,
        variant: Optional[str],
    ) -> "OpenCodeHarnessSession":
        if self.session_client_factory is None:
            # Kept local to this implementation so importing the neutral
            # session contract never imports an OpenCode client.
            from agent_core.harness.client import OpenCodeClient

            client = OpenCodeClient(
                repo_path=str(repo),
                config=self._config_for(model_id, repo),
            )
        else:
            parameters = tuple(
                inspect.signature(self.session_client_factory).parameters.values()
            )
            accepts_model = any(
                parameter.kind is inspect.Parameter.VAR_POSITIONAL
                for parameter in parameters
            ) or len(parameters) >= 2
            client = (
                self.session_client_factory(repo, model_id)
                if accepts_model
                else self.session_client_factory(repo)
            )
        return OpenCodeHarnessSession(
            client,
            repo_path=repo,
            model_id=model_id,
            permissions=permissions,
            variant=variant,
            timeout=self.timeout,
            lock_model=self._discovery_policy is not None,
        )


class OpenCodeHarnessSession:
    """Translate one OpenCode client conversation into reusable harness turns."""

    def __init__(
        self,
        client: Any,
        *,
        repo_path: Path,
        model_id: Optional[str],
        permissions: Any,
        variant: Optional[str],
        timeout: int,
        lock_model: bool = False,
    ) -> None:
        self._client = client
        self._repo_path = Path(repo_path).resolve()
        self._model_id = model_id
        self._variant = variant
        self._lock_model = lock_model
        self._timeout = timeout
        self._closed = False
        self._provider_cost_usd: float | None = 0.0
        native_permissions = (
            dict(permissions) if isinstance(permissions, Mapping) else permissions
        )
        self.session_id = client.create_session(
            model_id=model_id,
            permission=native_permissions,
            variant=variant,
        )

    def run_turn(
        self,
        *,
        prompt_file: Path,
        repo_path: Path,
        model_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        stalled_no_progress_seconds: Optional[int] = None,
        on_progress: Optional[Callable[[TurnProgress], None]] = None,
        on_update: Optional[Callable[[str], None]] = None,
        **_ignored: Any,
    ) -> TurnResult:
        if self._closed:
            raise RuntimeError("cannot run a turn on a closed harness session")
        if Path(repo_path).resolve() != self._repo_path:
            raise ValueError("a harness session cannot be reused for another repository")
        if on_progress is not None and on_update is not None:
            raise ValueError("pass only one progress callback: on_progress or on_update")
        if is_cancelled is not None and is_cancelled():
            return TurnResult(
                type="cancelled",
                session_id=self.session_id,
                session_refs=self._session_refs(),
            )

        native_update = on_update
        if on_progress is not None:
            native_update = lambda message: on_progress(_translate_progress(message))

        prompt = Path(prompt_file).read_text(encoding="utf-8")
        selected_model = self._model_id if self._lock_model else model_id or self._model_id
        self._client.send_message(
            self.session_id,
            prompt,
            model_id=selected_model,
            variant=self._variant,
        )
        poll_kwargs = {
            "timeout": int(timeout_seconds or self._timeout),
            "on_update": native_update,
        }
        optional_policy = {
            "is_cancelled": is_cancelled,
            "stalled_no_progress_seconds": stalled_no_progress_seconds,
        }
        parameters = inspect.signature(self._client.poll_completion).parameters.values()
        accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
        accepted_names = {parameter.name for parameter in parameters}
        poll_kwargs.update(
            {
                name: value
                for name, value in optional_policy.items()
                if value is not None and (accepts_kwargs or name in accepted_names)
            }
        )
        event = self._client.poll_completion(self.session_id, **poll_kwargs)
        result = self._turn_result(event, selected_model)
        if result.cost_usd is None:
            self._provider_cost_usd = None
        elif self._provider_cost_usd is not None:
            self._provider_cost_usd += float(result.cost_usd)
        return result

    def _turn_result(self, event: Mapping[str, Any], model_id: Optional[str]) -> TurnResult:
        latest = self._client.latest_turn_result(self.session_id)
        event_type = str(event.get("type") or getattr(latest, "type", "error"))
        error = event.get("error")
        if error is None and latest is not None:
            error = latest.error
        return TurnResult(
            type=event_type,
            result=str(event.get("result") or getattr(latest, "result", "") or ""),
            session_id=self.session_id,
            model_id=getattr(latest, "model_id", None) or model_id,
            cost_usd=getattr(latest, "cost_usd", None),
            tokens=dict(getattr(latest, "tokens", {}) or {}),
            error=error,
            patch_count=int(getattr(latest, "patch_count", 0) or 0),
            fallback_eligible=bool(
                event.get("fallback_eligible", getattr(latest, "fallback_eligible", False))
            ),
            fallback_reason=event.get("fallback_reason")
            or getattr(latest, "fallback_reason", None),
            raw_log_path=event.get("raw_log_path")
            or getattr(latest, "raw_log_path", None),
            session_refs=self._session_refs(),
        )

    def initialize(self, timeout_seconds: int) -> TurnResult:
        """Run OpenCode's own repository initialisation in this session.

        `/init` frequently does its work through files rather than a reply, so
        an absent or failed completion is reported as an unfinished turn rather
        than raised: the caller looks at the repository, not at this text.
        """
        if self._closed:
            raise RuntimeError("cannot initialise a closed harness session")
        provider_id, model_id = _split_model(self._model_id)
        user_info = (
            self._client.send_message_and_get_user_info(
                self.session_id,
                INIT_INSTRUCTION,
                model_id=self._model_id,
            )
            or {}
        )
        reported = user_info.get("model") or {}
        self._client.init_session(
            self.session_id,
            message_id=str(user_info.get("id") or ""),
            provider_id=provider_id or str(reported.get("providerID") or ""),
            model_id=model_id or str(reported.get("modelID") or ""),
        )
        try:
            event = self._client.poll_completion(
                self.session_id, timeout=int(timeout_seconds)
            )
        except Exception as exc:
            logger.warning("initialisation did not report a completion: %s", exc)
            event = None
        return self._turn_result(
            event if isinstance(event, Mapping) else {}, self._model_id
        )

    def _session_refs(self) -> tuple:
        """This conversation, under both of its names, scoped honestly.

        `self.session_id` is a UUID this package minted to address the client
        conversation; it means nothing to a later process, so it is
        `PROCESS`-scoped. OpenCode assigns its own id when the first message is
        sent and writes it into its own database, so that one is `DURABLE` and
        is what makes a session diagnosable after a restart. Both are published:
        the wrapper id is what this process's logs say, and dropping it would
        make a live run harder to follow to save a field.

        The durable one appears only once OpenCode has told us — before the
        first turn there is nothing to publish, and a placeholder would be a
        locator that resolves to nothing.
        """
        refs = [
            AgentSessionRef(
                harness="opencode",
                locator=str(self.session_id),
                scope=SessionLocatorScope.PROCESS,
            )
        ]
        native = None
        accessor = getattr(self._client, "native_session_id", None)
        if callable(accessor):
            native = accessor(self.session_id)
        if native:
            refs.append(
                AgentSessionRef(
                    harness="opencode",
                    locator=str(native),
                    scope=SessionLocatorScope.DURABLE,
                )
            )
        return tuple(refs)

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=self.session_id,
            session_refs=self._session_refs(),
            usage=self._client.analyze_session_tokens(self.session_id),
            retrospect=self._client.analyze_session_retrospect(self.session_id),
            patch_count=int(self._client.get_session_patch_count(self.session_id) or 0),
            provider_cost_usd=self._provider_cost_usd,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client.delete_session(self.session_id)


def _split_model(model_id: Optional[str]) -> tuple[str, str]:
    """`"openai/gpt-5"` into the provider and the model the endpoint wants."""
    if not model_id:
        return "", ""
    provider, separator, model = str(model_id).partition("/")
    return (provider, model) if separator else ("", provider)


def _expand_named_edit_permissions(permissions: Mapping[str, Any]) -> dict[str, Any]:
    """Match named artifacts after OpenCode normalizes an isolated path."""

    expanded = dict(permissions)
    edit = expanded.get("edit")
    if not isinstance(edit, Mapping):
        return expanded
    edit_rules: dict[str, Any] = {}
    for pattern, action in edit.items():
        name = str(pattern)
        edit_rules[name] = action
        if (
            action == "allow"
            and name not in {"", "*"}
            and "/" not in name
            and "\\" not in name
            and not any(marker in name for marker in "*?[]")
        ):
            edit_rules[f"**/{name}"] = action
    expanded["edit"] = edit_rules
    return expanded


def create_opencode_harness(
    source: Union[HarnessSpec, ConfigSource] = None,
) -> OpenCodeHarness:
    """Build OpenCode from either the neutral API or its low-level config."""
    if not isinstance(source, HarnessSpec):
        return OpenCodeHarness(source)

    options = dict(source.options)
    discovery_runtime = None
    if "model_selection_config" in options:
        # Only host env and explicit factory options may influence discovery.
        # In particular, never let BaseSettings load a target checkout's .env.
        environ = dict(os.environ)
        path = options.pop("model_selection_config")
        if "model_coding_index_min" in options:
            environ["AGENT_MODEL_CODING_INDEX_MIN"] = options.pop("model_coding_index_min")
        selection_config = load_selection_config(path, environ=environ)
        # Discovery owns admission, but must not silently remove an operator's
        # explicitly configured cross-provider fallback during migration.
        from agent_core.harness.tiered_router import parse_provider_chain

        legacy_providers = {candidate.provider for candidate in parse_provider_chain(
            options.get("opencode_provider_chain", ""))}
        if legacy_providers - {provider.id for provider in selection_config.providers}:
            raise ValueError(
                "discovery omits configured fallback providers; register them with "
                "credentials and approved bindings in model_selection_config, or "
                "explicitly remove them from opencode_provider_chain"
            )
        discovery_runtime = DiscoveryRuntime(selection_config, environ=environ)
    elif "model_coding_index_min" in options or options.get("opencode_selection_mode") == "discovery":
        raise ValueError("discovery requires an absolute model_selection_config path")
    isolate_attempts = bool(options.pop("isolate_attempts", True))
    explicit_turn_log_dir = bool(options.get("opencode_turn_log_dir"))
    if not options.get("opencode_bin") and not options.get("opencode_spawn_cmd"):
        installed = shutil.which("opencode")
        bundled = Path.home() / ".opencode" / "bin" / "opencode"
        if installed or bundled.is_file():
            options["opencode_bin"] = installed or str(bundled)
    options.setdefault("opencode_provider_fallback_enabled", True)
    options.setdefault("opencode_pass_model_flag", False)
    options["agent_cache_dir"] = source.cache_dir
    base = HarnessConfig(_env_file=None, **options) if discovery_runtime is not None else HarnessConfig(**options)

    def config_for(model_id: Optional[str], repo_path: Path) -> HarnessConfig:
        repo = Path(repo_path).resolve()
        directories = [
            Path(raw).expanduser().resolve()
            for raw in (base.opencode_permission_dirs or "").split(",")
            if raw.strip()
        ]
        directories.extend(Path(path).expanduser().resolve() for path in source.readable_dirs)
        directories.append(repo)
        directories = list(dict.fromkeys(directories))

        if explicit_turn_log_dir:
            log_dir = Path(base.opencode_turn_log_dir).expanduser()
            if not log_dir.is_absolute():
                log_dir = repo / log_dir
        else:
            cache_dir = Path(source.cache_dir).expanduser()
            if not cache_dir.is_absolute():
                cache_dir = repo / cache_dir
            log_dir = cache_dir / "opencode_turns"

        permissions = _expand_named_edit_permissions(
            dict(source.permissions) or dict(base.opencode_permissions)
        )
        return base.model_copy(
            update={
                "opencode_model": model_id or base.opencode_model,
                "opencode_permissions": permissions,
                "opencode_permission_dirs": ",".join(str(path) for path in directories),
                "opencode_turn_log_dir": str(log_dir),
            }
        )

    return OpenCodeHarness(
        config_for,
        timeout=source.timeout_seconds,
        isolate_attempts=isolate_attempts,
        discovery_runtime=discovery_runtime,
    )
