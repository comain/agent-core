"""Provider fallback orchestration.

`OpenCodeProcess.run_turn` runs exactly one turn against one model. This walks a
chain of candidates, so a dead provider degrades into the next choice instead of
failing the task.

The upstream harness sets ``TurnResult.fallback_eligible`` but **nothing acts on
it** -- the capability was half-built and never wired up. A consumer's fork
implemented the orchestration; this is that logic, generalised.

Two rules matter more than the loop itself:

* **A cancelled turn never falls back.** An operator pressing stop must not move
  the spend to the next provider in the chain.
* **A provider that is unreachable is skipped entirely**, not retried once per
  model it offers. Ten models behind one refused connection is ten pointless
  process spawns.
"""

from __future__ import annotations

import logging
import random
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence

from agent_core.harness.cost import PaidAttempts
from agent_core.harness.process import (
    OpenCodeProcess,
    TurnResult,
    has_authoritative_auth_evidence,
    provider_http_error_evidence,
)
from agent_core.harness.sessions import DiscoveryExecutionPolicy, merge_session_refs
from agent_core.harness.tiered_router import (
    available_provider_candidates,
    is_model_healthy,
    is_model_permanently_unhealthy,
    last_successful_model,
    mark_model_success,
    mark_model_unhealthy,
    opencode_model_id,
    provider_candidates,
)

logger = logging.getLogger(__name__)

#: Markers meaning the provider endpoint itself could not be reached, as opposed
#: to the model rejecting the request. Only these justify abandoning a provider's
#: remaining models -- a 400 or a rate limit says nothing about its other models.
_CONNECTION_REFUSED = ("ConnectionRefused", "ECONNREFUSED")
_TOOL_CONTINUE_PROMPT = "Continue the current turn and complete the requested work."
_MAX_TOOL_CONTINUATIONS = 32


def _merge_tokens(left: dict, right: dict) -> dict:
    """Add token counters without assuming one provider's token shape."""
    merged = {}
    for key in set(left) | set(right):
        old, new = left.get(key), right.get(key)
        if isinstance(old, dict) or isinstance(new, dict):
            merged[key] = _merge_tokens(
                old if isinstance(old, dict) else {},
                new if isinstance(new, dict) else {},
            )
        elif isinstance(old, (int, float)) or isinstance(new, (int, float)):
            merged[key] = (old if isinstance(old, (int, float)) else 0) + (
                new if isinstance(new, (int, float)) else 0
            )
        else:
            merged[key] = new if new is not None else old
    return merged


def _merge_continuation(previous: Optional[TurnResult], current: TurnResult) -> TurnResult:
    """Fold one resumed OpenCode process into the logical agent turn."""
    if previous is None:
        return current
    current.tokens = _merge_tokens(previous.tokens, current.tokens)
    current.patch_count += previous.patch_count
    if previous.cost_usd is not None or current.cost_usd is not None:
        current.cost_usd = float(previous.cost_usd or 0) + float(current.cost_usd or 0)
    current.session_refs = merge_session_refs(previous.session_refs, current.session_refs)
    texts = [text for text in (previous.result, current.result) if text]
    current.result = "\n".join(texts)
    return current


def _provider_of(model_id: Optional[str]) -> str:
    if not model_id or "/" not in model_id:
        return ""
    return model_id.split("/", 1)[0]


def _is_transport_failure(error: Optional[dict]) -> bool:
    """Whether the provider itself was unreachable, as opposed to the model failing.

    The distinction decides whether to skip one model or the whole provider.
    """
    if not error:
        return False
    blob = repr(error)
    return any(marker in blob for marker in _CONNECTION_REFUSED)


def _is_empty_stream_failure(result: TurnResult) -> bool:
    if result.fallback_reason not in {"provider_error", "provider_transport_error"}:
        return False
    return "empty_stream" in repr(result.error or {}).lower()


def should_skip_provider(result: TurnResult) -> bool:
    """Whether the remaining models of this provider are worth attempting.

    The consumer's original version keyed this on a specific provider name,
    which cannot ship in a shared package. What actually justifies abandoning a
    provider is that the connection was refused -- its other models are behind
    the same unreachable endpoint.
    """
    return (
        result.fallback_reason == "provider_auth_failed"
        and has_authoritative_auth_evidence(result.error)
    ) or (
        result.fallback_reason in {"provider_error", "provider_transport_error"}
        and _is_transport_failure(result.error)
    )


def _availability_reason(result: TurnResult) -> str:
    """Persist permanent auth quarantine only when the provider proved it."""
    reason = str(result.fallback_reason or "provider_error")
    if reason == "provider_auth_failed" and not has_authoritative_auth_evidence(result.error):
        return "provider_error"
    return reason


def _model_attempt(model: str, result: TurnResult) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "model": model,
        "outcome": str(result.type or "error"),
        "fallback_reason": str(result.fallback_reason or ""),
    }
    evidence = provider_http_error_evidence(result.error)
    if "httpStatus" in evidence:
        attempt["http_status"] = evidence["httpStatus"]
    if "errorCode" in evidence:
        attempt["error_code"] = evidence["errorCode"]
    if "errorDetail" in evidence:
        attempt["error_detail"] = evidence["errorDetail"]
    return attempt


def _record_discovery_result(
    policy: DiscoveryExecutionPolicy,
    model: str,
    candidates: Sequence[str],
    result: Any,
    *,
    observed_at: float,
) -> None:
    """Both execution paths publish the same existing failure classifications."""
    if getattr(result, "type", None) == "cancelled":
        return
    if getattr(result, "type", None) == "completed":
        policy.mark_success(model, observed_at=observed_at)
        return
    reason = getattr(result, "fallback_reason", None) or "provider_error"
    if not getattr(result, "fallback_eligible", False) and reason != "provider_auth_failed":
        return
    persisted_reason = _availability_reason(result)
    policy.mark_unhealthy(model, reason=persisted_reason)
    if _provider_of(model) and should_skip_provider(result):
        for candidate in candidates:
            if candidate != model and _provider_of(candidate) == _provider_of(model):
                policy.mark_unhealthy(
                    candidate,
                    reason="provider_auth_failed" if reason == "provider_auth_failed" else "provider_error",
                )


def run_turn_with_fallback(
    process: OpenCodeProcess,
    *,
    repo_path: str,
    message: Optional[str] = None,
    prompt_file: Optional[Path] = None,
    models: Optional[Sequence[str]] = None,
    preferred_model: Optional[str] = None,
    timeout: int = 3600,
    is_cancelled: Optional[Callable[[], bool]] = None,
    attachments: Optional[Sequence[str]] = None,
    on_update: Optional[Callable[[str], None]] = None,
    workspace_factory: Optional[Callable[[str], Any]] = None,
    paid_attempts: Optional[PaidAttempts] = None,
    session_id: Optional[str] = None,
    bootstrap_message: Optional[str] = None,
    discovery_policy: Optional[DiscoveryExecutionPolicy] = None,
    candidate_options: Optional[Callable[[str], Mapping[str, Any]]] = None,
    **turn_kwargs: Any,
) -> TurnResult:
    """Run a turn, walking the provider chain until one succeeds.

    ``models`` overrides the chain; otherwise healthy candidates are used, with
    ``preferred_model`` first when supplied.

    ``discovery_policy`` requires explicit ordered ``models`` and disables all
    legacy health, preferred/last-success promotion, and all-unhealthy retries.
    Duplicate IDs are visited once (the existing empty-stream retry is retained).
    ``candidate_options(model)`` supplies per-model run-turn options, including
    ``variant``, overriding generic ``turn_kwargs`` but not orchestration fields.
    It is called before billing and its errors propagate without quarantine.

    ``paid_attempts`` admits and records each candidate separately. A chain
    that fails over twice submitted three times and paid three times; counting
    the chain as one attempt is how a cap approves spend that already happened.

    ``workspace_factory`` is a context manager per attempt, taking the model id
    and yielding the working directory. Supply it when the generated
    ``opencode.json`` names the model -- otherwise every attempt in the chain
    would run against the *first* candidate's config, and a fallback would
    silently re-run the model that just failed. See
    :func:`agent_core.harness.workspace.per_turn_workspace`.
    """
    deadline = time.monotonic() + timeout
    # `is not None`, not truthiness: an explicitly empty list means "no
    # candidates", which must not silently fall through to the default chain.
    if discovery_policy is not None:
        if models is None:
            raise ValueError("discovery execution requires explicit ordered models")
        candidates = list(dict.fromkeys(models))
        preferred_model = None
    elif models is not None:
        candidates = [
            model
            for model in models
            if not is_model_permanently_unhealthy(model)
        ]
    else:
        candidates = [opencode_model_id(c) for c in available_provider_candidates()]
        if not candidates:
            configured = [
                opencode_model_id(candidate)
                for candidate in provider_candidates()
                if not is_model_permanently_unhealthy(
                    opencode_model_id(candidate)
                )
            ]
            if configured and not any(is_model_healthy(m) for m in configured):
                candidates = configured
    if discovery_policy is None and preferred_model is None:
        last_success = last_successful_model()
        if last_success in candidates:
            preferred_model = last_success
    if preferred_model:
        candidates = [preferred_model] + [m for m in candidates if m != preferred_model]
    if not candidates:
        return TurnResult(
            type="error",
            model_id=preferred_model,
            error={"message": "no opencode model candidate available"},
        )

    ledger = paid_attempts or PaidAttempts()
    last: Optional[TurnResult] = None
    seen_refs: tuple = ()
    model_attempts: list[dict[str, Any]] = []
    dead_providers: set = set()
    first_attempt = True
    pending_fallback: Optional[tuple[str, str]] = None

    for index, model in enumerate(candidates):
        provider = _provider_of(model)
        if provider and provider in dead_providers:
            # Already proven unreachable this call; do not spawn a process to
            # rediscover that.
            if discovery_policy is None:
                mark_model_unhealthy(model, reason="provider_error")
            continue
        unavailable = (
            not discovery_policy.is_healthy(model)
            if discovery_policy is not None
            else any(is_model_healthy(m) for m in candidates) and not is_model_healthy(model)
        )
        if unavailable:
            # A skipped continue target is failover: later models must not
            # inherit session_id, and they take bootstrap_message when set.
            first_attempt = False
            continue

        if pending_fallback is not None and on_update is not None:
            previous_model, reason = pending_fallback
            on_update(f"model-fallback: {previous_model} -> {model} reason={reason}")
        pending_fallback = None

        this_session = session_id if first_attempt else None
        this_message = message
        this_file = prompt_file
        if not first_attempt and bootstrap_message is not None:
            this_message = bootstrap_message
            this_file = None
        first_attempt = False
        options = dict(turn_kwargs)
        if candidate_options is not None:
            options.update(candidate_options(model))
        turn = dict(
            options,
            prompt_file=this_file,
            session_id=this_session,
            model_id=model,
            timeout=timeout,
            is_cancelled=is_cancelled,
            attachments=attachments,
            on_update=on_update,
        )

        same_model_retries = 0
        result: Optional[TurnResult] = None
        while True:
            workspace = workspace_factory(model) if workspace_factory is not None else nullcontext(repo_path)
            with workspace as cwd:
                accumulated: Optional[TurnResult] = None
                continuation_message = this_message
                continuation_turn = dict(turn)
                for continuation in range(_MAX_TOOL_CONTINUATIONS + 1):
                    # Observe before the health read: a concurrent failure
                    # after this point must survive this submission's success.
                    observed_at = time.time() if discovery_policy is not None else 0.0
                    if discovery_policy is not None and not discovery_policy.is_healthy(model):
                        if accumulated is not None:
                            accumulated = _merge_continuation(
                                accumulated,
                                TurnResult(
                                    type="error", model_id=model,
                                    session_id=accumulated.session_id,
                                    error={"message": "model unavailable before continuation"},
                                    fallback_eligible=not accumulated.patch_count,
                                    fallback_reason="model_unavailable",
                                ),
                            )
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        previous = accumulated if accumulated is not None else last
                        exhausted = TurnResult(
                            type="timeout",
                            model_id=previous.model_id if previous else None,
                            session_id=previous.session_id if previous else None,
                            fallback_reason="timeout",
                            error={"message": "turn deadline exhausted"},
                            model_attempts=tuple(model_attempts),
                            session_refs=seen_refs,
                        )
                        return _merge_continuation(previous, exhausted)
                    continuation_turn["timeout"] = min(timeout, remaining)
                    with ledger.submission() as attempt:
                        current = process.run_turn(
                            continuation_message,
                            repo_path=str(cwd),
                            **continuation_turn,
                        )
                        attempt.record_provider(getattr(current, "cost_usd", None))
                    accumulated = _merge_continuation(accumulated, current)
                    if current.type != "incomplete":
                        break
                    if not current.session_id or continuation >= _MAX_TOOL_CONTINUATIONS:
                        accumulated = _merge_continuation(
                            accumulated,
                            TurnResult(
                                type="stalled",
                                session_id=current.session_id,
                                model_id=model,
                                fallback_eligible=False,
                                error={
                                    "name": "OpenCodeToolContinuationLimit",
                                    "data": {
                                        "message": "OpenCode did not reach a terminal stop after tool continuations",
                                        "continuations": continuation,
                                    },
                                },
                            ),
                        )
                        break
                    continuation_message = _TOOL_CONTINUE_PROMPT
                    continuation_turn["prompt_file"] = None
                    continuation_turn["session_id"] = current.session_id
                if accumulated is None:
                    break
                result = accumulated
            if result.model_id is None:
                result.model_id = model
            nested_attempts = tuple(getattr(result, "model_attempts", ()) or ())
            if nested_attempts:
                model_attempts.extend(dict(item) for item in nested_attempts)
            else:
                model_attempts.append(
                    {
                        **_model_attempt(model, result),
                    }
                )
            # Carried forward before retry or failover: an abandoned candidate
            # was still paid for and its session remains useful diagnostics.
            seen_refs = merge_session_refs(seen_refs, result.session_refs)
            result.session_refs = seen_refs
            result.model_attempts = tuple(model_attempts)
            if (
                result.type == "cancelled"
                or same_model_retries >= 1
                or not _is_empty_stream_failure(result)
            ):
                break
            same_model_retries += 1
            logger.warning("retrying model=%s after empty_stream", model)
            time.sleep(random.uniform(1.0, 3.0))
            turn["session_id"] = None
            if bootstrap_message is not None:
                this_message = bootstrap_message
                turn["prompt_file"] = None
        if result is None:
            continue
        last = result

        has_next = index + 1 < len(candidates)
        if discovery_policy is not None:
            _record_discovery_result(
                discovery_policy, model, candidates, result, observed_at=observed_at
            )
        elif result.type == "completed":
            mark_model_success(model)
        if (
            discovery_policy is None
            and result.fallback_reason == "provider_auth_failed"
            and has_authoritative_auth_evidence(result.error)
        ):
            for candidate in candidates:
                if _provider_of(candidate) == provider:
                    mark_model_unhealthy(
                        candidate,
                        reason="provider_auth_failed",
                    )
        if result.type == "cancelled" or not result.fallback_eligible or not has_next:
            return result

        logger.warning(
            "falling back from model=%s reason=%s evidence=%s",
            model,
            result.fallback_reason or "provider_error",
            provider_http_error_evidence(result.error) or "none",
        )
        pending_fallback = (model, result.fallback_reason or "provider_error")
        if discovery_policy is None:
            mark_model_unhealthy(model, reason=_availability_reason(result))
        if provider and should_skip_provider(result):
            dead_providers.add(provider)

    return last or TurnResult(
        type="error", model_id=preferred_model, error={"message": "no opencode model candidate"}
    )
